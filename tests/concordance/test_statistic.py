# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §4: the derived statistic ``S``.

Two kinds of assertion here. The first is that ``S`` holds *only*
aggregates -- the boundary property the whole design rests on. The second
is that the aggregates are the right ones, checked against a
straightforward reference computed in exact rational arithmetic straight
from the row values, so the fixed-point path is compared with something
other than itself.
"""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from typing import Any, List, Mapping, Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.concordance.boundary.statistic import (
    UNGROUPED,
    bin_index,
    compute_property_statistic,
    compute_statistic,
    encode_value,
    require_record_count,
)
from flux_engine.concordance.predicate import PropertyKind
from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.errors import FluxError
from tests.concordance.conftest import build_compiled, vectors_document

pytestmark = pytest.mark.unit

_VECTORS = vectors_document()


def _by_kind(compiled, kind: PropertyKind, *, grouped: bool = False):
    for entry in compiled.properties:
        if entry.declaration.kind is kind and (entry.declaration.group_by is not None) == grouped:
            return entry
    raise AssertionError(f"no {kind.value} property")


def _encoded_amounts(rows: Sequence[Mapping[str, Any]], compiled) -> List[int]:
    column = compiled.block.column("amount")
    return [
        encode_value(row["amount"], column, compiled.block.grid, row_index=index)
        for index, row in enumerate(rows)
        if row["amount"] is not None
    ]


# ---------------------------------------------------------------------------
# What S holds -- and what it does not
# ---------------------------------------------------------------------------


def test_the_statistic_document_holds_no_record(compiled, committed_rows) -> None:
    """No subscriber id, no timestamp, no amount spelling reaches ``S``."""
    statistic = compute_statistic(committed_rows, compiled)
    rendered = json.dumps(statistic.to_document(), sort_keys=True)
    for row in committed_rows:
        assert row["subscriber_id"] not in rendered
        assert row["aborted_at"] not in rendered
    # The market values do appear -- the committed support is exactly the
    # sorted member list §4 says leaves the boundary.
    support = _by_kind(compiled, PropertyKind.SUPPORT)
    group = statistic.properties[support.property_id].groups[0]
    assert set(group.support) == {row["market"] for row in committed_rows}


def test_the_statistic_document_canonicalises(compiled, committed_rows) -> None:
    """Sums are strings, counts are integers, so RFC 8785 can serialise it."""
    statistic = compute_statistic(committed_rows, compiled)
    assert canonical_bytes(statistic.to_document())
    for entry in statistic.to_document()["properties"]:
        for group in entry["groups"]:
            assert isinstance(group["k"], int)
            for key in ("Sx", "Sy", "Sxx", "Sxy", "Syy", "quantileWitness"):
                if key in group and group[key] is not None:
                    assert isinstance(group[key], str)


def test_the_published_statistic_is_the_one_the_boundary_forms(
    compiled, committed_rows, document
) -> None:
    assert (
        compute_statistic(committed_rows, compiled).to_document() == document["expect"]["statistic"]
    )


# ---------------------------------------------------------------------------
# The aggregates, against an exact-rational reference
# ---------------------------------------------------------------------------


def test_the_mean_sum_is_the_sum_of_the_encoded_values(compiled, committed_rows) -> None:
    entry = _by_kind(compiled, PropertyKind.MEAN)
    statistic = compute_property_statistic(committed_rows, entry, compiled.block)
    group = statistic.group(UNGROUPED)
    assert group is not None
    assert group.sum_x == sum(_encoded_amounts(committed_rows, compiled))
    assert group.k == len(_encoded_amounts(committed_rows, compiled))


def test_the_bin_counts_account_for_every_contributing_record(compiled, committed_rows) -> None:
    entry = _by_kind(compiled, PropertyKind.MARGINAL_HISTOGRAM)
    statistic = compute_property_statistic(committed_rows, entry, compiled.block)
    group = statistic.group(UNGROUPED)
    assert group is not None
    assert sum(group.bin_counts) == group.k
    edges = [compiled.block.grid.quantise(edge) for edge in entry.declaration.bins]
    assert len(group.bin_counts) == len(edges) - 1
    expected = [0] * (len(edges) - 1)
    for units in _encoded_amounts(committed_rows, compiled):
        for index in range(len(edges) - 1):
            top = edges[index + 1]
            inside = units < top if index < len(edges) - 2 else units <= top
            if units >= edges[index] and inside:
                expected[index] += 1
                break
    assert list(group.bin_counts) == expected


def test_the_second_moments_are_the_sums_of_the_encoded_products(compiled, committed_rows) -> None:
    entry = _by_kind(compiled, PropertyKind.CORRELATION)
    statistic = compute_property_statistic(committed_rows, entry, compiled.block)
    group = statistic.group(UNGROUPED)
    assert group is not None
    amount = compiled.block.column("amount")
    attempts = compiled.block.column("attempts")
    grid = compiled.block.grid
    xs = [
        encode_value(row["amount"], amount, grid, row_index=i)
        for i, row in enumerate(committed_rows)
    ]
    ys = [
        encode_value(row["attempts"], attempts, grid, row_index=i)
        for i, row in enumerate(committed_rows)
    ]
    assert group.sum_x == sum(xs)
    assert group.sum_y == sum(ys)
    assert group.sum_xx == sum(x * x for x in xs)
    assert group.sum_yy == sum(y * y for y in ys)
    assert group.sum_xy == sum(x * y for x, y in zip(xs, ys, strict=True))


def test_the_quantile_witness_is_the_order_statistic_and_carries_its_ranks(
    compiled, committed_rows
) -> None:
    entry = _by_kind(compiled, PropertyKind.QUANTILE)
    statistic = compute_property_statistic(committed_rows, entry, compiled.block)
    group = statistic.group(UNGROUPED)
    assert group is not None
    ordered = sorted(_encoded_amounts(committed_rows, compiled))
    level = entry.declaration.level
    assert level is not None
    index = -((-level.numerator * len(ordered)) // level.denominator)
    assert group.quantile_witness == ordered[index - 1]
    assert group.quantile_below == sum(1 for v in ordered if v < group.quantile_witness)
    assert group.quantile_at_or_below == sum(1 for v in ordered if v <= group.quantile_witness)


def test_the_support_is_the_sorted_distinct_member_list(compiled, committed_rows) -> None:
    entry = _by_kind(compiled, PropertyKind.SUPPORT)
    statistic = compute_property_statistic(committed_rows, entry, compiled.block)
    group = statistic.group(UNGROUPED)
    assert group is not None
    assert list(group.support) == sorted({row["market"] for row in committed_rows})


def test_a_grouped_property_partitions_the_records(compiled, committed_rows) -> None:
    entry = _by_kind(compiled, PropertyKind.MEAN, grouped=True)
    statistic = compute_property_statistic(committed_rows, entry, compiled.block)
    assert sum(group.k for group in statistic.groups) == len(committed_rows)
    assert [group.group for group in statistic.groups] == sorted(
        {row["market"] for row in committed_rows}
    )
    assert statistic.n == len(committed_rows)


def test_a_group_that_is_not_present_reads_as_absent(compiled, committed_rows) -> None:
    entry = _by_kind(compiled, PropertyKind.MEAN, grouped=True)
    statistic = compute_property_statistic(committed_rows, entry, compiled.block)
    assert statistic.group("no-such-market") is None


# ---------------------------------------------------------------------------
# Encoding one value
# ---------------------------------------------------------------------------


def test_a_value_outside_the_declared_range_is_refused_by_class_not_by_value(
    compiled,
) -> None:
    column = compiled.block.column("amount")
    with pytest.raises(FluxError) as caught:
        encode_value("999", column, compiled.block.grid, row_index=7)
    assert caught.value.code.code == "FLX-5005"
    assert "999" not in caught.value.detail
    assert "row 7" in caught.value.detail


@pytest.mark.parametrize("value", [1.5, True, None, object(), b"1"])
def test_a_value_class_the_grid_cannot_encode_is_refused(compiled, value) -> None:
    column = compiled.block.column("amount")
    with pytest.raises(FluxError) as caught:
        encode_value(value, column, compiled.block.grid, row_index=0)
    assert caught.value.code.code == "FLX-5005"


def test_a_decimal_and_its_string_spelling_encode_alike(compiled) -> None:
    column = compiled.block.column("amount")
    grid = compiled.block.grid
    assert encode_value(Decimal("1.5"), column, grid, row_index=0) == encode_value(
        "1.5", column, grid, row_index=0
    )


def test_a_timestamp_is_encoded_through_the_millisecond_parser(compiled, document) -> None:
    column = compiled.block.column("aborted_at")
    grid = compiled.block.grid
    from flux_engine.core.clock import parse_rfc3339_ms

    declared = document["extension"]["fidelity"]["columns"]["aborted_at"]["range"]["min"]
    assert encode_value(declared, column, grid, row_index=0) == grid.quantise(
        Fraction(parse_rfc3339_ms(declared))
    )


def test_a_timestamp_column_refuses_a_non_string(compiled) -> None:
    with pytest.raises(FluxError) as caught:
        encode_value(0, compiled.block.column("aborted_at"), compiled.block.grid, row_index=0)
    assert caught.value.code.code == "FLX-5005"


def test_the_top_of_the_declared_range_is_binned_not_dropped(compiled, document) -> None:
    entry = _by_kind(compiled, PropertyKind.MARGINAL_HISTOGRAM)
    edges = [compiled.block.grid.quantise(edge) for edge in entry.declaration.bins]
    assert bin_index(edges[-1], edges) == len(edges) - 2
    assert bin_index(edges[0], edges) == 0


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


def test_a_record_count_below_the_declared_minimum_is_refused(compiled, committed_rows) -> None:
    with pytest.raises(FluxError) as caught:
        require_record_count(
            committed_rows[: compiled.block.records.minimum - 1],
            compiled.block,
            subject="sample",
        )
    assert caught.value.code.code == "FLX-5005"


def test_a_record_count_above_the_declared_maximum_is_refused(compiled, committed_rows) -> None:
    too_many = committed_rows * (compiled.block.records.maximum + 1)
    with pytest.raises(FluxError) as caught:
        require_record_count(too_many, compiled.block, subject="sample")
    assert caught.value.code.code == "FLX-5005"


def test_a_null_in_a_column_declared_fail_refuses_the_property(compiled, committed_rows) -> None:
    rows = [dict(row) for row in committed_rows]
    rows[0]["attempts"] = None
    entry = _by_kind(compiled, PropertyKind.CORRELATION)
    with pytest.raises(FluxError) as caught:
        compute_property_statistic(rows, entry, compiled.block)
    assert caught.value.code.code == "FLX-5005"
    assert "null is refused" in caught.value.detail


def test_a_null_in_a_column_declared_exclude_drops_the_record(compiled, committed_rows) -> None:
    rows = [dict(row) for row in committed_rows]
    rows[0]["amount"] = None
    entry = _by_kind(compiled, PropertyKind.MEAN)
    statistic = compute_property_statistic(rows, entry, compiled.block)
    group = statistic.group(UNGROUPED)
    assert group is not None
    assert group.k == len(rows) - 1
    assert statistic.n == len(rows)


def test_an_absent_column_is_refused_by_name(compiled, committed_rows) -> None:
    rows = [{k: v for k, v in row.items() if k != "amount"} for row in committed_rows]
    entry = _by_kind(compiled, PropertyKind.MEAN)
    with pytest.raises(FluxError) as caught:
        compute_property_statistic(rows, entry, compiled.block)
    assert caught.value.code.code == "FLX-5031"
    assert "amount" in caught.value.detail


def test_a_support_column_beyond_its_declared_cardinality_is_refused(
    compiled, committed_rows
) -> None:
    maximum = compiled.block.column("market").max_cardinality
    assert maximum is not None
    rows = [{**row, "market": f"m{index}"} for index, row in enumerate(committed_rows)]
    entry = _by_kind(compiled, PropertyKind.SUPPORT)
    with pytest.raises(FluxError) as caught:
        compute_property_statistic(rows, entry, compiled.block)
    assert caught.value.code.code == "FLX-5005"
    assert "maxCardinality" in caught.value.detail


def test_a_support_column_refuses_a_non_string_member(compiled, committed_rows) -> None:
    rows = [dict(row) for row in committed_rows]
    rows[0]["market"] = 7
    entry = _by_kind(compiled, PropertyKind.SUPPORT)
    with pytest.raises(FluxError) as caught:
        compute_property_statistic(rows, entry, compiled.block)
    assert caught.value.code.code == "FLX-5005"


def test_the_whole_statistic_refuses_where_one_property_would(compiled, committed_rows) -> None:
    """The boundary's path: a committed state is published whole or not at all."""
    rows = [dict(row) for row in committed_rows]
    rows[0]["amount"] = "999"
    with pytest.raises(FluxError):
        compute_statistic(rows, compiled)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@given(order=st.permutations(range(len(_VECTORS["committedSlice"]))))
