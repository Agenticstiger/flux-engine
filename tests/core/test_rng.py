# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §4: the draws.

Four normative properties are pinned here as properties rather than as examples:

* **fixed draw count** -- every sampler advances the stream position by exactly the
  documented number of raw words, whatever the parameters and whatever it drew;
* **``truncated_normal`` stays inside ``[lo, hi]``** and is monotone in ``u``;
* **the decimal inverse CDF reproduces known Φ⁻¹ values to 1e-9** -- checked against
  ``statistics.NormalDist``, an independent implementation of the same function that ships
  with the standard library (Wichura's AS241), never against this module's own output;
* **no float, and no numpy scalar, escapes a sampler**.

The helper assertions are parameterised by the implementation under test so that
``test_negative_kernels_streams.py`` can point the same properties at a deliberately broken
kernel and show they fire.
"""

from __future__ import annotations

import math
import statistics
from decimal import Decimal, getcontext, localcontext
from fractions import Fraction
from typing import Callable, Dict, List

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from flux_engine.core import rng
from flux_engine.core.errors import FluxError

# ---------------------------------------------------------------------------
# Fixtures and strategies
# ---------------------------------------------------------------------------

#: An arbitrary but fixed entropy value: these tests exercise the samplers, not the tree,
#: and every one of them must hold for every stream.
_ENTROPY = 0x5EED_0000_0000_0001


def stream(entropy: int = _ENTROPY) -> rng.DrawStream:
    return rng.DrawStream.from_entropy(entropy, label="test")


probabilities = st.decimals(
    min_value=Decimal(0), max_value=Decimal(1), allow_nan=False, allow_infinity=False, places=6
)
entropies = st.integers(min_value=0, max_value=2**64 - 1)


# ---------------------------------------------------------------------------
# The bit source
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_next_raw_returns_a_python_int_in_the_64_bit_domain() -> None:
    word = stream().next_raw()
    assert type(word) is int  # not numpy.uint64: the kernel never sees a numpy scalar
    assert 0 <= word < 2**rng.WORD_BITS


@pytest.mark.unit
def test_position_counts_words_consumed() -> None:
    source = stream()
    assert source.position == 0
    for expected in range(1, 6):
        source.next_raw()
        assert source.position == expected


@pytest.mark.unit
def test_two_streams_on_one_entropy_produce_one_word_sequence() -> None:
    first_stream, second_stream = stream(), stream()
    first = [first_stream.next_raw() for _ in range(4)]
    second = [second_stream.next_raw() for _ in range(4)]
    assert first == second
    assert len(set(first)) == 4  # and the stream advances rather than repeating one word


@pytest.mark.property
@given(entropy=entropies)
def test_streams_of_different_entropy_diverge(entropy: int) -> None:
    other = (entropy + 1) % 2**64
    assert stream(entropy).next_raw() != stream(other).next_raw()


@pytest.mark.unit
def test_negative_entropy_is_refused() -> None:
    with pytest.raises(FluxError) as caught:
        rng.DrawStream.from_entropy(-1)
    assert caught.value.code.code == "FLX-5021"


# ---------------------------------------------------------------------------
# Fixed draw count -- KERNEL-SPEC §4
# ---------------------------------------------------------------------------


def assert_word_cost(call: Callable[[rng.DrawStream], object], expected: int) -> None:
    """Assert *call* advances the stream position by exactly *expected* raw words."""
    source = stream()
    before = source.position
    call(source)
    assert source.position - before == expected


@pytest.mark.property
@given(p=probabilities)
def test_bernoulli_costs_one_word_whatever_p(p: Decimal) -> None:
    assert_word_cost(lambda s: rng.bernoulli(s, p), rng.RAW_WORDS_PER_DRAW["bernoulli"])


@pytest.mark.property
@given(n=st.integers(min_value=1, max_value=4096))
def test_bounded_uint_always_consumes_its_whole_budget(n: int) -> None:
    assert_word_cost(lambda s: rng.bounded_uint(s, n), rng.BOUNDED_UINT_DRAW_BUDGET)


@pytest.mark.property
@given(n=st.integers(min_value=0, max_value=24))
def test_permutation_cost_depends_on_length_alone(n: int) -> None:
    assert_word_cost(lambda s: rng.permutation(s, n), rng.permutation_word_cost(n))


@pytest.mark.property
@given(
    mean=st.decimals(min_value=Decimal("-100"), max_value=Decimal("100"), places=2),
    stddev=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("50"), places=2),
)
def test_truncated_normal_costs_one_word(mean: Decimal, stddev: Decimal) -> None:
    assert_word_cost(
        lambda s: rng.truncated_normal(
            s, mean=mean, stddev=stddev, lo=mean - 5 * stddev, hi=mean + 5 * stddev
        ),
        rng.RAW_WORDS_PER_DRAW["truncated_normal"],
    )


@pytest.mark.property
@given(mean=st.decimals(min_value=Decimal("0"), max_value=Decimal("500"), places=3))
def test_poisson_costs_one_word_whatever_the_value(mean: Decimal) -> None:
    assert_word_cost(lambda s: rng.poisson(s, mean=mean), rng.RAW_WORDS_PER_DRAW["poisson"])


@pytest.mark.property
@given(rate=st.decimals(min_value=Decimal("0.001"), max_value=Decimal("100"), places=3))
def test_exponential_costs_one_word(rate: Decimal) -> None:
    assert_word_cost(lambda s: rng.exponential(s, rate=rate), rng.RAW_WORDS_PER_DRAW["exponential"])


@pytest.mark.unit
def test_uniform53_and_categorical_cost_one_word() -> None:
    assert_word_cost(rng.uniform53, rng.RAW_WORDS_PER_DRAW["uniform53"])
    levels = {"low": Decimal("0.2"), "medium": Decimal("0.5"), "high": Decimal("0.3")}
    assert_word_cost(lambda s: rng.categorical(s, levels), rng.RAW_WORDS_PER_DRAW["categorical"])


@pytest.mark.unit
def test_the_documented_word_costs_are_the_ones_the_samplers_charge() -> None:
    """The table is the documentation; this pins it to the behaviour."""
    assert rng.RAW_WORDS_PER_DRAW["bounded_uint"] == rng.BOUNDED_UINT_DRAW_BUDGET
    assert rng.permutation_word_cost(1) == 0
    assert rng.permutation_word_cost(2) == rng.BOUNDED_UINT_DRAW_BUDGET
    assert set(rng.RAW_WORDS_PER_DRAW) == {
        "uniform53",
        "bernoulli",
        "categorical",
        "bounded_uint",
        "truncated_normal",
        "exponential",
        "poisson",
    }


# ---------------------------------------------------------------------------
# uniform53 / bernoulli / categorical
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(entropy=entropies)
def test_uniform53_is_inside_its_declared_domain(entropy: int) -> None:
    source = stream(entropy)
    for _ in range(8):
        draw = rng.uniform53(source)
        assert 0 <= draw < rng.UNIFORM53_DENOMINATOR


@pytest.mark.unit
def test_bernoulli_at_the_ends_of_its_domain_never_draws_a_surprise() -> None:
    never = stream()
    always = stream()
    for _ in range(64):
        assert rng.bernoulli(never, Decimal(0)) is False
        assert rng.bernoulli(always, Decimal(1)) is True


@pytest.mark.unit
def test_bernoulli_threshold_is_the_exact_rational_floor() -> None:
    """The float route rounds a third of every two-place probability the other way."""
    p = Decimal("0.13")
    exact = int(Fraction(13, 100) * rng.UNIFORM53_DENOMINATOR)
    assert rng._probability_threshold(p) == exact
    # The same threshold formed in binary floating point is one unit higher, which is one
    # subject in nine quadrillion assigned to the wrong side of a declared rate.
    assert int(float(p) * rng.UNIFORM53_DENOMINATOR) == exact + 1


@pytest.mark.unit
def test_bernoulli_rate_tracks_p() -> None:
    source = stream()
    trials = 20000
    hits = sum(rng.bernoulli(source, Decimal("0.3")) for _ in range(trials))
    assert abs(hits / trials - 0.3) < 0.02


@pytest.mark.unit
def test_bernoulli_refuses_a_probability_outside_the_unit_interval() -> None:
    for bad in (Decimal("-0.001"), Decimal("1.001")):
        with pytest.raises(FluxError) as caught:
            rng.bernoulli(stream(), bad)
        assert caught.value.code.code == "FLX-5021"


@pytest.mark.unit
def test_categorical_samples_the_declared_levels_at_their_declared_rates() -> None:
    # persona.flux.yml: priceSensitivity {low: 0.2, medium: 0.5, high: 0.3}
    levels = {"low": Decimal("0.2"), "medium": Decimal("0.5"), "high": Decimal("0.3")}
    source = stream()
    trials = 20000
    counts: Dict[str, int] = {key: 0 for key in levels}
    for _ in range(trials):
        counts[rng.categorical(source, levels)] += 1
    assert set(counts) == set(levels)
    for key, weight in levels.items():
        assert abs(counts[key] / trials - float(weight)) < 0.02


@pytest.mark.property
@given(entropy=entropies)
def test_categorical_is_total(entropy: int) -> None:
    """Every value uniform53 can return maps to a declared level."""
    levels = {"a": Decimal("0.3333"), "b": Decimal("0.3333"), "c": Decimal("0.3334")}
    source = stream(entropy)
    for _ in range(16):
        assert rng.categorical(source, levels) in levels


@pytest.mark.unit
def test_categorical_orders_levels_by_nfc_key_not_by_insertion() -> None:
    """The same level set declared in two orders draws the same level from one word."""
    forward = {"alpha": Decimal("0.5"), "beta": Decimal("0.3"), "gamma": Decimal("0.2")}
    reverse = {"gamma": Decimal("0.2"), "beta": Decimal("0.3"), "alpha": Decimal("0.5")}
    first, second = stream(), stream()
    for _ in range(64):
        assert rng.categorical(first, forward) == rng.categorical(second, reverse)


@pytest.mark.unit
def test_level_order_follows_the_nfc_form_not_the_raw_code_points() -> None:
    """'z' sorts before the composed e-acute and after the decomposed spelling.

    Ordering by raw code point would put the two spellings of one key on opposite sides of
    'z'; ordering by NFC form puts both on the same side, which is what makes a level set
    address the same cumulative interval however the document spelled it.
    """
    composed = "\u00e9"
    decomposed = "e\u0301"
    assert composed != decomposed
    assert decomposed < "z" < composed  # the raw code-point order, for contrast

    base = {"z": Decimal("0.5")}
    composed_order = [key for key, _ in rng.nfc_sorted_items({**base, composed: Decimal("0.5")})]
    decomposed_order = [
        key for key, _ in rng.nfc_sorted_items({**base, decomposed: Decimal("0.5")})
    ]
    assert composed_order == ["z", composed]
    assert decomposed_order == ["z", decomposed]


@pytest.mark.unit
def test_categorical_refuses_weights_that_do_not_sum_to_one() -> None:
    with pytest.raises(FluxError) as caught:
        rng.categorical(stream(), {"a": Decimal("0.5"), "b": Decimal("0.4")})
    assert caught.value.code.code == "FLX-5020"


@pytest.mark.unit
def test_categorical_refuses_an_empty_or_negative_declaration() -> None:
    with pytest.raises(FluxError):
        rng.categorical(stream(), {})
    with pytest.raises(FluxError):
        rng.categorical(stream(), {"a": Decimal("-0.5"), "b": Decimal("1.5")})


# ---------------------------------------------------------------------------
# bounded_uint / permutation
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(n=st.integers(min_value=1, max_value=1000), entropy=entropies)
def test_bounded_uint_is_inside_its_bound(n: int, entropy: int) -> None:
    source = stream(entropy)
    for _ in range(4):
        assert 0 <= rng.bounded_uint(source, n) < n


@pytest.mark.unit
def test_bounded_uint_covers_its_range_evenly() -> None:
    source = stream()
    counts = [0] * 5
    for _ in range(4000):
        counts[rng.bounded_uint(source, 5)] += 1
    assert min(counts) > 700 and max(counts) < 900


@pytest.mark.unit
def test_bounded_uint_refuses_a_bound_below_one() -> None:
    with pytest.raises(FluxError) as caught:
        rng.bounded_uint(stream(), 0)
    assert caught.value.code.code == "FLX-5021"


@pytest.mark.property
@given(n=st.integers(min_value=0, max_value=32), entropy=entropies)
def test_permutation_is_a_permutation(n: int, entropy: int) -> None:
    result = rng.permutation(stream(entropy), n)
    assert sorted(result) == list(range(n))


@pytest.mark.unit
def test_permutation_of_a_short_sequence_is_reproducible() -> None:
    assert rng.permutation(stream(), 8) == rng.permutation(stream(), 8)


# ---------------------------------------------------------------------------
# Φ and Φ⁻¹ in decimal
# ---------------------------------------------------------------------------

#: Published values of Φ⁻¹ at the quantiles every statistics table carries. The tolerance
#: below (1e-9) uses only digits that are unambiguous in any published table.
_KNOWN_PPF = {
    "0.5": "0",
    "0.9": "1.2815515655446004",
    "0.95": "1.6448536269514722",
    "0.975": "1.9599639845400545",
    "0.99": "2.3263478740408408",
    "0.995": "2.5758293035489004",
}


@pytest.mark.unit
def test_normal_ppf_reproduces_the_published_quantiles() -> None:
    for p, expected in _KNOWN_PPF.items():
        got = rng.normal_ppf(Decimal(p))
        assert abs(got - Decimal(expected)) < Decimal("1e-9"), (p, got)


@pytest.mark.property
@given(p=st.decimals(min_value=Decimal("0.000001"), max_value=Decimal("0.999999"), places=6))
def test_normal_ppf_agrees_with_the_standard_library_to_1e_9(p: Decimal) -> None:
    """``statistics.NormalDist`` is an independent implementation of the same function."""
    assume(Decimal(0) < p < Decimal(1))
    got = rng.normal_ppf(p)
    expected = statistics.NormalDist().inv_cdf(float(p))
    assert abs(float(got) - expected) < 1e-9


@pytest.mark.property
@given(x=st.decimals(min_value=Decimal("-8"), max_value=Decimal("8"), places=6))
def test_normal_cdf_agrees_with_the_standard_library(x: Decimal) -> None:
    got = float(rng.normal_cdf(x))
    expected = 0.5 * math.erfc(-float(x) / math.sqrt(2))
    assert abs(got - expected) <= 1e-12 * max(expected, 1e-300) + 1e-17


@pytest.mark.property
@given(p=st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("0.9999"), places=6))
def test_cdf_and_ppf_are_inverses(p: Decimal) -> None:
    assume(Decimal(0) < p < Decimal(1))
    round_trip = rng.normal_cdf(rng.normal_ppf(p), precision=25)
    assert abs(round_trip - p) < Decimal("1e-20")


@pytest.mark.unit
def test_normal_cdf_keeps_its_digits_in_the_far_lower_tail() -> None:
    """The tail is where an implementation that computed ``1 - Φ(|x|)`` would go flat."""
    tail = rng.normal_cdf(Decimal("-9"))
    assert tail > 0
    assert abs(float(tail) / 1.1285884059538424e-19 - 1) < 1e-12


@pytest.mark.property
@given(
    a=st.decimals(min_value=Decimal("-6"), max_value=Decimal("6"), places=3),
    b=st.decimals(min_value=Decimal("-6"), max_value=Decimal("6"), places=3),
)
def test_normal_cdf_is_monotone(a: Decimal, b: Decimal) -> None:
    assume(a < b)
    assert rng.normal_cdf(a) <= rng.normal_cdf(b)


@pytest.mark.unit
def test_normal_ppf_refuses_a_probability_at_or_outside_the_ends() -> None:
    for bad in (Decimal(0), Decimal(1), Decimal("-0.1"), Decimal("1.1")):
        with pytest.raises(FluxError) as caught:
            rng.normal_ppf(bad)
        assert caught.value.code.code == "FLX-5021"


@pytest.mark.unit
def test_the_evaluation_ignores_a_hostile_ambient_decimal_context() -> None:
    """A caller's context must not be able to move a hashed value."""
    reference = rng.normal_ppf(Decimal("0.975"))
    with localcontext() as ctx:
        ctx.prec = 4
        ctx.rounding = "ROUND_UP"
        assert rng.normal_ppf(Decimal("0.975")) == reference
    assert getcontext().prec != 0  # the ambient context is left as it was found


# ---------------------------------------------------------------------------
# truncated_normal
# ---------------------------------------------------------------------------

# persona.flux.yml: monthlySpend {dist: normal, mean: 42.0, stddev: 11.0, min: 0}
_TELCO_MEAN = Decimal("42.0")
_TELCO_STDDEV = Decimal("11.0")


@pytest.mark.property
@given(
    entropy=entropies,
    lo=st.decimals(min_value=Decimal("-50"), max_value=Decimal("40"), places=2),
    width=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("200"), places=2),
)
def test_truncated_normal_stays_inside_its_declared_interval(
    entropy: int, lo: Decimal, width: Decimal
) -> None:
    hi = lo + width
    source = stream(entropy)
    for _ in range(4):
        value = rng.truncated_normal(source, mean=_TELCO_MEAN, stddev=_TELCO_STDDEV, lo=lo, hi=hi)
        assert lo <= value <= hi


