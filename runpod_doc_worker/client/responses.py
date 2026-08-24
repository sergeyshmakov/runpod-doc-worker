"""Reading a worker's response safely: archives, filenames, base64 payloads.

Every function here treats the response as untrusted. That is not paranoia about
the worker — an archive extractor which trusts member names is a path-traversal
bug (CVE-2007-4559) regardless of who wrote the archive, and a ``transport="s3"``
result is fetched from a URL rather than read out of the response body.

**Why this lives in the harness.** It did not, and that cost real bugs. Each
consumer repo shipped its own copy of these helpers — one as ``_``-prefixed
private functions inside its client module, another lifted into a separate
module — so hardening one copy left the other untouched. Three fixes made in one
client (malformed tar, corrupt zip, and network failures escaping the client's own
error type) never reached the four identical sites in the other, and neither
validated base64 at all. Shared here, a fix lands once for every consumer,
including the next one.

Nothing in this module imports the rest of the harness, and nothing outside the
standard library — so importing it costs nothing at runtime beyond what Python has
already loaded.

That is a claim about imports, not about installation, and an earlier draft of this
docstring overstated it. Installing this distribution brings httpx and httpcore,
because the worker side declares them, and pip does that for anyone who depends on
it including a client that touches none of it. Putting those behind an extra would
change what every existing worker installs, so the trade is deliberate: the
alternative is a second distribution, which costs a whole release pipeline to save
two wheels.
"""

from __future__ import annotations

import base64
import binascii
import http.client
import io
import lzma
import re
import tarfile
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path
from urllib.parse import unquote, urlsplit


class ResponseError(RuntimeError):
    """A worker response could not be trusted, fetched, or read.

    One failure type for the whole module, which is the property a caller
    actually needs: a client wrapping these calls catches this and re-raises its
    own error, so anything escaping uncaught arrives at user code as a raw stdlib
    exception from a library that documents a single error class. Every path that
    used to leak — ``tarfile.ReadError`` on a truncated body,
    ``zipfile.BadZipFile`` on a corrupt one, ``HTTPError``/``URLError``/bare
    ``TimeoutError`` from a fetch, ``IncompleteRead`` from an interrupted one,
    ``zlib.error``/``LZMAError`` from a damaged compressed stream,
    ``binascii.Error`` from a decode, and ``TypeError`` from a field that was
    not the type it was annotated as — now arrives as this.

    The recurring shape, learned over four review rounds on this one module: an
    ordinary property of an untrusted response, reported by the standard library
    with an exception type the handler did not list. Every stdlib call here is a
    place a malformed response can speak, not only the ones that read bytes.
    """


# Socket timeout for archive downloads — long enough for a slow CDN or a large
# output, short enough that a dead URL cannot hang a caller forever. Mirrors the
# worker-side fetch timeout in runpod_doc_worker.transport.io.
DOWNLOAD_TIMEOUT_SECONDS = 120.0

# Base64 alphabet plus the padding character. Used to report *what* is wrong with
# a payload rather than only that something is.
_B64_ALPHABET = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")

# What a corrupt compressed stream raises from inside an archive reader. None of
# these is an OSError or the archive module's own error type, so none was caught
# by the handlers that look for those: `zlib.error` comes from a damaged deflate
# stream in either container, `lzma.LZMAError` from a damaged xz tar, and
# `EOFError` from one that ends mid-stream. bzip2 is absent on purpose — it
# reports through OSError, which is already covered.
_DECOMPRESSION_ERRORS = (zlib.error, lzma.LZMAError, EOFError)

# Reserved on Windows with any extension, and `open()` on one succeeds while
# discarding the data.
_DOS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{digit}" for digit in "123456789"}
    | {f"LPT{digit}" for digit in "123456789"}
)

# Mode bits the stdlib `data` tar filter clears, replicated for the fallback path
# on Python patch releases that predate the `filter` parameter.
_UNSAFE_MODE_BITS = 0o7022  # setuid, setgid, sticky, group- and other-writable


def within(destination: Path, name: str) -> bool:
    """Whether archive member ``name`` lands inside ``destination``.

    Both sides are resolved. Only the target used to be, so a *relative*
    destination — which is the obvious way to call an exported function — compared
    an absolute path against a relative one and returned False for every safe
    member. ``extract`` passes an already-resolved path and so never saw it, which
    is exactly why a public helper has to be correct on its own terms rather than
    on its caller's.
    """
    base = Path(destination).resolve()
    target = (base / name).resolve()
    return target == base or base in target.parents


