# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``quantile/2``: a quantile published as the bin that holds it (ADR 0012).

A quantile property that declares ``bins`` is formed and compared by
``quantile/2``. It takes the order statistic as ``quantile/1`` does, spells
the bin that holds it and the records below and through that bin, and never
the witness. A quantile without ``bins`` stays ``quantile/1``, which
``test_registry.py`` and ``test_definitions.py`` keep pinning unchanged.

This file covers the declaration, the measure, the spelling, the
comparison, the registry's dispatch, the group's round trip and the profile
reader's side: format ``1-1-0``, which holds the statistic, and format
``1-0-0``, which does not. Nothing here imports the engine.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import flux_stats
from flux_stats import (
    Check,
    GroupStatistic,
    Parameters,
    definition,
    definition_for_kind,
    definition_for_property,
    definitions,
    register,
)
from flux_stats.definitions import QUANTILE, QUANTILE_BINNED
from flux_stats.errors import FluxError
from flux_stats.fidelity import (
    DeclaredExpose,
    FidelityBlock,
    PropertyKind,
    measure_group,
    parse_fidelity_block,
)
from flux_stats.fixedpoint import distance_to_bin, quantile_bin_within
from flux_stats.profile import SCHEMA_URL, SCHEMA_URL_1_1_0, load_profile_text, parse_profile
from flux_stats.registry import read_group

pytestmark = pytest.mark.unit

HALF = Fraction(1, 2)

#: Encoded edges of three bins: ``[0, 4)``, ``[4, 8)`` and ``[8, 16]``.
EDGES = (0, 4, 8, 16)


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------

EXPOSE = DeclaredExpose(
    expose_id="recovery_moment",
    column_names=("amount", "market"),
    column_types={"amount": "DECIMAL(18,4)", "market": "STRING"},
)


def _block(**quantile: Any) -> Dict[str, Any]:
    declared: Dict[str, Any] = {
        "property": "quantile",
        "column": "amount",
        "level": "0.5",
        "tolerance": {"type": "absolute", "value": "1"},
    }
    declared.update(quantile)
    return {
        "encoding": {"fractionalBits": 4, "rounding": "floor"},
        "records": {"min": 2, "max": 8},
        "columns": {
            "amount": {"range": {"min": "1", "max": "8"}, "null": "exclude"},
            "market": {"support": {"values": ["ie", "uk"]}, "maxCardinality": 2, "null": "fail"},
        },
        "properties": [
            declared,
            {
                "property": "mean",
                "column": "amount",
                "tolerance": {"type": "absolute", "value": "1"},
            },
        ],
        "budget": {"maxChecksPerState": 3},
        "failureAction": "withholdBundle",
    }


def _parse(raw: Dict[str, Any]) -> FidelityBlock:
    return parse_fidelity_block(raw, expose=EXPOSE)


def _refuses(raw: Dict[str, Any], code: str) -> FluxError:
    with pytest.raises(FluxError) as caught:
        _parse(raw)
    assert caught.value.code.code == code, caught.value
    return caught.value


def test_a_quantile_may_carry_bins_that_partition_the_range() -> None:
    block = _parse(_block(bins=["1", "2", "4", "8"]))
    declaration = block.properties[0]
    assert declaration.kind is PropertyKind.QUANTILE
    assert declaration.bins == (Fraction(1), Fraction(2), Fraction(4), Fraction(8))
    assert declaration.level == HALF
    assert definition_for_property(declaration) is QUANTILE_BINNED


def test_a_quantile_without_bins_stays_quantile_1() -> None:
    declaration = _parse(_block()).properties[0]
    assert declaration.bins == ()
    assert definition_for_property(declaration) is QUANTILE


def test_a_property_of_another_kind_takes_the_definition_of_its_kind() -> None:
    mean = _parse(_block()).properties[1]
    assert definition_for_property(mean) is definition("mean/1")


