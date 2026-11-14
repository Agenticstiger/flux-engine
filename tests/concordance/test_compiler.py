# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §3: ``H``, ``G``, ``ε``, ``τ′``, and the magnitude bound.

The epsilon and margin assertions here are derived independently, from the
formulas the specification and KERNEL-SPEC §12 state, and compared with what
the compiler produced. A test that only replayed the compiler's own output
would move whenever the compiler moved and would say nothing about whether
either was right.
"""

from __future__ import annotations

import copy
import math
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, Mapping

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.concordance.compiler import (
    EXCLUDED_FIELD_NAMES,
    compile_fidelity,
    parse_exact,
    render_exact,
    seam_document,
    strip_excluded,
)
from flux_engine.concordance.predicate import PropertyKind, ToleranceKind
from flux_engine.core.digest import DigestDomain
from flux_engine.core.errors import FluxError
from flux_engine.core.fixedpoint import DEFAULT_FIELD_CAPACITY_BITS, Grid, magnitude_bits
from tests.concordance.conftest import build_compiled, build_expose

pytestmark = pytest.mark.unit


def _compiled_for(document: Mapping[str, Any]):
    return compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=document["extension"],
        expose=build_expose(document),
    )


# ---------------------------------------------------------------------------
# H and G
# ---------------------------------------------------------------------------


def test_h_is_the_digest_of_the_three_key_seam_document(compiled, document) -> None:
    from flux_engine.core.digest import digest_of_document

    expected = digest_of_document(
        seam_document(
            contract=document["contract"],
            emit=document["emit"],
            columns=document["expose"]["columns"],
            extension=document["extension"],
        ),
        subject="anything",
    )
    assert compiled.h.value == expected.value
    assert compiled.h.domain is DigestDomain.JCS_DOCUMENT


def test_the_published_digests_are_the_ones_the_compiler_takes(compiled, document) -> None:
    assert compiled.h.value == document["expect"]["H"]
    assert compiled.g.value == document["expect"]["G"]


def test_h_and_g_differ(compiled) -> None:
    assert compiled.h.value != compiled.g.value


def test_the_column_order_is_the_declared_one_not_a_sorted_one(document) -> None:
    """KERNEL-SPEC §8 emits arrays aligned to the declared order, so it is hashed."""
    reordered = copy.deepcopy(document)
    reordered["expose"]["columns"] = list(reversed(document["expose"]["columns"]))
    assert _compiled_for(reordered).h.value != _compiled_for(document).h.value


# --- what moves G, and what does not (§8's table) ---------------------------


def _mutated(document: Mapping[str, Any], mutate) -> Dict[str, Any]:
    copied = copy.deepcopy(dict(document))
    mutate(copied["extension"]["fidelity"])
    return copied


def test_changing_a_tolerance_moves_h_and_leaves_g(document, compiled) -> None:
    def mutate(block: Dict[str, Any]) -> None:
        block["properties"][0]["tolerance"]["value"] = "0.2"

    moved = _compiled_for(_mutated(document, mutate))
    assert moved.h.value != compiled.h.value
    assert moved.g.value == compiled.g.value


def test_changing_a_min_variance_moves_h_and_leaves_g(document, compiled) -> None:
    def mutate(block: Dict[str, Any]) -> None:
        for entry in block["properties"]:
            if entry["property"] == PropertyKind.CORRELATION.value:
                entry["minVariance"] = "9"

    moved = _compiled_for(_mutated(document, mutate))
    assert moved.h.value != compiled.h.value
    assert moved.g.value == compiled.g.value


def test_changing_max_state_age_moves_h_and_leaves_g(document, compiled) -> None:
    def mutate(block: Dict[str, Any]) -> None:
        block["budget"]["maxStateAge"] = "P7D"

    moved = _compiled_for(_mutated(document, mutate))
    assert moved.h.value != compiled.h.value
    assert moved.g.value == compiled.g.value


def test_changing_the_check_budget_moves_g(document, compiled) -> None:
    """``maxChecksPerState`` is not on §3's excluded list, so it moves ``G``."""

    def mutate(block: Dict[str, Any]) -> None:
        block["budget"]["maxChecksPerState"] = 99

    moved = _compiled_for(_mutated(document, mutate))
    assert moved.g.value != compiled.g.value


