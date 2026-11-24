# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Drive every vector in ``tests/vectors/fidelity-vectors.json``.

The file is the published pinning artefact for this subsystem: one small
contract, one committed slice, and the digests, margins, statistic and
per-property outcomes they produce. This module recomputes every one of
them, and asserts that the file's own coverage lists are complete -- so a
property, a fail-closed condition or a rounding direction that stops being
exercised is a failure here rather than a quiet gap.

``vectorsVersion`` 2 added ``levelHistogram`` as a block of its own under
``kinds``, with a contract, a committed slice and pins of its own.
``vectorsVersion`` 3 added ``quantileBins`` the same way: a quantile that
declares ``bins``, measured and compared by ``quantile/2``, whose statistic
spells the bin that holds the order statistic and never the witness. Version
1's block is untouched (a property added to it would move every digest it pins),
and this module holds it to that: the last section pins version 1 by digest,
and each added kind is recomputed and re-derived the same way version 1 is.
"""

from __future__ import annotations

import json
from collections import Counter
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Sequence

import pytest

from flux_engine.concordance.boundary.slice import commitment_c2
from flux_engine.concordance.boundary.statistic import compute_statistic
from flux_engine.concordance.compiler import parse_exact, render_exact
from flux_engine.concordance.predicate import PropertyKind
from flux_engine.concordance.verifier import ClearStatistic
from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_stats.fixedpoint import Grid, effective_tolerance
from tests.concordance.conftest import (
    VERSION_1,
    blinding_of,
    build_committed,
    build_compiled,
    evaluation_case,
    evaluation_ids,
    published_block,
    published_blocks,
    total_variation,
    vectors_document,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The file itself
# ---------------------------------------------------------------------------


def test_the_vectors_file_carries_no_json_number_for_a_measured_quantity(vectors) -> None:
    """Every declared and derived number is a string; only counts are integers.

    A JSON number would go through the IEEE 754 double domain, which is the
    one thing the grid exists to remove -- and a vectors file that carried
    one could not be reloaded exactly.
    """

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        else:
            assert not isinstance(node, float), f"{path} is a float"

    walk(vectors, "$")


def test_the_vectors_file_canonicalises(vectors) -> None:
    assert canonical_bytes(json.loads(json.dumps(vectors)))


def test_the_published_blocks_exercise_every_property_the_spec_names(vectors) -> None:
    published = {
        entry["property"]
        for _, block in published_blocks(vectors)
        for entry in block["extension"]["fidelity"]["properties"]
    }
    # A rank correlation is scored from a profile only: a committed slice names it and compares nothing.
    assert published == {kind.value for kind in PropertyKind} - {"rankCorrelation"}


def test_the_published_block_exercises_both_column_kinds_and_both_null_policies(
    vectors,
) -> None:
    columns = vectors["extension"]["fidelity"]["columns"].values()
    assert any("range" in column for column in columns)
    assert any("support" in column for column in columns)
    assert {column.get("null", "exclude") for column in columns} == {"exclude", "fail"}


def test_the_published_block_exercises_a_timestamp_range(vectors) -> None:
    ranges = [
        column["range"]
        for column in vectors["extension"]["fidelity"]["columns"].values()
        if "range" in column
    ]
    assert any("T" in str(bounds["min"]) for bounds in ranges)


def test_the_published_block_exercises_a_grouped_property(vectors) -> None:
    assert any("groupBy" in entry for entry in vectors["extension"]["fidelity"]["properties"])


def test_the_published_evaluations_cover_every_fail_closed_condition(vectors) -> None:
    """§5's list: ``k = 0``, count outside ``records``, group below
    ``records.min``, a value outside its range, a column absent."""
    ids = {case["id"] for case in vectors["evaluations"]}
    assert {
        "all-amounts-null",
        "too-few-records",
        "too-many-records",
        "group-below-min",
        "value-out-of-range",
        "column-absent",
    } <= ids


def test_the_published_evaluations_cover_within_and_outside(vectors) -> None:
    outcomes = {
        outcome
        for case in vectors["evaluations"]
        for outcome in case["expect"]["outcomes"].values()
    }
    assert outcomes == {"within", "outside"}
    assert any(case["expect"]["within"] for case in vectors["evaluations"])
    assert any(not case["expect"]["within"] for case in vectors["evaluations"])


def test_every_property_is_seen_both_within_and_outside_across_the_evaluations(
    vectors,
) -> None:
    seen: Dict[str, set] = {}
    for case in vectors["evaluations"]:
        for property_id, outcome in case["expect"]["outcomes"].items():
            seen.setdefault(property_id, set()).add(outcome)
    assert seen, "the evaluations report per-property outcomes"
    for property_id, outcomes in sorted(seen.items()):
        assert outcomes == {"within", "outside"}, f"{property_id} is only ever {outcomes}"


def test_the_rounding_cases_cover_both_directions_and_the_refusal(vectors) -> None:
    cases = vectors["roundingCases"]
    assert any("margin" in case["expect"] for case in cases)
    assert any(case["expect"].get("code") == "FLX-5002" for case in cases)
    assert any(
        "margin" in case["expect"]
        and parse_exact(case["expect"]["margin"]) < parse_exact(case["expect"]["notLooserThan"])
        for case in cases
    ), "at least one case must actually be rounded down"


# ---------------------------------------------------------------------------
# Recomputing the pinned values
# ---------------------------------------------------------------------------


def test_the_pinned_digests_are_recomputed(vectors) -> None:
    compiled = build_compiled(vectors_document())
    assert compiled.h.value == vectors["expect"]["H"]
    assert compiled.g.value == vectors["expect"]["G"]


def test_the_pinned_plan_fragment_is_recomputed(vectors) -> None:
    compiled = build_compiled(vectors_document())
    assert compiled.plan_document() == vectors["expect"]["plan"]


def test_the_pinned_statistic_is_recomputed(vectors) -> None:
    document = vectors_document()
    compiled = build_compiled(document)
    statistic = compute_statistic(document["committedSlice"], compiled)
    assert statistic.to_document() == vectors["expect"]["statistic"]


def test_the_pinned_commitment_is_recomputed(vectors) -> None:
    document = vectors_document()
    compiled = build_compiled(document)
    statistic = compute_statistic(document["committedSlice"], compiled)
    assert commitment_c2(statistic, blinding_of(document)) == vectors["expect"]["C2"]


def _assert_reproduced(block: Mapping[str, Any], case: Mapping[str, Any]) -> None:
    """Evaluate one published case against its block's committed state and compare."""
    compiled = build_compiled(block)
    committed = build_committed(block, compiled)
    evaluation = ClearStatistic(
        compiled=compiled, committed=committed, synthetic_rows=case["syntheticRows"]
    ).evaluate()
    expect = case["expect"]
    assert evaluation.within is expect["within"], case["note"]
    assert evaluation.refused is expect["refused"], case["note"]
    assert sorted({refusal.code for refusal in evaluation.refusals}) == expect["refusalCodes"]
    assert {outcome.property_id: outcome.outcome for outcome in evaluation.outcomes} == expect[
        "outcomes"
    ]


