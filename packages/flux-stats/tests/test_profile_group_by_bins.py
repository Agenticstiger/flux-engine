# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``groupByBins`` on a column statistic (profile format 1-4-0).

A statistic grouped by the bins of another number or timestamp column: the keys, the one grouping a
statistic may hold, the disclosure rules group by group, noise on a count table so grouped, and
every refusal of PROFILE-SPEC.md section 3.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from flux_stats.errors import FluxError
from flux_stats.profile import SCHEMA_URL_1_3_0, BinGrouping, parse_profile

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _spec()
BINS = ["0", "30", "60", "100"]
NOISE = {"mechanism": "gaussian", "scale": "5", "privacyLoss": {"rho": "0.02"}}


def _group(key: str, *counts: int) -> Dict[str, Any]:
    return {"group": key, "status": "F", "k": sum(counts), "binCounts": list(counts)}


def _histogram(**more: Any) -> Dict[str, Any]:
    """A marginal histogram of ``tenure`` grouped by the bins of ``age``, exact."""
    return {
        "definition": "marginalHistogram/1",
        "bins": ["0", "10", "20"],
        "sampleSize": {"min": 1000, "max": 9999},
        "disclosure": "open",
        "groupByBins": {"column": "age", "bins": list(BINS)},
        "groups": [_group("0", 5, 6), _group("30", 7, 8), _group("60", 1, 2)],
    } | more


def _noisy_histogram(**more: Any) -> Dict[str, Any]:
    return (
        _histogram(
            disclosure="noisy",
            noise=copy.deepcopy(NOISE),
            groups=[
                {"group": key, "status": "F", "noisyCounts": ["5", "6"]}
                for key in ("0", "30", "60")
            ],
        )
        | more
    )


def _document(*statistics: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "_producer": "urn:example:t",
        "_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile/1-4-0/FluxProfile.json",
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"open": {}, "noisy": {}, "small": {"minimumCell": 5}},
        "privacy": {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "5"},
            "privacyUnit": {"expose": "t"},
            "publicStructure": True,
        },
        "tables": {
            "t": {
                "columns": {
                    "age": {"domain": "number"},
                    "tenure": {"domain": "number", "statistics": list(statistics)},
                    "joined": {"domain": "timestamp"},
                    "region": {"domain": "category"},
                }
            }
        },
    }
    document.update(extra)
    if "noise" not in json.dumps(statistics):
        del document["privacy"]  # an exact profile states no account
    return document


def _statistic(document: Dict[str, Any]) -> Dict[str, Any]:
    found: Dict[str, Any] = document["tables"]["t"]["columns"]["tenure"]["statistics"][0]
    return found


def _changed(change: Callable[[Dict[str, Any]], None], statistic: Dict[str, Any]) -> Dict[str, Any]:
    document = _document(copy.deepcopy(statistic))
    change(_statistic(document))
    return document


def _refused(document: Dict[str, Any], *words: str) -> str:
    with pytest.raises(FluxError) as raised:
        parse_profile(document)
    message = str(raised.value)
    assert "FLX-5036" in message
    for word in words:
        assert word in message, message
    return message


# ---------------------------------------------------------------------------
# What it reads
# ---------------------------------------------------------------------------


def test_a_grouped_histogram_is_read_with_its_groups_keyed_by_lower_edge() -> None:
    profile = parse_profile(_document(_histogram()))
    (statistic,) = profile.tables["t"].columns["tenure"].statistics
    assert statistic.group_by is None and statistic.group_by_link is None
    assert statistic.group_by_bins == BinGrouping(
        column="age", bins=tuple(Fraction(edge) for edge in BINS)
    )
    assert statistic.group_by_bins.keys == ("0", "30", "60")
    assert [group.key for group in statistic.groups] == ["0", "30", "60"]
    assert statistic.groups[1].statistic is not None
    assert statistic.groups[1].statistic.bin_counts == (7, 8)
    assert statistic.noise is None and not statistic.derived


