"""Reading a zip: the central directory, quotas, and member names."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from runpod_doc_worker.client import (
    ResponseError,
    extract,
    limits,
)
from tests.client_fixtures import (
    _break_eocd_offset,
    _break_extract_version,
    _zip_with,
)


def test_a_traversing_zip_member_is_refused(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escaped.txt", "x")
    with pytest.raises(ResponseError, match="escapes the destination"):
        extract(buffer.getvalue(), tmp_path)


def test_a_well_formed_zip_extracts(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.md", "# hello")
    where = extract(buffer.getvalue(), tmp_path)
    assert (where / "doc.md").read_text(encoding="utf-8") == "# hello"


def test_an_empty_zip_is_recognised(tmp_path: Path) -> None:
    """A valid empty zip begins with the end-of-central-directory signature
    `PK\x05\x06`, not the local-file header the old check looked for — so it was
    routed to the tar reader and rejected as unreadable. Packaging an empty
    directory produces exactly this."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w"):
        pass
    assert buffer.getvalue()[:4] == b"PK\x05\x06"
    assert extract(buffer.getvalue(), tmp_path) == tmp_path.resolve()


def test_zip_extraction_failures_that_are_not_bad_zip_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An encrypted member raises RuntimeError and an unsupported compression
    method NotImplementedError. Neither is a programming error; both describe an
    archive this code cannot read."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.md", "# hi")

    for raised in (RuntimeError("File doc.md is encrypted"), NotImplementedError()):
        monkeypatch.setattr(
            zipfile.ZipFile,
            "extractall",
            lambda *a, _e=raised, **k: (_ for _ in ()).throw(_e),
        )
        with pytest.raises(ResponseError, match="could not be extracted"):
            extract(buffer.getvalue(), tmp_path)


def test_a_zip_with_a_corrupt_deflate_stream_is_refused(tmp_path: Path) -> None:
    """Intact central directory, damaged payload: `is_zipfile` says yes and
    `ZipFile()` opens it, so the damage surfaces as a raw `zlib.error` only when
    extraction inflates the member."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.md", "hello " * 400)
    raw = bytearray(buffer.getvalue())
    raw[45] ^= 0xFF
    raw[46] ^= 0xFF

    assert zipfile.is_zipfile(io.BytesIO(bytes(raw))), "the container must still parse"
    with pytest.raises(ResponseError, match="could not be extracted"):
        extract(bytes(raw), tmp_path / "out")


def test_a_zip_with_invalid_utf8_names_is_refused(tmp_path: Path) -> None:
    """An entry may set the UTF-8 flag and carry bytes that are not UTF-8.

    `is_zipfile` still says yes — it only looks for the end-of-central-directory
    record — so `ZipFile()` is where it surfaces, as a UnicodeDecodeError that is
    neither BadZipFile nor OSError.

    Built by writing a non-ASCII name so zipfile sets the flag itself, then
    replacing the encoded bytes with invalid ones of the same length, so the flag
    is genuinely set rather than poked in by hand.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("\u00e9.md", "x")

    raw = bytearray(buffer.getvalue())
    valid = "\u00e9.md".encode()
    invalid = b"\xff\xfe.md"
    assert len(valid) == len(invalid), "the patch must not move any offsets"
    patched = 0
    start = 0
    while True:
        at = raw.find(valid, start)
        if at < 0:
            break
        raw[at : at + len(valid)] = invalid
        patched += 1
        start = at + len(invalid)
    assert patched, "the name was not found to patch"
    assert zipfile.is_zipfile(io.BytesIO(bytes(raw))), "the container must still parse"

    with pytest.raises(ResponseError, match="could not be read"):
        extract(bytes(raw), tmp_path)


@pytest.mark.parametrize(
    ("mutate", "label"),
    [
        (_break_extract_version, "unsupported extract_version"),
        (_break_eocd_offset, "central-directory offset that seeks negative"),
    ],
)
def test_a_structurally_broken_zip_is_refused(mutate, label: str, tmp_path: Path) -> None:
    """`is_zipfile` looks only for the end-of-central-directory record, so every
    field *inside* the container is still untrusted — an unsupported version
    raises NotImplementedError and a bad offset raises ValueError, neither of them
    a BadZipFile.

    The offset case surfaces from `extractall` opening a member, not from the
    constructor. I first guarded the wrong call believing otherwise; the traceback
    settled it, and the guess had looked right.
    """
    data = _zip_with(mutate)
    assert zipfile.is_zipfile(io.BytesIO(data)), f"{label}: container must still parse"
    with pytest.raises(ResponseError):
        extract(data, tmp_path)


