# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Every refusal branch, exercised once.

The modules in this package refuse a great deal, and a refusal that is
never taken is a refusal nobody has read. This module walks the branches
the behavioural suites do not reach on their way to something else -- the
malformed shapes, the wrong value classes, the null policies on each
column kind -- so that no diagnostic in the package is text that has never
been produced.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any, Callable, Dict, Mapping

import pytest

from flux_engine.concordance.boundary.slice import SliceReference, commit_state
from flux_engine.concordance.boundary.statistic import (
    compute_property_statistic,
    encode_value,
)
from flux_engine.concordance.compiler import property_magnitudes
from flux_engine.concordance.gate import GateDecision
from flux_engine.concordance.predicate import (
    PropertyKind,
    parse_fidelity_block,
)
from flux_engine.concordance.record import load_record
from flux_engine.concordance.verifier import (
    FidelityEvaluation,
    PropertyOutcome,
    Refusal,
)
from flux_engine.core.errors import FluxError

pytestmark = pytest.mark.unit


def _block(document: Mapping[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(dict(document["extension"]["fidelity"]))


def _refuses(raw: Mapping[str, Any], expose, code: str) -> FluxError:
    with pytest.raises(FluxError) as caught:
        parse_fidelity_block(raw, expose=expose)
    assert caught.value.code.code == code, caught.value
    return caught.value


# ---------------------------------------------------------------------------
# Shapes and value classes in the declaration
# ---------------------------------------------------------------------------


def _mutations() -> Dict[str, Callable[[Dict[str, Any]], None]]:
    def properties_not_a_list(block: Dict[str, Any]) -> None:
        block["properties"] = {"property": "mean"}

    def a_section_that_is_not_a_mapping(block: Dict[str, Any]) -> None:
        block["budget"] = [1, 2]

    def records_missing_a_bound(block: Dict[str, Any]) -> None:
        block["records"] = {"min": 1}

    def a_non_integer_record_count(block: Dict[str, Any]) -> None:
        block["records"] = {"min": "1", "max": 8}

    def a_record_count_below_one(block: Dict[str, Any]) -> None:
        block["records"] = {"min": 0, "max": 8}

    def a_budget_without_a_check_count(block: Dict[str, Any]) -> None:
        block["budget"] = {"maxStateAge": "P30D"}

    def a_bool_tolerance(block: Dict[str, Any]) -> None:
        block["properties"][0]["tolerance"]["value"] = True

    def a_value_class_with_no_decimal_reading(block: Dict[str, Any]) -> None:
        block["properties"][0]["tolerance"]["value"] = [1]

    def a_non_finite_decimal_tolerance(block: Dict[str, Any]) -> None:
        block["properties"][0]["tolerance"]["value"] = "NaN"

    def a_tolerance_without_a_type(block: Dict[str, Any]) -> None:
        block["properties"][0]["tolerance"] = {"value": "0.25"}

    def a_tolerance_type_that_is_not_one(block: Dict[str, Any]) -> None:
        block["properties"][0]["tolerance"] = {"type": "hellinger", "value": "0.25"}

    def a_distance_tolerance_without_a_measure(block: Dict[str, Any]) -> None:
        for entry in block["properties"]:
            if entry["property"] == PropertyKind.MARGINAL_HISTOGRAM.value:
                entry["tolerance"] = {"type": "distance", "max": "0.2"}

    def a_relative_tolerance_without_a_value(block: Dict[str, Any]) -> None:
        block["properties"][0]["tolerance"] = {"type": "relative"}

    def a_non_positive_relative_tolerance(block: Dict[str, Any]) -> None:
        block["properties"][0]["tolerance"] = {"type": "relative", "value": "0"}

    def a_distance_tolerance_without_a_max(block: Dict[str, Any]) -> None:
        for entry in block["properties"]:
            if entry["property"] == PropertyKind.MARGINAL_HISTOGRAM.value:
                entry["tolerance"] = {"type": "distance", "measure": "totalVariation"}

    def a_distance_tolerance_carrying_a_value(block: Dict[str, Any]) -> None:
        for entry in block["properties"]:
            if entry["property"] == PropertyKind.MARGINAL_HISTOGRAM.value:
                entry["tolerance"] = {
                    "type": "distance",
                    "measure": "totalVariation",
                    "value": "0.2",
                }

    def a_non_positive_distance_maximum(block: Dict[str, Any]) -> None:
        for entry in block["properties"]:
            if entry["property"] == PropertyKind.MARGINAL_HISTOGRAM.value:
                entry["tolerance"] = {
                    "type": "distance",
                    "measure": "totalVariation",
                    "max": "0",
                }

    def bins_that_are_not_a_list(block: Dict[str, Any]) -> None:
        for entry in block["properties"]:
            if entry["property"] == PropertyKind.MARGINAL_HISTOGRAM.value:
                entry["bins"] = "1,2,4,8"

    def bins_on_a_property_that_takes_none(block: Dict[str, Any]) -> None:
        block["properties"][0]["bins"] = ["1", "8"]

    def a_level_on_a_property_that_takes_none(block: Dict[str, Any]) -> None:
        block["properties"][0]["level"] = "0.5"

    def columns_that_are_not_a_list(block: Dict[str, Any]) -> None:
        for entry in block["properties"]:
            if entry["property"] == PropertyKind.CORRELATION.value:
                entry["columns"] = "amount,attempts"

    def both_a_column_and_columns(block: Dict[str, Any]) -> None:
        block["properties"][0]["columns"] = ["amount"]

    def a_single_column_property_naming_two(block: Dict[str, Any]) -> None:
        entry = dict(block["properties"][0])
        entry.pop("column")
        entry["columns"] = ["amount", "attempts"]
        block["properties"][0] = entry

    def a_property_over_a_column_of_the_wrong_kind(block: Dict[str, Any]) -> None:
        block["properties"][0]["column"] = "market"

    def a_null_policy_that_is_not_one(block: Dict[str, Any]) -> None:
        block["columns"]["amount"]["null"] = "ignore"

    def a_support_source_that_is_not_declared(block: Dict[str, Any]) -> None:
        block["columns"]["market"]["support"] = "inferred"

    def a_range_column_carrying_a_max_cardinality(block: Dict[str, Any]) -> None:
        block["columns"]["amount"]["maxCardinality"] = 4

    def a_column_declaring_neither(block: Dict[str, Any]) -> None:
        block["columns"]["amount"] = {"null": "exclude"}

    def a_range_missing_a_bound(block: Dict[str, Any]) -> None:
        block["columns"]["amount"]["range"] = {"min": "1"}

    return {
        "properties_not_a_list": properties_not_a_list,
        "a_section_that_is_not_a_mapping": a_section_that_is_not_a_mapping,
        "records_missing_a_bound": records_missing_a_bound,
        "a_non_integer_record_count": a_non_integer_record_count,
        "a_record_count_below_one": a_record_count_below_one,
        "a_budget_without_a_check_count": a_budget_without_a_check_count,
        "a_bool_tolerance": a_bool_tolerance,
        "a_value_class_with_no_decimal_reading": a_value_class_with_no_decimal_reading,
        "a_non_finite_decimal_tolerance": a_non_finite_decimal_tolerance,
        "a_tolerance_without_a_type": a_tolerance_without_a_type,
        "a_tolerance_type_that_is_not_one": a_tolerance_type_that_is_not_one,
        "a_distance_tolerance_without_a_measure": a_distance_tolerance_without_a_measure,
        "a_relative_tolerance_without_a_value": a_relative_tolerance_without_a_value,
        "a_non_positive_relative_tolerance": a_non_positive_relative_tolerance,
        "a_distance_tolerance_without_a_max": a_distance_tolerance_without_a_max,
        "a_distance_tolerance_carrying_a_value": a_distance_tolerance_carrying_a_value,
        "a_non_positive_distance_maximum": a_non_positive_distance_maximum,
        "bins_that_are_not_a_list": bins_that_are_not_a_list,
        "bins_on_a_property_that_takes_none": bins_on_a_property_that_takes_none,
        "a_level_on_a_property_that_takes_none": a_level_on_a_property_that_takes_none,
        "columns_that_are_not_a_list": columns_that_are_not_a_list,
        "both_a_column_and_columns": both_a_column_and_columns,
        "a_single_column_property_naming_two": a_single_column_property_naming_two,
        "a_property_over_a_column_of_the_wrong_kind": a_property_over_a_column_of_the_wrong_kind,
        "a_null_policy_that_is_not_one": a_null_policy_that_is_not_one,
        "a_support_source_that_is_not_declared": a_support_source_that_is_not_declared,
        "a_range_column_carrying_a_max_cardinality": a_range_column_carrying_a_max_cardinality,
        "a_column_declaring_neither": a_column_declaring_neither,
        "a_range_missing_a_bound": a_range_missing_a_bound,
    }


@pytest.mark.parametrize("name", sorted(_mutations()))
def test_a_malformed_declaration_is_refused_with_a_code_in_the_right_band(
    document, expose, name
) -> None:
    raw = _block(document)
    _mutations()[name](raw)
    with pytest.raises(FluxError) as caught:
        parse_fidelity_block(raw, expose=expose)
    assert caught.value.code.code in ("FLX-5030", "FLX-5032", "FLX-5033", "FLX-5034")
    assert caught.value.detail, "a refusal says what it saw"


def test_an_expose_entry_that_is_not_a_mapping_is_skipped(document) -> None:
    """A malformed neighbour must not stop the named expose being found."""
    from flux_engine.concordance.predicate import expose_from_contract

    contract = copy.deepcopy(dict(document["contract"]))
    contract["exposes"] = ["not a mapping", *contract["exposes"]]
    read = expose_from_contract(contract, document["expose"]["exposeId"])
    assert list(read.column_names) == list(document["expose"]["columns"])


def test_a_duration_with_a_quantity_and_no_unit_is_refused() -> None:
    from flux_engine.concordance.predicate import parse_duration_ms

    with pytest.raises(FluxError) as caught:
        parse_duration_ms("P1X")
    assert caught.value.code.code == "FLX-5030"


# ---------------------------------------------------------------------------
# Value classes at statistic time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        (Decimal("NaN"), "non-finite Decimal"),
        ("NaN", "non-finite Decimal"),
        ("not a number", "non-numeric string"),
    ],
)
def test_a_value_the_grid_cannot_read_is_refused_by_class(compiled, value, fragment) -> None:
    with pytest.raises(FluxError) as caught:
        encode_value(value, compiled.block.column("amount"), compiled.block.grid, row_index=0)
    assert caught.value.code.code == "FLX-5005"
    assert fragment in caught.value.detail


