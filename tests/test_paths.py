from pathlib import Path

import pytest

from runpod_doc_worker import paths
from runpod_doc_worker.transport import io as worker_io


def _break_resolve(monkeypatch, candidate: Path) -> None:
    """Make one path unresolvable, the way a symlink loop does."""
    real_resolve = Path.resolve

    def resolve(path: Path, *args, **kwargs):
        if path == candidate:
            raise RuntimeError("Symlink loop from 'loop'")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)


def test_within_rejects_a_path_that_cannot_resolve(monkeypatch, tmp_path):
    candidate = tmp_path / "loop"
    _break_resolve(monkeypatch, candidate)

    assert paths.within(tmp_path, candidate) is False


def test_relation_names_the_three_answers_apart(monkeypatch, tmp_path):
    """`within` folds two different problems into one False. A caller that has
    to report a cause, or that treats an unreadable file differently from an
    escaping one, needs them apart."""
    inside = tmp_path / "sub" / "doc.md"
    inside.parent.mkdir()
    inside.write_text("body", encoding="utf-8")
    outside = tmp_path.parent / "elsewhere.md"

    assert paths.relation(tmp_path, inside) == paths.INSIDE
    assert paths.relation(tmp_path, tmp_path) == paths.INSIDE
    assert paths.relation(tmp_path, outside) == paths.OUTSIDE

    candidate = tmp_path / "loop"
    _break_resolve(monkeypatch, candidate)
    assert paths.relation(tmp_path, candidate) == paths.UNRESOLVABLE


def test_within_and_escapes_still_agree_with_relation(monkeypatch, tmp_path):
    """The two-answer helpers are the three-answer one, narrowed. They must not
    drift into a second implementation."""
    inside = tmp_path / "doc.md"
    inside.write_text("body", encoding="utf-8")
    outside = tmp_path.parent / "elsewhere.md"

    assert paths.within(tmp_path, inside) is True
    assert paths.escapes(tmp_path, inside) is False
    assert paths.within(tmp_path, outside) is False
    assert paths.escapes(tmp_path, outside) is True

    candidate = tmp_path / "loop"
    _break_resolve(monkeypatch, candidate)
    assert paths.within(tmp_path, candidate) is False
    assert paths.escapes(tmp_path, candidate) is True


def test_volume_file_reports_a_path_that_cannot_resolve(monkeypatch, tmp_path):
    candidate = tmp_path / "loop"
    real_resolve = Path.resolve

    def resolve(path: Path, *args, **kwargs):
        if path == candidate:
            raise RuntimeError("Symlink loop from 'loop'")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    with pytest.raises(ValueError, match="volume_path cannot be resolved"):
        worker_io.resolve_volume_file(str(candidate))
