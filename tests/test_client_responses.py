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
import os
import socketserver
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from runpod_doc_worker.client.responses import MAX_METADATA_BYTES

from runpod_doc_worker.client import responses
from runpod_doc_worker.client import (
    ResponseError,
    decode_b64,
    download,
    extract,
    require_fetchable_url,
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
    # `_opener`, not `urllib.request.urlopen`: download() stopped using the
    # module-level function when it gained a redirect-checking opener, and this
    # patch silently stopped applying. The test then passed on whatever a real
    # request to example.com did — a network error satisfied the assertion while
    # exercising none of these exceptions, and a success or a slow response would
    # have failed or hung the suite instead.
    class _Exploding:
        def open(self, *args: object, **kwargs: object):
            raise raised

    monkeypatch.setattr(responses, "_opener", lambda *_: _Exploding())
    with pytest.raises(ResponseError, match="fetching the archive failed"):
        download("https://example.com/out.tar.gz")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "/local/path"])
def test_a_non_http_url_is_refused_before_fetching(url: str) -> None:
    """`urlopen` would happily read `file://`."""
    with pytest.raises(ResponseError, match="expected an http"):
        require_fetchable_url(url)


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
        require_fetchable_url(url)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", [123, ["a"], {}, 0.5, True])
def test_an_output_name_that_is_not_a_string_is_refused(name: object) -> None:
    """A truthy non-string reached `Path(name)` and raised TypeError. A falsy one
    such as `{}` was caught, but reported as "not a usable filename" — the wrong
    problem, which is its own small defect."""
    with pytest.raises(ResponseError, match="should be a string"):
        safe_output_name(name, what="a basename")  # type: ignore[arg-type]


# --- Round six: the isolation promise, and three more type/platform gaps ------


def test_importing_the_client_does_not_load_worker_modules() -> None:
    """The promise the client subpackage makes, checked as the rule rather than
    as a symptom.

    The previous test looked for *heavy* modules (httpx and friends) and passed,
    while `import runpod_doc_worker.client` was in fact loading
    `runpod_doc_worker.config` every time: Python runs the root package
    initializer first, and that did `from runpod_doc_worker.config import ...`
    eagerly. A test that asserts the consequence instead of the invariant goes
    green the moment the invariant breaks in a way that is merely cheap.

    A subprocess, because `sys.modules` in this one is already polluted by the
    rest of the suite.
    """
    probe = (
        "import sys, runpod_doc_worker.client; "
        "print('|'.join(sorted(m for m in sys.modules "
        "if m.startswith('runpod_doc_worker'))))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
        check=True,
    )
    loaded = completed.stdout.strip().split("|")
    strays = [
        module
        for module in loaded
        if module != "runpod_doc_worker"
        and not module.startswith("runpod_doc_worker.client")
    ]
    assert not strays, f"importing the client pulled in worker modules: {strays}"


def test_the_lazy_root_exports_still_work() -> None:
    """Making the re-exports lazy must not change the worker-side API."""
    import runpod_doc_worker

    assert runpod_doc_worker.WorkerConfig is not None
    assert callable(runpod_doc_worker.configure)
    assert callable(runpod_doc_worker.active)
    assert "WorkerConfig" in dir(runpod_doc_worker)
    with pytest.raises(AttributeError):
        runpod_doc_worker.no_such_name


def test_a_percent_encoded_non_ascii_host_is_refused() -> None:
    """`http://%FF/` passes a printable-ASCII check because the check sees the
    encoded form. urllib then percent-decodes the authority, gets U+FFFD, and
    raises UnicodeEncodeError building the latin-1 Host header. A real IDN host
    arrives punycoded, which is ASCII, so nothing legitimate is refused."""
    with pytest.raises(ResponseError, match="not ASCII once percent-decoded"):
        download("http://%FF/")


@pytest.mark.parametrize("payload", ["not bytes", 12345, ["a"], {"a": 1}])
def test_an_archive_that_is_not_bytes_is_refused(payload: object, tmp_path: Path) -> None:
    """`io.BytesIO(data)` raises a bare TypeError, and `extract` is exported
    directly."""
    with pytest.raises(ResponseError, match="should be bytes"):
        extract(payload, tmp_path)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    ["NUL", "CON", "AUX", "PRN", "aux.txt", "COM1", "LPT9", "nul.json", "Con.md"],
)
def test_a_dos_device_name_is_refused(name: str) -> None:
    """On Windows `open("NUL", "wb")` succeeds and discards everything written,
    so an artifact named `NUL` is reported as saved and is gone. Reserved with
    any extension, and refused regardless of the host platform because the client
    writing the file may be on Windows even when the worker was not."""
    with pytest.raises(ResponseError, match="reserved device name"):
        safe_output_name(name, what="a basename")