def safe_output_name(name: str, *, what: str) -> str:
    """Return ``name`` if it is usable as a single output filename.

    Result dicts name the files a client writes — an entry's ``basename`` becomes
    a document stem, and each key of an image map becomes a file in a directory.
    Both are only ever plain filenames coming from a worker, so anything carrying
    a directory component means the caller is holding a result this code did not
    produce, and guessing what they meant is worse than saying so.
    """
    # A parsed response honours its annotation only if the worker sent what it
    # promised. A truthy non-string — `123`, `["a"]` — passed the emptiness check
    # and then made `Path(name)` raise a bare TypeError; a falsy one such as `{}`
    # was caught, but reported as "not a usable filename", which describes the
    # wrong problem.
    if not isinstance(name, str):
        raise ResponseError(
            f"{what} should be a string; got {type(name).__name__}"
        )
    if not name or name in (".", ".."):
        raise ResponseError(f"refusing {what} {name!r}: not a usable filename")
    if name != Path(name).name or "/" in name or "\\" in name:
        raise ResponseError(f"refusing {what} {name!r}: expected a plain filename")
    # A NUL passes every check above — it has no directory component and
    # `Path(name).name` keeps it — and then every file API rejects it with a
    # raw `ValueError: embedded null byte`. Control characters are the same
    # shape of problem: nothing here would stop them and nothing downstream
    # wants them in a filename.
    if any(character < " " or character == "\x7f" for character in name):
        raise ResponseError(
            f"refusing {what} {name!r}: contains a control character"
        )
    # A DOS device name is not a file. On Windows `open("NUL", "wb")` succeeds and
    # discards everything written to it, so an artifact named `NUL` would be
    # reported as saved and be gone — the quietest possible data loss. The names
    # are reserved with any extension (`aux.txt`) and the behaviour does not
    # depend on this process running on Windows, because the caller writing the
    # file might be.
    if name.split(".")[0].upper() in _DOS_DEVICE_NAMES:
        raise ResponseError(
            f"refusing {what} {name!r}: reserved device name on Windows"
        )
    # Windows silently strips these, so `report.` becomes `report` and two
    # artifacts one dot apart would collide and overwrite each other.
    if name[-1] in " .":
        raise ResponseError(
            f"refusing {what} {name!r}: trailing dot or space"
        )
    return name


def decode_b64(payload: object, *, what: str) -> bytes:
    """Decode a base64 field from a response, strictly.

    ``base64.b64decode`` **discards** characters outside the alphabet by default:
    ``b64decode("!!!!")`` returns ``b""``. So a corrupted or truncated payload
    decoded to empty or altered bytes, a client wrote that to disk, and the job
    was reported as a success. Both consumer clients did this at two sites each.

    Whitespace is stripped before validating rather than rejected. ``validate=True``
    refuses newlines, and line-wrapped base64 is what several encoders emit — so
    validating the raw string would trade one silent-corruption bug for a
    false-negative on well-formed input.
    """
    if not isinstance(payload, str):
        raise ResponseError(
            f"{what} should be a base64 string; got {type(payload).__name__}"
        )
    compact = "".join(payload.split())
    if not _B64_ALPHABET.match(compact):
        raise ResponseError(f"{what} contains characters that are not base64")
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as e:
        # Wrong length, misplaced padding — real for a truncated payload, which
        # is the case the default decoder does report.
        raise ResponseError(f"{what} is not valid base64: {e}") from e