@pytest.mark.parametrize("identifier", evaluation_ids())
def test_a_published_evaluation_is_reproduced(identifier) -> None:
    _assert_reproduced(vectors_document(), evaluation_case(identifier))


@pytest.mark.parametrize("case", vectors_document()["roundingCases"], ids=lambda c: c["id"])
def test_a_published_rounding_case_is_reproduced(case) -> None:
    grid = Grid(fractional_bits=case["fractionalBits"])
    tolerance = parse_exact(case["tolerance"])
    epsilon = parse_exact(case["epsilon"])
    if "code" in case["expect"]:
        with pytest.raises(FluxError) as caught:
            effective_tolerance(grid, tolerance, epsilon)
        assert caught.value.code.code == case["expect"]["code"]
        return
    margin = effective_tolerance(grid, tolerance, epsilon)
    assert render_exact(margin) == case["expect"]["margin"]
    assert margin <= tolerance - epsilon, "a margin is never looser than tau - epsilon"
    assert render_exact(tolerance - epsilon) == case["expect"]["notLooserThan"]
    assert grid.is_on_grid(margin) is case["expect"]["onGrid"]


def test_the_committed_slice_holds_records_min_records_or_more(vectors) -> None:
    records = vectors["extension"]["fidelity"]["records"]
    assert records["min"] <= len(vectors["committedSlice"]) <= records["max"]


# ---------------------------------------------------------------------------
# Versions 2 and 3: the property kinds and forms added after version 1
# ---------------------------------------------------------------------------