@pytest.mark.parametrize("name", ["report.", "report ", "a.b.", "x "])
def test_a_trailing_dot_or_space_is_refused(name: str) -> None:
    """Windows strips both silently, so two artifacts one dot apart would collide
    and the second would overwrite the first."""
    with pytest.raises(ResponseError, match="trailing dot or space"):
        safe_output_name(name, what="a basename")


@pytest.mark.parametrize("name", ["CONSOLE.md", "AUXILIARY.txt", "COM.md", "nullable.py"])
def test_names_merely_starting_like_a_device_are_allowed(name: str) -> None:
    """The boundary in the other direction: only the exact stem is reserved, so
    the check must not swallow ordinary filenames."""
    assert safe_output_name(name, what="a basename") == name


# --- Round eight: the name collision, and destination resolution -------------


def test_the_client_url_helper_does_not_share_a_name_with_the_worker_one() -> None:
    """`transport.net.require_http_url` returns the validated **host**; this
    module's helper returns nothing and raises on refusal.

    Two functions one import apart with the same name and different contracts is
    the exact trap AGENTS.md records about the worker-side helper — a consumer
    writing `url = require_http_url(url)` gets None. Naming the client's the same
    thing would have doubled it, so it is `require_fetchable_url` and the old name
    is not exported.
    """
    from runpod_doc_worker import client
    from runpod_doc_worker.transport import net

    assert not hasattr(client, "require_http_url"), "the colliding name is exported"
    assert "require_fetchable_url" in client.__all__
    # The contracts really do differ, which is why the names must.
    assert net.require_http_url("https://example.com/a.tar", field="u") == "example.com"
    assert require_fetchable_url("https://example.com/a.tar") is None


@pytest.mark.parametrize("dest", [12345, None, object()])
def test_a_destination_that_is_not_a_path_is_refused(dest: object) -> None:
    """`Path(dest_dir)` raises TypeError before the guarded `mkdir` is reached, so
    this sat outside the contract even though it happened inside the public
    call."""
    with pytest.raises(ResponseError, match="not a usable path"):
        extract(b"x", dest)  # type: ignore[arg-type]


def test_a_destination_with_a_nul_is_refused() -> None:
    """A NUL survives `Path()` and `resolve()` and is rejected by `mkdir` as
    `ValueError: embedded null character in path` — not an OSError, so guarding
    the resolve alone left it escaping. The reproduction caught that; the first
    version of this fix did not."""
    with pytest.raises(ResponseError):
        extract(b"x", "bad\0dir")


# --- Round nine: authority, redirects, usable modes, strided buffers ---------


def test_percent_encoded_userinfo_is_refused() -> None:
    """Only the hostname was decoded and checked, so `http://%FF@example.com/`
    passed and then raised UnicodeEncodeError building the Authorization header.
    Checking one component of a string that gets decoded in several places is a
    check in the wrong place."""
    with pytest.raises(ResponseError, match="not ASCII once percent-decoded"):
        download("http://%FF@example.com/")


