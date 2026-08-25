"""Archive fixtures the client test modules share.

Here rather than duplicated per module: several of these build a deliberately
malformed archive, and a second copy that drifted would test a different defect
from the one its name claims.
"""

from __future__ import annotations

import gzip
import io
import tarfile
import zipfile


def _tar_with(name: str, *, kind: str = "file") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name)
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
        else:
            info.size = 0
        tar.addfile(info, io.BytesIO(b""))
    return buffer.getvalue()


def _tar_of(names: list[str], *, size: int = 0, mode: str = "w") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as tar:
        for name in names:
            body = b"0" * size
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _zip_with(mutate) -> bytes:
    """A minimal valid zip, then `mutate(bytearray)` applied to it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.md", "x")
    raw = bytearray(buffer.getvalue())
    mutate(raw)
    return bytes(raw)


def _break_extract_version(raw: bytearray) -> None:
    raw[raw.find(b"PK\x01\x02") + 6] = 255


def _break_eocd_offset(raw: bytearray) -> None:
    at = raw.find(b"PK\x05\x06")
    raw[at + 16 : at + 20] = b"\xff\xff\xff\xff"


def _metadata_header(member_type: bytes, size: int, name: str) -> bytes:
    """A tar header announcing ``size`` bytes of metadata, and no payload.

    The payload is deliberately absent: the guard fires on the declared size
    before anything is read, so a fixture that actually carried the bytes would be
    proving the opposite of what is under test -- that this code can allocate
    them.
    """
    info = tarfile.TarInfo(name)
    info.type = member_type
    info.size = size
    return info.tobuf(tarfile.GNU_FORMAT)


def _zip64_archive(*, end_record: bool, entries: int = 41) -> bytes:
    """An archive with a real ZIP64 end record, which needs forcing.

    `force_zip64=True` on a member only puts a ZIP64 extra field in that member's
    directory record. The end record and locator are written when the *archive*
    exceeds a limit, so lowering the entry-count limit is what produces the layout
    under test -- and the previous version of this test did not, so it described a
    ZIP64 archive while building an ordinary one.
    """
    saved = zipfile.ZIP_FILECOUNT_LIMIT
    if end_record:
        zipfile.ZIP_FILECOUNT_LIMIT = 4
    try:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", allowZip64=True) as archive:
            for index in range(entries - 1):
                archive.writestr(f"f{index}.txt", "")
            with archive.open(
                zipfile.ZipInfo("big.bin"), "w", force_zip64=True
            ) as handle:
                handle.write(b"x")
        return buffer.getvalue()
    finally:
        zipfile.ZIP_FILECOUNT_LIMIT = saved


def _oversized_pax_gzip() -> bytes:
    """A tiny gzip whose first member carries a two-mebibyte PAX value.

    The point of the fixture is the ratio: the archive is small enough to pass
    every size check on the way in, and the metadata alone is larger than the
    limit. Compressing highly repetitive bytes is what makes that possible, which
    is why a byte-count check on the *response* cannot catch this.
    """
    value = b"x" * (2 * 1024 * 1024)
    prefix = len(b" path=\n") + len(str(len(value))) + len(value)
    records = b"%d path=%s\n" % (prefix, value)
    header = tarfile.TarInfo("pax_header")
    header.type = tarfile.XHDTYPE
    header.size = len(records)
    member = tarfile.TarInfo("doc.md")
    member.size = 0
    raw = (
        header.tobuf(tarfile.GNU_FORMAT)
        + records
        + b"\0" * ((-len(records)) % 512)
        + member.tobuf(tarfile.GNU_FORMAT)
        + b"\0" * 1024
    )
    return gzip.compress(raw, 9)