@pytest.mark.property
@given(
    lo=st.decimals(min_value=Decimal("-20"), max_value=Decimal("20"), places=1),
    width=st.decimals(min_value=Decimal("1"), max_value=Decimal("80"), places=1),
    shift=st.decimals(min_value=Decimal("-60"), max_value=Decimal("60"), places=1),
)
def test_truncated_normal_is_monotone_in_u(lo: Decimal, width: Decimal, shift: Decimal) -> None:
    """Two streams whose first word differs give values ordered the same way."""
    hi = lo + width
    previous: List[Decimal] = []
    for word_scale in (0, 1, 2, 3, 4, 5, 6, 7):
        draw = (2**53 - 1) * word_scale // 7
        value = _truncated_normal_at(draw, mean=shift, stddev=Decimal("9"), lo=lo, hi=hi)
        if previous:
            assert value >= previous[-1]
        previous.append(value)
    assert previous[-1] >= previous[0]


def _truncated_normal_at(
    draw: int, *, mean: Decimal, stddev: Decimal, lo: Decimal, hi: Decimal
) -> Decimal:
    """Evaluate ``truncated_normal`` at a chosen ``uniform53`` value.

    Uses a stub stream so the *mapping* can be probed at chosen points; the mapping is what
    monotonicity is a property of, and picking points out of a real stream would test the
    bit generator rather than the inverse CDF.
    """

    class _FixedStream(rng.DrawStream):
        def __init__(self, word: int) -> None:
            super().__init__(rng.DrawStream.from_entropy(1)._generator)
            self._word = word

        def next_raw(self) -> int:
            self._position += 1
            return self._word

    source = _FixedStream(draw << (rng.WORD_BITS - 53))
    return rng.truncated_normal(source, mean=mean, stddev=stddev, lo=lo, hi=hi)