def test_a_key_in_order_but_not_canonical_names_the_form_it_is_written_in() -> None:
    statistic = _histogram(groups=[_group("0", 1, 1), _group("30.0", 1, 1)])
    _refused(_document(statistic), "groups", "canonical decimal text")


def test_the_key_of_a_bin_is_the_canonical_decimal_text_of_its_lower_edge() -> None:
    edges = ["-0", "0.50", "1.2500", "-5", "10"]
    ordered = ["-5", "-0", "0.50", "1.2500", "10"]
    del edges
    statistic = _histogram(
        groupByBins={"column": "age", "bins": ordered},
        groups=[_group(key, 1, 1) for key in sorted(["-5", "0", "0.5", "1.25"])],
    )
    profile = parse_profile(_document(statistic))
    grouping = profile.tables["t"].columns["tenure"].statistics[0].group_by_bins
    assert grouping is not None and grouping.keys == ("-5", "0", "0.5", "1.25")


@pytest.mark.parametrize("spelling", ["0.50", "-0", "00", "0.5e0", "+30", "30.0", "", " 30"])
def test_a_key_written_in_any_other_spelling_is_refused(spelling: str) -> None:
    statistic = _histogram(groups=[_group("0", 1, 1), _group(spelling, 1, 1)])
    _refused(_document(statistic), "groups")


def test_a_key_that_is_no_bins_lower_edge_is_refused_the_last_edge_included() -> None:
    for key in ("100", "45", "-1"):
        statistic = _histogram(groups=[_group("0", 1, 1), _group(key, 1, 1)])
        _refused(_document(statistic), "groups")


def test_the_null_group_is_never_written() -> None:
    statistic = _histogram(groups=[_group("", 1, 1), _group("0", 1, 1)])
    _refused(_document(statistic), "groups")
    statistic = _histogram(groups=[{"group": "", "status": "A"}])
    _refused(_document(statistic), "groups")


def test_groups_are_distinct_and_sorted_by_key() -> None:
    _refused(_document(_histogram(groups=[_group("30", 1, 1), _group("0", 1, 1)])), "sorted")
    _refused(_document(_histogram(groups=[_group("0", 1, 1), _group("0", 1, 1)])), "sorted")


def test_every_kind_of_column_statistic_may_be_grouped_by_bins() -> None:
    mean = {
        "definition": "mean/1",
        "sampleSize": {"min": 1000},
        "disclosure": "open",
        "groupByBins": {"column": "age", "bins": BINS},
        "groups": [{"group": "0", "k": 5400, "Sx": "1", "status": "F"}],
    }
    levels = {
        "definition": "levelHistogram/1",
        "levels": ["a", "b"],
        "sampleSize": {"min": 1000},
        "disclosure": "open",
        "groupByBins": {"column": "age", "bins": BINS},
        "groups": [{"group": "30", "status": "F", "k": 3, "levelCounts": [1, 2]}],
    }
    document = _document(mean)
    document["tables"]["t"]["columns"]["region"]["statistics"] = [levels]
    table = parse_profile(document).tables["t"]
    assert table.columns["tenure"].statistics[0].group_by_bins is not None
    assert table.columns["region"].statistics[0].group_by_bins is not None


def test_a_timestamp_column_may_group_and_be_grouped() -> None:
    document = _document(_histogram(groupByBins={"column": "joined", "bins": ["0", "86400000"]}))
    document["tables"]["t"]["columns"]["tenure"]["statistics"][0]["groups"] = [_group("0", 1, 2)]
    (statistic,) = parse_profile(document).tables["t"].columns["tenure"].statistics
    assert statistic.group_by_bins is not None and statistic.group_by_bins.column == "joined"
    grouped = _document()
    grouped["tables"]["t"]["columns"]["joined"]["statistics"] = [_histogram()]
    assert parse_profile(grouped).tables["t"].columns["joined"].statistics[0].group_by_bins


def test_two_statistics_that_differ_in_their_bins_are_two_questions() -> None:
    other = _histogram(
        groupByBins={"column": "age", "bins": ["0", "50", "100"]},
        groups=[_group("0", 1, 2), _group("50", 3, 4)],
    )
    assert (
        len(parse_profile(_document(_histogram(), other)).tables["t"].columns["tenure"].statistics)
        == 2
    )
    _refused(_document(_histogram(), _histogram()), "repeats a statistic")


