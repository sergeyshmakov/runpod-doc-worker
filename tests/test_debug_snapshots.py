"""The probe's model-directory search. -- snapshots."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from runpod_doc_worker.obs import model_cache, probe_limits


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


def test_finds_a_model_within_the_depth_bound(volume):
    found, _ = model_cache.find_model_dirs(volume)
    assert [f["path"] for f in found] == [str(volume / "hub" / "models--acme--parser")]
    assert found[0]["depth"] == 2
    assert found[0]["snapshots"] == ["abc123"]


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
    found, _ = model_cache.find_model_dirs(tmp_path)
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
    model_cache._resolve_snapshot_path(tmp_path, "acme/parser")
    assert consumed < 100, f"enumerated {consumed} of 300 snapshot entries"


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
    out = model_cache._resolve_snapshot_path(tmp_path, "acme/parser")

    assert "PermissionError" in str(out["issue"])
    assert "stale" not in str(out["issue"]).lower()
    assert out["resolved_path"] is None


def test_a_genuinely_stale_refs_main_still_says_so(tmp_path):
    model = tmp_path / "models--acme--parser"
    (model / "refs").mkdir(parents=True)
    (model / "refs" / "main").write_text("missinghash", encoding="utf-8")
    (model / "snapshots" / "otherhash").mkdir(parents=True)
    out = model_cache._resolve_snapshot_path(tmp_path, "acme/parser")
    assert "stale refs/main" in out["issue"]


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
    model_cache.find_model_dir.cache_clear()

    real_scandir = os.scandir

    def unreadable(path="."):
        if Path(path).name == "snapshots":
            raise PermissionError(13, "Permission denied")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", unreadable)
    try:
        # The model directory is still found; only the snapshot below it is
        # unreadable, so the field degrades rather than the job failing.
        assert model_cache.find_model_dir() == str(hub / "models--acme--parser")
    finally:
        model_cache.find_model_dir.cache_clear()
        config.reset()


def test_find_model_dir_returns_the_snapshot_when_readable(tmp_path, monkeypatch):
    from runpod_doc_worker import config

    hub = tmp_path / "hub"
    (hub / "models--acme--parser" / "snapshots" / "abc").mkdir(parents=True)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    model_cache.find_model_dir.cache_clear()
    try:
        assert model_cache.find_model_dir().endswith("abc")
    finally:
        model_cache.find_model_dir.cache_clear()
        config.reset()


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

    model_cache.find_model_dirs(tmp_path)
    assert consumed["n"] <= 120, f"read {consumed['n']} entries for one snapshot name"


def test_resolve_snapshot_path_stops_at_the_raw_entry_cap(tmp_path, consumed):
    model = tmp_path / "models--acme--parser"
    snaps = model / "snapshots"
    snaps.mkdir(parents=True)
    (snaps / "hash000").mkdir()
    for i in range(400):
        (snaps / f"junk{i:03d}.bin").write_bytes(b"x")

    model_cache._resolve_snapshot_path(tmp_path, "acme/parser")
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
    model_cache.find_model_dir.cache_clear()
    try:
        result = model_cache.find_model_dir()
        assert result is not None
        assert consumed["n"] <= 120, f"read {consumed['n']} of 400 snapshot entries"
    finally:
        model_cache.find_model_dir.cache_clear()
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
    model_cache.find_model_dir.cache_clear()
    try:
        assert model_cache.find_model_dir() is not None
        assert consumed["n"] <= probe_limits.PROBE_MAX_VISITS + 60, (
            f"read {consumed['n']} entries from a 3000-entry cache"
        )
    finally:
        model_cache.find_model_dir.cache_clear()
        config.reset()


def test_a_candidate_that_cannot_be_statted_does_not_erase_the_others(tmp_path, monkeypatch):
    """One stale entry used to take the whole answer down with it, so the first
    successful response reported the model as absent."""
    from runpod_doc_worker import config

    hub = _hub_with(tmp_path, monkeypatch)
    (hub / "models--acme--good" / "snapshots" / "abc").mkdir(parents=True)
    (hub / "models--acme--gone").mkdir()

    real_stat = Path.stat

    def flaky(self, *args, **kwargs):
        if self.name == "models--acme--gone":
            raise FileNotFoundError(2, "vanished")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    try:
        result = model_cache.find_model_dir()
        assert result is not None, "one stale candidate erased a valid one"
        assert "good" in result
    finally:
        model_cache.find_model_dir.cache_clear()
        config.reset()


def test_an_unstattable_snapshot_does_not_erase_its_siblings(tmp_path, monkeypatch):
    from runpod_doc_worker import config

    hub = _hub_with(tmp_path, monkeypatch)
    snaps = hub / "models--acme--parser" / "snapshots"
    (snaps / "good").mkdir(parents=True)
    (snaps / "gone").mkdir()

    real_stat = Path.stat

    def flaky(self, *args, **kwargs):
        if self.name == "gone":
            raise FileNotFoundError(2, "vanished")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky)
    try:
        assert model_cache.find_model_dir().endswith("good")
    finally:
        model_cache.find_model_dir.cache_clear()
        config.reset()


def test_a_non_utf8_refs_main_is_reported_not_raised(tmp_path):
    """`UnicodeDecodeError` is a ValueError, not an OSError, so the guard added
    for unreadable refs/main did not cover an undecodable one — and the probe
    exists precisely to diagnose corrupt caches."""
    model = tmp_path / "models--acme--parser"
    (model / "refs").mkdir(parents=True)
    (model / "refs" / "main").write_bytes(b"\xff\xfe\x00not utf-8")
    (model / "snapshots" / "abc").mkdir(parents=True)

    out = model_cache._resolve_snapshot_path(tmp_path, "acme/parser")
    assert "could not be read" in str(out["issue"])
    assert "UnicodeDecodeError" in str(out["issue"])
    assert out["resolved_path"] is None


@pytest.mark.parametrize("ref", ["/etc", "../../..", "sub/deeper", "..", "."])
def test_a_refs_main_pointing_outside_snapshots_is_refused(tmp_path, ref):
    """Joining an absolute ref replaces the base entirely, so `/etc` resolved
    to `/etc` and — if it exists — was reported as a successful resolution with
    no issue recorded."""
    model = tmp_path / "models--acme--parser"
    (model / "refs").mkdir(parents=True)
    (model / "refs" / "main").write_text(ref, encoding="utf-8")
    (model / "snapshots" / "abc").mkdir(parents=True)

    out = model_cache._resolve_snapshot_path(tmp_path, "acme/parser")
    assert out["resolution_method"] != "refs/main", f"accepted {ref!r}"
    assert out["issue"] is not None


def test_a_normal_refs_main_still_resolves(tmp_path):
    model = tmp_path / "models--acme--parser"
    (model / "refs").mkdir(parents=True)
    (model / "refs" / "main").write_text("abc123", encoding="utf-8")
    (model / "snapshots" / "abc123").mkdir(parents=True)
    out = model_cache._resolve_snapshot_path(tmp_path, "acme/parser")
    assert out["resolution_method"] == "refs/main"
    assert out["issue"] is None


@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks need privileges on Windows")
def test_a_symlinked_model_dir_is_not_reported_as_being_in_the_cache(tmp_path, monkeypatch):
    """Found by auditing for the whole class of defect rather than the one
    instance of it. The reported path read as though it were inside the cache
    while resolving elsewhere."""
    from runpod_doc_worker import config

    (tmp_path / "elsewhere" / "snapshots" / "xyz").mkdir(parents=True)
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "models--acme--parser").symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    model_cache.find_model_dir.cache_clear()
    try:
        assert model_cache.find_model_dir() is None
    finally:
        model_cache.find_model_dir.cache_clear()
        config.reset()


@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks need privileges on Windows")
def test_a_symlinked_snapshot_is_not_reported_as_resolved(tmp_path):
    (tmp_path / "elsewhere").mkdir()
    model = tmp_path / "hub" / "models--acme--parser"
    (model / "refs").mkdir(parents=True)
    (model / "refs" / "main").write_text("abc123", encoding="utf-8")
    (model / "snapshots").mkdir()
    (model / "snapshots" / "abc123").symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    out = model_cache._resolve_snapshot_path(tmp_path / "hub", "acme/parser")
    assert out["resolution_method"] != "refs/main"
    assert "outside snapshots" in str(out["issue"])
