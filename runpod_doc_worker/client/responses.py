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
import os
import time
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
_DECOMPRESSION_ERRORS = (zlib.error, lzma.LZMAError, EOFError)

# Caps on what a response may expand to. `extract` reads the archive into memory,
# so an unbounded body is a memory-exhaustion vector on its own, and a small
# archive can still expand to an unbounded amount of disk. Deliberately generous:
# these are backstops against a hostile or broken worker, not a policy on document
# size. A caller needing more can raise them on the module.
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000

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


def _directory_mode(destination: Path) -> int:
    """The mode ``mkdir`` produces here and now, under the current umask.

    Measured by creating a directory and looking at it, which is the only way to
    learn this without touching process-global state. Two earlier versions were
    each wrong in a different direction:

    * reading the umask means setting it to zero and setting it back, and that is
      process-global - another thread creating a file inside that window gets
      permissions as though no mask were set, and two concurrent extractions can
      interleave their swaps and leave the umask permanently changed;
    * copying the *destination's* mode is not the same as creating under the
      umask. An existing ``0o700`` destination under umask ``022`` made every
      extracted subdirectory ``0o700``, and a permissive existing destination
      overrode a restrictive umask in the other direction.

    A probe answers the question that is actually being asked. It is created
    inside ``destination`` so it lands on the same filesystem, and removed
    immediately. Falls back to ``0o755`` when it cannot be created, which is the
    conservative value the first version of this used.
    """
    probe = destination / ".runpod-doc-worker-mode-probe"
    try:
        probe.mkdir()
    except OSError:
        return 0o755
    try:
        # 0o7777, not 0o777. A setgid destination gives the probe an inherited
        # 0o2000, and masking it off produced a concrete mode that legacy tarfile
        # then applied -- clearing setgid from every extracted directory, so files
        # created there afterwards stopped inheriting the shared group. The
        # standard filter avoids this by not setting a mode at all; keeping the
        # bit is how a concrete mode reproduces that.
        return probe.stat().st_mode & 0o7777
    except OSError:
        return 0o755
    finally:
        try:
            probe.rmdir()
        except OSError:
            pass


def _apply_data_filter_mode(member: tarfile.TarInfo, directory_mode: int) -> None:
    """Apply the stdlib ``data`` filter's permission rules to ``member``.

    Transcribed from ``tarfile._get_filtered_attrs`` rather than reconstructed
    from memory, which is the point: this is the fifth revision of this code, and
    the first four each replicated part of the filter and missed part. In order —
    extract with full trust; clear the dangerous bits; also grant owner
    read/write; stop overriding directory modes; and now the conditional that was
    missing from all of them.

    That conditional is the one worth naming. The filter clears **every** execute
    bit when owner-execute was not set, so a member archived as ``0o001`` ends up
    ``0o600``. Masking and then OR-ing owner read/write, as this did, left it
    ``0o601`` — still executable by others, from an untrusted archive.

    Two deliberate deviations, both for the same reason: the filter expresses
    "leave this alone" as ``None``, and the older ``tarfile`` this fallback exists
    for passes ``None`` straight to ``os.chmod`` and ``os.chown``. Modern
    ``TarFile.chown`` turns ``None`` into ``-1`` itself, and that guard arrived
    with filter support — so copying the literal breaks the interpreters this code
    exists for, exactly as it did for ``mode``. Copy the semantics instead:

    * a directory gets a concrete mode derived from the destination;
    * ownership is set to ``-1``, which is ``os.chown``'s own "do not change" and
      an int on every version. Zero was wrong: it means *root*, so a root client
      extracting into a setgid or shared destination replaced the inherited group
      and could make the artifacts unreachable for the people meant to read them.

    ``uname``/``gname`` stay empty so the name-based lookup in ``chown`` finds
    nothing and cannot override the numeric values.
    """
    mode = member.mode
    if mode is not None:
        mode = mode & 0o755
        if member.isreg() or member.islnk():
            if not mode & 0o100:
                mode &= ~0o111
            mode |= 0o600
        elif member.isdir() or member.issym():
            mode = directory_mode
        member.mode = mode
    member.uid, member.gid = -1, -1
    member.uname, member.gname = "", ""


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