@settings(max_examples=25, deadline=None)
@pytest.mark.property
def test_the_statistic_is_invariant_under_the_order_records_arrive_in(order) -> None:
    """Two record sets with the same contents give one ``S``, byte for byte.

    Nothing about the derived statistic may depend on the order rows were
    read in: a boundary that committed a different ``C2`` for the same
    records under a different read order would make ``C2`` a fact about
    the reader, not about the data.
    """
    document = vectors_document()
    compiled = build_compiled(document)
    rows = document["committedSlice"]
    reference = compute_statistic(rows, compiled)
    shuffled = compute_statistic([rows[index] for index in order], compiled)
    assert canonical_bytes(shuffled.to_document()) == canonical_bytes(reference.to_document())


@given(values=st.lists(st.integers(min_value=16, max_value=128), min_size=1, max_size=40))
@pytest.mark.property
def test_every_encoded_value_lands_in_exactly_one_bin(values) -> None:
    edges = [16, 32, 64, 128]
    counts = [0] * (len(edges) - 1)
    for units in values:
        counts[bin_index(units, edges)] += 1
    assert sum(counts) == len(values)
    for units in values:
        index = bin_index(units, edges)
        assert edges[index] <= units
        assert units <= edges[index + 1]


# ---------------------------------------------------------------------------
# Fail-closed on a correlation's null policies (both columns, not the first)
# ---------------------------------------------------------------------------


