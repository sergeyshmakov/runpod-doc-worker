"""The rejection envelope: every message stays inside `input validation failed: `.

Split from test_coerce.py at the 500-line cap, by concern. That file covers what
each validator accepts and refuses; this one covers a narrower and more slippery
property -- that *no* rejection path can produce an error outside the documented
envelope, whatever the caller sends.

Four separate findings landed here, each a different route out:

* `float()` on a 309-digit integer raises `OverflowError`, which is not a
  `ValueError`.
* The fix's own `len(str(value))` raises past Python's 4300-digit conversion cap.
* `bounded_int` and `one_of` never used that conversion, and formatted the raw
  value themselves.
* `repr` recurses, so an oversized integer nested in a list raised from inside the
  formatter that was supposed to be safe.

Every one was reachable from an ordinary JSON body.
"""

from __future__ import annotations

import pytest

from runpod_doc_worker.contract import coerce


PREFIX = "input validation failed: "


# -----------------------------------------------------------------------------
# Integers outside the float range
# -----------------------------------------------------------------------------
#
# JSON puts no bound on an integer and Python parses arbitrary precision, so a
# caller can send one with 309-plus digits. `float()` then raises OverflowError,
# which is not a ValueError and so escapes every check in this module -- turning
# malformed job input into an internal worker error instead of the documented
# rejection. `math.isfinite` cannot catch it: the conversion raises before there
# is a float to test.

HUGE_INT = 10**400


@pytest.mark.parametrize("validator", ["fraction", "positive_number"])
def test_an_integer_too_large_for_a_float_is_a_validation_error(validator: str) -> None:
    with pytest.raises(ValueError, match=r"^input validation failed: "):
        getattr(coerce, validator)("t", HUGE_INT)


@pytest.mark.parametrize("validator", ["fraction", "positive_number"])
def test_an_oversized_integer_does_not_raise_overflowerror(validator: str) -> None:
    """Asserted separately because OverflowError is not a subclass of ValueError.

    A `pytest.raises(ValueError)` above would report the same failure either way if
    the two were related; they are not, so this pins the distinction that matters
    to a caller -- a 400-error envelope rather than a 500.
    """
    with pytest.raises(Exception) as caught:  # noqa: PT011 - the type is the assertion
        getattr(coerce, validator)("t", HUGE_INT)

    assert not isinstance(caught.value, OverflowError)
    assert isinstance(caught.value, ValueError)


def test_the_oversized_integer_message_names_the_field_and_the_scale() -> None:
    """`{value!r}` on a 400-digit integer would put 400 digits in the response."""
    with pytest.raises(ValueError) as caught:
        coerce.fraction("layout_threshold", HUGE_INT)

    message = str(caught.value)
    assert "layout_threshold" in message
    assert "401-digit" in message
    assert len(message) < 200, "the message embedded the number itself"


@pytest.mark.parametrize("validator", ["fraction", "positive_number"])
def test_a_negative_integer_too_large_for_a_float_is_also_caught(
    validator: str,
) -> None:
    with pytest.raises(ValueError, match=r"^input validation failed: "):
        getattr(coerce, validator)("t", -HUGE_INT)


@pytest.mark.parametrize("validator", ["fraction", "positive_number"])
def test_an_integer_past_the_str_conversion_limit_stays_in_the_contract(
    validator: str,
) -> None:
    """A regression introduced by the first fix for this, and worth its own test.

    Python caps integer-to-string conversion at 4300 digits by default, so the
    error formatter's own `len(str(value))` raised an unprefixed ValueError for
    `10**5000` -- putting the caller straight back outside the contract the
    conversion guard exists to keep them inside.
    """
    with pytest.raises(ValueError) as caught:
        getattr(coerce, validator)("t", 10**5000)

    assert str(caught.value).startswith("input validation failed: ")


def test_the_digit_count_is_reported_without_stringifying_the_value() -> None:
    """Derived from bit_length, so there is no 4300-digit ceiling to trip over."""
    with pytest.raises(ValueError) as caught:
        coerce.fraction("t", 10**5000)

    assert "5001-digit" in str(caught.value)


def test_the_digit_count_is_exact_for_a_representable_boundary() -> None:
    """bit_length * 643 // 2136 + 1 is log10(2) in integer arithmetic.

    Asserted against a value whose digit count is known independently, so a
    sloppier approximation cannot pass.
    """
    with pytest.raises(ValueError) as caught:
        coerce.fraction("t", 10**400)

    assert "401-digit" in str(caught.value)


