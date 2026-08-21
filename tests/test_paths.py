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


def test_kind_separates_a_broken_entry_from_a_directory(tmp_path):
    """`is_file()` answers False for both, which is why asking it directly
    drops the broken ones in with the ones a caller meant to skip."""
    a_file = tmp_path / "doc.md"
    a_file.write_text("body", encoding="utf-8")
    a_dir = tmp_path / "images"
    a_dir.mkdir()

    assert paths.kind(a_file) == paths.FILE
    assert paths.kind(a_dir) == paths.DIRECTORY

    loop, other = tmp_path / "loop.md", tmp_path / "loop.md.cycle"
    try:
        loop.symlink_to(other)
        other.symlink_to(loop)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    assert loop.is_file() is False  # the trap this function exists for
    assert paths.kind(loop) == paths.UNRESOLVABLE


def test_kind_reports_an_entry_it_cannot_stat(tmp_path, monkeypatch):
    """A permission denial is not in pathlib's ignored errnos, so `is_file()`
    raises instead of answering. A caller asking about a type should not have
    to handle that."""
    target = tmp_path / "doc.md"
    target.write_text("body", encoding="utf-8")

    def is_dir(self, *args, **kwargs):
        raise PermissionError("Permission denied")

    monkeypatch.setattr(Path, "is_dir", is_dir)
    assert paths.kind(target) == paths.UNRESOLVABLE


def test_kind_reports_a_link_to_nothing(tmp_path):
    dangling = tmp_path / "doc.md"
    try:
        dangling.symlink_to(tmp_path / "never-written.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    assert paths.kind(dangling) == paths.UNRESOLVABLE