@pytest.mark.parametrize(
    "bins",
    [
        ["1", "4", "2", "8"],
        ["2", "4", "8"],
        ["1", "2", "4"],
        ["1", "1", "8"],
        ["1"],
        ["0", "8"],
    ],
    ids=["unordered", "starts late", "ends early", "repeats an edge", "one edge", "starts early"],
)
def test_bins_on_a_quantile_that_do_not_partition_the_range_are_refused(
    bins: List[str],
) -> None:
    _refuses(_block(bins=bins), "FLX-5032")


def test_bins_on_a_quantile_are_read_in_the_columns_domain() -> None:
    _refuses(_block(bins=["2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z"]), "FLX-5032")
    _refuses(_block(bins="1,8"), "FLX-5030")


def test_a_quantile_still_declares_its_level() -> None:
    raw = _block(bins=["1", "8"])
    del raw["properties"][0]["level"]
    _refuses(raw, "FLX-5034")


def test_no_other_kind_but_a_histogram_carries_bins() -> None:
    raw = _block()
    raw["properties"][1]["bins"] = ["1", "8"]
    refusal = _refuses(raw, "FLX-5030")
    assert "only a marginalHistogram or a quantile carries bins" in refusal.detail


def test_a_level_histogram_keeps_its_own_refusal_of_bins() -> None:
    raw = _block()
    raw["properties"].append(
        {
            "property": "levelHistogram",
            "column": "market",
            "bins": ["a", "b"],
            "tolerance": {"type": "distance", "measure": "totalVariation", "max": "0.2"},
        }
    )
    assert "carries no bins" in _refuses(raw, "FLX-5030").detail


# ---------------------------------------------------------------------------
# The measure
# ---------------------------------------------------------------------------


def _measure(values: Sequence[int], level: Fraction = HALF) -> GroupStatistic:
    return QUANTILE_BINNED.measure("g", values, Parameters(edges=EDGES, level=level))


def test_the_measure_takes_the_order_statistic_and_the_bin_that_holds_it() -> None:
    # Eight records, level 1/2: the fourth smallest, 7, in [4, 8).
    state = _measure([15, 1, 9, 3, 5, 11, 13, 7])
    assert state.k == 8
    assert (state.quantile_witness, state.quantile_below, state.quantile_at_or_below) == (7, 3, 4)
    assert state.quantile_bin == 1
    assert (state.bin_below, state.bin_through) == (2, 4)
    # The ranks that show the order statistic in the bin: binBelow < j <= binThrough.
    assert state.bin_below < 4 <= state.bin_through


def test_the_measure_agrees_with_quantile_1_on_the_order_statistic() -> None:
    values = [9, 1, 5, 5, 12, 3, 3, 10, 2]
    plain = QUANTILE.measure("g", values, Parameters(level=Fraction(3, 4)))
    binned = _measure(values, Fraction(3, 4))
    assert (binned.quantile_witness, binned.quantile_below, binned.quantile_at_or_below) == (
        plain.quantile_witness,
        plain.quantile_below,
        plain.quantile_at_or_below,
    )


def test_a_witness_exactly_at_an_inner_edge_is_in_the_bin_that_starts_there() -> None:
    state = _measure([1, 3, 5, 8, 9, 10, 12, 14])
    assert state.quantile_witness == 8
    assert state.quantile_bin == 2
    assert (state.bin_below, state.bin_through) == (3, 8)


def test_a_witness_at_the_declared_maximum_is_in_the_last_bin_which_counts_every_record() -> None:
    state = _measure([1, 2, 16, 16], Fraction(1))
    assert state.quantile_witness == 16
    assert state.quantile_bin == 2
    assert (state.bin_below, state.bin_through) == (2, 4)


def test_a_witness_at_the_declared_minimum_is_in_the_first_bin() -> None:
    state = _measure([0, 0, 0, 5], HALF)
    assert (state.quantile_bin, state.bin_below, state.bin_through) == (0, 0, 3)


def test_an_empty_group_forms_no_bin() -> None:
    state = _measure([])
    assert state == GroupStatistic(group="g", k=0)
    assert state.quantile_bin is None


def test_a_measure_without_edges_is_refused() -> None:
    with pytest.raises(FluxError) as caught:
        QUANTILE_BINNED.measure("g", [1, 2], Parameters(level=HALF))
    assert caught.value.code.code == "FLX-5004"


