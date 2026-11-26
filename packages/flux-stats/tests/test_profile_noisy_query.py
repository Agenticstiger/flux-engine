# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Profile format 1-5-0: a noisy query rate and its ``unit`` (PROFILE-SPEC.md section 3a).

``docs/spec/profile-1-5-history-example.json`` holds three noisy queries over ``card_version``, a
table the unit follows into at a cap of 4: each row a record (``m_T`` = 4), groups by the child
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


def _group_by_tier(document: Dict[str, Any], index: int) -> None:
    statistic = _statistic(document, index)
    statistic["groupBy"] = "tier"
    statistic["groups"] = [
        {"group": level, "status": "F", "noisyCounts": ["400.2", "80.1"]}
        for level in ("basic", "plus")
    ]


@pytest.mark.parametrize("index", [MEMBERS, CARDS])
def test_a_query_over_groups_of_rows_may_not_group_by_a_column_too(index: int) -> None:
    """A record is a group of rows and can hold several levels of the column: it can land in
    more than one cell. A query over rows (``{table}``) with a ``groupBy`` stays allowed."""
    message = _refused(_edit(lambda document: _group_by_tier(document, index)))
    assert f"queries[{index}]" in message and "tier" not in message
    assert ("members_with_a_lapsed_card", "cards_that_lapsed")[index - 1] in message
    parse_profile(EXAMPLE)  # the rows query groups by tier


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


def _laplace(document: Dict[str, Any], index: int, scale: str, rho: str) -> None:
    statistic = _statistic(document, index)
    statistic["noise"] = {
        "mechanism": "laplace",
        "scale": scale,
        "privacyLoss": {"rho": rho},
    }


def test_the_unreached_form_is_charged_two_in_l1_under_laplace() -> None:
    """A Laplace of scale 2 spends rho (L1 / 2)^2 / 2: 1/2 at L1 = 2, where L1 = 1 would need 1/8."""

    def share(rho: str) -> Dict[str, Any]:
        def change(document: Dict[str, Any]) -> None:
            _unfollow(document)
            _laplace(document, 0, "2", rho)

        return _edit(change)

    assert "queries[0]" in _refused(share("0.49"))
    parse_profile(share("0.5"))


def _reached_twice(document: Dict[str, Any]) -> None:
    """One unit holds at most 2 rows of ``card_version``, and the budget has room."""
    document["privacy"]["privacyUnit"]["follows"][0]["maxChildren"] = 2
    document["privacy"]["privacyLoss"]["rho"] = "10"


def test_a_query_over_rows_is_charged_the_square_of_the_rows_one_unit_holds() -> None:
    """m_T = 2, scale 8: rho = m_T^2 / (2 x 64) = 1/32; m_T alone would need 1/64."""

    def share(rho: str) -> Dict[str, Any]:
        def change(document: Dict[str, Any]) -> None:
            _reached_twice(document)
            _statistic(document, ROWS)["noise"]["privacyLoss"]["rho"] = rho

        return _edit(change)

    assert "queries[0]" in _refused(share("0.03"))
    parse_profile(share("0.03125"))


def test_a_unit_that_groups_by_a_column_the_table_does_not_hold_is_refused_for_it() -> None:
    def other(document: Dict[str, Any]) -> None:
        document["queries"][ROWS]["unit"] = {"table": "card_version", "by": ["nowhere"]}

    message = _refused(_edit(other))
    assert "queries[0].unit.by" in message and "a column is not one of its table" in message


def test_a_noisy_query_in_a_1_4_0_document_is_refused_as_a_form_of_1_5_0() -> None:
    """With no unit to read first, the noise block is what the document may not hold."""

    def older(document: Dict[str, Any]) -> None:
        document["_schemaURL"] = SCHEMA_URL_1_4_0
        document["tables"]["card_version"].pop("history")
        for table, column in (("member", "segment"), ("card_version", "valid_to")):
            document["tables"][table]["columns"][column]["statistics"] = []
        document["queries"] = document["queries"][:1]
        del document["queries"][0]["unit"]

    message = _refused(_edit(older))
    assert "queries[0].statistics" in message
    assert "a noise block on a query's statistic is a form of format 1-5-0" in message


def _link(parent: str, parent_column: str, child: str, child_column: str) -> Dict[str, Any]:
    return {
        "parent": {"table": parent, "columns": [parent_column]},
        "child": {"table": child, "columns": [child_column]},
    }


def _with_accounts(document: Dict[str, Any]) -> None:
    """An ``account`` table between ``member`` and ``card_version``, which the unit follows into
    at a cap of 4."""
    identifier = document["tables"]["member"]["columns"]["id"]
    document["tables"]["account"] = {
        "columns": {"id": copy.deepcopy(identifier), "member_id": copy.deepcopy(identifier)}
    }
    document["privacy"]["privacyUnit"]["follows"] = [
        {**_link("member", "id", "account", "member_id"), "maxChildren": 4}
    ]
    document["privacy"]["privacyLoss"]["rho"] = "100"


