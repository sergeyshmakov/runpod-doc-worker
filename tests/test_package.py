"""Response packaging: the three transports and the entry they build.

`package_results_entry` is the function a worker rewires when it adopts this
package, and the one a byte-identical response diff is measured against. The
cases here pin the entry's shape — which keys appear, where they come from, and
who wins when two layers name the same one.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
import zipfile

import pytest

from runpod_doc_worker.contract.artifacts import Artifact
from runpod_doc_worker.transport import package


MANIFEST = (
    Artifact("markdown", ("{basename}.md",), kind="text"),
    Artifact(
        "content_list",
        ("{basename}_content_list.json", "{basename}_content_list_v2.json"),
        kind="json",
        default=[],
    ),
    Artifact("middle", ("{basename}_middle.json",), kind="json"),
    Artifact("images", ("images/*",), kind="b64map"),
)


@pytest.fixture
def output_dir(tmp_path):
    # Written as bytes: the archive cases compare what came out of the container
    # with what went in, and text mode would translate newlines on Windows.
    (tmp_path / "doc.md").write_bytes(b"# hello\n")
    (tmp_path / "doc_content_list.json").write_bytes(b'[{"type": "text"}]')
    (tmp_path / "doc_middle.json").write_bytes(b'{"pdf_info": []}')
    images = tmp_path / "images"
    images.mkdir()
    (images / "fig1.png").write_bytes(b"\x89PNG\r\n\x1a\nfig1")
    return tmp_path


# -----------------------------------------------------------------------------
# Archives
# -----------------------------------------------------------------------------

def test_tarball_carries_every_file(output_dir):
    raw = base64.b64decode(package.package_tarball(output_dir))
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        names = {m.name for m in tar.getmembers() if m.isfile()}
    assert names == {
        "doc.md", "doc_content_list.json", "doc_middle.json", "images/fig1.png",
    }


def test_zip_carries_the_same_files(output_dir):
    raw = base64.b64decode(package.package_tarball(output_dir, "zip"))
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        assert set(zf.namelist()) == {
            "doc.md", "doc_content_list.json", "doc_middle.json", "images/fig1.png",
        }


def test_zip_content_round_trips(output_dir):
    raw = base64.b64decode(package.package_tarball(output_dir, "zip"))
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        assert zf.read("doc.md").decode("utf-8") == "# hello\n"


def test_unknown_archive_format_falls_back_to_tar(output_dir):
    """archive_format is validated upstream; the fallback keeps a typo from
    producing an empty response rather than an archive."""
    raw = base64.b64decode(package.package_tarball(output_dir, "7z"))
    assert raw[:2] == b"\x1f\x8b"  # gzip magic


# -----------------------------------------------------------------------------
# Inline
# -----------------------------------------------------------------------------

def test_inline_returns_every_manifest_key(output_dir):
    out = package.package_inline(output_dir, "doc", MANIFEST)
    assert set(out) == {"markdown", "content_list", "middle", "images"}
    assert out["markdown"] == "# hello\n"
    assert out["content_list"] == [{"type": "text"}]
    assert out["middle"] == {"pdf_info": []}
    assert list(out["images"]) == ["fig1.png"]


def test_inline_formats_filter_omits_keys(output_dir):
    out = package.package_inline(output_dir, "doc", MANIFEST, formats=["markdown"])
    assert out == {"markdown": "# hello\n"}


# -----------------------------------------------------------------------------
# The results entry
# -----------------------------------------------------------------------------

def _entry(output_dir, **overrides):
    kwargs = dict(
        transport="inline",
        formats=["markdown"],
        output_dir=output_dir,
        basename="doc",
        source="url:https://example.com/doc.pdf",
        manifest=MANIFEST,
    )
    kwargs.update(overrides)
    return package.package_results_entry(**kwargs)


def test_entry_carries_the_envelope_fields(output_dir):
    entry = _entry(output_dir)
    assert entry["basename"] == "doc"
    assert entry["source"] == "url:https://example.com/doc.pdf"


def test_entry_merges_engine_metadata(output_dir):
    entry = _entry(output_dir, metadata={"pages_requested": 12})
    assert entry["pages_requested"] == 12


def test_entry_without_metadata_has_only_envelope_and_payload(output_dir):
    assert set(_entry(output_dir)) == {"basename", "source", "markdown"}


@pytest.mark.parametrize("reserved", ["basename", "source"])
def test_metadata_may_not_claim_a_reserved_key(output_dir, reserved):
    """The harness owns these. Silently losing the field that says where a
    document came from is worse than a loud rejection at the call site."""
    with pytest.raises(ValueError, match=reserved):
        _entry(output_dir, metadata={reserved: "OVERWRITTEN"})


def test_tarball_transport_puts_the_archive_in_the_entry(output_dir):
    entry = _entry(output_dir, transport="tarball_b64")
    assert isinstance(entry["tarball_b64"], str)
    assert "markdown" not in entry


def test_inline_transport_puts_the_artifacts_in_the_entry(output_dir):
    entry = _entry(output_dir, transport="inline", formats=None)
    assert entry["markdown"] == "# hello\n"
    assert "tarball_b64" not in entry


def test_unknown_transport_raises(output_dir):
    """Returning a successful entry with a different payload than the caller
    asked for surfaces days later, in someone else's code."""
    with pytest.raises(ValueError, match="transport must be one of"):
        _entry(output_dir, transport="tarball")