def _correlation(compiled):
    return _by_kind(compiled, PropertyKind.CORRELATION)


def test_a_null_in_the_second_column_of_a_correlation_is_refused() -> None:
    """A null in *either* column declared ``null: fail`` refuses.

    The vectors' correlation is over one ``null: exclude`` column and one
    ``null: fail`` column, in that declared order. Reading the policy from
    the first column only would let a row null in *both* halves take the
    exclude branch and never consult the second column's declaration, so
    a dataset carrying nulls in a ``null: fail`` column would pass the
    property SEAM-FIDELITY-SPEC §5 says must refuse.
    """
    document = vectors_document()
    compiled = build_compiled(document)
    entry = _correlation(compiled)
    first, second = entry.declaration.columns
    assert compiled.block.column(first).null_policy.value == "exclude"
    assert compiled.block.column(second).null_policy.value == "fail"

    rows = [dict(row) for row in document["committedSlice"]]
    rows[0][first] = None
    rows[0][second] = None
    with pytest.raises(FluxError) as caught:
        compute_property_statistic(rows, entry, compiled.block, subject="s")
    assert caught.value.code.code == "FLX-5005"
    assert second in (caught.value.detail or "")


def test_a_null_only_in_the_excluded_column_of_a_correlation_still_excludes() -> None:
    """The negative control: the ``null: exclude`` half alone drops the row."""
    document = vectors_document()
    compiled = build_compiled(document)
    entry = _correlation(compiled)
    first = entry.declaration.columns[0]
    rows = [dict(row) for row in document["committedSlice"]]
    rows[0][first] = None
    statistic = compute_property_statistic(rows, entry, compiled.block, subject="s")
    assert statistic.groups[0].k == len(rows) - 1