# -----------------------------------------------------------------------------
# Every rejection message, not just the conversion
# -----------------------------------------------------------------------------
#
# Fixing the conversion in _as_float was not enough. `bounded_int` never calls it,
# and `one_of` formats whatever it was handed, so both still interpolated a raw
# integer -- and past 4300 digits that interpolation raises an unprefixed
# ValueError of its own. Every message that carries a caller value now goes
# through `_describe`.

BEYOND_STR_LIMIT = 10**5000


def test_bounded_int_out_of_range_keeps_the_prefix_for_a_huge_integer() -> None:
    with pytest.raises(ValueError) as caught:
        coerce.bounded_int("n", BEYOND_STR_LIMIT, low=1, high=10)

    assert str(caught.value).startswith(PREFIX)
    assert "5001-digit integer" in str(caught.value)


def test_one_of_keeps_the_prefix_for_a_huge_integer() -> None:
    with pytest.raises(ValueError) as caught:
        coerce.one_of("mode", BEYOND_STR_LIMIT, frozenset({"fast"}), "fast")

    assert str(caught.value).startswith(PREFIX)
    assert "5001-digit integer" in str(caught.value)


@pytest.mark.parametrize(
    "call",
    [
        lambda: coerce.bounded_int("n", BEYOND_STR_LIMIT, low=1, high=10),
        lambda: coerce.one_of("m", BEYOND_STR_LIMIT, frozenset({"a"}), "a"),
        lambda: coerce.fraction("t", BEYOND_STR_LIMIT),
        lambda: coerce.positive_number("t", BEYOND_STR_LIMIT),
    ],
)
def test_no_rejection_path_stringifies_an_unbounded_integer(call) -> None:
    """One test over every entry point, so a new message cannot skip the helper."""
    with pytest.raises(ValueError) as caught:
        call()

    message = str(caught.value)
    assert message.startswith(PREFIX), message[:90]
    assert "Exceeds the limit" not in message


# -----------------------------------------------------------------------------
# Subclasses, where isinstance is not enough
# -----------------------------------------------------------------------------
#
# Every guard in this module gates on `isinstance`, and both `int` and `str` can
# be subclassed with the exact behaviour the guard assumed away. None of these is
# reachable from a JSON body -- json produces exact `int` and `str` -- but these
# are public functions in a shared package, so a consumer can hand them anything.


class _UnhashableStr(str):
    """A string that cannot be a set member. `__hash__ = None` is all it takes."""

    __hash__ = None  # type: ignore[assignment]


class _HashRaisesStr(str):
    def __hash__(self) -> int:
        raise RuntimeError("hash is broken")


class _ReprRaisesInt(int):
    def __repr__(self) -> str:
        raise RuntimeError("int repr is broken")


class _StrRaisesValueError(ValueError):
    """A ValueError whose own __str__ raises. The caller picks the exception type
    that comes out of its __repr__, so it can pick this one."""

    def __str__(self) -> str:
        raise RuntimeError("str of the exception is broken too")


class _ReprRaisesAnUnreadableError:
    def __repr__(self) -> str:
        raise _StrRaisesValueError("x")


@pytest.mark.parametrize(
    "call",
    [
        lambda v: coerce.fraction("t", v),
        lambda v: coerce.positive_number("t", v),
        lambda v: coerce.bounded_int("n", v, low=1, high=10),
        lambda v: coerce.one_of("m", v, frozenset({"a"}), "a"),
    ],
    ids=["fraction", "positive_number", "bounded_int", "one_of"],
)
def test_an_exception_whose_str_also_raises_does_not_escape(call) -> None:
    """The last layer of this onion: reading the discriminator was unguarded.

    `_describe` narrows its ValueError branch on the exception's message, and that
    exception came out of a caller's own `__repr__` -- so the caller chose its type.
    A ValueError subclass whose `__str__` also raises threw that second exception
    straight out of the handler written to stop exactly this leak.
    """
    with pytest.raises(ValueError) as caught:
        call(_ReprRaisesAnUnreadableError())

    assert str(caught.value).startswith(PREFIX)
    assert "_ReprRaisesAnUnreadableError" in str(caught.value)


def test_an_unreadable_discriminator_falls_back_to_the_vaguer_message() -> None:
    """Vaguer, not false: it cannot know whether an integer was involved."""
    with pytest.raises(ValueError) as caught:
        coerce.fraction("t", _ReprRaisesAnUnreadableError())

    assert "could not be rendered" in str(caught.value)
    assert "oversized integer" not in str(caught.value)


class _ReprRaisesUnrelatedValueError:
    def __repr__(self) -> str:
        raise ValueError("something entirely unrelated to integers")