def test_the_disclosure_rules_apply_group_by_group() -> None:
    suppressed = _histogram(
        disclosure="small",
        groups=[_group("0", 5, 6), {"group": "30", "status": "A"}, _group("60", 1, 2)],
    )
    (statistic,) = parse_profile(_document(suppressed)).tables["t"].columns["tenure"].statistics
    assert [g.published for g in statistic.groups] == [True, False, True]
    missing = _histogram(
        groups=[_group("0", 5, 6), {"group": "30", "status": "A", "k": 3, "binCounts": [1, 2]}]
    )
    _refused(_document(missing), "groups[1]")
    _refused(_document(_histogram(disclosure="nowhere")), "disclosure")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("grouping", "words"),
    [
        ({"column": "tenure", "bins": BINS}, "other than the statistic's own"),
        ({"column": "region", "bins": BINS}, "no number or timestamp column"),
        ({"column": "elsewhere", "bins": BINS}, "no number or timestamp column"),
        ({"column": "age", "bins": ["0"]}, "at least two, strictly increasing"),
        ({"column": "age", "bins": ["0", "10", "10"]}, "strictly increasing"),
        ({"column": "age", "bins": ["10", "0"]}, "strictly increasing"),
        ({"column": "age", "bins": ["0", "1e2"]}, "plain decimal"),
        ({"column": "age", "bins": ["0", 100]}, "plain decimal"),
        ({"column": "age", "bins": "0,100"}, "expected a list"),
        ({"column": "age"}, "missing key 'bins'"),
        ({"bins": BINS}, "missing key 'column'"),
        ({"column": "age", "bins": BINS, "extra": 1}, "unknown key"),
        ({"column": 3, "bins": BINS}, "groupByBins.column"),
        ("age", "expected an object"),
    ],
)
def test_every_refusal_of_a_grouping_names_where_and_quotes_no_value(
    grouping: Any, words: str
) -> None:
    document = _changed(lambda s: s.update(groupByBins=grouping), _histogram())
    _refused(document, "groupByBins", words)


def test_a_statistic_holds_at_most_one_of_group_by_group_by_link_group_by_bins_and_per() -> None:
    with_group_by = _histogram(groupBy="region")
    _refused(_document(with_group_by), "at most one of groupBy")
    link = {
        "parent": {"table": "t", "columns": ["id"]},
        "columns": ["id"],
        "column": "age",
    }
    _refused(_document(_histogram(groupByLink=link)), "at most one of groupBy")
    _refused(_document(_histogram(per=["region"])), "per holds no groupBy")


def test_a_period_a_query_a_relationship_and_a_history_statistic_take_no_grouping() -> None:
    document = _document()
    document["relationships"] = [
        {
            "parent": {"table": "t", "columns": ["age"]},
            "child": {"table": "t", "columns": ["age"]},
            "statistics": [_histogram()],
        }
    ]
    _refused(document, "relationships[0].statistics[0]", "groupByBins")
    document = _document()
    document["queries"] = [{"name": "q", "statistics": [_histogram()]}]
    _refused(document, "queries[0]")


def test_a_grouping_is_a_form_of_1_4_0_and_an_earlier_format_refuses_it() -> None:
    document = _document(_histogram())
    document["_schemaURL"] = SCHEMA_URL_1_3_0
    document.pop("privacy", None)
    _refused(document, "groupByBins", "1-4-0", "earlier format")


# ---------------------------------------------------------------------------
# Noise
# ---------------------------------------------------------------------------