def test_changing_a_declared_range_moves_g(document, compiled) -> None:
    def mutate(block: Dict[str, Any]) -> None:
        block["columns"]["attempts"]["range"]["max"] = "32"

    moved = _compiled_for(_mutated(document, mutate))
    assert moved.g.value != compiled.g.value


def test_changing_the_contract_moves_g(document, compiled) -> None:
    """§8: "any other declaration in the block **or the contract**"."""
    changed = copy.deepcopy(dict(document))
    changed["contract"]["metadata"] = {"owner": {"team": "another"}}
    assert _compiled_for(changed).g.value != compiled.g.value


def test_the_excluded_set_is_the_one_the_spec_names(document) -> None:
    stripped = strip_excluded(document["extension"])
    block = stripped["fidelity"]
    assert "maxStateAge" not in block["budget"]
    assert "maxChecksPerState" in block["budget"]
    for entry in block["properties"]:
        assert not set(entry) & set(EXCLUDED_FIELD_NAMES)
        assert "property" in entry


def test_a_tolerance_outside_the_fidelity_block_is_not_stripped(document) -> None:
    emit = copy.deepcopy(dict(document["emit"]))
    emit["tolerance"] = {"type": "relative", "value": "0.5"}
    assert strip_excluded(emit)["tolerance"] == emit["tolerance"]


def test_an_emit_without_a_fidelity_block_is_refused(document) -> None:
    """The block is looked for in the extension entry, and nowhere else.

    An emit that still carries a ``fidelity`` key of its own -- the
    pre-extension spelling, which FLUX 0.5.0's closed ``spec.emits[]``
    refuses outright -- is not a declaration this compiler reads.
    """
    extension = {key: value for key, value in document["extension"].items() if key != "fidelity"}
    legacy = {**document["emit"], "fidelity": document["extension"]["fidelity"]}
    for entry in (None, {}, extension):
        with pytest.raises(FluxError) as caught:
            compile_fidelity(
                contract=document["contract"],
                emit=legacy,
                extension=entry,
                expose=build_expose(document),
            )
        assert caught.value.code.code == "FLX-5030"


# ---------------------------------------------------------------------------
# epsilon and the margin, derived independently
# ---------------------------------------------------------------------------


def _by_kind(compiled, kind: PropertyKind, *, grouped: bool = False):
    for entry in compiled.properties:
        if entry.declaration.kind is kind and (entry.declaration.group_by is not None) == grouped:
            return entry
    raise AssertionError(f"the published block declares no {kind.value} property")


def test_the_relative_mean_epsilon_is_delta_times_one_plus_tau_over_the_reference(
    compiled, block
) -> None:
    """``ε = δ·(1+τ)/M`` with ``M`` the smallest magnitude the mean can take."""
    entry = _by_kind(compiled, PropertyKind.MEAN)
    assert entry.declaration.tolerance.kind is ToleranceKind.RELATIVE
    delta = block.grid.resolution
    tau = entry.declaration.tolerance.magnitude
    reference = block.column(entry.declaration.columns[0]).interval.min_magnitude
    assert entry.epsilon == delta * (1 + tau) / reference


def test_the_absolute_mean_epsilon_is_one_grid_step(compiled, block) -> None:
    entry = _by_kind(compiled, PropertyKind.MEAN, grouped=True)
    assert entry.declaration.tolerance.kind is ToleranceKind.ABSOLUTE
    assert entry.epsilon == block.grid.resolution


def test_a_histogram_on_grid_edges_carries_no_encoding_error(compiled) -> None:
    assert _by_kind(compiled, PropertyKind.MARGINAL_HISTOGRAM).epsilon == 0


def test_an_interior_bin_edge_off_the_grid_is_refused_at_compile_time(document, expose) -> None:
    """The histogram's epsilon is zero only because every edge is a grid point.

    ``fluxsim validate`` checks that the edges increase and partition the
    declared range (SEAM-FIDELITY-SPEC §2); landing on the grid is what
    makes ``epsilon_marginal_histogram`` zero (KERNEL-SPEC §12), and an
    interior edge that misses it is refused here rather than absorbed
    into a bound nothing justifies. The offending edge is computed as a
    half grid step above one the block already declares.
    """
    raw = copy.deepcopy(dict(document["extension"]))
    bins = raw["fidelity"]["properties"][1]["bins"]
    grid = Grid(fractional_bits=raw["fidelity"]["encoding"]["fractionalBits"])
    interior = Fraction(Decimal(bins[1])) + grid.resolution / 2
    assert not grid.is_on_grid(interior)
    bins[1] = render_exact(interior)
    with pytest.raises(FluxError) as caught:
        compile_fidelity(
            contract=document["contract"],
            emit=document["emit"],
            extension=raw,
            expose=expose,
        )
    assert caught.value.code.code == "FLX-5004"