#: The digest of everything version 1 published: every top-level key except the
#: version number and ``kinds``. A later version adds a block and moves nothing
#: else, so this does not change when one is published.
VERSION_1_DIGEST = "sha256:d27bef75d954062b141022bc75bdf76652d78c1d1e90cb8ae9528f3baf33f23e"

#: A block carries what version 1's document does, bar the rounding cases, plus
#: the version it was added in and a note.
BLOCK_KEYS = {
    "since",
    "note",
    "contract",
    "emit",
    "extension",
    "expose",
    "committedSlice",
    "blindingHex",
    "committedAt",
    "stateOrdinal",
    "expect",
    "evaluations",
}

#: A block is named for the property kind it adds. One that adds a form of a kind
#: version 1 already holds is named for the form, and says here which kind it is
#: and which key the form is spelled with: ``quantileBins`` is a ``quantile``
#: with ``bins`` (``quantile/2``), which version 1's quantile does not have.
ADDED_FORMS = {"quantileBins": ("quantile", "bins")}

KINDS = [name for name, _ in published_blocks(vectors_document()) if name != VERSION_1]
KIND_CASES = [(kind, identifier) for kind in KINDS for identifier in evaluation_ids(kind)]


def test_the_version_1_cases_are_frozen(vectors) -> None:
    frozen = {k: v for k, v in vectors.items() if k not in {"vectorsVersion", "kinds"}}
    assert digest_of_document(frozen, subject="fidelityVectorsVersion1").value == VERSION_1_DIGEST


def test_the_file_names_the_newest_version_it_carries(vectors) -> None:
    newest = max([1, *(block["since"] for block in vectors.get("kinds", {}).values())])
    assert vectors["vectorsVersion"] == newest


def _declared(block: Mapping[str, Any], kind: str, form: str | None) -> List[Mapping[str, Any]]:
    """The properties of *kind* a block declares that spell *form* (any, if there is none)."""
    return [
        entry
        for entry in block["extension"]["fidelity"]["properties"]
        if entry["property"] == kind and (form is None or form in entry)
    ]


@pytest.mark.parametrize("name", KINDS)
def test_an_added_kind_is_one_self_contained_block_that_declares_it(name, vectors) -> None:
    block = published_block(name)
    assert set(block) == BLOCK_KEYS
    assert block["since"] > 1
    kind, form = ADDED_FORMS.get(name, (name, None))
    assert kind in {member.value for member in PropertyKind}
    assert _declared(block, kind, form), f"{name} declares no {kind} property"
    assert not _declared(
        vectors, kind, form
    ), "version 1's block is frozen: a new kind or form is a block of its own"


@pytest.mark.parametrize("kind", KINDS)
def test_an_added_kinds_pinned_values_are_recomputed(kind) -> None:
    block = published_block(kind)
    compiled = build_compiled(block)
    statistic = compute_statistic(block["committedSlice"], compiled)
    pinned = block["expect"]
    assert (compiled.h.value, compiled.g.value) == (pinned["H"], pinned["G"])
    assert compiled.plan_document() == pinned["plan"]
    assert statistic.to_document() == pinned["statistic"]
    assert commitment_c2(statistic, blinding_of(block)) == pinned["C2"]


@pytest.mark.parametrize(("kind", "identifier"), KIND_CASES)
def test_an_added_kinds_published_evaluation_is_reproduced(kind, identifier) -> None:
    _assert_reproduced(published_block(kind), evaluation_case(identifier, kind))


def _level_table(
    rows: Sequence[Mapping[str, Any]], column: str, group_by: str | None
) -> Dict[str, Counter]:
    """Rows counted per (group, level); a null level (``null: exclude``) drops the record."""
    held: Dict[str, Counter] = {}
    for row in rows:
        if row[column] is not None:
            held.setdefault(row[group_by] if group_by else "", Counter())[row[column]] += 1
    return held


