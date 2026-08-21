from pathlib import Path

import pytest

from runpod_doc_worker import paths
from runpod_doc_worker.transport import io as worker_io


def test_within_rejects_a_path_that_cannot_resolve(monkeypatch, tmp_path):
    candidate = tmp_path / "loop"
    real_resolve = Path.resolve

    def resolve(path: Path, *args, **kwargs):
        if path == candidate:
            raise RuntimeError("Symlink loop from 'loop'")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    assert paths.within(tmp_path, candidate) is False


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
