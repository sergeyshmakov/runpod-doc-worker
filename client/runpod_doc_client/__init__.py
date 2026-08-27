"""The client half: reading a worker's response safely.

Everything else in this package runs *inside* a worker. This subpackage is the
exception — it is for the code that talks *to* one, and it exists because two
consumer repos each carried their own copy of it and drifted apart.

Import-light on purpose: nothing here touches httpx, boto3, or the rest of the
harness, so a client package can depend on it without pulling a worker's
transport stack into an end user's environment.

    from runpod_doc_client import ResponseError, decode_b64, download, extract
"""

from __future__ import annotations

from runpod_doc_client.archives import extract
from runpod_doc_client.errors import ResponseError
from runpod_doc_client.fetch import download, require_fetchable_url
# The module, not its values. Exporting the numbers made
# `client.MAX_ARCHIVE_BYTES = bigger` look like the documented way to raise a cap
# while changing nothing: the readers go through `limits`, so the assignment
# landed on this package and the original number stayed in force. A caller who
# followed the public surface got silence instead of a larger allowance.
from runpod_doc_client import limits
from runpod_doc_client.names import safe_output_name, within
from runpod_doc_client.payloads import decode_b64
from runpod_doc_client.responses import (
    GATEWAY_RESPONSE_CAP_MB,
    describe_dropped_response,
)

__all__ = [
    "GATEWAY_RESPONSE_CAP_MB",
    "ResponseError",
    "decode_b64",
    "describe_dropped_response",
    "download",
    "extract",
    "require_fetchable_url",
    "safe_output_name",
    "limits",
    "within",
]