def test_measure_group_reads_the_edges_on_the_grid_and_forms_the_bin() -> None:
    block = _parse(_block(bins=["1", "2", "4", "8"]))
    rows = [{"amount": value} for value in ("1", "3", "5", "7")]
    group = measure_group(rows, [0, 1, 2, 3], "all", block.properties[0], block.columns, block.grid)
    # At four fractional bits the edges are 16, 32, 64 and 128; the second
    # smallest of 16, 48, 80 and 112 is 48, in [32, 64).
    assert group.quantile_witness == 48
    assert (group.quantile_bin, group.bin_below, group.bin_through) == (1, 1, 2)


# ---------------------------------------------------------------------------
# The spelling
# ---------------------------------------------------------------------------


def test_a_group_spells_the_bin_and_its_counts_and_never_the_witness() -> None:
    state = _measure([15, 1, 9, 3, 5, 11, 13, 7])
    document = state.to_document(QUANTILE_BINNED)
    assert document == {"group": "g", "k": 8, "quantileBin": 1, "binBelow": 2, "binThrough": 4}
    assert all(type(document[key]) is int for key in ("k", "quantileBin", "binBelow", "binThrough"))
    assert "7" not in json.dumps(document).replace('"g"', "")
    assert "quantileWitness" not in document


def test_a_binned_group_is_spelled_by_its_definition_and_never_by_its_kind() -> None:
    # The kind names quantile/1, whose spelling carries the witness.
    state = _measure([1, 2, 3, 4])
    with pytest.raises(ValueError, match="spelled by its definition"):
        state.to_document("quantile")
    assert "quantileWitness" not in state.to_document(QUANTILE_BINNED)
    unbinned = GroupStatistic(group="", k=1, quantile_witness=3, quantile_at_or_below=1)
    assert "quantileWitness" in unbinned.to_document("quantile")


def test_a_group_with_no_bin_spells_null_and_reads_back() -> None:
    empty = GroupStatistic(group="", k=0)
    document = empty.to_document(QUANTILE_BINNED)
    assert document["quantileBin"] is None
    assert read_group(QUANTILE_BINNED, document, "doc.groups[0]") == empty


def test_a_binned_group_reads_back_as_it_was_spelled() -> None:
    state = _measure([15, 1, 9, 3, 5, 11, 13, 7])
    document = state.to_document(QUANTILE_BINNED)
    read = read_group(QUANTILE_BINNED, document, "doc.groups[0]")
    assert (read.k, read.quantile_bin, read.bin_below, read.bin_through) == (8, 1, 2, 4)
    assert read.quantile_witness is None, "the witness never leaves the boundary"
    assert read.to_document(QUANTILE_BINNED) == document


@pytest.mark.parametrize(
    "change",
    [
        {"quantileWitness": "7"},
        {"quantileBin": "1"},
        {"quantileBin": True},
        {"binBelow": 2.0},
        {"binThrough": -1},
    ],
    ids=["a witness", "a bin as text", "a bin as a boolean", "a float count", "a negative count"],
)
def test_a_binned_group_in_any_other_spelling_is_refused(change: Dict[str, Any]) -> None:
    document = {
        **_measure([15, 1, 9, 3, 5, 11, 13, 7]).to_document(QUANTILE_BINNED),
        **change,
    }
    with pytest.raises(FluxError) as caught:
        read_group(QUANTILE_BINNED, document, "doc.groups[0]")
    assert caught.value.code.code == "FLX-5036"
    assert caught.value.detail.startswith("doc.groups[0]")


# ---------------------------------------------------------------------------
# The grid helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "units, index, distance",
    [
        (4, 1, 0),
        (7, 1, 0),
        (3, 1, 1),
        (0, 1, 4),
        (8, 1, 1),
        (10, 1, 3),
        (8, 2, 0),
        (16, 2, 0),
        (17, 2, 1),
        (7, 2, 1),
        (-3, 0, 3),
        (0, 0, 0),
        (3, 0, 0),
        (4, 0, 1),
    ],
)
def test_the_distance_to_a_bin_counts_grid_steps_from_its_nearest_grid_value(
    units: int, index: int, distance: int
) -> None:
    assert distance_to_bin(units, EDGES, index) == distance