def test_an_interior_bin_edge_on_the_grid_compiles(document, expose) -> None:
    """The paired positive case: the refusal is about the grid, not the edge."""
    raw = copy.deepcopy(dict(document["extension"]))
    bins = raw["fidelity"]["properties"][1]["bins"]
    grid = Grid(fractional_bits=raw["fidelity"]["encoding"]["fractionalBits"])
    interior = Fraction(Decimal(bins[1])) + grid.resolution
    assert grid.is_on_grid(interior) and interior < Fraction(Decimal(bins[2]))
    bins[1] = render_exact(interior)
    compiled = compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=raw,
        expose=expose,
    )
    assert _by_kind(compiled, PropertyKind.MARGINAL_HISTOGRAM).epsilon == 0


def test_a_support_check_carries_no_encoding_error(compiled) -> None:
    entry = _by_kind(compiled, PropertyKind.SUPPORT)
    assert entry.epsilon == 0
    assert entry.margin is None
    assert entry.max_non_members == int(entry.declaration.tolerance.magnitude)


def test_the_quantile_epsilon_is_one_grid_step(compiled, block) -> None:
    assert _by_kind(compiled, PropertyKind.QUANTILE).epsilon == block.grid.resolution


def test_the_correlation_epsilon_is_eight_delta_over_the_root_of_the_variance_floor(
    compiled, block
) -> None:
    """``ε = 8·δ/sqrt(v)`` with the root replaced by an exact lower bound."""
    entry = _by_kind(compiled, PropertyKind.CORRELATION)
    floor = entry.declaration.min_variance
    assert floor is not None
    # The published floor is a whole perfect square, so the rational lower
    # bound of its root is the root exactly and the expected epsilon can be
    # written down from the formula rather than read off the compiler.
    root = Fraction(math.isqrt(int(floor)))
    assert (
        floor.denominator == 1 and root * root == floor
    ), "this assertion needs a whole perfect-square variance floor"
    assert entry.epsilon == 8 * block.grid.resolution / root


@pytest.mark.parametrize("kind", list(PropertyKind))
def test_every_margin_is_tau_less_epsilon_rounded_down_onto_the_grid(compiled, block, kind) -> None:
    for entry in compiled.properties:
        if entry.declaration.kind is not kind:
            continue
        if entry.margin is None:
            assert kind is PropertyKind.SUPPORT
            continue
        exact = entry.declaration.tolerance.magnitude - entry.epsilon
        assert entry.margin <= exact, "a margin is never looser than tau - epsilon"
        assert block.grid.is_on_grid(entry.margin)
        assert exact - entry.margin < block.grid.resolution


def test_a_value_domain_margin_is_carried_in_grid_steps(compiled, block) -> None:
    for entry in compiled.properties:
        needs_units = entry.declaration.kind is PropertyKind.QUANTILE or (
            entry.declaration.kind is PropertyKind.MEAN
            and entry.declaration.tolerance.kind is ToleranceKind.ABSOLUTE
        )
        if needs_units:
            assert entry.margin is not None
            assert entry.margin_units == block.grid.quantise(entry.margin)
        else:
            assert entry.margin_units is None


@pytest.mark.parametrize(
    "value",
    [
        "0.0001",
        # As long as a number may be (1000 decimals). The refusal prints it, a
        # denominator of 1001 digits, inside CPython's 4300-digit limit on
        # printing an int; at 1E-4300 that print raised a bare ValueError.
        pytest.param("1E-1000", id="the-longest-tolerance-a-block-may-declare"),
    ],
)
def test_an_epsilon_at_or_above_the_tolerance_is_refused_at_compile_time(
    document, value: str
) -> None:
    changed = copy.deepcopy(dict(document))
    for entry in changed["extension"]["fidelity"]["properties"]:
        if entry["property"] == PropertyKind.QUANTILE.value:
            entry["tolerance"] = {"type": "absolute", "value": value}
    with pytest.raises(FluxError) as caught:
        _compiled_for(changed)
    assert caught.value.code.code == "FLX-5002"


