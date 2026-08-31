"""Turning one caller-supplied value into what an engine expects.

Every function here either returns a normalised value or raises ``ValueError``
with a message naming the field. They are about one value at a time, which is
what separates them from a worker's cross-field schema rules — and what makes
them worth testing exhaustively and shareable at all.

``fail`` is the reason this module is in ``contract``. The prefix is part of the
job contract every adopting worker presents: each rejection a caller sees starts
with the same words, so a client can branch on "my request was bad" without
pattern-matching prose. Both workers written before this package existed chose
that same wording independently, and each carried its own copy of the function;
changing it is a ``contract:`` change, not a refactor.

What is deliberately **not** here: validators that need a worker's own schema
constants (a format list, a basename length budget) and validators for concepts
only one engine has (layout class ids, per-class detection thresholds). Those
stay with the worker that defines them. A generic-looking signature over
engine-specific meaning is worse than an honest duplicate.

For the *security* half of basename validation — path separators and traversal,
which no worker should be trusted to have done itself — see
:func:`runpod_doc_worker.contract.paths.check_basename`. The two are
complementary: that one refuses a name that escapes the output directory, these
refuse a value the engine cannot use.
"""

from __future__ import annotations

import math

from typing import Any, Callable, Union


__all__ = [
    "bounded_int",
    "fail",
    "fraction",
    "one_of",
    "positive_number",
]


def fail(msg: str) -> None:
    """Raise the standard input-rejection error.

    Annotated as returning ``None`` because it always raises: call sites read
    ``fail(...)`` as a statement, and typing it as ``NoReturn`` would be more
    honest but makes every caller that follows it with a ``return`` read as
    unreachable to some checkers.
    """
    raise ValueError(f"input validation failed: {msg}")


def fraction(name: str, value: Any) -> float:
    """A score threshold or probability: a real number in (0, 1].

    Bools are refused explicitly. ``isinstance(True, int)`` is True in Python, so
    without the check a field set to ``true`` would normalise to 1.0 and run —
    silently, as a maximally permissive threshold.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(f"{name} must be a number between 0 and 1; got {value!r}")
    number = float(value)
    if not 0.0 < number <= 1.0:
        fail(f"{name} must be greater than 0 and at most 1; got {number}")
    return number


def positive_number(name: str, value: Any) -> float:
    """A finite number above zero.

    The finiteness check is not decoration. ``nan <= 0`` is False, so a bare
    lower-bound test let NaN through, and ``inf <= 0`` is False too — both
    reached the engine as a ratio. :func:`fraction` above rejects NaN already,
    but only by accident of being written as ``not 0.0 < n <= 1.0``, where the
    negation catches the always-False comparison. Two functions in one file
    disagreeing on NaN by accident of phrasing is worth removing rather than
    leaving to luck.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(f"{name} must be a positive number; got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        fail(f"{name} must be a finite number; got {number}")
    if number <= 0:
        fail(f"{name} must be greater than 0; got {number}")
    return number


def bounded_int(name: str, value: Any, *, low: int, high: int) -> int:
    """An integer within an inclusive range. Bools are not integers here."""
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{name} must be an integer; got {value!r}")
    if not low <= value <= high:
        fail(f"{name} must be between {low} and {high}; got {value}")
    return value


def one_of(
    field: str,
    value: Any,
    allowed: frozenset[str],
    default: Union[str, Callable[[], str]],
) -> str:
    """A value from a closed set, with the empty case falling back.

    ``default`` may be a callable, resolved only when ``value`` is absent. That
    matters wherever resolving the default can itself fail: Python evaluates
    arguments eagerly, so passing ``policy.default_backend()`` ran the
    environment check on every call — and a typo in the operator's env var then
    rejected jobs that had named their choice explicitly and could not have been
    affected by it. Worse, the error told the caller to set the field on the job
    to work around it, which was the one thing that did not help.
    """
    chosen = value if value else (default() if callable(default) else default)
    if chosen not in allowed:
        fail(f"{field} must be one of {sorted(allowed)}; got {chosen!r}")
    return chosen
