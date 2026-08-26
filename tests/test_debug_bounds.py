"""The probe's model-directory search. -- bounds."""

from __future__ import annotations

import os
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
    found, _ = model_cache.find_model_dirs(volume, limit=5)

    assert len(found) == 5
    assert consumed < 50, f"enumerated {consumed} entries to return 5"


def test_a_truncated_hub_scan_is_not_reported_as_absence(tmp_path, monkeypatch):
    """`_scan` reports truncation and the caller discarded it, so a cache too
    large to scan fully reported the model as missing rather than as not-found-
    in-the-part-we-looked-at. Same defect as the sibling search had, one
    function over."""
    from runpod_doc_worker import config

    hub = tmp_path / "hub"
    hub.mkdir()
    for i in range(50):
        (hub / f"models--other--m{i:03d}").mkdir()

    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setattr(probe_limits, "PROBE_MAX_VISITS", 10)
    config.configure(config.WorkerConfig(model_globs=("models--acme--*",)))
    model_cache.find_model_dir.cache_clear()
    try:
        result = model_cache.find_model_dir()
        assert result is not None, "a truncated scan reported a definitive miss"
        assert "truncated" in result or "partial" in result, result
    finally:
        model_cache.find_model_dir.cache_clear()
        config.reset()


def test_lowering_a_probe_limit_through_debug_still_takes_effect() -> None:
    """`debug.PROBE_MAX_VISITS = 10` used to change what the search read.

    After the split the implementation reads `probe_limits`, so the re-exported
    name became a snapshot: the assignment landed on `debug`, the scan went on
    using 2000, and nothing said so. Third instance of one shape -- a name that
    used to be read where it was assigned is now read somewhere else -- and the
    only one where the old control point had to keep working, because this module
    is released and documented.
    """
    from runpod_doc_worker.obs import debug, probe_limits

    original = probe_limits.PROBE_MAX_VISITS
    try:
        debug.PROBE_MAX_VISITS = 10
        assert probe_limits.PROBE_MAX_VISITS == 10, (
            "assigning the re-exported name must reach the module that reads it"
        )
    finally:
        debug.PROBE_MAX_VISITS = original
        probe_limits.PROBE_MAX_VISITS = original
