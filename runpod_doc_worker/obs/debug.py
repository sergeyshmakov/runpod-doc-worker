"""Debug / observability helpers.

Almost everything here is best-effort — operator tooling that should never
crash the request path. The probe payload is the big one: when the effective
worker policy enables it and a job has ``probe: true``, the handler returns a
filesystem dump of /runpod-volume so we can debug RunPod Cached Models setups
without shelling into a worker.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from runpod_doc_worker import config as _config
from runpod_doc_worker.obs import probe_limits
from runpod_doc_worker.obs.dirwalk import _is_dir, _scan
from runpod_doc_worker.obs.model_cache import (
    _hub_cache_path,
    _resolve_snapshot_path,
    find_model_dirs,
)


def collect_gpu_info() -> dict[str, Any]:
    """Best-effort GPU inventory for the response's `debug` block.

    Helps callers distinguish a 4090 from an A5000 from a Blackwell MIG slice
    without having to read worker logs. ``compute_capability`` is reported
    because attention-kernel compatibility turns on it, and a worker that
    lands on an unexpected architecture usually fails in a way that only
    makes sense once you know which card it got.
    """
    try:
        import torch  # noqa: PLC0415
        if not torch.cuda.is_available():
            return {"available": False}
        props = torch.cuda.get_device_properties(0)
        return {
            "available": True,
            "name": props.name,
            "compute_capability": f"{props.major}.{props.minor}",
            "total_memory_gb": round(props.total_memory / 1024**3, 2),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def probe_filesystem() -> dict[str, Any]:
    """Inspect /runpod-volume layout for Cached Models debugging.

    Returns whatever's actually on disk where the HF lookup expects it.
    Triggered by `probe: true` in the input. Used to diagnose
    LocalEntryNotFoundError on workers that have Cached Models configured but
    aren't finding the model.

    Safe to call without the engine installed. Read-only. No network.

    **Whether a caller may ask for this is the worker's decision, and this
    function does not make it.** The payload names worker-local paths and the
    env values a worker declared as diagnostic, so a worker that serves callers
    who should not see either gates the call itself — it knows who its callers
    are and this package cannot. That gate used to live here, read env vars
    this package named, and the naming and default of those vars then changed
    twice in two releases while the only thing anyone actually wanted was to
    decide for themselves.
    """
    _list = list_directory

    hf_home = os.environ.get("HF_HOME", "")
    hub_path = _hub_cache_path()

    out: dict[str, Any] = {
        "env": {
            "HF_HOME": hf_home,
            "HF_HUB_CACHE": os.environ.get("HF_HUB_CACHE", ""),
            "HUGGINGFACE_HUB_CACHE": os.environ.get("HUGGINGFACE_HUB_CACHE", ""),
            "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME", ""),
            "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE", ""),
            "TRANSFORMERS_OFFLINE": os.environ.get("TRANSFORMERS_OFFLINE", ""),
            "TRANSFORMERS_CACHE": os.environ.get("TRANSFORMERS_CACHE", ""),
            # Whatever else this worker considers diagnostic — model source,
            # model-name overrides, anything an operator can get wrong.
            **{k: os.environ.get(k, "") for k in _config.active().probe_env_keys},
        },
        "paths": {},
        "models_found": [],
        "resolution_attempts": [],
    }

    # Try the tutorial's snapshot resolver for each model this worker cares
    # about. Reports whether refs/main is stale, whether canonical casing is
    # present, and what (if anything) the engine's library would find.
    if hub_path.is_dir():
        for model_id in _config.active().probe_model_ids:
            out["resolution_attempts"].append(
                _resolve_snapshot_path(hub_path, model_id)
            )

    for label, path_str in (
        ("/runpod-volume", "/runpod-volume"),
        ("/runpod-volume/huggingface-cache", "/runpod-volume/huggingface-cache"),
        ("/runpod-volume/huggingface-cache/hub", "/runpod-volume/huggingface-cache/hub"),
        ("HF_HOME", hf_home),
        ("HF_HOME/hub", str(Path(hf_home) / "hub") if hf_home else ""),
        ("Hugging Face hub cache (effective)", str(hub_path)),
    ):
        if not path_str:
            out["paths"][label] = "<empty path>"
            continue
        p = Path(path_str)
        if not p.exists():
            out["paths"][label] = "<not present>"
            continue
        if not p.is_dir():
            out["paths"][label] = "<not a directory>"
            continue
        out["paths"][label] = _list(p)

    # Hunt for any `models--*` directories anywhere under /runpod-volume, to
    # catch the case where RunPod populated a different path than HF_HOME/hub.
    for search_root in ("/runpod-volume",):
        root = Path(search_root)
        if not root.is_dir():
            continue
        try:
            out["models_found"], note = find_model_dirs(root)
            if note:
                # A partial answer that reads as complete would send an
                # operator looking in the wrong place.
                out["models_search_note"] = note
        except (PermissionError, OSError) as e:
            out["models_found_error"] = f"{type(e).__name__}: {e}"

    return out


def list_directory(p: Path, max_entries: int = probe_limits.PROBE_MAX_ENTRIES) -> list[str] | str:
    """A bounded listing of one directory, for the probe payload.

    Enumeration stops at the limit rather than being trimmed to it. Sorting the
    directory first would mean materialising every entry in it before the cap
    applied — on a network volume holding hundreds of thousands of files, the
    bound would describe the response while the work stayed unbounded.

    The costs are stated rather than hidden: entries are sorted only within the
    slice that was read, so with more than ``max_entries`` present the listing
    is the filesystem's order, and the tail is reported as "more entries"
    without a count, because counting them is the walk being avoided.
    """
    try:
        # One extra tells us something was left behind without reading it all.
        entries, truncated = _scan(p, max_entries + 1)
    except OSError as e:
        return f"<error: {type(e).__name__}: {e}>"

    if len(entries) > max_entries:
        entries, truncated = entries[:max_entries], True

    result: list[str] = []
    for entry in sorted(entries, key=lambda e: e.name):
        kind = "d" if _is_dir(entry) else "f"
        try:
            size = entry.stat().st_size if not _is_dir(entry) else "-"
        except OSError:
            size = "?"
        result.append(f"{kind} {entry.name} {size}")
    if truncated:
        result.append(f"... (more entries elided; listing stops at {max_entries})")
    return result