def test_a_count_table_grouped_by_bins_may_carry_noise_charged_as_a_count_table_once() -> None:
    profile = parse_profile(_document(_noisy_histogram()))
    (statistic,) = profile.tables["t"].columns["tenure"].statistics
    assert statistic.noise is not None and statistic.cell_variance == 25
    assert [place for place, _ in profile.parts] == [
        "profile.tables.t.columns.tenure.statistics[0]"
    ]
    assert [group.noisy for group in statistic.groups][0] == (Fraction(5), Fraction(6))
    levels = {
        "definition": "levelHistogram/1",
        "levels": ["a", "b"],
        "sampleSize": {"min": 1000},
        "disclosure": "noisy",
        "noise": copy.deepcopy(NOISE),
        "groupByBins": {"column": "age", "bins": BINS},
        "groups": [
            {"group": key, "status": "F", "noisyCounts": ["1", "2"]} for key in ("0", "30", "60")
        ],
    }
    document = _document(_noisy_histogram())
    document["tables"]["t"]["columns"]["region"]["statistics"] = [levels]
    assert len(parse_profile(document).parts) == 2


def test_the_noise_of_a_grouped_count_table_is_charged_a_sensitivity_of_one() -> None:
    # a Gaussian of scale 5 spends rho = 1 / (2 * 25) = 0.02 on a count table, and one record lands
    # in one cell of one group
    exact = _changed(lambda s: s["noise"]["privacyLoss"].update(rho="0.02"), _noisy_histogram())
    assert parse_profile(exact)
    short = _changed(
        lambda s: s["noise"]["privacyLoss"].update(rho="0.0199999"), _noisy_histogram()
    )
    _refused(short, "noise.privacyLoss", "count table")
    laplace = _changed(
        lambda s: s.update(
            noise={"mechanism": "laplace", "scale": "2", "privacyLoss": {"rho": "0.125"}}
        ),
        _noisy_histogram(),
    )
    assert parse_profile(laplace)  # epsilon 1 / 2, rho = epsilon^2 / 2


def test_a_noisy_grouped_statistic_publishes_one_group_for_every_bin_and_no_other() -> None:
    fewer = _noisy_histogram(
        groups=[{"group": key, "status": "F", "noisyCounts": ["5", "6"]} for key in ("0", "30")]
    )
    _refused(_document(fewer), "groups", "one group for every bin")
    suppressed = _noisy_histogram()
    suppressed["groups"][1] = {"group": "30", "status": "A"}
    _refused(_document(suppressed), "groups[1]", "noisyCounts")
    wrong_cells = _noisy_histogram()
    wrong_cells["groups"][0]["noisyCounts"] = ["5"]
    _refused(_document(wrong_cells), "noisyCounts")


def test_only_a_level_or_marginal_histogram_may_carry_noise_when_grouped_by_bins() -> None:
    mean = {
        "definition": "mean/1",
        "sampleSize": {"min": 1000},
        "disclosure": "noisy",
        "noise": copy.deepcopy(NOISE),
        "groupByBins": {"column": "age", "bins": BINS},
        "groups": [{"group": "0", "k": 5400, "Sx": "1", "status": "F"}],
    }
    _refused(_document(mean), "noise", "levelHistogram")


def test_noise_on_a_grouped_statistic_needs_the_account_and_the_units_table() -> None:
    document = _document(_noisy_histogram())
    del document["privacy"]
    _refused(document, "privacy block")
    document = _document(_noisy_histogram())
    document["privacy"]["privacyUnit"] = {"expose": "elsewhere"}
    _refused(document, "unit of privacy")


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------


def test_the_schema_holds_the_grouping_and_refuses_what_the_reader_refuses() -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads((SPEC / "profile-1-4.schema.json").read_text("utf-8"))
    validator = Draft202012Validator(schema)
    validator.validate(_document(_histogram()))
    validator.validate(_document(_noisy_histogram()))
    bad: List[Dict[str, Any]] = [
        {"column": "age"},
        {"bins": BINS},
        {"column": "age", "bins": ["0"]},
        {"column": "age", "bins": ["0", 100]},
        {"column": "age", "bins": BINS, "extra": 1},
    ]
    for grouping in bad:
        document = _changed(lambda s, g=grouping: s.update(groupByBins=g), _histogram())
        assert list(validator.iter_errors(document))
    earlier = json.loads((SPEC / "profile-1-3.schema.json").read_text("utf-8"))
    assert list(Draft202012Validator(earlier).iter_errors(_document(_histogram())))
