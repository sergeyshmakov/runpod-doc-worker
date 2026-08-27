"""Refusing a response the gateway would silently discard.

RunPod's gateway caps a response at 20 MB on ``/runsync`` and 10 MB on ``/run``.
Over that it does not return an error: the job is reported ``COMPLETED`` with a
normal ``executionTime`` and the ``output`` key is simply absent, so the SDK hands
the caller ``None`` and nothing in the reply names size as the cause. A worker that
has already spent GPU time producing the output is the last place that knows the
size, which makes it the only place that can say so.

The request direction of the same cap lives in :mod:`runpod_doc_worker.transport.io`
as ``MAX_INLINE_FILE_MB``. This is its mirror.

The number is repeated in ``runpod_doc_client.responses`` because the client half is
a separate distribution that this package does not depend on -- deliberately, so a
consumer unpacking a tarball does not acquire httpx. ``tests/test_response_size.py``
asserts the two stay equal, which is the cheapest way to keep a duplicated constant
honest.
"""

from __future__ import annotations

# The /runsync ceiling. /run is half this; a worker cannot tell which endpoint the
# caller used, so the larger one is the only bound it can enforce without refusing
# responses that would have been delivered.
MAX_RESPONSE_MB = 20


# How much of the cap a measured response is allowed to claim, per transport.
#
# Not one number, because what is unaccounted for differs. For a base64 tarball
# the string *is* the payload, so only the envelope around it -- the debug block, a
# second entry, the results wrapper -- is missing, and 3% covers it. An inline
# response is many text and base64 fields whose JSON escaping `measure_entry_bytes`
# does not model, and one entry is more often joined by others, so 90% leaves room
# for both.
#
# Being wrong in this direction costs a caller a response that would have fitted.
# Being wrong in the other costs them the whole job with no explanation, which is
# the failure this module exists to remove.
_HEADROOM = {
    "tarball_b64": 0.97,
    "inline": 0.90,
}

# s3 ships a presigned URL, so the response is a few hundred bytes whatever the
# output weighed. It is named here so the exemption is a decision rather than a
# gap in a lookup table.
_EXEMPT = frozenset({"s3"})

# The heaviest optional output this worker produces, named in the refusal so it can
# suggest the smallest `formats` change that would fit. Set once by the consumer --
# it is a property of the engine, not of any one job, and threading it through the
# packaging call would put an engine detail in a signature that has none.
BULKY_ARTIFACT: str | None = None

# Whether an undeliverable response is refused. Off by default, so adopting a
# release that contains this does not change what any existing worker returns --
# `package_results_entry` went on returning oversized entries before this existed,
# and a library should not start raising from a version bump.
#
# A consumer turns it on where it configures the rest of its harness settings, and
# should: the alternative is a job the caller sees as COMPLETED and empty. Off, the
# refusal is inert and only the client-side explanation applies.
ENFORCE_RESPONSE_CAP = False


class ResponseTooLargeError(RuntimeError):
    """A packaged response would be discarded by the gateway rather than sent.

    Raised from :func:`runpod_doc_worker.transport.package.package_results_entry`,
    so a handler that already turns an exception into ``ok=false`` reports it to
    the caller with no extra wiring. That is the whole point: the alternative is
    a job the caller sees as ``COMPLETED`` and empty.

    A deployment that genuinely is not behind the cap -- a proxy in front of the
    worker, a caller reading results some other way -- raises
    ``response_size.MAX_RESPONSE_MB`` to disable the refusal.
    """


# What one character costs once serialised. `json.dumps` defaults to
# `ensure_ascii=True`, which is what the SDK returning a job result uses, so the
# body the gateway measures is pure ASCII: every non-ASCII character is written as
# a six-byte `\uXXXX` escape, and one outside the BMP as a twelve-byte surrogate
# pair.
#
# That is the difference between a rounding error and a factor of two on the
# documents this worker exists for. A CJK character is three UTF-8 bytes and six
# serialised; Cyrillic and accented Latin are two and six. Counting UTF-8 bytes
# under-measured a Chinese or Russian parse by more than half, and under-measuring
# is the failure this module exists to prevent -- the response sails past the real
# ceiling and is dropped with no explanation.
#
# Over-measuring costs a caller a response that would have fitted, which arrives as
# a loud error naming the escape hatch. The asymmetry is why this counts the worst
# case rather than the average one.
_SHORT_ESCAPES = frozenset('"' + chr(92) + chr(8) + chr(12) + chr(10) + chr(13) + chr(9))


def _encoded_length(text: str) -> int:
    """The bytes ``text`` occupies inside a JSON string, escaping included.

    Counted in one pass rather than by serialising: `json.dumps` on a 20 MB payload
    allocates a second copy of it, and doing that in order to decide whether the
    response is too large is the wrong trade in a container that has just finished
    a GPU parse.
    """
    total = 0
    for char in text:
        code = ord(char)
        if char in _SHORT_ESCAPES:
            total += 2
        elif code < 0x20:
            total += 6  # \u001f
        elif code < 0x80:
            total += 1
        elif code < 0x10000:
            total += 6  # \uXXXX
        else:
            total += 12  # a surrogate pair, \uXXXX\uXXXX
    return total


