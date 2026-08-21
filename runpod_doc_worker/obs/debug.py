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
from fnmatch import fnmatch
from itertools import islice as _islice
from pathlib import Path
from typing import Any, Iterable

from runpod_doc_worker import config as _config
from runpod_doc_worker import paths as _paths


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


def _hub_cache_path() -> Path:
    """The Hub repository cache, using huggingface_hub's environment order."""
    def expanded(value: str) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(value)))

    if hub_cache := os.environ.get("HF_HUB_CACHE", "").strip():
        return expanded(hub_cache)
    if legacy_cache := os.environ.get("HUGGINGFACE_HUB_CACHE", "").strip():
        return expanded(legacy_cache)

    if hf_home := os.environ.get("HF_HOME", "").strip():
        home = expanded(hf_home)
    elif xdg_cache := os.environ.get("XDG_CACHE_HOME", "").strip():
        home = expanded(xdg_cache) / "huggingface"
    else:
        home = expanded("~/.cache/huggingface")
    return home / "hub"


def _model_candidate(path: Path, partial_scans: list[str]) -> str:
    """Describe ``path`` without presenting a bounded search as complete."""
    if not partial_scans:
        return str(path)
    return f"<partial cache scan; candidate={path}; {'; '.join(partial_scans)}>"


@functools.lru_cache(maxsize=1)
def find_model_dir() -> str | None:
    """Locate the model snapshot in the Hub cache so we can prove which weights
    actually loaded, rather than which ones were meant to.

    Matches ``config.model_globs`` against ``$HF_HUB_CACHE`` when set, then the
    cache derived from ``HF_HOME`` or the platform default. A worker that
    declares no globs gets ``None`` and no directory walk.

    Cached because the model dir doesn't change after worker boot and reading
    the cache is non-trivial on a cold or network-backed volume. The cache is
    keyed on nothing, so a test that reconfigures the worker calls
    ``find_model_dir.cache_clear()`` first.
    """
    globs = _config.active().model_globs
    if not globs:
        return None
    hub = _hub_cache_path()
    if not hub.is_dir():
        return None

    # Every filesystem read here is inside the guard, including the iteration
    # rather than only the call that starts it. This runs on the response path
    # of a successful job — an unreadable cache directory must cost the
    # `model_dir` field, not the job.
    try:
        # Bounded like everything else that reads a directory here. `hub.glob`
        # would enumerate the whole cache — and retain every match — before one
        # was chosen, which on a shared volume is a large read on the first
        # job's response path. Patterns are matched against entry names, so a
        # model glob is a name pattern rather than a path pattern.
        entries, hub_truncated = _scan(hub, PROBE_MAX_VISITS)
        matches = [
            Path(e.path) for e in entries
            # A model is a directory. A regular file whose name matches — a
            # partially written cache entry, say — is not a candidate, and with
            # a newer mtime it would otherwise have been reported as the loaded
            # model directory.
            # Resolved containment, not just a name match: a symlinked entry
            # reports a path that reads as though it were inside the cache
            # while pointing somewhere else, which for the field that says
            # where the weights are is a wrong answer rather than a missing
            # one.
            if _is_dir(e)
            and any(fnmatch(e.name, glob) for glob in globs)
            and _paths.within(hub, Path(e.path))
        ]
        if not matches:
            # "Not here" and "not in the part we looked at" are different
            # answers, and the bound above makes the second one possible. A
            # cache large enough to truncate is exactly the one where a
            # definitive-sounding miss would send someone looking in the wrong
            # place — which is the whole failure this field exists to prevent.
            if hub_truncated:
                return (
                    f"<not found in the first {PROBE_MAX_VISITS} entries of "
                    f"{hub}; scan truncated>"
                )
            return None
        # If multiple model dirs are cached, report the most recently used one
        # — that's the one the library most likely resolved to. A candidate
        # that cannot be statted is skipped rather than raising: it is not an
        # answer, but it must not take the others down with it.
        best = _newest(matches)
        if best is None:
            if hub_truncated:
                return (
                    f"<no usable match in the first {PROBE_MAX_VISITS} entries "
                    f"of {hub}; scan truncated>"
                )
            return None
        partial_scans: list[str] = []
        if hub_truncated:
            partial_scans.append(
                f"Hub listing truncated after {PROBE_MAX_VISITS} entries"
            )
        snapshots = best / "snapshots"
        if not _paths.within(best, snapshots):
            return (
                f"<invalid cache; model candidate={best}; snapshots directory "
                "resolves outside the model>"
            )
        if snapshots.is_dir():
            # Bounded like every other listing in this module, and for a
            # sharper reason: this one runs while building the first successful
            # response, so an unexpectedly large cache would stall a real job
            # rather than a diagnostic. The cost is that "most recently used"
            # becomes "most recently used among the first PROBE_MAX_ENTRIES" —
            # acceptable for a field that reports which weights a worker
            # appears to have loaded.
            # Guarded on its own rather than by the outer handler: failing to
            # read the snapshots directory says nothing about whether the model
            # directory was found, and answering `None` would throw away the
            # part that did work.
            try:
                entries, snapshots_truncated = _scan(
                    snapshots, PROBE_MAX_ENTRIES
                )
                if snapshots_truncated:
                    partial_scans.append(
                        "snapshot listing truncated after "
                        f"{PROBE_MAX_ENTRIES} entries"
                    )
                # This path is returned as the model actually loaded. Do not
                # let a directory symlink make a path outside the selected
                # cache look like one of its snapshots.
                newest = _newest(
                    Path(e.path) for e in entries if _is_dir_nofollow(e)
                )
                if newest is not None:
                    return _model_candidate(newest, partial_scans)
            except OSError:
                pass
        return _model_candidate(best, partial_scans)
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
    if not _paths.within(hub_root, model_root):
        out["issue"] = "model_root resolves outside the configured Hub cache"
        return out
    if not model_root.is_dir():
        out["issue"] = "model_root not present (RunPod didn't populate, or wrong casing)"
        return out
    out["model_root_exists"] = True

    refs_main = model_root / "refs" / "main"
    out["refs_main_path"] = str(refs_main)
    refs_main_unreadable = False
    if not _paths.within(model_root, refs_main):
        out["issue"] = "refs/main resolves outside the selected model root"
        return out
    if refs_main.is_file():
        try:
            out["refs_main_content"] = refs_main.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            # Kept as the diagnosis rather than written into the field a hash
            # belongs in. Storing the error text there made the resolution
            # branch below report "stale refs/main", replacing a permission or
            # volume error with a wrong answer — in exactly the conditions this
            # probe exists to explain.
            # OSError and UnicodeDecodeError both, because a cache corrupt
            # enough to be worth diagnosing is corrupt in both ways — and
            # UnicodeDecodeError is a ValueError, so an OSError-only guard let
            # it escape and fail the whole probe job.
            refs_main_unreadable = True
            out["issue"] = f"refs/main could not be read: {type(e).__name__}: {e}"

    snapshots_dir = model_root / "snapshots"
    snapshots_truncated = False
    if not _paths.within(model_root, snapshots_dir):
        out["issue"] = "snapshots/ resolves outside the selected model root"
        return out
    out["snapshots_dir_exists"] = snapshots_dir.is_dir()
    if out["snapshots_dir_exists"]:
        try:
            # Bounded like every other listing here, and bounded on entries
            # read rather than on subdirectories kept: a cache holding
            # thousands of stray files must not decide how long the diagnostic
            # takes just because few of them are directories.
            entries, snapshots_truncated = _scan(
                snapshots_dir, PROBE_MAX_ENTRIES
            )
            out["snapshot_subdirs"] = sorted(
                e.name for e in entries if _is_dir_nofollow(e)
            )
        except OSError as e:
            out["issue"] = f"snapshots/ iter error: {e}"
            return out

    if refs_main_unreadable:
        # No hash to resolve from, and guessing past the failure would bury it.
        return out

    # Resolution attempt 1: refs/main → snapshots/<hash>/
    if out["refs_main_content"] and isinstance(out["refs_main_content"], str):
        ref = out["refs_main_content"]
        # refs/main names one snapshot directory. Anything else is a corrupt
        # cache, and joining it blindly would follow it out: an absolute path
        # replaces the base entirely, so `/etc` resolved to `/etc` and, if it
        # happened to exist, was reported as a successful resolution with no
        # issue recorded — a wrong answer from the tool whose job is to be
        # right about this.
        if ref in (".", "..") or "/" in ref or "\\" in ref or Path(ref).is_absolute():
            out["issue"] = (
                f"refs/main does not name a snapshot directory; it contains "
                f"{ref!r}, which points outside snapshots/"
            )
            return out
        candidate = snapshots_dir / ref
        if not _paths.within(snapshots_dir, candidate):
            out["issue"] = (
                f"refs/main names {ref!r}, which resolves outside snapshots/ "
                f"(a link out of the cache)"
            )
            return out
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
        if not _paths.within(snapshots_dir, first):
            out["issue"] = "fallback snapshot resolves outside snapshots/"
            return out
        if not first.is_dir():
            out["issue"] = "fallback snapshot vanished before it could be resolved"
            return out
        out["resolved_path"] = str(first)
        out["resolution_method"] = "first snapshot subdir (fallback)"
        if snapshots_truncated:
            partial = (
                f"fallback selected from the first {PROBE_MAX_ENTRIES} entries; "
                "snapshots/ listing was truncated"
            )
            out["issue"] = f"{out['issue']}; {partial}" if out["issue"] else partial
        return out

    if out["issue"] is None:
        if snapshots_truncated:
            out["issue"] = (
                f"no snapshot directory found in the first {PROBE_MAX_ENTRIES} "
                "entries; snapshots/ listing was truncated"
            )
        else:
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


