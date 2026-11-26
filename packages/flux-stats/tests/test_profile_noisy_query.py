# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Profile format 1-5-0: a noisy query rate and its ``unit`` (PROFILE-SPEC.md section 3a).

``docs/spec/profile-1-5-history-example.json`` holds three noisy queries over ``card_version``, a
table the unit follows into at a cap of 4: a row-keeping filter (``m_T`` = 4), groups by the child
columns of the followed relationship from ``member`` (``m_P`` = 1) and groups by the history's
entity (``m_T`` = 4).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict

import pytest

from flux_stats.errors import FluxError
from flux_stats.profile import SCHEMA_URL_1_4_0, QueryUnit, parse_profile

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _spec()
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-1-5-history-example.json").read_text("utf-8"))
ROWS, MEMBERS, CARDS = 0, 1, 2


def _edit(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


def _statistic(document: Dict[str, Any], index: int) -> Dict[str, Any]:
    return document["queries"][index]["statistics"][0]


def test_the_three_kinds_of_unit_are_read() -> None:
    queries = parse_profile(EXAMPLE).queries
    assert [query.unit for query in queries] == [
        QueryUnit("card_version"),
        QueryUnit("card_version", ("member_id",)),
        QueryUnit("card_version", ("card_no",)),
    ]
    assert all(query.statistics[0].noise is not None for query in queries)


@pytest.mark.parametrize(
    ("index", "figure"),
    [(ROWS, "0.0078125"), (MEMBERS, "0.0078125"), (CARDS, "0.0078125")],
)
def test_each_unit_is_charged_its_own_figure(index: int, figure: str) -> None:
    """Scales 8, 2 and 8 at charges 4, 1 and 4 need 1/8 each: a share of 1/128 is understated."""

    def understate(document: Dict[str, Any]) -> None:
        _statistic(document, index)["noise"]["privacyLoss"]["rho"] = figure

    message = _refused(_edit(understate))
    assert f"queries[{index}]" in message


def test_the_member_form_is_charged_the_parent_not_the_table() -> None:
    """Form (a) at m_P = 1: scale 2 at rho 1/8 is exact; the table's 4 would need rho 2."""
    profile = parse_profile(EXAMPLE)
    assert profile.accounting.reach("card_version") == 4  # type: ignore[union-attr]
    assert profile.queries[MEMBERS].statistics[0].noise.scale == 2  # type: ignore[union-attr]


def test_every_query_share_adds_to_the_budget() -> None:
    def overspend(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyLoss"]["rho"] = "1.3"

    assert "privacy" in _refused(_edit(overspend))


def test_a_noisy_query_with_no_unit_is_refused() -> None:
    def strip(document: Dict[str, Any]) -> None:
        del document["queries"][ROWS]["unit"]

    message = _refused(_edit(strip))
    assert "queries[0].unit" in message and "card_rows_that_lapsed" in message


@pytest.mark.parametrize(
    "unit",
    [
        {"table": "nowhere"},
        {"table": "card_version", "by": ["nowhere"]},
        {"table": "card_version", "by": ["tier"]},
        {"table": "card_version", "by": ["member_id", "card_no"]},
        {"table": "card_version", "by": []},
        {"table": "card_version", "extra": 1},
    ],
)
def test_any_other_unit_is_refused_naming_the_query(unit: Dict[str, Any]) -> None:
    def other(document: Dict[str, Any]) -> None:
        document["queries"][ROWS]["unit"] = unit

    assert "queries[0].unit" in _refused(_edit(other))


def test_a_unit_the_privacy_unit_does_not_reach_has_no_charge() -> None:
    def unreach(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyUnit"] = {"expose": "member"}
        for table, column in (("member", "segment"), ("card_version", "valid_to")):
            document["tables"][table]["columns"][column]["statistics"] = []
        document["tables"]["card_version"].pop("history")
        document["queries"] = document["queries"][:1]

    assert "queries[0].unit" in _refused(_edit(unreach))


def _unfollow(document: Dict[str, Any]) -> None:
    # the relationship stays listed, but the unit follows nothing: card_version is unreached,
    # which is the other case form (a) allows, and its own rows then have no charge
    document["privacy"]["privacyUnit"] = {"expose": "member"}
    for table, column in (("member", "segment"), ("card_version", "valid_to")):
        document["tables"][table]["columns"][column]["statistics"] = []
    document["tables"]["card_version"].pop("history")
    document["queries"] = [document["queries"][MEMBERS]]


def test_the_member_form_in_a_table_that_is_not_reached_moves_a_group_between_cells() -> None:
    """The table's rows are the same in every neighbouring data set, so a group stays when its
    row of the parent leaves, and a query that reads that row can move the group from one cell
    of the rate to the other: per row of the parent L1 = 2 and L2^2 = 2. At m_P = 1 and scale 2
    that needs rho 1/4; 1/8 is the charge of a group that leaves whole."""

    def share(rho: str) -> Dict[str, Any]:
        def change(document: Dict[str, Any]) -> None:
            _unfollow(document)
            _statistic(document, 0)["noise"]["privacyLoss"]["rho"] = rho

        return _edit(change)

    message = _refused(share("0.125"))
    assert "queries[0]" in message
    parse_profile(share("0.25"))


def test_the_member_form_is_refused_when_the_table_is_reached_through_another_relationship() -> (
    None
):
    def other_path(document: Dict[str, Any]) -> None:
        document["relationships"].append(
            {
                "parent": {"table": "member", "columns": ["segment"]},
                "child": {"table": "card_version", "columns": ["card_no"]},
                "statistics": [],
            }
        )
        document["queries"][MEMBERS]["unit"] = {"table": "card_version", "by": ["card_no"]}
        document["tables"]["card_version"]["history"]["entity"] = ["member_id"]

    message = _refused(_edit(other_path))
    assert "queries[1].unit.by" in message


def _exact(member: Any) -> None:
    """A noisy history member released without noise instead, its counts the rounded noisy ones."""
    if "noise" not in member:
        return
    member.pop("noise")
    member["disclosure"] = "none"
    key = "binCounts" if member["definition"].startswith("marginal") else "levelCounts"
    for group in member["groups"]:
        counts = [max(0, round(float(value))) for value in group.pop("noisyCounts")]
        group["k"] = sum(counts)
        group[key] = counts


def test_the_entity_form_is_refused_in_the_unit_s_own_expose() -> None:
    """One unit is one version there: removing it can move its entity between the rate's cells.

    The charge of a count table (1) would then understate what one unit changes (one cell down,
    the other up), as for a noisy history member in that table.
    """

    def own_expose(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyUnit"] = {"expose": "card_version"}
        history = document["tables"]["card_version"]["history"]
        for key in ("versionsPerEntity", "firstStart", "versionLength", "chain", "lastVersion"):
            _exact(history[key])
        for column in history["columns"].values():
            _exact(column["initial"])
            _exact(column["transitions"])
        document["relationships"] = []
        for table in document["tables"].values():
            for column in table["columns"].values():
                column.pop("statistics", None)
        document["queries"] = [document["queries"][CARDS]]
        assert document["queries"][0]["unit"] == {"table": "card_version", "by": ["card_no"]}

    message = _refused(_edit(own_expose))
    assert "queries[0].unit" in message


def test_a_noisy_query_in_a_document_of_an_earlier_format_is_refused() -> None:
    document = _edit(lambda d: d.update(_schemaURL=SCHEMA_URL_1_4_0))
    message = _refused(document)
    assert "format 1-5-0" in message


def test_a_noisy_query_needs_the_privacy_block() -> None:
    def drop(document: Dict[str, Any]) -> None:
        del document["privacy"]

    assert "privacy" in _refused(_edit(drop))


def test_an_exact_query_reads_as_before_and_its_unit_is_checked_for_shape_only() -> None:
    document = json.loads((SPEC / "profile-1-3-example.json").read_text("utf-8"))
    parse_profile(document)
    exact = copy.deepcopy(EXAMPLE)
    exact["queries"] = [
        {
            "name": "exact_rate",
            "unit": {"table": "nowhere"},
            "statistics": [
                {
                    "definition": "levelHistogram/1",
                    "levels": ["false", "true"],
                    "sampleSize": {"min": 1000, "max": 9999},
                    "disclosure": "none",
                    "groups": [{"group": "", "k": 10, "levelCounts": [4, 6], "status": "F"}],
                }
            ],
        }
    ]
    parse_profile(exact)  # an unknown table is no refusal for an exact query
    exact["queries"][0]["unit"] = {"table": "x", "by": "y"}
    assert "unit.by" in _refused(exact)


def test_a_unit_in_a_document_of_an_earlier_format_is_refused() -> None:
    document = json.loads((SPEC / "profile-1-3-example.json").read_text("utf-8"))
    document["queries"][0]["unit"] = {"table": "plan_version"}
    assert "unit" in _refused(document)


@pytest.mark.parametrize(
    "name",
    ["profile-1-3-example.json", "profile-1-4-example.json", "profile-1-4-follows-example.json"],
)
def test_documents_of_earlier_formats_read_as_before(name: str) -> None:
    profile = parse_profile(json.loads((SPEC / name).read_text("utf-8")))
    assert all(query.unit is None for query in profile.queries)