def require_fetchable_url(url: str) -> None:
    """Reject a URL that is not a usable HTTP(S) target, before fetching it.

    Raises on refusal and returns nothing. Deliberately *not* named
    ``require_http_url``: :func:`runpod_doc_worker.transport.net.require_http_url`
    has that name and returns the validated **host**, so two functions one import
    apart would have had the same name and different contracts, and a consumer
    writing the familiar ``url = require_http_url(url)`` would have replaced the
    URL with ``None``. That is the trap AGENTS.md already records about the
    worker-side helper; giving it a same-named sibling would have doubled it.

    Worker presigned URLs are always HTTPS. Anything else in that field means the
    result did not come from where the caller thinks it did, and ``urlopen``
    would happily read ``file://``.

    The scheme prefix alone is not enough. A string can start with ``https://`` and
    still be malformed in ways that raise from inside the stdlib rather than from
    here — ``https://[bad`` raises ``ValueError: Invalid IPv6 URL`` while splitting,
    and ``https://host:bad/x`` raises ``http.client.InvalidURL: nonnumeric port`` at
    connect time. Both escaped the single-error contract, so the parse happens here
    where it can be reported as one.
    """
    if not isinstance(url, str):
        # Iterating the characters below is the first thing that touches the
        # value, and `for character in None` raises a bare TypeError from inside
        # a function whose whole purpose is to report bad input as ResponseError.
        raise ResponseError(f"a URL should be a string; got {type(url).__name__}")
    # A request target is ASCII, and only its printable range. Everything outside
    # that reaches the network layer as a raw exception rather than as this
    # function's error: a space or control character raises `InvalidURL` from
    # inside http.client, and a non-ASCII character such as the `é` in
    # `https://example.com/é` raises `UnicodeEncodeError` while the request line
    # is being encoded. A caller that means to fetch such a path percent-encodes
    # it, which is ASCII; an IDN host needs punycode, which is also ASCII.
    #
    # The newline is the one that matters beyond tidy error types: it is how a
    # response would try to smuggle a second request line into the connection.
    for character in url:
        if not ("\x21" <= character <= "\x7e"):
            raise ResponseError(
                f"refusing to fetch {url!r}: {character!r} cannot appear in a "
                f"request target (expected printable ASCII, percent-encoded)"
            )
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise ResponseError(f"refusing to fetch {url!r}: {e}") from e
    if parts.scheme.lower() not in ("http", "https"):
        raise ResponseError(f"refusing to fetch {url!r}: expected an http(s) URL")
    try:
        host = parts.hostname
        parts.port  # noqa: B018 — property raises on a non-numeric port
    except ValueError as e:
        raise ResponseError(f"refusing to fetch {url!r}: {e}") from e
    if not host:
        raise ResponseError(f"refusing to fetch {url!r}: no host")
    # The printable-ASCII check above sees the *encoded* URL, so `http://%FF/`
    # passes it and passes the host check — and then urllib percent-decodes the
    # authority, gets U+FFFD, and raises UnicodeEncodeError building the latin-1
    # Host header. A real IDN host arrives already punycoded (`xn--…`), which is
    # ASCII, so nothing legitimate decodes to non-ASCII here.
    try:
        unquote(host, errors="strict").encode("ascii")
    except (UnicodeDecodeError, UnicodeEncodeError) as e:
        raise ResponseError(
            f"refusing to fetch {url!r}: host is not ASCII once percent-decoded"
        ) from e