@pytest.mark.parametrize("index", [-1, 3, 7])
def test_a_bin_index_outside_the_edges_is_refused(index: int) -> None:
    with pytest.raises(FluxError) as caught:
        distance_to_bin(5, EDGES, index)
    assert caught.value.code.code == "FLX-5005"


def test_one_edge_names_no_bin() -> None:
    with pytest.raises(FluxError) as caught:
        distance_to_bin(5, (0,), 0)
    assert caught.value.code.code == "FLX-5005"


def test_a_negative_margin_is_refused() -> None:
    with pytest.raises(FluxError) as caught:
        quantile_bin_within(witness=5, edges=EDGES, bin_index=1, tolerance_units=-1)
    assert caught.value.code.code == "FLX-5004"


def test_a_witness_is_within_a_bin_by_the_margin_either_side() -> None:
    def within(witness: int, index: int, margin: int) -> bool:
        return quantile_bin_within(
            witness=witness, edges=EDGES, bin_index=index, tolerance_units=margin
        )

    assert within(5, 1, 0) and within(4, 1, 0) and within(7, 1, 0)
    assert not within(3, 1, 0) and not within(8, 1, 0)
    assert within(2, 1, 2) and not within(1, 1, 2)
    assert within(9, 1, 2) and not within(10, 1, 2)


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def _check(margin: int = 0, **fields: Any) -> Check:
    values: Dict[str, Any] = {
        "tolerance": "absolute",
        "margin_units": margin,
        "level": HALF,
        "edges": EDGES,
    }
    values.update(fields)
    return Check(subject="p0", **values)


def _reference(index: int, below: int, through: int, k: int = 8) -> GroupStatistic:
    return GroupStatistic(group="", k=k, quantile_bin=index, bin_below=below, bin_through=through)


def _synthetic(witness: int) -> GroupStatistic:
    """Eight records, every one the witness: its ranks show it is the order statistic."""
    return _measure([witness] * 8)


#: The reference side every case below compares with: bin 1, ``[4, 8)``, whose
#: counts show the fourth record of eight in it (``2 < 4 <= 4``).
INSIDE = _reference(1, 2, 4)


def _compare(synthetic: GroupStatistic, reference: GroupStatistic, check: Check) -> bool:
    return QUANTILE_BINNED.compare(synthetic, reference, check)


@pytest.mark.parametrize("witness", [4, 5, 7])
def test_a_witness_inside_the_reference_bin_is_within_at_no_margin(witness: int) -> None:
    assert _compare(_synthetic(witness), INSIDE, _check(0)) is True


def test_a_witness_just_outside_the_bin_is_outside_at_no_margin() -> None:
    assert _compare(_synthetic(3), INSIDE, _check(0)) is False
    assert _compare(_synthetic(8), INSIDE, _check(0)) is False


def test_a_witness_exactly_margin_steps_below_the_lower_edge_is_within_and_one_more_is_not() -> (
    None
):
    assert _compare(_synthetic(2), INSIDE, _check(2)) is True
    assert _compare(_synthetic(1), INSIDE, _check(2)) is False


def test_a_witness_exactly_margin_steps_above_the_top_is_within_and_one_more_is_not() -> None:
    # The top of [4, 8) is 7.
    assert _compare(_synthetic(9), INSIDE, _check(2)) is True
    assert _compare(_synthetic(10), INSIDE, _check(2)) is False


def test_the_last_bin_is_closed_at_the_top() -> None:
    last = _reference(2, 3, 8)
    assert _compare(_synthetic(16), last, _check(0)) is True
    assert _compare(_synthetic(8), last, _check(0)) is True
    assert _compare(_synthetic(7), last, _check(0)) is False
    assert _compare(_synthetic(17), last, _check(0)) is False
    assert _compare(_synthetic(18), last, _check(1)) is False
    assert _compare(_synthetic(17), last, _check(1)) is True