def _through_accounts(document: Dict[str, Any]) -> None:
    """The unit follows member -> account at a cap of 4 and account -> card_version at a cap of
    1, and the members query groups ``card_version`` by its account: form (a) at m_P = 4."""
    _with_accounts(document)
    identifier = document["tables"]["member"]["columns"]["id"]
    document["tables"]["card_version"]["columns"]["account_id"] = copy.deepcopy(identifier)
    document["privacy"]["privacyUnit"]["follows"].append(
        {**_link("account", "id", "card_version", "account_id"), "maxChildren": 1}
    )
    document["relationships"] = [
        {**_link("member", "id", "account", "member_id"), "statistics": []},
        {**_link("account", "id", "card_version", "account_id"), "statistics": []},
    ]
    for table in document["tables"].values():
        for column in table["columns"].values():
            column["statistics"] = [
                statistic for statistic in column.get("statistics", []) if "noise" not in statistic
            ]
    document["tables"]["card_version"].pop("history")
    query = document["queries"][MEMBERS]
    query["unit"] = {"table": "card_version", "by": ["account_id"]}
    document["queries"] = [query]


def test_the_member_form_is_charged_every_row_of_the_parent_one_unit_holds() -> None:
    """Form (a) at m_P = 4 and scale 2: rho = m_P^2 / (2 x 4) = 2, where one row of the parent
    would need 1/8."""

    def share(rho: str) -> Dict[str, Any]:
        def change(document: Dict[str, Any]) -> None:
            _through_accounts(document)
            _statistic(document, 0)["noise"]["privacyLoss"]["rho"] = rho

        return _edit(change)

    message = _refused(share("0.125"))
    assert "queries[0]" in message and "16 / (2 scale^2)" in message
    assert parse_profile(share("2")).accounting.reach("account") == 4  # type: ignore[union-attr]


def _two_parents(document: Dict[str, Any], account_first: bool) -> None:
    """``card_version`` is a table the unit does not reach, and its ``member_id`` is the child
    column of two listed relationships: from ``member`` (m_P = 1) and from ``account``, which the
    unit follows into at a cap of 4 (m_P = 4)."""
    _unfollow(document)
    _with_accounts(document)
    listed = document["relationships"]
    from_account = {**_link("account", "id", "card_version", "member_id"), "statistics": []}
    followed = {**_link("member", "id", "account", "member_id"), "statistics": []}
    document["relationships"] = (
        [from_account, *listed, followed] if account_first else [*listed, from_account, followed]
    )


@pytest.mark.parametrize("laplace", [False, True])
@pytest.mark.parametrize("account_first", [False, True])
def test_two_parents_the_unit_reaches_on_the_same_child_columns_are_refused_in_either_order(
    account_first: bool, laplace: bool
) -> None:
    """In a table the unit does not reach, a row of each parent can move a group: one unit's
    member row and its four account rows can move five groups, so the two relationships'
    charges would add. The query is refused, naming both parents, even at a share that would
    pay for their sum."""

    def change(document: Dict[str, Any]) -> None:
        _two_parents(document, account_first)
        _statistic(document, 0)["noise"]["privacyLoss"]["rho"] = "50"
        if laplace:
            _laplace(document, 0, "2", "50")

    message = _refused(_edit(change))
    assert "queries[0].unit.by" in message
    assert "more than one parent the unit reaches (account(id), member(id))" in message


@pytest.mark.parametrize("store_first", [False, True])
def test_a_parent_the_unit_does_not_reach_gives_no_charge_beside_one_that_does(
    store_first: bool,
) -> None:
    """A relationship from ``store``, a table the unit does not reach, on the same child columns
    moves no group, so the query keeps the member form's charge: at m_P = 1 and scale 2, rho 1/4."""

    def share(rho: str) -> Dict[str, Any]:
        def change(document: Dict[str, Any]) -> None:
            _unfollow(document)
            identifier = document["tables"]["member"]["columns"]["id"]
            document["tables"]["store"] = {"columns": {"id": copy.deepcopy(identifier)}}
            from_store = {**_link("store", "id", "card_version", "member_id"), "statistics": []}
            listed = document["relationships"]
            document["relationships"] = (
                [from_store, *listed] if store_first else [*listed, from_store]
            )
            _statistic(document, 0)["noise"]["privacyLoss"]["rho"] = rho

        return _edit(change)

    assert "queries[0]" in _refused(share("0.125"))
    parse_profile(share("0.25"))


@pytest.mark.parametrize("account_reached", [False, True])
@pytest.mark.parametrize("account_first", [False, True])
def test_a_relationship_the_unit_does_not_follow_leaves_the_followed_charge_in_either_order(
    account_first: bool, account_reached: bool
) -> None:
    """``card_version`` is reached through ``member``, and its ``member_id`` is also the child
    column of a relationship from ``account`` that the unit does not follow (``account`` itself
    unreached, or reached through ``member``): a group still leaves whole with its member,
    m_P = 1, whichever relationship is listed first."""

    def change(document: Dict[str, Any]) -> None:
        identifier = document["tables"]["member"]["columns"]["id"]
        document["tables"]["account"] = {
            "columns": {"id": copy.deepcopy(identifier), "member_id": copy.deepcopy(identifier)}
        }
        from_account = {**_link("account", "id", "card_version", "member_id"), "statistics": []}
        listed = document["relationships"]
        document["relationships"] = (
            [from_account, *listed] if account_first else [*listed, from_account]
        )
        if account_reached:
            document["privacy"]["privacyUnit"]["follows"].append(
                {**_link("member", "id", "account", "member_id"), "maxChildren": 4}
            )
            document["relationships"].append(
                {**_link("member", "id", "account", "member_id"), "statistics": []}
            )

    profile = parse_profile(_edit(change))
    assert profile.queries[MEMBERS].statistics[0].noise.scale == 2  # type: ignore[union-attr]
    if account_reached:
        assert profile.accounting.reach("account") == 4  # type: ignore[union-attr]
