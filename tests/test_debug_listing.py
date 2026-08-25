"""The probe's model-directory search. -- listing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from runpod_doc_worker.obs import debug


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


def test_listing_a_huge_directory_reads_only_what_it_shows(tmp_path, consumed):
    for i in range(500):
        (tmp_path / f"f{i:03d}.txt").write_bytes(b"x")
    listed = debug.list_directory(tmp_path, max_entries=10)
    assert len(listed) == 11
    assert consumed["n"] <= 15, f"read {consumed['n']} of 500 entries to show 10"