def test_a_witness_at_an_inner_edge_belongs_to_the_bin_above() -> None:
    # 8 is the first value of bin 2 and one step above the top of bin 1.
    assert _compare(_synthetic(8), _reference(2, 3, 8), _check(0)) is True
    assert _compare(_synthetic(8), INSIDE, _check(0)) is False
    assert _compare(_synthetic(8), INSIDE, _check(1)) is True


def test_a_bin_whose_counts_do_not_show_the_order_statistic_in_it_is_refused() -> None:
    for below, through in ((4, 8), (0, 3)):
        with pytest.raises(FluxError) as caught:
            _compare(_synthetic(5), _reference(1, below, through), _check(0))
        assert caught.value.code.code == "FLX-5005"
        assert "committed side's quantile bin for p0" in caught.value.detail
        assert "does not carry the ranks" in caught.value.detail


def test_counts_that_are_not_a_sub_count_of_the_records_are_refused() -> None:
    with pytest.raises(FluxError) as caught:
        _compare(_synthetic(5), _reference(1, 2, 9), _check(0))
    assert caught.value.code.code == "FLX-5005"


def test_a_reference_that_offers_no_bin_names_its_side_and_property() -> None:
    empty = GroupStatistic(group="", k=0)
    with pytest.raises(FluxError, match="committed side offers no quantile bin for p0") as caught:
        _compare(_synthetic(5), empty, _check(0))
    assert caught.value.code.code == "FLX-5005"
    with pytest.raises(FluxError, match="profile side offers no quantile bin for p0"):
        _compare(_synthetic(5), empty, _check(0, reference_side="profile"))


@pytest.mark.parametrize("index", [3, 4, -1])
def test_a_bin_index_that_names_no_declared_bin_is_refused(index: int) -> None:
    with pytest.raises(FluxError, match="names no declared bin") as caught:
        _compare(_synthetic(5), _reference(index, 2, 4), _check(0))
    assert caught.value.code.code == "FLX-5005"


def test_a_synthetic_side_with_no_witness_or_a_false_one_is_refused() -> None:
    with pytest.raises(FluxError, match="synthetic side offers no quantile witness for p0"):
        _compare(GroupStatistic(group="", k=0), INSIDE, _check(0))
    lying = replace(_synthetic(5), quantile_below=4)
    with pytest.raises(FluxError, match="synthetic side's quantile witness for p0"):
        _compare(lying, INSIDE, _check(0))


def test_a_negative_margin_is_refused_by_the_comparison() -> None:
    with pytest.raises(FluxError) as caught:
        _compare(_synthetic(5), INSIDE, _check(-1))
    assert caught.value.code.code == "FLX-5004"


def test_the_comparison_needs_the_level_the_margin_and_the_edges() -> None:
    for broken in (_check(0, level=None), _check(0, margin_units=None)):
        with pytest.raises(AssertionError):
            _compare(_synthetic(5), INSIDE, broken)
    with pytest.raises(FluxError, match="names no declared bin"):
        _compare(_synthetic(5), INSIDE, _check(0, edges=()))


def test_a_synthetic_quantile_within_the_margin_of_the_reference_quantile_always_passes() -> None:
    """The bin only widens what passes: a value near the reference witness is near its bin."""
    for reference_witness in range(0, 17):
        reference = _measure([reference_witness] * 8)
        bin_reference = _reference(
            reference.quantile_bin or 0, reference.bin_below, reference.bin_through
        )
        for margin in (0, 1, 3):
            for synthetic in range(
                max(0, reference_witness - margin), reference_witness + margin + 1
            ):
                assert _compare(_synthetic(synthetic), bin_reference, _check(margin)), (
                    reference_witness,
                    synthetic,
                    margin,
                )


# ---------------------------------------------------------------------------
# The registry's dispatch
# ---------------------------------------------------------------------------


def test_a_quantile_without_bins_is_quantile_1_and_with_them_quantile_2() -> None:
    assert definition_for_kind("quantile") is QUANTILE
    assert definition_for_kind("quantile", binned=False) is QUANTILE
    assert definition_for_kind("quantile", binned=True) is QUANTILE_BINNED
    assert QUANTILE_BINNED.identifier == "quantile/2" and QUANTILE_BINNED.binned
    assert QUANTILE.identifier == "quantile/1" and not QUANTILE.binned
    assert definition("quantile/2") is QUANTILE_BINNED


