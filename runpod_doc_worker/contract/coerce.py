"""Turning one caller-supplied value into what an engine expects.

Every function here either returns a normalised value or raises ``ValueError``
with a message naming the field. They are about one value at a time, which is
what separates them from a worker's cross-field schema rules — and what makes
them worth testing exhaustively and shareable at all.

Two exceptions to that contract, both in :func:`one_of`, both deliberate:

* A **callable** ``default`` that raises propagates its own exception unchanged.
  When the value is absent, the default's failure *is* the failure, and it names
  the operator's misconfigured variable. Flattening it into a "not one of" message
  would hide which variable was malformed — the bug the callable form exists to
  fix. A consumer wrapping these functions and catching only ``ValueError`` will
  not catch it.
* A **falsey** value is treated as absent and replaced by the default, so ``False``,
  ``0``, ``""`` and ``[]`` select the default rather than being rejected. Note the
  asymmetry this produces: ``True`` is truthy, so it reaches the membership check
  and is refused, while ``False`` is not. Unlike the numeric validators, ``one_of``
  does **not** refuse booleans.

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


# Above this many bits an integer's decimal form is both useless in a message and
# unsafe to produce. 425 bits is about 128 decimal digits, well under the limit.
_MAX_DESCRIBED_BITS = 425

# Longest rendering of any other value an error message will carry.
_MAX_DESCRIBED_CHARS = 120


def _int_digits(value: int) -> int:
    """Decimal digit count, from ``bit_length`` rather than ``str``.

    ``643 / 2136`` is log10(2) to seven places, evaluated in integer arithmetic so
    nothing converts to a float on the way. Exact, cheap, and with no conversion
    limit to trip over — see :func:`_describe`.
    """
    return abs(value).bit_length() * 643 // 2136 + 1


def _describe(value: Any) -> str:
    """A rendering of a caller's value that is safe to put in an error message.

    Two hazards, both reachable from an ordinary JSON body:

    * **A large integer cannot be stringified at all.** Python caps
      integer-to-decimal conversion at 4300 digits by default (CVE-2020-10735), so
      ``f"{value!r}"`` raises a ``ValueError`` of its own — one without this
      module's prefix, which hands the caller a broken-worker error for what is
      only bad input. Every rejection here interpolates the offending value, so
      every one of them was a way out of the contract. Fixing the conversion in
      :func:`_as_float` was not enough: ``bounded_int`` never calls it, and
      ``one_of`` formats whatever it was given.
    * **A very long value bloats the response** with content the caller already
      has, and the response has a delivery cap.
    """
    # The integer branch is inside the `try` because `int` can be subclassed: an
    # `int` subclass with a raising `__repr__` passes the isinstance check and then
    # escapes, which is what happens when the fast path sits outside the guard.
    try:
        if isinstance(value, int) and not isinstance(value, bool):
            if value.bit_length() > _MAX_DESCRIBED_BITS:
                sign = "negative " if value < 0 else ""
                return f"a {sign}{_int_digits(value)}-digit integer"
            return repr(value)
        rendered = repr(value)
    except ValueError as e:
        # The common case is an oversized integer *nested* in a container: the
        # bit_length guard only sees a top-level one, and `repr` recurses, so
        # `[10**5000]` stringifies the element and raises the very error this
        # function exists to prevent. Catching it covers any depth and any
        # container without walking the structure -- which a hand-rolled recursion
        # would get wrong for the next shape nobody thought of.
        #
        # CPython raises a plain `ValueError` for the int-to-decimal limit with no
        # distinct type, so its message is the only discriminator available. Narrow
        # on it rather than assuming: an unrelated `ValueError` out of a caller's
        # own `__repr__` should not be reported as holding an integer it does not
        # hold. If CPython ever rewords this, the fallback is the generic message
        # -- vaguer, but not false, which is the safe direction to be wrong in.
        if "Exceeds the limit" in str(e):
            return f"a {type(value).__name__} containing an oversized integer"
        return f"a {type(value).__name__} that could not be rendered"
    except Exception:  # noqa: BLE001
        # A caller-supplied object whose own `__repr__` raises anything else.
        # Nothing in this module would survive it either, and a rejection must not
        # become a crash.
        return f"a {type(value).__name__} that could not be rendered"
    if len(rendered) > _MAX_DESCRIBED_CHARS:
        return rendered[: _MAX_DESCRIBED_CHARS - 3] + "..."
    return rendered


def _as_float(name: str, value: Any) -> float:
    """``float(value)``, with an out-of-range integer kept inside the contract.

    JSON has no integer bound and Python parses arbitrary precision, so a caller
    can send an integer of 309-plus digits. ``float()`` then raises ``OverflowError``
    -- not ``ValueError`` -- and it escapes past every check in this module, so
    malformed job input surfaces as an internal worker error instead of the
    documented ``input validation failed:`` rejection.

    ``math.isfinite`` cannot catch this: the conversion raises before there is a
    float to test.

    The digit count is derived from ``bit_length`` rather than ``len(str(value))``.
    Python caps integer-to-string conversion at 4300 digits by default (CVE-2020-10735),
    so ``str()`` on a large enough integer raises a ``ValueError`` of its own -- one
    without this module's prefix, which put the caller right back outside the
    contract this function exists to keep them inside. ``bit_length`` is exact,
    cheap, and has no such limit; ``643 / 2136`` is log10(2) to seven places, in
    integer arithmetic so nothing converts to a float on the way.
    """
    try:
        return float(value)
    except OverflowError:
        fail(f"{name} must be within the range of a float; got a {_int_digits(value)}-digit integer")
        raise  # unreachable; `fail` always raises. Present for the type checker.


def fraction(name: str, value: Any) -> float:
    """A score threshold or probability: a real number in (0, 1].

    Bools are refused explicitly. ``isinstance(True, int)`` is True in Python, so
    without the check a field set to ``true`` would normalise to 1.0 and run —
    silently, as a maximally permissive threshold.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(f"{name} must be a number between 0 and 1; got {_describe(value)}")
    number = _as_float(name, value)
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
        fail(f"{name} must be a positive number; got {_describe(value)}")
    number = _as_float(name, value)
    if not math.isfinite(number):
        fail(f"{name} must be a finite number; got {number}")
    if number <= 0:
        fail(f"{name} must be greater than 0; got {number}")
    return number


