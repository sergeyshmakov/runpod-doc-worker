"""Turning an engine's output directory into archive bytes.

Separated from :mod:`runpod_doc_worker.transport.package`, which is about the shape
of a response entry. These functions are about the shape of an archive: which files
become members, what they are called inside it, and how the bytes are produced for
each container.

Everything here is private, and re-exported from ``package`` because two consumers
alias ``_build_tarball_bytes`` / ``_build_zip_bytes`` from there and one repository
has already lost a re-export to an unused-import autofix.
"""

from __future__ import annotations

import io
import os
import stat
import tarfile
import time
import zipfile
from pathlib import Path
from typing import BinaryIO

from runpod_doc_worker import paths as _paths
from runpod_doc_worker.contract import degraded as _degraded
from runpod_doc_worker.transport import archive_requirements as _requirements

__all__ = [
    "_archive_members",
    "_build_archive_bytes",
    "_build_tarball_bytes",
    "_build_zip_bytes",
    "_safe_arcname",
    "_zip_info",
]


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
    output_dir: Path,
    report: _degraded.Report | None = None,
    required_members: _requirements.RequiredMembers | None = None,
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
        what = _paths.kind(child)
        if what == _paths.UNRESOLVABLE:
            report.note(reason=_degraded.UNRESOLVABLE, file=child.name)
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
        # Skip only after containment: an ordinary in-tree directory is a
        # silent non-member, but an outside directory link is reported above.
        if what == _paths.DIRECTORY:
            continue
        if not _safe_arcname(child.relative_to(output_dir).as_posix()):
            report.note(reason=_degraded.UNSAFE_NAME, file=child.name)
            continue
        kept.append(child)
    _requirements.ensure_included(kept, required_members or {})
    return kept


def _build_tarball_bytes(
    output_dir: Path,
    report: _degraded.Report | None = None,
    required_members: _requirements.RequiredMembers | None = None,
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
    report = _degraded.sink(report)
    required_members = required_members or {}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", dereference=True) as tar:
        for child in _archive_members(output_dir, report, required_members):
            arcname = child.relative_to(output_dir).as_posix()
            required = required_members.get(child, ())

            def describe(source: BinaryIO) -> tarfile.TarInfo:
                info = tar.gettarinfo(fileobj=source, arcname=arcname)
                if info is None or not info.isreg():
                    raise IsADirectoryError(f"archive member is not a regular file: {child}")
                return info

            with _requirements.capture(child, required, report, describe) as snapshot:
                if snapshot is None:
                    continue
                snapshot.metadata.size = snapshot.size
                tar.addfile(snapshot.metadata, snapshot.data)
    return buf.getvalue()


def _zip_info(source: BinaryIO, arcname: str, child: Path) -> zipfile.ZipInfo:
    """Describe a regular ZIP member from its already-open source."""
    source_stat = os.fstat(source.fileno())
    if not stat.S_ISREG(source_stat.st_mode):
        raise IsADirectoryError(f"archive member is not a regular file: {child}")
    info = zipfile.ZipInfo(arcname, time.localtime(source_stat.st_mtime)[:6])
    info.external_attr = (source_stat.st_mode & 0xFFFF) << 16
    info.file_size = source_stat.st_size
    return info


def _build_zip_bytes(
    output_dir: Path,
    report: _degraded.Report | None = None,
    required_members: _requirements.RequiredMembers | None = None,
) -> bytes:
    """Zip (DEFLATE) the engine output dir; returns the raw bytes.

    Used when a caller requests ``archive_format="zip"``, which is what a
    client emulating an upstream REST API needs when that API returns a `.zip`.

    Carries exactly the members `_build_tarball_bytes` does — both take their
    file list from `_archive_members`, so the two containers hold the same
    files under the same names, and neither carries a link out of the output
    directory.
    """
    report = _degraded.sink(report)
    required_members = required_members or {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for child in _archive_members(output_dir, report, required_members):
            arcname = child.relative_to(output_dir).as_posix()
            required = required_members.get(child, ())

            def describe(source: BinaryIO) -> zipfile.ZipInfo:
                return _zip_info(source, arcname, child)

            with _requirements.capture(child, required, report, describe) as snapshot:
                if snapshot is None:
                    continue
                snapshot.metadata.file_size = snapshot.size
                snapshot.metadata.compress_type = zf.compression
                with zf.open(snapshot.metadata, mode="w") as destination:
                    while chunk := snapshot.data.read(_requirements._COPY_CHUNK_BYTES):
                        destination.write(chunk)
    return buf.getvalue()


def _build_archive_bytes(
    output_dir: Path,
    archive_format: str = "tar.gz",
    report: _degraded.Report | None = None,
    required_members: _requirements.RequiredMembers | None = None,
) -> bytes:
    """Build the output archive in the requested container ("tar.gz" or "zip")."""
    if archive_format == "zip":
        return _build_zip_bytes(output_dir, report, required_members)
    return _build_tarball_bytes(output_dir, report, required_members)