def test_a_malformed_redirect_target_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validating the URL handed in says nothing about where the server sends us
    next. urllib follows redirects internally, so a `Location` of `http://[bad`
    raises ValueError from inside urlopen without passing through the
    validator."""
    class _Exploding:
        def open(self, *args: object, **kwargs: object):
            raise ValueError("Invalid IPv6 URL")

    monkeypatch.setattr(responses, "_opener", lambda *_: _Exploding())
    with pytest.raises(ResponseError, match="fetching the archive failed"):
        download("https://example.com/out.tar.gz")


@pytest.mark.parametrize("payload", [memoryview(b"abcdef")[::2], memoryview(b"abcdef")[::-1]])
def test_a_strided_memoryview_is_handled(payload: object, tmp_path: Path) -> None:
    """The type check advertised `memoryview`, but BytesIO needs a contiguous
    buffer, so a strided view reached archive detection and raised BufferError.
    It is normalised rather than rejected, since a contiguous view is genuinely
    fine and the copy only happens for the odd case — so this surfaces as an
    ordinary "not an archive" refusal."""
    with pytest.raises(ResponseError):
        extract(payload, tmp_path)  # type: ignore[arg-type]


# --- Round ten: redirect schemes, within(), umask, Windows characters --------


@pytest.mark.parametrize("name", ["report?.txt", "a*.png", "x|y.txt", 'q"t.md', "a<b.md", "a>b.md"])
def test_a_windows_forbidden_character_is_refused(name: str) -> None:
    """These cannot be created as files on Windows, and they fail only at write
    time — so a caller trusting the documented "usable filename" result gets an
    OSError instead of a refusal."""
    with pytest.raises(ResponseError, match="Windows filename"):
        safe_output_name(name, what="a basename")


@pytest.mark.parametrize("name", ["fine_name.md", "a-b.c.jpg", "p0001_fig_0.jpg"])
def test_ordinary_names_still_pass(name: str) -> None:
    assert safe_output_name(name, what="a basename") == name


def test_within_refuses_a_nul_in_the_member_name() -> None:
    """Whether `resolve()` raises on a NUL depends on the platform — POSIX
    rejects it, Windows computes a path and answered True — so the same archive
    got two different answers from an exported helper. Checked explicitly for a
    consistent one."""
    with pytest.raises(ResponseError, match="NUL"):
        within(Path("out"), "bad\0name")


def test_within_refuses_a_destination_that_is_not_a_path() -> None:
    """Exported, so a direct caller wraps it in the same `except ResponseError`
    as everything else here; `Path(None)` raised a bare TypeError."""
    with pytest.raises(ResponseError):
        within(None, "x")  # type: ignore[arg-type]


def test_a_redirect_to_another_scheme_is_refused() -> None:
    """urllib's default opener has an FTP handler installed and its redirect
    handler follows a `Location` wherever it points, so an accepted HTTPS
    endpoint redirecting to `ftp://` was fetched over FTP — past a validator
    whose documented job is to reject exactly that.

    Asserted against the redirect handler directly rather than by standing up a
    redirecting server, because the check belongs to the handler and this keeps
    the test off the network.
    """
    from runpod_doc_worker.client.responses import _CheckedRedirectHandler

    handler = _CheckedRedirectHandler()
    with pytest.raises(ResponseError, match="expected an http"):
        handler.redirect_request(None, None, 302, "Found", {}, "ftp://example.com/x")


def test_the_opener_offers_only_http_handlers() -> None:
    """The opener is built explicitly instead of using the module default, whose
    handler set includes FTP and local file access — capabilities this function
    has no use for and cannot safely offer an untrusted URL."""
    from runpod_doc_worker.client.responses import _opener

    names = {type(h).__name__ for h in _opener().handlers}
    assert "FTPHandler" not in names
    assert "FileHandler" not in names


# --- Round twelve: zip names, symlink loops, name length ---------------------


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
    ("name", "refused"),
    [
        ("a" * 256, True),
        ("a" * 251 + ".jpg", False),
        ("文" * 90, True),    # 270 bytes from 90 characters, refused
        ("文" * 80, False),   # 240 bytes, fits and must not be refused
    ],
)
def test_an_overlong_output_name_is_refused(name: str, refused: bool) -> None:
    """A caller writing this gets `OSError: File name too long`, which is exactly
    the failure this helper exists to turn into a refusal.

    Measured in bytes rather than characters because the permitted charset
    includes non-ASCII: 90 CJK characters is 270 bytes and passes any character
    count, while 80 is 240 and fits.
    """
    if refused:
        with pytest.raises(ResponseError, match="filename limit"):
            safe_output_name(name, what="a basename")
    else:
        assert safe_output_name(name, what="a basename") == name


def test_within_normalises_a_resolution_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On 3.10 and 3.11, a symlink loop encountered while resolving raises
    RuntimeError — not an OSError, so it escaped. A destination reused across
    extractions can contain one, and this function is the code that walks into
    it.

    Forced, because the interpreter running this suite returns the path instead
    of raising.
    """
    original = Path.resolve

    def exploding_resolve(self, *args, **kwargs):
        if "loop" in str(self):
            raise RuntimeError("Symlink loop from " + str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", exploding_resolve)
    with pytest.raises(ResponseError, match="cannot place"):
        within(Path("loop"), "member.md")


# --- Round thirteen: more zip constructor failures, surrogates ---------------


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


def test_an_unpaired_surrogate_in_an_output_name_is_refused() -> None:
    """An unpaired surrogate survives JSON decoding, so a response can carry one.

    The length check measured with errors="surrogatepass" so it could not crash,
    which silently admitted a name that raises UnicodeEncodeError the moment the
    caller writes it. That was a hole created by the previous round's fix.
    """
    name = "x" + chr(0xD800) + ".txt"
    with pytest.raises(ResponseError, match="not encodable as UTF-8"):
        safe_output_name(name, what="a basename")


@pytest.mark.parametrize("name", ["ordinary.md", "\u6587\u66f8.md", "a-b_c.jpg"])
def test_encodable_names_still_pass(name: str) -> None:
    """Encoding strictly must not reject legitimate non-ASCII names."""
    assert safe_output_name(name, what="a basename") == name


# --- Round fourteen: the data filter's real rules, and timestamps ------------


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


# --- Round fifteen: device spellings, the umask race, neutral ownership ------


@pytest.mark.parametrize(
    "name",
    [
        "NUL .txt",          # Windows ignores trailing spaces when matching
        "NUL...txt",
        "NUL.",
        "COM\u00b9.txt",      # superscript one is COM1
        "LPT\u00b2.log",
        "AUX .md",
        "con .json",
    ],
)
def test_every_device_name_spelling_is_refused(name: str) -> None:
    """An exact-stem lookup missed two shapes Windows still treats as devices:
    trailing spaces or dots before the extension, and superscript digits. Both
    were returned as usable and neither can be saved as an ordinary file."""
    with pytest.raises(ResponseError, match="reserved device name"):
        safe_output_name(name, what="a basename")


@pytest.mark.parametrize("name", ["CONSOLE.md", "NULL.md", "COMMS.txt", "AUXILIARY.py"])
def test_names_merely_containing_a_device_name_still_pass(name: str) -> None:
    """The boundary: normalising the stem must not widen the match."""
    assert safe_output_name(name, what="a basename") == name


def test_the_module_no_longer_touches_the_umask() -> None:
    """Structural, because the race is invisible in a single-threaded test: the
    only safe amount of `os.umask` in this module is none."""
    source = Path(responses.__file__).read_text(encoding="utf-8")
    assert "os.umask" not in source


# --- Round sixteen: container detection, bounds, and the mode probe ----------


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


def test_an_oversized_download_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The socket timeout bounds idle time, not total volume, so a peer that keeps
    sending holds the connection and grows the buffer without ever tripping it."""
    monkeypatch.setattr(responses, "MAX_ARCHIVE_BYTES", 4096)

    class _Endless:
        headers: dict[str, str] = {}

        def read(self, size: int = -1) -> bytes:
            return b"x" * (size if size and size > 0 else 1024)

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(responses, "_opener", lambda *_: type("O", (), {"open": lambda *a, **k: _Endless()})())
    with pytest.raises(ResponseError, match="limit"):
        download("https://example.com/endless.tar")


def test_a_declared_oversize_is_refused_before_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """An early exit on Content-Length, which is a courtesy rather than the
    protection - the running total is what actually stops an untruthful one."""
    monkeypatch.setattr(responses, "MAX_ARCHIVE_BYTES", 4096)

    class _Declared:
        headers = {"Content-Length": "999999999"}

        def read(self, size: int = -1) -> bytes:
            raise AssertionError("must not read a body it already refused")

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(responses, "_opener", lambda *_: type("O", (), {"open": lambda *a, **k: _Declared()})())
    with pytest.raises(ResponseError, match="over the"):
        download("https://example.com/huge.tar")


def test_a_zip_declaring_a_huge_expansion_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decompression bomb: small compressed, enormous expanded. The download cap
    says nothing about it, because what that bounds is the compressed form."""
    monkeypatch.setattr(responses, "MAX_EXTRACTED_BYTES", 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.txt", "0" * 100_000)
    assert len(buffer.getvalue()) < 1024, "the archive itself must be small"
    with pytest.raises(ResponseError, match="expands to"):
        extract(buffer.getvalue(), tmp_path)


def test_a_zip_declaring_too_many_members_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(responses, "MAX_ARCHIVE_MEMBERS", 3)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(5):
            archive.writestr(f"f{index}.txt", "x")
    with pytest.raises(ResponseError, match="members"):
        extract(buffer.getvalue(), tmp_path)


def test_a_destination_that_is_a_symlink_loop_is_refused(tmp_path: Path) -> None:
    """`extract`'s resolve guard omitted RuntimeError while `within` had it - the
    same call, two guards, one narrower."""
    loop = tmp_path / "loop"
    try:
        loop.symlink_to(loop)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    with pytest.raises(ResponseError):
        extract(b"junk", loop)


# --- Round seventeen: tar quotas, member paths, inherited setgid -------------


def _tar_of(names: list[str], *, size: int = 0, mode: str = "w") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as tar:
        for name in names:
            body = b"0" * size
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def test_a_tar_declaring_too_many_members_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quotas were enforced only by the zip path, so the protection depended
    on which container the worker happened to send."""
    monkeypatch.setattr(responses, "MAX_ARCHIVE_MEMBERS", 10)
    with pytest.raises(ResponseError, match="members"):
        extract(_tar_of([f"f{i}.txt" for i in range(50)]), tmp_path)


def test_a_compressed_tar_declaring_a_huge_expansion_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compressed tar hides the ratio exactly as a zip does: the reproduction
    was a 541-byte gzip expanding to 50 KB."""
    monkeypatch.setattr(responses, "MAX_EXTRACTED_BYTES", 1000)
    data = _tar_of([f"f{i}.txt" for i in range(50)], size=1000, mode="w:gz")
    assert len(data) < 5000, "the archive itself must be small"
    with pytest.raises(ResponseError, match="expands to"):
        extract(data, tmp_path)


@pytest.mark.parametrize(
    "name",
    ["document.txt:payload", "NUL", "sub/COM1.txt", "sub/aux.log", "LPT9"],
)
def test_a_windows_special_member_path_is_refused(name: str, tmp_path: Path) -> None:
    """Containment is not enough: `within` says these land under the destination,
    and they do — but tarfile then opens a DOS device or an NTFS alternate data
    stream instead of creating an artifact, so the member is silently discarded
    or hidden while extraction reports success.

    Checked per path component, and on every platform, for the same reason
    `safe_output_name` is: the worker writing the archive and the client
    unpacking it need not share an OS.
    """
    with pytest.raises(ResponseError, match="refusing tar member"):
        extract(_tar_of([name]), tmp_path)


def test_the_same_check_applies_to_zip_members(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.txt:ads", "x")
    with pytest.raises(ResponseError, match="refusing zip member"):
        extract(buffer.getvalue(), tmp_path)


def test_ordinary_member_paths_still_extract(tmp_path: Path) -> None:
    """The constraint: a nested path with an extension must stay ordinary."""
    extract(_tar_of(["doc.md", "images/fig.jpg"], size=4), tmp_path)
    assert (tmp_path / "doc.md").is_file()
    assert (tmp_path / "images" / "fig.jpg").is_file()


# --- Round eighteen: one filename rule, incremental quotas, a deadline -------


def test_members_windows_would_collapse_together_are_refused(tmp_path: Path) -> None:
    """The P1. Windows rewrites both `a?.txt` and `a*.txt` to `a_.txt`, so an
    archive carrying both has one silently overwrite the other while extraction
    reports success.

    The gap was not the missing characters, it was that `_check_member_name` was
    written as a fresh, narrower copy of a rule `safe_output_name` already had --
    so `a?.txt` was refused as an output name and accepted as a member.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a?.txt", "first")
        archive.writestr("a*.txt", "second")
    with pytest.raises(ResponseError, match="Windows filename"):
        extract(buffer.getvalue(), tmp_path)


@pytest.mark.parametrize(
    "name", ["a?.txt", "NUL", "doc:ads", "trailing.", "trailing ", "a|b.txt"]
)
def test_one_rule_answers_for_both_callers(name: str, tmp_path: Path) -> None:
    """The invariant the fix establishes: a name unusable as an output is also
    unusable as a member. Asserted as agreement rather than as two lists, since
    two lists is exactly what drifted."""
    assert responses._windows_component_problem(name) is not None
    with pytest.raises(ResponseError):
        safe_output_name(name, what="a basename")
    with pytest.raises(ResponseError):
        extract(_tar_of([name]), tmp_path)


def test_the_reason_given_is_the_most_specific_one() -> None:
    """`NUL.` is a device name that also ends in a dot; the device reason is the
    useful one, so the checks are ordered most specific first."""
    assert "device name" in (responses._windows_component_problem("NUL.") or "")
    assert "trailing dot" in (responses._windows_component_problem("report.") or "")


def test_tar_quotas_abort_during_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`getmembers()` decompresses the whole stream and materialises every
    TarInfo before either quota can be read, so a tiny archive declaring millions
    of headers exhausts memory before `len(members)` is reached. Walking
    incrementally bounds the cost by the quota instead of by the archive."""
    monkeypatch.setattr(responses, "MAX_ARCHIVE_MEMBERS", 5)
    with pytest.raises(ResponseError, match="members"):
        extract(_tar_of([f"f{i}.txt" for i in range(200)]), tmp_path)


def test_the_size_quota_also_aborts_during_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(responses, "MAX_EXTRACTED_BYTES", 500)
    with pytest.raises(ResponseError, match="expands to"):
        extract(_tar_of([f"f{i}.txt" for i in range(20)], size=100, mode="w:gz"), tmp_path)


def test_a_download_that_never_finishes_hits_the_deadline(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket timeout bounds idle time and is reset by every successful read,
    so a peer trickling bytes can hold the call open indefinitely without ever
    approaching the byte cap."""
    monkeypatch.setattr(responses, "DOWNLOAD_DEADLINE_SECONDS", 0.05)

    class _Trickle:
        headers: dict[str, str] = {}

        def read(self, size: int = -1) -> bytes:
            time.sleep(0.02)
            return b"x"

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(
        responses, "_opener", lambda *_: type("O", (), {"open": lambda *a, **k: _Trickle()})()
    )
    with pytest.raises(ResponseError, match="exceeded"):
        download("https://example.com/trickle.tar")


# --- Round nineteen: the deadline covers open, and zip is preflighted --------


def test_the_deadline_covers_connection_and_headers(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timer started after `open()` returned, so connection setup, every
    redirect hop and the response headers were all outside it — and a server can
    trickle header bytes often enough that the per-socket idle timeout never
    fires. A deadline that begins after the slow part is not a deadline."""
    monkeypatch.setattr(responses, "DOWNLOAD_DEADLINE_SECONDS", 0.01)

    class _SlowOpen:
        def open(self, *args: object, **kwargs: object):
            time.sleep(0.05)

            class _Empty:
                headers: dict[str, str] = {}

                def read(self, size: int = -1) -> bytes:
                    return b""

                def __enter__(self):
                    return self

                def __exit__(self, *exc: object) -> None:
                    return None

            return _Empty()

    monkeypatch.setattr(responses, "_opener", lambda *_: _SlowOpen())
    with pytest.raises(ResponseError, match="exceeded"):
        download("https://example.com/slow.tar")


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
    assert responses._counted_zip_entries(honest, 100) == 40

    # Same archive, both EOCD count fields rewritten to claim one entry.
    lying = bytearray(honest)
    at = lying.rfind(b"PK\x05\x06")
    lying[at + 8 : at + 10] = (1).to_bytes(2, "little")
    lying[at + 10 : at + 12] = (1).to_bytes(2, "little")
    assert responses._counted_zip_entries(bytes(lying), 100) == 40, (
        "the count must come from the records, not the claim"
    )


def test_a_zip_declaring_too_many_entries_is_refused_before_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ZipFile()` parses the whole central directory and materialises every
    ZipInfo in `filelist` before any member check runs, so millions of empty
    entries exhaust memory and surface as MemoryError rather than a refusal.

    Same shape as the tar `getmembers()` finding — and the same mistake of fixing
    one container and leaving the other, which is twice in this review.
    """
    monkeypatch.setattr(responses, "MAX_ARCHIVE_MEMBERS", 10)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(40):
            archive.writestr(f"f{index}.txt", "")
    with pytest.raises(ResponseError, match="members"):
        extract(buffer.getvalue(), tmp_path)


def test_the_preflight_declines_to_guess(tmp_path: Path) -> None:
    """Returning None on anything unexpected is deliberate: this is a cheap
    pre-filter and `ZipFile` stays the authority on readability. A body with no
    EOCD must not be refused *by the preflight* — it is refused, but as an
    unreadable archive."""
    assert responses._counted_zip_entries(b"not an archive at all", 100) is None
    with pytest.raises(ResponseError, match="could not be read"):
        extract(b"PK\x03\x04 truncated", tmp_path)


def test_an_ordinary_zip_still_extracts_after_the_preflight(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.md", "# hello")
    extract(buffer.getvalue(), tmp_path)
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "# hello"


# --- Round twenty: case collisions, a counted directory, zstd ----------------


@pytest.mark.parametrize(
    ("first", "second"),
    [("Report.txt", "report.txt"), ("A/B.txt", "a/b.txt"), ("Doc.MD", "doc.md")],
)
def test_members_colliding_under_case_folding_are_refused(
    first: str, second: str, tmp_path: Path
) -> None:
    """Windows and macOS default to case-insensitive, so these are one file there
    and the second extraction silently overwrites the first.

    Every per-name check passes — both names are perfectly legal — because the
    problem is a *relationship* between names. Per-name validation cannot see it
    at all, which is why this needs a pass over the set rather than another rule.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(first, "first")
        archive.writestr(second, "second")
    with pytest.raises(ResponseError, match="same file"):
        extract(buffer.getvalue(), tmp_path)


@pytest.mark.filterwarnings("ignore:Duplicate name:UserWarning")
def test_the_same_name_twice_is_not_a_collision(tmp_path: Path) -> None:
    """A zip may legitimately carry the same name twice; that is a duplicate, not
    a case collision, and `ZipFile` already has a warning for it. Refusing it here
    would reject archives that work."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("doc.md", "first")
        archive.writestr("doc.md", "second")
    extract(buffer.getvalue(), tmp_path)
    assert (tmp_path / "doc.md").is_file()


def test_a_lying_entry_count_does_not_bypass_the_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ZipFile` does not act on the EOCD count — it walks the directory for the
    declared byte size — so an archive can say one entry and carry thirty, and a
    preflight that trusted the number was no protection against the case it
    existed for."""
    monkeypatch.setattr(responses, "MAX_ARCHIVE_MEMBERS", 5)
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


def test_the_count_walk_is_bounded(tmp_path: Path) -> None:
    """It stops as soon as the limit is passed, so a hostile archive costs a fixed
    amount of work rather than one proportional to its member count."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(50):
            archive.writestr(f"f{index}.txt", "")
    assert responses._counted_zip_entries(buffer.getvalue(), 5) == 6


def test_the_decompression_tuple_tracks_the_interpreter() -> None:
    """Zip method 93 is Zstandard, supported from 3.14, and a malformed payload
    raises ZstdError — neither an OSError nor any of the others, so it escaped the
    way zlib.error and LZMAError each did in turn.

    Asserted as a property of the running interpreter rather than a fixed list, so
    the test says the same thing on every supported release.
    """
    names = {error.__name__ for error in responses._DECOMPRESSION_ERRORS}
    assert {"error", "LZMAError", "EOFError"} <= names
    try:
        from compression.zstd import ZstdError  # noqa: F401
    except ImportError:
        assert "ZstdError" not in names
    else:
        assert "ZstdError" in names


# --- Round twenty-two: canonical paths, prepended data, a real deadline ------


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

    assert responses._counted_zip_entries(plain, 100) == 30
    assert responses._counted_zip_entries(with_stub, 100) == 30


def test_a_self_extracting_archive_cannot_bypass_the_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(responses, "MAX_ARCHIVE_MEMBERS", 5)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(30):
            archive.writestr(f"f{index}.txt", "")
    with_stub = b"MZ" + b"stub" * 500 + buffer.getvalue()
    with pytest.raises(ResponseError, match="over"):
        extract(with_stub, tmp_path)


def test_the_deadline_bounds_a_blocking_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two earlier attempts checked a clock in the read loop, and neither bounded
    anything: the timeout urllib takes is an *idle* socket timeout, so a server
    trickling header bytes keeps `open()` inside the network stack indefinitely.

    A clock consulted after a blocking call returns cannot bound that call. The
    fetch runs on a daemon thread joined against the deadline, so the only way to
    bound it — stopping waiting on it — is what actually happens.
    """
    monkeypatch.setattr(responses, "DOWNLOAD_DEADLINE_SECONDS", 0.2)

    class _NeverReturns:
        def open(self, *args: object, **kwargs: object):
            time.sleep(30)

    monkeypatch.setattr(responses, "_opener", lambda *_: _NeverReturns())
    started = time.monotonic()
    with pytest.raises(ResponseError, match="exceeded"):
        download("https://example.com/blocked.tar")
    assert time.monotonic() - started < 5, "the call must not wait for the sleep"


def test_a_successful_fetch_still_returns_its_body(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thread has to hand the body back, not just the failures."""

    class _Body:
        headers = {"Content-Length": "5"}

        def __init__(self) -> None:
            self._sent = False

        def read(self, size: int = -1) -> bytes:
            if self._sent:
                return b""
            self._sent = True
            return b"hello"

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(
        responses, "_opener", lambda *_: type("O", (), {"open": lambda *a, **k: _Body()})()
    )
    assert download("https://example.com/ok.tar") == b"hello"


# --- Round twenty-three: parent components, ZIP64, real cancellation ---------


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
    assert responses._counted_zip_entries(buffer.getvalue(), 100) == 41


def test_a_timed_out_fetch_closes_its_response(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marking the thread a daemon only stops an abandoned fetch holding the
    *process* open — it goes on holding a socket and its accumulated chunks, so a
    series of timed-out downloads would retain one apiece.

    The response is closed on timeout, which makes the blocked read fail and
    releases the connection immediately.
    """
    monkeypatch.setattr(responses, "DOWNLOAD_DEADLINE_SECONDS", 0.2)
    closed: list[bool] = []

    class _Stalling:
        headers: dict[str, str] = {}

        def read(self, size: int = -1) -> bytes:
            time.sleep(5)
            return b"x"

        def close(self) -> None:
            closed.append(True)

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(
        responses,
        "_opener",
        lambda *_: type("O", (), {"open": lambda *a, **k: _Stalling()})(),
    )
    with pytest.raises(ResponseError, match="exceeded"):
        download("https://example.com/stalled.tar")
    assert closed, "the response was abandoned rather than closed"


# --- Round twenty-four: container semantics, ZIP64 stubs, header stalls ------


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

    All four metadata types, not just the PAX pair the finding named -- they share
    one dispatch point and therefore one guard.
    """
    payload = _metadata_header(member_type, MAX_METADATA_BYTES + 1, name)
    with pytest.raises(ResponseError, match="metadata"):
        extract(payload, tmp_path)


def test_a_sparse_member_is_not_treated_as_metadata() -> None:
    """A GNU sparse member's declared size is its file length, not a metadata
    block, so bounding it would refuse a large member that extracts perfectly
    well. Grouping by "reads something" rather than by what the number means is
    how a guard like this acquires a false positive."""
    assert tarfile.GNUTYPE_SPARSE not in responses._TAR_METADATA_TYPES


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
    second implementation of security-relevant behaviour and produced six separate
    review findings; the floor was raised instead. What is worth asserting now is
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


def test_a_tar_parent_component_is_resolved_rather_than_removed(
    tmp_path: Path
) -> None:
    """The other half of the same rule, which a single canonical form gets wrong.

    In a tar `a/../b.txt` lands at `b.txt`, so it does *not* collide with
    `a/b.txt` -- and refusing that pair would reject an archive that extracts
    perfectly well. Applying the tar rule to both containers would have swapped
    one false negative for one false positive.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in ("a/../b.txt", "a/b.txt"):
            info = tarfile.TarInfo(name)
            info.size = 0
            tar.addfile(info, io.BytesIO(b""))
    extract(buffer.getvalue(), tmp_path)
    assert (tmp_path / "b.txt").is_file()
    assert (tmp_path / "a" / "b.txt").is_file()


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
        assert responses._counted_zip_entries(data, 100) == 41, label


def test_a_zip64_self_extracting_archive_cannot_bypass_the_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consequence of the above, which is the part that matters: returning
    None skipped the member cap, so the layout that defeated the arithmetic was
    also the layout that got past the limit."""
    monkeypatch.setattr(responses, "MAX_ARCHIVE_MEMBERS", 5)
    payload = b"MZ" + b"stub" * 500 + _zip64_archive(end_record=True)
    with pytest.raises(ResponseError, match="over"):
        extract(payload, tmp_path)


def test_the_recording_handler_publishes_the_connection_it_builds(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connection is captured where urllib creates it, keyword arguments and
    all -- `do_open` rather than `http_open`, so `context` and `check_hostname`
    are forwarded without this code having to know which of them this
    interpreter's handler passes."""
    sink: dict[str, object] = {}
    handler = responses._RecordingHTTPHandler(sink)

    class _Connection:
        def __init__(self, host: str, **kwargs: object) -> None:
            self.host = host
            self.kwargs = kwargs

    def fake_do_open(self, http_class, req, **kwargs):
        # What AbstractHTTPHandler does with the factory it is handed.
        return http_class("example.com", timeout=7, **kwargs)

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", fake_do_open)

    built = handler.do_open(_Connection, object(), context="ctx")
    assert sink["connection"] is built
    assert built.host == "example.com"
    assert built.kwargs == {"timeout": 7, "context": "ctx"}


def test_the_opener_records_connections_only_when_given_somewhere_to_put_them(
) -> None:
    """The recording handlers are opt-in, so every caller that does not need
    cancellation keeps the plain handler set -- which the handler-inventory test
    above is asserting about."""
    plain = {type(h).__name__ for h in responses._opener().handlers}
    assert "HTTPSHandler" in plain
    assert "_RecordingHTTPSHandler" not in plain

    recording = {type(h).__name__ for h in responses._opener({}).handlers}
    assert "_RecordingHTTPSHandler" in recording
    assert "_RecordingHTTPHandler" in recording


def test_the_deadline_cancels_a_stall_before_the_headers_arrive(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that trickles headers left the timeout with nothing to close.

    `open()` had not returned, so no response had been published -- and the
    previous fix closed only the response. The fetch went on holding the socket
    until its own idle timeout, which is the state that fix was meant to end. The
    connection exists well before the headers are parsed, so it is what gets
    closed.
    """
    closed: list[str] = []
    release = threading.Event()

    class _Connection:
        def close(self) -> None:
            closed.append("connection")
            release.set()

    class _Opener:
        def __init__(self, sink: dict[str, object]) -> None:
            self._sink = sink

        def open(self, *args: object, **kwargs: object) -> object:
            self._sink["connection"] = _Connection()
            release.wait(10)
            raise AssertionError("the stalled open should never complete")

    monkeypatch.setattr(responses, "DOWNLOAD_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(responses, "_opener", lambda sink=None: _Opener(sink))
    try:
        with pytest.raises(ResponseError, match="exceeded"):
            download("https://example.com/trickled-headers.tar")
        assert closed == ["connection"], (
            "the header-phase stall was abandoned rather than cancelled"
        )
    finally:
        release.set()
