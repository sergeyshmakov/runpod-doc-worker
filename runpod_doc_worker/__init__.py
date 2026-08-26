"""Engine-agnostic harness for RunPod serverless document-processing workers.

Subpackages:
  transport  — input fetch + format detection, outbound target checks, response
               packaging (tarball / inline / s3)
  obs        — failure-text redaction, structured logging, GPU + filesystem
               debug probes
  contract   — the artifact manifest a worker declares its outputs with, and
               what a response says when it could not carry all of it
  testing    — checks a worker repo can reuse in its own suite
  client     — the one subpackage that does not run inside a worker: reading a
               worker's response safely, for the code that calls one. Standard
               library only, so a client package can depend on it without
               pulling this package's transport stack into an end user's
               environment.

A worker supplies the engine and the entry point; see :mod:`runpod_doc_worker.config`
for the handful of values it has to declare about itself.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — for type checkers and editors only
    from runpod_doc_worker.config import WorkerConfig, active, configure

try:
    # Read from installed metadata rather than a literal: releases rewrite the
    # version in pyproject.toml only, so a hard-coded copy here would report
    # the wrong harness for the life of the package — and telling an operator
    # which harness is in an image is this string's whole job.
    __version__ = _pkg_version("runpod-doc-worker")
except PackageNotFoundError:  # pragma: no cover — running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = ["WorkerConfig", "active", "configure", "__version__"]

# Re-exported lazily (PEP 562) rather than imported at module scope.
#
# The client subpackage promises isolation from the rest of the harness, and an
# eager `from runpod_doc_worker.config import ...` here broke it invisibly:
# `import runpod_doc_worker.client` runs this initializer first, so
# `runpod_doc_worker.config` was always in `sys.modules` afterwards. The test
# guarding the promise looked for *heavy* modules and config is not heavy, so it
# passed while the isolation it was written to protect was already gone — the
# test asserted the symptom instead of the rule.
#
# Nothing about the worker-side API changes: `from runpod_doc_worker import
# WorkerConfig` still works and still imports config, just on the first access
# rather than on package import.
_LAZY = frozenset({"WorkerConfig", "active", "configure"})


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from runpod_doc_worker import config

        return getattr(config, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
