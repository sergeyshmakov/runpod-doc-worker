"""The zip preflight: walking the central directory instead of trusting it."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from runpod_doc_worker.client import (
    ResponseError,
    extract,
    zips,
)
from tests.client_fixtures import (
    _zip64_archive,
)


def test_the_zip_entry_count_is_counted_not_trusted(tmp_path: Path) -> None:
    """The preflight walks the central directory rather than reading the count
    field, so a lying archive gains nothing.

    The previous version trusted the end-of-central-directory number, which
    `ZipFile` itself does not: it walks the directory for the declared byte size.
    So an archive could say one entry, carry forty, and have all forty
    materialised -- the preflight was no protection against the case it existed
    for.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(40):
            archive.writestr(f"f{index}.txt", "")
    honest = buffer.getvalue()
    assert zips._counted_zip_entries(honest, 100) == 40

    # Same archive, both EOCD count fields rewritten to claim one entry.
    lying = bytearray(honest)
    at = lying.rfind(b"PK\x05\x06")
    lying[at + 8 : at + 10] = (1).to_bytes(2, "little")
    lying[at + 10 : at + 12] = (1).to_bytes(2, "little")
    assert zips._counted_zip_entries(bytes(lying), 100) == 40, (
        "the count must come from the records, not the claim"
    )


def test_the_preflight_declines_to_guess(tmp_path: Path) -> None:
    """Returning None on anything unexpected is deliberate: this is a cheap
    pre-filter and `ZipFile` stays the authority on readability. A body with no
    EOCD must not be refused *by the preflight* — it is refused, but as an
    unreadable archive."""
    assert zips._counted_zip_entries(b"not an archive at all", 100) is None
    with pytest.raises(ResponseError, match="could not be read"):
        extract(b"PK\x03\x04 truncated", tmp_path)


def test_the_count_walk_is_bounded(tmp_path: Path) -> None:
    """It stops as soon as the limit is passed, so a hostile archive costs a fixed
    amount of work rather than one proportional to its member count."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(50):
            archive.writestr(f"f{index}.txt", "")
    assert zips._counted_zip_entries(buffer.getvalue(), 5) == 6


def test_the_preflight_accounts_for_prepended_data() -> None:
    """A self-extracting zip carries a stub, and the EOCD offsets are relative to
    the embedded archive. `ZipFile` corrects for the discrepancy; the scan used
    the raw offset, landed in the stub, gave up, and skipped the preflight
    entirely — so an over-limit archive got through by being self-extracting."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(30):
            archive.writestr(f"f{index}.txt", "")
    plain = buffer.getvalue()
    with_stub = b"MZ" + b"stub" * 500 + plain

    assert zips._counted_zip_entries(plain, 100) == 30
    assert zips._counted_zip_entries(with_stub, 100) == 30


def test_a_zip64_archive_is_still_counted() -> None:
    """A member carrying a ZIP64 extra field in its directory record is counted.

    The docstring here used to claim this covered the ZIP64 *end record* layout.
    It did not: `force_zip64=True` affects one member's directory record, while the
    end record and locator are written only when the archive itself exceeds a
    limit. So this asserts that the wider directory record does not throw the walk
    off, which is worth having, and
    `test_the_zip64_end_record_is_not_mistaken_for_a_prepended_stub` covers the
    layout this one was named for.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", allowZip64=True) as archive:
        for index in range(40):
            archive.writestr(f"f{index}.txt", "")
        info = zipfile.ZipInfo("big.bin")
        with archive.open(info, "w", force_zip64=True) as handle:
            handle.write(b"x")
    assert zips._counted_zip_entries(buffer.getvalue(), 100) == 41


def test_the_zip64_end_record_is_not_mistaken_for_a_prepended_stub() -> None:
    """ZIP64 *and* a self-extracting stub together defeated both candidates.

    The stub correction is the distance between the directory's end and the EOCD.
    A ZIP64 archive puts its own end record and locator in that gap, so measuring
    to the EOCD counted 76 extra bytes as stub. With either feature alone one of
    the two candidates was right; with both, neither was, and the preflight was
    skipped on exactly the large self-extracting archives it exists for.

    Verified against the previous implementation, which returns None for the last
    case here and the correct count for the other three.
    """
    stub = b"MZ" + b"stub" * 500
    plain = _zip64_archive(end_record=False)
    zip64 = _zip64_archive(end_record=True)
    assert b"PK\x06\x06" in zip64, "the fixture is not a ZIP64 archive"
    assert b"PK\x06\x06" not in plain

    for label, data in (
        ("plain", plain),
        ("plain + stub", stub + plain),
        ("zip64", zip64),
        ("zip64 + stub", stub + zip64),
    ):
        assert zips._counted_zip_entries(data, 100) == 41, label
