"""Every operator-facing env var actually reads the worker's prefix.

This is the promise adoption rests on: a worker installs its own prefix and the
knobs its operators already set keep working. Nothing else in the suite touches
a non-default config, so a module that quietly went back to a literal
``os.environ[...]`` would pass everywhere else.

Each case therefore asserts BOTH directions — the prefixed name is honoured,
and the name it would have had under a different prefix is ignored.
"""

from __future__ import annotations

import json

import pytest

from runpod_doc_worker import config
from runpod_doc_worker.obs import debug as worker_debug
from runpod_doc_worker.obs import logging as worker_logging
from runpod_doc_worker.transport import io as worker_io
from runpod_doc_worker.transport import net as worker_net


PREFIX = "ACME"


@pytest.fixture
def configured():
    config.configure(config.WorkerConfig(env_prefix=PREFIX, logger_name="acme-worker"))
    yield
    config.reset()


# -----------------------------------------------------------------------------
# The env-reading call sites
# -----------------------------------------------------------------------------

def test_volume_roots_reads_the_configured_prefix(configured, monkeypatch, tmp_path):
    monkeypatch.setenv(f"{PREFIX}_VOLUME_ROOTS", str(tmp_path))
    assert [str(p) for p in worker_io.volume_roots()] == [str(tmp_path)]


def test_volume_roots_ignores_another_prefix(configured, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKER_VOLUME_ROOTS", str(tmp_path))
    assert str(tmp_path) not in [str(p) for p in worker_io.volume_roots()]


def test_allow_local_fetch_reads_the_configured_prefix(configured, monkeypatch):
    monkeypatch.setenv(f"{PREFIX}_ALLOW_LOCAL_FETCH", "1")
    assert worker_net.allow_local_targets() is True


def test_allow_local_fetch_ignores_another_prefix(configured, monkeypatch):
    monkeypatch.setenv("WORKER_ALLOW_LOCAL_FETCH", "1")
    assert worker_net.allow_local_targets() is False


def test_nothing_here_reads_a_probe_environment_variable(configured, monkeypatch):
    """The gate this package used to own is gone, and this is what keeps it
    gone. Whether a caller may ask for diagnostics depends on who a worker's
    callers are, which is knowable in a worker and not here — and while it was
    guessed at here, the name and the default of an operator-facing knob moved
    twice in two releases.
    """
    for spelling in ("ENABLE_PROBE", "DISABLE_PROBE"):
        monkeypatch.setenv(f"{PREFIX}_{spelling}", "0")

    # Reachable, and reachable identically whatever those variables say.
    assert isinstance(worker_debug.probe_filesystem(), dict)
    assert not hasattr(worker_debug, "probe_enabled")
    assert not hasattr(config.WorkerConfig(), "probe_default")


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("", False), ("maybe", False),
])
def test_truthy_spellings(configured, monkeypatch, value, expected):
    monkeypatch.setenv(f"{PREFIX}_SOMETHING", value)
    assert config.active().truthy("SOMETHING") is expected


# -----------------------------------------------------------------------------
# Error messages name the knob an operator would actually set
# -----------------------------------------------------------------------------

def test_volume_path_error_names_the_prefixed_knob(configured, monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "doc.pdf"
    monkeypatch.setenv(f"{PREFIX}_VOLUME_ROOTS", str(root))
    with pytest.raises(ValueError, match=f"{PREFIX}_VOLUME_ROOTS"):
        worker_io.resolve_volume_file(str(outside))


def test_non_routable_error_names_the_prefixed_knob(configured, monkeypatch):
    monkeypatch.setattr(
        worker_net, "_addresses_for", lambda host, port: ["127.0.0.1"],
    )
    with pytest.raises(ValueError, match=f"{PREFIX}_ALLOW_LOCAL_FETCH"):
        worker_net.resolve_checked_host("localhost", 80, field="file_url")


# -----------------------------------------------------------------------------
# Non-env config
# -----------------------------------------------------------------------------

def test_logger_name_comes_from_config(configured, monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "json")
    worker_logging.info("hello")
    record = json.loads(capsys.readouterr().out.strip())
    assert record["logger"] == "acme-worker"


def test_volume_roots_default_to_the_configured_list(monkeypatch):
    monkeypatch.delenv("WORKER_VOLUME_ROOTS", raising=False)
    config.configure(config.WorkerConfig(volume_roots=("/opt/acme", "/tmp")))
    try:
        assert [str(p) for p in worker_io.volume_roots()] == [
            str(worker_io.Path("/opt/acme")), str(worker_io.Path("/tmp")),
        ]
    finally:
        config.reset()


def test_env_override_beats_the_configured_roots(monkeypatch, tmp_path):
    """An operator can still narrow or move the roots on a live endpoint."""
    config.configure(config.WorkerConfig(volume_roots=("/opt/acme",)))
    try:
        monkeypatch.setenv("WORKER_VOLUME_ROOTS", str(tmp_path))
        assert [str(p) for p in worker_io.volume_roots()] == [str(tmp_path)]
    finally:
        config.reset()


def test_configure_is_visible_to_modules_that_imported_config_earlier():
    """Modules read active() at call time, so a worker may configure after import."""
    before = worker_io.volume_roots()
    config.configure(config.WorkerConfig(volume_roots=("/opt/acme",)))
    try:
        assert worker_io.volume_roots() != before
    finally:
        config.reset()
    assert worker_io.volume_roots() == before


def test_reset_restores_the_default_prefix(configured):
    assert config.active().env_prefix == PREFIX
    config.reset()
    assert config.active().env_prefix == "WORKER"


def test_harness_default_roots_carry_no_image_layout():
    """The defaults are places any RunPod worker can receive a file. A path that
    exists because some image put it there belongs in that worker's config."""
    assert config.DEFAULT_VOLUME_ROOTS == ("/runpod-volume", "/workspace", "/tmp")


# -----------------------------------------------------------------------------
# The log mirror
# -----------------------------------------------------------------------------

def test_log_mirror_receives_every_record(monkeypatch, capsys):
    seen = []
    config.configure(config.WorkerConfig(log_mirror=lambda lvl, msg, f: seen.append((lvl, msg, f))))
    try:
        monkeypatch.setenv("LOG_FORMAT", "json")
        worker_logging.info("hello", backend="local")
        worker_logging.error("boom")
    finally:
        config.reset()
    capsys.readouterr()
    assert [(lvl, msg) for lvl, msg, _ in seen] == [("info", "hello"), ("error", "boom")]
    assert seen[0][2]["backend"] == "local"


def test_log_mirror_gets_the_job_id(monkeypatch, capsys):
    seen = []
    config.configure(config.WorkerConfig(log_mirror=lambda lvl, msg, f: seen.append(f)))
    try:
        worker_logging.job_id_var.set("job-42")
        worker_logging.info("hello")
    finally:
        worker_logging.job_id_var.set(None)
        config.reset()
    capsys.readouterr()
    assert seen[0]["job_id"] == "job-42"


def test_a_raising_mirror_cannot_fail_the_caller(monkeypatch, capsys):
    """Telemetry export is additive. It must never be able to fail a job."""
    def boom(level, msg, fields):
        raise RuntimeError("collector unreachable")

    config.configure(config.WorkerConfig(log_mirror=boom))
    try:
        worker_logging.info("hello")
    finally:
        config.reset()
    out = capsys.readouterr().out
    assert "hello" in out
    assert "log mirror raised" in out


def test_no_mirror_configured_is_a_no_op(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "json")
    worker_logging.info("hello")
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
