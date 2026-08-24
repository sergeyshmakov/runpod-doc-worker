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
import subprocess
import sys
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
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(raised)
    )
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


@pytest.mark.filterwarnings("ignore:Python 3.14 will:DeprecationWarning")
def test_the_legacy_tar_fallback_strips_unsafe_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On patch releases without `filter`, the fallback used to extract with full
    trust.

    The member checks reject links, special files and traversal, but say nothing
    about permissions — so an unfiltered extractall honoured setuid, setgid and
    world-writable bits and the archive's own uid/gid. A crafted response could
    drop a setuid binary, especially with a client running as root.

    Forced here by making `filter="data"` raise TypeError the way an old
    interpreter would, since the one running this suite supports it.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("payload.sh")
        info.size = 0
        info.mode = 0o4777
        info.uid, info.gid = 1234, 5678
        info.uname, info.gname = "attacker", "attacker"
        tar.addfile(info, io.BytesIO(b""))

    seen: list[tarfile.TarInfo] = []
    real_extractall = tarfile.TarFile.extractall

    def fake_extractall(self, path=None, members=None, **kwargs):
        if "filter" in kwargs:
            raise TypeError("extractall() got an unexpected keyword argument 'filter'")
        seen.extend(members or [])
        return real_extractall(self, path, members)

    monkeypatch.setattr(tarfile.TarFile, "extractall", fake_extractall)
    extract(buffer.getvalue(), tmp_path)

    assert seen, "the fallback path did not run"
    for member in seen:
        assert not member.mode & 0o7000, f"{member.name} kept a setuid/setgid/sticky bit"
        assert not member.mode & 0o022, f"{member.name} stayed group/other-writable"
        assert (member.uid, member.gid) == (0, 0), "archive-supplied ownership survived"
        assert (member.uname, member.gname) == ("", "")


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
    def exploding_urlopen(*args: object, **kwargs: object):
        raise ValueError("Invalid IPv6 URL")

    monkeypatch.setattr(urllib.request, "urlopen", exploding_urlopen)
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


@pytest.mark.filterwarnings("ignore:Python 3.14 will:DeprecationWarning")
def test_the_legacy_tar_fallback_leaves_usable_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing the dangerous bits is only half of what `filter="data"` does.

    It also makes the result usable: a regular file gets owner read/write and an
    archived directory mode is ignored in favour of something traversable. The
    previous fix only removed bits, so a member stored as mode 000 extracted as
    000 and the client was handed a file it could not open while the job looked
    like a success.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        locked = tarfile.TarInfo("locked.txt")
        locked.size = 0
        locked.mode = 0o000
        tar.addfile(locked, io.BytesIO(b""))
        folder = tarfile.TarInfo("sub")
        folder.type = tarfile.DIRTYPE
        folder.mode = 0o000
        tar.addfile(folder)

    seen: list[tarfile.TarInfo] = []
    real_extractall = tarfile.TarFile.extractall

    def fake_extractall(self, path=None, members=None, **kwargs):
        if "filter" in kwargs:
            raise TypeError("extractall() got an unexpected keyword argument 'filter'")
        seen.extend(members or [])
        return real_extractall(self, path, members)

    monkeypatch.setattr(tarfile.TarFile, "extractall", fake_extractall)
    extract(buffer.getvalue(), tmp_path)

    assert seen, "the fallback path did not run"
    by_name = {member.name: member for member in seen}
    assert by_name["locked.txt"].mode & 0o600 == 0o600, "the file is unreadable"
    # A directory's mode is left as None so creation honours the umask, which is
    # what the `data` filter does — this assertion said 0o755 until a review
    # pointed out that hard-coding it overrides the caller's umask.
    assert by_name["sub"].mode is not None, "None breaks the legacy tarfile"
    # Scoped to files. A directory's mode is `0o777 & ~umask`, which under umask 0
    # is world-writable — and correctly so: that is what `mkdir` produces, and it
    # is what `filter="data"` leaves behind by skipping the chmod entirely. The
    # unsafe-bits rule exists to stop the *archive* dictating permissions, not to
    # override the operator's umask.
    for member in seen:
        if not member.isdir():
            assert not member.mode & 0o7022, "an unsafe bit survived on a file"


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


@pytest.mark.filterwarnings("ignore:Python 3.14 will:DeprecationWarning")
def test_the_legacy_fallback_leaves_directory_mode_to_the_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `data` filter sets directory mode to None so creation honours the
    process umask. Hard-coding 0o755 made an archive directory world-traversable
    for a client running under umask 077 — replicating the shape of the filter's
    behaviour and not its point."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        folder = tarfile.TarInfo("sub")
        folder.type = tarfile.DIRTYPE
        folder.mode = 0o777
        tar.addfile(folder)

    seen: list[tarfile.TarInfo] = []
    real_extractall = tarfile.TarFile.extractall

    def fake_extractall(self, path=None, members=None, **kwargs):
        if "filter" in kwargs:
            raise TypeError("no filter on this release")
        seen.extend(members or [])
        return real_extractall(self, path, members)

    monkeypatch.setattr(tarfile.TarFile, "extractall", fake_extractall)
    extract(buffer.getvalue(), tmp_path)

    directories = [m for m in seen if m.isdir()]
    assert directories, "the fallback path did not run"
    import os as _os

    mask = _os.umask(0)
    _os.umask(mask)
    expected = 0o777 & ~mask
    for member in directories:
        assert member.mode == expected, "the directory mode ignores the umask"
        # Not None. This assertion said `is None` for one round, copying what the
        # `data` filter does — but the `mode is None` guard in TarFile.chmod
        # arrived together with filter support, so on the older releases this
        # fallback exists for, None reaches os.chmod and raises TypeError. The
        # fix worked only where it never runs.
        assert member.mode is not None, "None is unusable on the legacy tarfile"


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
