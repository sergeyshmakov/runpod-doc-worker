"""The client half: every way a response can be untrustworthy.

These are not hypothetical cases. Each one was a live defect in a consumer repo,
and the reason they are here rather than there is that both consumers carried
their own copy of this code: three fixes made in one client never reached the
four identical sites in the other, and neither validated base64 at all.

The invariant the whole module exists to hold: **nothing escapes as a raw stdlib
exception.** A client wraps these calls in one ``except ResponseError``, so a leak
means user code receives a ``tarfile.ReadError`` or a ``binascii.Error`` from a
library documenting a single error class.
"""

from __future__ import annotations

import base64
import io
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from runpod_doc_worker.client import (
    ResponseError,
    decode_b64,
    download,
    extract,
    require_http_url,
    safe_output_name,
    within,
)


# -----------------------------------------------------------------------------
# Strict base64
# -----------------------------------------------------------------------------

def test_the_default_decoder_would_have_silently_returned_nothing() -> None:
    """The reason this function exists, pinned as a fact about the stdlib rather
    than left as a claim in a comment."""
    assert base64.b64decode("!!!!") == b""


@pytest.mark.parametrize("payload", ["!!!!", "abc$def==", "not base64 at all!"])
def test_a_corrupt_payload_is_refused(payload: str) -> None:
    with pytest.raises(ResponseError, match="base64"):
        decode_b64(payload, what="images/fig.jpg")


def test_the_refusal_names_the_artifact() -> None:
    """A client writes many artifacts from one response; "invalid base64" alone
    does not say which one."""
    with pytest.raises(ResponseError, match="images/fig.jpg"):
        decode_b64("!!!!", what="images/fig.jpg")


def test_a_truncated_payload_is_refused() -> None:
    encoded = base64.b64encode(b"x" * 100).decode()
    with pytest.raises(ResponseError):
        decode_b64(encoded[:-3], what="tarball_b64")


def test_a_non_string_payload_is_refused() -> None:
    """JSON gives no guarantee the field is a string."""
    with pytest.raises(ResponseError, match="should be a base64 string"):
        decode_b64(None, what="tarball_b64")


def test_a_valid_payload_round_trips() -> None:
    assert decode_b64(base64.b64encode(b"hello").decode(), what="x") == b"hello"


def test_line_wrapped_base64_is_accepted() -> None:
    """`validate=True` rejects newlines, and wrapping is what several encoders
    emit — so validating the raw string would trade a silent-corruption bug for a
    false negative on well-formed input."""
    wrapped = "\n".join(
        base64.b64encode(b"y" * 300).decode()[i : i + 76] for i in range(0, 400, 76)
    )
    assert decode_b64(wrapped, what="x") == b"y" * 300


# -----------------------------------------------------------------------------
# Archives: nothing raw escapes
# -----------------------------------------------------------------------------

def test_a_body_that_is_not_an_archive_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ResponseError, match="could not be read"):
        extract(b"this was never a tar", tmp_path)


def test_a_corrupt_zip_is_refused(tmp_path: Path) -> None:
    """Reachable because `extract` picks the container from the leading bytes of
    whatever actually arrived, not from the requested archive format."""
    with pytest.raises(ResponseError, match="could not be read"):
        extract(b"PK\x03\x04 truncated right after the signature", tmp_path)


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


def test_a_traversing_tar_member_is_refused(tmp_path: Path) -> None:
    """CVE-2007-4559. Checked before extraction rather than relying on the stdlib
    filter, so the guarantee does not depend on the Python patch release."""
    with pytest.raises(ResponseError, match="escapes the destination"):
        extract(_tar_with("../escaped.txt"), tmp_path)


def test_a_non_regular_tar_member_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ResponseError, match="not a regular file or dir"):
        extract(_tar_with("link", kind="symlink"), tmp_path)


def test_a_traversing_zip_member_is_refused(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escaped.txt", "x")
    with pytest.raises(ResponseError, match="escapes the destination"):
        extract(buffer.getvalue(), tmp_path)


def test_a_well_formed_tar_extracts(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("doc.md")
        body = b"# hello"
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    where = extract(buffer.getvalue(), tmp_path)
    assert (where / "doc.md").read_text(encoding="utf-8") == "# hello"


def test_a_well_formed_zip_extracts(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.md", "# hello")
    where = extract(buffer.getvalue(), tmp_path)
    assert (where / "doc.md").read_text(encoding="utf-8") == "# hello"


# -----------------------------------------------------------------------------
# Fetching
# -----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raised",
    [
        TimeoutError("stalled mid-body"),
        urllib.error.URLError("dns failure"),
        urllib.error.HTTPError("https://x/a.tar", 403, "Forbidden", {}, None),
    ],
)
def test_a_fetch_failure_is_refused(monkeypatch: pytest.MonkeyPatch, raised) -> None:
    """An expired presigned URL is the ordinary case here, not an exotic one, and
    a bare TimeoutError arrives unwrapped when the stall is in the body."""
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(raised)
    )
    with pytest.raises(ResponseError, match="fetching the archive failed"):
        download("https://example.com/out.tar.gz")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "/local/path"])
def test_a_non_http_url_is_refused_before_fetching(url: str) -> None:
    """`urlopen` would happily read `file://`."""
    with pytest.raises(ResponseError, match="expected an http"):
        require_http_url(url)


# -----------------------------------------------------------------------------
# Output names
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["", ".", "..", "a/b.jpg", "a\\b.jpg", "/abs.jpg"])
def test_an_unusable_output_name_is_refused(name: str) -> None:
    with pytest.raises(ResponseError):
        safe_output_name(name, what="image key")


def test_a_plain_filename_passes() -> None:
    assert safe_output_name("fig_0.jpg", what="image key") == "fig_0.jpg"


def test_within_accepts_the_destination_itself(tmp_path: Path) -> None:
    assert within(tmp_path.resolve(), ".") is True
    assert within(tmp_path.resolve(), "../elsewhere") is False


# -----------------------------------------------------------------------------
# The import contract
# -----------------------------------------------------------------------------

def test_the_client_half_pulls_in_nothing_heavy() -> None:
    """A client package depends on this to read a response. It must not drag a
    worker's transport stack into an end user's environment — which is also what
    lets the distribution keep declaring httpx without that mattering here.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import runpod_doc_worker.client as c; "
        "assert c.decode_b64; "
        "heavy = [m for m in ('httpx', 'httpcore', 'boto3', 'anyio') "
        "         if m in sys.modules]; "
        "print(','.join(heavy))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", f"client import pulled in {out.stdout.strip()}"
