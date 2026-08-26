"""Input-source resolution: which files and which URLs the worker accepts. -- volume."""

from __future__ import annotations

import asyncio

import pytest

from runpod_doc_worker.transport import io as worker_io


def _resolve(job_input: dict):
    return asyncio.run(worker_io.resolve_input_bytes(job_input))


_PROXY_VARS = (
    "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
)


def test_volume_path_inside_a_root_is_read(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKER_VOLUME_ROOTS", str(tmp_path))
    doc = tmp_path / "nested" / "doc.pdf"
    doc.parent.mkdir()
    doc.write_bytes(b"%PDF-1.4 nested")
    raw, src = _resolve({"volume_path": str(doc)})
    assert raw == b"%PDF-1.4 nested"
    assert src == f"volume:{doc}"


def test_volume_path_outside_the_roots_is_rejected(monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(b"%PDF-1.4 elsewhere")
    monkeypatch.setenv("WORKER_VOLUME_ROOTS", str(root))
    with pytest.raises(ValueError, match="outside the configured input roots"):
        _resolve({"volume_path": str(outside)})


def test_volume_path_with_parent_segments_is_rejected(monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(b"%PDF-1.4 elsewhere")
    monkeypatch.setenv("WORKER_VOLUME_ROOTS", str(root))
    with_parent_segment = root / ".." / "elsewhere.pdf"
    with pytest.raises(ValueError, match="outside the configured input roots"):
        _resolve({"volume_path": str(with_parent_segment)})


def test_volume_path_symlink_leaving_the_root_is_rejected(monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(b"%PDF-1.4 elsewhere")
    link = root / "link.pdf"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    monkeypatch.setenv("WORKER_VOLUME_ROOTS", str(root))
    with pytest.raises(ValueError, match="outside the configured input roots"):
        _resolve({"volume_path": str(link)})


def test_volume_path_must_be_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKER_VOLUME_ROOTS", str(tmp_path))
    with pytest.raises(ValueError, match="must be an absolute path"):
        _resolve({"volume_path": "relative/doc.pdf"})


def test_volume_path_missing_file_keeps_its_message(monkeypatch, tmp_path):
    # The wording is quoted in the network-volumes guide and matched by
    # callers' own error handling — it must not drift.
    monkeypatch.setenv("WORKER_VOLUME_ROOTS", str(tmp_path))
    with pytest.raises(ValueError, match="volume_path not found inside container"):
        _resolve({"volume_path": str(tmp_path / "absent.pdf")})