def test_transport_matching_is_case_sensitive(output_dir):
    with pytest.raises(ValueError):
        _entry(output_dir, transport="S3")


# -----------------------------------------------------------------------------
# Presigned URL lifetime
# -----------------------------------------------------------------------------

def test_presign_ttl_defaults_to_an_hour(monkeypatch):
    monkeypatch.delenv("BUCKET_PRESIGN_TTL_SECONDS", raising=False)
    assert package.presign_ttl_seconds() == package.S3_PRESIGN_TTL_SECONDS


@pytest.mark.parametrize("raw,expected", [
    ("300", 300),
    ("1", package.MIN_PRESIGN_TTL_SECONDS),
    ("999999999", package.MAX_PRESIGN_TTL_SECONDS),
    ("not-a-number", package.S3_PRESIGN_TTL_SECONDS),
    ("", package.S3_PRESIGN_TTL_SECONDS),
])
def test_presign_ttl_clamps_rather_than_failing(monkeypatch, raw, expected):
    """A bad value here would otherwise turn every successful parse into a
    failed job."""
    monkeypatch.setenv("BUCKET_PRESIGN_TTL_SECONDS", raw)
    assert package.presign_ttl_seconds() == expected


def test_s3_without_credentials_names_every_missing_var(monkeypatch, output_dir):
    for var in (
        "BUCKET_ENDPOINT_URL", "BUCKET_NAME",
        "BUCKET_ACCESS_KEY_ID", "BUCKET_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError) as exc:
        package.package_s3(output_dir, "doc")
    for var in (
        "BUCKET_ENDPOINT_URL", "BUCKET_NAME",
        "BUCKET_ACCESS_KEY_ID", "BUCKET_SECRET_ACCESS_KEY",
    ):
        assert var in str(exc.value)


def test_s3_reports_only_the_vars_that_are_missing(monkeypatch, output_dir):
    monkeypatch.setenv("BUCKET_ENDPOINT_URL", "https://s3.example.com")
    monkeypatch.setenv("BUCKET_NAME", "bucket")
    monkeypatch.setenv("BUCKET_ACCESS_KEY_ID", "key")
    monkeypatch.delenv("BUCKET_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(ValueError) as exc:
        package.package_s3(output_dir, "doc")
    assert "BUCKET_SECRET_ACCESS_KEY" in str(exc.value)
    assert "BUCKET_NAME" not in str(exc.value)