# ---------------------------------------------------------------------------
# A group is named by its ordinal, never by its key
# ---------------------------------------------------------------------------


def test_a_group_too_small_is_refused_without_naming_the_group_key() -> None:
    """The refusal names the group's ordinal, not the value it grouped on.

    The key is a value read out of the owner's committed slice, and this
    refusal fires only for groups below ``records.min`` -- the rare,
    re-identifying ones -- and travels out to the operator's terminal and
    the ``--json`` envelope. KERNEL-SPEC §9 keeps values out of
    diagnostics; the ordinal is enough to act on.
    """
    document = vectors_document()
    compiled = build_compiled(document)
    entry = _by_kind(compiled, PropertyKind.MEAN, grouped=True)
    group_by = entry.declaration.group_by
    assert group_by is not None

    rows = [dict(row) for row in document["committedSlice"]]
    rows[0][group_by] = "MENTAL_HEALTH_CLINIC_PATIENT"
    with pytest.raises(FluxError) as caught:
        compute_property_statistic(rows, entry, compiled.block, subject="committed state")
    detail = caught.value.detail or ""
    assert caught.value.code.code == "FLX-5005"
    assert "MENTAL_HEALTH_CLINIC_PATIENT" not in detail
    assert "group #" in detail


def test_an_ungrouped_sample_refusal_names_no_group_at_all() -> None:
    document = vectors_document()
    compiled = build_compiled(document)
    entry = _by_kind(compiled, PropertyKind.MEAN)
    with pytest.raises(FluxError) as caught:
        compute_property_statistic([], entry, compiled.block, subject="committed state")
    assert "group" not in (caught.value.detail or "")


