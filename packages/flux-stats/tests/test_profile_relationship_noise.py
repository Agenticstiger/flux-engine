# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A relationship's statistics may group by a parent column and carry noise, and a statistic grouped by a link may too.

ADR 0037 decisions 2 and 3 and ``docs/spec/PROFILE-SPEC.md`` section 3a. A relationship's
``marginalHistogram/1`` is charged by what one unit reaches of its parent and child tables; a
statistic grouped by a link is charged its table's reach, and only where the link's parent is fixed
or the unit follows the link. The shares are checked at the boundary with exact fractions.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from jsonschema import Draft202012Validator

from flux_stats.errors import FluxError
from flux_stats.profile import SCHEMA_URL_1_3_0, parse_profile

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _spec()
SCHEMA: Dict[str, Any] = json.loads((SPEC / "profile-1-4.schema.json").read_text("utf-8"))
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-1-4-follows-example.json").read_text("utf-8"))

# Tables: acct (the expose), inv (3 per account), line (4 per invoice), other (never followed) and
# plan (a fixed parent with a category column).
COLUMNS = ("id", "parent_id", "other_id", "kind", "tier")


def _key(table: str, column: str) -> Dict[str, Any]:
    return {"table": table, "columns": [column]}


def _edge(parent: str, child: str, column: str = "parent_id", **more: Any) -> Dict[str, Any]:
    return {"parent": _key(parent, "id"), "child": _key(child, column), "statistics": []} | more


def _follow(parent: str, child: str, cap: int, column: str = "parent_id") -> Dict[str, Any]:
    return {"parent": _key(parent, "id"), "child": _key(child, column), "maxChildren": cap}


def _histogram(
    scale: str, rho: str, bins: Optional[List[str]] = None, **more: Any
) -> Dict[str, Any]:
    edges = bins or ["0", "1", "3", "4"]
    return {
        "definition": "marginalHistogram/1",
        "bins": edges,
        "sampleSize": {"min": 5000, "max": 9999},
        "disclosure": "noisy",
        "noise": {"mechanism": "gaussian", "scale": scale, "privacyLoss": {"rho": rho}},
        "groups": [{"group": "", "status": "F", "noisyCounts": ["10"] * (len(edges) - 1)}],
    } | more


def _count_histogram(scale: str, rho: str) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "levels": ["a", "b"],
        "sampleSize": {"min": 5000, "max": 9999},
        "disclosure": "noisy",
        "noise": {"mechanism": "gaussian", "scale": scale, "privacyLoss": {"rho": rho}},
        "groups": [{"group": "", "status": "F", "noisyCounts": ["10", "20"]}],
    }


def _document(
    edges: List[Dict[str, Any]],
    follows: Optional[List[Dict[str, Any]]] = None,
    expose: str = "acct",
) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    document["tables"] = {
        name: {"columns": {column: {"domain": "category"} for column in COLUMNS}}
        for name in ("acct", "inv", "line", "other", "plan")
    }
    for table in document["tables"].values():
        table["columns"]["n"] = {"domain": "number"}
        table["columns"]["t"] = {"domain": "timestamp"}
    unit: Dict[str, Any] = {"expose": expose}
    if follows is not None:
        unit["follows"] = follows
    document["privacy"]["privacyUnit"] = unit
    document["privacy"]["privacyLoss"] = {"rho": "1000"}
    document["relationships"] = edges
    # the expose holds one noisy histogram, so the account is spent
    document["tables"][expose]["columns"]["kind"]["statistics"] = [_count_histogram("10", "0.005")]
    return document


def _chain(*statistics: Dict[str, Any]) -> Dict[str, Any]:
    """acct -> inv (3) -> line (4), the statistics on the first relationship."""
    return _document(
        [_edge("acct", "inv", statistics=list(statistics)), _edge("inv", "line")],
        [_follow("acct", "inv", 3), _follow("inv", "line", 4)],
    )


def _refused(document: Dict[str, Any], *words: str) -> str:
    with pytest.raises(FluxError) as raised:
        parse_profile(document)
    message = str(raised.value)
    assert "FLX-5036" in message
    for word in words:
        assert word in message, message
    return message


