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
import logging
import lzma
import re
import socket
import tarfile
import threading
import unicodedata
import urllib.error
import urllib.request
import zipfile
import zlib

try:  # pragma: no cover - present from Python 3.14
    from compression.zstd import ZstdError as _ZstdError
except ImportError:  # pragma: no cover - earlier releases
    _ZstdError = None
from pathlib import Path
from urllib.parse import unquote, urlsplit

# Stdlib logging, not the harness logger: this module's stated property is
# that it imports nothing outside the standard library, and a client that
# reaches for it should not acquire the worker's logging stack to do so.
_log = logging.getLogger(__name__)


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

    The recurring shape in this module: an ordinary property of an untrusted
    response, reported by the standard library with an exception type the handler
    did not list. Every stdlib call here is a
    place a malformed response can speak, not only the ones that read bytes.
    """


# Socket timeout for archive downloads — long enough for a slow CDN or a large
# output, short enough that a dead URL cannot hang a caller forever. Mirrors the
# worker-side fetch timeout in runpod_doc_worker.transport.io.
DOWNLOAD_TIMEOUT_SECONDS = 120.0

# Total wall-clock a fetch may take. The socket timeout above bounds only *idle*
# time and is reset by every successful read, so a peer trickling a few bytes
# before each timeout can hold the call open indefinitely without ever
# approaching the byte cap. Generous enough for a large archive over a slow link.
DOWNLOAD_DEADLINE_SECONDS = 900.0

# Base64 alphabet plus the padding character. Used to report *what* is wrong with
# a payload rather than only that something is.
_B64_ALPHABET = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")

# What a corrupt compressed stream raises from inside an archive reader. None of
# these is an OSError or the archive module's own error type, so none was caught
# by the handlers that look for those: `zlib.error` comes from a damaged deflate
# stream in either container, `lzma.LZMAError` from a damaged xz tar, and
# `EOFError` from one that ends mid-stream. bzip2 is absent on purpose — it
# reports through OSError, which is already covered.
_DECOMPRESSION_ERRORS: tuple[type[BaseException], ...] = (
    zlib.error,
    lzma.LZMAError,
    EOFError,
)
if _ZstdError is not None:  # pragma: no cover - 3.14 and later
    # Zip method 93 is Zstandard, supported from 3.14. A malformed payload
    # raises ZstdError, which is neither an OSError nor any of the above, so
    # it escaped exactly the way zlib.error and LZMAError each did in turn.
    # Added conditionally so the module keeps importing on earlier releases.
    _DECOMPRESSION_ERRORS = (*_DECOMPRESSION_ERRORS, _ZstdError)

# Caps on what a response may expand to. `extract` reads the archive into memory,
# so an unbounded body is a memory-exhaustion vector on its own, and a small
# archive can still expand to an unbounded amount of disk. Deliberately generous:
# these are backstops against a hostile or broken worker, not a policy on document
# size. A caller needing more can raise them on the module.
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000

# A tar member's *metadata* -- a PAX record block or a GNU long-name block -- as
# opposed to its contents, which `MAX_EXTRACTED_BYTES` covers. One mebibyte is
# roughly 250 times the longest path any mainstream filesystem accepts, so this
# does not constrain a real archive; what it constrains is a header that declares
# a size no real header would.
MAX_METADATA_BYTES = 1024 * 1024

# The member types whose declared size is metadata to be read into memory rather
# than file contents to be written out. Looked up rather than written as literals
# so a name this `tarfile` does not have is simply absent instead of raising at
# import; `SOLARIS_XHDTYPE` is the one that has moved.
#
# `GNUTYPE_SPARSE` is deliberately *not* here. Its `size` is the file's own data
# length, not a metadata block, so bounding it would refuse a large sparse member
# that extracts perfectly well -- the kind of false positive that comes from
# grouping by "reads something" instead of by what the number means.
_TAR_METADATA_TYPES = frozenset(
    value
    for value in (
        getattr(tarfile, name, None)
        for name in (
            "XHDTYPE",
            "XGLTYPE",
            "SOLARIS_XHDTYPE",
            "GNUTYPE_LONGNAME",
            "GNUTYPE_LONGLINK",
        )
    )
    if value is not None
)

# Reserved on Windows with any extension, and `open()` on one succeeds while
# discarding the data.
_DOS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{digit}" for digit in "123456789"}
    | {f"LPT{digit}" for digit in "123456789"}
)

# Filesystems bound a path component in bytes, and 255 is the limit on ext4,
# APFS and NTFS alike. Checked in bytes rather than characters because the charset
# permitted here includes non-ASCII: 80 CJK characters already exceed this while
# passing any character count.
MAX_OUTPUT_NAME_BYTES = 255

# Characters Windows refuses in a filename. `:` and the separators are already
# caught by the plain-filename check; these are the rest, and they fail only when
# the file is created — so a caller trusting this helper's "usable filename"
# result gets an OSError at write time instead of a refusal here.
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')

# Mode bits the stdlib `data` tar filter clears, replicated for the fallback path
# on Python patch releases that predate the `filter` parameter.
_UNSAFE_MODE_BITS = 0o7022  # setuid, setgid, sticky, group- and other-writable


def _device_stem(name: str) -> str:
    """The stem Windows compares against its reserved device names.

    Not simply ``name.split(".")[0].upper()``. Windows ignores trailing spaces and
    dots when matching, so ``"NUL .txt"`` is the ``NUL`` device, and it treats the
    superscript digits as their ASCII equivalents, so ``"COM\u00b9.txt"`` is
    ``COM1``. Both passed an exact-stem lookup and neither can be saved as an
    ordinary file.
    """
    stem = name.split(".")[0]
    for superscript, digit in (("\u00b9", "1"), ("\u00b2", "2"), ("\u00b3", "3")):
        stem = stem.replace(superscript, digit)
    return stem.rstrip(" .").upper()


def _windows_component_problem(part: str) -> str | None:
    """Why Windows cannot store a path component under this exact name, or None.

    One rule with two callers. ``safe_output_name`` had the full set and
    ``_check_member_name`` was written as a fresh, narrower copy that knew only
    about device names and colons, so ``a?.txt`` was refused as an output name and
    accepted as an archive member. That gap is not cosmetic: Windows silently
    rewrites both ``a?.txt`` and ``a*.txt`` to ``a_.txt``, so an archive carrying
    both has one overwrite the other while extraction reports success.

    Duplicating a rule and weakening the copy is the failure here, not the
    characters that were missing from it.

    Ordered most specific first, so the reason given is the useful one: ``NUL.``
    is reported as a device name rather than as a trailing dot.
    """
    if any(character < chr(32) or character == chr(127) for character in part):
        return "contains a control character"
    if _device_stem(part) in _DOS_DEVICE_NAMES:
        return "is a reserved device name on Windows"
    if ":" in part:
        return "is alternate-data-stream syntax on Windows"
    forbidden = sorted(_WINDOWS_FORBIDDEN.intersection(part))
    if forbidden:
        return (
            "contains " + repr("".join(forbidden))
            + ", which cannot appear in a Windows filename"
        )
    if part[-1] in " .":
        return "has a trailing dot or space, which Windows strips"
    return None


class _BoundedTarInfo(tarfile.TarInfo):
    """A member whose metadata is bounded *before* it is read.

    `tar.next()` processes a PAX extended header or a GNU long-name block on the
    way to the member it describes: it reads the whole declared size into memory
    and returns the real member afterwards. Both quotas below run on what `next()`
    returns, so neither had happened yet -- a 10 KB gzip could make one call
    materialise a 10 MB value while the only member it announced had size zero,
    and the size field is twelve octal digits, so far larger is expressible. The
    failure then arrives as a bare `MemoryError`, outside this module's one-error
    contract.

    Bounding it after the fact is no bound at all, since the allocation is the
    harm. So the check happens in `_proc_member`, which is where `tarfile`
    dispatches on the member type and therefore the one place ahead of all three
    metadata readers. Guarding `_proc_pax` alone would have left the two GNU
    long-name types reachable -- the same "fixed one of the call sites" shape that
    has repeatedly turned out to cover one caller and miss the other.
    """

    def _proc_member(self, archive):
        if self.type in _TAR_METADATA_TYPES and self.size > MAX_METADATA_BYTES:
            raise ResponseError(
                f"refusing tar member metadata of {self.size} bytes, over the "
                f"{MAX_METADATA_BYTES}-byte limit"
            )
        return super()._proc_member(archive)


def _canonical_member(name: str, *, container: str) -> str:
    """The path the extractor will actually write, as a comparison key.

    Container-specific, because the two containers resolve parent components
    differently and one rule is wrong for one of them:

    * zip -- ``zipfile`` *removes* ``..`` while extracting, so ``a/../b.txt``
      lands at ``a/b.txt``;
    * tar -- extraction lets the filesystem resolve it, so the same member lands
      at ``b.txt``.

    A zip-shaped rule applied to both meant a tar carrying ``a/../b.txt`` and
    ``b.txt`` compared ``a/b.txt`` against ``b.txt``, found no collision, and let
    the second overwrite the first. Folding answers "same name"; this has to
    answer "same file", and that depends on who does the extracting.
    """
    parts: list[str] = []
    for part in name.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if container == "tar" and parts:
                parts.pop()
            continue
        parts.append(part)
    # Normalised before folding. macOS is normalisation-insensitive as well as
    # case-insensitive, so NFC `\u00e9` and NFD `e` + combining acute are one file
    # there -- and `casefold` leaves the two strings distinct, so the archive
    # passed this check and the second member overwrote the first. Folded again
    # afterwards, then re-normalised, because case folding can itself denormalise
    # its result; that is the composition the Unicode caseless-matching rule
    # specifies rather than something invented here.
    joined = unicodedata.normalize("NFC", "/".join(parts))
    return unicodedata.normalize("NFC", joined.casefold())


def _check_member_collisions(names: list[str], *, container: str) -> None:
    """Refuse an archive whose member paths collide on a case-insensitive volume.

    Windows and macOS default to case-insensitive, so ``Report.txt`` and
    ``report.txt`` are one file there. Each name passes every per-name check --
    they are both perfectly legal -- and then the second extraction overwrites the
    first, silently, while the archive reports two members and extraction reports
    success.

    That is the same shape as the earlier collision finding, where Windows itself
    rewrote two distinct names into one. Per-name validation cannot see either:
    the problem is a relationship between names, so it needs a pass over the set.
    """
    seen: dict[str, str] = {}
    for name in names:
        key = _canonical_member(name, container=container)
        if not key:
            continue
        first = seen.get(key)
        if first is not None and first != name:
            raise ResponseError(
                f"refusing {container} members {first!r} and {name!r}: "
                f"they resolve to the same file"
            )
        seen[key] = name


def _check_member_name(name: str, *, container: str) -> None:
    """Refuse an archive member whose path Windows would not store faithfully.

    Containment is not enough. `within` answers "does this land under the
    destination", and a member called `NUL`, `document.txt:payload` or `a?.txt`
    does -- but the filesystem then opens a device, creates an alternate data
    stream, or silently renames it, so the member is discarded, hidden, or
    collides with another while extraction reports success.

    Checked per component and on every platform, for the same reason
    `safe_output_name` is: the worker writing the archive and the client unpacking
    it need not share an OS.
    """
    for part in name.replace("\\", "/").split("/"):
        if not part or part in (".", ".."):
            continue
        problem = _windows_component_problem(part)
        if problem is not None:
            raise ResponseError(
                f"refusing {container} member {name!r}: {part!r} {problem}"
            )


def within(destination: Path, name: str) -> bool:
    """Whether archive member ``name`` lands inside ``destination``.

    Both sides are resolved. Only the target used to be, so a *relative*
    destination — which is the obvious way to call an exported function — compared
    an absolute path against a relative one and returned False for every safe
    member. ``extract`` passes an already-resolved path and so never saw it, which
    is exactly why a public helper has to be correct on its own terms rather than
    on its caller's.
    """
    if isinstance(name, str) and "\x00" in name:
        # Checked explicitly because whether `resolve()` raises on a NUL depends
        # on the platform: POSIX rejects it, Windows computes a path and returns
        # True, so the same archive got two different answers. No valid member
        # name contains one, and a member name is untrusted by definition.
        raise ResponseError(f"refusing member {name!r}: contains a NUL")
    try:
        base = Path(destination).resolve()
        target = (base / name).resolve()
    except (TypeError, ValueError, OSError, RuntimeError) as e:
        # Exported, so a direct caller wraps it in the same `except ResponseError`
        # as everything else here. A NUL in either argument raises ValueError and a
        # non-path destination raises TypeError, both from `Path` rather than from
        # this function — and a member name is untrusted input by definition, which
        # is the whole reason this check exists.
        #
        # RuntimeError is the 3.10/3.11 spelling for a symlink loop encountered
        # while resolving. A destination reused across extractions can contain
        # one, and this function is exactly the code that walks into it.
        raise ResponseError(f"cannot place {name!r} in {destination!r}: {e}") from e
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
    problem = _windows_component_problem(name)
    if problem is not None:
        raise ResponseError(f"refusing {what} {name!r}: {problem}")
    try:
        encoded = len(name.encode("utf-8"))
    except UnicodeEncodeError as e:
        # An unpaired surrogate survives JSON decoding, so a response can carry
        # a name containing one. This measured length with errors="surrogatepass"
        # so the check itself could not crash -- which silently admitted a name
        # that raises UnicodeEncodeError the moment the caller writes it on a
        # UTF-8 filesystem. Encoding strictly makes measuring and accepting the
        # same question, which is what it should have been.
        raise ResponseError(
            f"refusing {what} {name!r}: not encodable as UTF-8 ({e.reason})"
        ) from e
    if encoded > MAX_OUTPUT_NAME_BYTES:
        # A caller writing this gets `OSError: [Errno 36] File name too long`,
        # which is the failure this helper exists to turn into a refusal — it
        # validates response-provided names *so that* the write is safe.
        raise ResponseError(
            f"refusing {what}: {encoded} bytes exceeds the "
            f"{MAX_OUTPUT_NAME_BYTES}-byte filename limit"
        )
    if _device_stem(name) in _DOS_DEVICE_NAMES:
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
    # The whole authority, not just the host: userinfo is percent-decoded for the
    # Authorization header the same way the host is for the Host header, so
    # `http://%FF@example.com/` passed a hostname-only check and still raised
    # UnicodeEncodeError from inside urlopen. Checking one component of a string
    # that gets decoded in several places is a check in the wrong place.
    try:
        unquote(parts.netloc, errors="strict").encode("ascii")
    except (UnicodeDecodeError, UnicodeEncodeError) as e:
        raise ResponseError(
            f"refusing to fetch {url!r}: the authority is not ASCII once "
            f"percent-decoded"
        ) from e


class _CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Applies :func:`require_fetchable_url` to every hop.

    urllib's default handler follows a ``Location`` wherever it points, and its
    opener has an FTP handler installed — so an accepted HTTPS endpoint
    redirecting to ``ftp://`` was fetched over FTP, past a validator whose whole
    documented job is to reject exactly that. Validating the URL a caller hands
    in says nothing about the ones the *server* chooses, and a redirect target is
    as untrusted as a response body.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        require_fetchable_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _ConnectionRecorder:
    """Publishes each connection into a caller-supplied dict as it is created.

    The deadline needs an object to close, and the response does not exist until
    ``open()`` returns -- so a server that trickles *headers* left the timeout
    with nothing to cancel and the fetch went on reading. The connection is
    created well before the headers are parsed, and closing it makes the blocked
    read fail at once.

    ``do_open`` is the interception point rather than ``http_open``/``https_open``
    because it is where the connection class is used, and it receives whatever
    keyword arguments this interpreter's handler passes (``context``,
    ``check_hostname``) without this code having to know them.
    """

    def __init__(self, sink: dict[str, object]) -> None:
        super().__init__()
        self._sink = sink

    def do_open(self, http_class, req, **kwargs):
        def build(host, **connection_args):
            connection = http_class(host, **connection_args)
            self._sink["connection"] = connection
            return connection

        return super().do_open(build, req, **kwargs)


class _RecordingHTTPHandler(_ConnectionRecorder, urllib.request.HTTPHandler):
    """``HTTPHandler``, publishing its connection as it is created."""


class _RecordingHTTPSHandler(_ConnectionRecorder, urllib.request.HTTPSHandler):
    """``HTTPSHandler``, publishing its connection as it is created."""


def _opener(sink: dict[str, object] | None = None) -> urllib.request.OpenerDirector:
    """An opener that speaks only HTTP(S) and checks every redirect.

    Assembled from an empty ``OpenerDirector`` rather than with
    ``build_opener``, which *adds to* the default handler set rather than
    replacing it — so passing the HTTP handlers still left ``FTPHandler`` and
    ``FileHandler`` installed, which is what a first attempt at this did. The
    redirect check above already refuses a non-HTTP hop; removing the handlers
    means a miss there has nothing to reach.

    ``ProxyHandler`` is kept deliberately: an operator behind a proxy expects
    ``HTTPS_PROXY`` to be honoured, and dropping it would silently change how
    every fetch is routed.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        _RecordingHTTPHandler(sink)
        if sink is not None
        else urllib.request.HTTPHandler(),
        _RecordingHTTPSHandler(sink)
        if sink is not None
        else urllib.request.HTTPSHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.UnknownHandler(),
        _CheckedRedirectHandler(),
    ):
        opener.add_handler(handler)
    return opener


