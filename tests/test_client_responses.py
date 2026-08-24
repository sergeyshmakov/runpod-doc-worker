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
import http.server
import io
import socketserver
import tarfile
import threading
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

def test_importing_the_client_half_loads_nothing_heavy() -> None:
    """Importing this must not load the worker transport stack.

    Scoped to imports deliberately. `pip install` of this distribution *does* bring
    httpx and httpcore, because the worker side declares them — an earlier version
    of this test's docstring claimed otherwise, which was not true. What this pins
    is that a client reading a response pays no import cost for them, which is the
    part this module controls.
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


# -----------------------------------------------------------------------------
# Second review round: the boundary was leakier than the first pass thought
# -----------------------------------------------------------------------------
#
# Every case below escaped `ResponseError` when this module was first written. They
# share a shape worth naming: each is an *ordinary property of an untrusted
# response* — a malformed URL, an encrypted member, an unwritable destination, a
# NUL in a name — that the stdlib reports with an exception type the handler did
# not list. "Treat the response as untrusted" has to include the exceptions it
# provokes, not only the values it carries.

@pytest.mark.parametrize(
    "url",
    [
        "https://[bad",                  # ValueError: Invalid IPv6 URL, while splitting
        "https://example.com:bad/x",     # http.client.InvalidURL, at connect time
        "https://",                      # no host at all
    ],
)
def test_a_malformed_url_that_starts_with_a_good_scheme_is_refused(url: str) -> None:
    """The prefix check accepted these, and the failure surfaced from inside the
    stdlib instead of from here."""
    with pytest.raises(ResponseError, match="refusing to fetch"):
        download(url)


def test_a_nul_in_an_output_name_is_refused() -> None:
    """It has no directory component and `Path(name).name` keeps it, so every check
    passed — and then the write raised a raw `ValueError: embedded null byte`."""
    with pytest.raises(ResponseError, match="control character"):
        safe_output_name("fig\x00.jpg", what="image key")


@pytest.mark.parametrize("name", ["fig\n.jpg", "fig\t.jpg", "fig\x7f.jpg"])
def test_other_control_characters_are_refused_too(name: str) -> None:
    with pytest.raises(ResponseError, match="control character"):
        safe_output_name(name, what="image key")


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


# -----------------------------------------------------------------------------
# Third review round: the boundary again, plus one wrong answer
# -----------------------------------------------------------------------------

def test_within_works_with_a_relative_destination() -> None:
    """The defect here was not a leak but a *wrong answer*: only the target was
    resolved, so a relative destination compared an absolute path against a
    relative one and returned False for every safe member. `extract` passes an
    already-resolved path and so never saw it — a public helper has to be correct
    on its own terms rather than on its caller's."""
    assert within(Path("out"), "doc.md") is True
    assert within(Path("out"), "../escaped") is False


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


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/has space",
        "https://example.com\n.evil/x",
        "https://example.com/\ttab",
    ],
)
def test_a_url_with_a_forbidden_character_is_refused(url: str) -> None:
    """`urlopen` raises `InvalidURL` from inside http.client for these. The newline
    is the one that matters most: it is how a response would try to smuggle a
    second request line into the connection."""
    with pytest.raises(ResponseError, match="cannot appear in a request target"):
        download(url)


def test_a_destination_that_cannot_be_created_is_refused(tmp_path: Path) -> None:
    """`dest_dir` naming an existing regular file raises from `mkdir`, before
    either archive helper runs — outside the contract despite being inside the
    public call."""
    blocker = tmp_path / "afile"
    blocker.write_text("i am a file", encoding="utf-8")

    with pytest.raises(ResponseError, match="destination could not be created"):
        extract(b"junk", blocker)


# --- Round four: the same class again, found four more times ----------------
#
# Every one of these is an ordinary property of an untrusted response that the
# standard library reports with an exception type the handler had not listed.
# After four rounds the rule is stated in the module docstring: each stdlib call
# here is a place a malformed response can speak, not only the ones that read
# bytes.


class _TruncatingHandler(http.server.BaseHTTPRequestHandler):
    """Declares 4096 bytes, sends 512, hangs up."""

    def do_GET(self) -> None:  # noqa: N802 — stdlib's spelling
        self.send_response(200)
        self.send_header("Content-Length", "4096")
        self.end_headers()
        self.wfile.write(b"x" * 512)
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


def test_a_truncated_download_is_refused() -> None:
    """A server closing before its declared Content-Length raises
    `http.client.IncompleteRead` from `read()`. It is an HTTPException rather
    than an OSError, so the interrupted-download case — the most ordinary
    network failure there is — escaped every handler in `download`."""
    server = socketserver.TCPServer(("127.0.0.1", 0), _TruncatingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(ResponseError, match="IncompleteRead"):
            download(f"http://127.0.0.1:{server.server_address[1]}/a.tar")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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


def test_a_non_ascii_url_is_refused() -> None:
    """A request target is ASCII. `https://example.com/é` passes a scheme check
    and then raises `UnicodeEncodeError` while the request line is encoded — a
    caller meaning to fetch that path percent-encodes it, which is ASCII."""
    with pytest.raises(ResponseError, match="cannot appear in a request target"):
        download("https://example.com/é")


@pytest.mark.parametrize("url", [None, 1234, b"https://example.com/a.tar", ["x"]])
def test_a_url_that_is_not_a_string_is_refused(url: object) -> None:
    """`for character in None` raises a bare TypeError from inside the function
    whose whole job is to report bad input as ResponseError. A parsed response
    honours its annotation only if the worker sent what it promised."""
    with pytest.raises(ResponseError, match="should be a string"):
        require_http_url(url)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", [123, ["a"], {}, 0.5, True])
def test_an_output_name_that_is_not_a_string_is_refused(name: object) -> None:
    """A truthy non-string reached `Path(name)` and raised TypeError. A falsy one
    such as `{}` was caught, but reported as "not a usable filename" — the wrong
    problem, which is its own small defect."""
    with pytest.raises(ResponseError, match="should be a string"):
        safe_output_name(name, what="a basename")  # type: ignore[arg-type]
