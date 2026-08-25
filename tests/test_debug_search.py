"""The probe's model-directory search. -- search."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from runpod_doc_worker.obs import debug, model_cache


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


def _hub_with(tmp_path, monkeypatch):
    from runpod_doc_worker import config

    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    model_cache.find_model_dir.cache_clear()
    return hub


def test_a_model_below_the_depth_bound_is_not_reported(volume):
    found, _ = model_cache.find_model_dirs(volume)
    assert all("buried" not in f["path"] for f in found)


def test_the_walk_never_descends_past_the_depth_bound(volume, monkeypatch):
    """`rglob` descended everything and discarded deep matches afterwards, so
    the bound described the results and not the work."""
    opened = _record_opened(monkeypatch)
    model_cache.find_model_dirs(volume, max_depth=4)

    traversed = _traversed(opened, volume)
    assert traversed, "recorded nothing — the probe is watching the wrong call"
    too_deep = [p for p in traversed if _depth(p, volume) >= 4]
    assert not too_deep, f"opened a directory past the depth bound: {too_deep[:3]}"


def test_the_depth_bound_is_honoured_for_other_values(volume, monkeypatch):
    opened = _record_opened(monkeypatch)
    model_cache.find_model_dirs(volume, max_depth=2)

    depths = [_depth(p, volume) for p in _traversed(opened, volume)]
    assert depths, "recorded nothing — the probe is watching the wrong call"
    assert max(depths) < 2, f"opened depth {max(depths)} under a bound of 2"


def test_the_match_limit_stops_the_search(volume):
    hub = volume / "hub"
    for i in range(30):
        (hub / f"models--acme--m{i}").mkdir()
    found, note = model_cache.find_model_dirs(volume, limit=5)
    assert len(found) == 5
    assert note is not None


def test_an_empty_volume_returns_nothing(tmp_path):
    assert model_cache.find_model_dirs(tmp_path) == ([], None)


def test_a_file_named_like_a_model_is_ignored(tmp_path):
    (tmp_path / "models--acme--notadir").write_text("x", encoding="utf-8")
    assert model_cache.find_model_dirs(tmp_path) == ([], None)


def test_a_model_at_the_root_is_found(tmp_path):
    (tmp_path / "models--acme--parser").mkdir()
    found, _ = model_cache.find_model_dirs(tmp_path)
    assert len(found) == 1
    assert found[0]["depth"] == 1


def test_a_volume_with_no_models_stops_at_the_visit_budget(tmp_path):
    """islice can only stop a generator that yields. A broad tree with no
    matches yields nothing, so the search runs to exhaustion proving a
    negative — and a model-free volume is exactly what someone probes."""
    for i in range(60):
        for j in range(10):
            (tmp_path / f"d{i:02d}" / f"s{j:02d}").mkdir(parents=True)

    found, note = model_cache.find_model_dirs(tmp_path, max_visits=100)
    assert found == []
    assert note is not None and "100" in note


def test_the_visit_budget_is_not_hit_on_a_small_volume(tmp_path):
    (tmp_path / "hub" / "models--acme--parser").mkdir(parents=True)
    found, note = model_cache.find_model_dirs(tmp_path)
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
    model_cache.find_model_dirs(tmp_path, max_visits=15)
    assert visited <= 20, f"visited {visited} entries under a budget of 15"


def test_an_unreadable_directory_does_not_abort_the_search(tmp_path, monkeypatch):
    """A queued directory can vanish or become unreadable between being listed
    and being walked. Losing that subtree is expected; losing the models found
    before it, and reporting only an error, is not."""
    (tmp_path / "a_hub" / "models--acme--parser").mkdir(parents=True)
    (tmp_path / "z_broken").mkdir()

    monkeypatch.setattr(Path, "iterdir", _lazy_iterdir("z_broken"))
    found, note = model_cache.find_model_dirs(tmp_path)

    assert [f["path"] for f in found] == [str(tmp_path / "a_hub" / "models--acme--parser")]


def test_a_later_subtree_is_still_searched_after_an_unreadable_one(tmp_path, monkeypatch):
    (tmp_path / "a_broken").mkdir()
    (tmp_path / "b_hub" / "models--acme--parser").mkdir(parents=True)

    monkeypatch.setattr(Path, "iterdir", _lazy_iterdir("a_broken"))
    found, _ = model_cache.find_model_dirs(tmp_path)

    assert len(found) == 1, "an early unreadable directory suppressed a later match"


def test_the_visit_budget_bounds_real_enumeration(tmp_path, consumed):
    for i in range(300):
        (tmp_path / f"d{i:03d}").mkdir()
    model_cache.find_model_dirs(tmp_path, max_visits=20)
    assert consumed["n"] <= 30, f"read {consumed['n']} of 300 entries under a budget of 20"


def test_model_globs_still_match_by_name(tmp_path, monkeypatch):
    from runpod_doc_worker import config

    hub = tmp_path / "hub"
    (hub / "models--acme--parser").mkdir(parents=True)
    (hub / "models--other--thing").mkdir()
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    model_cache.find_model_dir.cache_clear()
    try:
        assert model_cache.find_model_dir() == str(hub / "models--acme--parser")
    finally:
        model_cache.find_model_dir.cache_clear()
        config.reset()


def test_a_complete_scan_with_no_match_still_reports_absence(tmp_path, monkeypatch):
    from runpod_doc_worker import config

    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "models--other--thing").mkdir()
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    model_cache.find_model_dir.cache_clear()
    try:
        assert model_cache.find_model_dir() is None
    finally:
        model_cache.find_model_dir.cache_clear()
        config.reset()


def test_a_file_matching_the_glob_is_not_reported_as_a_model_dir(tmp_path, monkeypatch):
    """A partially written cache entry with a newer mtime used to win, and the
    function reported a regular file as the loaded model directory."""
    from runpod_doc_worker import config

    hub = _hub_with(tmp_path, monkeypatch)
    (hub / "models--acme--parser").mkdir()
    time.sleep(0.01)
    (hub / "models--acme--stray").write_text("partially written", encoding="utf-8")
    try:
        result = model_cache.find_model_dir()
        assert result is not None
        assert Path(result).is_dir(), f"reported a non-directory: {result}"
        assert "stray" not in result
    finally:
        model_cache.find_model_dir.cache_clear()
        config.reset()


def test_every_candidate_unusable_falls_back_to_no_answer(tmp_path, monkeypatch):
    from runpod_doc_worker import config

    hub = _hub_with(tmp_path, monkeypatch)
    (hub / "models--acme--gone").mkdir()

    real_stat = Path.stat

    def flaky(self, *args, **kwargs):
        if self.name.startswith("models--acme--"):
            raise FileNotFoundError(2, "vanished")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    try:
        assert model_cache.find_model_dir() is None
    finally:
        model_cache.find_model_dir.cache_clear()
        config.reset()


def test_the_whole_probe_survives_a_corrupt_refs_main(tmp_path, monkeypatch):
    from runpod_doc_worker import config

    hub = tmp_path / "hub"
    model = hub / "models--acme--parser"
    (model / "refs").mkdir(parents=True)
    (model / "refs" / "main").write_bytes(b"\xff\xfe\x00")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    config.configure(config.WorkerConfig(probe_model_ids=("acme/parser",)))
    try:
        out = debug.probe_filesystem()
        assert out["resolution_attempts"], "the probe returned no attempt at all"
    finally:
        config.reset()


@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks need privileges on Windows")
def test_the_model_search_does_not_follow_a_symlink_out_of_the_root(tmp_path):
    """`entry.is_dir()` follows links, so a directory symlink under the search
    root queued its target and the probe reported models living elsewhere."""
    outside = tmp_path / "outside"
    (outside / "models--acme--elsewhere").mkdir(parents=True)
    root = tmp_path / "volume"
    root.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    found, _ = model_cache.find_model_dirs(root)
    assert found == [], f"traversed out of the root: {found}"
