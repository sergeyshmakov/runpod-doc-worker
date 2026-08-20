"""Per-worker configuration: the few values the harness cannot know itself.

Everything in this package is engine-agnostic, but three things still vary by
worker and cannot be derived: what its operator-facing env vars are called, what
its model weights are called on disk, and which extra env vars belong in the
probe dump. Those live here, set once at boot by the worker's entry point:

    from runpod_doc_worker import config

    config.configure(config.WorkerConfig(
        env_prefix="ACME",
        logger_name="acme-worker",
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


_TRUTHY = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class WorkerConfig:
    """What one worker tells the harness about itself.

    :param env_prefix: Prefix for every operator-facing env var this package
        reads, without the underscore. ``"ACME"`` yields
        ``ACME_ALLOW_LOCAL_FETCH``.
    :param logger_name: Value of the ``logger`` field on every structured log
        record, and the bracketed tag in text format.
    :param model_globs: Glob patterns matched against ``$HF_HOME/hub`` by
        :func:`runpod_doc_worker.obs.debug.find_model_dir` to report which
        weights a worker actually loaded. Empty means "do not look".
    :param probe_model_ids: ``org/name`` model ids the probe response resolves
        snapshot paths for, to diagnose a cache that is present but unreadable.
    :param probe_env_keys: Extra env var names to include in the probe's env
        dump, on top of the HuggingFace ones every worker shares.
    """

    env_prefix: str = "WORKER"
    logger_name: str = "worker"
    model_globs: tuple[str, ...] = ()
    probe_model_ids: tuple[str, ...] = ()
    probe_env_keys: tuple[str, ...] = field(default=())

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