def _opener() -> urllib.request.OpenerDirector:
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
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.UnknownHandler(),
        _CheckedRedirectHandler(),
    ):
        opener.add_handler(handler)
    return opener


def download(url: str) -> bytes:
    """Fetch an archive. Network failures arrive as :class:`ResponseError`.

    An expired presigned URL, an endpoint that is refusing, or a stalled read all
    used to surface as urllib exceptions straight past a client's own handler.
    The ordinary case is the expired URL, which is also the one a caller most
    needs to catch.
    """
    require_fetchable_url(url)
    try:
        with _opener().open(  # noqa: S310 - scheme checked, redirects checked
            url, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            # Read in bounded chunks rather than `response.read()`. The socket
            # timeout only bounds *idle* time, so a peer that keeps sending can
            # hold the connection open indefinitely and grow this buffer without
            # ever tripping it. The declared Content-Length is not trusted for
            # this - it is checked as an early exit, but the running total is what
            # actually stops the read.
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_ARCHIVE_BYTES:
                raise ResponseError(
                    f"the archive is {int(declared)} bytes, over the "
                    f"{MAX_ARCHIVE_BYTES}-byte limit"
                )
            chunks: list[bytes] = []
            total = 0
            started = time.monotonic()
            while True:
                if time.monotonic() - started > DOWNLOAD_DEADLINE_SECONDS:
                    raise ResponseError(
                        f"fetching the archive exceeded "
                        f"{DOWNLOAD_DEADLINE_SECONDS:.0f}s"
                    )
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise ResponseError(
                        f"the archive exceeds the {MAX_ARCHIVE_BYTES}-byte limit"
                    )
                chunks.append(chunk)
            # Reading in chunks loses the truncation check that `read()` performs
            # for free: `read(n)` returns what has arrived and then returns b"" at
            # EOF, so a server that hangs up early yields a short body instead of
            # `IncompleteRead`. Bounding the read silently disabled the detection
            # of a truncated one, which an existing test caught. Compared against
            # the declared length here instead.
            if declared and declared.isdigit() and total < int(declared):
                raise ResponseError(
                    f"fetching the archive failed: IncompleteRead "
                    f"({total} bytes read, {int(declared) - total} more expected)"
                )
            return b"".join(chunks)
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
    except (ValueError, UnicodeError) as e:
        # Validating the URL handed in says nothing about where the server sends
        # us next: urllib follows redirects internally, and a `Location` of
        # `http://[bad` raises ValueError from inside urlopen without ever passing
        # through require_fetchable_url. A response's redirect target is as
        # untrusted as its body.
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
                # Directories are created here, shallowest first, so each one
                # inherits from its *own* parent -- a setgid `shared/` passes the
                # bit to `shared/new/`. A single probe mode applied to every
                # directory could not do that: it was measured against the
                # destination, so a directory created under a setgid subdirectory
                # had that bit chmod'd away by the legacy extractall.
                for member in sorted(
                    (m for m in members if m.isdir()), key=lambda m: m.name.count("/")
                ):
                    if within(destination, member.name):
                        made = destination / member.name
                        try:
                            made.mkdir(parents=True, exist_ok=True)
                            member.mode = made.stat().st_mode & 0o7777
                        except OSError:
                            member.mode = _directory_mode(destination)
                directory_mode = _directory_mode(destination)
                for member in members:
                    if member.isdir():
                        # Already carries the mode its parent gave it.
                        member.uid, member.gid = -1, -1
                        member.uname, member.gname = "", ""
                        continue
                    _apply_data_filter_mode(member, directory_mode)
                tar.extractall(destination, members=members)  # noqa: S202
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


def _extract_zip(data: bytes, destination: Path) -> None:
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
    # `is_tarfile` reads the header magic at a fixed offset, so it has no such
    # ambiguity and can decide first.
    try:
        looks_like_tar = tarfile.is_tarfile(io.BytesIO(data))
    except (ValueError, OSError, tarfile.TarError, *_DECOMPRESSION_ERRORS) as e:
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
