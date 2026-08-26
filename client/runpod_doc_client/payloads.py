"""Decoding a base64 field out of a response, strictly.

Its own module because the rule is one sentence and the reason is several, and
because both consumer clients had this wrong at two call sites each before it was
shared.
"""

from __future__ import annotations

import base64
import binascii
import re

from runpod_doc_client.errors import ResponseError

# Base64 alphabet plus the padding character. Used to report *what* is wrong with
# a payload rather than only that something is.
_B64_ALPHABET = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")


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
