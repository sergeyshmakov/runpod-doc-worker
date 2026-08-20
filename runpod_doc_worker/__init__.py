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

from runpod_doc_worker.config import WorkerConfig, active, configure

__version__ = "0.0.1"

__all__ = ["WorkerConfig", "active", "configure", "__version__"]
