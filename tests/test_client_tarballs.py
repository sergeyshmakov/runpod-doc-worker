"""Reading a tar: member rules, quotas, and the data filter."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest
from runpod_doc_client import (
    ResponseError,
    archives,
    extract,
    fetch,
    limits,
    names,
    tarballs,
    zips,
)
from runpod_doc_client.limits import MAX_METADATA_BYTES

from tests.client_fixtures import (
    _metadata_header,
)


def test_a_well_formed_tar_extracts(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("doc.md")
        body = b"# hello"
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    where = extract(buffer.getvalue(), tmp_path)
    assert (where / "doc.md").read_text(encoding="utf-8") == "# hello"


def test_a_tar_member_that_cannot_be_written_is_refused(tmp_path: Path) -> None:
    """A file member landing where a directory already exists raises
    IsADirectoryError or PermissionError. The zip path caught OSError; the tar path
    did not, so the same situation leaked or not depending on the container."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("collide")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    (tmp_path / "collide").mkdir()

    with pytest.raises(ResponseError, match="could not be extracted"):
        extract(buffer.getvalue(), tmp_path)


def test_a_tar_truncated_after_its_first_header_is_refused(tmp_path: Path) -> None:
    """`tarfile.open` succeeds and `getmembers()` raises, which is before the
    extraction handler that was the only one guarding this."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("a.txt")
        info.size = 4096
        tar.addfile(info, io.BytesIO(b"x" * 4096))

    with pytest.raises(ResponseError, match="could not be read"):
        extract(buffer.getvalue()[:1024], tmp_path)


def test_a_tar_with_a_corrupt_xz_stream_is_refused(tmp_path: Path) -> None:
    """`lzma.LZMAError` is neither an OSError nor a TarError.

    This one was not reported — it was found by asking whether the tar path had
    the zip path's gap. It surfaces from `getmembers()` rather than from
    `extractall()`, because enumerating a compressed tar decompresses the whole
    stream, so widening only the extraction handler left it escaping."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as tar:
        payload = ("hello " * 2000).encode()
        info = tarfile.TarInfo("doc.md")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    raw = bytearray(buffer.getvalue())
    midpoint = len(raw) // 2
    for index in range(midpoint, min(midpoint + 32, len(raw))):
        raw[index] ^= 0xFF

    with pytest.raises(ResponseError, match="could not be read"):
        extract(bytes(raw), tmp_path / "out")


@pytest.mark.filterwarnings("ignore:Python 3.14 will:DeprecationWarning")
def test_an_unusable_timestamp_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PAX mtime of nan, or one outside the platform's time_t range, reaches
    os.utime and raises there. Neither the member checks nor the `data` filter
    inspects mtime, so this applies to the modern path too.

    Forced rather than reproduced, and worth saying so: an out-of-range mtime is
    constructible (tarfile writes 1e18 happily) but Windows accepts it at utime,
    and a nan cannot be written by tarfile at all — `addfile` rejects it while
    building the PAX header. So this pins that the exception type is handled, not
    that a given platform raises it.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("doc.md")
        info.size = 0
        info.mtime = 1e18
        tar.addfile(info, io.BytesIO(b""))

    import os as _os

    real_utime = _os.utime

    def exploding_utime(path, times=None, **kwargs):
        raise OverflowError("timestamp out of range for platform time_t")

    monkeypatch.setattr(_os, "utime", exploding_utime)
    with pytest.raises(ResponseError, match="could not be extracted"):
        extract(buffer.getvalue(), tmp_path)
    assert real_utime is not None  # the monkeypatch is scoped to this test


@pytest.mark.parametrize(
    ("member_type", "name"),
    [
        (tarfile.XHDTYPE, "pax_header"),
        (tarfile.XGLTYPE, "pax_global_header"),
        (tarfile.GNUTYPE_LONGNAME, "././@LongLink"),
        (tarfile.GNUTYPE_LONGLINK, "././@LongLink"),
    ],
)
def test_oversized_member_metadata_is_refused(
    tmp_path: Path, member_type: bytes, name: str
) -> None:
    """`tar.next()` reads a metadata block whole before returning the member it
    describes, so both quotas ran too late to matter: a tiny compressed archive
    could make one call allocate megabytes while announcing an empty file, and a
    large enough one exhausts memory and leaks a raw `MemoryError`.

    All four metadata types, not just the PAX pair -- they share one dispatch
    point and therefore one guard.
    """
    payload = _metadata_header(member_type, MAX_METADATA_BYTES + 1, name)
    with pytest.raises(ResponseError, match="metadata"):
        extract(payload, tmp_path)


def test_a_sparse_member_is_not_treated_as_metadata() -> None:
    """A GNU sparse member's declared size is its file length, not a metadata
    block, so bounding it would refuse a large member that extracts perfectly
    well. Grouping by "reads something" rather than by what the number means is
    how a guard like this acquires a false positive."""
    assert tarfile.GNUTYPE_SPARSE not in limits._TAR_METADATA_TYPES


