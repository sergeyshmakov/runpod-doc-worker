"""What a response entry lost on its way out, said in the response itself.

Three places in this package answer a broken output directory by dropping the
broken part and keeping the job: an artifact that cannot be read falls back to
its declared default, a collection member that cannot be read is left out of
the map, and an archive member that cannot be safely named is left out of the
archive. Each of those is the right trade — one corrupt image should not cost a
caller the forty good ones and the GPU time that produced them.

The trade only holds if the caller can tell it happened. Until this module they
could not: the substitution was written to a log line and the response said
``ok`` with an empty field, which is indistinguishable from a document that
genuinely had no text on that page. At one document a human reads the log. At a
hundred thousand, nobody joins logs to results per document, so the holes are
invisible until something downstream trips over them — and by then the only
remedy is reprocessing the corpus, because there is no record of *which*
documents were holed.

So every drop goes through :meth:`Report.note`, which writes the log line
*and* records the item. One call, both effects: a future drop site cannot log
without reporting, because logging is not separately reachable from here. The
report is attached to the results entry by
:func:`runpod_doc_worker.transport.package.package_results_entry` under the
:data:`ENTRY_KEY` key, and is absent entirely when nothing was lost — so a
clean job's response is exactly what it was before.

What this is not: a substitute for failing. An artifact a worker cannot produce
a useful response without is declared ``required`` in its manifest, and that
raises. This is for the parts that are worth shipping without.
"""

from __future__ import annotations

from typing import Any

from runpod_doc_worker.obs import logging as _logging


# Key the report appears under on a results entry.
ENTRY_KEY = "degraded"

# Why a file was unusable. Bounded and lower-case-with-underscores because
# these are read by machines as much as by people — a metric label, a filter in
# a log sink, a branch in a caller's retry logic. Adding one is a deliberate
# act; spelling one differently in two places is not something a reader would
# notice.
UNREADABLE = "unreadable"
UNRESOLVABLE = "unresolvable"
OUTSIDE_OUTPUT_DIR = "outside_output_dir"
UNSAFE_NAME = "unsafe_name"

VALID_REASONS = (UNREADABLE, UNRESOLVABLE, OUTSIDE_OUTPUT_DIR, UNSAFE_NAME)

# One message for every drop, with the specifics as fields. A sink can alert on
# this string alone and catch every present and future drop site, which is the
# opposite of what three separately-worded messages give you.
#
# Public, and a contract rather than a detail. A worker documents this string to
# its operators as the thing to alert on, and a log mirror branches on it to
# count degradations without carrying a literal of its own. Changing it would
# silence every one of those without failing anything.
MESSAGE = "response degraded"

# How many items a report will describe. A pathological output directory can
# produce thousands of drops, and a response is not the place to enumerate them
# — but `count` is the true total either way, so a truncated report says how
# much it is not showing rather than looking complete.
MAX_ITEMS = 50


class Report:
    """The drops behind one response entry.

    Deliberately defines no ``__bool__``, so an instance is always truthy. The
    obvious way to write the optional-argument default is ``report or
    Report()``, and a ``__bool__`` meaning "has anything been noted" would make
    that swap a caller's still-empty report for a throwaway — every drop noted
    afterwards landing in the discarded copy, on exactly the responses that
    needed the field. Ask :meth:`entry` and compare against None.
    """

    __slots__ = ("_items", "_count")

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._count = 0

    def note(
        self,
        *,
        reason: str,
        file: str,
        artifact: str | None = None,
        error_type: str | None = None,
    ) -> None:
        """Record one dropped or substituted file, and log it.

        :param reason: One of :data:`VALID_REASONS`.
        :param file: Name of the file, not its path — a response entry is
            something a caller keeps, and the absolute path of a temp
            directory on a worker means nothing to them.
        :param artifact: Manifest key this cost the response, when the drop
            happened while reading one. ``None`` for an archive member, which
            is not resolved through the manifest at all.
        :param error_type: Exception class name, when an exception is what
            revealed the problem.
        """
        if reason not in VALID_REASONS:
            raise ValueError(
                f"reason must be one of {list(VALID_REASONS)}; got {reason!r}"
            )
        self._count += 1
        item: dict[str, Any] = {"artifact": artifact, "file": file, "reason": reason}
        if error_type is not None:
            item["error_type"] = error_type
        if len(self._items) < MAX_ITEMS:
            self._items.append(item)
        _logging.warning(MESSAGE, **item)

    @property
    def count(self) -> int:
        """Everything noted, including what the report does not describe."""
        return self._count

    def entry(self) -> dict[str, Any] | None:
        """The value for :data:`ENTRY_KEY`, or None when nothing was lost.

        ``count`` over ``len(items)`` is how a caller sees that the list is
        truncated; there is no separate flag to miss.
        """
        if not self._count:
            return None
        return {"count": self._count, "items": list(self._items)}


def sink(report: Report | None) -> Report:
    """``report``, or a throwaway one when a caller did not supply it.

    The functions that drop things take an optional report so they stay usable
    on their own, and route through this so the logging happens either way. A
    caller that passes nothing is choosing not to read the record, not turning
    the record off.
    """
    return Report() if report is None else report
