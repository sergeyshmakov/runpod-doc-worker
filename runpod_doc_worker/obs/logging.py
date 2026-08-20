"""Structured logging for the worker, via direct prints to stdout.

We deliberately do NOT use Python's `logging` module here. Empirically,
the runpod SDK's serverless runtime reconfigures the root logger inside
`runpod.serverless.start()` in ways that silently swallow records from
loggers configured before it. Direct `print(..., flush=True)` is the
only reliable channel — RunPod captures stdout regardless of what any
logging library does to it (proven by the SDK's own `Started.` /
`Finished.` lines using the same mechanism).

Output is one JSON object per line by default — easier to filter in
RunPod's log viewer or any downstream JSON sink. ``LOG_FORMAT=text``
flips to a human-readable single-line format for local development.

A ``job_id`` ContextVar is auto-injected into every emission, per
RunPod's write-logs guidance ("Include the job ID or request ID in
log entries for traceability").

A worker that exports telemetry registers a second sink through
``WorkerConfig.log_mirror``; every record written here is passed to it as
``(level, msg, fields)`` after the stdout line. The mirror is additive and
never authoritative — stdout is what RunPod's dashboard reads, so it goes
first and a failing mirror cannot take a job down with it.
"""

from __future__ import annotations

import contextvars
import json
import os
import sys
import time
from typing import Any

from runpod_doc_worker import config as _config


# Set by the handler at the top of each request; read on every emission.
# ContextVars are asyncio-safe — concurrent jobs in the same event loop
# don't bleed into each other's context.
job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "worker_job_id", default=None
)


# Fields the record is identified and indexed by. A caller passing one of these
# as a keyword is shadowing the thing a log sink sorts on — an `info()` call
# arriving as `level: "error"`, or a job's own id replaced by whatever a caller
# had to hand, which defeats the correlation the contextvar exists for. They are
# dropped rather than merged, and identically in both formats: the same call
# must not mean two different things depending on an env var.
RESERVED_FIELDS = frozenset({"ts", "level", "logger", "msg", "job_id"})


def _caller_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if k not in RESERVED_FIELDS}


def _format_json(level: str, msg: str, fields: dict[str, Any]) -> str:
    """Build a one-line JSON record. Always includes ts, level, logger, msg."""
    now = time.time()
    ms = int((now - int(now)) * 1000)
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{ms:03d}Z",
        "level": level,
        "logger": _config.active().logger_name,
        "msg": msg,
    }
    if (jid := job_id_var.get()) is not None:
        record["job_id"] = jid
    record.update(_caller_fields(fields))
    return json.dumps(record, default=str)


def _format_text(level: str, msg: str, fields: dict[str, Any]) -> str:
    """Compact human-readable single-line format."""
    ts = time.strftime("%H:%M:%S")
    parts = [f"{ts} {level.upper():<5} [{_config.active().logger_name}] {msg}"]
    if (jid := job_id_var.get()) is not None:
        parts.append(f"job_id={jid}")
    for k, v in _caller_fields(fields).items():
        parts.append(f"{k}={v}")
    return " ".join(parts)


def _emit(level: str, msg: str, fields: dict[str, Any]) -> None:
    """Write one log line to stdout. Re-reads LOG_FORMAT each call so it
    can be flipped at runtime without restarting (mostly useful for tests).

    Then mirror the same record to the worker's second sink, if it registered
    one. The stdout line fires first so RunPod's dashboard is never gated on an
    external collector being reachable, and a raising mirror is swallowed for
    the same reason — telemetry export must not be able to fail a job.
    """
    fmt = os.environ.get("LOG_FORMAT", "json").lower()
    line = _format_text(level, msg, fields) if fmt == "text" else _format_json(level, msg, fields)
    print(line, file=sys.stdout, flush=True)

    mirror = _config.active().log_mirror
    if mirror is None:
        return
    # Pass the job_id along as an attribute so the mirrored record carries the
    # same correlation field the stdout line has — and the same one, so a
    # caller-supplied job_id cannot make the two sinks disagree.
    attrs = _caller_fields(fields)
    if (jid := job_id_var.get()) is not None:
        attrs["job_id"] = jid
    try:
        mirror(level, msg, attrs)
    except Exception as e:  # noqa: BLE001
        # One line to stdout, which is the sink we know works. Not routed back
        # through _emit(): a mirror that raises on every record would recurse.
        print(
            f"{{\"level\":\"warning\",\"logger\":\"{_config.active().logger_name}\","
            f"\"msg\":\"log mirror raised\",\"error\":\"{type(e).__name__}\"}}",
            file=sys.stdout,
            flush=True,
        )


def info(msg: str, **fields: Any) -> None:
    _emit("info", msg, fields)


def warning(msg: str, **fields: Any) -> None:
    _emit("warning", msg, fields)


def error(msg: str, **fields: Any) -> None:
    _emit("error", msg, fields)


def debug(msg: str, **fields: Any) -> None:
    # Only emit debug if explicitly enabled. Most of the time these are
    # too noisy for production but useful for local triage.
    if os.environ.get("LOG_LEVEL", "info").lower() == "debug":
        _emit("debug", msg, fields)