def _property(compiled, kind: PropertyKind, *, grouped: bool = False):
    for entry in compiled.properties:
        if entry.declaration.kind is kind and (entry.declaration.group_by is not None) == grouped:
            return entry
    raise AssertionError(f"no {kind.value} property")


def test_a_null_group_key_in_a_fail_column_refuses_the_grouped_property(
    compiled, committed_rows
) -> None:
    rows = [dict(row) for row in committed_rows]
    rows[0]["market"] = None
    entry = _property(compiled, PropertyKind.MEAN, grouped=True)
    with pytest.raises(FluxError) as caught:
        compute_property_statistic(rows, entry, compiled.block)
    assert caught.value.code.code == "FLX-5005"
    assert "market" in caught.value.detail


def test_a_null_group_key_in_an_exclude_column_drops_the_record(
    document, expose, committed_rows
) -> None:
    """The paired positive case: the policy is read from the column, not guessed."""
    from flux_engine.concordance.compiler import compile_fidelity

    raw = copy.deepcopy(dict(document["extension"]))
    raw["fidelity"]["columns"]["market"]["null"] = "exclude"
    raw["fidelity"]["records"]["min"] = 3
    compiled = compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=raw,
        expose=expose,
    )
    rows = [dict(row) for row in committed_rows]
    rows[0]["market"] = None
    entry = _property(compiled, PropertyKind.MEAN, grouped=True)
    statistic = compute_property_statistic(rows, entry, compiled.block)
    assert sum(group.k for group in statistic.groups) == len(rows) - 1