def _rho_of(document: Dict[str, Any], place: str) -> Fraction:
    profile = parse_profile(document)
    loss = dict(profile.parts)[place].loss.rho
    assert loss is not None
    return loss


def _boundary(build: Any, scale: str, exact: str, below: str, place: str) -> None:
    """The share at the charge is read as written; a hair below it is refused."""
    assert _rho_of(build(scale, exact), place) == Fraction(exact)
    message = _refused(build(scale, below), "privacyLoss")
    assert place in message


REL = "profile.relationships[0].statistics[0]"


# ---------------------------------------------------------------------------
# Charges: a relationship's histogram
# ---------------------------------------------------------------------------


def test_a_followed_relationship_is_charged_the_reach_of_its_parent() -> None:
    # m_P = 1, m_C' = 0: L1 = L2 = 1, rho = 1 / (2 s^2) = 1/2 at scale 1
    _boundary(lambda s, r: _chain(_histogram(s, r)), "1", "0.5", "0.4999", REL)


def test_a_followed_relationship_below_the_expose_is_charged_its_parents_reach() -> None:
    document = _document(
        [_edge("acct", "inv"), _edge("inv", "line", statistics=[_histogram("3", "0.5")])],
        [_follow("acct", "inv", 3), _follow("inv", "line", 4)],
    )
    # m_P = 3: L2 squared = 9, rho = 9 / (2 * 9) = 1/2
    assert _rho_of(document, "profile.relationships[1].statistics[0]") == Fraction(1, 2)
    document["relationships"][1]["statistics"][0]["noise"]["privacyLoss"]["rho"] = "0.4999"
    _refused(document, "privacyLoss")


def test_an_unlisted_relationship_to_an_unreached_child_is_charged_the_parents_reach() -> None:
    document = _document([_edge("acct", "other", statistics=[_histogram("1", "0.5")])], None)
    assert _rho_of(document, REL) == Fraction(1, 2)
    document["relationships"][0]["statistics"][0]["noise"]["privacyLoss"]["rho"] = "0.49"
    _refused(document, "privacyLoss")


def test_a_relationship_into_a_child_the_unit_reaches_otherwise_is_refused() -> None:
    # inv is reached through acct -> inv (parent_id); a second, unlisted relationship into it from
    # the expose would count some of the child's rows twice
    document = _document(
        [
            _edge("acct", "inv", "parent_id"),
            _edge("acct", "inv", "other_id", statistics=[_histogram("1", "0.5")]),
        ],
        [_follow("acct", "inv", 3)],
    )
    _refused(document, "profile.relationships[1].statistics[0].noise", "through this relationship")


def test_the_child_only_case_is_charged_twice_the_childs_reach_in_l1_and_sqrt_two_in_l2() -> None:
    # parent 'other' is not reached, child inv has m_C = 3: L1 = 6 and the squared L2 = 18
    def build(scale: str, rho: str) -> Dict[str, Any]:
        return _document(
            [
                _edge("acct", "inv"),
                _edge("other", "inv", "other_id", statistics=[_histogram(scale, rho)]),
            ],
            [_follow("acct", "inv", 3)],
        )

    _boundary(build, "3", "1", "0.9999", "profile.relationships[1].statistics[0]")


def test_the_child_only_case_of_a_unit_that_is_the_child_expose_without_follows() -> None:
    # plan -> acct, the unit's expose is the child with m_C = 1: squared L2 = 2, rho = 2 / (2 s^2)
    def build(scale: str, rho: str) -> Dict[str, Any]:
        return _document([_edge("plan", "acct", statistics=[_histogram(scale, rho)])], None)

    _boundary(build, "1", "1", "0.9999", REL)


