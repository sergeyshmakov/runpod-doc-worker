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
"""

from __future__ import annotations

import base64
import io
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Iterable

from runpod_doc_worker import paths as _paths
from runpod_doc_worker.contract import artifacts as _artifacts
from runpod_doc_worker.contract import degraded as _degraded


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


def _safe_arcname(name: str) -> bool:
    """Whether ``name`` is safe to write into an archive as a member name.

    Containment checks answer where a file *is*; this answers what the archive
    would *call* it, which is what an extractor acts on. A backslash is a legal
    character in a POSIX filename, so a file can sit legitimately inside the
    output directory under a name like ``..\\outside.txt`` — and an extractor
    that treats backslashes as separators then writes outside its destination.

    Both separator conventions are checked, because the archive is built on one
    platform and opened on another.
    """
    if not name or name in (".", ".."):
        return False
    if name.startswith(("/", "\\")):
        return False
    # A drive letter or a UNC prefix makes the name absolute on Windows.
    if len(name) >= 2 and name[1] == ":":
        return False
    parts = name.replace("\\", "/").split("/")
    return not any(part in ("", ".", "..") for part in parts)


def _archive_members(
    output_dir: Path, report: _degraded.Report | None = None
) -> list[Path]:
    """Regular files under ``output_dir`` that stay inside it, in a stable order.

    An entry that escapes is skipped rather than raised on: it is an artefact
    of how the engine laid out its own directory, and dropping a job over it
    would be a worse trade than shipping the rest. What was left out goes into
    ``report``, because an archive missing a file the engine wrote is otherwise
    indistinguishable from one the engine never wrote it into.
    """
    report = _degraded.sink(report)
    kept: list[Path] = []
    for child in sorted(output_dir.rglob("*")):
        if not child.is_file():
            continue
        where = _paths.relation(output_dir, child)
        if where != _paths.INSIDE:
            # Two different problems, and calling the second one an escape sends
            # a reader hunting a traversal that nothing has evidence of.
            report.note(
                reason=(
                    _degraded.OUTSIDE_OUTPUT_DIR
                    if where == _paths.OUTSIDE
                    else _degraded.UNRESOLVABLE
                ),
                file=child.name,
            )
            continue
        if not _safe_arcname(child.relative_to(output_dir).as_posix()):
            report.note(reason=_degraded.UNSAFE_NAME, file=child.name)
            continue
        kept.append(child)
    return kept


def _build_tarball_bytes(
    output_dir: Path, report: _degraded.Report | None = None
) -> bytes:
    """Gzip-tar the engine output dir; returns the raw bytes.

    ``dereference=True`` stores the bytes behind a symlink rather than the link
    itself. Without it a kept link is archived with its original absolute
    target, so the tarball extracts to a dangling path — or is refused by an
    extractor that checks — while the zip of the same output carries the file.
    A caller should get the same artifacts whichever container they asked for.

    Following links is safe here only because `_archive_members` has already
    dropped every member that resolves outside the output directory. The two
    belong together: dereferencing an unfiltered list is how the zip path was
    leaking in the first place.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", dereference=True) as tar:
        for child in _archive_members(output_dir, report):
            tar.add(child, arcname=child.relative_to(output_dir).as_posix())
    return buf.getvalue()


def _build_zip_bytes(
    output_dir: Path, report: _degraded.Report | None = None
) -> bytes:
    """Zip (DEFLATE) the engine output dir; returns the raw bytes.

    Used when a caller requests ``archive_format="zip"``, which is what a
    client emulating an upstream REST API needs when that API returns a `.zip`.

    Carries exactly the members `_build_tarball_bytes` does — both take their
    file list from `_archive_members`, so the two containers hold the same
    files under the same names, and neither carries a link out of the output
    directory.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for child in _archive_members(output_dir, report):
            zf.write(child, arcname=child.relative_to(output_dir).as_posix())
    return buf.getvalue()


def _build_archive_bytes(
    output_dir: Path,
    archive_format: str = "tar.gz",
    report: _degraded.Report | None = None,
) -> bytes:
    """Build the output archive in the requested container ("tar.gz" or "zip")."""
    if archive_format == "zip":
        return _build_zip_bytes(output_dir, report)
    return _build_tarball_bytes(output_dir, report)


def package_tarball(
    output_dir: Path,
    archive_format: str = "tar.gz",
    report: _degraded.Report | None = None,
) -> str:
    """Base64-encode the output archive for JSON transport.

    ``archive_format`` selects the container ("tar.gz" default, or "zip"); the
    response key is ``tarball_b64`` regardless.

    ``report`` collects any member the archive could not carry. Without one the
    omissions are still logged, but the response cannot say the archive is
    short of what the engine wrote.
    """
    return base64.b64encode(
        _build_archive_bytes(output_dir, archive_format, report)
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

    archive_bytes = _build_archive_bytes(output_dir, archive_format, report)
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
    report = _degraded.Report()
    if transport == "tarball_b64":
        entry["tarball_b64"] = package_tarball(output_dir, archive_format, report)
    elif transport == "s3":
        entry.update(package_s3(output_dir, basename, archive_format, report))
    else:  # inline
        entry.update(
            package_inline(output_dir, basename, entries, formats=formats, report=report)
        )
    # Last, so a manifest or metadata key cannot land on top of it. Both are
    # refused above, but the ordering is what makes that a check rather than
    # the only thing standing between a caller and a response that says it is
    # complete when it is not.
    if (lost := report.entry()) is not None:
        entry[_degraded.ENTRY_KEY] = lost
    return entry
