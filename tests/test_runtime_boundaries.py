"""Regression tests for caller-triggered runtime boundary work."""

from __future__ import annotations

import asyncio
import base64
import tracemalloc

import pytest

from runpod_doc_worker.obs import debug
from runpod_doc_worker.transport import io as worker_io


def _resolve(job_input: dict) -> tuple[bytes, str]:
    return asyncio.run(worker_io.resolve_input_bytes(job_input))


@pytest.mark.parametrize("sep", ["\N{NO-BREAK SPACE}", "\N{EM SPACE}"])
def test_non_ascii_whitespace_is_not_base64_formatting(sep):
    """Base64 and its ordinary transport wrapping are ASCII."""
    encoded = base64.b64encode(b"%PDF-1.4 hello").decode()

    with pytest.raises(ValueError, match="not valid base64"):
        _resolve({"file_b64": sep.join((encoded[:8], encoded[8:]))})


def test_many_short_base64_chunks_keep_peak_allocation_bounded():
    """Whitespace removal must not retain one object per encoded chunk."""
    payload = "QUJD " * 50_000
    tracemalloc.start()
    try:
        raw, _ = _resolve({"file_b64": payload})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert raw == b"ABC" * 50_000
    assert peak < len(payload) * 4


def test_the_filesystem_probe_requires_explicit_enablement(monkeypatch):
    monkeypatch.delenv("WORKER_ENABLE_PROBE", raising=False)

    with pytest.raises(PermissionError, match="WORKER_ENABLE_PROBE"):
        debug.probe_filesystem()
