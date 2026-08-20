# Security policy

## Reporting a vulnerability

Please use [GitHub Security Advisories](https://github.com/sergeyshmakov/runpod-doc-worker/security/advisories/new) to report security issues privately. Do **not** open public issues for security problems.

You should expect an initial response within 5 working days.

## Scope

This package sits on the boundary between a caller's input and a worker's filesystem and network, so most of what matters is here rather than in the worker repos that use it.

In scope:

- Path traversal via `volume_path` or `basename`, including through symlinks and `..` segments
- Outbound target checks: a URL that passes `transport.net` but connects somewhere else — a redirect hop that escapes the check, a resolve-then-connect gap, a proxy path that bypasses it
- Resource exhaustion through crafted input that bypasses the documented limits (`MAX_INLINE_FILE_MB`, `MAX_URL_FILE_MB`, fetch timeouts)
- Archive construction: entries that escape the extraction directory on the client side, or names that collide across concurrent jobs
- Credential exposure through this package's code paths — `BUCKET_*` values in logs, presigned URLs in responses or traces, secrets surviving `obs.redact`
- S3 transport: presigned URL leakage, bucket-key collisions, signature replay

Out of scope (please report upstream):

- Vulnerabilities in an engine a worker runs, or in the model weights it loads — report to that project
- Vulnerabilities in httpx, boto3, the RunPod platform or SDK, base images, or transitive dependencies — report to their maintainers
- A worker repo's own handler, deploy scripts or Dockerfile — report to that repo

## Hardening notes for operators

- Treat `volume_path` as a privileged input. Mount only volumes you control, narrow `<PREFIX>_VOLUME_ROOTS` to the subtree a worker actually needs, and validate any path your own code builds from user input.
- `<PREFIX>_ALLOW_LOCAL_FETCH=1` disables the routability requirement on fetched URLs. It exists for local development and for operators serving documents from inside their own network — do not set it on an endpoint that accepts caller-supplied URLs.
- Treat a presigned URL as a short-lived bearer credential. The default lifetime is one hour; `BUCKET_PRESIGN_TTL_SECONDS` adjusts it within SigV4's bounds. Don't log it.
- Pass credentials (`BUCKET_SECRET_ACCESS_KEY`, API keys) through the endpoint's environment, never through job input, and never commit them.
- Set the endpoint's execution timeout low enough that a stuck job cannot run up a GPU bill.
