"""Debug / observability helpers.

Almost everything here is best-effort — operator tooling that should never
crash the request path. The probe payload is the big one: when a job has
``probe: true``, the handler returns a filesystem dump of /runpod-volume so
we can debug RunPod Cached Models setups without shelling into a worker.
"""

from __future__ import annotations

import functools
import os
from collections import deque
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

    # Every filesystem read here is inside the guard, including the iteration
    # rather than only the call that starts it. This runs on the response path
    # of a successful job — an unreadable cache directory must cost the
    # `model_dir` field, not the job.
    try:
        matches = [p for glob in globs for p in hub.glob(glob)]
        if not matches:
            return None
        # If multiple model dirs are cached, report the most recently used one
        # — that's the one the library most likely resolved to.
        best = max(matches, key=lambda p: p.stat().st_mtime)
        snapshots = best / "snapshots"
        if snapshots.is_dir():
            snap_dirs = [d for d in snapshots.iterdir() if d.is_dir()]
            if snap_dirs:
                return str(max(snap_dirs, key=lambda p: p.stat().st_mtime))
        return str(best)
    except OSError:
        return None


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
    refs_main_unreadable = False
    if refs_main.is_file():
        try:
            out["refs_main_content"] = refs_main.read_text(encoding="utf-8").strip()
        except OSError as e:
            # Kept as the diagnosis rather than written into the field a hash
            # belongs in. Storing the error text there made the resolution
            # branch below report "stale refs/main", replacing a permission or
            # volume error with a wrong answer — in exactly the conditions this
            # probe exists to explain.
            refs_main_unreadable = True
            out["issue"] = f"refs/main could not be read: {type(e).__name__}: {e}"

    snapshots_dir = model_root / "snapshots"
    out["snapshots_dir_exists"] = snapshots_dir.is_dir()
    if out["snapshots_dir_exists"]:
        try:
            # Bounded like every other listing here, and bounded on entries
            # read rather than on subdirectories kept: a cache holding
            # thousands of stray files must not decide how long the diagnostic
            # takes just because few of them are directories.
            entries, _ = _scan(snapshots_dir, PROBE_MAX_ENTRIES)
            out["snapshot_subdirs"] = sorted(e.name for e in entries if _is_dir(e))
        except OSError as e:
            out["issue"] = f"snapshots/ iter error: {e}"
            return out

    if refs_main_unreadable:
        # No hash to resolve from, and guessing past the failure would bury it.
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
    _list = list_directory

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
            out["models_found"], note = find_model_dirs(root)
            if note:
                # A partial answer that reads as complete would send an
                # operator looking in the wrong place.
                out["models_search_note"] = note
        except (PermissionError, OSError) as e:
            out["models_found_error"] = f"{type(e).__name__}: {e}"

    return out


# How far under a search root the probe will look, how many hits it will
# report, and how much of any one directory it will list. All three are bounds
# on a diagnostic that runs against a network volume of unknown size while a
# caller waits for the response.
PROBE_MAX_DEPTH = 4
PROBE_MAX_MATCHES = 20
PROBE_MAX_ENTRIES = 50
# Directory entries the model search will look at before giving up. This is
# the bound that survives a volume with no models in it at all.
PROBE_MAX_VISITS = 2000
PROBE_MAX_SNAPSHOTS = 5


def _scan(directory: Path, max_entries: int) -> tuple[list[Any], bool]:
    """Up to ``max_entries`` raw directory entries. Returns ``(entries, more)``.

    Uses ``os.scandir`` rather than ``Path.iterdir`` because it is the only one
    that is lazy on every interpreter this package supports: ``iterdir`` is a
    generator through 3.12 and materialises the whole scandir result from 3.13,
    so slicing it bounds the response and not the work.

    The cap counts entries **read**, before any filtering. Filtering first and
    slicing after counts only what survives the filter, so a directory holding
    ten thousand files and three subdirectories is read in full to prove there
    is no fourth subdirectory.
    """
    entries: list[Any] = []
    more = False
    with os.scandir(directory) as scan:
        for entry in scan:
            if len(entries) >= max_entries:
                more = True
                break
            entries.append(entry)
    return entries, more


def _is_dir(entry: Any) -> bool:
    """``entry.is_dir()`` without letting a stat failure escape."""
    try:
        return entry.is_dir()
    except OSError:
        return False


def list_directory(p: Path, max_entries: int = PROBE_MAX_ENTRIES) -> list[str] | str:
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


def _snapshot_names(model_dir: Path, limit: int = PROBE_MAX_SNAPSHOTS) -> list[str]:
    """Up to ``limit`` snapshot directory names, from a bounded read.

    Two caps, because they bound different things: ``PROBE_MAX_ENTRIES`` bounds
    what is read from the directory, ``limit`` bounds what is reported from it.
    A snapshots directory full of ordinary files yields fewer than ``limit``
    names and still stops, where a limit applied after the is-a-directory
    filter would have read to the end looking for one more.
    """
    snapshots = model_dir / "snapshots"
    try:
        entries, _ = _scan(snapshots, PROBE_MAX_ENTRIES)
    except OSError:
        return []
    return [e.name for e in entries if _is_dir(e)][:limit]


def find_model_dirs(
    root: Path,
    max_depth: int = PROBE_MAX_DEPTH,
    limit: int = PROBE_MAX_MATCHES,
    max_visits: int = PROBE_MAX_VISITS,
) -> tuple[list[dict[str, Any]], str | None]:
    """Model directories under ``root``. Returns ``(found, note)``.

    Bounded three ways, because two were not enough. Depth stops it descending
    forever; ``limit`` stops it once it has enough answers; and ``max_visits``
    stops it looking. That third bound is the one a glob cannot express: a
    lazy pattern can only be cut short by *yielding*, so a tree with no matches
    at all yields nothing and gets enumerated in full to prove a negative — and
    a volume with no models is precisely what an operator probes.

    So this walks explicitly rather than globbing per level. ``note`` is
    non-None when a budget stopped the search, because a partial answer that
    looks complete is worse than no answer: "no models found" and "no models
    found in the first 2000 directories" lead to different next steps.

    A directory that matches is recorded and not descended into.
    """
    found: list[dict[str, Any]] = []
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    visits = 0

    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue

        # os.scandir rather than Path.iterdir: it is the only one that is lazy
        # on every supported interpreter, so the visit budget below bounds the
        # work and not just the answer. iterdir is a generator through 3.12 and
        # materialises the whole directory from 3.13.
        #
        # The iteration is inside the guard, not just the call that starts it.
        # On the versions where iterdir is lazy the error arrives on advance,
        # and one unreadable directory would otherwise abort the whole search
        # and discard every model already found.
        try:
            with os.scandir(current) as scan:
                for entry in scan:
                    visits += 1
                    if visits > max_visits:
                        return found, (
                            f"search stopped after visiting {max_visits} "
                            f"directory entries; results are partial"
                        )
                    if not _is_dir(entry):
                        continue

                    path = Path(entry.path)
                    if entry.name.startswith("models--"):
                        found.append({
                            "path": str(path),
                            "depth": depth + 1,
                            "snapshots": _snapshot_names(path),
                        })
                        if len(found) >= limit:
                            return found, (
                                f"stopped at the {limit}-match limit; there may "
                                f"be more"
                            )
                    else:
                        queue.append((path, depth + 1))
        except OSError:
            # This subtree is unreadable. That costs this subtree; the queue
            # and everything already found survive.
            continue

    return found, None