def test_a_null_in_a_support_column_declared_exclude_leaves_the_member_list(
    document, expose, committed_rows
) -> None:
    from flux_engine.concordance.compiler import compile_fidelity

    raw = copy.deepcopy(dict(document["extension"]))
    raw["fidelity"]["columns"]["market"]["null"] = "exclude"
    compiled = compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=raw,
        expose=expose,
    )
    rows = [dict(row) for row in committed_rows]
    rows[0]["market"] = None
    entry = _property(compiled, PropertyKind.SUPPORT)
    statistic = compute_property_statistic(rows, entry, compiled.block)
    assert statistic.groups[0].k == len(rows) - 1


def test_a_null_in_a_support_column_declared_fail_refuses_the_property(
    compiled, committed_rows
) -> None:
    """The paired branch: ``market`` is declared ``null: fail`` in the vectors.

    SEAM-FIDELITY-SPEC §5's fail-closed list is read per column
    declaration, not per property kind -- a categorical column carries the
    same two null policies a numeric one does, and ``fail`` refuses the
    member list rather than shortening it.
    """
    rows = [dict(row) for row in committed_rows]
    rows[0]["market"] = None
    entry = _property(compiled, PropertyKind.SUPPORT)
    with pytest.raises(FluxError) as caught:
        compute_property_statistic(rows, entry, compiled.block)
    assert caught.value.code.code == "FLX-5005"
    assert "market" in caught.value.detail and "null is refused" in caught.value.detail


