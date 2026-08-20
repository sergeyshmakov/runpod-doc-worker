"""Debug / observability helpers.

Almost everything here is best-effort — operator tooling that should never
crash the request path. The probe payload is the big one: when a job has
``probe: true``, the handler returns a filesystem dump of /runpod-volume so
we can debug RunPod Cached Models setups without shelling into a worker.
"""

from __future__ import annotations

import functools
import os
from itertools import islice as _islice
from pathlib import Path
from typing import Any

from runpod_doc_worker import config as _config


def probe_enabled() -> bool:
    """Whether this worker answers ``probe: true`` jobs.

    On by default — the probe payload is how an operator diagnoses a
    model-cache problem without shelling into a worker. Operators running an
    endpoint whose callers have no business seeing its disk layout set
    ``<PREFIX>_DISABLE_PROBE=1`` and get the normal error envelope instead.
    """
    return not _config.active().truthy("DISABLE_PROBE")


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


@functools.lru_cache(maxsize=1)
def find_model_dir() -> str | None:
    """Locate the model snapshot under HF_HOME so we can prove which weights
    actually loaded, rather than which ones were meant to.

    Matches ``config.model_globs`` against ``$HF_HOME/hub``; a worker that
    declares none gets ``None`` and no directory walk.

    Cached because the model dir doesn't change after worker boot and the
    rglob over ~/.cache/huggingface/hub is non-trivial on cold cache. The
    cache is keyed on nothing, so a test that reconfigures the worker calls
    ``find_model_dir.cache_clear()`` first.
    """
    globs = _config.active().model_globs
    if not globs:
        return None
    hf_home = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    hub = Path(hf_home) / "hub"
    if not hub.is_dir():
        return None
    matches = [p for glob in globs for p in hub.glob(glob)]
    if not matches:
        return None
    # If multiple model dirs are cached, report the most recently used one —
    # that's the one the library most likely resolved to.
    best = max(matches, key=lambda p: p.stat().st_mtime)
    snapshots = best / "snapshots"
    if snapshots.is_dir():
        snap_dirs = [d for d in snapshots.iterdir() if d.is_dir()]
        if snap_dirs:
            return str(max(snap_dirs, key=lambda p: p.stat().st_mtime))
    return str(best)


def _resolve_snapshot_path(hub_root: Path, model_id: str) -> dict[str, Any]:
    """Emulate the resolve_snapshot_path() helper from RunPod's tutorial.

    Returns a dict that says what the tutorial's algorithm would have found
    for `model_id` at `hub_root` — including whether refs/main is stale
    (points at a hash that doesn't exist in snapshots/).
    """
    out: dict[str, Any] = {
        "model_id": model_id,
        "expected_root": "",
        "model_root_exists": False,
        "refs_main_path": "",
        "refs_main_content": None,
        "snapshots_dir_exists": False,
        "snapshot_subdirs": [],
        "resolved_path": None,
        "resolution_method": None,
        "issue": None,
    }
    if "/" not in model_id:
        out["issue"] = f"model_id {model_id!r} not in org/name format"
        return out
    org, name = model_id.split("/", 1)
    model_root = hub_root / f"models--{org}--{name}"
    out["expected_root"] = str(model_root)
    if not model_root.is_dir():
        out["issue"] = "model_root not present (RunPod didn't populate, or wrong casing)"
        return out
    out["model_root_exists"] = True

    refs_main = model_root / "refs" / "main"
    out["refs_main_path"] = str(refs_main)
    if refs_main.is_file():
        try:
            out["refs_main_content"] = refs_main.read_text(encoding="utf-8").strip()
        except OSError as e:
            out["refs_main_content"] = f"<read error: {e}>"

    snapshots_dir = model_root / "snapshots"
    out["snapshots_dir_exists"] = snapshots_dir.is_dir()
    if out["snapshots_dir_exists"]:
        try:
            out["snapshot_subdirs"] = sorted(
                d.name for d in snapshots_dir.iterdir() if d.is_dir()
            )
        except OSError as e:
            out["issue"] = f"snapshots/ iter error: {e}"
            return out

    # Resolution attempt 1: refs/main → snapshots/<hash>/
    if out["refs_main_content"] and isinstance(out["refs_main_content"], str):
        candidate = snapshots_dir / out["refs_main_content"]
        if candidate.is_dir():
            out["resolved_path"] = str(candidate)
            out["resolution_method"] = "refs/main"
            return out
        out["issue"] = (
            f"refs/main points at {out['refs_main_content']!r} but "
            f"snapshots/{out['refs_main_content']}/ does not exist (stale refs/main)"
        )

    # Resolution attempt 2: first available snapshot subdir
    if out["snapshot_subdirs"]:
        first = snapshots_dir / out["snapshot_subdirs"][0]
        out["resolved_path"] = str(first)
        out["resolution_method"] = "first snapshot subdir (fallback)"
        return out

    if out["issue"] is None:
        out["issue"] = "no snapshots/ subdir or no entries inside it"
    return out