@pytest.mark.parametrize("bad", [_UnhashableStr("nope"), _HashRaisesStr("nope")])
def test_one_of_rejects_a_string_subclass_that_cannot_be_hashed(bad: str) -> None:
    """`isinstance(chosen, str)` passing does not make the set lookup safe.

    A `str` subclass may set `__hash__ = None` or raise from it, so the membership
    test itself throws after the type check has already passed. A membership test
    that cannot be performed is not a match.
    """
    with pytest.raises(ValueError) as caught:
        coerce.one_of("mode", bad, frozenset({"fast"}), "fast")

    assert str(caught.value).startswith(PREFIX)
    assert "mode must be one of" in str(caught.value)


def test_an_int_subclass_with_a_raising_repr_is_still_described() -> None:
    """The integer fast path used to sit outside the try, so this escaped.

    `int` is subclassable, so `isinstance(value, int)` is not a promise that
    `repr(value)` is safe.
    """
    with pytest.raises(ValueError) as caught:
        coerce.bounded_int("n", _ReprRaisesInt(99), low=1, high=10)

    assert str(caught.value).startswith(PREFIX)
    assert "_ReprRaisesInt" in str(caught.value)


def test_an_unrelated_value_error_is_not_blamed_on_an_oversized_integer() -> None:
    """The attribution has to be narrowed, not assumed.

    CPython raises a plain `ValueError` for the int-to-decimal limit with no
    distinct type, so the message is the only discriminator. Before narrowing on
    it, any `ValueError` out of a caller's `__repr__` was reported as "containing
    an oversized integer" -- of an object that is not a container and holds no
    integer.
    """
    with pytest.raises(ValueError) as caught:
        coerce.fraction("t", _ReprRaisesUnrelatedValueError())

    message = str(caught.value)
    assert message.startswith(PREFIX)
    assert "oversized integer" not in message
    assert "could not be rendered" in message


def test_the_oversized_integer_attribution_survives_the_narrowing() -> None:
    """The narrowing must not cost the genuine case its specific message."""
    with pytest.raises(ValueError) as caught:
        coerce.fraction("t", [10**5000])

    assert "list containing an oversized integer" in str(caught.value)


class _ReprRaises:
    """A caller value whose own __repr__ blows up. Not exotic: a pydantic model or
    a numpy array with a broken dtype can do this, and a rejection must not become
    a crash."""

    def __repr__(self) -> str:
        raise RuntimeError("repr is broken")


@pytest.mark.parametrize(
    "container",
    [
        [10**5000],
        (10**5000,),
        {"a": 10**5000},
        [[[10**5000]]],
    ],
    ids=["list", "tuple", "dict-value", "nested-three-deep"],
)
def test_an_oversized_integer_inside_a_container_stays_in_the_contract(
    container: object,
) -> None:
    """The top-level guard does not see a nested one, and `repr` recurses.

    So `fraction("t", [10**5000])` stringified the element and raised the same
    unprefixed ValueError the guard exists to prevent. Catching the conversion
    failure covers any depth and any container without walking the structure --
    which a hand-rolled recursion would get wrong for the next shape.
    """
    with pytest.raises(ValueError) as caught:
        coerce.fraction("t", container)

    message = str(caught.value)
    assert message.startswith(PREFIX), message[:90]
    assert "Exceeds the limit" not in message
    assert "containing an oversized integer" in message


def test_a_value_whose_repr_raises_still_produces_a_rejection() -> None:
    with pytest.raises(ValueError) as caught:
        coerce.bounded_int("n", _ReprRaises(), low=1, high=10)

    assert str(caught.value).startswith(PREFIX)
    assert "_ReprRaises" in str(caught.value)


def test_a_negative_huge_integer_is_described_as_negative() -> None:
    with pytest.raises(ValueError) as caught:
        coerce.bounded_int("n", -BEYOND_STR_LIMIT, low=1, high=10)

    assert "negative 5001-digit integer" in str(caught.value)


def test_an_ordinary_value_is_still_shown_verbatim() -> None:
    """The safe formatter must not degrade the common case into vagueness."""
    with pytest.raises(ValueError) as caught:
        coerce.bounded_int("n", 42, low=1, high=10)
    assert "got 42" in str(caught.value)

    with pytest.raises(ValueError) as caught:
        coerce.one_of("mode", "turbo", frozenset({"fast"}), "fast")
    assert "got 'turbo'" in str(caught.value)


def test_a_very_long_value_is_truncated_rather_than_echoed() -> None:
    """The response has a delivery cap, and the caller already has their input."""
    with pytest.raises(ValueError) as caught:
        coerce.fraction("t", "x" * 5000)

    message = str(caught.value)
    assert len(message) < 300, len(message)
    assert message.endswith("...")


def test_a_large_but_representable_integer_still_works() -> None:
    """The guard must not reject numbers a float can hold."""
    assert coerce.positive_number("r", 10**300) == float(10**300)
