"""The client half: reading a worker's response safely.

Everything else in this package runs *inside* a worker. This subpackage is the
exception — it is for the code that talks *to* one, and it exists because two
consumer repos each carried their own copy of it and drifted apart.

Import-light on purpose: nothing here touches httpx, boto3, or the rest of the
harness, so a client package can depend on it without pulling a worker's
transport stack into an end user's environment.

    from runpod_doc_worker.client import ResponseError, decode_b64, download, extract
"""

from __future__ import annotations

from runpod_doc_worker.client.responses import (
    DOWNLOAD_TIMEOUT_SECONDS,
    ResponseError,
    decode_b64,
    download,
    extract,
    require_http_url,
    safe_output_name,
    within,
)

__all__ = [
    "DOWNLOAD_TIMEOUT_SECONDS",
    "ResponseError",
    "decode_b64",
    "download",
    "extract",
    "require_http_url",
    "safe_output_name",
    "within",
]