def test_a_long_path_carried_in_pax_metadata_still_extracts(tmp_path: Path) -> None:
    """The limit is 250 times the longest path a mainstream filesystem accepts, so
    a genuine PAX header -- which is how any path over 100 bytes is stored at all
    -- has to pass. A bound that refused those would break ordinary archives."""
    long_name = "/".join(["directory"] * 12) + "/report.md"
    assert len(long_name) > 100, "the fixture must actually need a PAX header"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo(long_name)
        info.size = 4
        tar.addfile(info, io.BytesIO(b"text"))
    extract(buffer.getvalue(), tmp_path)
    assert (tmp_path / long_name).read_bytes() == b"text"


def test_extraction_delegates_the_permission_rules_to_the_stdlib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`filter="data"` is passed, and nothing reimplements what it does.

    This replaces nine tests that checked a hand-written copy of the filter's
    permission rules, kept for interpreters older than 3.10.12. That copy was a
    second implementation of security-relevant behaviour and a recurring source of
    defects; the floor was raised instead. What is worth asserting now is
    the delegation itself, because losing it silently would reintroduce every one
    of them.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("payload.sh")
        info.size = 0
        info.mode = 0o4777
        info.uid, info.gid = 1234, 5678
        info.uname, info.gname = "attacker", "attacker"
        tar.addfile(info, io.BytesIO(b""))

    seen: list[dict[str, object]] = []
    real_extractall = tarfile.TarFile.extractall

    def recording_extractall(self, path=None, members=None, **kwargs):
        seen.append(dict(kwargs))
        return real_extractall(self, path, members, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "extractall", recording_extractall)
    extract(buffer.getvalue(), tmp_path)

    assert seen == [{"filter": "data"}], "the data filter was not the one thing used"
    if os.name == "posix":
        mode = (tmp_path / "payload.sh").stat().st_mode & 0o7777
        assert not mode & 0o7000, "a setuid/setgid/sticky bit survived"
        assert not mode & 0o022, "the archive dictated group/other write"
        assert mode & 0o600 == 0o600, "the extracted file is unusable"


def test_a_tar_member_resolving_onto_another_is_a_collision(tmp_path: Path) -> None:
    """`a/../b.txt` and `b.txt` are one file in a tar, and were not compared as one.

    The canonical form was written for zip, which *removes* parent components --
    so `a/../b.txt` folded to `a/b.txt`. A tar lets the filesystem resolve them,
    which makes the same member land at `b.txt`, and the two names therefore
    compared as different while the second overwrote the first. `within` cannot
    catch it either: the path stays inside the destination, so this is a
    collision and not an escape.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in ("a/../b.txt", "b.txt"):
            info = tarfile.TarInfo(name)
            info.size = 0
            tar.addfile(info, io.BytesIO(b""))
    with pytest.raises(ResponseError, match="same file"):
        extract(buffer.getvalue(), tmp_path)


def test_there_is_one_place_that_opens_a_tar() -> None:
    """The metadata bound is installed by an argument to `tarfile.open`, so it is a
    property of the call rather than of the package. A second call site is how it
    came to apply to extraction and not to detection, which makes "there is one"
    the invariant worth asserting."""
    source = "".join(
        Path(module.__file__).read_text(encoding="utf-8")
        for module in (archives, fetch, names, tarballs, zips)
    )
    assert source.count("tarfile.open(") == 1, (
        "every tar must be opened through _open_tar, which installs the bound"
    )


def test_the_metadata_budget_is_cumulative_not_only_per_header() -> None:
    """A hundred thousand members each just under the per-header cap is a hundred
    gigabytes, and none of it is transient: `tarfile` retains every `TarInfo` it
    has produced until enumeration ends.

    So the per-header limit bounded the wrong quantity, and the archive that
    defeats it is the ordinary one with many members rather than an exotic single
    header.
    """
    budget = tarballs._MetadataBudget()
    chunk = limits.MAX_METADATA_BYTES // 2
    spent = 0
    with pytest.raises(ResponseError, match="metadata in total"):
        for _ in range(10_000):
            budget.charge(chunk)
            spent += chunk
    assert spent <= limits.MAX_TOTAL_METADATA_BYTES + chunk, (
        "the budget must trip at its limit, not well past it"
    )


def test_an_ordinary_archive_is_nowhere_near_the_metadata_budget() -> None:
    """The guard. A PAX header exists to carry a long path, so a realistic archive
    spends kilobytes here -- refusing one would be worse than the bug."""
    budget = tarballs._MetadataBudget()
    for _ in range(1000):
        budget.charge(4096)
    assert budget.spent < limits.MAX_TOTAL_METADATA_BYTES


def test_each_archive_gets_its_own_budget() -> None:
    """Carried on the archive object rather than a module global, so two
    concurrent extractions cannot spend each other's allowance."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("doc.md")
        info.size = 0
        tar.addfile(info, io.BytesIO(b""))
    data = buffer.getvalue()
    first = tarballs._open_tar(data)
    second = tarballs._open_tar(data)
    try:
        assert first._runpod_metadata_budget is not second._runpod_metadata_budget
    finally:
        first.close()
        second.close()


