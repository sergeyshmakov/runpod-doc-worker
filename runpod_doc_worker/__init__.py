"""Engine-agnostic harness for RunPod serverless document-processing workers.

Subpackages:
  transport  — input fetch + format detection, outbound target checks, response
               packaging (tarball / inline / s3)
  obs        — failure-text redaction, structured logging, GPU + filesystem
               debug probes
  contract   — the artifact manifest a worker declares its outputs with
  testing    — checks a worker repo can reuse in its own suite

A worker supplies the engine and the entry point; see :mod:`runpod_doc_worker.config`
for the handful of values it has to declare about itself.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

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
