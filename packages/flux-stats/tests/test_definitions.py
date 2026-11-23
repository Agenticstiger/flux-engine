# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The seven definitions and the registry's reader, pinned at their edges.

``test_registry.py`` shows each definition forming and comparing the
quantities §4 and §5 name. This file pins what a caller reads off the
result beyond the quantities themselves: the group and the record count a
measure carries, the cells a comparison reads on each side, the length
check between a support and its counts, the assertions that guard a
compiled check, and where a refusal says a spelled group is wrong. It also
covers what a registry caller relies on that the definitions never
exercise: a kind given as an enum member, and a column metric registered,
found and listed (the enum-kind tests are the enum variants of the ones in
``test_registry.py``, which name kinds by string).

The reader's refusals all carry ``FLX-5036`` and open with the location they
name, and several share one location, so a few words of the message tell the
branches apart; those words are pinned, and no more of the prose.

Nothing here imports the engine.
"""

from __future__ import annotations

import enum
from dataclasses import replace
from fractions import Fraction
from typing import Any, Dict, Sequence

import pytest

from flux_stats import (
    Check,
    GroupStatistic,
    MetricDefinition,
    Parameters,
    definition,
    definition_for_kind,
    metric,
    metrics,
    register,
    register_metric,
)
from flux_stats import registry as registry_module
from flux_stats.errors import FluxError
from flux_stats.registry import read_group

pytestmark = pytest.mark.unit

HALF = Fraction(1, 2)


def _measure(kind: str, values: Sequence[Any], parameters: Parameters) -> GroupStatistic:
    return definition_for_kind(kind).measure("g", values, parameters)


# ---------------------------------------------------------------------------
# What a measure carries besides the quantities
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, values, parameters",
    [
        ("mean", [3, 5, 7], Parameters()),
        ("marginalHistogram", [0, 1, 3], Parameters(edges=(0, 2, 4))),
        ("levelHistogram", [2, 0, 2, 1], Parameters(level_count=3)),
        ("support", ["IE", "GB", "IE"], Parameters()),
        ("correlation", [(1, 2), (3, 5), (4, 4)], Parameters()),
        ("quantile", [9, 1, 5, 5], Parameters(level=HALF)),
    ],
)
def test_every_measure_names_its_group_and_counts_its_records(
    kind: str, values: Sequence[Any], parameters: Parameters
) -> None:
    state = _measure(kind, values, parameters)
    assert state.group == "g"
    assert state.k == len(values)


def test_the_support_counts_line_up_with_the_sorted_support() -> None:
    state = _measure("support", ["b", "a", "b", "c", "b"], Parameters())
    assert state.support == ("a", "b", "c")
    assert state.support_counts == (1, 3, 1)


# ---------------------------------------------------------------------------
# What a comparison reads on each side
# ---------------------------------------------------------------------------


def _check(**fields: Any) -> Check:
    return Check(subject="p0", **fields)


def test_a_level_histogram_reads_the_reference_s_level_counts_not_its_bins() -> None:
    distance = _check(tolerance="distance", margin=Fraction(1, 10))
    levels = definition_for_kind("levelHistogram")
    bins = definition_for_kind("marginalHistogram")
    # One side whose bin counts and level counts disagree, compared with an
    # identical copy of itself: a statistic that reads its own cells on both
    # sides sees no distance, one that reads the wrong cells on the
    # reference side sees the gap between the two tables.
    state = GroupStatistic(group="", k=10, bin_counts=(5, 5), level_counts=(9, 1))
    assert levels.compare(state, state, distance) is True
    assert bins.compare(state, state, distance) is True
    swapped = replace(state, bin_counts=(9, 1), level_counts=(5, 5))
    assert levels.compare(state, swapped, distance) is False
    assert bins.compare(state, swapped, distance) is False


def test_non_members_are_counted_in_records_and_read_by_the_comparison() -> None:
    from flux_stats.definitions import non_member_records

    support = definition_for_kind("support")
    reference = _measure("support", ["GB", "IE"], Parameters())
    # Two strange values, held by four of the six records.
    synthetic = _measure("support", ["FR", "FR", "FR", "GB", "US", "IE"], Parameters())
    assert non_member_records(synthetic, reference) == 4
    assert non_member_records(reference, reference) == 0
    assert support.compare(synthetic, reference, _check(tolerance="count", max_non_members=4))
    assert not support.compare(synthetic, reference, _check(tolerance="count", max_non_members=3))


def test_a_support_whose_counts_do_not_match_its_members_is_a_caller_error() -> None:
    support = definition_for_kind("support")
    reference = GroupStatistic(group="", k=3, support=("GB",))
    short = GroupStatistic(group="", k=3, support=("FR", "IE"), support_counts=(3,))
    with pytest.raises(ValueError):
        support.compare(short, reference, _check(tolerance="count", max_non_members=9))
    long = GroupStatistic(group="", k=3, support=("FR",), support_counts=(2, 1))
    with pytest.raises(ValueError):
        support.compare(long, reference, _check(tolerance="count", max_non_members=9))


def test_a_quantile_check_must_carry_its_level_and_its_margin() -> None:
    quantile = definition_for_kind("quantile")
    state = _measure("quantile", [1, 2, 3], Parameters(level=HALF))
    complete = _check(tolerance="absolute", margin_units=0, level=HALF)
    assert quantile.compare(state, state, complete) is True
    for broken in (replace(complete, level=None), replace(complete, margin_units=None)):
        with pytest.raises(AssertionError):
            quantile.compare(state, state, broken)


# ---------------------------------------------------------------------------
# A kind spelled as an enum member
# ---------------------------------------------------------------------------


class _Kind(enum.Enum):
    """Stands for the engine's ``PropertyKind``: a member whose value is the spelling."""

    MEDIAN = "median"
    MEAN = "mean"


