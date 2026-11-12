# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The draws' memos and the batched word read move no value.

Each shortcut in :mod:`flux_engine.core.rng` either remembers a result that
is a function of a sampler's parameters alone, or reads the bit generator in
one call instead of many. Every test here sets a shortcut against the
computation it stands in for, on the same stream, and asks for the same
values in the same representation.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Dict, List, Mapping

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.core import rng
from flux_engine.core.errors import FluxError

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The batched read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entropy", [0, 1, 2026, (1 << 255) + 17])
def test_a_batched_read_returns_the_words_single_reads_return(entropy: int) -> None:
    single = rng.DrawStream.from_entropy(entropy)
    batched = rng.DrawStream.from_entropy(entropy)
    expected = [single.next_raw() for _ in range(3 + rng.BOUNDED_UINT_DRAW_BUDGET + 2)]

    words = [batched.next_raw() for _ in range(3)]
    words += batched.next_raw_words(rng.BOUNDED_UINT_DRAW_BUDGET)
    words += [batched.next_raw(), batched.next_raw()]

    assert words == expected
    assert batched.position == single.position == len(expected)
    assert all(type(word) is int and 0 <= word < 1 << rng.WORD_BITS for word in words)


def test_an_empty_batch_reads_nothing() -> None:
    stream = rng.DrawStream.from_entropy(5)
    assert stream.next_raw_words(0) == []
    assert stream.position == 0


def test_a_stream_that_overrides_next_raw_keeps_its_own_words_in_a_batch() -> None:
    class _Counting(rng.DrawStream):
        def __init__(self) -> None:
            super().__init__(rng.DrawStream.from_entropy(1)._generator)
            self._count = 0

        def next_raw(self) -> int:
            self._position += 1
            self._count += 1
            return self._count

    stream = _Counting()
    assert stream.next_raw_words(4) == [1, 2, 3, 4]
    assert stream.position == 4


@pytest.mark.parametrize("n", [1, 2, 3, 7, 1000, (1 << 64) - 1])
def test_bounded_uint_draws_what_single_reads_drew(n: int) -> None:
    def reference(stream: rng.DrawStream) -> int:
        words = [stream.next_raw() for _ in range(rng.BOUNDED_UINT_DRAW_BUDGET)]
        threshold = (1 << rng.WORD_BITS) % n
        for word in words:
            product = word * n
            if (product & ((1 << rng.WORD_BITS) - 1)) >= threshold:
                return product >> rng.WORD_BITS
        return words[-1] % n

    fast = rng.DrawStream.from_entropy(n)
    slow = rng.DrawStream.from_entropy(n)
    assert [rng.bounded_uint(fast, n) for _ in range(25)] == [reference(slow) for _ in range(25)]
    assert fast.position == slow.position


# ---------------------------------------------------------------------------
# The truncated normal's bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["-3", "-3.0", "-3.000", "0", "0.00", "-0.0", "2.5", "-6", "7.25"])
def test_a_remembered_bound_is_exactly_what_its_evaluation_returns(text: str) -> None:
    """Keyed on the representation, so ``-3`` and ``-3.000`` are never confused."""
    bound = Decimal(text)
    expected = rng.normal_cdf(bound, precision=rng._WORKING_PRECISION)
    first = rng._bound_cdf(bound)
    assert first.as_tuple() == expected.as_tuple()
    assert rng._bound_cdf(bound) is first


@pytest.mark.parametrize(
    "mean,stddev,lo,hi,scale",
    [
        ("45.5", "12.25", "0", "120", 2),  # bounds either side of the mean
        ("10", "2", "11", "30", 4),  # the whole interval above the mean: mirrored
        ("0", "1", "-9", "-6.5", 6),  # far in the lower tail: the continued fraction
        ("5", "0.001", "4.9", "5.1", 0),  # bounds far out in units of the spread
    ],
)
def test_a_warm_memo_draws_what_a_cold_one_draws(
    mean: str, stddev: str, lo: str, hi: str, scale: int
) -> None:
    """A cold memo evaluates both bounds on every draw, as the sampler once did."""
    params = dict(mean=Decimal(mean), stddev=Decimal(stddev), lo=Decimal(lo), hi=Decimal(hi))
    warm = rng.DrawStream.from_entropy(7)
    cold = rng.DrawStream.from_entropy(7)
    warm_values = [rng.truncated_normal(warm, scale=scale, **params) for _ in range(60)]
    cold_values = []
    for _ in range(60):
        rng._bound_cdf_of.cache_clear()
        cold_values.append(rng.truncated_normal(cold, scale=scale, **params))
    assert [value.as_tuple() for value in warm_values] == [
        value.as_tuple() for value in cold_values
    ]


