"""Explaining a job that reported success and carried nothing.

RunPod's gateway caps a response at 20 MB on ``/runsync`` and 10 MB on ``/run``.
Over that it does not report an error: the job comes back ``COMPLETED`` with a
normal ``executionTime`` and no ``output`` key at all, which the SDK surfaces as
``None``. Both consumer clients reported that as a Python type name, sending the reader to
look for a bug in the handler instead of at the size of what they asked for.

The cap was documented in both docs sites the whole time. It was the error that
never mentioned it, at the one moment anyone is reading errors.

Shared because the transports and the cap are the harness's, not any engine's, so
the explanation is the same on both sides of it. Only the name of the heaviest
optional artifact differs, and that is a parameter.
"""

from __future__ import annotations

# Mirrors runpod_doc_worker.transport.response_size.MAX_RESPONSE_MB. Repeated
# rather than imported: this package is a separate distribution that deliberately
# does not depend on the worker half, so that a consumer decoding a tarball does
# not acquire httpx. The worker's tests assert the two agree.
GATEWAY_RESPONSE_CAP_MB = 20


def describe_dropped_response(
    transport: str,
    *,
    bulky_artifact: str | None = None,
) -> str:
    """Why a COMPLETED job carried no output, and every way to get it anyway.

    ``bulky_artifact`` is the consumer's name for the heaviest optional output
    its worker produces -- typically the per-page JSON of blocks and boxes --
    because leaving that out of ``formats`` is usually the smallest change that
    fits under the cap.

    Written for the caller's terminal, so it names the transport they actually
    used: ``tarball_b64`` is the default in both clients and the one most likely
    to pass the cap, and a reader who never chose a transport has no reason to
    know that.
    """
    if transport == "tarball_b64":
        cause = (
            'transport="tarball_b64" ships the whole output directory as one '
            "base64 blob, which is the transport most likely to pass the cap."
        )
    elif transport == "inline":
        detail = bulky_artifact or "the largest format"
        cause = (
            'transport="inline" returns the artifacts as JSON, and '
            f"{detail} is usually the bulk of it."
        )
    elif transport == "s3":
        # Not a size problem, and saying so matters: an s3 response is a presigned
        # URL a few hundred bytes long, so the cap cannot be what dropped it, and
        # the remedies below would be telling the caller to switch to the transport
        # they are already on.
        return (
            "the job reported COMPLETED but carried no output.\n"
            "\n"
            "This was a transport=\"s3\" job, whose response is a "
            "presigned URL rather than the output itself -- so the "
            "response-size cap is not what happened here, whatever the "
            "document weighed.\n"
            "\n"
            "What to check instead:\n"
            "  * the endpoint has the BUCKET_* env vars set, and the worker "
            "was installed with the s3 extra -- the upload imports boto3;\n"
            "  * the worker log for an upload failure, which is where a "
            "bucket rejection or a credentials problem is reported;\n"
            "  * that the job reached a worker at all, rather than being "
            "dropped before one picked it up."
        )
    else:
        cause = f"transport={transport!r} returned no output."

    # Suggesting a transport the caller is already using reads as if the
    # message never looked at the request, so on inline the advice is the
    # formats list by itself.
    prefix = "" if transport == "inline" else 'transport="inline" with '
    formats = (
        f"a formats list that leaves out {bulky_artifact}"
        if bulky_artifact
        else "a shorter formats list"
    )
    drop = f"  * {prefix}{formats};\n"
    return (
        "the job reported COMPLETED but carried no output.\n"
        "\n"
        "The usual cause is RunPod's response-size cap "
        f"({GATEWAY_RESPONSE_CAP_MB} MB on /runsync, "
        f"{GATEWAY_RESPONSE_CAP_MB // 2} MB on /run): the gateway drops an "
        "oversized response and still reports success, without naming size "
        "anywhere in the reply.\n"
        f"\n{cause}\n"
        "\n"
        "Any of these returns the output:\n"
        f"{drop}"
        '  * transport="s3", which returns a presigned URL instead of the bytes '
        "-- needs the BUCKET_* env vars on the endpoint, and the worker "
        "installed with the s3 extra, since the upload imports boto3;\n"
        "  * a bounded start_page / end_page range, the only option that keeps "
        "every format."
    )