def test_a_kind_given_as_an_enum_member_is_read_by_its_value() -> None:
    assert definition_for_kind(_Kind.MEAN) is definition("mean/1")
    with pytest.raises(KeyError) as unserved:
        definition_for_kind(_Kind.MEDIAN)
    assert unserved.value.args[0].startswith("no statistic serves kind 'median'; registered: ")


def test_a_definition_registered_under_an_enum_kind_is_keyed_by_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_module, "_DEFINITIONS", dict(registry_module._DEFINITIONS))
    monkeypatch.setattr(registry_module, "_BY_KIND", dict(registry_module._BY_KIND))
    median = register(
        replace(definition("mean/1"), identifier="median/1", kind=_Kind.MEDIAN)  # type: ignore[arg-type]
    )
    assert definition_for_kind("median") is median
    assert definition_for_kind(_Kind.MEDIAN) is median
    with pytest.raises(ValueError, match="kind 'median' is already measured by median/1"):
        register(replace(median, identifier="median/2"))


# ---------------------------------------------------------------------------
# Column metrics in the registry
# ---------------------------------------------------------------------------


def _spell(value: Any) -> Dict[str, Any]:
    return {"value": str(value)}


def _read(document: Any, where: str) -> Any:
    return document["value"]


def _measure_nothing(values: Sequence[Any], levels: Any) -> Any:
    return len(values)


def _a_metric(name: str, identifier: str) -> MetricDefinition:
    return MetricDefinition(
        identifier=identifier,
        name=name,
        summary="a metric for the registry's own tests",
        domains=frozenset({"integer"}),
        measure=_measure_nothing,
        spell=_spell,
        read=_read,
    )


