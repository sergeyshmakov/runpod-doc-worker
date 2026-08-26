"""The error type every function in this package raises, and what feeds it.

Separated from the code that raises it so the decompression-error tuple has one
home. It is widened whenever the standard library turns out to report a malformed
archive through a type nothing here listed, and that had happened in three
different places before it was collected into one name.
"""

from __future__ import annotations

import logging
import lzma
import zlib

try:  # pragma: no cover - present from Python 3.14
    from compression.zstd import ZstdError as _ZstdError
except ImportError:  # pragma: no cover - earlier releases
    _ZstdError = None

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
