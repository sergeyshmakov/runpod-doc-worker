"""Debug / observability helpers.

Almost everything here is best-effort — operator tooling that should never
crash the request path. The probe payload is the big one: when the effective
worker policy enables it and a job has ``probe: true``, the handler returns a
filesystem dump of /runpod-volume so we can debug RunPod Cached Models setups
without shelling into a worker.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from typing import Any

from runpod_doc_worker import config as _config
from runpod_doc_worker.obs import model_cache, probe_limits
from runpod_doc_worker.obs.dirwalk import _is_dir, _scan
from runpod_doc_worker.obs.model_cache import (
    _hub_cache_path,
    _newest,  # noqa: F401 - re-exported; consumers patch it through this module
    _resolve_snapshot_path,
    _snapshot_names,  # noqa: F401 - re-exported for the same reason
    find_model_dir,
    find_model_dirs,
)
from runpod_doc_worker.obs.probe_limits import (  # noqa: F401 - see _Module below
    PROBE_MAX_DEPTH,
    PROBE_MAX_ENTRIES,
    PROBE_MAX_MATCHES,
    PROBE_MAX_SNAPSHOTS,
    PROBE_MAX_VISITS,
)

# Declared, not incidental. Most of the names above are re-exported from the
# modules this one was split into, and an import with no local use is exactly what
# an unused-import autofix deletes -- which is how `find_model_dir` briefly stopped
# existing here, after being named in the README and the reference docs since the
# first release. `__all__` says the export is the point.
__all__ = [
    "PROBE_MAX_DEPTH",
    "PROBE_MAX_ENTRIES",
    "PROBE_MAX_MATCHES",
    "PROBE_MAX_SNAPSHOTS",
    "PROBE_MAX_VISITS",
    "collect_gpu_info",
    "find_model_dir",
    "find_model_dirs",
    "list_directory",
    "probe_filesystem",
]


class _Module(types.ModuleType):
    """This module, with the probe limits forwarded to where they are read.

    ``debug.PROBE_MAX_VISITS = 10`` used to change what ``find_model_dir`` saw,
    because the name was defined and read in one place. After the split it is read
    from ``probe_limits``, so the plain re-export above became a snapshot: an
    assignment landed here, the scan went on using 2000, and nothing said so.

    Reading is fine through the re-export -- the value only changes by assignment.
    It is *writing* that has to be forwarded, and a module has no ``__setattr__``
    hook, so the module's class is replaced with this one. That is the documented
    way to do it and the only one that keeps the old control point working.
    """

    def __setattr__(self, name: str, value: object) -> None:
        target = _FORWARDED.get(name)
        if target is not None:
            setattr(target, name, value)
        super().__setattr__(name, value)


# Every re-exported name, and the module that actually reads it. The limits were
# forwarded first; the helpers were not, and a consumer patching `debug._newest`
# -- which this module's own comments promise -- got a binding nothing reads,
# because `find_model_dir` executes with `model_cache` globals. Fourth instance of
# one shape, so the mapping is now the whole re-export list rather than the subset
# that had been reported.
_FORWARDED = {
    "PROBE_MAX_DEPTH": probe_limits,
    "PROBE_MAX_ENTRIES": probe_limits,
    "PROBE_MAX_MATCHES": probe_limits,
    "PROBE_MAX_SNAPSHOTS": probe_limits,
    "PROBE_MAX_VISITS": probe_limits,
    "_hub_cache_path": model_cache,
    "_newest": model_cache,
    "_resolve_snapshot_path": model_cache,
    "_snapshot_names": model_cache,
    "find_model_dir": model_cache,
    "find_model_dirs": model_cache,
}

sys.modules[__name__].__class__ = _Module


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