def test_the_child_only_case_under_a_laplace_is_charged_two_m_c_in_l1() -> None:
    def build(epsilon: str) -> Dict[str, Any]:
        document = _document([_edge("plan", "acct", statistics=[_histogram("1", "1")])], None)
        document["privacy"]["measure"] = "approximateDP"
        document["privacy"]["privacyLoss"] = {"epsilon": "100", "delta": "0"}
        document["tables"]["acct"]["columns"]["kind"]["statistics"][0]["noise"] = {
            "mechanism": "laplace",
            "scale": "10",
            "privacyLoss": {"epsilon": "0.2", "delta": "0"},
        }
        document["relationships"][0]["statistics"][0]["noise"] = {
            "mechanism": "laplace",
            "scale": "4",
            "privacyLoss": {"epsilon": epsilon, "delta": "0"},
        }
        return document

    parse_profile(build("0.5"))  # 2 * 1 / 4
    _refused(build("0.4999"), "an epsilon below")


def test_a_relationship_between_tables_the_unit_does_not_reach_is_refused() -> None:
    document = _document([_edge("other", "plan", statistics=[_histogram("1", "5")])], None)
    _refused(document, f"{REL}.noise", "reaches the parent table")


def test_a_grouping_leaves_the_charge_as_it_is() -> None:
    grouped = _histogram(
        "1",
        "0.5",
        groupBy="tier",
        groups=[
            {"group": "gold", "status": "F", "noisyCounts": ["1", "2", "3"]},
            {"group": "iron", "status": "F", "noisyCounts": ["1", "2", "3"]},
        ],
    )
    assert _rho_of(_chain(grouped), REL) == Fraction(1, 2)
    document = _chain(grouped)
    document["relationships"][0]["statistics"][0]["noise"]["privacyLoss"]["rho"] = "0.4999"
    _refused(document, "privacyLoss")


def test_a_relationship_statistic_other_than_a_histogram_may_not_carry_noise() -> None:
    mean = {
        "definition": "mean/1",
        "sampleSize": {"min": 5000, "max": 9999},
        "disclosure": "noisy",
        "noise": {"mechanism": "gaussian", "scale": "1", "privacyLoss": {"rho": "1"}},
        "groups": [{"group": "", "status": "F", "noisyValue": "1"}],
    }
    _refused(_chain(mean), f"{REL}.noise", "as counts")
    quantile = {
        "definition": "quantile/1",
        "level": "0.5",
        "sampleSize": {"min": 5000, "max": 9999},
        "disclosure": "noisy",
        "noise": {"mechanism": "gaussian", "scale": "1", "privacyLoss": {"rho": "1"}},
        "groups": [{"group": "", "status": "F"}],
    }
    _refused(_chain(quantile), f"{REL}.noise")


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


def test_a_bin_that_starts_above_the_cap_is_refused_whatever_its_count() -> None:
    # c = 3: a bin from 4 holds no parent
    for bins in (["0", "1", "4", "6"], ["0", "1", "3.5", "6"], ["0", "9", "10"]):
        document = _chain(_histogram("1", "0.5", bins))
        document["relationships"][0]["statistics"][0]["groups"][0]["noisyCounts"] = ["0"] * (
            len(bins) - 1
        )
        _refused(document, f"{REL}.bins[", "above the unit's cap")
    # the least count of [2.5, 6) is 3, which the cap allows
    parse_profile(_chain(_histogram("1", "0.5", ["0", "1", "2.5", "6"])))
    # a bin that starts at the cap is read
    parse_profile(_chain(_histogram("1", "0.5", ["0", "1", "3", "6"])))


def test_the_cap_applies_to_an_exact_statistic_of_a_followed_relationship_too() -> None:
    exact = {
        "definition": "marginalHistogram/1",
        "bins": ["0", "1", "4"],
        "sampleSize": {"min": 5000, "max": 9999},
        "disclosure": "noisy",
        "groups": [{"group": "", "status": "F", "k": 10, "binCounts": [4, 6]}],
    }
    document = _chain(exact)
    document["relationships"][0]["statistics"][0]["bins"] = ["0", "4", "5"]
    _refused(document, "above the unit's cap")
    document["relationships"][0]["statistics"][0]["bins"] = ["0", "3", "5"]
    parse_profile(document)


def test_a_relationship_the_unit_does_not_follow_has_no_cap() -> None:
    document = _document(
        [_edge("acct", "other", statistics=[_histogram("1", "0.5", ["0", "5", "50"])])], None
    )
    parse_profile(document)


