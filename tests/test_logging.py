"""Structured logging: direct-print JSON / text emission + job_id contextvar.

The implementation deliberately bypasses Python's `logging` module — see the
module docstring for the reasoning. These tests capture stdout directly
because that's the only output channel.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from runpod_doc_worker import config
from runpod_doc_worker.obs import logging as worker_logging


def _capture(callable_, *args, **kwargs) -> str:
    """Run a callable while capturing stdout. Returns the captured text."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        callable_(*args, **kwargs)
    return buf.getvalue()


# -----------------------------------------------------------------------------
# JSON output (default)
# -----------------------------------------------------------------------------

def test_info_emits_one_line_json(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    out = _capture(worker_logging.info, "test message", backend="local-engine", pages=10)
    assert out.count("\n") == 1
    data = json.loads(out.strip())
    assert data["msg"] == "test message"
    assert data["level"] == "info"
    assert data["logger"] == config.active().logger_name
    assert data["backend"] == "local-engine"
    assert data["pages"] == 10
    assert data["ts"].endswith("Z")


def test_warning_and_error_use_correct_levels(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    warn_out = _capture(worker_logging.warning, "watch out")
    err_out = _capture(worker_logging.error, "kaboom", code=42)
    assert json.loads(warn_out)["level"] == "warning"
    err = json.loads(err_out)
    assert err["level"] == "error"
    assert err["code"] == 42


def test_debug_is_silent_by_default(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    out = _capture(worker_logging.debug, "noisy detail")
    assert out == ""  # debug is suppressed unless LOG_LEVEL=debug


def test_debug_enabled_when_log_level_debug(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    out = _capture(worker_logging.debug, "now you see me", detail="visible")
    data = json.loads(out.strip())
    assert data["level"] == "debug"
    assert data["detail"] == "visible"


def test_flush_happens_per_emission(monkeypatch, capsys):
    """RunPod docs warn about buffered output; we pass flush=True every call."""
    monkeypatch.setenv("LOG_FORMAT", "json")
    worker_logging.info("first")
    worker_logging.info("second")
    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["msg"] == "first"
    assert json.loads(lines[1])["msg"] == "second"


# -----------------------------------------------------------------------------
# Text mode for local development
# -----------------------------------------------------------------------------

def test_text_format_is_human_readable(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "text")
    out = _capture(worker_logging.info, "hello", backend="pipeline", pages=42)
    assert "hello" in out
    assert "INFO" in out
    assert f"[{config.active().logger_name}]" in out
    assert "backend=pipeline" in out
    assert "pages=42" in out


def test_log_format_read_per_call(monkeypatch):
    """LOG_FORMAT is read on every emission so tests can flip modes."""
    monkeypatch.setenv("LOG_FORMAT", "json")
    json_out = _capture(worker_logging.info, "first")
    monkeypatch.setenv("LOG_FORMAT", "text")
    text_out = _capture(worker_logging.info, "second")
    json.loads(json_out.strip())  # parses as JSON
    assert "INFO" in text_out  # not JSON


# -----------------------------------------------------------------------------
# job_id contextvar — auto-injected so cross-job correlation works.
# -----------------------------------------------------------------------------

def test_job_id_appears_in_json(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    token = worker_logging.job_id_var.set("test-job-abc-123")
    try:
        out = _capture(worker_logging.info, "hi")
        assert json.loads(out.strip())["job_id"] == "test-job-abc-123"
    finally:
        worker_logging.job_id_var.reset(token)


def test_job_id_appears_in_text(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "text")
    token = worker_logging.job_id_var.set("test-job-xyz-456")
    try:
        out = _capture(worker_logging.info, "hi")
        assert "job_id=test-job-xyz-456" in out
    finally:
        worker_logging.job_id_var.reset(token)


def test_job_id_omitted_when_unset(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    worker_logging.job_id_var.set(None)
    out = _capture(worker_logging.info, "hi")
    assert "job_id" not in json.loads(out.strip())



# -----------------------------------------------------------------------------
# Caller fields cannot shadow the fields the record is indexed by
# -----------------------------------------------------------------------------

def test_caller_fields_cannot_shadow_authoritative_json_fields(monkeypatch):
    """`info(..., level="error")` must not make an info line index as an error,
    and a caller-supplied job_id must not defeat the contextvar correlation."""
    monkeypatch.setenv("LOG_FORMAT", "json")
    worker_logging.job_id_var.set("real-job")
    try:
        out = _capture(
            worker_logging.info, "hello",
            level="error", logger="somewhere-else", ts="1999-01-01T00:00:00Z",
            job_id="spoofed", backend="local-engine",
        )
    finally:
        worker_logging.job_id_var.set(None)
    record = json.loads(out.strip())
    assert record["level"] == "info"
    assert record["logger"] == config.active().logger_name
    assert record["job_id"] == "real-job"
    assert record["ts"] != "1999-01-01T00:00:00Z"
    assert record["backend"] == "local-engine"


def test_text_format_does_not_repeat_a_shadowed_field(monkeypatch):
    """Text kept the generated values while json replaced them, so the same
    call meant different things depending on a format env var."""
    monkeypatch.setenv("LOG_FORMAT", "text")
    worker_logging.job_id_var.set("real-job")
    try:
        out = _capture(worker_logging.info, "hello", job_id="spoofed")
    finally:
        worker_logging.job_id_var.set(None)
    assert out.count("job_id=") == 1
    assert "spoofed" not in out


def test_the_mirror_sees_the_authoritative_fields(monkeypatch):
    seen = []
    config.configure(config.WorkerConfig(log_mirror=lambda lvl, msg, f: seen.append(f)))
    try:
        worker_logging.job_id_var.set("real-job")
        _capture(worker_logging.info, "hello", job_id="spoofed")
    finally:
        worker_logging.job_id_var.set(None)
        config.reset()
    assert seen[0]["job_id"] == "real-job"


def test_a_mirror_failure_record_carries_the_job_id(monkeypatch, capsys):
    """The failure path is where correlation matters most: concurrent jobs, and
    a warning that cannot be attributed to the request whose export failed."""
    def boom(level, msg, fields):
        raise RuntimeError("collector unreachable")

    monkeypatch.setenv("LOG_FORMAT", "json")
    config.configure(config.WorkerConfig(log_mirror=boom))
    try:
        worker_logging.job_id_var.set("job-42")
        worker_logging.info("hello")
    finally:
        worker_logging.job_id_var.set(None)
        config.reset()

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    warning = json.loads(lines[-1])
    assert warning["level"] == "warning"
    assert warning["job_id"] == "job-42"
    assert "ts" in warning
    assert warning["logger"] == "worker"
    assert "RuntimeError" in json.dumps(warning)


def test_a_mirror_failure_record_is_valid_json_for_any_logger_name(monkeypatch, capsys):
    """The record was hand-built by f-string, so a quote in the logger name
    produced output no JSON reader could parse."""
    def boom(level, msg, fields):
        raise RuntimeError("nope")

    monkeypatch.setenv("LOG_FORMAT", "json")
    config.configure(config.WorkerConfig(logger_name='we"ird', log_mirror=boom))
    try:
        worker_logging.info("hello")
    finally:
        config.reset()

    for line in capsys.readouterr().out.splitlines():
        if line.strip():
            assert json.loads(line)["logger"] == 'we"ird'


def test_a_failing_mirror_is_not_invoked_by_its_own_failure_record(monkeypatch, capsys):
    """A mirror that raises on every record would recurse without this."""
    calls = []

    def boom(level, msg, fields):
        calls.append(msg)
        raise RuntimeError("nope")

    config.configure(config.WorkerConfig(log_mirror=boom))
    try:
        worker_logging.info("hello")
    finally:
        config.reset()
    capsys.readouterr()
    assert calls == ["hello"]