def _level_histogram_verdicts(
    block: Mapping[str, Any], synthetic: Sequence[Mapping[str, Any]]
) -> Dict[str, str]:
    """Each ``levelHistogram`` verdict, re-derived from the rows alone.

    Counts the rows per (group, level) with :class:`collections.Counter` and
    compares each group's shares by total variation in exact rationals, against
    the margin the block's own plan fragment pins. A property the check refuses
    (a level the declaration does not name, two sides that group differently) has
    no verdict and is left out: it fails closed.
    """
    columns = block["extension"]["fidelity"]["columns"]
    verdicts: Dict[str, str] = {}
    for entry in block["expect"]["plan"]["properties"]:
        (column,) = entry["columns"]
        group_by = entry.get("groupBy")
        levels = set(columns[column]["support"]["values"])
        committed = _level_table(block["committedSlice"], column, group_by)
        held = _level_table(synthetic, column, group_by)
        if set(committed) != set(held) or any(set(counts) - levels for counts in held.values()):
            continue
        margin = parse_exact(entry["margin"])
        within = all(total_variation(committed[g], held[g]) <= margin for g in committed)
        verdicts[entry["propertyId"]] = "within" if within else "outside"
    return verdicts


@pytest.mark.parametrize("identifier", evaluation_ids("levelHistogram"))
def test_a_published_level_histogram_verdict_is_total_variation_against_the_margin(
    identifier,
) -> None:
    block = published_block("levelHistogram")
    case = evaluation_case(identifier, "levelHistogram")
    outcomes = case["expect"]["outcomes"]
    verdicts = _level_histogram_verdicts(block, case["syntheticRows"])
    for property_id, verdict in verdicts.items():
        assert outcomes[property_id] == verdict, case["note"]
    for property_id in set(outcomes) - set(verdicts):
        assert outcomes[property_id] == "outside", "a refused property is outside"
        assert case["expect"]["refused"], case["note"]


def test_the_level_histogram_block_exercises_both_forms_and_every_outcome() -> None:
    block = published_block("levelHistogram")
    properties = block["extension"]["fidelity"]["properties"]
    assert {"groupBy" in entry for entry in properties} == {True, False}, "grouped and ungrouped"
    seen: Dict[str, set] = {}
    for case in block["evaluations"]:
        for property_id, outcome in case["expect"]["outcomes"].items():
            seen.setdefault(property_id, set()).add(outcome)
    assert set(seen) == {entry["propertyId"] for entry in block["expect"]["plan"]["properties"]}
    for property_id, outcomes in sorted(seen.items()):
        assert outcomes == {"within", "outside"}, f"{property_id} is only ever {outcomes}"


def test_the_level_histogram_block_exercises_each_rule_of_the_kind() -> None:
    block = published_block("levelHistogram")
    ids = {case["id"] for case in block["evaluations"]}
    assert {
        "on-the-margin",
        "one-record-past-the-margin",
        "zero-cell-drawn",
        "null-plan-excluded",
        "undeclared-level",
        "group-not-held",
    } <= ids
    # An out-of-order declaration, so the sorted cell order is pinned, and a
    # declared level a group never holds, so a zero cell is pinned.
    for column in block["extension"]["fidelity"]["columns"].values():
        values = column["support"]["values"]
        assert values != sorted(values)
    counts: List[List[int]] = [
        group["levelCounts"]
        for entry in block["expect"]["statistic"]["properties"]
        for group in entry["groups"]
    ]
    assert any(0 in group for group in counts)
    refused = {case["id"] for case in block["evaluations"] if case["expect"]["refused"]}
    assert refused == {"undeclared-level", "group-not-held"}


# ---------------------------------------------------------------------------
# Version 3: a quantile with bins (quantile/2)
# ---------------------------------------------------------------------------


