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
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping

import pytest

from flux_engine.concordance.boundary.slice import commitment_c2
from flux_engine.concordance.boundary.statistic import compute_statistic
from flux_engine.concordance.compiler import parse_exact, render_exact
from flux_engine.concordance.predicate import PropertyKind
from flux_engine.concordance.verifier import ClearStatistic
from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.errors import FluxError
from flux_engine.core.fixedpoint import Grid, effective_tolerance
from tests.concordance.conftest import (
    PINNED_OUTSIDE_THE_VECTORS,
    REPO_ROOT,
    blinding_of,
    build_committed,
    build_compiled,
    evaluation_case,
    evaluation_ids,
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


def test_the_published_block_exercises_every_property_the_spec_names(vectors) -> None:
    declared = {entry["property"] for entry in vectors["extension"]["fidelity"]["properties"]}
    later = {kind.value for kind in PINNED_OUTSIDE_THE_VECTORS}
    assert declared.isdisjoint(later), "a kind the file pins needs no second pin"
    assert declared | later == {kind.value for kind in PropertyKind}
    for path in PINNED_OUTSIDE_THE_VECTORS.values():
        assert (REPO_ROOT / path).is_file(), f"{path} pins a kind the file does not"


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


@pytest.mark.parametrize("identifier", evaluation_ids())
def test_a_published_evaluation_is_reproduced(identifier) -> None:
    case = evaluation_case(identifier)
    document = vectors_document()
    compiled = build_compiled(document)
    committed = build_committed(document, compiled)
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
