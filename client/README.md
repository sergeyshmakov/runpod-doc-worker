# runpod-doc-client

Reading a `runpod-doc-worker` response safely: archives, filenames, base64
payloads, and bounded downloads. Standard library only.

## Why it is a separate distribution

A client reads a response. It does not need the worker's transport stack, and
installing `runpod-doc-worker` brings httpx, httpcore and anyio whether or not
anything imports them — the worker side subclasses types from httpx and httpcore
to build its checked-target transport.

Lazy imports keep those modules out of `sys.modules`. Only a separate
distribution keeps them out of the environment, which is the property a consumer
actually cares about when the environment is a container image.

## Install

```
runpod-doc-client @ https://github.com/sergeyshmakov/runpod-doc-worker/archive/refs/tags/v0.6.0.tar.gz#subdirectory=client
```

Nothing here depends on `runpod-doc-worker`, and `runpod-doc-worker` does not
depend on this — the two are independent and either can be installed alone.

## What it does

Everything treats the response as untrusted, because an archive extractor that
trusts member names is a path-traversal bug regardless of who wrote the archive,
and a `transport="s3"` result is fetched from a URL rather than read out of the
response body.

```python
from runpod_doc_client import ResponseError, decode_b64, download, extract

data = download(entry["tarball_url"])
extract(data, "out/")
```

Refusals arrive as `ResponseError` and nothing else — a malformed tar, a corrupt
zip, an HTTP failure and a decompression bomb all surface through the one type.