@pytest.mark.unit
def test_truncated_normal_reproduces_the_declared_moments() -> None:
    source = stream()
    values = [
        float(
            rng.truncated_normal(
                source,
                mean=_TELCO_MEAN,
                stddev=_TELCO_STDDEV,
                lo=Decimal("0"),
                hi=Decimal("1000"),
            )
        )
        for _ in range(4000)
    ]
    assert abs(statistics.mean(values) - float(_TELCO_MEAN)) < 0.6
    assert abs(statistics.stdev(values) - float(_TELCO_STDDEV)) < 0.6


@pytest.mark.property
@given(scale=st.integers(min_value=0, max_value=9))
def test_truncated_normal_is_quantised_to_the_declared_scale(scale: int) -> None:
    value = rng.truncated_normal(
        stream(),
        mean=_TELCO_MEAN,
        stddev=_TELCO_STDDEV,
        lo=Decimal("0"),
        hi=Decimal("100"),
        scale=scale,
    )
    assert -value.as_tuple().exponent == scale


@pytest.mark.unit
def test_truncated_normal_handles_the_degenerate_declarations() -> None:
    exact = rng.truncated_normal(
        stream(), mean=Decimal("5"), stddev=Decimal("0"), lo=Decimal("0"), hi=Decimal("10")
    )
    assert exact == Decimal("5.000000")
    pinned = rng.truncated_normal(
        stream(), mean=Decimal("5"), stddev=Decimal("2"), lo=Decimal("3"), hi=Decimal("3")
    )
    assert pinned == Decimal("3.000000")