def test_a_well_formed_zip_still_extracts_after_the_widened_guards(tmp_path: Path) -> None:
    """ValueError and OSError are broad, so this pins that the happy path is
    unaffected by adding them."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.md", "# hello")
    where = extract(buffer.getvalue(), tmp_path)
    assert (where / "doc.md").read_text(encoding="utf-8") == "# hello"


def test_a_tar_containing_a_zip_is_extracted_as_a_tar(tmp_path: Path) -> None:
    """The P1, and the worst-shaped bug in this module so far: a wrong answer
    with no error at all.

    `is_zipfile` looks for an end-of-central-directory record near the end of the
    data and tolerates arbitrary bytes before it, which is how a self-extracting
    zip works. So it answers "is there a zip in here somewhere", not "is this a
    zip". A tar carrying a `nested.zip` member said True, the whole tar was read
    as a zip, and extraction succeeded while returning only the nested archive's
    entries and dropping every real member.
    """
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as nested:
        nested.writestr("nested-only.txt", "i am inside the nested zip")

    outer = io.BytesIO()
    with tarfile.open(fileobj=outer, mode="w") as tar:
        for name, body in (("doc.md", b"# the real document"), ("nested.zip", inner.getvalue())):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))

    assert zipfile.is_zipfile(io.BytesIO(outer.getvalue())), (
        "the premise: is_zipfile still says yes, which is why tar has to be asked first"
    )
    extract(outer.getvalue(), tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["doc.md", "nested.zip"]


def test_a_real_zip_is_still_extracted_as_a_zip(tmp_path: Path) -> None:
    """Asking tar first must not cost the zip path."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.md", "# hello")
    extract(buffer.getvalue(), tmp_path)
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "# hello"


def test_an_empty_zip_is_still_recognised(tmp_path: Path) -> None:
    """An empty zip is EOCD-only and is not a tar, so it must fall through."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w"):
        pass
    extract(buffer.getvalue(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_zip_declaring_a_huge_expansion_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decompression bomb: small compressed, enormous expanded. The download cap
    says nothing about it, because what that bounds is the compressed form."""
    monkeypatch.setattr(limits, "MAX_EXTRACTED_BYTES", 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.txt", "0" * 100_000)
    assert len(buffer.getvalue()) < 1024, "the archive itself must be small"
    with pytest.raises(ResponseError, match="expands to"):
        extract(buffer.getvalue(), tmp_path)


def test_a_zip_declaring_too_many_members_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "MAX_ARCHIVE_MEMBERS", 3)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(5):
            archive.writestr(f"f{index}.txt", "x")
    with pytest.raises(ResponseError, match="members"):
        extract(buffer.getvalue(), tmp_path)


