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
standard library. It is safe to import from a client package that has no reason
to pull in httpx or boto3.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


class ResponseError(RuntimeError):
    """A worker response could not be trusted, fetched, or read.

    One failure type for the whole module, which is the property a caller
    actually needs: a client wrapping these calls catches this and re-raises its
    own error, so anything escaping uncaught arrives at user code as a raw stdlib
    exception from a library that documents a single error class. Every path that
    used to leak — ``tarfile.ReadError`` on a truncated body,
    ``zipfile.BadZipFile`` on a corrupt one, ``HTTPError``/``URLError``/bare
    ``TimeoutError`` from a fetch, and ``binascii.Error`` from a decode — now
    arrives as this.
    """


# Socket timeout for archive downloads — long enough for a slow CDN or a large
# output, short enough that a dead URL cannot hang a caller forever. Mirrors the
# worker-side fetch timeout in runpod_doc_worker.transport.io.
DOWNLOAD_TIMEOUT_SECONDS = 120.0

# Base64 alphabet plus the padding character. Used to report *what* is wrong with
# a payload rather than only that something is.
_B64_ALPHABET = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")


def within(destination: Path, name: str) -> bool:
    """Whether archive member ``name`` lands inside ``destination``."""
    target = (destination / name).resolve()
    return target == destination or destination in target.parents


def safe_output_name(name: str, *, what: str) -> str:
    """Return ``name`` if it is usable as a single output filename.

    Result dicts name the files a client writes — an entry's ``basename`` becomes
    a document stem, and each key of an image map becomes a file in a directory.
    Both are only ever plain filenames coming from a worker, so anything carrying
    a directory component means the caller is holding a result this code did not
    produce, and guessing what they meant is worse than saying so.
    """
    if not name or name in (".", ".."):
        raise ResponseError(f"refusing {what} {name!r}: not a usable filename")
    if name != Path(name).name or "/" in name or "\\" in name:
        raise ResponseError(f"refusing {what} {name!r}: expected a plain filename")
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


def require_http_url(url: str) -> None:
    """Reject a non-HTTP(S) archive URL before fetching it.

    Worker presigned URLs are always HTTPS. Anything else in that field means the
    result did not come from where the caller thinks it did, and ``urlopen``
    would happily read ``file://``.
    """
    if not url.lower().startswith(("http://", "https://")):
        raise ResponseError(f"refusing to fetch {url!r}: expected an http(s) URL")


def download(url: str) -> bytes:
    """Fetch an archive. Network failures arrive as :class:`ResponseError`.

    An expired presigned URL, an endpoint that is refusing, or a stalled read all
    used to surface as urllib exceptions straight past a client's own handler.
    The ordinary case is the expired URL, which is also the one a caller most
    needs to catch.
    """
    require_http_url(url)
    try:
        with urllib.request.urlopen(  # noqa: S310 — scheme checked above
            url, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise ResponseError(f"fetching the archive failed: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ResponseError(f"fetching the archive failed: {e.reason}") from e
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
    except tarfile.TarError as e:
        # A truncated download, or a body that was never a tar. Raised before any
        # of the safety checks below could run, so it used to bypass them and the
        # client's error type together.
        raise ResponseError(f"the archive could not be read: {e}") from e

    with tar_file as tar:
        for member in tar.getmembers():
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
                # Older patch releases have no filter parameter; the checks above
                # already made this safe.
                tar.extractall(destination)  # noqa: S202
        except tarfile.TarError as e:
            # Truncation is only discovered on read for a streamed member.
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
        except (zipfile.BadZipFile, OSError) as e:
            raise ResponseError(f"the archive could not be extracted: {e}") from e


def extract(data: bytes, dest_dir: str | Path) -> Path:
    """Extract a tarball or zip into ``dest_dir``. Returns the directory.

    The container is detected from the bytes rather than from a field on the
    response, because the archive format is a *request* parameter and the response
    is what actually arrived.
    """
    destination = Path(dest_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if data[:4] == b"PK\x03\x04":
        _extract_zip(data, destination)
    else:
        _extract_tar(data, destination)
    return destination