def download(url: str) -> bytes:
    """Fetch an archive. Network failures arrive as :class:`ResponseError`.

    An expired presigned URL, an endpoint that is refusing, or a stalled read all
    used to surface as urllib exceptions straight past a client's own handler.
    The ordinary case is the expired URL, which is also the one a caller most
    needs to catch.
    """
    require_fetchable_url(url)
    try:
        with urllib.request.urlopen(  # noqa: S310 — scheme checked above
            url, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise ResponseError(f"fetching the archive failed: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ResponseError(f"fetching the archive failed: {e.reason}") from e
    except http.client.HTTPException as e:
        # A server that closes after sending fewer bytes than its declared
        # Content-Length raises `IncompleteRead` from `read()` — an interrupted
        # download, the most ordinary failure there is, and an HTTPException
        # rather than an OSError, so it escaped every handler here. The base
        # class covers its siblings too: a malformed status line, an
        # over-long header.
        raise ResponseError(
            f"fetching the archive failed: {type(e).__name__}: {e}"
        ) from e
    except (TimeoutError, OSError) as e:
        # TimeoutError arrives unwrapped when the stall is in the response body
        # rather than the connect, and URLError is itself an OSError — so this
        # stays last to remain reachable.
        raise ResponseError(
            f"fetching the archive failed: {type(e).__name__}: {e}"
        ) from e


def _extract_tar(data: bytes, destination: Path) -> None:
    """Extract a tar, refusing members that escape or are not regular files.

    Checked before extracting rather than relying on the stdlib filter alone, so
    the guarantee does not depend on the Python patch release. The ``data`` filter
    is then used where available for defence in depth and to avoid the 3.14
    default-filter deprecation.
    """
    try:
        tar_file = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except (tarfile.TarError, OSError, *_DECOMPRESSION_ERRORS) as e:
        # A truncated download, or a body that was never a tar. Raised before any
        # of the safety checks below could run, so it used to bypass them and the
        # client's error type together.
        raise ResponseError(f"the archive could not be read: {e}") from e

    with tar_file as tar:
        try:
            members = tar.getmembers()
        except (tarfile.TarError, OSError, *_DECOMPRESSION_ERRORS) as e:
            # A tar truncated after a valid first header opens cleanly and fails
            # here, so the extraction handler below was never reached.
            #
            # Enumerating a *compressed* tar decompresses the whole stream, which
            # makes this — not the extraction below — where damage deep inside an
            # xz or gzip stream actually surfaces, as `LZMAError` or `zlib.error`.
            # Widening only the extraction handler left this case escaping, which
            # is why the fix was verified by reproduction rather than by reading.
            raise ResponseError(f"the archive could not be read: {e}") from e
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise ResponseError(
                    f"refusing unsafe tar member {member.name!r} "
                    f"(not a regular file or dir)"
                )
            if not within(destination, member.name):
                raise ResponseError(
                    f"refusing tar member {member.name!r}: path escapes the destination"
                )
        try:
            try:
                tar.extractall(destination, filter="data")
            except TypeError:
                # Older patch releases have no `filter` parameter. The checks
                # above reject links, special files and traversal, but they say
                # nothing about permissions — so an unfiltered extractall here
                # would honour setuid, setgid and world-writable bits and the
                # archive's own uid/gid, which is what the `data` filter exists
                # to strip. A crafted response could drop a setuid binary,
                # especially with a client running as root.
                for member in members:
                    member.mode &= ~_UNSAFE_MODE_BITS
                    member.uid, member.gid = 0, 0
                    member.uname, member.gname = "", ""
                tar.extractall(destination, members=members)  # noqa: S202
        except (tarfile.TarError, OSError, *_DECOMPRESSION_ERRORS) as e:
            # TarError covers truncation, which is only discovered on read for a
            # streamed member. OSError covers the destination refusing the write —
            # a file member landing where a directory already exists raises
            # IsADirectoryError or PermissionError, which describes a response this
            # code cannot use rather than a bug in the caller. The zip path below
            # already caught OSError; this one did not.
            #
            # The decompression errors are here for the case `tarfile.open` cannot
            # see: corruption deep in a compressed stream. The header decompresses,
            # the archive opens, and the damage surfaces only when extraction reads
            # that far — as `LZMAError` for an xz tar, verified escaping this
            # handler before the tuple was widened.
            raise ResponseError(f"the archive could not be extracted: {e}") from e


def _extract_zip(data: bytes, destination: Path) -> None:
    try:
        zip_file = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        # Reachable for the same reason the tar path is: `extract` chooses the
        # container from the leading bytes of whatever actually arrived.
        raise ResponseError(f"the archive could not be read: {e}") from e

    with zip_file as archive:
        for name in archive.namelist():
            if not within(destination, name):
                raise ResponseError(
                    f"refusing zip member {name!r}: path escapes the destination"
                )
        try:
            archive.extractall(destination)  # noqa: S202 — names checked above
        except (
            zipfile.BadZipFile,
            OSError,
            RuntimeError,
            NotImplementedError,
            *_DECOMPRESSION_ERRORS,
        ) as e:
            # RuntimeError is what an encrypted member raises, and
            # NotImplementedError an unsupported compression method. Both describe
            # an archive this code cannot read rather than a programming error, and
            # treating an untrusted archive as untrusted means they belong inside
            # the contract.
            #
            # The decompression errors cover the case the header cannot reveal: a
            # zip whose central directory is intact — so `is_zipfile` says yes and
            # `ZipFile()` opens it — but whose deflate payload has a flipped byte.
            # That surfaces as a raw `zlib.error` only once extraction inflates it.
            raise ResponseError(f"the archive could not be extracted: {e}") from e


def extract(data: bytes, dest_dir: str | Path) -> Path:
    """Extract a tarball or zip into ``dest_dir``. Returns the directory.

    The container is detected from the bytes rather than from a field on the
    response, because the archive format is a *request* parameter and the response
    is what actually arrived.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        # `io.BytesIO(data)` raises a bare TypeError, and `extract` is exported
        # directly — a parsed response field honours its annotation only if the
        # worker sent what it promised, which is the same reason the URL, base64
        # and output-name helpers all check their own input.
        raise ResponseError(
            f"an archive should be bytes; got {type(data).__name__}"
        )
    try:
        destination = Path(dest_dir).resolve()
    except (TypeError, ValueError, OSError) as e:
        # Resolution happens before the guarded creation below, so it was outside
        # the contract: a NUL in the path raises ValueError, and a non-path
        # `dest_dir` raises TypeError from `Path()` itself.
        raise ResponseError(f"the destination is not a usable path: {e}") from e
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as e:
        # ValueError, not only OSError: a NUL in the path survives `Path()` and
        # `resolve()` and is rejected here as
        # `ValueError: embedded null character in path`. Guarding the resolve
        # alone left this one escaping, which the reproduction caught.
        # `dest_dir` naming an existing regular file, or a parent that cannot be
        # written, raises before either archive helper runs — so the failure fell
        # outside the contract even though it happened inside the public call.
        raise ResponseError(f"the destination could not be created: {e}") from e
    # `is_zipfile` looks for the end-of-central-directory record, so it recognises
    # every valid layout. The signature check this replaces knew only the
    # local-file header `PK\x03\x04` — so an empty zip, which begins with the EOCD
    # signature `PK\x05\x06` and is exactly what packaging an empty directory
    # produces, was routed to the tar reader and rejected as unreadable.
    if zipfile.is_zipfile(io.BytesIO(data)):
        _extract_zip(data, destination)
    else:
        _extract_tar(data, destination)
    return destination
