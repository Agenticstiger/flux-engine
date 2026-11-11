# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §6: the dial partition, its expectation, and its variance."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Dict, List, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.core.errors import FluxError
from flux_engine.core.lrm import largest_remainder
from flux_engine.core.rng import DrawStream
from flux_engine.runtime.temperature import Dial, partition

pytestmark = pytest.mark.unit


def streams(entropy: int = 1) -> Tuple[Dict[int, DrawStream], List[int]]:
    """A stream factory that records which ordinals asked for one."""
    opened: List[int] = []
    made: Dict[int, DrawStream] = {}

    def factory(ordinal: int) -> DrawStream:
        opened.append(ordinal)
        made.setdefault(ordinal, DrawStream.from_entropy(entropy * 1000 + ordinal))
        return made[ordinal]

    return factory, opened  # type: ignore[return-value]


DIALS = [Decimal(0), Decimal("0.25"), Decimal("0.5"), Decimal("0.75"), Decimal(1)]


@pytest.mark.parametrize("t", DIALS, ids=str)
def test_the_two_shares_partition_the_eligible_set(t: Decimal) -> None:
    eligible = tuple(range(37))
    split = partition(eligible, t)
    assert split.frozen + split.stochastic == eligible
    assert split.size == len(eligible)


@pytest.mark.property
@settings(max_examples=100)
@given(
    st.integers(min_value=0, max_value=200),
    st.integers(min_value=0, max_value=100),
)
def test_the_partition_is_the_largest_remainder_of_the_dial(size: int, percent: int) -> None:
    t = Decimal(percent) / Decimal(100)
    eligible = tuple(range(size))
    split = partition(eligible, t)
    if size:
        expected = largest_remainder(size, {"frozen": Decimal(1) - t, "stochastic": t})
        assert len(split.frozen) == expected["frozen"]
    assert split.frozen + split.stochastic == eligible


def test_the_frozen_share_takes_the_first_ordinals_so_growth_is_stable() -> None:
    """Adding subjects at the end must not unfreeze one that was frozen."""
    t = Decimal("0.5")
    small = partition(tuple(range(10)), t)
    larger = partition(tuple(range(20)), t)
    assert set(small.frozen) <= set(larger.frozen)


def test_a_zero_dial_freezes_everything_and_opens_no_stream() -> None:
    factory, opened = streams()
    dial = Dial(name="engagement", value=Decimal(0))
    realisation = dial.realise(tuple(range(10)), Decimal("0.3"), factory)
    assert realisation.partition.stochastic == ()
    assert opened == []
    assert len(realisation.positive) == 3


def test_a_full_dial_draws_for_every_subject() -> None:
    factory, opened = streams()
    dial = Dial(name="engagement", value=Decimal(1))
    realisation = dial.realise(tuple(range(10)), Decimal("0.3"), factory)
    assert realisation.partition.frozen == ()
    assert opened == list(range(10))


def test_the_frozen_share_is_exactly_the_largest_remainder_of_the_rate() -> None:
    factory, _ = streams()
    dial = Dial(name="d", value=Decimal("0.5"))
    realisation = dial.realise(tuple(range(100)), Decimal("0.42"), factory)
    frozen = realisation.partition.frozen
    expected = largest_remainder(
        len(frozen), {"positive": Decimal("0.42"), "negative": Decimal("0.58")}
    )
    assert len(realisation.frozen_positive) == expected["positive"]


def test_the_frozen_positives_are_the_first_frozen_ordinals() -> None:
    factory, _ = streams()
    realisation = Dial(name="d", value=Decimal("0.5")).realise(
        tuple(range(40)), Decimal("0.25"), factory
    )
    frozen = realisation.partition.frozen
    positives = realisation.frozen_positive
    assert positives == frozen[: len(positives)]


RATE = Decimal("0.4")
#: Independent entropies averaged per dial value. One realisation is a
#: sample, not an expectation, and the assertion below is about an
#: expectation.
_TRIALS = 200


def _mean_positive_count(t: Decimal, eligible: Tuple[int, ...], trials: int) -> Fraction:
    """The Monte-Carlo mean of the realised positive count at dial *t*."""
    total = 0
    for entropy in range(1, trials + 1):
        factory, _ = streams(entropy=entropy)
        total += len(Dial(name="d", value=t).realise(eligible, RATE, factory).positive)
    return Fraction(total, trials)


def _expected_positive_count(t: Decimal, eligible: Tuple[int, ...]) -> Fraction:
    """§6's ``E[outcome rate] = p``, in exact rational arithmetic.

    The frozen share contributes its largest-remainder rounding of ``p``
    exactly (zero variance); the stochastic share contributes ``p`` per
    subject in expectation.
    """
    split = partition(eligible, t)
    frozen_positive = (
        largest_remainder(len(split.frozen), {"positive": RATE, "negative": Decimal(1) - RATE})[
            "positive"
        ]
        if split.frozen
        else 0
    )
    return Fraction(frozen_positive) + Fraction(len(split.stochastic)) * Fraction(RATE)