def measure_entry_bytes(entry: object) -> int:
    """Roughly the bytes an entry will occupy in the response JSON.

    Walks the structure and sums the payload lengths rather than serialising it.
    ``json.dumps`` on a 20 MB base64 string allocates a second 20 MB string, and
    doing that to *decide whether the response is too big* is the wrong trade in a
    container that has just finished a GPU parse.

    Strings are counted at their *serialised* length, which on the content this
    measures is not close to their byte length: the SDK serialises with
    ``ensure_ascii=True``, so every non-ASCII character becomes a six-byte escape.
    A Chinese or Russian parse is more than twice its UTF-8 size once encoded, and
    counting bytes under-measured exactly the documents this worker is for.

    The per-transport headroom below covers the envelope around the entry, not
    this -- no margin absorbs a factor of two.
    """
    if isinstance(entry, str):
        return _encoded_length(entry)
    if isinstance(entry, dict):
        return sum(
            len(str(key)) + 4 + measure_entry_bytes(value)
            for key, value in entry.items()
        )
    if isinstance(entry, (list, tuple)):
        return sum(measure_entry_bytes(item) + 1 for item in entry)
    # Numbers, booleans, None: a handful of characters each, and never the reason
    # a response is too large.
    return 8


def budget_bytes(transport: str) -> int:
    """The size at which a response of this transport is refused."""
    headroom = _HEADROOM.get(transport, 0.90)
    return int(MAX_RESPONSE_MB * 1024 * 1024 * headroom)


def exceeds_gateway_cap(size_bytes: int, *, transport: str) -> bool:
    """Whether a response this large would be dropped rather than delivered."""
    if transport in _EXEMPT:
        return False
    return size_bytes > budget_bytes(transport)


class ResponseBudget:
    """The running size of one response, across every entry it will carry.

    A per-entry check misses the case it was written for: two entries of 11 MB
    each pass individually and the 22 MB response they form is still discarded.
    The gateway measures the whole body, so a budget has to as well.

    One per job, never module-level: a worker with a concurrency modifier runs
    several jobs in one process, and a shared counter would refuse whichever
    unlucky job happened to arrive after the others.

    ``envelope`` is what the handler adds around the entries -- the debug block,
    timings, the results wrapper itself. It has a default because most of it is
    small and a caller who has not measured theirs should still get a budget that
    errs toward refusing.
    """

    __slots__ = ("_transport", "used")

    def __init__(self, *, transport: str, envelope: int = 8 * 1024) -> None:
        self._transport = transport
        self.used = envelope

    def add(self, entry: dict) -> int:
        """Charge ``entry`` to the budget and return the new total."""
        self.used += measure_entry_bytes(entry)
        return self.used

    def exceeded(self) -> bool:
        return exceeds_gateway_cap(self.used, transport=self._transport)


def refuse_if_undeliverable(
    entry: dict,
    *,
    transport: str,
    budget: ResponseBudget | None = None,
) -> None:
    """Raise :class:`ResponseTooLargeError` if this entry would not be delivered.

    Measurement, policy and message in one call, so the packaging path is a single
    line and this module stays the only place that knows the cap.
    """
    if not ENFORCE_RESPONSE_CAP:
        return
    if budget is not None:
        measured = budget.add(entry)
        over = budget.exceeded()
    else:
        measured = measure_entry_bytes(entry)
        over = exceeds_gateway_cap(measured, transport=transport)
    if over:
        raise ResponseTooLargeError(
            oversized_response_error(
                measured, transport=transport, bulky_artifact=BULKY_ARTIFACT
            )
        )


def oversized_response_error(
    size_bytes: int,
    *,
    transport: str,
    bulky_artifact: str | None = None,
) -> str:
    """Why the response was refused, and every way to get the output anyway.

    Addressed to whoever submitted the job, which is why it reports the measured
    size rather than a rule. An error that says only "too large" leaves the reader
    to guess by how much.

    ``bulky_artifact`` is the caller's name for the heaviest optional output this
    worker produces -- typically the per-page JSON of blocks and boxes, which is
    the bulk of a scanned parse -- since dropping it from ``formats`` is usually
    the smallest change that fits.
    """
    measured = size_bytes / (1024 * 1024)
    limit = budget_bytes(transport) / (1024 * 1024)
    drop = (
        f' * transport="inline" with a formats list that leaves out '
        f"{bulky_artifact};\n"
        if bulky_artifact
        else ' * transport="inline" with a shorter formats list;\n'
    )
    return (
        f"the output is {measured:.1f} MB, past the {limit:.1f} MB this "
        f'transport can return (RunPod caps a response at {MAX_RESPONSE_MB} MB on '
        f"/runsync and half that on /run).\n"
        f"\n"
        f"Refused here rather than sent, because the gateway discards an oversized "
        f"response and still reports the job COMPLETED -- with no output and "
        f"nothing naming size, which is a worse way to find out.\n"
        f"\n"
        f"Any of these returns the output:\n"
        f"{drop}"
        f' * transport="s3", which returns a presigned URL instead of the bytes '
        f"(needs the BUCKET_* env vars on this endpoint, and the worker installed "
        f"with the s3 extra -- the upload imports boto3);\n"
        f" * a bounded start_page / end_page range, the only option that keeps "
        f"every format."
    )