@pytest.mark.unit
def test_truncated_normal_reaches_the_far_upper_tail_without_losing_its_digits() -> None:
    """An interval entirely above the mean is where the mirroring branch earns its place."""
    values = {
        float(
            rng.truncated_normal(
                stream(entropy),
                mean=Decimal("0"),
                stddev=Decimal("1"),
                lo=Decimal("6"),
                hi=Decimal("9"),
                scale=9,
            )
        )
        for entropy in range(32)
    }
    assert all(6.0 <= value <= 9.0 for value in values)
    assert len(values) > 16  # not collapsed onto one bound by lost precision


@pytest.mark.unit
def test_truncated_normal_refuses_declarations_it_cannot_satisfy() -> None:
    with pytest.raises(FluxError) as inverted:
        rng.truncated_normal(
            stream(), mean=Decimal(0), stddev=Decimal(1), lo=Decimal(5), hi=Decimal(1)
        )
    assert inverted.value.code.code == "FLX-5021"

    with pytest.raises(FluxError) as unsatisfiable:
        rng.truncated_normal(
            stream(),
            mean=Decimal(0),
            stddev=Decimal(1),
            lo=Decimal("0.0000001"),
            hi=Decimal("0.0000002"),
            scale=3,
        )
    assert unsatisfiable.value.code.code == "FLX-5021"

    with pytest.raises(FluxError):
        rng.truncated_normal(
            stream(), mean=Decimal(0), stddev=Decimal("-1"), lo=Decimal(0), hi=Decimal(1)
        )