def test_only_a_quantile_has_a_binned_form() -> None:
    assert [entry.identifier for entry in definitions() if entry.binned] == ["quantile/2"]
    for kind in ("mean", "marginalHistogram", "levelHistogram", "support", "correlation"):
        with pytest.raises(KeyError, match=f"no statistic serves kind '{kind}' with bins"):
            definition_for_kind(kind, binned=True)


def test_the_property_helper_is_exported_with_the_package() -> None:
    assert flux_stats.definition_for_property is definition_for_property


def test_a_second_definition_for_one_kind_and_form_is_refused() -> None:
    with pytest.raises(
        ValueError, match="kind 'quantile' with bins is already measured by quantile/2"
    ):
        register(replace(QUANTILE_BINNED, identifier="quantile/3"))
    with pytest.raises(ValueError, match="kind 'quantile' is already measured by quantile/1"):
        register(replace(QUANTILE, identifier="quantile/3"))
    with pytest.raises(KeyError):
        definition("quantile/3")


# ---------------------------------------------------------------------------
# The profile document
# ---------------------------------------------------------------------------


def _repository_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _repository_root() / "docs" / "spec"
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-example.json").read_text(encoding="utf-8"))
SCHEMA: Dict[str, Any] = json.loads((SPEC / "profile-1.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

TENURE = ("tables", "subscriber", "columns", "tenure_months")
BINNED_STATISTIC: Dict[str, Any] = {
    "definition": "quantile/2",
    "level": "0.5",
    "bins": ["0", "12", "24", "48", "120"],
    "sampleSize": {"min": 1000, "max": 9999},
    "disclosure": "acro-default",
    "groups": [
        {
            "group": "",
            "k": 1195,
            "quantileBin": 2,
            "binBelow": 565,
            "binThrough": 1030,
            "status": "F",
        }
    ],
}


def _statistics(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    found = document
    for step in TENURE:
        found = found[step]
    statistics: List[Dict[str, Any]] = found["statistics"]
    return statistics


def _binned_document(*, url: str = SCHEMA_URL_1_1_0) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    document["_schemaURL"] = url
    _statistics(document).append(copy.deepcopy(BINNED_STATISTIC))
    return document


def _last(document: Dict[str, Any]) -> Dict[str, Any]:
    return _statistics(document)[-1]


WHERE = "profile.tables.subscriber.columns.tenure_months.statistics[4]"


def _refused(document: Dict[str, Any]) -> FluxError:
    with pytest.raises(FluxError) as caught:
        parse_profile(document)
    assert caught.value.code.code == "FLX-5036"
    return caught.value


def test_the_two_formats_are_the_two_urls_the_specification_names() -> None:
    assert SCHEMA_URL.endswith("/profile/1-0-0/FluxProfile.json")
    assert SCHEMA_URL_1_1_0.endswith("/profile/1-1-0/FluxProfile.json")
    assert SCHEMA["$id"] == SCHEMA_URL_1_1_0


def test_a_1_1_0_profile_reads_a_quantile_2_statistic() -> None:
    profile = parse_profile(_binned_document())
    (statistic,) = [
        found
        for found in profile.column_statistics("subscriber", "tenure_months")
        if found.identifier == "quantile/2"
    ]
    assert statistic.definition is QUANTILE_BINNED
    assert statistic.bins == tuple(Fraction(edge) for edge in ("0", "12", "24", "48", "120"))
    assert statistic.level == HALF
    (group,) = statistic.groups
    assert group.published and group.statistic is not None
    assert (group.statistic.quantile_bin, group.statistic.bin_below) == (2, 565)
    assert (group.statistic.bin_through, group.statistic.k) == (1030, 1195)
    assert group.statistic.quantile_witness is None


def test_the_schema_accepts_a_1_1_0_document_that_holds_a_quantile_2_statistic() -> None:
    document = _binned_document()
    assert not list(VALIDATOR.iter_errors(document))
    profile, parsed = load_profile_text(json.dumps(document))
    assert parsed == document and profile.tables


def test_a_document_that_names_1_0_0_still_reads_and_validates_as_before() -> None:
    assert EXAMPLE["_schemaURL"] == SCHEMA_URL
    assert not list(VALIDATOR.iter_errors(EXAMPLE))
    assert parse_profile(copy.deepcopy(EXAMPLE)).tables


def test_a_1_1_0_document_may_hold_none_of_the_new_statistic() -> None:
    document = copy.deepcopy(EXAMPLE)
    document["_schemaURL"] = SCHEMA_URL_1_1_0
    assert not list(VALIDATOR.iter_errors(document))
    assert parse_profile(document).tables


def test_a_1_0_0_document_that_holds_a_quantile_2_statistic_is_refused() -> None:
    refusal = _refused(_binned_document(url=SCHEMA_URL))
    assert refusal.detail.startswith(f"{WHERE}.definition")
    assert "1-0-0" in refusal.detail


def test_a_quantile_2_statistic_in_a_relationship_is_read_and_validates() -> None:
    document = _binned_document()
    document["relationships"][0]["statistics"].append(copy.deepcopy(BINNED_STATISTIC))
    assert not list(VALIDATOR.iter_errors(document))
    profile = parse_profile(document)
    assert profile.relationships[0].statistics[-1].identifier == "quantile/2"


def test_an_unknown_format_url_is_refused() -> None:
    document = _binned_document()
    document["_schemaURL"] = SCHEMA_URL_1_1_0.replace("1-1-0", "1-6-0")
    assert _refused(document).detail.startswith("profile._schemaURL")
    assert list(VALIDATOR.iter_errors(document)), "the schema refuses it too"


def _drop(key: str) -> Callable[[Dict[str, Any]], None]:
    def change(statistic: Dict[str, Any]) -> None:
        del statistic[key]

    return change


def _set_group(**fields: Any) -> Callable[[Dict[str, Any]], None]:
    def change(statistic: Dict[str, Any]) -> None:
        statistic["groups"][0].update(fields)

    return change


def _drop_from_group(key: str) -> Callable[[Dict[str, Any]], None]:
    def change(statistic: Dict[str, Any]) -> None:
        del statistic["groups"][0][key]

    return change


#: (change, where the reader says the problem is, whether the schema refuses it too)
BROKEN = [
    pytest.param(_drop("bins"), WHERE, True, id="bins missing"),
    pytest.param(_drop("level"), WHERE, True, id="level missing"),
    pytest.param(
        lambda statistic: statistic.update(bins=["0"]), f"{WHERE}.bins", True, id="one edge"
    ),
    pytest.param(
        lambda statistic: statistic.update(bins=["0", "48", "12", "120"]),
        f"{WHERE}.bins",
        False,
        id="edges out of order",
    ),
    pytest.param(
        lambda statistic: statistic.update(level="1.5"), f"{WHERE}.level", True, id="level above 1"
    ),
    pytest.param(
        _set_group(quantileBin=4), f"{WHERE}.groups[0].quantileBin", False, id="bin past the last"
    ),
    pytest.param(
        _set_group(quantileBin=None), f"{WHERE}.groups[0].quantileBin", False, id="bin null"
    ),
    pytest.param(
        _set_group(k=0, quantileBin=None, binBelow=5, binThrough=9),
        f"{WHERE}.groups[0].binThrough",
        False,
        id="an empty group that counts records",
    ),
    pytest.param(
        _set_group(quantileBin=0, binBelow=5, binThrough=300),
        f"{WHERE}.groups[0].binBelow",
        False,
        id="records below the first bin",
    ),
    pytest.param(
        _set_group(quantileBin=3, binBelow=1100, binThrough=1190),
        f"{WHERE}.groups[0].binThrough",
        False,
        id="records above the last bin",
    ),
    pytest.param(
        _set_group(quantileBin=-1), f"{WHERE}.groups[0].quantileBin", True, id="bin negative"
    ),
    pytest.param(
        _set_group(quantileBin="2"), f"{WHERE}.groups[0].quantileBin", True, id="bin as text"
    ),
    pytest.param(
        _set_group(binBelow=1031), f"{WHERE}.groups[0].binThrough", False, id="below above through"
    ),
    pytest.param(
        _set_group(binThrough=1196), f"{WHERE}.groups[0].binThrough", False, id="through above k"
    ),
    pytest.param(_set_group(quantileWitness="1703936"), f"{WHERE}.groups[0]", True, id="a witness"),
    pytest.param(_drop_from_group("binBelow"), f"{WHERE}.groups[0]", True, id="binBelow missing"),
    pytest.param(
        _drop_from_group("binThrough"), f"{WHERE}.groups[0]", True, id="binThrough missing"
    ),
    pytest.param(
        _set_group(cellStatus=["F"]), f"{WHERE}.groups[0].cellStatus", True, id="cell statuses"
    ),
]


def test_an_empty_group_reads_as_quantile_1_s_does_and_is_refused_where_it_is_compared() -> None:
    # quantile/1 reads a group with k 0 and a null witness; quantile/2 reads one
    # with k 0 and a null bin the same way, and the property refuses it (FLX-5005).
    document = _binned_document()
    _set_group(k=0, quantileBin=None, binBelow=0, binThrough=0)(_last(document))
    assert not list(VALIDATOR.iter_errors(document))
    (statistic,) = [
        found
        for found in parse_profile(document).column_statistics("subscriber", "tenure_months")
        if found.identifier == "quantile/2"
    ]
    (group,) = statistic.groups
    assert group.statistic is not None and group.statistic.quantile_bin is None


@pytest.mark.parametrize("change, where, schema_refuses", BROKEN)
def test_a_broken_quantile_2_statistic_is_refused_where_it_breaks(
    change: Callable[[Dict[str, Any]], None], where: str, schema_refuses: bool
) -> None:
    document = _binned_document()
    change(_last(document))
    assert _refused(document).detail.startswith(where)
    if schema_refuses:
        assert list(VALIDATOR.iter_errors(document)), "the schema accepts what the reader refuses"


def test_a_refusal_names_where_and_never_quotes_a_value() -> None:
    document = _binned_document()
    _last(document)["groups"][0]["quantileBin"] = 97531
    assert "97531" not in _refused(document).detail
    document = _binned_document()
    _last(document)["groups"][0]["binThrough"] = 13579000
    assert "13579000" not in _refused(document).detail


def test_a_withheld_quantile_2_group_holds_only_its_group_and_status() -> None:
    document = _binned_document()
    _last(document)["groups"] = [{"group": "", "status": "A"}]
    assert not list(VALIDATOR.iter_errors(document))
    (group,) = parse_profile(document).column_statistics("subscriber", "tenure_months")[-1].groups
    assert not group.published and group.statistic is None
    document = _binned_document()
    _last(document)["groups"] = [{"group": "", "k": 1195, "status": "A"}]
    assert _refused(document).detail.startswith(f"{WHERE}.groups[0]")
    assert list(VALIDATOR.iter_errors(document))


def test_a_quantile_1_statistic_still_declares_no_bins() -> None:
    document = copy.deepcopy(EXAMPLE)
    plain = next(entry for entry in _statistics(document) if entry["definition"] == "quantile/1")
    plain["bins"] = ["0", "120"]
    assert _refused(document).detail.endswith("quantile/1 declares no bins")
    assert list(VALIDATOR.iter_errors(document))


def test_quantile_1_and_quantile_2_may_describe_one_column_and_level() -> None:
    document = _binned_document()
    document["_schemaURL"] = SCHEMA_URL_1_1_0
    found = parse_profile(document).column_statistics("subscriber", "tenure_months")
    assert {entry.identifier for entry in found if entry.level == HALF} == {
        "quantile/1",
        "quantile/2",
    }
    document = _binned_document()
    _statistics(document).append(copy.deepcopy(BINNED_STATISTIC))
    assert _refused(document).detail.startswith(f"{WHERE[:-2]}5]")