def test_a_relative_tolerance_over_a_range_spanning_zero_is_refused(document) -> None:
    """A mean of values in ``[0, hi]`` can be zero, so no relative bound exists."""
    changed = copy.deepcopy(dict(document))
    changed["extension"]["fidelity"]["columns"]["amount"]["range"]["min"] = "0"
    for entry in changed["extension"]["fidelity"]["properties"]:
        if entry["property"] == PropertyKind.MARGINAL_HISTOGRAM.value:
            entry["bins"] = ["0"] + list(entry["bins"])[1:]
    with pytest.raises(FluxError) as caught:
        _compiled_for(changed)
    assert caught.value.code.code == "FLX-5002"
    assert "spans zero" in caught.value.detail


# ---------------------------------------------------------------------------
# the magnitude bound
# ---------------------------------------------------------------------------


def test_only_the_intermediates_a_comparison_forms_are_bounded(compiled) -> None:
    formed = {
        PropertyKind.MEAN: {"value", "sum", "meanComparison"},
        PropertyKind.MARGINAL_HISTOGRAM: {"value", "totalVariationComparison"},
        PropertyKind.SUPPORT: set(),
        PropertyKind.QUANTILE: {"value"},
        PropertyKind.CORRELATION: {
            "value",
            "sum",
            "sumOfProducts",
            "centredSecondMoment",
            "correlationNumerator",
            "correlationComparison",
        },
    }
    for entry in compiled.properties:
        assert set(entry.magnitudes) == formed[entry.declaration.kind]


def test_a_bound_the_field_cannot_hold_is_refused_at_compile_time(document) -> None:
    changed = copy.deepcopy(dict(document))
    with pytest.raises(FluxError) as caught:
        compile_fidelity(
            contract=changed["contract"],
            emit=changed["emit"],
            extension=changed["extension"],
            expose=build_expose(changed),
            capacity_bits=16,
        )
    assert caught.value.code.code == "FLX-5001"


def test_the_widest_bound_is_the_correlation_comparison(compiled) -> None:
    correlation = _by_kind(compiled, PropertyKind.CORRELATION)
    widest = max(correlation.magnitudes.values())
    assert correlation.magnitudes["correlationComparison"] == widest
    assert widest <= DEFAULT_FIELD_CAPACITY_BITS


def test_a_timestamp_column_is_not_refused_for_a_product_it_never_forms(document) -> None:
    """A mean over a timestamp column bounds three intermediates, not eight.

    A timestamp encoded onto the grid is a large integer, and the width of
    the product a *correlation* would form over it exceeds the field
    capacity. A mean never forms that product, so bounding only what the
    comparison forms is what keeps the declaration usable at all.
    """
    changed = copy.deepcopy(dict(document))
    changed["extension"]["fidelity"]["records"]["max"] = 100_000
    changed["extension"]["fidelity"]["properties"] = [
        {
            "property": "mean",
            "column": "aborted_at",
            "tolerance": {"type": "absolute", "value": "60000"},
        }
    ]
    entry = _compiled_for(changed).properties[0]
    assert set(entry.magnitudes) == {"value", "sum", "meanComparison"}
    everything = magnitude_bits(
        grid=Grid(changed["extension"]["fidelity"]["encoding"]["fractionalBits"]),
        value_range=_compiled_for(changed).block.column("aborted_at").interval,
        max_records=changed["extension"]["fidelity"]["records"]["max"],
        tolerance=entry.margin,
    )
    assert everything["correlationComparison"] > DEFAULT_FIELD_CAPACITY_BITS
    assert max(entry.magnitudes.values()) <= DEFAULT_FIELD_CAPACITY_BITS


# ---------------------------------------------------------------------------
# the plan fragment
# ---------------------------------------------------------------------------


