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

from runpod_doc_worker.client.archives import extract
from runpod_doc_worker.client.errors import ResponseError
from runpod_doc_worker.client.fetch import download, require_fetchable_url
from runpod_doc_worker.client.limits import (
    DOWNLOAD_DEADLINE_SECONDS,
    DOWNLOAD_TIMEOUT_SECONDS,
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_MEMBERS,
    MAX_EXTRACTED_BYTES,
    MAX_METADATA_BYTES,
    MAX_OUTPUT_NAME_BYTES,
)
from runpod_doc_worker.client.names import safe_output_name, within
from runpod_doc_worker.client.payloads import decode_b64

__all__ = [
    "DOWNLOAD_DEADLINE_SECONDS",
    "DOWNLOAD_TIMEOUT_SECONDS",
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCHIVE_MEMBERS",
    "MAX_EXTRACTED_BYTES",
    "MAX_METADATA_BYTES",
    "MAX_OUTPUT_NAME_BYTES",
    "ResponseError",
    "decode_b64",
    "download",
    "extract",
    "require_fetchable_url",
    "safe_output_name",
    "within",
]
