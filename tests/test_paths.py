from pathlib import Path

from runpod_doc_worker import paths


def test_within_rejects_a_path_that_cannot_resolve(monkeypatch, tmp_path):
    candidate = tmp_path / "loop"
    real_resolve = Path.resolve

    def resolve(path: Path, *args, **kwargs):
        if path == candidate:
            raise RuntimeError("Symlink loop from 'loop'")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    assert paths.within(tmp_path, candidate) is False
