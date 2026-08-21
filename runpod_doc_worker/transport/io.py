"""Input transport + format detection.

Fetches raw bytes from whichever transport the caller used and tells the
caller what kind of file we got. Format-specific preprocessing (e.g.
image → PDF) belongs to the engine, not here.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from pathlib import Path

import httpx

from runpod_doc_worker import config as _config
from runpod_doc_worker.config import DEFAULT_VOLUME_ROOTS  # noqa: F401  (re-export)
from runpod_doc_worker.transport import net as _net


# Base64 is an ASCII format. Accept the whitespace ordinary encoders use for
# wrapping, but remove it in one translation pass rather than splitting into a
# Python object for every caller-controlled chunk.
_BASE64_WHITESPACE = str.maketrans("", "", " \t\r\n\v\f")


# RunPod's gateway caps payloads at 10 MB (/run) and 20 MB (/runsync). The
# 20 MB ceiling is the largest a caller can realistically send inline; the
# handler enforces it defensively but oversized requests are normally
# rejected at the gateway before reaching us. For larger files, use
# file_url or volume_path.
MAX_INLINE_FILE_MB = 20

# Same ceiling expressed in encoded characters, so an oversized payload is
# rejected by measuring the string we were handed instead of after allocating
# the decoded copy of it. Base64 costs 4 characters per 3 bytes; the 5%
# headroom absorbs padding and the line breaks some encoders insert.
#
# Derived at call time rather than at import, so it cannot drift from
# MAX_INLINE_FILE_MB — a monkeypatched or (later) configurable ceiling has to
# move both numbers together or neither.
def max_inline_b64_chars() -> int:
    """The encoded length at which an inline payload is refused.

    Public because a worker asserting that refusal has to build a string that
    exceeds it, and the alternative is copying the arithmetic above into a test
    — where it silently stops describing this function the first time the
    headroom changes.
    """
    return int((MAX_INLINE_FILE_MB * 1024 * 1024) / 3 * 4 * 1.05)

# Cap on file_url downloads. Larger than MAX_INLINE_FILE_MB because URL
# fetches aren't constrained by RunPod's gateway, but still bounded so a
# hostile or misconfigured URL can't OOM the worker.
MAX_URL_FILE_MB = 200

# httpx timeout for the file_url GET. Long enough for slow CDNs / large
# files; short enough that a dead URL doesn't pin a worker indefinitely.
#
# This is an *inactivity* timeout — httpx restarts it on every byte received,
# so on its own it bounds a silent server and nothing else.
URL_FETCH_TIMEOUT_SECONDS = 120.0

# Wall-clock budget for the whole fetch, which is what actually bounds a server
# that keeps the connection alive by trickling. Generous enough that a genuinely
# slow CDN delivering 200 MB still finishes: at this budget the floor is roughly
# 0.7 MB/s sustained.
MAX_URL_FETCH_SECONDS = 300.0

# Magic bytes for the document formats a worker is expected to accept. This
# reports the container the bytes are in, coarsely — enough for a caller who
# base64'd the wrong thing to get a useful message instead of a failure deep
# inside a parser. It is not a content sniffer, and the labels are broader than
# the names suggest: see `detect_format`.
_IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"\xff\xd8\xff",        # JPEG
    b"GIF87a", b"GIF89a",   # GIF
    b"BM",                  # BMP
    b"II*\x00",             # TIFF little-endian
    b"MM\x00*",             # TIFF big-endian
)
_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"

# RIFF is a container, not a format: WebP, WAV and AVI all open with it. Only
# the ones whose fourcc says WEBP are images, so the fourcc is checked rather
# than trusting the container magic — otherwise a WAV file is reported as an
# image and the engine finds out the hard way.
_RIFF_MAGIC = b"RIFF"
_WEBP_FOURCC = b"WEBP"


def detect_format(file_bytes: bytes) -> str:
    """Return one of: "pdf" | "image" | "ooxml" | "unknown".

    The labels are coarse on purpose, and two of them are broader than they
    read:

    * ``"image"`` covers PNG, JPEG, GIF, BMP, TIFF and WebP.
    * ``"ooxml"`` means "a ZIP container" — DOCX, PPTX and XLSX all start with
      the ZIP magic, and so do EPUB, ODT and JAR. Telling them apart means
      reading the archive's content-types, which is the engine's job; this
      only says the bytes are a ZIP, not that they are Office XML.

    An engine that cannot accept everything a label covers rejects it itself.
    """
    if not file_bytes:
        return "unknown"
    if file_bytes.startswith(_PDF_MAGIC):
        return "pdf"
    if any(file_bytes.startswith(m) for m in _IMAGE_MAGIC):
        return "image"
    if file_bytes.startswith(_RIFF_MAGIC) and file_bytes[8:12] == _WEBP_FOURCC:
        return "image"
    if file_bytes.startswith(_ZIP_MAGIC):
        return "ooxml"
    return "unknown"


def volume_roots() -> list[Path]:
    """Return the directories a `volume_path` may resolve inside.

    ``<PREFIX>_VOLUME_ROOTS`` (comma-separated absolute paths) replaces the
    worker's configured roots when set; blank entries are ignored so a trailing
    comma or an accidentally-empty value falls back to the configured list.
    """
    cfg = _config.active()
    raw = cfg.env("VOLUME_ROOTS")
    entries = [e.strip() for e in raw.split(",") if e.strip()]
    return [Path(e) for e in (entries or cfg.volume_roots)]


def resolve_volume_file(volume_path: str) -> Path:
    """Resolve a `volume_path` input to the file the worker will read.

    Resolving first (rather than reading the string as given) means a path
    that arrives with `..` segments, a trailing separator, or a symlink in the
    middle is compared in its canonical form — so the path we check is the
    path we open, and a mistyped input reports the root it landed outside of
    instead of quietly reading some unrelated file. Relative paths are
    rejected: `volume_path` has always been documented as absolute, and
    resolving one against the worker's cwd would be a coin flip.
    """
    p = Path(volume_path)
    if not p.is_absolute():
        raise ValueError(f"volume_path must be an absolute path; got {volume_path!r}")

    try:
        resolved = p.resolve()
    except (OSError, RuntimeError):
        raise ValueError(f"volume_path cannot be resolved: {volume_path}") from None
    roots = volume_roots()
    for root in roots:
        try:
            r = root.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved == r or r in resolved.parents:
            break
    else:
        raise ValueError(
            f"volume_path is outside the configured input roots "
            f"({', '.join(str(r) for r in roots)}): {volume_path} "
            f"(set {_config.active().env_name('VOLUME_ROOTS')} to a "
            f"comma-separated list of absolute paths to change them)"
        )

    if not resolved.is_file():
        raise ValueError(f"volume_path not found inside container: {volume_path}")
    return resolved


def telemetry_source_kind(source_label: str) -> str:
    """Return a bounded, non-sensitive input-source label for telemetry."""
    kind = source_label.partition(":")[0]
    return kind if kind in {"url", "b64", "volume"} else "unknown"


async def resolve_input_bytes(job_input: dict) -> tuple[bytes, str]:
    """Return (file_bytes, source_label). Raises ValueError on a bad source.

    Enforces XOR over the three sources as a defensive check — the schema
    validates this too, but this function is safe to use standalone (and the
    test suite calls it directly). Format is auto-detected downstream by
    `detect_format`.
    """
    provided = [k for k in ("file_url", "file_b64", "volume_path") if job_input.get(k)]
    if len(provided) != 1:
        raise ValueError(
            f"must provide exactly one of file_url / file_b64 / volume_path "
            f"(got {provided!r})"
        )

    if file_url := job_input.get("file_url"):
        try:
            return await asyncio.wait_for(
                _fetch_url(file_url), timeout=MAX_URL_FETCH_SECONDS
            )
        except asyncio.TimeoutError:
            # Covers the parts a check between chunks cannot reach: a chain of
            # slow redirects, a connection that stalls before the first byte,
            # and a chunk that never arrives at all.
            raise ValueError(
                f"file_url fetch exceeded the {MAX_URL_FETCH_SECONDS:.0f}s budget"
            ) from None

    if file_b64 := job_input.get("file_b64"):
        if len(file_b64) > max_inline_b64_chars():
            raise ValueError(
                f"inline file too large (encoded length {len(file_b64)} chars); "
                f"use file_url or volume_path for files > {MAX_INLINE_FILE_MB} MB"
            )
        # Whitespace first, then strict decoding. `b64decode` discards
        # characters outside the alphabet by default, so `"!!!!"` decoded to
        # empty bytes and a payload with stray characters decoded to a
        # different document than the caller sent — both reported as a
        # successful fetch, with the engine left to fail confusingly on the
        # result. Validating without stripping first would be its own bug:
        # encoders wrap base64 across lines, and the ceiling above already
        # assumes they do.
        compact = file_b64.translate(_BASE64_WHITESPACE)
        if not compact:
            # Stripping whitespace is what makes this case possible: `""` is
            # falsy and never reaches here, while `"   "` is truthy, survives
            # the source check, and normalises to nothing. Strict decoding
            # accepts an empty string, so this would have been a successful
            # fetch of no document — two spellings of one caller mistake
            # ending differently.
            raise ValueError(
                "file_b64 contains no base64 data (it is empty once whitespace "
                "is removed); send the encoded document body, or use file_url "
                "or volume_path"
            )
        try:
            raw = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(
                f"file_b64 is not valid base64 ({e}); it must be the "
                f"base64-encoded body of the document, and nothing else"
            ) from None
        if len(raw) > MAX_INLINE_FILE_MB * 1024 * 1024:
            raise ValueError(
                f"inline file too large ({len(raw) / 1024 / 1024:.1f} MB); "
                f"use file_url or volume_path for files > {MAX_INLINE_FILE_MB} MB"
            )
        return raw, "b64"

    volume_path = job_input["volume_path"]
    resolved = resolve_volume_file(volume_path)
    # Label keeps the caller's own spelling of the path — it's what they sent
    # and what they'll match against in their own logs.
    return resolved.read_bytes(), f"volume:{volume_path}"


async def _fetch_url(file_url: str) -> tuple[bytes, str]:
    """GET a document, bounded by size and by wall clock.

    The caller wraps this in `asyncio.wait_for`, which is what bounds the
    phases with no loop to check in — connect, redirects, and the wait for a
    chunk that never comes. The in-loop check below is kept because it can say
    how far the download got before it ran out of budget, which a cancelled
    task cannot.
    """
    max_bytes = MAX_URL_FILE_MB * 1024 * 1024
    # httpx's timeout is an inactivity timeout: it resets on every byte
    # that arrives. A server dripping one chunk every 119 seconds never
    # trips it, and never approaches the size cap either, so the download
    # is bounded by nothing without this.
    deadline = time.monotonic() + MAX_URL_FETCH_SECONDS
    # The checked transport is the client's default, and whatever the
    # environment wants proxied is mounted over it. Supplying a transport
    # is what stops httpx reading the proxy environment itself, so that
    # reading is done for it — otherwise an operator whose egress needs a
    # proxy would have every download attempt a direct connection and fail.
    #
    # Requests the environment does not proxy — another scheme, or a
    # NO_PROXY host — fall through the mounts to the default and stay on the
    # checked path. Requests that are proxied open their socket to the
    # proxy, which resolves the document host itself; where those end up is
    # the proxy's to decide and its policy to enforce.
    #
    # The hook runs per request either way, which includes every redirect
    # httpx follows on its own.
    async with httpx.AsyncClient(
        timeout=URL_FETCH_TIMEOUT_SECONDS,
        transport=_net.CheckedTargetTransport(field="file_url"),
        mounts=_net.environment_proxy_mounts(),
        event_hooks={"request": [_net.request_hook]},
    ) as client:
        async with client.stream("GET", file_url, follow_redirects=True) as resp:
            resp.raise_for_status()
            # Pre-check Content-Length when the server provided one so we
            # can fail before pulling bytes. Some CDNs omit it; for those
            # we enforce the cap incrementally below.
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > max_bytes:
                raise ValueError(
                    f"file_url body too large ({int(cl) / 1024 / 1024:.1f} MB); "
                    f"max is {MAX_URL_FILE_MB} MB"
                )
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise ValueError(
                        f"file_url body exceeded {MAX_URL_FILE_MB} MB while streaming"
                    )
                if time.monotonic() > deadline:
                    raise ValueError(
                        f"file_url download exceeded the "
                        f"{MAX_URL_FETCH_SECONDS:.0f}s budget after "
                        f"{len(buf) / 1024 / 1024:.1f} MB; the server is "
                        f"sending too slowly to finish"
                    )
            return bytes(buf), f"url:{file_url}"
