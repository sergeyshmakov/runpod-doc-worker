"""Three ways to ship an engine's output back to the caller.

- tarball_b64: base64-encoded archive embedded in the response
- inline:      the declared artifacts embedded directly in the response
- s3:          archive uploaded to an S3-compatible bucket, presigned URL returned

`archive_format` selects the container for the two archive transports
(tarball_b64 / s3): "tar.gz" (default) or "zip". Inline ignores it.

`formats` filters the inline payload — callers asking for `["markdown"]` only
get the markdown key back. For tarball_b64 and s3 the archive carries the whole
output directory regardless, so `formats` is a no-op on those.

What "the declared artifacts" means is the engine's to say; it hands in a
manifest of :class:`runpod_doc_worker.contract.artifacts.Artifact`.

Anything packaging had to leave out — an unreadable artifact, a member the
archive cannot carry — is reported on the entry under ``degraded``, and only
when there is something to report. See
:mod:`runpod_doc_worker.contract.degraded` for why that lives in the response
rather than only in the log.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Iterable

from runpod_doc_worker.contract import artifacts as _artifacts
from runpod_doc_worker.contract import degraded as _degraded
from runpod_doc_worker.transport import archive_build as _archive_build
from runpod_doc_worker.transport import archive_requirements as _requirements
from runpod_doc_worker.transport import response_size as _response_size


# The ways output can be returned. A worker's own schema is what rejects a bad
# value at the edge; this is the backstop for anything assembling the entry
# directly.
VALID_TRANSPORTS = frozenset({"tarball_b64", "inline", "s3"})

# Entry keys the harness is authoritative for. Neither an engine's metadata nor
# its artifact manifest may supply them: both are merged into the entry, so
# either could otherwise replace the field that says which document this is and
# where it came from.
RESERVED_ENTRY_KEYS = frozenset({"basename", "source", _degraded.ENTRY_KEY})


def _refuse_reserved(what: str, keys: set[str]) -> None:
    claimed = RESERVED_ENTRY_KEYS & keys
    if claimed:
        raise ValueError(
            f"{what} may not contain {', '.join(sorted(claimed))} — the harness "
            f"owns {', '.join(sorted(RESERVED_ENTRY_KEYS))} on a results entry"
        )


# Re-exported: two consumers alias `_build_tarball_bytes` / `_build_zip_bytes` from
# this module, and the tests reach for `_safe_arcname` and `_build_archive_bytes`.
# Named in `__all__` so an unused-import autofix cannot decide they are dead --
# which is exactly how a documented name was lost in this repository once already.
_archive_members = _archive_build._archive_members
_build_archive_bytes = _archive_build._build_archive_bytes
_build_tarball_bytes = _archive_build._build_tarball_bytes
_build_zip_bytes = _archive_build._build_zip_bytes
_safe_arcname = _archive_build._safe_arcname
_zip_info = _archive_build._zip_info

__all__ = [
    "MIN_PRESIGN_TTL_SECONDS",
    "RESERVED_ENTRY_KEYS",
    "VALID_TRANSPORTS",
    "_archive_members",
    "_build_archive_bytes",
    "_build_tarball_bytes",
    "_build_zip_bytes",
    "_safe_arcname",
    "_zip_info",
    "package_inline",
    "package_results_entry",
    "package_s3",
    "package_tarball",
    "presign_ttl_seconds",
]


def package_tarball(
    output_dir: Path,
    archive_format: str = "tar.gz",
    report: _degraded.Report | None = None,
    *,
    _required_members: _requirements.RequiredMembers | None = None,
) -> str:
    """Base64-encode the output archive for JSON transport.

    ``archive_format`` selects the container ("tar.gz" default, or "zip"); the
    response key is ``tarball_b64`` regardless.

    ``report`` collects any member the archive could not carry. Without one the
    omissions are still logged, but the response cannot say the archive is
    short of what the engine wrote.
    """
    return base64.b64encode(
        _build_archive_bytes(output_dir, archive_format, report, _required_members)
    ).decode("ascii")


def package_inline(
    output_dir: Path,
    basename: str,
    manifest: Iterable[_artifacts.Artifact],
    formats: Iterable[str] | None = None,
    report: _degraded.Report | None = None,
) -> dict[str, Any]:
    """Assemble the requested artifacts from the engine's output dir.

    ``formats`` is a subset of the manifest's keys; None means all of them.
    Only the requested keys appear in the returned dict — a filtered format is
    omitted, not present-as-empty. An artifact that is asked for but produced
    nothing appears with its declared default.
    """
    return _artifacts.resolve(
        manifest, output_dir, basename, keys=formats, report=report
    )


# Default presigned URL lifetime for `transport: "s3"` uploads.
# An hour is enough for a caller to fetch the tarball but short enough that a
# leaked URL stops working before it's interesting.
S3_PRESIGN_TTL_SECONDS = 3600

# Bounds for the BUCKET_PRESIGN_TTL_SECONDS override. The floor leaves a
# caller time to actually fetch the object; the ceiling is SigV4's own
# seven-day maximum, past which providers reject the signature.
MIN_PRESIGN_TTL_SECONDS = 60
MAX_PRESIGN_TTL_SECONDS = 604800


def presign_ttl_seconds() -> int:
    """Lifetime to sign output URLs with.

    BUCKET_PRESIGN_TTL_SECONDS overrides the default for callers who fetch
    promptly and would rather the URL not outlive that, or who need a longer
    window for a slow downstream job. Out-of-range and unparseable values
    clamp rather than fail: a bad value here would otherwise turn every
    successful parse into a failed job.
    """
    raw = os.environ.get("BUCKET_PRESIGN_TTL_SECONDS", "").strip()
    if not raw:
        return S3_PRESIGN_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError:
        return S3_PRESIGN_TTL_SECONDS
    return max(MIN_PRESIGN_TTL_SECONDS, min(MAX_PRESIGN_TTL_SECONDS, ttl))


def package_s3(
    output_dir: Path,
    basename: str,
    archive_format: str = "tar.gz",
    report: _degraded.Report | None = None,
    *,
    _required_members: _requirements.RequiredMembers | None = None,
) -> dict[str, Any]:
    """Upload the output archive to an S3-compatible bucket and return a
    presigned GET URL.

    ``archive_format`` selects the container ("tar.gz" default, or "zip"); it
    sets the object key extension and Content-Type. The response key is
    ``tarball_url`` regardless of container.

    Required worker env vars: BUCKET_ENDPOINT_URL, BUCKET_NAME,
    BUCKET_ACCESS_KEY_ID, BUCKET_SECRET_ACCESS_KEY. Optional:
    BUCKET_REGION (some providers need this; default empty), BUCKET_PREFIX
    (key path prefix inside the bucket; default empty).
    """
    endpoint = os.environ.get("BUCKET_ENDPOINT_URL", "").strip()
    bucket = os.environ.get("BUCKET_NAME", "").strip()
    access_key = os.environ.get("BUCKET_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("BUCKET_SECRET_ACCESS_KEY", "").strip()
    missing = [
        name for name, val in (
            ("BUCKET_ENDPOINT_URL", endpoint),
            ("BUCKET_NAME", bucket),
            ("BUCKET_ACCESS_KEY_ID", access_key),
            ("BUCKET_SECRET_ACCESS_KEY", secret_key),
        ) if not val
    ]
    if missing:
        raise ValueError(
            f"transport='s3' requires worker env vars: {', '.join(missing)}. "
            f"Set these in the RunPod endpoint env config and redeploy."
        )

    region = os.environ.get("BUCKET_REGION", "").strip() or None
    prefix = os.environ.get("BUCKET_PREFIX", "").strip().lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    # boto3 import is lazy so workers that never call transport='s3' don't pay
    # the ~50 MB cold-import cost.
    import boto3  # noqa: PLC0415
    from botocore.client import Config  # noqa: PLC0415

    archive_bytes = _build_archive_bytes(
        output_dir, archive_format, report, _required_members
    )
    ext = "zip" if archive_format == "zip" else "tar.gz"
    content_type = "application/zip" if archive_format == "zip" else "application/gzip"
    # Use a UUID so concurrent jobs with the same basename don't collide.
    import uuid  # noqa: PLC0415
    key = f"{prefix}{basename}-{uuid.uuid4().hex}.{ext}"

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        # SigV4 is required by most S3-compatible providers (R2, B2, MinIO).
        config=Config(signature_version="s3v4"),
    )
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=archive_bytes,
        ContentType=content_type,
    )
    ttl = presign_ttl_seconds()
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl,
    )
    return {
        "tarball_url": url,
        "tarball_url_expires_in": ttl,
        "bucket_key": key,
        "bucket_bytes": len(archive_bytes),
    }


def package_results_entry(
    *,
    transport: str,
    formats: Iterable[str],
    output_dir: Path,
    basename: str,
    source: str,
    manifest: Iterable[_artifacts.Artifact],
    archive_format: str = "tar.gz",
    metadata: dict[str, Any] | None = None,
    report: _degraded.Report | None = None,
    budget: _response_size.ResponseBudget | None = None,
) -> dict[str, Any]:
    """Build one entry of the ``results: [...]`` response array.

    Combines the per-file metadata with the transport-specific payload. For
    inline, ``formats`` selects which artifacts ride along; for tarball_b64 and
    s3 the archive carries the whole output directory regardless (so
    ``formats`` is a no-op on those paths).

    ``metadata`` is where an engine puts whatever it counts (pages requested,
    regions found, sheets read). The harness does not interpret it, but it does
    own the keys in :data:`RESERVED_ENTRY_KEYS`, so metadata may not claim
    those — silently losing the field that says where a document came from, or
    the one that says the response is incomplete, is worse than a loud
    rejection at the call site.

    Anything the packaging had to drop or substitute appears under
    ``degraded``, and only then: a job that lost nothing returns exactly what
    it always did. This is the entry point that attaches it, because it is the
    only one that builds something a caller reads. See
    :mod:`runpod_doc_worker.contract.degraded`.

    Pass ``report`` to accumulate that record across a whole job. Each entry
    still carries only the losses recorded while packaging that entry; the
    supplied report is what lets a worker count job-wide degradations without
    reading its responses back, or attach them to the span it already has open.

    Raises ``response_size.ResponseTooLargeError`` when the packaged entry exceeds
    what the gateway will deliver -- but only when ``ENFORCE_RESPONSE_CAP`` is on,
    which it is not by default. A handler that converts exceptions then reports it
    as ``ok=false`` instead of returning a response that is silently discarded.

    Pass one ``budget`` across every entry of a single response so the total is
    what is measured. Without it each entry is judged alone, and two that each fit
    can still form a response that does not.

    ``transport`` must be one of ``{"tarball_b64", "inline", "s3"}``. An
    unrecognised value raises: returning a successful entry carrying a
    different payload than the caller asked for is the kind of wrong that
    surfaces days later, in someone else's code.
    """
    if transport not in VALID_TRANSPORTS:
        raise ValueError(
            f"transport must be one of {sorted(VALID_TRANSPORTS)}; got {transport!r}"
        )
    _refuse_reserved("metadata", set(metadata or {}))
    # Materialised once, then used for both the check and the packaging.
    # `manifest` is typed as an Iterable, so a generator is a legal thing to
    # hand in — and reading it twice would leave the second read an exhausted
    # iterator, dropping every artifact from the response without a word.
    entries = _artifacts.validate(manifest)
    # The inline payload is merged in after metadata, so a manifest declaring
    # `source` would overwrite the envelope by the very route metadata cannot.
    # Checked on every transport: the archive paths do not read the manifest,
    # but a manifest that could corrupt an inline entry is a declaration bug
    # whichever transport happens to be asked for first.
    _refuse_reserved("manifest", {a.key for a in entries})

    entry: dict[str, Any] = {
        "basename": basename,
        "source": source,
        **(metadata or {}),
    }
    aggregate_report = report
    report = _degraded.Report()
    try:
        required_members = (
            _requirements.select(entries, output_dir, basename, report)
            if transport != "inline"
            else {}
        )
        if transport == "tarball_b64":
            entry["tarball_b64"] = package_tarball(
                output_dir,
                archive_format,
                report,
                _required_members=required_members,
            )
        elif transport == "s3":
            entry.update(
                package_s3(
                    output_dir,
                    basename,
                    archive_format,
                    report,
                    _required_members=required_members,
                )
            )
        else:  # inline
            entry.update(
                package_inline(
                    output_dir, basename, entries, formats=formats, report=report
                )
            )
    finally:
        if aggregate_report is not None:
            aggregate_report._extend(report)
    # Last, so a manifest or metadata key cannot land on top of it. Both are
    # refused above, but the ordering is what makes that a check rather than
    # the only thing standing between a caller and a response that says it is
    # complete when it is not.
    if (lost := report.entry()) is not None:
        entry[_degraded.ENTRY_KEY] = lost
    # Last. An oversized entry is discarded by the gateway, which then reports the
    # job COMPLETED with no output -- the caller pays for the parse to be told nothing.
    _response_size.refuse_if_undeliverable(
        entry, transport=transport, budget=budget
    )
    return entry