def _order_statistic_units(rows: Sequence[Mapping[str, Any]], level: Fraction, scale: int) -> int:
    """The order statistic at *level* of the ``amount`` column, in grid units.

    The ``ceil(level * k)``-th smallest value of the ``k`` records, each value
    ``v`` read as ``v * scale`` units. Exact rationals throughout; the values
    are on the grid, so the units are whole numbers.
    """
    units = sorted(Fraction(row["amount"]) * scale for row in rows)
    rank = -((-level.numerator * len(units)) // level.denominator)
    held = units[max(rank, 1) - 1]
    assert held.denominator == 1
    return int(held)


def _quantile_bins_verdicts(
    block: Mapping[str, Any], synthetic: Sequence[Mapping[str, Any]]
) -> Dict[str, str]:
    """Each ``quantileBins`` verdict, re-derived from the rows and the declared edges alone.

    The reference bin is the one that holds the committed order statistic (the
    last is closed at its top). The synthetic order statistic ``w`` is within
    iff ``e_i - margin <= w <= t_i + margin`` in grid units, where ``t_i`` is
    the bin's largest grid value, ``e_{i+1} - 1``, or ``e_n`` for the last bin
    (ADR 0012). The margin comes from the block's own plan fragment. A property
    whose synthetic rows leave the declared range has no verdict and is left
    out: the check refuses it.
    """
    fidelity = block["extension"]["fidelity"]
    scale = 1 << fidelity["encoding"]["fractionalBits"]
    bounds = fidelity["columns"]["amount"]["range"]
    low, high = Fraction(bounds["min"]), Fraction(bounds["max"])
    if any(not low <= Fraction(row["amount"]) <= high for row in synthetic):
        return {}
    verdicts: Dict[str, str] = {}
    for declared, planned in zip(
        fidelity["properties"], block["expect"]["plan"]["properties"], strict=True
    ):
        edges = [int(Fraction(edge) * scale) for edge in declared["bins"]]
        level = Fraction(declared["level"])
        margin = int(parse_exact(planned["margin"]) * scale)
        reference = _order_statistic_units(block["committedSlice"], level, scale)
        index = max(i for i in range(len(edges) - 1) if edges[i] <= reference)
        top = edges[index + 1] if index == len(edges) - 2 else edges[index + 1] - 1
        witness = _order_statistic_units(synthetic, level, scale)
        within = edges[index] - margin <= witness <= top + margin
        verdicts[planned["propertyId"]] = "within" if within else "outside"
    return verdicts


@pytest.mark.parametrize("identifier", evaluation_ids("quantileBins"))
def test_a_published_quantile_bins_verdict_is_the_order_statistic_against_the_bin(
    identifier,
) -> None:
    block = published_block("quantileBins")
    case = evaluation_case(identifier, "quantileBins")
    outcomes = case["expect"]["outcomes"]
    verdicts = _quantile_bins_verdicts(block, case["syntheticRows"])
    for property_id, verdict in verdicts.items():
        assert outcomes[property_id] == verdict, case["note"]
    for property_id in set(outcomes) - set(verdicts):
        assert outcomes[property_id] == "outside", "a refused property is outside"
        assert case["expect"]["refused"], case["note"]


def test_the_quantile_bins_block_pins_the_bin_and_never_the_witness() -> None:
    block = published_block("quantileBins")
    groups = [
        group for entry in block["expect"]["statistic"]["properties"] for group in entry["groups"]
    ]
    assert [set(group) for group in groups] == [
        {"group", "k", "quantileBin", "binBelow", "binThrough"}
    ] * 2
    # The order statistic of rank j lies in the bin when binBelow < j <= binThrough.
    ranks = [5, 9]
    for group, rank in zip(groups, ranks, strict=True):
        assert group["binBelow"] < rank <= group["binThrough"]
    last = max(group["quantileBin"] for group in groups)
    assert last == len(block["extension"]["fidelity"]["properties"][0]["bins"]) - 2
    assert [group["binThrough"] for group in groups if group["quantileBin"] == last] == [
        group["k"] for group in groups if group["quantileBin"] == last
    ], "the last bin is closed at the top: it counts every record through it"
    assert "quantileWitness" not in json.dumps(block["expect"])


def test_the_quantile_bins_block_exercises_each_edge_of_the_margin() -> None:
    block = published_block("quantileBins")
    seen: Dict[str, set] = {}
    for case in block["evaluations"]:
        for property_id, outcome in case["expect"]["outcomes"].items():
            seen.setdefault(property_id, set()).add(outcome)
    assert set(seen) == {entry["propertyId"] for entry in block["expect"]["plan"]["properties"]}
    for property_id, outcomes in sorted(seen.items()):
        assert outcomes == {"within", "outside"}, f"{property_id} is only ever {outcomes}"
    ids = {case["id"] for case in block["evaluations"]}
    assert {
        "inside-the-bin",
        "margin-below-the-lower-edge",
        "past-the-margin-below-the-lower-edge",
        "margin-above-the-top",
        "past-the-margin-above-the-top",
        "last-bin-closed-at-the-top",
        "value-out-of-range",
    } <= ids
    refused = {case["id"] for case in block["evaluations"] if case["expect"]["refused"]}
    assert refused == {"value-out-of-range"}