def test_a_followed_relationship_may_hold_an_empty_list_of_statistics() -> None:
    document = _chain()
    assert document["relationships"][0]["statistics"] == []
    parse_profile(document)


# ---------------------------------------------------------------------------
# The groupings of a relationship's statistic
# ---------------------------------------------------------------------------


def _exact(**more: Any) -> Dict[str, Any]:
    return {
        "definition": "marginalHistogram/1",
        "bins": ["0", "1", "3"],
        "sampleSize": {"min": 5000, "max": 9999},
        "disclosure": "noisy",
        "groups": [{"group": "gold", "status": "F", "k": 10, "binCounts": [4, 6]}],
    } | more


def test_a_relationship_statistic_groups_by_a_category_column_of_its_parent() -> None:
    document = _chain(_exact(groupBy="tier"))
    profile = parse_profile(document)
    (statistic,) = profile.relationships[0].statistics
    assert statistic.group_by == "tier" and [g.key for g in statistic.groups] == ["gold"]
    Draft202012Validator(SCHEMA).validate(document)


def test_a_relationship_statistic_groups_by_the_bins_of_a_number_or_timestamp_column() -> None:
    for column, edges in (("n", ["0", "10", "20"]), ("t", ["0", "86400000", "172800000"])):
        document = _chain(
            _exact(
                groupByBins={"column": column, "bins": edges},
                groups=[
                    {"group": key, "status": "F", "k": 10, "binCounts": [4, 6]} for key in edges[:2]
                ],
            )
        )
        profile = parse_profile(document)
        (statistic,) = profile.relationships[0].statistics
        assert statistic.group_by_bins is not None and statistic.group_by_bins.column == column
        Draft202012Validator(SCHEMA).validate(document)


def test_a_grouping_must_name_a_column_of_the_parent_table_of_the_right_domain() -> None:
    _refused(_chain(_exact(groupBy="n")), "groupBy")  # a number column is not a category
    _refused(_chain(_exact(groupBy="nope")), "groupBy")
    _refused(
        _chain(_exact(groupByBins={"column": "tier", "bins": ["0", "1"]})), "groupByBins.column"
    )
    both = _exact(groupBy="tier", groupByBins={"column": "n", "bins": ["0", "1"]})
    _refused(_chain(both), "at most one")


def test_a_relationship_statistic_takes_no_link_and_no_per() -> None:
    link = {"parent": _key("plan", "id"), "columns": ["parent_id"], "column": "tier"}
    _refused(_chain(_exact(groupByLink=link)), "groupByLink")
    _refused(_chain(_exact(per=["tier"])), "per")


def test_a_grouping_and_noise_on_a_relationship_are_forms_of_1_4_0() -> None:
    document = _document([_edge("acct", "other", statistics=[_exact(groupBy="tier")])], None)
    document["_schemaURL"] = SCHEMA_URL_1_3_0
    _refused(document, "groupBy", "1-4-0")
    document = _document([_edge("acct", "other", statistics=[_histogram("1", "5")])], None)
    document["_schemaURL"] = SCHEMA_URL_1_3_0
    _refused(document, f"{REL}.noise", "1-4-0")


def test_a_noisy_relationship_with_no_account_is_refused() -> None:
    document = _document([_edge("acct", "other", statistics=[_histogram("1", "5")])], None)
    del document["privacy"]
    _refused(document, "privacy")


# ---------------------------------------------------------------------------
# A statistic grouped by a link
# ---------------------------------------------------------------------------


def _linked(table: str, parent: str, column: str, scale: str, rho: str) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "levels": ["a", "b"],
        "sampleSize": {"min": 5000, "max": 9999},
        "disclosure": "noisy",
        "groupByLink": {"parent": _key(parent, "id"), "columns": [column], "column": "tier"},
        "noise": {"mechanism": "gaussian", "scale": scale, "privacyLoss": {"rho": rho}},
        "groups": [
            {"group": "gold", "status": "F", "noisyCounts": ["1", "2"]},
            {"group": "iron", "status": "F", "noisyCounts": ["3", "4"]},
        ],
    }