def test_the_plan_fragment_carries_h_g_and_every_epsilon(compiled, document) -> None:
    plan = compiled.plan_document()
    assert plan["H"] == compiled.h.value
    assert plan["G"] == compiled.g.value
    assert len(plan["properties"]) == len(compiled.properties)
    for row, entry in zip(plan["properties"], compiled.properties, strict=True):
        assert parse_exact(row["epsilon"]) == entry.epsilon
        if entry.margin is None:
            assert row["margin"] is None
        else:
            assert parse_exact(row["margin"]) == entry.margin
    assert plan == document["expect"]["plan"]


def test_the_plan_fragment_canonicalises(compiled) -> None:
    """It carries no float, so it can be hashed into ``plan.json``."""
    from flux_engine.core.canonical import canonical_bytes

    assert canonical_bytes(compiled.plan_document())


def test_an_unknown_property_id_is_refused(compiled) -> None:
    with pytest.raises(FluxError) as caught:
        compiled.property_by_id("999:mean:nothing")
    assert caught.value.code.code == "FLX-5030"
    assert compiled.property_by_id(compiled.properties[0].property_id) is compiled.properties[0]


# ---------------------------------------------------------------------------
# exact rendering
# ---------------------------------------------------------------------------


@given(
    numerator=st.integers(min_value=-(10**12), max_value=10**12),
    denominator=st.integers(min_value=1, max_value=10**12),
)
@pytest.mark.property
def test_an_exact_rational_round_trips_through_its_rendering(numerator, denominator) -> None:
    value = Fraction(numerator, denominator)
    assert parse_exact(render_exact(value)) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Fraction(1, 2), "0.5"),
        (Fraction(1, 4), "0.25"),
        (Fraction(-1, 8), "-0.125"),
        (Fraction(0), "0"),
        (Fraction(3), "3"),
        (Fraction(1, 3), "1/3"),
        (Fraction(2, 7), "2/7"),
    ],
)
def test_a_terminating_rational_is_spelled_as_a_decimal_and_the_rest_as_a_fraction(
    value, expected
) -> None:
    assert render_exact(value) == expected


def test_a_correlation_epsilon_is_spelled_without_rounding(compiled) -> None:
    """The one epsilon whose denominator is not a product of twos and fives."""
    entry = _by_kind(compiled, PropertyKind.CORRELATION)
    rendered = render_exact(entry.epsilon)
    assert parse_exact(rendered) == entry.epsilon


@pytest.mark.parametrize("power", [1, 8, 27, 36, 60])
def test_a_terminating_rational_longer_than_the_decimal_context_is_still_exact(power) -> None:
    """A dividing spelling rounds at 28 significant digits; this one must not.

    ``689/2**36`` terminates in decimal at exactly 28 significant digits,
    and every larger power of two needs more. ``decimal``'s default
    context rounds a division there, so a spelling built by dividing two
    ``Decimal`` s is already a different number -- and the number in
    question is an ``epsilon``, which the margin ``τ′ = τ − ε`` is formed
    by subtracting (SEAM-FIDELITY-SPEC §3). Every case here is computed,
    and the reference is exact rational arithmetic, not a typed literal.
    """
    value = Fraction(689, 2**power)
    rendered = render_exact(value)
    assert "/" not in rendered, "a power of two terminates in decimal"
    assert parse_exact(rendered) == value
    # The rendered string is the exact decimal, digit for digit: the
    # fraction times 10**scale is a whole number, and reading the digits
    # back over that same power of ten returns the value.
    scale = len(rendered.partition(".")[2])
    assert Fraction(int(rendered.replace(".", "")), 10**scale) == value


def test_the_rendering_is_a_plain_decimal_with_no_exponent() -> None:
    """``plan.json`` and the record carry numbers a reader can read.

    A spelling in exponent notation parses back, but KERNEL-SPEC §2.1's
    rule is that a number in a document is a decimal string; ``1E-8`` is
    the spelling ``decimal`` reaches for and is not one a bin edge or a
    margin should ever be compared against by eye.
    """
    for power in (8, 20, 36, 60):
        rendered = render_exact(Fraction(1, 10**power))
        assert "E" not in rendered.upper()
        assert rendered.startswith("0.")
        assert parse_exact(rendered) == Fraction(1, 10**power)


def test_compiling_twice_produces_one_result(document) -> None:
    first = build_compiled(document)
    second = build_compiled(document)
    assert first.h.value == second.h.value
    assert first.g.value == second.g.value
    assert first.plan_document() == second.plan_document()