def probe_filesystem() -> dict[str, Any]:
    """Inspect /runpod-volume layout for Cached Models debugging.

    Returns whatever's actually on disk where the HF lookup expects it.
    Triggered by `probe: true` in the input. Used to diagnose
    LocalEntryNotFoundError on workers that have Cached Models configured but
    aren't finding the model.

    Safe to call without the engine installed. Read-only. No network.
    """
    def _list(p: Path, max_entries: int = 50) -> list[str] | str:
        try:
            entries = sorted(p.iterdir())
        except (PermissionError, FileNotFoundError) as e:
            return f"<error: {type(e).__name__}: {e}>"
        result: list[str] = []
        for entry in entries[:max_entries]:
            kind = "d" if entry.is_dir() else "f"
            try:
                size = entry.stat().st_size if entry.is_file() else "-"
            except OSError:
                size = "?"
            result.append(f"{kind} {entry.name} {size}")
        if len(entries) > max_entries:
            result.append(f"... ({len(entries) - max_entries} more entries elided)")
        return result

    hf_home = os.environ.get("HF_HOME", "")
    hub_path = Path(hf_home) / "hub" if hf_home else None

    out: dict[str, Any] = {
        "env": {
            "HF_HOME": hf_home,
            "HF_HUB_CACHE": os.environ.get("HF_HUB_CACHE", ""),
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
    if hub_path and hub_path.is_dir():
        for model_id in _config.active().probe_model_ids:
            out["resolution_attempts"].append(
                _resolve_snapshot_path(hub_path, model_id)
            )

    for label, path_str in (
        ("/runpod-volume", "/runpod-volume"),
        ("/runpod-volume/huggingface-cache", "/runpod-volume/huggingface-cache"),
        ("/runpod-volume/huggingface-cache/hub", "/runpod-volume/huggingface-cache/hub"),
        ("HF_HOME", hf_home),
        ("HF_HOME/hub", str(hub_path) if hub_path else ""),
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
            out["models_found"] = find_model_dirs(root)
        except (PermissionError, OSError) as e:
            out["models_found_error"] = f"{type(e).__name__}: {e}"

    return out


# How far under a search root the probe will look, and how many hits it will
# report. Both are bounds on a diagnostic that runs against a network volume of
# unknown size while a caller waits for the response.
PROBE_MAX_DEPTH = 4
PROBE_MAX_MATCHES = 20


def find_model_dirs(
    root: Path,
    max_depth: int = PROBE_MAX_DEPTH,
    limit: int = PROBE_MAX_MATCHES,
) -> list[dict[str, Any]]:
    """Model directories under ``root``, no deeper than ``max_depth``.

    Globs one level at a time rather than walking the tree and filtering after
    the fact. `rglob` descends everything it can reach before anything gets
    discarded, so on a large network volume the depth bound described the
    results while the traversal stayed unbounded — and a probe that finds
    nothing was the case that scanned the most.

    ``limit`` stops the enumeration rather than trimming its result: sorting a
    level first would mean visiting every entry in it before the cap could
    apply, which is the same mistake one layer down. The cost is that when a
    level holds more matches than the limit, which ones come back is the
    filesystem's order rather than sorted order. For a diagnostic answering
    "is anything here at all", a bounded answer beats a complete one.
    """
    found: list[dict[str, Any]] = []
    for depth in range(1, max_depth + 1):
        pattern = "/".join(["*"] * (depth - 1) + ["models--*"])
        # islice keeps the generator lazy, so enumeration stops with us.
        for path in _islice(root.glob(pattern), limit - len(found)):
            if not path.is_dir():
                continue
            snapshots = path / "snapshots"
            snap_names: list[str] = []
            if snapshots.is_dir():
                try:
                    snap_names = [d.name for d in snapshots.iterdir() if d.is_dir()][:5]
                except OSError:
                    pass
            found.append({
                "path": str(path),
                "depth": depth,
                "snapshots": snap_names,
            })
            if len(found) >= limit:
                return found
    return found