@pytest.mark.parametrize("t", DIALS, ids=str)
def test_the_expected_outcome_rate_is_the_authored_rate_at_every_dial(t: Decimal) -> None:
    """§6: ``E[outcome rate] = p`` for every ``t`` -- estimated, not sampled.

    The previous version of this check drew ONE realisation per dial value
    and allowed a 30%-relative slop (``abs(share - 0.4) < 0.12``). Run
    against a deliberately biased implementation -- ``rate * 1.18`` -- its
    worst deviation across all 101 dial values was 0.11, inside the
    threshold: it passed. It estimated no expectation and so could not
    tell the specified sampler from a materially biased one.
    """
    eligible = tuple(range(400))
    observed = _mean_positive_count(t, eligible, _TRIALS)
    expected = _expected_positive_count(t, eligible)
    # One subject of slack on a 400-subject population: the Monte-Carlo
    # standard error at p=0.4 over 200 trials is ~0.7 subjects.
    assert abs(observed - expected) <= 1, (t, float(observed), float(expected))


def test_a_zero_dial_realises_the_expectation_exactly() -> None:
    """At ``t = 0`` there is no variance, so §6's identity is an equality."""
    eligible = tuple(range(400))
    factory, _ = streams()
    realisation = Dial(name="d", value=Decimal(0)).realise(eligible, RATE, factory)
    expected = largest_remainder(len(eligible), {"positive": RATE, "negative": Decimal(1) - RATE})[
        "positive"
    ]
    assert len(realisation.positive) == expected


def test_a_biased_sampler_fails_the_expectation_check() -> None:
    """The negative control: an 18%-biased rate must be rejected.

    Exactly the bias the single-realisation version let through.
    """
    eligible = tuple(range(400))
    biased = RATE * Decimal("1.18")
    failures = 0
    for t in DIALS:
        total = 0
        for entropy in range(1, _TRIALS + 1):
            factory, _ = streams(entropy=entropy)
            total += len(Dial(name="d", value=t).realise(eligible, biased, factory).positive)
        observed = Fraction(total, _TRIALS)
        if abs(observed - _expected_positive_count(t, eligible)) > 1:
            failures += 1
    assert failures == len(DIALS), "a biased sampler slipped past the expectation check"


def test_variance_is_monotone_in_the_dial() -> None:
    """§6: "variance is monotone in ``t``" -- at ``t = 0`` it is exactly zero."""
    rate = Decimal("0.4")
    eligible = tuple(range(200))
    spreads = []
    for t in (Decimal(0), Decimal("0.5"), Decimal(1)):
        counts = []
        for entropy in range(1, 12):
            factory, _ = streams(entropy=entropy)
            counts.append(len(Dial(name="d", value=t).realise(eligible, rate, factory).positive))
        spreads.append(max(counts) - min(counts))
    assert spreads[0] == 0
    assert spreads[0] <= spreads[1] <= spreads[2]


def test_a_rate_of_zero_and_a_rate_of_one_are_total() -> None:
    factory, _ = streams()
    assert (
        Dial(name="d", value=Decimal(1)).realise(range(20), Decimal(0), factory).positive
        == frozenset()
    )
    factory, _ = streams()
    everyone = Dial(name="d", value=Decimal(1)).realise(range(20), Decimal(1), factory)
    assert len(everyone.positive) == 20


def test_an_empty_eligible_set_realises_nothing() -> None:
    factory, opened = streams()
    realisation = Dial(name="d", value=Decimal("0.5")).realise((), Decimal("0.5"), factory)
    assert realisation.positive == frozenset()
    assert opened == []


def test_the_membership_test_reads_naturally() -> None:
    factory, _ = streams()
    realisation = Dial(name="d", value=Decimal(0)).realise(range(4), Decimal(1), factory)
    assert 0 in realisation and 3 in realisation


def test_an_undeclared_dial_defaults_to_frozen() -> None:
    assert Dial.declared({}, "engagement").value == Decimal(0)
    assert Dial.declared({"engagement": Decimal("0.5")}, "engagement").value == Decimal("0.5")


@pytest.mark.parametrize("bad", [Decimal("-0.1"), Decimal("1.1")])
def test_a_dial_outside_the_unit_interval_is_refused(bad: Decimal) -> None:
    with pytest.raises(FluxError):
        partition(range(4), bad)


def test_a_rate_outside_the_unit_interval_is_refused() -> None:
    factory, _ = streams()
    with pytest.raises(FluxError):
        Dial(name="d", value=Decimal("0.5")).realise(range(4), Decimal("1.5"), factory)


def test_the_partition_never_depends_on_the_seed() -> None:
    """§5's guarantee, inherited: the split is an allocation, not a draw."""
    first, _ = streams(entropy=1)
    second, _ = streams(entropy=99)
    dial = Dial(name="d", value=Decimal("0.5"))
    a = dial.realise(range(50), Decimal("0.3"), first)
    b = dial.realise(range(50), Decimal("0.3"), second)
    assert a.partition == b.partition


def test_the_stochastic_positives_are_a_subset_of_the_stochastic_share() -> None:
    factory, _ = streams()
    realisation = Dial(name="d", value=Decimal("0.5")).realise(range(50), Decimal("0.5"), factory)
    assert set(realisation.stochastic_positive) <= set(realisation.partition.stochastic)
    assert set(realisation.frozen_positive) <= set(realisation.partition.frozen)