def _with_link(document: Dict[str, Any], table: str, statistic: Dict[str, Any]) -> Dict[str, Any]:
    document["tables"][table]["columns"]["tier"]["statistics"] = [statistic]
    return document


def _place(table: str) -> str:
    return f"profile.tables.{table}.columns.tier.statistics[0]"


def test_a_link_to_a_fixed_parent_is_charged_as_a_count_table_of_its_own_table() -> None:
    # inv has m = 3 and plan is not reached: squared L2 = 9, rho = 9 / (2 * 9) = 1/2 at scale 3
    def build(scale: str, rho: str) -> Dict[str, Any]:
        document = _document([_edge("acct", "inv")], [_follow("acct", "inv", 3)])
        return _with_link(document, "inv", _linked("inv", "plan", "other_id", scale, rho))

    _boundary(build, "3", "0.5", "0.4999", _place("inv"))


def test_a_link_in_the_expose_to_a_fixed_parent_is_charged_one() -> None:
    def build(scale: str, rho: str) -> Dict[str, Any]:
        document = _document([_edge("acct", "inv")], [_follow("acct", "inv", 3)])
        return _with_link(document, "acct", _linked("acct", "plan", "other_id", scale, rho))

    _boundary(build, "1", "0.5", "0.4999", _place("acct"))


def test_a_followed_link_is_charged_its_childs_reach() -> None:
    # inv grouped by a column of its parent acct through the listed relationship: m_T = 3
    def build(scale: str, rho: str) -> Dict[str, Any]:
        document = _document([_edge("acct", "inv")], [_follow("acct", "inv", 3)])
        return _with_link(document, "inv", _linked("inv", "acct", "parent_id", scale, rho))

    _boundary(build, "3", "0.5", "0.4999", _place("inv"))

    # and two levels down: line grouped by a column of inv, m_T = 12
    def deeper(scale: str, rho: str) -> Dict[str, Any]:
        document = _document(
            [_edge("acct", "inv"), _edge("inv", "line")],
            [_follow("acct", "inv", 3), _follow("inv", "line", 4)],
        )
        return _with_link(document, "line", _linked("line", "inv", "parent_id", scale, rho))

    _boundary(deeper, "12", "0.5", "0.4999", _place("line"))


def test_a_link_to_a_parent_the_unit_reaches_but_does_not_follow_it_through_is_refused() -> None:
    # the parent is the expose, but this link's columns are no listed relationship
    document = _document([_edge("acct", "inv")], [_follow("acct", "inv", 3)])
    _with_link(document, "inv", _linked("inv", "acct", "other_id", "3", "0.5"))
    _refused(document, f"{_place('inv')}.noise", "follows the link")
    # without follows at all, a link to the expose from a table the unit does not reach
    document = _document([], None)
    _with_link(document, "acct", _linked("acct", "acct", "other_id", "1", "0.5"))
    _refused(document, "follows the link")


def test_a_link_in_a_table_the_unit_does_not_reach_is_refused() -> None:
    document = _document([_edge("acct", "inv")], [_follow("acct", "inv", 3)])
    _with_link(document, "other", _linked("other", "plan", "other_id", "1", "5"))
    _refused(document, f"{_place('other')}.noise", "follows no relationship into this table")


def test_a_link_with_noise_is_a_form_of_1_4_0() -> None:
    document = _document([_edge("acct", "inv")], [_follow("acct", "inv", 3)])
    _with_link(document, "acct", _linked("acct", "plan", "other_id", "1", "0.5"))
    document["_schemaURL"] = SCHEMA_URL_1_3_0
    del document["privacy"]["privacyUnit"]["follows"]
    _refused(document, "1-4-0")


def test_a_link_statistic_without_noise_still_reads_whatever_its_parent() -> None:
    document = _document([_edge("acct", "inv")], [_follow("acct", "inv", 3)])
    statistic = _linked("inv", "acct", "other_id", "1", "0.5")
    del statistic["noise"]
    statistic["disclosure"] = "noisy"
    statistic["groups"] = [
        {"group": "gold", "status": "F", "k": 10, "levelCounts": [4, 6]},
    ]
    _with_link(document, "inv", statistic)
    parse_profile(document)