def bounded_int(name: str, value: Any, *, low: int, high: int) -> int:
    """An integer within an inclusive range. Bools are not integers here."""
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{name} must be an integer; got {_describe(value)}")
    if not low <= value <= high:
        fail(f"{name} must be between {low} and {high}; got {_describe(value)}")
    return value


def one_of(
    field: str,
    value: Any,
    allowed: frozenset[str],
    default: Union[str, Callable[[], str]],
) -> str:
    """A value from a closed set, with the empty case falling back.

    **Absent means falsey**, not just ``None``: ``False``, ``0``, ``""`` and ``[]``
    all select the default instead of being rejected. So unlike the numeric
    validators in this module, this one does not refuse booleans — and it is
    asymmetric about them, since ``True`` reaches the membership check and is
    refused while ``False`` never gets there. A worker that must reject a boolean
    for an enum-shaped field checks the type before calling this.

    **A callable ``default`` that raises propagates**, rather than being turned into
    a ``ValueError`` from this module. See the module docstring.

    ``default`` may be a callable, resolved only when ``value`` is absent. That
    matters wherever resolving the default can itself fail: Python evaluates
    arguments eagerly, so passing ``policy.default_backend()`` ran the
    environment check on every call — and a typo in the operator's env var then
    rejected jobs that had named their choice explicitly and could not have been
    affected by it. Worse, the error told the caller to set the field on the job
    to work around it, which was the one thing that did not help.
    """
    chosen = value if value else (default() if callable(default) else default)
    # The type check comes first and short-circuits, which is most of the point:
    # `allowed` is a set of strings, so a truthy non-string is invalid by
    # definition -- and a list or dict is *unhashable*, so the membership test on
    # its own raised a bare `TypeError` rather than rejecting.
    #
    # The isinstance check alone is not enough, though. `str` can be subclassed,
    # and a subclass may set `__hash__ = None` or raise from it, so a value can
    # pass as a string and still make the set lookup throw. A membership test that
    # cannot be performed is not a match -- the same conclusion, reached without
    # letting the TypeError out.
    try:
        acceptable = isinstance(chosen, str) and chosen in allowed
    except Exception:  # noqa: BLE001
        # Not just `TypeError`. An unhashable subclass raises that, but one whose
        # `__hash__` is defined and *raises* propagates whatever it likes -- and a
        # first pass here caught only TypeError and still let a RuntimeError out.
        # Whatever the membership test raises, it did not establish a match.
        acceptable = False
    if not acceptable:
        fail(f"{field} must be one of {sorted(allowed)}; got {_describe(chosen)}")
    return chosen
