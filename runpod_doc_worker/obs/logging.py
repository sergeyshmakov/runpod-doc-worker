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


def _render(level: str, msg: str, fields: dict[str, Any]) -> str:
    """One record, in whichever format is configured.

    Separate from :func:`_emit` so the mirror-failure path can produce a fully
    formed record — timestamp, logger and job id included — without going back
    through the mirror it is reporting on.

    LOG_FORMAT is read per call so it can be flipped at runtime without a
    restart, which is mostly useful in tests.
    """
    if os.environ.get("LOG_FORMAT", "json").lower() == "text":
        return _format_text(level, msg, fields)
    return _format_json(level, msg, fields)


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


def _one_line(value: Any) -> str:
    """Render ``value`` so it cannot break the record it belongs to.

    The text format promises one line per record, and a message carrying a
    newline broke that promise in a way that mattered: the trailing part
    appeared on its own line, formatted like any other record and
    indistinguishable from one. Exception text routinely quotes caller input,
    so this is reachable without anyone intending it.

    The json format never had the problem — `json.dumps` escapes control
    characters — which is why this lives here rather than in `_emit`.
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _format_text(level: str, msg: str, fields: dict[str, Any]) -> str:
    """Compact human-readable single-line format."""
    ts = time.strftime("%H:%M:%S")
    logger = _one_line(_config.active().logger_name)
    parts = [f"{ts} {level.upper():<5} [{logger}] {_one_line(msg)}"]
    if (jid := job_id_var.get()) is not None:
        parts.append(f"job_id={_one_line(jid)}")
    for k, v in _caller_fields(fields).items():
        parts.append(f"{_one_line(k)}={_one_line(v)}")
    return " ".join(parts)


def _emit(level: str, msg: str, fields: dict[str, Any]) -> None:
    """Write one log line to stdout, then mirror it to the worker's second
    sink if it registered one. The stdout line fires first so RunPod's dashboard is never gated on an
    external collector being reachable, and a raising mirror is swallowed for
    the same reason — telemetry export must not be able to fail a job.
    """
    print(_render(level, msg, fields), file=sys.stdout, flush=True)

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
        # Built by the same formatter as every other record, so it carries the
        # timestamp and job id too. The failure path is where correlation
        # matters most: concurrent jobs, and a warning nobody can attribute to
        # the request whose export failed.
        #
        # Written straight to stdout rather than routed back through _emit():
        # a mirror that raises on every record would otherwise recurse.
        print(
            _render("warning", "log mirror raised", {"error_type": type(e).__name__}),
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