def test_a_null_half_of_a_correlation_pair_is_read_from_its_own_column(
    document, expose, committed_rows
) -> None:
    from flux_engine.concordance.compiler import compile_fidelity

    raw = copy.deepcopy(dict(document["extension"]))
    raw["fidelity"]["columns"]["attempts"]["null"] = "exclude"
    compiled = compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=raw,
        expose=expose,
    )
    rows = [dict(row) for row in committed_rows]
    rows[0]["attempts"] = None
    entry = _property(compiled, PropertyKind.CORRELATION)
    statistic = compute_property_statistic(rows, entry, compiled.block)
    assert statistic.groups[0].k == len(rows) - 1


def test_a_property_that_forms_no_group_is_refused(document, expose, committed_rows) -> None:
    """Every group key excluded leaves nothing to compare, so the check declines."""
    from flux_engine.concordance.compiler import compile_fidelity

    raw = copy.deepcopy(dict(document["extension"]))
    raw["fidelity"]["columns"]["market"]["null"] = "exclude"
    compiled = compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=raw,
        expose=expose,
    )
    rows = [{**row, "market": None} for row in committed_rows]
    entry = _property(compiled, PropertyKind.MEAN, grouped=True)
    with pytest.raises(FluxError) as caught:
        compute_property_statistic(rows, entry, compiled.block)
    assert caught.value.code.code == "FLX-5005"
    assert "formed no group" in caught.value.detail


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def test_a_null_in_an_exclude_column_commits_the_null_encoding(
    document, expose, committed_rows
) -> None:
    from flux_engine.concordance.compiler import compile_fidelity

    raw = copy.deepcopy(dict(document["extension"]))
    raw["fidelity"]["records"]["min"] = 3
    compiled = compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=raw,
        expose=expose,
    )
    rows = [dict(row) for row in committed_rows]
    rows[0]["amount"] = None
    state = commit_state(
        rows=rows,
        compiled=compiled,
        ordinal=1,
        committed_at=document["committedAt"],
        blinding=bytes(range(32)),
        slice_reference=SliceReference(uri="file:///s", first_line=1, last_line=len(rows)),
    )
    reference = commit_state(
        rows=committed_rows,
        compiled=compiled,
        ordinal=1,
        committed_at=document["committedAt"],
        blinding=bytes(range(32)),
        slice_reference=SliceReference(
            uri="file:///s", first_line=1, last_line=len(committed_rows)
        ),
    )
    assert state.column_root("amount") != reference.column_root("amount")


def test_a_support_column_carrying_a_non_string_refuses_the_commitment(
    document, compiled, committed_rows
) -> None:
    rows = [dict(row) for row in committed_rows]
    rows[0]["market"] = 7
    with pytest.raises(FluxError) as caught:
        commit_state(
            rows=rows,
            compiled=compiled,
            ordinal=1,
            committed_at=document["committedAt"],
            blinding=bytes(range(32)),
            slice_reference=SliceReference(uri="file:///s", first_line=1, last_line=1),
        )
    assert caught.value.code.code == "FLX-5035"


# ---------------------------------------------------------------------------
# The compiler, the verifier, the record, the gate
# ---------------------------------------------------------------------------


def test_a_support_property_bounds_no_intermediate(compiled) -> None:
    entry = _property(compiled, PropertyKind.SUPPORT)
    assert (
        property_magnitudes(block=compiled.block, declaration=entry.declaration, margin=None) == {}
    )


def test_a_two_column_property_bounds_against_the_wider_range(document, expose) -> None:
    """Either column can drive the product, so the wider range is the bound."""
    from flux_engine.concordance.compiler import compile_fidelity

    raw = copy.deepcopy(dict(document["extension"]))
    raw["fidelity"]["columns"]["attempts"]["range"]["max"] = "4096"
    compiled = compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=raw,
        expose=expose,
    )
    entry = _property(compiled, PropertyKind.CORRELATION)
    narrow = compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=document["extension"],
        expose=expose,
    )
    assert (
        entry.magnitudes["value"] > _property(narrow, PropertyKind.CORRELATION).magnitudes["value"]
    )