def test_the_same_check_applies_to_zip_members(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.txt:ads", "x")
    with pytest.raises(ResponseError, match="refusing zip member"):
        extract(buffer.getvalue(), tmp_path)


def test_a_zip_declaring_too_many_entries_is_refused_before_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ZipFile()` parses the whole central directory and materialises every
    ZipInfo in `filelist` before any member check runs, so millions of empty
    entries exhaust memory and surface as MemoryError rather than a refusal.

    Same shape as the tar `getmembers()` finding — and the same mistake of fixing
    one container and leaving the other.
    """
    monkeypatch.setattr(limits, "MAX_ARCHIVE_MEMBERS", 10)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(40):
            archive.writestr(f"f{index}.txt", "")
    with pytest.raises(ResponseError, match="members"):
        extract(buffer.getvalue(), tmp_path)


def test_an_ordinary_zip_still_extracts_after_the_preflight(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.md", "# hello")
    extract(buffer.getvalue(), tmp_path)
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "# hello"


@pytest.mark.filterwarnings("ignore:Duplicate name:UserWarning")
def test_the_same_name_twice_is_a_collision(tmp_path: Path) -> None:
    """A zip may carry one name twice, and extracting it still loses a payload.

    This reverses an earlier decision here, which was to allow it: a duplicate
    name is legal, `ZipFile` warns about it, and refusing it would reject archives
    that extract. All of that is true and the conclusion was wrong. Extraction
    writes one file, so the first member's contents are gone with nothing said --
    and "does not lose data quietly" is the property every other check in this
    module exists to hold. Legal and lossless are different questions, and the
    exemption answered the first one.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.md", "first")
        archive.writestr("doc.md", "second")
    with pytest.raises(ResponseError, match="twice"):
        extract(buffer.getvalue(), tmp_path)


def test_two_members_with_one_name_are_named_in_the_refusal(tmp_path: Path) -> None:
    """The message distinguishes the two cases, because they read differently to
    whoever has to act on it: one name twice, versus two spellings of one name."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Report.txt", "first")
        archive.writestr("report.txt", "second")
    with pytest.raises(ResponseError, match="members .* and .*same file"):
        extract(buffer.getvalue(), tmp_path)


def test_a_lying_entry_count_does_not_bypass_the_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ZipFile` does not act on the EOCD count — it walks the directory for the
    declared byte size — so an archive can say one entry and carry thirty, and a
    preflight that trusted the number was no protection against the case it
    existed for."""
    monkeypatch.setattr(limits, "MAX_ARCHIVE_MEMBERS", 5)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(30):
            archive.writestr(f"f{index}.txt", "")
    lying = bytearray(buffer.getvalue())
    at = lying.rfind(b"PK\x05\x06")
    lying[at + 8 : at + 10] = (1).to_bytes(2, "little")
    lying[at + 10 : at + 12] = (1).to_bytes(2, "little")

    with pytest.raises(ResponseError, match="over"):
        extract(bytes(lying), tmp_path)


def test_dot_components_collide_with_their_canonical_form(tmp_path: Path) -> None:
    """`a/./b.txt` and `a/b.txt` fold differently as strings and extract to the
    same place, because `zipfile` drops `.` components. Folding answers "same
    name"; the check has to answer "same file"."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a/./b.txt", "first")
        archive.writestr("a/b.txt", "second")
    with pytest.raises(ResponseError, match="same file"):
        extract(buffer.getvalue(), tmp_path)


def test_a_self_extracting_archive_cannot_bypass_the_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "MAX_ARCHIVE_MEMBERS", 5)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(30):
            archive.writestr(f"f{index}.txt", "")
    with_stub = b"MZ" + b"stub" * 500 + buffer.getvalue()
    with pytest.raises(ResponseError, match="over"):
        extract(with_stub, tmp_path)


def test_parent_components_collide_with_their_canonical_form(tmp_path: Path) -> None:
    """`zipfile` *removes* `..` components rather than resolving them, so
    `a/../b.txt` and `a/b.txt` are one file. `within` cannot catch this: the
    resolved path stays inside the destination, so it is a collision rather than an
    escape.

    Second round on this check, and the same mistake both times — comparing a
    representation instead of what the filesystem will see.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a/../b.txt", "first")
        archive.writestr("a/b.txt", "second")
    with pytest.raises(ResponseError, match="same file"):
        extract(buffer.getvalue(), tmp_path)


def test_a_zip_parent_component_is_removed_rather_than_resolved(
    tmp_path: Path
) -> None:
    """And the zip half, which is the case the shared rule was written for."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a/../b.txt", "first")
        archive.writestr("a/b.txt", "second")
    with pytest.raises(ResponseError, match="same file"):
        extract(buffer.getvalue(), tmp_path)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("r\u00e9sum\u00e9.txt", "re\u0301sume\u0301.txt"),
        ("\u00c5ngstr\u00f6m.md", "A\u030angstro\u0308m.md"),
    ],
)
def test_canonically_equivalent_names_are_one_file(
    tmp_path: Path, first: str, second: str
) -> None:
    """macOS is normalisation-insensitive as well as case-insensitive, so NFC and
    NFD spellings of the same name are one file there -- and `casefold` leaves the
    two strings distinct, so the archive passed the check and the second member
    silently overwrote the first.

    The same shape as the case and the parent-component findings: three ways for
    two names to be one file, and this check has now been wrong about each of
    them once.
    """
    assert first != second, "the fixture must be two distinct strings"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(first, "first")
        archive.writestr(second, "second")
    with pytest.raises(ResponseError, match="same file"):
        extract(buffer.getvalue(), tmp_path)


def test_normalisation_does_not_merge_genuinely_different_names(
    tmp_path: Path,
) -> None:
    """The guard: normalisation folds spellings of one character, not different
    characters. A check that refused these would reject ordinary archives."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("r\u00e9sum\u00e9.txt", "accented")
        archive.writestr("resume.txt", "plain")
    extract(buffer.getvalue(), tmp_path)
    assert (tmp_path / "resume.txt").read_text(encoding="utf-8") == "plain"
