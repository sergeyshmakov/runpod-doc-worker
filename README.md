# runpod-doc-worker

The engine-agnostic half of a RunPod serverless document-processing worker.

A worker on RunPod spends most of its code on things that have nothing to do
with the model it runs: pulling the input from a URL, a base64 blob or a network
volume; refusing a URL that resolves somewhere it should not; sniffing the
format; packing the result into a tarball, an inline payload or an S3 object;
emitting logs RunPod's viewer can filter; answering a probe job when the model
cache is not where it should be. This package is that half, so a worker repo can
be the engine and the entry point.

**Status: pre-release.** The package installs and its input transport, target
checks, packaging and artifact manifest are covered by tests, but there is no
handler in it yet — the job envelope, the stage pipeline and the worker
bootstrap land in following releases. Worker repositories already consume
pinned releases, so public behavior evolves through explicit compatibility
decisions, downstream verification and accurate release notes.

## Install

Pin a tag rather than a branch. No `git` binary is needed on the build node.
In a `requirements.txt`, use the PEP 508 direct-reference form so extras come
with it — a bare URL cannot carry them:

```
runpod-doc-worker[s3] @ https://github.com/sergeyshmakov/runpod-doc-worker/archive/refs/tags/v0.1.0.tar.gz
```

Extras: `s3` for the S3 transport (boto3), `metrics` for the metric catalog
(`opentelemetry-api`), `test` for the suite. Drop `[s3]` if the worker only
returns tarballs or inline payloads, and `[metrics]` if it exports none.

**Minimum Python is 3.10.12.** Not 3.10: `tarfile` gained the `data` extraction
filter in that patch release (June 2023), and the response reader depends on it
outright rather than carrying a hand-written copy of its permission rules. Pip
refuses the install below it, which is the intended behaviour — a worker whose
archive extraction silently applied different rules would be worse. Anything on
3.10.0–3.10.11 needs a patch upgrade, not a code change.

## What a worker declares

Two things, both data:

```python
from runpod_doc_worker import config
from runpod_doc_worker.config import DEFAULT_VOLUME_ROOTS
from runpod_doc_worker.contract.artifacts import Artifact

config.configure(config.WorkerConfig(
    env_prefix="ACME",              # ACME_ALLOW_LOCAL_FETCH, ACME_VOLUME_ROOTS, ...
    logger_name="acme-worker",
    volume_roots=DEFAULT_VOLUME_ROOTS + ("/worker",),   # where this image bakes files
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
| `config` | the handful of values a worker declares about itself |
| `transport.io` | fetch bytes from url / b64 / volume, detect format |
| `transport.net` | resolve and check outbound targets, connect only to checked addresses |
| `transport.package` | tarball / inline / s3 responses, presigned URLs |
| `obs.logging` | one JSON object per line on stdout, with a job-id contextvar |
| `obs.redact` | one readable shape for the text a failure reports |
| `obs.debug` | GPU inventory and the filesystem probe payload |
| `contract.artifacts` | the manifest a worker declares its outputs with |
| `contract.degraded` | what a response says when it could not carry all of them |
| `testing.hub` | hub.json checks a worker repo runs in its own suite |

## The client distribution

`runpod-doc-client` is a second distribution built from `client/` in this repo,
for code that *calls* a worker rather than being one: archive extraction, output
naming, bounded downloads, strict base64.

```
runpod-doc-client @ https://github.com/sergeyshmakov/runpod-doc-worker/archive/refs/tags/v0.6.0.tar.gz#subdirectory=client
```

It is separate because a client should not install the worker's transport stack
to read a response. Depending on `runpod-doc-worker` brings httpx, httpcore and
anyio whether anything imports them or not — the worker side subclasses types
from httpx and httpcore for its checked-target transport. Lazy imports keep those
out of `sys.modules`; only a separate distribution keeps them out of the image.

Neither distribution depends on the other. The worker does not import the client
half, and the client imports nothing outside the standard library.

## Licence

MIT.
