"""Choosing which container a response body is, and extracting it.

The order of the two checks is the whole content of this module, and it is not
arbitrary -- see the comment on ``extract``.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from runpod_doc_worker.client.errors import _DECOMPRESSION_ERRORS, ResponseError
from runpod_doc_worker.client.tarballs import _extract_tar, _looks_like_tar
from runpod_doc_worker.client.zips import _extract_zip


def extract(data: bytes, dest_dir: str | Path) -> Path:
    """Extract a tarball or zip into ``dest_dir``. Returns the directory.

    The container is detected from the bytes rather than from a field on the
    response, because the archive format is a *request* parameter and the response
    is what actually arrived.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        # `io.BytesIO(data)` raises a bare TypeError, and `extract` is exported
        # directly — a parsed response field honours its annotation only if the
        # worker sent what it promised, which is the same reason the URL, base64
        # and output-name helpers all check their own input.
        raise ResponseError(
            f"an archive should be bytes; got {type(data).__name__}"
        )
    try:
        # Accepting `memoryview` in the check above advertised an input this
        # function could not actually take: `BytesIO` needs a contiguous buffer,
        # so a strided view such as `memoryview(b"abcdef")[::2]` reached archive
        # detection and raised a bare BufferError. Normalising here is better
        # than narrowing the check, because a contiguous view is genuinely fine
        # and the copy only happens for the odd case.
        data = bytes(data)
    except (BufferError, ValueError, TypeError) as e:
        raise ResponseError(f"the archive payload could not be read: {e}") from e
    try:
        destination = Path(dest_dir).resolve()
    except (TypeError, ValueError, OSError, RuntimeError) as e:
        # Resolution happens before the guarded creation below, so it was outside
        # the contract: a NUL in the path raises ValueError, and a non-path
        # `dest_dir` raises TypeError from `Path()` itself.
        raise ResponseError(f"the destination is not a usable path: {e}") from e
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as e:
        # ValueError, not only OSError: a NUL in the path survives `Path()` and
        # `resolve()` and is rejected here as
        # `ValueError: embedded null character in path`. Guarding the resolve
        # alone left this one escaping, which the reproduction caught.
        # RuntimeError is a symlink loop, which `within` already handled and this
        # did not - the same call, two guards, one of them narrower. A destination
        # reused across extractions is exactly where such a link accumulates.
        #
        # `dest_dir` naming an existing regular file, or a parent that cannot be
        # written, raises before either archive helper runs — so the failure fell
        # outside the contract even though it happened inside the public call.
        raise ResponseError(f"the destination could not be created: {e}") from e
    # `is_zipfile` looks for the end-of-central-directory record, so it recognises
    # every valid layout. The signature check this replaces knew only the
    # local-file header `PK\x03\x04` — so an empty zip, which begins with the EOCD
    # signature `PK\x05\x06` and is exactly what packaging an empty directory
    # produces, was routed to the tar reader and rejected as unreadable.
    # Guarded because the *detection* can fail too: `is_zipfile` catches only
    # OSError internally, so a ValueError from a bad seek while reading the
    # end-of-central-directory record would propagate from here, ahead of either
    # helper and outside their handlers.
    #
    # Precautionary rather than reproduced — worth saying, because I first added
    # this believing it was where the corrupted-offset ValueError came from. It is
    # not: that one surfaces from `extractall` opening a member, and the fix for it
    # is in `_extract_zip`. Tracing beat guessing, and the guess looked right.
    # Tar first. `is_zipfile` looks for an end-of-central-directory record near the
    # *end* of the data and tolerates arbitrary bytes before it - that is how a
    # self-extracting zip works - so it answers "is there a zip in here
    # somewhere", not "is this a zip". A tar carrying a `nested.zip` member
    # therefore said True, the whole tar was read as a zip, and extraction
    # succeeded while returning only the nested archive's entries and silently
    # dropping every real member. A wrong answer with no error, which is the worst
    # shape available here.
    #
    # Tar detection has no such ambiguity and can decide first. The comment here
    # used to say `is_tarfile` reads header magic at a fixed offset, which is
    # wrong for a compressed tar and is a fair part of why the metadata bound was
    # missing from this path: detection *parses the first member*, decompressing
    # whatever that takes. So it goes through the same bounded parser extraction
    # uses.
    try:
        looks_like_tar = _looks_like_tar(data)
    except (ValueError, OSError, *_DECOMPRESSION_ERRORS) as e:
        raise ResponseError(f"the archive could not be read: {e}") from e
    if looks_like_tar:
        _extract_tar(data, destination)
        return destination

    try:
        looks_like_zip = zipfile.is_zipfile(io.BytesIO(data))
    except (ValueError, OSError, zipfile.BadZipFile) as e:
        raise ResponseError(f"the archive could not be read: {e}") from e
    if looks_like_zip:
        _extract_zip(data, destination)
    else:
        # Neither container. Reported through the tar reader so the message keeps
        # naming what is actually wrong with the bytes.
        _extract_tar(data, destination)
    return destination