# ---------------------------------------------------------------------------
# exponential / poisson
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_exponential_reproduces_its_declared_mean() -> None:
    source = stream()
    values = [float(rng.exponential(source, rate=Decimal("2"))) for _ in range(5000)]
    assert all(value >= 0 for value in values)
    assert abs(statistics.mean(values) - 0.5) < 0.03


@pytest.mark.unit
def test_exponential_refuses_a_non_positive_rate() -> None:
    for bad in (Decimal(0), Decimal("-1")):
        with pytest.raises(FluxError) as caught:
            rng.exponential(stream(), rate=bad)
        assert caught.value.code.code == "FLX-5021"


@pytest.mark.unit
def test_poisson_reproduces_its_declared_mean_and_variance() -> None:
    source = stream()
    values = [rng.poisson(source, mean=Decimal("4")) for _ in range(5000)]
    assert all(isinstance(value, int) and value >= 0 for value in values)
    assert abs(statistics.mean(values) - 4) < 0.15
    assert abs(statistics.variance(values) - 4) < 0.4


@pytest.mark.property
@given(mean=st.decimals(min_value=Decimal("0"), max_value=Decimal("400"), places=2))
def test_poisson_never_exceeds_its_declared_cap(mean: Decimal) -> None:
    assert rng.poisson(stream(), mean=mean) <= rng.poisson_cap(mean)