def test_many_sub_limit_metadata_blocks_are_refused_through_extract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, because the unit tests on the budget do not prove it is wired.

    Disabling the charge left every budget test passing -- they call `charge`
    directly -- so this drives a real archive whose headers are each far under the
    per-header cap and whose total is over the budget. That is the shape the
    finding described: an ordinary archive with many members, rather than one
    exotic header.
    """
    monkeypatch.setattr(limits, "MAX_TOTAL_METADATA_BYTES", 64 * 1024)
    parts = []
    for index in range(40):
        value = b"x" * 4096
        records = b"%d path=%s\n" % (
            len(b" path=\n") + len(str(len(value))) + len(value),
            value,
        )
        header = tarfile.TarInfo(f"pax{index}")
        header.type = tarfile.XHDTYPE
        header.size = len(records)
        member = tarfile.TarInfo(f"doc{index}.md")
        member.size = 0
        parts.append(
            header.tobuf(tarfile.GNU_FORMAT)
            + records
            + b"\0" * ((-len(records)) % 512)
            + member.tobuf(tarfile.GNU_FORMAT)
        )
    payload = b"".join(parts) + b"\0" * 1024
    with pytest.raises(ResponseError, match="metadata in total"):
        extract(payload, tmp_path)


def test_an_archive_within_the_metadata_budget_still_extracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard on the above: the same shape, under the budget, has to work. A
    long path is stored in a PAX header, so this is what a real archive of deeply
    nested documents looks like."""
    monkeypatch.setattr(limits, "MAX_TOTAL_METADATA_BYTES", 1024 * 1024)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for index in range(20):
            info = tarfile.TarInfo("/".join(["directory"] * 10) + f"/doc{index}.md")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"text"))
    extract(buffer.getvalue(), tmp_path)
    assert list(tmp_path.rglob("doc0.md"))


def test_metadata_before_the_first_member_is_charged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tarfile.open` reads the first real member inside the constructor.

    It recursively consumes every metadata header ahead of that member on the way,
    so a budget attached to the *returned* object was never charged for any of
    them -- an archive could put unbounded metadata before its first file and the
    limit added to stop exactly that was inert there. The budget now rides on a
    per-call `TarInfo` subclass, which is the only channel open while
    `TarFile.__init__` is still running.
    """
    monkeypatch.setattr(limits, "MAX_TOTAL_METADATA_BYTES", 32 * 1024)
    parts = []
    for index in range(40):
        value = b"x" * 4096
        records = b"%d path=%s\n" % (
            len(b" path=\n") + len(str(len(value))) + len(value),
            value,
        )
        header = tarfile.TarInfo(f"pax{index}")
        header.type = tarfile.XHDTYPE
        header.size = len(records)
        parts.append(
            header.tobuf(tarfile.GNU_FORMAT)
            + records
            + b"\0" * ((-len(records)) % 512)
        )
    member = tarfile.TarInfo("doc.md")
    member.size = 0
    payload = b"".join(parts) + member.tobuf(tarfile.GNU_FORMAT) + b"\0" * 1024
    with pytest.raises(ResponseError, match="metadata in total"):
        extract(payload, tmp_path)


def test_a_global_header_is_bounded_more_tightly_than_a_member_one(
    tmp_path: Path,
) -> None:
    """A global PAX header's keys are copied into every member that follows.

    `_apply_pax_info` puts the whole dictionary on each `TarInfo`, and enumeration
    retains them all -- so its real cost is its size times the member count. A few
    kilobytes becomes gigabytes across a hundred thousand empty members while
    every individual header stays under both other limits, which is why this one
    is separate and much smaller.
    """
    value = b"y" * (limits.MAX_GLOBAL_METADATA_BYTES + 4096)
    records = b"%d comment=%s\n" % (
        len(b" comment=\n") + len(str(len(value))) + len(value),
        value,
    )
    header = tarfile.TarInfo("pax_global_header")
    header.type = tarfile.XGLTYPE
    header.size = len(records)
    payload = (
        header.tobuf(tarfile.GNU_FORMAT)
        + records
        + b"\0" * ((-len(records)) % 512)
        + b"\0" * 1024
    )
    with pytest.raises(ResponseError, match="global tar header"):
        extract(payload, tmp_path)