@pytest.fixture
def private_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registers into a copy of the metric table, so no other test sees the entry."""
    monkeypatch.setattr(registry_module, "_METRICS", dict(registry_module._METRICS))


def test_a_registered_metric_is_found_by_its_identifier_and_listed(
    private_metrics: None,
) -> None:
    before = [entry.identifier for entry in metrics()]
    entry = register_metric(_a_metric("spread", "spread/2"))
    assert metric("spread/2") is entry
    after = [found.identifier for found in metrics()]
    assert after == sorted(before + ["spread/2"])
    # Registering the same object again changes nothing.
    assert register_metric(entry) is entry
    assert [found.identifier for found in metrics()] == after


def test_a_metric_takes_its_name_from_the_identifier_s_name_part(
    private_metrics: None,
) -> None:
    with pytest.raises(ValueError, match="'spread'"):
        register_metric(_a_metric("spread", "range/1"))
    with pytest.raises(ValueError, match="'1'"):
        register_metric(_a_metric("1", "spread/1"))
    with pytest.raises(KeyError):
        metric("range/1")
    assert register_metric(_a_metric("range", "range/1")) is metric("range/1")


def test_an_unknown_metric_names_what_is_registered() -> None:
    identifiers = [entry.identifier for entry in metrics()]
    with pytest.raises(KeyError) as unknown:
        metric("nothing/1")
    assert unknown.value.args == (
        f"no metric is registered as 'nothing/1'; registered: {identifiers}",
    )


# ---------------------------------------------------------------------------
# Reading a spelled group back, and where a refusal says it is wrong
# ---------------------------------------------------------------------------

WHERE = "doc.groups[0]"
DECOMPOSED = "e" + chr(0x301)


def _refusal(entry_id: str, document: Any) -> str:
    """The detail of the FLX-5036 refusal reading *document* raises."""
    with pytest.raises(FluxError) as raised:
        read_group(definition(entry_id), document, WHERE)
    assert raised.value.code.code == "FLX-5036"
    return raised.value.detail


def test_every_definition_reads_back_what_it_spells() -> None:
    states = {
        "mean/1": _measure("mean", [3, 5], Parameters()),
        "marginalHistogram/1": _measure("marginalHistogram", [0, 3], Parameters(edges=(0, 2, 4))),
        "levelHistogram/1": _measure("levelHistogram", [1, 1], Parameters(level_count=2)),
        "support/1": _measure("support", ["a", "b"], Parameters()),
        "correlation/1": _measure("correlation", [(1, 2), (2, 1)], Parameters()),
        "quantile/1": _measure("quantile", [4, 8], Parameters(level=HALF)),
    }
    for identifier, state in states.items():
        entry = definition(identifier)
        document = state.to_document(entry)
        read = read_group(entry, document, WHERE)
        assert read.to_document(entry) == document
        assert (read.group, read.k) == ("g", 2)


def test_a_quantile_group_with_no_witness_reads_back_as_none() -> None:
    entry = definition("quantile/1")
    empty = GroupStatistic(group="", k=0)
    document = empty.to_document(entry.kind)
    assert document["quantileWitness"] is None
    read = read_group(entry, document, WHERE)
    assert read.quantile_witness is None
    assert read == empty


def test_an_ungrouped_statistic_reads_with_the_empty_group_key() -> None:
    entry = definition("mean/1")
    read = read_group(entry, {"group": "", "k": 2, "Sx": "7"}, WHERE)
    assert (read.group, read.k, read.sum_x) == ("", 2, 7)


def test_a_group_key_is_refused_where_it_is_written() -> None:
    entry = definition("mean/1")
    for key, problem in ((DECOMPOSED, "Normalization Form C"), (5, "expected a string")):
        detail = _refusal(entry.identifier, {"group": key, "k": 1, "Sx": "1"})
        assert detail.startswith(f"{WHERE}.group: "), detail
        assert problem in detail


def test_the_keys_a_group_carries_are_exactly_the_definition_s() -> None:
    mean = {"group": "", "k": 1, "Sx": "1"}
    assert read_group(definition("mean/1"), mean, WHERE).sum_x == 1
    refused = [
        _refusal("mean/1", {**mean, "extra": 1}),
        _refusal("mean/1", {"group": "", "k": 1}),
        # One wrong key and one missing key at once is refused as well.
        _refusal("mean/1", {"group": "", "k": 1, "Sy": "1"}),
    ]
    for detail in refused:
        assert detail.startswith(f"{WHERE}: mean/1 groups carry"), detail
    shape = _refusal("mean/1", ["group", "k", "Sx"])
    assert shape.startswith(f"{WHERE}: ") and "list" in shape


def test_a_cell_table_is_refused_where_it_is_written() -> None:
    base = {"group": "", "k": 2}
    for entry_id, key in (
        ("marginalHistogram/1", "binCounts"),
        ("levelHistogram/1", "levelCounts"),
    ):
        for cells, where, problem in (
            ("2", f"{WHERE}.{key}", "expected a list of counts"),
            ([1, None], f"{WHERE}.{key}", "not null"),
            ([1, -1], f"{WHERE}.{key}[1]", "from 0 to"),
        ):
            detail = _refusal(entry_id, {**base, key: cells})
            assert detail.startswith(f"{where}: "), detail
            assert problem in detail


def test_support_members_are_refused_where_they_are_written() -> None:
    base = {"group": "", "k": 2}
    expected = (
        ("a", f"{WHERE}.support", "expected a list of strings"),
        (["a", ""], f"{WHERE}.support[1]", "must not be empty"),
        (["b", "a"], f"{WHERE}.support", "distinct and sorted"),
        (["a", "a"], f"{WHERE}.support", "distinct and sorted"),
    )
    for members, where, problem in expected:
        detail = _refusal("support/1", {**base, "support": members})
        assert detail.startswith(f"{where}: "), detail
        assert problem in detail
    ok = read_group(definition("support/1"), {**base, "support": ["a", "b"]}, WHERE)
    assert ok.support == ("a", "b")


def test_a_quantile_witness_and_its_ranks_are_refused_where_they_are_written() -> None:
    entry = definition("quantile/1")
    good: Dict[str, Any] = {
        "group": "",
        "k": 3,
        "quantileWitness": "5",
        "quantileBelow": 1,
        "quantileAtOrBelow": 2,
    }
    assert read_group(entry, good, WHERE).quantile_witness == 5
    for key, value, problem in (
        ("quantileWitness", "1.5", "an integer string"),
        ("quantileWitness", 5, "is a string"),
        ("quantileBelow", -1, "from 0 to"),
        ("quantileAtOrBelow", "2", "a JSON integer"),
    ):
        detail = _refusal(entry.identifier, {**good, key: value})
        assert detail.startswith(f"{WHERE}.{key}: "), detail
        assert problem in detail


def test_a_group_must_be_written_in_its_one_canonical_spelling() -> None:
    # "007" is not an integer string, "7" is: the reader takes no second spelling.
    detail = _refusal("mean/1", {"group": "", "k": 1, "Sx": "007"})
    assert detail.startswith(f"{WHERE}.Sx: ")
    read = read_group(definition("mean/1"), {"group": "", "k": 1, "Sx": "-7"}, WHERE)
    assert read.sum_x == -7