def test_a_refusal_renders_as_a_document(compiled) -> None:
    refusal = Refusal(code="FLX-5005", reason="a reason", property_id="000:mean:amount")
    assert refusal.to_document() == {
        "code": "FLX-5005",
        "reason": "a reason",
        "propertyId": "000:mean:amount",
    }


def test_a_quantile_witness_that_is_absent_refuses_the_comparison(
    compiled, committed, committed_rows
) -> None:
    import dataclasses
    from types import MappingProxyType

    from flux_engine.concordance.verifier import ClearStatistic

    entry = _property(compiled, PropertyKind.QUANTILE)
    statistic = committed.statistic.properties[entry.property_id]
    blanked = dataclasses.replace(
        statistic,
        groups=tuple(
            dataclasses.replace(group, quantile_witness=None) for group in statistic.groups
        ),
    )
    moved = dataclasses.replace(
        committed,
        statistic=dataclasses.replace(
            committed.statistic,
            properties=MappingProxyType(
                {**committed.statistic.properties, entry.property_id: blanked}
            ),
        ),
    )
    evaluation = ClearStatistic(
        compiled=compiled, committed=moved, synthetic_rows=committed_rows
    ).evaluate()
    assert entry.property_id in {refusal.property_id for refusal in evaluation.refusals}


def test_a_quantile_witness_with_impossible_ranks_refuses_the_comparison(
    compiled, committed, committed_rows
) -> None:
    import dataclasses
    from types import MappingProxyType

    from flux_engine.concordance.verifier import ClearStatistic

    entry = _property(compiled, PropertyKind.QUANTILE)
    statistic = committed.statistic.properties[entry.property_id]
    lying = dataclasses.replace(
        statistic,
        groups=tuple(
            dataclasses.replace(group, quantile_below=0, quantile_at_or_below=0)
            for group in statistic.groups
        ),
    )
    moved = dataclasses.replace(
        committed,
        statistic=dataclasses.replace(
            committed.statistic,
            properties=MappingProxyType(
                {**committed.statistic.properties, entry.property_id: lying}
            ),
        ),
    )
    evaluation = ClearStatistic(
        compiled=compiled, committed=moved, synthetic_rows=committed_rows
    ).evaluate()
    reasons = [
        refusal.reason
        for refusal in evaluation.refusals
        if refusal.property_id == entry.property_id
    ]
    assert reasons and "ranks the order statistic requires" in reasons[0]


def test_a_record_whose_properties_are_not_a_list_is_refused(
    compiled, committed, committed_rows, signer
) -> None:
    from flux_engine.concordance.record import SyntheticReference, build_record
    from flux_engine.concordance.verifier import ClearStatistic

    record = build_record(
        compiled=compiled,
        committed=committed,
        evaluation=ClearStatistic(
            compiled=compiled, committed=committed, synthetic_rows=committed_rows
        ).evaluate(),
        synthetic=SyntheticReference(
            output_digest="sha256:" + "a" * 64, rows=8, generator_digest="sha256:" + "b" * 64
        ),
        product_ref="p",
        fluid_version="^0.7.3",
        contract_digest="sha256:" + "c" * 64,
        check_ordinal=1,
        signer=signer,
    )
    document = dict(record.document)
    document["properties"] = "not a list"
    with pytest.raises(FluxError) as caught:
        load_record(document)
    assert caught.value.code.code == "FLX-8002"


def test_a_decision_with_no_evaluation_renders_without_property_rows() -> None:
    from flux_engine.concordance.gate import GateAction, GateCheck

    decision = GateDecision(
        action=GateAction.WITHHOLD,
        expose_id="e",
        checks=(GateCheck("record.shape", False, "malformed"),),
        code="FLX-8001",
    )
    document = decision.to_document()
    assert "properties" not in document
    assert "refusals" not in document
    assert document["checks"][0]["passed"] is False


def test_an_evaluation_reports_its_outside_properties() -> None:
    outcome = PropertyOutcome(
        property_id="p",
        kind=PropertyKind.MEAN,
        columns=("amount",),
        group_by=None,
        tolerance={},
        margin=None,
        epsilon="0",
        outcome="outside",
    )
    evaluation = FidelityEvaluation(expose_id="e", outcomes=(outcome,))
    assert evaluation.outside == (outcome,)
    assert not evaluation.within
    assert not evaluation.refused


