"""Single-value validators: the bool trap, the NaN trap, and the lazy default.

These are exhaustive on purpose. Each function is a few lines with no
dependencies, which makes the cost of full coverage near zero and the cost of a
gap high -- every one of them sits directly on the path a caller controls.
"""

from __future__ import annotations

import math

import pytest

from runpod_doc_worker.contract import coerce


PREFIX = "input validation failed: "


# -----------------------------------------------------------------------------
# fail
# -----------------------------------------------------------------------------

def test_fail_raises_with_the_contract_prefix() -> None:
    """The prefix is a job contract, not a formatting choice.

    A client branches on it to tell "you sent something invalid" from "the worker
    broke", without matching on prose that may be reworded.
    """
    with pytest.raises(ValueError, match=rf"^{PREFIX}the reason$"):
        coerce.fail("the reason")


def test_fail_always_raises() -> None:
    """Typed as returning None, but no call site should be reachable after it."""
    with pytest.raises(ValueError):
        coerce.fail("x")


# -----------------------------------------------------------------------------
# fraction
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("value", [0.5, 1, 1.0, 0.001, 0.9999])
def test_fraction_accepts_the_open_unit_interval(value: object) -> None:
    assert coerce.fraction("t", value) == float(value)


@pytest.mark.parametrize("value", [0, 0.0, -0.1, 1.1, 2, -1])
def test_fraction_refuses_values_outside_the_interval(value: object) -> None:
    with pytest.raises(ValueError, match="t must be"):
        coerce.fraction("t", value)


@pytest.mark.parametrize("value", [True, False])
def test_fraction_refuses_bools(value: bool) -> None:
    """`isinstance(True, int)` is True, so this needs its own check.

    Without it, `threshold: true` becomes 1.0 -- accepted, and the most permissive
    threshold there is. A caller who typo'd a boolean gets a silently degraded
    parse rather than an error.
    """
    with pytest.raises(ValueError, match="must be a number"):
        coerce.fraction("t", value)


@pytest.mark.parametrize("value", ["0.5", None, [], {}, b"1"])
def test_fraction_refuses_non_numbers(value: object) -> None:
    with pytest.raises(ValueError, match="must be a number"):
        coerce.fraction("t", value)


def test_fraction_refuses_nan() -> None:
    """Only works because the bound is written as a negated comparison.

    `nan <= 1.0` is False, so `not 0.0 < nan <= 1.0` is True and it is rejected.
    Asserted explicitly so a future rewrite to `if n <= 0 or n > 1` -- which reads
    equivalent and lets NaN through -- fails here.
    """
    with pytest.raises(ValueError):
        coerce.fraction("t", float("nan"))


def test_fraction_refuses_infinity() -> None:
    with pytest.raises(ValueError):
        coerce.fraction("t", float("inf"))


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


def test_one_of_does_not_refuse_false_and_the_docs_must_not_claim_it_does() -> None:
    """The asymmetry the reference used to get wrong.

    `True` is truthy, so it reaches the membership check and is refused. `False` is
    falsey, so it is treated as absent and selects the default. Pinned because the
    reference documentation asserted that every validator here refuses booleans,
    which was true of the numeric three and not of this one.
    """
    allowed = frozenset({"fast", "accurate"})

    assert coerce.one_of("mode", False, allowed, "accurate") == "accurate"
    with pytest.raises(ValueError):
        coerce.one_of("mode", True, allowed, "accurate")


def test_a_large_but_representable_integer_still_works() -> None:
    """The guard must not reject numbers a float can hold."""
    assert coerce.positive_number("r", 10**300) == float(10**300)


def test_fraction_returns_a_float_even_for_an_int() -> None:
    """Downstream does arithmetic on it; an int that stays an int floor-divides."""
    result = coerce.fraction("t", 1)
    assert isinstance(result, float)


# -----------------------------------------------------------------------------
# positive_number
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("value", [0.001, 1, 1.5, 1000, 1e9])
def test_positive_number_accepts_finite_positives(value: object) -> None:
    assert coerce.positive_number("r", value) == float(value)


@pytest.mark.parametrize("value", [0, 0.0, -1, -0.001])
def test_positive_number_refuses_zero_and_below(value: object) -> None:
    with pytest.raises(ValueError, match="r must be"):
        coerce.positive_number("r", value)


def test_positive_number_refuses_nan_with_a_message_about_finiteness() -> None:
    """`nan <= 0` is False, so the lower bound alone would pass it through.

    The distinct message matters: "must be a finite number" tells a caller what is
    wrong, where "must be greater than 0" about a NaN reads as a lie.
    """
    with pytest.raises(ValueError, match="must be a finite number"):
        coerce.positive_number("r", float("nan"))


def test_positive_number_refuses_infinity_with_the_same_message() -> None:
    """`inf <= 0` is False too, and inf reached the engine as an unclip ratio."""
    with pytest.raises(ValueError, match="must be a finite number"):
        coerce.positive_number("r", float("inf"))


