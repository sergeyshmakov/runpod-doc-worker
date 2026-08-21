"""Transactional staging of source files before archive mutation."""

from __future__ import annotations

import base64
import io
import stat
import tarfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from runpod_doc_worker.transport import archive_requirements
from runpod_doc_worker.transport import package


@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_an_archive_keeps_the_opened_file_type_when_its_path_becomes_a_directory(
    tmp_path, monkeypatch, archive_format
):
    member = tmp_path / "doc.md"
    original = b"# captured file\n"
    member.write_bytes(original)
    real_capture = archive_requirements.capture

    @contextmanager
    def capture(child, *args, **kwargs):
        with real_capture(child, *args, **kwargs) as snapshot:
            member.unlink()
            member.mkdir()
            yield snapshot

    monkeypatch.setattr(archive_requirements, "capture", capture)
    raw = base64.b64decode(package.package_tarball(tmp_path, archive_format))

    if archive_format == "zip":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.getinfo("doc.md")
            assert not info.is_dir()
            assert stat.S_ISREG(info.external_attr >> 16)
            assert archive.read(info) == original
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            info = archive.getmember("doc.md")
            assert info.isreg()
            assert archive.extractfile(info).read() == original


@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_large_archive_members_use_bounded_reads_and_roll_out_of_memory(
    tmp_path, monkeypatch, archive_format
):
    member = tmp_path / "large.bin"
    member.write_bytes(b"large member")
    spools = []
    readers = []
    real_spooled_file = archive_requirements.tempfile.SpooledTemporaryFile
    real_open = Path.open

    class BoundedReader:
        def __init__(self, source):
            self.source = source
            self.reads = 0

        def __getattr__(self, name):
            return getattr(self.source, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def read(self, size=-1):
            assert 0 < size <= 4
            self.reads += 1
            return self.source.read(size)

    def open_file(path, *args, **kwargs):
        source = real_open(path, *args, **kwargs)
        if path == member:
            reader = BoundedReader(source)
            readers.append(reader)
            return reader
        return source

    def spooled_file(*args, **kwargs):
        spool = real_spooled_file(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(archive_requirements, "_MEMBER_SPOOL_LIMIT_BYTES", 4)
    monkeypatch.setattr(archive_requirements, "_COPY_CHUNK_BYTES", 4)
    monkeypatch.setattr(
        archive_requirements.tempfile, "SpooledTemporaryFile", spooled_file
    )
    monkeypatch.setattr(Path, "open", open_file)

    raw = base64.b64decode(package.package_tarball(tmp_path, archive_format))

    assert len(readers) == 1 and readers[0].reads > 1 and readers[0].closed
    assert spools and all(spool._rolled and spool.closed for spool in spools)
    if archive_format == "zip":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert archive.read("large.bin") == b"large member"
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            assert archive.extractfile("large.bin").read() == b"large member"


@pytest.mark.parametrize("archive_format", ["tar.gz", "zip"])
def test_an_archive_writer_failure_aborts_instead_of_degrading(
    tmp_path, monkeypatch, archive_format
):
    (tmp_path / "member.txt").write_text("body", encoding="utf-8")

    def fail(*args, **kwargs):
        raise OSError("archive destination failed")

    if archive_format == "zip":
        monkeypatch.setattr(zipfile.ZipFile, "open", fail)
    else:
        monkeypatch.setattr(tarfile.TarFile, "addfile", fail)

    with pytest.raises(OSError, match="archive destination failed"):
        package.package_tarball(tmp_path, archive_format)
