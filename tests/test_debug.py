"""The probe's model-directory search.

The probe answers a question an operator cannot answer from outside a worker,
and it runs against a network volume of unknown size while a caller waits. So
the bound that matters is on the traversal, not on the result set.
"""

from __future__ import annotations

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


def test_the_walk_never_descends_past_the_depth_bound(volume, monkeypatch):
    """`rglob` descended everything and discarded deep matches afterwards, so
    the bound described the results and not the work.

    The earlier version of this test asserted on glob patterns, because
    pathlib's traversal is not observable from Python. The search walks
    explicitly now, so the directories it opens can be checked directly — which
    is what the pattern assertion was standing in for.
    """
    opened: list[Path] = []
    real_iterdir = Path.iterdir

    def recording(self):
        opened.append(self)
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", recording)
    debug.find_model_dirs(volume, max_depth=4)

    too_deep = [p for p in _traversed(opened, volume) if _depth(p, volume) >= 4]
    assert not too_deep, f"opened a directory past the depth bound: {too_deep[:3]}"


def test_the_depth_bound_is_honoured_for_other_values(volume, monkeypatch):
    opened: list[Path] = []
    real_iterdir = Path.iterdir

    def recording(self):
        opened.append(self)
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", recording)
    debug.find_model_dirs(volume, max_depth=2)

    depths = [_depth(p, volume) for p in _traversed(opened, volume)]
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