def test_positive_number_refuses_negative_infinity() -> None:
    with pytest.raises(ValueError, match="must be a finite number"):
        coerce.positive_number("r", float("-inf"))


@pytest.mark.parametrize("value", [True, False])
def test_positive_number_refuses_bools(value: bool) -> None:
    with pytest.raises(ValueError, match="must be a positive number"):
        coerce.positive_number("r", value)


def test_the_two_numeric_validators_agree_on_nan() -> None:
    """The whole reason the finiteness check was made explicit.

    They previously agreed by accident of phrasing. This asserts the agreement
    directly, so a rewrite of either cannot quietly break the pairing.
    """
    for validator in (coerce.fraction, coerce.positive_number):
        with pytest.raises(ValueError):
            validator("f", math.nan)


# -----------------------------------------------------------------------------
# bounded_int
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("value", [1, 5, 10])
def test_bounded_int_accepts_the_inclusive_range(value: int) -> None:
    assert coerce.bounded_int("n", value, low=1, high=10) == value


@pytest.mark.parametrize("value", [0, 11, -1, 100])
def test_bounded_int_refuses_outside_the_range(value: int) -> None:
    with pytest.raises(ValueError, match="n must be between 1 and 10"):
        coerce.bounded_int("n", value, low=1, high=10)


@pytest.mark.parametrize("value", [True, False])
def test_bounded_int_refuses_bools(value: bool) -> None:
    """True would otherwise pass a low=1 bound as the integer 1."""
    with pytest.raises(ValueError, match="must be an integer"):
        coerce.bounded_int("n", value, low=1, high=10)


@pytest.mark.parametrize("value", [1.0, 1.5, "1", None])
def test_bounded_int_refuses_non_integers(value: object) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        coerce.bounded_int("n", value, low=1, high=10)


def test_bounded_int_accepts_a_single_point_range() -> None:
    assert coerce.bounded_int("n", 3, low=3, high=3) == 3


# -----------------------------------------------------------------------------
# one_of
# -----------------------------------------------------------------------------

ALLOWED = frozenset({"fast", "accurate"})


def test_one_of_accepts_a_member() -> None:
    assert coerce.one_of("mode", "fast", ALLOWED, "accurate") == "fast"


def test_one_of_refuses_a_non_member_and_lists_the_options() -> None:
    with pytest.raises(ValueError, match=r"must be one of \['accurate', 'fast'\]"):
        coerce.one_of("mode", "turbo", ALLOWED, "fast")


def test_the_listed_options_are_sorted_rather_than_in_set_order() -> None:
    """The message is part of the error contract, so it has to be stable.

    A frozenset's iteration order is not: it depends on the hashes of its members,
    and str hashing is per-process randomised. Rendering with `list(allowed)` gives
    an error message that differs between two runs of the same worker on the same
    input, which is miserable to match on and looks like a real behaviour change in
    a diff of two logs.

    Enough members that set order is very unlikely to coincide with sorted order --
    with two, `list()` and `sorted()` agree half the time, and this assertion
    passed against `list()` for exactly that reason.
    """
    allowed = frozenset({"zebra", "alpha", "mike", "delta", "quebec", "bravo"})

    with pytest.raises(ValueError) as caught:
        coerce.one_of("mode", "nope", allowed, "alpha")

    listed = str(caught.value).split("must be one of ")[1].split("];")[0] + "]"
    assert listed == str(sorted(allowed))


@pytest.mark.parametrize("absent", [None, "", 0, False, []])
def test_one_of_falls_back_when_the_value_is_absent(absent: object) -> None:
    assert coerce.one_of("mode", absent, ALLOWED, "accurate") == "accurate"


def test_a_callable_default_is_not_resolved_when_the_value_is_present() -> None:
    """The bug this signature exists for.

    An eagerly-evaluated default ran an environment check on every call, so a typo
    in an operator's env var rejected jobs that had named their choice explicitly
    and could not have been affected by it.
    """
    calls = []

    def default() -> str:
        calls.append(1)
        raise AssertionError("the default must not be resolved")

    assert coerce.one_of("mode", "fast", ALLOWED, default) == "fast"
    assert calls == []


def test_a_callable_default_is_resolved_when_the_value_is_absent() -> None:
    calls = []

    def default() -> str:
        calls.append(1)
        return "accurate"

    assert coerce.one_of("mode", None, ALLOWED, default) == "accurate"
    assert calls == [1]


def test_a_callable_default_that_returns_a_non_member_still_fails() -> None:
    """An operator setting a bad default is an error, not a silent pass-through."""
    with pytest.raises(ValueError, match="must be one of"):
        coerce.one_of("mode", None, ALLOWED, lambda: "turbo")


def test_a_callable_default_may_raise_through() -> None:
    """When the value is absent the default's own failure is the real failure.

    It must not be swallowed into a generic "not one of" message that hides which
    env var was malformed.
    """
    with pytest.raises(RuntimeError, match="MODE is malformed"):
        coerce.one_of("mode", None, ALLOWED, lambda: (_ for _ in ()).throw(
            RuntimeError("MODE is malformed")
        ))