def test_a_pre_parsed_block_is_compiled_without_reparsing(document, expose, block) -> None:
    """The seam parses once at validate time and hands the block to plan."""
    from flux_engine.concordance.compiler import compile_fidelity

    compiled = compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=document["extension"],
        expose=expose,
        block=block,
    )
    assert compiled.block is block
    assert compiled.h.value == document["expect"]["H"]


def test_a_two_column_property_takes_the_wider_of_its_two_declared_ranges(document, expose) -> None:
    """``property_magnitudes`` widens onto the second column, never narrows.

    KERNEL-SPEC §12 bounds an intermediate "from the declared ranges";
    for a pair, either column can be the one whose encoded magnitude
    drives the product, so the bound is taken over the wider of the two.
    The vectors declare the correlation's second column over the wider
    range, so the pair's bound is strictly wider than the single-column
    mean's; narrowing that second column past the first moves the bound
    back onto the first and no lower. Every width here is computed from a
    compilation, never typed in.
    """
    from flux_engine.concordance.compiler import compile_fidelity

    declared = compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=document["extension"],
        expose=expose,
    )
    # The mean is declared over the correlation's first column alone, so
    # its bound is what the pair's would be if that column were the wider.
    first_only = _property(declared, PropertyKind.MEAN).magnitudes["value"]
    pair = _property(declared, PropertyKind.CORRELATION).magnitudes["value"]
    assert pair > first_only, "the vectors declare the second column over the wider range"

    raw = copy.deepcopy(dict(document["extension"]))
    raw["fidelity"]["columns"]["attempts"]["range"]["max"] = "2"
    narrowed = compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=raw,
        expose=expose,
    )
    assert _property(narrowed, PropertyKind.CORRELATION).magnitudes["value"] == first_only
    # The single-column property over the untouched column does not move.
    assert _property(narrowed, PropertyKind.MEAN).magnitudes["value"] == first_only


def test_a_null_in_a_single_column_range_declared_fail_refuses_the_property(
    document, expose, committed_rows
) -> None:
    from flux_engine.concordance.compiler import compile_fidelity

    raw = copy.deepcopy(dict(document["extension"]))
    raw["fidelity"]["columns"]["amount"]["null"] = "fail"
    compiled = compile_fidelity(
        contract=document["contract"],
        emit=document["emit"],
        extension=raw,
        expose=expose,
    )
    rows = [dict(row) for row in committed_rows]
    rows[0]["amount"] = None
    entry = _property(compiled, PropertyKind.MEAN)
    with pytest.raises(FluxError) as caught:
        compute_property_statistic(rows, entry, compiled.block)
    assert caught.value.code.code == "FLX-5005"
    assert "amount" in caught.value.detail and "null is refused" in caught.value.detail


def test_a_null_in_the_second_half_of_a_correlation_pair_is_refused(
    compiled, committed_rows
) -> None:
    """``attempts`` is declared ``null: fail``, and it is the pair's second column."""
    rows = [dict(row) for row in committed_rows]
    rows[2]["attempts"] = None
    entry = _property(compiled, PropertyKind.CORRELATION)
    with pytest.raises(FluxError) as caught:
        compute_property_statistic(rows, entry, compiled.block)
    assert caught.value.code.code == "FLX-5005"
    assert "attempts" in caught.value.detail


def test_a_tolerance_value_that_is_not_a_decimal_is_refused(document, expose) -> None:
    raw = _block(document)
    raw["properties"][0]["tolerance"]["value"] = "two per cent"
    _refuses(raw, expose, "FLX-5030")


def test_a_non_distance_tolerance_carrying_a_measure_is_refused(document, expose) -> None:
    raw = _block(document)
    raw["properties"][0]["tolerance"]["measure"] = "totalVariation"
    error = _refuses(raw, expose, "FLX-5030")
    assert "only a distance tolerance carries a measure" in error.detail


def test_a_count_tolerance_carrying_a_value_is_refused(document, expose) -> None:
    raw = _block(document)
    for entry in raw["properties"]:
        if entry["property"] == PropertyKind.SUPPORT.value:
            entry["tolerance"] = {"type": "count", "max": 0, "value": "1"}
    error = _refuses(raw, expose, "FLX-5030")
    assert "has no value" in error.detail
