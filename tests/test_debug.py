"""The probe's model-directory search.

The probe answers a question an operator cannot answer from outside a worker,
and it runs against a network volume of unknown size while a caller waits. So
the bound that matters is on the traversal, not on the result set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runpod_doc_worker.obs import debug


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
    found = debug.find_model_dirs(volume)
    assert [f["path"] for f in found] == [str(volume / "hub" / "models--acme--parser")]
    assert found[0]["depth"] == 2
    assert found[0]["snapshots"] == ["abc123"]


def test_a_model_below_the_depth_bound_is_not_reported(volume):
    assert all("buried" not in f["path"] for f in debug.find_model_dirs(volume))


def test_the_search_is_bounded_by_pattern_not_by_filtering(volume, monkeypatch):
    """The point of the fix: `rglob` descends the whole tree and discards deep
    matches afterwards, so a volume with no models at all was the case that
    scanned the most of it. The bound has to be in the pattern.

    pathlib's traversal is not observable from Python — patching `os.scandir`
    or `Path.iterdir` records nothing — so this asserts on the patterns issued,
    which is the thing that actually decides how far it descends.
    """
    patterns: list[str] = []
    real_glob = Path.glob
    real_rglob = Path.rglob

    def recording_glob(self, pattern, *args, **kwargs):
        patterns.append(str(pattern))
        return real_glob(self, pattern, *args, **kwargs)

    def recording_rglob(self, pattern, *args, **kwargs):
        patterns.append("**/" + str(pattern))
        return real_rglob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", recording_glob)
    monkeypatch.setattr(Path, "rglob", recording_rglob)
    debug.find_model_dirs(volume, max_depth=4)

    assert patterns, "no directory search was issued at all"
    assert not any("**" in p for p in patterns), (
        f"a recursive pattern descends without bound: {patterns}"
    )
    too_deep = [p for p in patterns if len(p.split("/")) > 4]
    assert not too_deep, f"pattern reaches past the depth bound: {too_deep}"


def test_the_depth_bound_is_honoured_for_other_values(volume, monkeypatch):
    patterns: list[str] = []
    real_glob = Path.glob
    monkeypatch.setattr(
        Path, "glob",
        lambda self, pattern, *a, **k: (patterns.append(str(pattern)), real_glob(self, pattern, *a, **k))[1],
    )
    debug.find_model_dirs(volume, max_depth=2)
    assert max(len(p.split("/")) for p in patterns) == 2


def test_the_match_limit_stops_the_search(volume):
    hub = volume / "hub"
    for i in range(30):
        (hub / f"models--acme--m{i}").mkdir()
    assert len(debug.find_model_dirs(volume, limit=5)) == 5


def test_an_empty_volume_returns_nothing(tmp_path):
    assert debug.find_model_dirs(tmp_path) == []


def test_a_file_named_like_a_model_is_ignored(tmp_path):
    (tmp_path / "models--acme--notadir").write_text("x", encoding="utf-8")
    assert debug.find_model_dirs(tmp_path) == []


def test_a_model_at_the_root_is_found(tmp_path):
    (tmp_path / "models--acme--parser").mkdir()
    found = debug.find_model_dirs(tmp_path)
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
    found = debug.find_model_dirs(volume, limit=5)

    assert len(found) == 5
    assert consumed < 50, f"enumerated {consumed} entries to return 5"