@pytest.mark.unit
def test_poisson_cap_is_the_documented_formula() -> None:
    for mean in ("0", "1", "4", "250"):
        value = Decimal(mean)
        expected = int(float(value) + 10 * math.sqrt(float(value)) + 10)
        assert rng.poisson_cap(value) == expected


@pytest.mark.unit
def test_poisson_of_a_zero_mean_is_zero() -> None:
    assert rng.poisson(stream(), mean=Decimal(0)) == 0


@pytest.mark.unit
def test_poisson_and_exponential_refuse_a_negative_mean() -> None:
    with pytest.raises(FluxError):
        rng.poisson(stream(), mean=Decimal("-1"))
    with pytest.raises(FluxError):
        rng.poisson_cap(Decimal("-1"))


# ---------------------------------------------------------------------------
# No float leaks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_sampler_returns_a_float_or_a_numpy_scalar() -> None:
    source = stream()
    results = [
        rng.uniform53(source),
        rng.bernoulli(source, Decimal("0.5")),
        rng.categorical(source, {"a": Decimal("0.5"), "b": Decimal("0.5")}),
        rng.bounded_uint(source, 9),
        rng.permutation(source, 3),
        rng.truncated_normal(
            source, mean=Decimal(0), stddev=Decimal(1), lo=Decimal(-3), hi=Decimal(3)
        ),
        rng.exponential(source, rate=Decimal(1)),
        rng.poisson(source, mean=Decimal(2)),
    ]
    for value in results:
        flat = value if not isinstance(value, tuple) else value[0] if value else 0
        assert not isinstance(flat, float)
        assert type(flat).__module__ == "builtins" or isinstance(flat, Decimal)