# ---------------------------------------------------------------------------
# The support statistic counts records, not just distinct members
# ---------------------------------------------------------------------------


def test_the_support_statistic_carries_a_record_count_per_member() -> None:
    """``support_counts`` is aligned to ``support`` and sums to ``k``.

    Without it the SUPPORT comparison can only count how many *different*
    strange values a dataset holds, never how much of the dataset is
    strange.
    """
    document = vectors_document()
    compiled = build_compiled(document)
    entry = _by_kind(compiled, PropertyKind.SUPPORT)
    column = entry.declaration.columns[0]
    rows = [dict(row) for row in document["committedSlice"]]
    for row in rows:
        row[column] = "ie"
    statistic = compute_property_statistic(rows, entry, compiled.block, subject="s")
    group = statistic.groups[0]
    assert group.support == ("ie",)
    assert group.support_counts == (len(rows),)
    assert sum(group.support_counts) == group.k


def test_the_committed_commitment_publishes_no_per_member_record_count() -> None:
    """§4 publishes "the sorted committed support" -- and nothing per value.

    ``support_counts`` is an in-process quantity; putting it in the ``C2``
    pre-image would be a new fact about the real data leaving the boundary.
    """
    document = vectors_document()
    compiled = build_compiled(document)
    entry = _by_kind(compiled, PropertyKind.SUPPORT)
    statistic = compute_property_statistic(
        document["committedSlice"], entry, compiled.block, subject="s"
    )
    rendered = json.loads(canonical_bytes(statistic.to_document()).decode("utf-8"))
    assert "supportCounts" not in json.dumps(rendered)
    assert "support_counts" not in json.dumps(rendered)
