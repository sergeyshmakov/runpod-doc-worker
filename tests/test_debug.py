"""The probe's model-directory search.

The probe answers a question an operator cannot answer from outside a worker,
and it runs against a network volume of unknown size while a caller waits. So
the bound that matters is on the traversal, not on the result set.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from runpod_doc_worker.obs import debug


def _depth(path: Path, root: Path) -> int:
    return len(path.resolve().relative_to(root.resolve()).parts)


def _traversed(opened: list[Path], root: Path) -> list[Path]:
    """Only the directories the search opened while walking.

    Reading a found model's `snapshots/` is not traversal — it sits under a
    match and is bounded separately — so counting it against the depth budget
    would fail the test for the wrong reason.
    """
    return [
        p for p in opened
        if not any(part.startswith("models--") for part in p.resolve().parts)
    ]


@pytest.fixture
def volume(tmp_path):
    """A volume with a model at depth 2 and a deep tree that has none."""
    (tmp_path / "hub" / "models--acme--parser" / "snapshots" / "abc123").mkdir(parents=True)
    deep = tmp_path
    for level in range(8):
        deep = deep / f"level{level}"
    deep.mkdir(parents=True)
    (deep / "models--acme--buried").mkdir()
    return tmp_path


def test_finds_a_model_within_the_depth_bound(volume):
    found, _ = debug.find_model_dirs(volume)
    assert [f["path"] for f in found] == [str(volume / "hub" / "models--acme--parser")]
    assert found[0]["depth"] == 2
    assert found[0]["snapshots"] == ["abc123"]


def test_a_model_below_the_depth_bound_is_not_reported(volume):
    found, _ = debug.find_model_dirs(volume)
    assert all("buried" not in f["path"] for f in found)


def _record_opened(monkeypatch) -> list[Path]:
    """Record every directory the search opens, keeping enumeration lazy.

    Must wrap os.scandir, which is what the walk uses. An earlier version of
    these tests recorded `Path.iterdir`; when the walk moved to os.scandir one
    of them started passing on an empty list rather than failing, which is the
    quieter half of the same mistake.
    """
    opened: list[Path] = []
    real_scandir = os.scandir

    class _Recording:
        def __init__(self, path):
            opened.append(Path(path))
            self._it = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            try:
                self._it.close()
            except Exception:
                pass

        def __iter__(self):
            return iter(self._it)

        def close(self):
            self._it.close()

    monkeypatch.setattr(os, "scandir", lambda path=".": _Recording(path))
    return opened


def test_the_walk_never_descends_past_the_depth_bound(volume, monkeypatch):
    """`rglob` descended everything and discarded deep matches afterwards, so
    the bound described the results and not the work."""
    opened = _record_opened(monkeypatch)
    debug.find_model_dirs(volume, max_depth=4)

    traversed = _traversed(opened, volume)
    assert traversed, "recorded nothing — the probe is watching the wrong call"
    too_deep = [p for p in traversed if _depth(p, volume) >= 4]
    assert not too_deep, f"opened a directory past the depth bound: {too_deep[:3]}"


def test_the_depth_bound_is_honoured_for_other_values(volume, monkeypatch):
    opened = _record_opened(monkeypatch)
    debug.find_model_dirs(volume, max_depth=2)

    depths = [_depth(p, volume) for p in _traversed(opened, volume)]
    assert depths, "recorded nothing — the probe is watching the wrong call"
    assert max(depths) < 2, f"opened depth {max(depths)} under a bound of 2"


def test_the_match_limit_stops_the_search(volume):
    hub = volume / "hub"
    for i in range(30):
        (hub / f"models--acme--m{i}").mkdir()
    found, note = debug.find_model_dirs(volume, limit=5)
    assert len(found) == 5
    assert note is not None


def test_an_empty_volume_returns_nothing(tmp_path):
    assert debug.find_model_dirs(tmp_path) == ([], None)


def test_a_file_named_like_a_model_is_ignored(tmp_path):
    (tmp_path / "models--acme--notadir").write_text("x", encoding="utf-8")
    assert debug.find_model_dirs(tmp_path) == ([], None)


def test_a_model_at_the_root_is_found(tmp_path):
    (tmp_path / "models--acme--parser").mkdir()
    found, _ = debug.find_model_dirs(tmp_path)
    assert len(found) == 1
    assert found[0]["depth"] == 1


def test_the_limit_stops_enumeration_rather_than_trimming_the_result(volume, monkeypatch):
    """Sorting a level before applying the cap means visiting every entry in it
    first, which is the same unbounded-work mistake one layer down."""
    hub = volume / "hub"
    for i in range(200):
        (hub / f"models--acme--m{i:03d}").mkdir()

    consumed = 0
    real_glob = Path.glob

    def counting_glob(self, pattern, *args, **kwargs):
        def gen():
            nonlocal consumed
            for item in real_glob(self, pattern, *args, **kwargs):
                consumed += 1
                yield item
        return gen()

    monkeypatch.setattr(Path, "glob", counting_glob)
    found, _ = debug.find_model_dirs(volume, limit=5)

    assert len(found) == 5
    assert consumed < 50, f"enumerated {consumed} entries to return 5"


# -----------------------------------------------------------------------------
# The probe's directory listing
# -----------------------------------------------------------------------------

def test_a_small_directory_lists_in_sorted_order(tmp_path):
    (tmp_path / "b.txt").write_bytes(b"xx")
    (tmp_path / "a.txt").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    assert debug.list_directory(tmp_path) == ["f a.txt 1", "f b.txt 2", "d sub -"]


def test_a_large_directory_is_truncated_with_a_marker(tmp_path):
    for i in range(120):
        (tmp_path / f"f{i:03d}.txt").write_bytes(b"x")
    listed = debug.list_directory(tmp_path, max_entries=10)
    assert len(listed) == 11
    assert listed[-1].startswith("... (more entries elided")


def test_the_listing_stops_enumerating_at_the_limit(tmp_path, monkeypatch):
    """`sorted(p.iterdir())` materialised the whole directory before the slice,
    so the cap bounded the response while the walk stayed unbounded."""
    for i in range(500):
        (tmp_path / f"f{i:03d}.txt").write_bytes(b"x")

    consumed = 0
    real_iterdir = Path.iterdir

    def counting_iterdir(self):
        nonlocal consumed
        for item in real_iterdir(self):
            consumed += 1
            yield item

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)
    debug.list_directory(tmp_path, max_entries=10)
    assert consumed <= 11, f"enumerated {consumed} entries to list 10"


def test_an_unreadable_directory_reports_rather_than_raising(tmp_path):
    """Probe helpers are best-effort; nothing here may raise into the request."""
    assert debug.list_directory(tmp_path / "absent").startswith("<error:")


# -----------------------------------------------------------------------------
# The search is bounded by what it visits, not only by what it finds
# -----------------------------------------------------------------------------

def test_a_volume_with_no_models_stops_at_the_visit_budget(tmp_path):
    """islice can only stop a generator that yields. A broad tree with no
    matches yields nothing, so the search runs to exhaustion proving a
    negative — and a model-free volume is exactly what someone probes."""
    for i in range(60):
        for j in range(10):
            (tmp_path / f"d{i:02d}" / f"s{j:02d}").mkdir(parents=True)

    found, note = debug.find_model_dirs(tmp_path, max_visits=100)
    assert found == []
    assert note is not None and "100" in note


def test_the_visit_budget_is_not_hit_on_a_small_volume(tmp_path):
    (tmp_path / "hub" / "models--acme--parser").mkdir(parents=True)
    found, note = debug.find_model_dirs(tmp_path)
    assert len(found) == 1
    assert note is None


def test_visits_are_counted_across_depths(tmp_path, monkeypatch):
    for i in range(40):
        (tmp_path / f"d{i:02d}").mkdir()

    visited = 0
    real_iterdir = Path.iterdir

    def counting(self):
        nonlocal visited
        for item in real_iterdir(self):
            visited += 1
            yield item

    monkeypatch.setattr(Path, "iterdir", counting)
    debug.find_model_dirs(tmp_path, max_visits=15)
    assert visited <= 20, f"visited {visited} entries under a budget of 15"


# -----------------------------------------------------------------------------
# Snapshot listings
# -----------------------------------------------------------------------------

def test_snapshot_names_are_bounded(tmp_path, monkeypatch):
    model = tmp_path / "models--acme--parser"
    snaps = model / "snapshots"
    snaps.mkdir(parents=True)
    for i in range(200):
        (snaps / f"hash{i:03d}").mkdir()

    consumed = 0
    real_iterdir = Path.iterdir

    def counting(self):
        nonlocal consumed
        for item in real_iterdir(self):
            consumed += 1
            yield item

    monkeypatch.setattr(Path, "iterdir", counting)
    found, _ = debug.find_model_dirs(tmp_path)
    assert len(found[0]["snapshots"]) == 5
    assert consumed < 50, f"enumerated {consumed} snapshot entries to report 5"


def test_resolve_snapshot_path_bounds_its_listing(tmp_path, monkeypatch):
    model = tmp_path / "models--acme--parser"
    snaps = model / "snapshots"
    snaps.mkdir(parents=True)
    for i in range(300):
        (snaps / f"hash{i:03d}").mkdir()

    consumed = 0
    real_iterdir = Path.iterdir

    def counting(self):
        nonlocal consumed
        for item in real_iterdir(self):
            consumed += 1
            yield item

    monkeypatch.setattr(Path, "iterdir", counting)
    debug._resolve_snapshot_path(tmp_path, "acme/parser")
    assert consumed < 100, f"enumerated {consumed} of 300 snapshot entries"


# -----------------------------------------------------------------------------
# A read failure is the diagnosis, not a corrupted input to the next guess
# -----------------------------------------------------------------------------

def test_an_unreadable_refs_main_is_reported_as_itself(tmp_path, monkeypatch):
    """Storing the error text where a hash belongs made the next branch report
    'stale refs/main' — burying a permission error under a wrong diagnosis, in
    the one situation the probe exists to explain."""
    model = tmp_path / "models--acme--parser"
    (model / "refs").mkdir(parents=True)
    (model / "refs" / "main").write_text("abc123", encoding="utf-8")
    (model / "snapshots" / "abc123").mkdir(parents=True)

    real_read_text = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self.name == "main":
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    out = debug._resolve_snapshot_path(tmp_path, "acme/parser")

    assert "PermissionError" in str(out["issue"])
    assert "stale" not in str(out["issue"]).lower()
    assert out["resolved_path"] is None


def test_a_genuinely_stale_refs_main_still_says_so(tmp_path):
    model = tmp_path / "models--acme--parser"
    (model / "refs").mkdir(parents=True)
    (model / "refs" / "main").write_text("missinghash", encoding="utf-8")
    (model / "snapshots" / "otherhash").mkdir(parents=True)
    out = debug._resolve_snapshot_path(tmp_path, "acme/parser")
    assert "stale refs/main" in out["issue"]


# -----------------------------------------------------------------------------
# An unreadable subtree costs that subtree, not the search
# -----------------------------------------------------------------------------

def _lazy_iterdir(broken_name: str):
    """Mimic pathlib's pre-3.13 `iterdir`: a generator that raises on advance.

    Up to 3.12 `Path.iterdir` is a generator function, so guarding the *call*
    guards nothing — the OSError surfaces when the loop advances it. 3.13
    switched to an eager `os.scandir`, which means this fails on the three
    interpreter versions CI runs and passes on the one used locally. The stub
    keeps the test honest on both.
    """
    real_iterdir = Path.iterdir

    def lazy(self):
        names = list(real_iterdir(self))

        def gen():
            if self.name == broken_name:
                raise PermissionError(13, "Permission denied")
            yield from names

        return gen()

    return lazy


def test_an_unreadable_directory_does_not_abort_the_search(tmp_path, monkeypatch):
    """A queued directory can vanish or become unreadable between being listed
    and being walked. Losing that subtree is expected; losing the models found
    before it, and reporting only an error, is not."""
    (tmp_path / "a_hub" / "models--acme--parser").mkdir(parents=True)
    (tmp_path / "z_broken").mkdir()

    monkeypatch.setattr(Path, "iterdir", _lazy_iterdir("z_broken"))
    found, note = debug.find_model_dirs(tmp_path)

    assert [f["path"] for f in found] == [str(tmp_path / "a_hub" / "models--acme--parser")]


def test_a_later_subtree_is_still_searched_after_an_unreadable_one(tmp_path, monkeypatch):
    (tmp_path / "a_broken").mkdir()
    (tmp_path / "b_hub" / "models--acme--parser").mkdir(parents=True)

    monkeypatch.setattr(Path, "iterdir", _lazy_iterdir("a_broken"))
    found, _ = debug.find_model_dirs(tmp_path)

    assert len(found) == 1, "an early unreadable directory suppressed a later match"


def test_find_model_dir_survives_an_unreadable_cache(tmp_path, monkeypatch):
    """This runs on the response path of a successful job. An unreadable cache
    directory must cost the `model_dir` field, not the job.

    Watches os.scandir, which is what the bounded scan uses — an earlier
    version stubbed `Path.iterdir` and stopped intercepting anything when the
    implementation moved.
    """
    from runpod_doc_worker import config

    hub = tmp_path / "hub"
    (hub / "models--acme--parser" / "snapshots" / "abc").mkdir(parents=True)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    debug.find_model_dir.cache_clear()

    real_scandir = os.scandir

    def unreadable(path="."):
        if Path(path).name == "snapshots":
            raise PermissionError(13, "Permission denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", unreadable)
    try:
        # The model directory is still found; only the snapshot below it is
        # unreadable, so the field degrades rather than the job failing.
        assert debug.find_model_dir() == str(hub / "models--acme--parser")
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


def test_find_model_dir_returns_the_snapshot_when_readable(tmp_path, monkeypatch):
    from runpod_doc_worker import config

    hub = tmp_path / "hub"
    (hub / "models--acme--parser" / "snapshots" / "abc").mkdir(parents=True)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    debug.find_model_dir.cache_clear()
    try:
        assert debug.find_model_dir().endswith("abc")
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


# -----------------------------------------------------------------------------
# Bounded for real, on every supported interpreter
#
# `Path.iterdir` is a generator through 3.12 and materialises the whole
# os.scandir result from 3.13, so `islice` over it bounds nothing on a modern
# interpreter. These cases wrap the real os.scandir and count what is actually
# consumed, rather than substituting a lazy stub — a stub is bounded by
# construction and would report success regardless of what the code does.
# -----------------------------------------------------------------------------

@pytest.fixture
def consumed(monkeypatch):
    """Counts directory entries the code actually pulls from the OS."""
    counter = {"n": 0}
    real_scandir = os.scandir

    class _Counting:
        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            try:
                self._it.close()
            except Exception:
                pass

        def __iter__(self):
            for entry in self._it:
                counter["n"] += 1
                yield entry

        def close(self):
            self._it.close()

    monkeypatch.setattr(os, "scandir", lambda path=".": _Counting(path))
    return counter


def test_listing_a_huge_directory_reads_only_what_it_shows(tmp_path, consumed):
    for i in range(500):
        (tmp_path / f"f{i:03d}.txt").write_bytes(b"x")
    listed = debug.list_directory(tmp_path, max_entries=10)
    assert len(listed) == 11
    assert consumed["n"] <= 15, f"read {consumed['n']} of 500 entries to show 10"


def test_the_visit_budget_bounds_real_enumeration(tmp_path, consumed):
    for i in range(300):
        (tmp_path / f"d{i:03d}").mkdir()
    debug.find_model_dirs(tmp_path, max_visits=20)
    assert consumed["n"] <= 30, f"read {consumed['n']} of 300 entries under a budget of 20"


def test_snapshot_names_stop_at_the_raw_entry_cap(tmp_path, consumed):
    """islice after a filter counts only what survives it, so a snapshots dir
    full of ordinary files is scanned in full to prove no more subdirectories
    exist. The cap has to apply to entries read, not to entries kept."""
    model = tmp_path / "models--acme--parser"
    snaps = model / "snapshots"
    snaps.mkdir(parents=True)
    (snaps / "hash000").mkdir()
    for i in range(400):
        (snaps / f"junk{i:03d}.bin").write_bytes(b"x")

    debug.find_model_dirs(tmp_path)
    assert consumed["n"] <= 120, f"read {consumed['n']} entries for one snapshot name"


def test_resolve_snapshot_path_stops_at_the_raw_entry_cap(tmp_path, consumed):
    model = tmp_path / "models--acme--parser"
    snaps = model / "snapshots"
    snaps.mkdir(parents=True)
    (snaps / "hash000").mkdir()
    for i in range(400):
        (snaps / f"junk{i:03d}.bin").write_bytes(b"x")

    debug._resolve_snapshot_path(tmp_path, "acme/parser")
    assert consumed["n"] <= 120, f"read {consumed['n']} of 400 entries"


def test_find_model_dir_bounds_its_snapshot_scan(tmp_path, monkeypatch, consumed):
    """This runs on the success path of a real job. Every other listing in the
    module is bounded; leaving this one unbounded also means the next reader
    assumes it is."""
    from runpod_doc_worker import config

    model = tmp_path / "hub" / "models--acme--parser"
    snaps = model / "snapshots"
    snaps.mkdir(parents=True)
    for i in range(400):
        (snaps / f"hash{i:03d}").mkdir()

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    debug.find_model_dir.cache_clear()
    try:
        result = debug.find_model_dir()
        assert result is not None
        assert consumed["n"] <= 120, f"read {consumed['n']} of 400 snapshot entries"
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


def test_find_model_dir_bounds_the_hub_scan(tmp_path, monkeypatch, consumed):
    """The last unbounded enumeration in this module. `hub.glob` read the whole
    cache before one match was chosen, on the first job's response path."""
    from runpod_doc_worker import config

    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "models--acme--parser" / "snapshots" / "abc").mkdir(parents=True)
    for i in range(3000):
        (hub / f"models--other--m{i:04d}").mkdir()

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    debug.find_model_dir.cache_clear()
    try:
        assert debug.find_model_dir() is not None
        assert consumed["n"] <= debug.PROBE_MAX_VISITS + 60, (
            f"read {consumed['n']} entries from a 3000-entry cache"
        )
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()


def test_model_globs_still_match_by_name(tmp_path, monkeypatch):
    from runpod_doc_worker import config

    hub = tmp_path / "hub"
    (hub / "models--acme--parser").mkdir(parents=True)
    (hub / "models--other--thing").mkdir()
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    debug.find_model_dir.cache_clear()
    try:
        assert debug.find_model_dir() == str(hub / "models--acme--parser")
    finally:
        debug.find_model_dir.cache_clear()
        config.reset()
