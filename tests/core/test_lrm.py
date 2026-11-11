# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §5: the Largest Remainder Method.

§5 names three properties -- "sum equals ``n`` exactly; invariant to input order; changing
the seed never changes cohort sizes" -- and one rule, "ties broken by NFC-sorted key". All
four are pinned here as properties over generated inputs, and the vector file
``tests/vectors/lrm-vectors.json`` pins a set of splits taken from the shipped
telco-payment-recovery example, including adversarial ties.

``assert_lrm_contract`` is parameterised by the implementation under test:
``test_negative_kernels_streams.py`` points it at a float-accumulating LRM and shows the
contract fires.

Regenerating the vector file: the counts in it are produced by
``flux_engine.core.lrm.largest_remainder`` over the weights the file itself carries -- run
``python - <<'PY'`` with the loop in :func:`test_lrm_vectors_are_reproduced`, writing the
computed counts back. Nothing in the file is typed by hand; a value that moves is a
``kernelVersion`` bump with a ``GOLDEN-CHANGELOG.md`` row.
"""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Callable, Dict, List, Mapping

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError
from flux_engine.core.lrm import WEIGHT_SUM_TOLERANCE, largest_remainder, residual_order

VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "lrm-vectors.json"

LrmImplementation = Callable[[int, Mapping[str, Decimal]], Dict[str, int]]

# world.flux.yml spec.population.lifecycleMix, and the population size it splits.
TELCO_LIFECYCLE_MIX = {
    "new": Decimal("0.15"),
    "active": Decimal("0.55"),
    "atrisk": Decimal("0.20"),
    "churned": Decimal("0.10"),
}
TELCO_POPULATION = 25000

# experiment.flux.yml spec.variants.
TELCO_VARIANTS = {"control": Decimal("0.5"), "fast-recovery": Decimal("0.5")}


# ---------------------------------------------------------------------------
# The contract, written once and pointed at whichever implementation is under test
# ---------------------------------------------------------------------------


def exact_split(n: int, weights: Mapping[str, Decimal]) -> Dict[str, int]:
    """KERNEL-SPEC §5's algorithm, written out on exact rationals.

    An independent statement of the rule rather than a call into the module under test:
    floors of ``n·w_i`` on ``Fraction``, residual units to the largest fractional parts,
    ties by NFC-sorted key. If the implementation and this statement disagree, one of them
    is wrong and the test says which input separates them.
    """
    scaled = {key: n * Fraction(weight) for key, weight in weights.items()}
    floors = {key: int(value) for key, value in scaled.items()}
    residual = n - sum(floors.values())
    order = sorted(
        scaled,
        key=lambda key: (-(scaled[key] - floors[key]), nfc(key), key),
    )
    for key in order[:residual]:
        floors[key] += 1
    return {key: floors[key] for key in weights}


def assert_lrm_contract(
    implementation: LrmImplementation, n: int, weights: Mapping[str, Decimal]
) -> None:
    """Assert every property KERNEL-SPEC §5 states, for one input."""
    counts = implementation(n, weights)

    # Every declared key is present, and only declared keys.
    assert set(counts) == set(weights)
    # Output preserves the input key order.
    assert list(counts) == list(weights)
    # Sum equals n exactly.
    assert sum(counts.values()) == n
    # No negative count.
    assert all(count >= 0 for count in counts.values())
    # Each count is the exact floor or one above it.
    for key, weight in weights.items():
        floor = int(n * Fraction(weight))
        assert counts[key] in (floor, floor + 1), (key, counts[key], floor)
    # The split is the one §5 describes, ties included.
    assert counts == exact_split(n, weights)
    # Invariant to input order.
    reversed_weights = {key: weights[key] for key in reversed(list(weights))}
    assert implementation(n, reversed_weights) == {key: counts[key] for key in reversed_weights}
    # A pure function of its two arguments: no clock, no stream, no ambient state.
    assert implementation(n, weights) == counts


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


@st.composite
def weight_sets(draw: st.DrawFn, min_keys: int = 1, max_keys: int = 8) -> Dict[str, Decimal]:
    """A set of declared shares summing to exactly 1, on a two-decimal grid."""
    count = draw(st.integers(min_value=min_keys, max_value=max_keys))
    keys = draw(
        st.lists(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=6),
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    parts = draw(st.lists(st.integers(min_value=0, max_value=100), min_size=count, max_size=count))
    total = sum(parts)
    assume(total > 0)
    # Renormalise onto a grid of hundredths that sums to exactly 1.
    scaled = [part * 100 // total for part in parts]
    scaled[0] += 100 - sum(scaled)
    assume(all(value >= 0 for value in scaled))
    return {key: Decimal(value) / 100 for key, value in zip(keys, scaled, strict=True)}


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@pytest.mark.property
@settings(suppress_health_check=[HealthCheck.too_slow])
@given(n=st.integers(min_value=1, max_value=100_000), weights=weight_sets())
def test_the_split_satisfies_the_whole_contract(n: int, weights: Dict[str, Decimal]) -> None:
    assert_lrm_contract(largest_remainder, n, weights)


@pytest.mark.property
@given(n=st.integers(min_value=1, max_value=50_000))
def test_the_telco_mix_always_sums_to_the_declared_population(n: int) -> None:
    counts = largest_remainder(n, TELCO_LIFECYCLE_MIX)
    assert sum(counts.values()) == n


@pytest.mark.property
@given(
    n=st.integers(min_value=1, max_value=5000),
    keys=st.lists(
        st.text(alphabet="abcxyz", min_size=1, max_size=3), min_size=2, max_size=6, unique=True
    ),
)
def test_an_exact_tie_is_broken_by_nfc_sorted_key(n: int, keys: List[str]) -> None:
    """Equal shares give equal fractional parts; the earliest NFC keys take the residual."""
    weight = Decimal(1) / Decimal(len(keys))
    assume(weight * len(keys) == 1)  # only the key counts that divide 1 exactly
    weights = {key: weight for key in keys}
    counts = largest_remainder(n, weights)
    residual = n - sum(int(n * Fraction(weight)) for _ in keys)
    winners = sorted(keys, key=lambda key: (nfc(key), key))[:residual]
    for key in keys:
        floor = int(n * Fraction(weight))
        assert counts[key] == floor + (1 if key in winners else 0)


@pytest.mark.property
@given(n=st.integers(min_value=1, max_value=10_000), weights=weight_sets(min_keys=2))
def test_declaration_order_does_not_move_a_single_subject(
    n: int, weights: Dict[str, Decimal]
) -> None:
    forward = largest_remainder(n, weights)
    backward = largest_remainder(n, {key: weights[key] for key in reversed(list(weights))})
    assert forward == {key: backward[key] for key in forward}


@pytest.mark.property
@given(n=st.integers(min_value=1, max_value=10_000))
def test_a_declared_trailing_zero_is_not_significance(n: int) -> None:
    """``0.150`` and ``0.15`` are one number and must produce one split."""
    padded = {key: Decimal(f"{value:.4f}") for key, value in TELCO_LIFECYCLE_MIX.items()}
    assert largest_remainder(n, padded) == largest_remainder(n, TELCO_LIFECYCLE_MIX)


@pytest.mark.unit
def test_the_split_takes_no_seed_and_no_stream() -> None:
    """§5: "changing the seed never changes cohort sizes"."""
    import inspect

    parameters = list(inspect.signature(largest_remainder).parameters)
    assert parameters == ["n", "weights"]
    assert "seed" not in parameters and "stream" not in parameters


@pytest.mark.unit
def test_residual_order_is_the_queue_for_the_residual_units() -> None:
    order = residual_order(25001, TELCO_VARIANTS)
    assert order == ("control", "fast-recovery")
    counts = largest_remainder(25001, TELCO_VARIANTS)
    assert counts["control"] == counts["fast-recovery"] + 1


# ---------------------------------------------------------------------------
# The shipped example
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_telco_lifecycle_mix_divides_its_declared_population_exactly() -> None:
    counts = largest_remainder(TELCO_POPULATION, TELCO_LIFECYCLE_MIX)
    assert sum(counts.values()) == TELCO_POPULATION
    # Every share divides 25000 exactly, so no residual unit is placed at all: each count
    # is the exact product, computed here rather than transcribed.
    for key, weight in TELCO_LIFECYCLE_MIX.items():
        assert counts[key] == TELCO_POPULATION * Fraction(weight)


@pytest.mark.unit
def test_the_experiment_variants_split_an_odd_population_by_the_tie_rule() -> None:
    counts = largest_remainder(TELCO_POPULATION + 1, TELCO_VARIANTS)
    assert sum(counts.values()) == TELCO_POPULATION + 1
    assert counts["control"] - counts["fast-recovery"] == 1


# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------


def load_lrm_vectors() -> List[Dict[str, object]]:
    document = json.loads(VECTORS.read_text())
    vectors = document["vectors"]
    assert isinstance(vectors, list) and vectors
    return vectors


def _vector_weights(vector: Mapping[str, object]) -> Dict[str, Decimal]:
    raw = vector["weights"]
    assert isinstance(raw, dict)
    return {key: Decimal(value) for key, value in raw.items()}


@pytest.mark.golden
@pytest.mark.parametrize("vector", load_lrm_vectors(), ids=lambda v: str(v["name"])[:48])
def test_lrm_vectors_are_reproduced(vector: Dict[str, object]) -> None:
    weights = _vector_weights(vector)
    n = vector["n"]
    assert isinstance(n, int)
    counts = largest_remainder(n, weights)
    assert counts == vector["counts"]
    assert list(counts) == vector["countKeyOrder"]
    assert list(residual_order(n, weights)) == vector["residualOrder"]
    assert sum(counts.values()) == vector["sum"] == n


@pytest.mark.golden
def test_every_vector_also_satisfies_the_contract() -> None:
    for vector in load_lrm_vectors():
        n = vector["n"]
        assert isinstance(n, int)
        assert_lrm_contract(largest_remainder, n, _vector_weights(vector))


@pytest.mark.golden
def test_the_vector_file_covers_the_adversarial_ties() -> None:
    names = [str(vector["name"]) for vector in load_lrm_vectors()]
    assert sum("tie" in name for name in names) >= 4
    assert any("NFC" in name for name in names)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "n, weights",
    [
        (0, TELCO_LIFECYCLE_MIX),
        (-1, TELCO_LIFECYCLE_MIX),
        (10, {}),
        (10, {"a": Decimal("0.5"), "b": Decimal("0.4")}),
        (10, {"a": Decimal("0.5"), "b": Decimal("0.6")}),
        (10, {"a": Decimal("1.5"), "b": Decimal("-0.5")}),
    ],
)
def test_a_declaration_the_method_cannot_split_is_refused(
    n: int, weights: Mapping[str, Decimal]
) -> None:
    with pytest.raises(FluxError) as caught:
        largest_remainder(n, weights)
    assert caught.value.code.code == "FLX-5020"


@pytest.mark.unit
def test_the_declared_tolerance_is_honoured_at_its_edge() -> None:
    """A sum one tolerance away from 1 is accepted; ten tolerances away is not."""
    inside = {"a": Decimal(1) - WEIGHT_SUM_TOLERANCE}
    assert sum(largest_remainder(1000, inside).values()) == 1000
    outside = {"a": Decimal(1) - WEIGHT_SUM_TOLERANCE * 10}
    with pytest.raises(FluxError):
        largest_remainder(1000, outside)


@pytest.mark.unit
def test_two_spellings_of_one_key_stay_two_keys_but_order_by_one_form() -> None:
    """NFC decides the tie-break order; it does not merge two declared keys."""
    composed = "\u00e9"
    decomposed = "e\u0301"
    weights = {decomposed: Decimal("0.5"), "z": Decimal("0.5")}
    counts = largest_remainder(3, weights)
    assert set(counts) == {decomposed, "z"}
    # Raw code points would put the decomposed spelling first; NFC puts 'z' first, so 'z'
    # takes the residual unit.
    assert decomposed < "z" < composed
    assert counts["z"] == 2 and counts[decomposed] == 1


@pytest.mark.unit
def test_counts_are_plain_ints() -> None:
    counts = largest_remainder(TELCO_POPULATION, TELCO_LIFECYCLE_MIX)
    assert all(type(value) is int for value in counts.values())