def _is_dir_nofollow(entry: Any) -> bool:
    """``entry.is_dir()`` that refuses to be led out of the tree by a symlink."""
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _newest(paths: Iterable[Path]) -> Path | None:
    """The most recently modified path, skipping any that cannot be statted.

    A cache entry can vanish or become unreadable between being listed and
    being examined. Letting that raise made one stale entry decide the whole
    answer — the function returned nothing, and a valid model directory sitting
    beside it went unreported.

    An element that cannot be read is not an answer, but it is not a failure
    either; it is simply not a candidate. Returns None only when nothing was
    usable.
    """
    best: Path | None = None
    best_mtime: float | None = None
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if best_mtime is None or mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


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


def _snapshot_names(
    model_dir: Path, limit: int = PROBE_MAX_SNAPSHOTS
) -> tuple[list[str], bool]:
    """Snapshot names and whether the bounded listing omitted any entries.

    Two caps, because they bound different things: ``PROBE_MAX_ENTRIES`` bounds
    what is read from the directory, ``limit`` bounds what is reported from it.
    A snapshots directory full of ordinary files yields fewer than ``limit``
    names and still stops, where a limit applied after the is-a-directory
    filter would have read to the end looking for one more.
    """
    snapshots = model_dir / "snapshots"
    if not _paths.within(model_dir, snapshots):
        return [], False
    try:
        entries, truncated = _scan(snapshots, PROBE_MAX_ENTRIES)
    except OSError:
        return [], False
    names = [e.name for e in entries if _is_dir_nofollow(e)]
    return names[:limit], truncated or len(names) > limit


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
                    # Links are not followed here, unlike everywhere else in
                    # this module that only reports an entry's type. This loop
                    # queues what it finds, so following a directory symlink
                    # would walk out of the root it was given and report models
                    # that live somewhere else entirely — and a probe that
                    # answers about a different volume than the one asked about
                    # is worse than one that answers nothing.
                    if not _is_dir_nofollow(entry):
                        continue

                    path = Path(entry.path)
                    if entry.name.startswith("models--"):
                        snapshots, snapshots_truncated = _snapshot_names(path)
                        found.append({
                            "path": str(path),
                            "depth": depth + 1,
                            "snapshots": snapshots,
                            "snapshots_truncated": snapshots_truncated,
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
