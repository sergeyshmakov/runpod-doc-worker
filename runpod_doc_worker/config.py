"""Per-worker configuration: the few values the harness cannot know itself.

Everything in this package is engine-agnostic, but some things still vary by
worker and cannot be derived: what its operator-facing env vars are called,
where its inputs are allowed to come from, what its model weights are called on
disk, and which extra env vars belong in the probe dump. Those live here, set
once at boot by the worker's entry point:

    from runpod_doc_worker import config

    config.configure(config.WorkerConfig(
        env_prefix="ACME",
        logger_name="acme-worker",
        volume_roots=DEFAULT_VOLUME_ROOTS + ("/opt/acme",),
        model_globs=("models--acme--parser*",),
        probe_model_ids=("acme/parser-1.0",),
        probe_env_keys=("ACME_MODEL_SOURCE",),
    ))

``env_prefix`` is why an existing endpoint keeps working after a worker adopts
this package: the knobs its operators already set (``<PREFIX>_VOLUME_ROOTS``,
``<PREFIX>_ALLOW_LOCAL_FETCH``, ``<PREFIX>_DISABLE_PROBE``) keep the spellings
they were documented under, because the prefix comes from the worker rather than
from here.

The active config is process-wide state, which is deliberate: the modules that
read it are called from request paths that would otherwise have to thread a
config argument through every signature, and a worker process serves exactly one
engine for its lifetime. Reads go through :func:`active` at call time, not at
import time, so a worker may configure after importing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable


_TRUTHY = ("1", "true", "yes", "on")


# Directories a ``volume_path`` input may resolve under, before a worker says
# otherwise. These are places any RunPod worker can receive a file: the
# network-volume mount (``/runpod-volume``, or ``/workspace`` when an operator
# mounts it there) and the per-job temp tree.
#
# A path baked into a particular image — a fixture next to the handler, a
# pre-staged corpus — is that worker's own business and belongs in its
# ``volume_roots``, not here. See :class:`WorkerConfig`.
DEFAULT_VOLUME_ROOTS: tuple[str, ...] = (
    "/runpod-volume",
    "/workspace",
    "/tmp",
)


@dataclass(frozen=True)
class WorkerConfig:
    """What one worker tells the harness about itself.

    :param env_prefix: Prefix for every operator-facing env var this package
        reads, without the underscore. ``"ACME"`` yields
        ``ACME_ALLOW_LOCAL_FETCH``.
    :param logger_name: Value of the ``logger`` field on every structured log
        record, and the bracketed tag in text format.
    :param volume_roots: Directories a ``volume_path`` input may resolve under.
        A worker whose image bakes files somewhere adds that directory here;
        ``<PREFIX>_VOLUME_ROOTS`` still overrides the whole list at runtime.
    :param model_globs: Glob patterns matched against the effective Hugging
        Face Hub cache (``HF_HUB_CACHE``, legacy ``HUGGINGFACE_HUB_CACHE``, or
        the cache derived from ``HF_HOME``/``XDG_CACHE_HOME``) by
        :func:`runpod_doc_worker.obs.debug.find_model_dir` to report which
        weights a worker actually loaded. Empty means "do not look".
    :param probe_model_ids: ``org/name`` model ids the probe response resolves
        snapshot paths for, to diagnose a cache that is present but unreadable.
    :param probe_env_keys: Extra env var names to include in the probe's env
        dump, on top of the HuggingFace ones every worker shares.
    :param log_mirror: Optional second sink for log records, called as
        ``(level, msg, fields)`` after the stdout line is written. A worker with
        its own telemetry export registers it here; see
        :mod:`runpod_doc_worker.obs.logging`.
    """

    env_prefix: str = "WORKER"
    logger_name: str = "worker"
    volume_roots: tuple[str, ...] = DEFAULT_VOLUME_ROOTS
    model_globs: tuple[str, ...] = ()
    probe_model_ids: tuple[str, ...] = ()
    probe_env_keys: tuple[str, ...] = field(default=())
    log_mirror: Callable[[str, str, dict], None] | None = None

    def env_name(self, name: str) -> str:
        """Full env var name for ``name``, for reads and for error messages."""
        return f"{self.env_prefix}_{name}"

    def env(self, name: str, default: str = "") -> str:
        return os.environ.get(self.env_name(name), default)

    def truthy(self, name: str) -> bool:
        """Whether ``<PREFIX>_<name>`` is set to an affirmative value."""
        return self.env(name).strip().lower() in _TRUTHY


_active: WorkerConfig = WorkerConfig()


def configure(config: WorkerConfig) -> None:
    """Install ``config`` as the active one. Call once, at worker boot."""
    global _active
    _active = config


def active() -> WorkerConfig:
    """The active config. Defaults to a ``WORKER``-prefixed one until set."""
    return _active


def reset() -> None:
    """Restore the default config. For tests."""
    global _active
    _active = WorkerConfig()