@pytest.mark.property
@settings(max_examples=25)
@given(entropy=entropies)
def test_a_whole_draw_sequence_is_reproducible_from_its_entropy(entropy: int) -> None:
    def sequence() -> List[object]:
        source = stream(entropy)
        return [
            rng.uniform53(source),
            rng.bernoulli(source, Decimal("0.42")),
            rng.bounded_uint(source, 17),
            rng.truncated_normal(
                source,
                mean=_TELCO_MEAN,
                stddev=_TELCO_STDDEV,
                lo=Decimal(0),
                hi=Decimal(100),
            ),
            rng.poisson(source, mean=Decimal("3.5")),
        ]

    assert sequence() == sequence()


# ---------------------------------------------------------------------------
# The branches a real stream reaches only by accident
# ---------------------------------------------------------------------------


class _ConstantStream(rng.DrawStream):
    """A stream that returns one chosen word forever.

    Used to reach branches whose probability under a real bit source is below 2**-64: a
    documented fallback nothing exercises is a documented fallback nobody has run.
    """

    def __init__(self, word: int) -> None:
        super().__init__(rng.DrawStream.from_entropy(1)._generator)
        self._word = word

    def next_raw(self) -> int:
        self._position += 1
        return self._word


@pytest.mark.unit
def test_bounded_uint_falls_back_when_the_whole_budget_is_rejected() -> None:
    """The documented, effectively unreachable branch of §4's fixed-budget debiasing.

    With ``n = 3`` Lemire's threshold is 1, so a word is rejected only when ``word · 3``
    is zero modulo 2**64 -- that is, only for the word 0. A stream of zeros therefore
    exhausts the whole budget, and the sampler takes the last word modulo ``n`` rather
    than drawing a 65th.
    """
    source = _ConstantStream(0)
    assert rng.bounded_uint(source, 3) == 0
    assert source.position == rng.BOUNDED_UINT_DRAW_BUDGET


@pytest.mark.unit
def test_the_word_budget_is_spent_even_when_the_first_word_is_accepted() -> None:
    source = _ConstantStream(1)
    assert rng.bounded_uint(source, 3) == 0
    assert source.position == rng.BOUNDED_UINT_DRAW_BUDGET


@pytest.mark.unit
def test_a_negative_length_or_scale_is_refused() -> None:
    with pytest.raises(FluxError):
        rng.permutation(stream(), -1)
    with pytest.raises(FluxError):
        rng.permutation_word_cost(-1)
    with pytest.raises(FluxError):
        rng.exponential(stream(), rate=Decimal(1), scale=-1)
    with pytest.raises(FluxError):
        rng.truncated_normal(
            stream(), mean=Decimal(0), stddev=Decimal(1), lo=Decimal(0), hi=Decimal(1), scale=-1
        )


@pytest.mark.unit
def test_a_mean_outside_the_interval_is_clamped_to_the_interval() -> None:
    """Quantisation and clamping happen after the draw, so the bounds always hold."""
    above = rng.truncated_normal(
        stream(), mean=Decimal("100"), stddev=Decimal(0), lo=Decimal(0), hi=Decimal(10)
    )
    below = rng.truncated_normal(
        stream(), mean=Decimal("-100"), stddev=Decimal(0), lo=Decimal(0), hi=Decimal(10)
    )
    assert above == Decimal("10.000000")
    assert below == Decimal("0.000000")


@pytest.mark.unit
def test_an_interval_whose_mass_underflows_returns_a_declared_bound() -> None:
    """Far enough into the tail, Φ underflows to zero -- and the answer is still in range."""
    value = rng.truncated_normal(
        _ConstantStream(0),
        mean=Decimal(0),
        stddev=Decimal(1),
        lo=Decimal("-100000"),
        hi=Decimal("100000"),
    )
    assert value == Decimal("-100000.000000")

    with localcontext(rng.kernel_context(50)):
        # u = 1 is outside what uniform53 can draw; the guard is defensive, and an
        # untested defensive guard is a guess.
        assert rng._standard_truncated(Decimal(1), Decimal(0), Decimal("100000")) == Decimal(
            "100000"
        )
