"""Reading a zip safely: the central directory, the quotas, and the member names.

The recurring difficulty here is that ``zipfile`` answers questions about what an
archive *claims* rather than what it contains, so the preflight walks the central
directory itself rather than trusting the counts in the end record.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from runpod_doc_client import limits
from runpod_doc_client.errors import _DECOMPRESSION_ERRORS, ResponseError
from runpod_doc_client.names import (
    _check_member_collisions,
    _check_member_name,
    within,
)


def _counted_zip_entries(data: bytes, limit: int) -> int | None:
    """Central-directory records, counted by walking them, capped at ``limit``.

    Not the count in the end-of-central-directory record. That field is a claim,
    and ``ZipFile`` does not act on it: it walks the central directory for the
    declared *byte size*, so an archive can carry a thousand entries while saying
    one and still have every ``ZipInfo`` materialised. A preflight that trusts the
    number is therefore no protection against the case it exists for -- which is
    what the previous version of this did.

    Walking is bounded two ways: it stops as soon as the count exceeds ``limit``,
    so a hostile archive costs a fixed amount of work, and it refuses to read
    outside the declared directory. Returns None if the structure cannot be
    followed, leaving ``ZipFile`` as the authority on readability.
    """
    tail = data[-(65_536 + 22):]
    base = len(data) - len(tail)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        return None
    size = int.from_bytes(tail[at + 12 : at + 16], "little")
    offset = int.from_bytes(tail[at + 16 : at + 20], "little")
    if size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        at64 = tail.rfind(b"PK\x06\x06", 0, at)
        if at64 < 0 or len(tail) - at64 < 56:
            return None
        size = int.from_bytes(tail[at64 + 40 : at64 + 48], "little")
        offset = int.from_bytes(tail[at64 + 48 : at64 + 56], "little")
    # A self-extracting zip carries a stub before the archive and the EOCD offsets
    # are relative to the *embedded* archive, so the discrepancy has to be measured
    # and added -- but a ZIP64 archive puts its own end record and locator
    # *between* the directory and the EOCD, so measuring as far as the EOCD counts
    # those 76 bytes as stub. With either feature alone one candidate was right;
    # with both, the correction was off by the ZIP64 records and neither candidate
    # landed on the directory, so the walk gave up on exactly the large archives
    # the preflight exists for.
    #
    # So the measurement runs to where the directory actually ends -- the ZIP64
    # end record if there is one, the EOCD otherwise -- and each candidate is then
    # checked for a central-directory signature rather than trusted.
    eocd_at = base + at
    directory_end = eocd_at
    locator = tail.rfind(b"PK\x06\x07", 0, at)
    if locator >= 0 and at - locator == 20:
        directory_end = base + locator
        zip64_at = tail.rfind(b"PK\x06\x06", 0, locator)
        if zip64_at >= 0 and locator - zip64_at >= 56:
            directory_end = base + zip64_at

    candidates = [offset]
    prepended = directory_end - (offset + size)
    if prepended > 0:
        candidates.append(offset + prepended)
    offset = -1
    for candidate in candidates:
        if 0 <= candidate <= len(data) - 4 and data[candidate : candidate + 4] == (
            b"PK\x01\x02"
        ):
            offset = candidate
            break
    if offset < 0 or size < 0 or offset + size > len(data):
        return None
    end = offset + size

    count = 0
    cursor = offset
    while cursor + 46 <= end:
        if data[cursor : cursor + 4] != b"PK\x01\x02":
            return None
        name_len = int.from_bytes(data[cursor + 28 : cursor + 30], "little")
        extra_len = int.from_bytes(data[cursor + 30 : cursor + 32], "little")
        comment_len = int.from_bytes(data[cursor + 32 : cursor + 34], "little")
        cursor += 46 + name_len + extra_len + comment_len
        count += 1
        if count > limit:
            return count
    return count


def _extract_zip(data: bytes, destination: Path) -> None:
    counted_entries = _counted_zip_entries(data, limits.MAX_ARCHIVE_MEMBERS)
    if counted_entries is not None and counted_entries > limits.MAX_ARCHIVE_MEMBERS:
        raise ResponseError(
            f"the archive contains over {limits.MAX_ARCHIVE_MEMBERS} members"
        )
    try:
        zip_file = zipfile.ZipFile(io.BytesIO(data))
    except (
        zipfile.BadZipFile,
        UnicodeDecodeError,
        NotImplementedError,
        ValueError,
        OSError,
    ) as e:
        # Reachable for the same reason the tar path is: `extract` chooses the
        # container from the leading bytes of whatever actually arrived.
        #
        # UnicodeDecodeError because a central-directory entry may set the UTF-8
        # flag and then carry bytes that are not UTF-8. `is_zipfile` still says
        # yes — it only looks for the end-of-central-directory record — so the
        # constructor is where it surfaces, and it is neither a BadZipFile nor an
        # OSError.
        #
        # NotImplementedError comes from an unsupported `extract_version`, and
        # ValueError from a central-directory offset that seeks negative. The
        # pattern across all four: `is_zipfile` looks only for the
        # end-of-central-directory record, so every field *inside* the container
        # is still untrusted and the constructor is where each is first read.
        raise ResponseError(f"the archive could not be read: {e}") from e

    with zip_file as archive:
        infos = archive.infolist()
        if len(infos) > limits.MAX_ARCHIVE_MEMBERS:
            raise ResponseError(
                f"the archive declares {len(infos)} members, over the "
                f"{limits.MAX_ARCHIVE_MEMBERS} limit"
            )
        # Checked before writing anything. A small archive can declare an enormous
        # expansion - the classic decompression bomb - and the download cap says
        # nothing about it, because what that bounded was the *compressed* form.
        declared = sum(info.file_size for info in infos)
        if declared > limits.MAX_EXTRACTED_BYTES:
            raise ResponseError(
                f"the archive expands to {declared} bytes, over the "
                f"{limits.MAX_EXTRACTED_BYTES}-byte limit"
            )
        _check_member_collisions(archive.namelist(), container="zip", destination=destination)
        for name in archive.namelist():
            _check_member_name(name, container="zip")
            if not within(destination, name):
                raise ResponseError(
                    f"refusing zip member {name!r}: path escapes the destination"
                )
        try:
            archive.extractall(destination)  # noqa: S202 — names checked above
        except (
            zipfile.BadZipFile,
            OSError,
            RuntimeError,
            NotImplementedError,
            ValueError,
            OverflowError,
            *_DECOMPRESSION_ERRORS,
        ) as e:
            # RuntimeError is what an encrypted member raises, and
            # NotImplementedError an unsupported compression method. Both describe
            # an archive this code cannot read rather than a programming error, and
            # treating an untrusted archive as untrusted means they belong inside
            # the contract.
            #
            # ValueError is what a corrupted local-header offset produces: opening
            # a member seeks to it, and a negative result raises
            # `ValueError: negative seek value` rather than BadZipFile. The central
            # directory parsed, so nothing earlier had reason to object.
            #
            # The decompression errors cover the case the header cannot reveal: a
            # zip whose central directory is intact — so `is_zipfile` says yes and
            # `ZipFile()` opens it — but whose deflate payload has a flipped byte.
            # That surfaces as a raw `zlib.error` only once extraction inflates it.
            raise ResponseError(f"the archive could not be extracted: {e}") from e
