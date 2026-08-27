"""Refusing a response the gateway would discard, and explaining one it did.

Both halves of the same failure. RunPod caps a response at 20 MB on /runsync and
drops anything over it while still reporting the job COMPLETED with no output --
so a caller gets `None` and no reason. The worker half refuses before sending; the
client half explains when a drop happened anyway, against an older worker or on
/run where the cap is half as high.

Prompted by a real run: a scanned document on the default `tarball_b64` came back
COMPLETED and empty, and both consumer clients reported it as a Python type name.
"""

from __future__ import annotations

import pytest

from runpod_doc_client import responses as client_responses
from runpod_doc_worker.transport import response_size

MB = 1024 * 1024


# -----------------------------------------------------------------------------
# The worker half: is this response deliverable?
# -----------------------------------------------------------------------------


def test_a_small_response_is_fine() -> None:
    assert not response_size.exceeds_gateway_cap(1 * MB, transport="tarball_b64")
    assert not response_size.exceeds_gateway_cap(1 * MB, transport="inline")


def test_a_response_past_the_cap_is_refused() -> None:
    assert response_size.exceeds_gateway_cap(25 * MB, transport="tarball_b64")
    assert response_size.exceeds_gateway_cap(25 * MB, transport="inline")


def test_inline_keeps_more_headroom_than_a_tarball() -> None:
    """The measurements differ in kind, so one margin would be wrong for one of
    them: a base64 string is the payload exactly, while an inline size is a sum of
    markdown and image bytes that ignores JSON overhead and runs low."""
    assert response_size.budget_bytes("inline") < response_size.budget_bytes(
        "tarball_b64"
    )
    assert response_size.budget_bytes("tarball_b64") < response_size.MAX_RESPONSE_MB * MB


def test_s3_is_never_too_large() -> None:
    """It ships a presigned URL, so the response is small whatever the output
    weighed. Refusing an s3 job for size would be refusing the fix for size."""
    assert not response_size.exceeds_gateway_cap(500 * MB, transport="s3")


def test_an_unknown_transport_gets_the_cautious_margin() -> None:
    """A transport added later must not fall through to "no limit"."""
    assert response_size.exceeds_gateway_cap(19 * MB, transport="something-new")


def test_the_refusal_reports_the_measured_size_and_the_limit() -> None:
    """"Too large" without a number leaves the reader to guess by how much."""
    message = response_size.oversized_response_error(
        30 * MB, transport="tarball_b64", bulky_artifact="middle.json"
    )
    assert "30.0 MB" in message
    assert "20 MB on /runsync" in message


def test_the_refusal_names_every_way_out() -> None:
    message = response_size.oversized_response_error(
        30 * MB, transport="inline", bulky_artifact="middle.json"
    )
    assert "middle.json" in message
    assert 'transport="s3"' in message
    assert "BUCKET_" in message, "s3 is useless without saying what it needs"
    assert "start_page" in message


def test_the_refusal_works_without_a_named_artifact() -> None:
    """`bulky_artifact` is per-engine and optional; omitting it must not produce
    "leaves out None"."""
    message = response_size.oversized_response_error(30 * MB, transport="inline")
    assert "None" not in message
    assert "formats list" in message


# -----------------------------------------------------------------------------
# The client half: why did a COMPLETED job carry nothing?
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("transport", ["tarball_b64", "inline", "something-new"])
def test_the_explanation_always_names_the_cap(transport: str) -> None:
    message = client_responses.describe_dropped_response(transport)
    assert "20 MB on /runsync" in message
    assert "10 MB on /run" in message


def test_the_explanation_names_the_transport_that_was_used() -> None:
    """A caller who never chose a transport got `tarball_b64`, and has no reason
    to know it is the one most likely to trip the cap."""
    assert 'transport="tarball_b64"' in client_responses.describe_dropped_response(
        "tarball_b64"
    )


def test_the_advice_does_not_recommend_the_transport_already_in_use() -> None:
    """On inline, suggesting inline reads as if the message never looked at the
    request."""
    message = client_responses.describe_dropped_response(
        "inline", bulky_artifact="middle.json"
    )
    body = message.split("Any of these")[1]
    assert 'transport="inline"' not in body
    assert "leaves out middle.json" in body


def test_an_unknown_transport_does_not_raise() -> None:
    """The per-transport wording is a lookup, and a KeyError here would replace a
    size explanation with a stack trace."""
    message = client_responses.describe_dropped_response("something-new")
    assert "something-new" in message


# -----------------------------------------------------------------------------
# The two halves are separate distributions, so the shared number is duplicated
# -----------------------------------------------------------------------------


def test_both_halves_agree_on_the_cap() -> None:
    """The client package deliberately does not depend on the worker package, so
    the constant exists twice. This is the cheapest thing that keeps it honest --
    without it, raising one and not the other means the worker sends a response
    the client's explanation says should have fitted."""
    assert (
        client_responses.GATEWAY_RESPONSE_CAP_MB == response_size.MAX_RESPONSE_MB
    ), "the response cap has drifted between the worker and client halves"


def test_both_halves_offer_the_same_remedies() -> None:
    """The texts are written for different moments and are allowed to differ in
    wording, but not in what they tell someone to do."""
    refusal = response_size.oversized_response_error(
        30 * MB, transport="tarball_b64", bulky_artifact="middle.json"
    )
    explanation = client_responses.describe_dropped_response(
        "tarball_b64", bulky_artifact="middle.json"
    )
    for remedy in ('transport="s3"', "BUCKET_", "start_page", "middle.json"):
        assert remedy in refusal, f"{remedy} missing from the worker refusal"
        assert remedy in explanation, f"{remedy} missing from the client explanation"