def _fetch(url: str, holder: dict[str, object]) -> bytes:
    """Open, read and return the body. Blocking; bounded by its caller."""
    with _opener(holder).open(  # noqa: S310 - scheme checked, redirects checked
        url, timeout=DOWNLOAD_TIMEOUT_SECONDS
    ) as response:
        # Published so the caller can close it on timeout. A daemon thread only
        # stops holding the *process* open; it goes on holding a socket and its
        # accumulated chunks, so repeated timed-out fetches would each retain one.
        holder["response"] = response
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_ARCHIVE_BYTES:
            raise ResponseError(
                f"the archive is {int(declared)} bytes, over the "
                f"{MAX_ARCHIVE_BYTES}-byte limit"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ResponseError(
                    f"the archive exceeds the {MAX_ARCHIVE_BYTES}-byte limit"
                )
            chunks.append(chunk)
        # Reading in chunks loses the truncation check `read()` performs for free:
        # `read(n)` returns what has arrived and then b"" at EOF, so a server that
        # hangs up early yields a short body rather than IncompleteRead.
        if declared and declared.isdigit() and total < int(declared):
            raise ResponseError(
                f"fetching the archive failed: IncompleteRead "
                f"({total} bytes read, {int(declared) - total} more expected)"
            )
        return b"".join(chunks)


def download(url: str) -> bytes:
    """Fetch an archive. Network failures arrive as :class:`ResponseError`.

    An expired presigned URL, an endpoint that is refusing, or a stalled read all
    used to surface as urllib exceptions straight past a client's own handler.
    The ordinary case is the expired URL, which is also the one a caller most
    needs to catch.

    The fetch runs on a worker thread joined against a deadline. Two earlier
    attempts checked a clock in the read loop, and neither bounded anything: the
    timeout urllib takes is an *idle* socket timeout, so a server trickling header
    bytes can keep ``open()`` inside the network stack indefinitely, and a
    trickled chunk does the same to a single ``read()``. A clock consulted after a
    blocking call returns cannot bound that call -- the only way to bound it from
    here is to stop waiting on it.

    On timeout the response is closed, which makes the blocked read fail and
    releases the socket immediately. Marking the thread a daemon was not enough on
    its own: that only stops an abandoned fetch holding the *process* open, while
    it goes on holding a connection and its accumulated chunks.
    """
    require_fetchable_url(url)
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["body"] = _fetch(url, outcome)
        except BaseException as error:  # noqa: BLE001 - re-raised on the caller
            outcome["error"] = error

    worker = threading.Thread(target=run, name="runpod-doc-worker-fetch", daemon=True)
    worker.start()
    worker.join(DOWNLOAD_DEADLINE_SECONDS)
    if worker.is_alive():
        # Close the response so the blocked read fails and the socket is released
        # now rather than whenever the idle timeout happens to fire. Without this
        # the deadline bounded only the *caller*: the fetch carried on reading, and
        # a series of timed-out downloads accumulated a thread, a connection and a
        # growing chunk list apiece.
        # Shut the socket down first, then close. `close()` on either object is
        # not enough on its own: `HTTPResponse` holds a file object made from the
        # same socket, and a socket with outstanding `makefile` references does
        # not release its descriptor when closed -- so the connection survived.
        # Measured on Linux against a trickling local server: after `close()`
        # alone the server went on writing headers successfully, and after
        # `shutdown` its next write failed. `shutdown` acts on the descriptor
        # rather than on a reference to it, which is the whole difference.
        connection = outcome.get("connection")
        sock = getattr(connection, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                # Already gone, or never connected. Either way there is nothing
                # left to cancel.
                pass
        # Then both objects, response first: it owns the file wrapper, and the
        # response only exists once the headers have been read, so a timeout in
        # the header phase has only the connection.
        for key in ("response", "connection"):
            target = outcome.get(key)
            close = getattr(target, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - already abandoning this fetch
                    pass
        # Brief, and deliberately not longer. After a shutdown the blocked read
        # raises at once, so a second is generous -- and waiting any longer would
        # turn "the deadline bounds the caller" into "the deadline plus the wait",
        # which is the guarantee this whole path exists to provide. A first
        # attempt at this used five seconds and broke exactly that.
        worker.join(1.0)
        if worker.is_alive() and sock is not None:
            # Only when there was a socket to shut down. Without one there was
            # nothing to cancel and no leak to report -- and reporting one anyway
            # would cry wolf on every caller that never reached the network.
            # Reported rather than ignored otherwise: a thread still running here
            # means the cancellation did not work, and this has previously been
            # believed working while it was not.
            _log.warning(
                "the timed-out fetch did not stop after its socket was shut down"
            )
        raise ResponseError(
            f"fetching the archive exceeded {DOWNLOAD_DEADLINE_SECONDS:.0f}s"
        )

    error = outcome.get("error")
    if error is not None:
        if isinstance(error, ResponseError):
            raise error
        if isinstance(error, urllib.error.HTTPError):
            raise ResponseError(
                f"fetching the archive failed: HTTP {error.code}"
            ) from error
        if isinstance(error, urllib.error.URLError):
            raise ResponseError(
                f"fetching the archive failed: {error.reason}"
            ) from error
        if isinstance(error, http.client.HTTPException):
            raise ResponseError(
                f"fetching the archive failed: {type(error).__name__}: {error}"
            ) from error
        if isinstance(error, (TimeoutError, OSError, ValueError, UnicodeError)):
            raise ResponseError(
                f"fetching the archive failed: {type(error).__name__}: {error}"
            ) from error
        raise error
    body = outcome.get("body")
    if not isinstance(body, bytes):  # pragma: no cover - defensive
        raise ResponseError("fetching the archive produced no body")
    return body


def _open_tar(data: bytes) -> tarfile.TarFile:
    """Open a tar with the bounded parser. The only place that opens one.

    It became the only place because there had been two. The metadata bound is
    installed by passing ``tarinfo=``, so it applied to extraction and not to the
    ``is_tarfile`` call that *detects* a tar -- and detection parses the first
    member, which is exactly where an oversized PAX or GNU long-name block sits.
    A 2,180-byte gzip decompressed 2,098,688 bytes there before the limit further
    down could refuse anything, so the bound existed and the archive that defeats
    it never reached it.

    That is the same "fixed one of two call sites" shape as six earlier findings in
    this review, and the answer is the same: one producer, so the next thing added
    here cannot apply to one caller and miss the other.
    """
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:*", tarinfo=_BoundedTarInfo)


def _looks_like_tar(data: bytes) -> bool:
    """Whether this is a tar, decided by the bounded parser.

    Reimplements ``tarfile.is_tarfile``'s contract rather than calling it, because
    that function offers no way to pass ``tarinfo=``. The contract copied is the
    error split, which matters: a ``TarError`` means "not a tar" and a
    decompression or OS error means "unreadable", and the caller renders those
    differently. A ``ResponseError`` from the metadata bound is neither, so it
    propagates -- refusing at detection, which is the point.
    """
    try:
        with _open_tar(data):
            return True
    except tarfile.TarError:
        return False


def _extract_tar(data: bytes, destination: Path) -> None:
    """Extract a tar, refusing members that escape or are not regular files.

    Checked before extracting rather than relying on the stdlib filter alone, so
    the guarantee does not depend on the Python patch release. The ``data`` filter
    is then used where available for defence in depth and to avoid the 3.14
    default-filter deprecation.
    """
    try:
        tar_file = _open_tar(data)
    except (
        tarfile.TarError,
        OSError,
        TypeError,
        ValueError,
        OverflowError,
        *_DECOMPRESSION_ERRORS,
    ) as e:
        # A truncated download, or a body that was never a tar. Raised before any
        # of the safety checks below could run, so it used to bypass them and the
        # client's error type together.
        raise ResponseError(f"the archive could not be read: {e}") from e

    with tar_file as tar:
        try:
            # Incremental, not `getmembers()`. That decompresses the whole
            # stream and materialises every TarInfo before either quota can be
            # read, so a tiny gzip declaring millions of empty headers exhausts
            # memory before `len(members)` is even reached. Aborting mid-walk
            # means the cost of a hostile archive is bounded by the quota rather
            # than by the archive.
            members = []
            declared_total = 0
            while True:
                member = tar.next()
                if member is None:
                    break
                members.append(member)
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise ResponseError(
                        f"the archive declares over {MAX_ARCHIVE_MEMBERS} members"
                    )
                declared_total += member.size
                if declared_total > MAX_EXTRACTED_BYTES:
                    raise ResponseError(
                        f"the archive expands to over {MAX_EXTRACTED_BYTES} bytes"
                    )
        except (
            tarfile.TarError,
            OSError,
            TypeError,
            ValueError,
            OverflowError,
            *_DECOMPRESSION_ERRORS,
        ) as e:
            # A tar truncated after a valid first header opens cleanly and fails
            # here, so the extraction handler below was never reached.
            #
            # Enumerating a *compressed* tar decompresses the whole stream, which
            # makes this — not the extraction below — where damage deep inside an
            # xz or gzip stream actually surfaces, as `LZMAError` or `zlib.error`.
            # Widening only the extraction handler left this case escaping, which
            # is why the fix was verified by reproduction rather than by reading.
            raise ResponseError(f"the archive could not be read: {e}") from e

        _check_member_collisions([m.name for m in members], container="tar")
        for member in members:
            _check_member_name(member.name, container="tar")
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
            # `filter="data"` and nothing else. It is the standard library's own
            # hardening -- setuid/setgid and world-writable bits stripped,
            # archive-supplied ownership discarded, links and special files
            # refused -- and it exists in every interpreter this distribution now
            # supports, which is the whole reason the floor was raised to 3.10.12.
            #
            # There used to be a filterless fallback here, 88 lines re-deriving
            # those permission rules for 3.10.0-3.10.11. It was a defect source in
            # its own right -- the usable-mode mask, the umask read
            # racing other threads, an inherited setgid bit, ownership defaulting
            # to root, `None` meaning "leave alone" to the filter and "crash" to
            # the older `os.chmod` -- because it was a second implementation of
            # security-relevant behaviour, kept in step with the first by hand.
            # Deleting it removes that whole class of defect rather than the six
            # instances of it, and costs eleven patch releases from June 2023.
            tar.extractall(destination, filter="data")
        except (
            tarfile.TarError,
            OSError,
            TypeError,
            ValueError,
            OverflowError,
            *_DECOMPRESSION_ERRORS,
        ) as e:
            # TarError covers truncation, which is only discovered on read for a
            # streamed member. OSError covers the destination refusing the write —
            # a file member landing where a directory already exists raises
            # IsADirectoryError or PermissionError, which describes a response this
            # code cannot use rather than a bug in the caller. The zip path below
            # already caught OSError; this one did not.
            #
            # ValueError and OverflowError come from the timestamp: a PAX `mtime`
            # of `nan`, or one outside the platform's time_t range, reaches
            # `os.utime` and raises there. Neither the checks above nor the `data`
            # filter inspects mtime, so this covers the modern path as much as the
            # fallback -- and extraction has written files by then, which is the
            # more uncomfortable half of it.
            #
            # The decompression errors are here for the case `tarfile.open` cannot
            # see: corruption deep in a compressed stream. The header decompresses,
            # the archive opens, and the damage surfaces only when extraction reads
            # that far — as `LZMAError` for an xz tar, verified escaping this
            # handler before the tuple was widened.
            raise ResponseError(f"the archive could not be extracted: {e}") from e


def _counted_zip_entries(data: bytes, limit: int) -> int | None:
    """Central-directory records, counted by walking them, capped at ``limit``.

    Not the count in the end-of-central-directory record. That field is a claim,
    and ``ZipFile`` does not act on it: it walks the central directory for the
    declared *byte size*, so an archive can carry a thousand entries while saying
    one and still have every ``ZipInfo`` materialised. A preflight that trusts the
    number is therefore no protection against the case it exists for -- which is
    what the previous version of this did.

    Walking is bounded two ways: it stops as soon as the count exceeds ``limit``,
    so a hostile archive costs a fixed amount of work, and it refuses to read
    outside the declared directory. Returns None if the structure cannot be
    followed, leaving ``ZipFile`` as the authority on readability.
    """
    tail = data[-(65_536 + 22):]
    base = len(data) - len(tail)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        return None
    size = int.from_bytes(tail[at + 12 : at + 16], "little")
    offset = int.from_bytes(tail[at + 16 : at + 20], "little")
    if size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        at64 = tail.rfind(b"PK\x06\x06", 0, at)
        if at64 < 0 or len(tail) - at64 < 56:
            return None
        size = int.from_bytes(tail[at64 + 40 : at64 + 48], "little")
        offset = int.from_bytes(tail[at64 + 48 : at64 + 56], "little")
    # A self-extracting zip carries a stub before the archive and the EOCD offsets
    # are relative to the *embedded* archive, so the discrepancy has to be measured
    # and added -- but a ZIP64 archive puts its own end record and locator
    # *between* the directory and the EOCD, so measuring as far as the EOCD counts
    # those 76 bytes as stub. With either feature alone one candidate was right;
    # with both, the correction was off by the ZIP64 records and neither candidate
    # landed on the directory, so the walk gave up on exactly the large archives
    # the preflight exists for.
    #
    # So the measurement runs to where the directory actually ends -- the ZIP64
    # end record if there is one, the EOCD otherwise -- and each candidate is then
    # checked for a central-directory signature rather than trusted.
    eocd_at = base + at
    directory_end = eocd_at
    locator = tail.rfind(b"PK\x06\x07", 0, at)
    if locator >= 0 and at - locator == 20:
        directory_end = base + locator
        zip64_at = tail.rfind(b"PK\x06\x06", 0, locator)
        if zip64_at >= 0 and locator - zip64_at >= 56:
            directory_end = base + zip64_at

    candidates = [offset]
    prepended = directory_end - (offset + size)
    if prepended > 0:
        candidates.append(offset + prepended)
    offset = -1
    for candidate in candidates:
        if 0 <= candidate <= len(data) - 4 and data[candidate : candidate + 4] == (
            b"PK\x01\x02"
        ):
            offset = candidate
            break
    if offset < 0 or size < 0 or offset + size > len(data):
        return None
    end = offset + size

    count = 0
    cursor = offset
    while cursor + 46 <= end:
        if data[cursor : cursor + 4] != b"PK\x01\x02":
            return None
        name_len = int.from_bytes(data[cursor + 28 : cursor + 30], "little")
        extra_len = int.from_bytes(data[cursor + 30 : cursor + 32], "little")
        comment_len = int.from_bytes(data[cursor + 32 : cursor + 34], "little")
        cursor += 46 + name_len + extra_len + comment_len
        count += 1
        if count > limit:
            return count
    return count


def _extract_zip(data: bytes, destination: Path) -> None:
    counted_entries = _counted_zip_entries(data, MAX_ARCHIVE_MEMBERS)
    if counted_entries is not None and counted_entries > MAX_ARCHIVE_MEMBERS:
        raise ResponseError(
            f"the archive contains over {MAX_ARCHIVE_MEMBERS} members"
        )
    try:
        zip_file = zipfile.ZipFile(io.BytesIO(data))
    except (
        zipfile.BadZipFile,
        UnicodeDecodeError,
        NotImplementedError,
        ValueError,
        OSError,
    ) as e:
        # Reachable for the same reason the tar path is: `extract` chooses the
        # container from the leading bytes of whatever actually arrived.
        #
        # UnicodeDecodeError because a central-directory entry may set the UTF-8
        # flag and then carry bytes that are not UTF-8. `is_zipfile` still says
        # yes — it only looks for the end-of-central-directory record — so the
        # constructor is where it surfaces, and it is neither a BadZipFile nor an
        # OSError.
        #
        # NotImplementedError comes from an unsupported `extract_version`, and
        # ValueError from a central-directory offset that seeks negative. The
        # pattern across all four: `is_zipfile` looks only for the
        # end-of-central-directory record, so every field *inside* the container
        # is still untrusted and the constructor is where each is first read.
        raise ResponseError(f"the archive could not be read: {e}") from e

    with zip_file as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise ResponseError(
                f"the archive declares {len(infos)} members, over the "
                f"{MAX_ARCHIVE_MEMBERS} limit"
            )
        # Checked before writing anything. A small archive can declare an enormous
        # expansion - the classic decompression bomb - and the download cap says
        # nothing about it, because what that bounded was the *compressed* form.
        declared = sum(info.file_size for info in infos)
        if declared > MAX_EXTRACTED_BYTES:
            raise ResponseError(
                f"the archive expands to {declared} bytes, over the "
                f"{MAX_EXTRACTED_BYTES}-byte limit"
            )
        _check_member_collisions(archive.namelist(), container="zip")
        for name in archive.namelist():
            _check_member_name(name, container="zip")
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
            ValueError,
            OverflowError,
            *_DECOMPRESSION_ERRORS,
        ) as e:
            # RuntimeError is what an encrypted member raises, and
            # NotImplementedError an unsupported compression method. Both describe
            # an archive this code cannot read rather than a programming error, and
            # treating an untrusted archive as untrusted means they belong inside
            # the contract.
            #
            # ValueError is what a corrupted local-header offset produces: opening
            # a member seeks to it, and a negative result raises
            # `ValueError: negative seek value` rather than BadZipFile. The central
            # directory parsed, so nothing earlier had reason to object.
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
        # Accepting `memoryview` in the check above advertised an input this
        # function could not actually take: `BytesIO` needs a contiguous buffer,
        # so a strided view such as `memoryview(b"abcdef")[::2]` reached archive
        # detection and raised a bare BufferError. Normalising here is better
        # than narrowing the check, because a contiguous view is genuinely fine
        # and the copy only happens for the odd case.
        data = bytes(data)
    except (BufferError, ValueError, TypeError) as e:
        raise ResponseError(f"the archive payload could not be read: {e}") from e
    try:
        destination = Path(dest_dir).resolve()
    except (TypeError, ValueError, OSError, RuntimeError) as e:
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
        # RuntimeError is a symlink loop, which `within` already handled and this
        # did not - the same call, two guards, one of them narrower. A destination
        # reused across extractions is exactly where such a link accumulates.
        #
        # `dest_dir` naming an existing regular file, or a parent that cannot be
        # written, raises before either archive helper runs — so the failure fell
        # outside the contract even though it happened inside the public call.
        raise ResponseError(f"the destination could not be created: {e}") from e
    # `is_zipfile` looks for the end-of-central-directory record, so it recognises
    # every valid layout. The signature check this replaces knew only the
    # local-file header `PK\x03\x04` — so an empty zip, which begins with the EOCD
    # signature `PK\x05\x06` and is exactly what packaging an empty directory
    # produces, was routed to the tar reader and rejected as unreadable.
    # Guarded because the *detection* can fail too: `is_zipfile` catches only
    # OSError internally, so a ValueError from a bad seek while reading the
    # end-of-central-directory record would propagate from here, ahead of either
    # helper and outside their handlers.
    #
    # Precautionary rather than reproduced — worth saying, because I first added
    # this believing it was where the corrupted-offset ValueError came from. It is
    # not: that one surfaces from `extractall` opening a member, and the fix for it
    # is in `_extract_zip`. Tracing beat guessing, and the guess looked right.
    # Tar first. `is_zipfile` looks for an end-of-central-directory record near the
    # *end* of the data and tolerates arbitrary bytes before it - that is how a
    # self-extracting zip works - so it answers "is there a zip in here
    # somewhere", not "is this a zip". A tar carrying a `nested.zip` member
    # therefore said True, the whole tar was read as a zip, and extraction
    # succeeded while returning only the nested archive's entries and silently
    # dropping every real member. A wrong answer with no error, which is the worst
    # shape available here.
    #
    # Tar detection has no such ambiguity and can decide first. The comment here
    # used to say `is_tarfile` reads header magic at a fixed offset, which is
    # wrong for a compressed tar and is a fair part of why the metadata bound was
    # missing from this path: detection *parses the first member*, decompressing
    # whatever that takes. So it goes through the same bounded parser extraction
    # uses.
    try:
        looks_like_tar = _looks_like_tar(data)
    except (ValueError, OSError, *_DECOMPRESSION_ERRORS) as e:
        raise ResponseError(f"the archive could not be read: {e}") from e
    if looks_like_tar:
        _extract_tar(data, destination)
        return destination

    try:
        looks_like_zip = zipfile.is_zipfile(io.BytesIO(data))
    except (ValueError, OSError, zipfile.BadZipFile) as e:
        raise ResponseError(f"the archive could not be read: {e}") from e
    if looks_like_zip:
        _extract_zip(data, destination)
    else:
        # Neither container. Reported through the tar reader so the message keeps
        # naming what is actually wrong with the bytes.
        _extract_tar(data, destination)
    return destination
