# runpod-doc-worker

The engine-agnostic half of a RunPod serverless document-processing worker.

A worker on RunPod spends most of its code on things that have nothing to do
with the model it runs: pulling the input from a URL, a base64 blob or a network
volume; refusing a URL that resolves somewhere it should not; sniffing the
format; packing the result into a tarball, an inline payload or an S3 object;
emitting logs RunPod's viewer can filter; answering a probe job when the model
cache is not where it should be. This package is that half, so a worker repo can
be the engine and the entry point.

**Status: pre-release.** The package installs and the transport layer is
covered, but there is no handler in it yet — the job envelope, the stage
pipeline and the worker bootstrap land in following releases. Nothing depends on
this yet; the API is expected to move.

## Install

Pin a tag rather than a branch. No `git` binary is needed on the build node:

```
https://github.com/sergeyshmakov/runpod-doc-worker/archive/refs/tags/v0.1.0.tar.gz
```

Extras: `s3` for the S3 transport, `otel` for telemetry export, `test` for the
suite.

## What a worker declares

Two things, both data:

```python
from runpod_doc_worker import config
from runpod_doc_worker.contract.artifacts import Artifact

config.configure(config.WorkerConfig(
    env_prefix="ACME",              # ACME_ALLOW_LOCAL_FETCH, ACME_VOLUME_ROOTS, ...
    logger_name="acme-worker",
    model_globs=("models--acme--parser*",),
))

MANIFEST = (
    Artifact("markdown", ("{basename}.md",), kind="text"),
    Artifact("blocks", ("{basename}_blocks.json",), kind="json", default=[]),
    Artifact("images", ("images/*",), kind="b64map"),
)
```

`env_prefix` is why adopting this package does not disturb a running endpoint:
the knobs an operator already sets keep the names they were documented under,
because the prefix comes from the worker rather than from here.

## Layout

| Module | What it does |
|---|---|
| `transport.io` | fetch bytes from url / b64 / volume, detect format |
| `transport.net` | resolve and check outbound targets, connect only to checked addresses |
| `transport.package` | tarball / inline / s3 responses, presigned URLs |
| `obs.logging` | one JSON object per line on stdout, with a job-id contextvar |
| `obs.redact` | one readable shape for the text a failure reports |
| `obs.debug` | GPU inventory and the filesystem probe payload |
| `contract.artifacts` | the manifest a worker declares its outputs with |
| `testing.hub` | hub.json checks a worker repo runs in its own suite |

## Licence

MIT.