# ---------------------------------------------------------------------------
# Categorical thresholds
# ---------------------------------------------------------------------------


def _per_draw_categorical(stream: rng.DrawStream, levels: Mapping[str, Decimal]) -> str:
    """The computation :func:`rng.categorical` ran on every draw before its memo."""
    ordered = rng.nfc_sorted_items(levels)
    draw = rng.uniform53(stream)
    cumulative = Fraction(0)
    for key, weight in ordered[:-1]:
        cumulative += Fraction(weight)
        if draw < int(cumulative * rng.UNIFORM53_DENOMINATOR):
            return key
    return ordered[-1][0]


#: Keys that sort differently raw and under NFC, and two spellings of one word.
_KEYS = ["b", "a", "Z", "\u00e9", "e\u0301", "true", "false", "x" * 40]


@given(
    weights=st.lists(st.integers(min_value=1, max_value=10_000), min_size=1, max_size=8),
    seed=st.integers(min_value=0, max_value=2**32),
)
@settings(max_examples=80, deadline=None)
def test_categorical_draws_what_the_per_draw_computation_drew(
    weights: List[int], seed: int
) -> None:
    total = sum(weights)
    levels: Dict[str, Decimal] = {}
    for key, weight in zip(_KEYS[: len(weights) - 1], weights[:-1], strict=True):
        levels[key] = Decimal(weight) / Decimal(total)
    last_key = _KEYS[len(weights) - 1]
    levels[last_key] = Decimal(1) - sum(levels.values(), Decimal(0))

    fast = rng.DrawStream.from_entropy(seed)
    slow = rng.DrawStream.from_entropy(seed)
    assert [rng.categorical(fast, levels) for _ in range(20)] == [
        _per_draw_categorical(slow, levels) for _ in range(20)
    ]


@pytest.mark.parametrize(
    "levels,message",
    [
        ({"a": Decimal("0.7"), "b": Decimal("-0.1"), "c": Decimal("0.4")}, "negative"),
        ({"a": Decimal("0.5"), "b": Decimal("0.4")}, "further than"),
    ],
)
def test_a_refused_level_set_is_refused_every_time_before_a_word_is_drawn(
    levels: Mapping[str, Decimal], message: str
) -> None:
    for _ in range(2):
        stream = rng.DrawStream.from_entropy(3)
        with pytest.raises(FluxError, match=message):
            rng.categorical(stream, levels)
        assert stream.position == 0


def test_a_threshold_is_a_function_of_the_probabilitys_value() -> None:
    assert rng._probability_threshold(Decimal("0.5")) == 1 << 52
    assert rng._probability_threshold(Decimal("0.50")) == 1 << 52
    assert rng._probability_threshold(Decimal("0.1")) == int(
        Fraction(Decimal("0.1")) * rng.UNIFORM53_DENOMINATOR
    )


# ---------------------------------------------------------------------------
# The shared contexts
# ---------------------------------------------------------------------------


def test_the_shared_contexts_are_never_made_current() -> None:
    """``localcontext`` installs a copy: the shared originals keep their pinned fields."""
    stream = rng.DrawStream.from_entropy(11)
    for _ in range(20):
        rng.truncated_normal(
            stream, mean=Decimal(0), stddev=Decimal(1), lo=Decimal(-2), hi=Decimal(3)
        )
        rng.exponential(stream, rate=Decimal("0.5"))
        rng.poisson(stream, mean=Decimal("4.5"))
    for precision in (rng.KERNEL_DECIMAL_PRECISION, rng._WORKING_PRECISION):
        shared = rng._pinned(precision)
        fresh = rng.kernel_context(precision)
        assert not any(shared.flags.values())
        assert (shared.prec, shared.rounding, shared.Emin, shared.Emax, shared.clamp) == (
            fresh.prec,
            fresh.rounding,
            fresh.Emin,
            fresh.Emax,
            fresh.clamp,
        )
        assert {s for s, on in shared.traps.items() if on} == {
            s for s, on in fresh.traps.items() if on
        }


def test_kernel_context_still_hands_out_a_context_of_ones_own() -> None:
    first = rng.kernel_context()
    first.prec = 3
    assert rng.kernel_context().prec == rng.KERNEL_DECIMAL_PRECISION
    assert rng._pinned(rng.KERNEL_DECIMAL_PRECISION).prec == rng.KERNEL_DECIMAL_PRECISION
