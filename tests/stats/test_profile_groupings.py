# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Profile format 1-3-0: a grouping by a number column, by a linked row, and ``per``.

``docs/spec/profile-1-3-example.json`` holds every addition. It must validate against the schema,
read, and each broken copy must be refused with ``FLX-5036`` naming where (PROFILE-SPEC.md
section 3). A document of an earlier format that holds one is refused, and one that holds none
reads as it did.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from flux_stats.errors import FluxError
from flux_stats.profile import (
    SCHEMA_URL_1_2_0,
    SCHEMA_URL_1_3_0,
    LinkGrouping,
    StatisticEntry,
    load_profile_text,
    parse_profile,
)

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
TEXT = (SPEC / "profile-1-3-example.json").read_text("utf-8")
EXAMPLE: Dict[str, Any] = json.loads(TEXT)
SCHEMA: Dict[str, Any] = json.loads((SPEC / "profile-1-3.schema.json").read_text("utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

PAYMENT = ("profile.tables.payment.columns.amount.statistics", "profile.tables.payment.columns")


def _edit(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def _amount(document: Dict[str, Any]) -> list:
    return document["tables"]["payment"]["columns"]["amount"]["statistics"]


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


def _statistics(profile: Any) -> Dict[str, StatisticEntry]:
    return {
        "by_number": profile.tables["payment"].columns["amount"].statistics[1],
        "by_link": profile.tables["payment"].columns["amount"].statistics[2],
        "per": profile.tables["payment"].columns["segment"].statistics[0],
    }


def test_the_example_validates_and_reads_every_addition() -> None:
    assert not list(VALIDATOR.iter_errors(EXAMPLE))
    assert EXAMPLE["_schemaURL"] == SCHEMA["$id"] == SCHEMA_URL_1_3_0
    profile, document = load_profile_text(TEXT)
    assert document == EXAMPLE
    found = _statistics(profile)
    assert found["by_number"].group_by == "attempts"
    assert [group.key for group in found["by_number"].groups] == ["1", "2", "3"]
    assert found["by_link"].group_by is None
    assert found["by_link"].group_by_link == LinkGrouping(
        parent_table="plan",
        parent_columns=("plan_id",),
        child_columns=("plan_id",),
        column="tier",
    )
    assert found["per"].per == ("subscriber_id",)
    assert found["per"].group_by is None and found["per"].group_by_link is None
    assert set(profile.references) == {"plan"}


def test_the_earlier_schemas_do_not_know_the_additions() -> None:
    older = json.loads((SPEC / "profile-1-2.schema.json").read_text("utf-8"))
    assert list(Draft202012Validator(older).iter_errors(EXAMPLE))


def test_a_statistic_of_an_earlier_format_has_no_link_and_no_per() -> None:
    profile = parse_profile(json.loads((SPEC / "profile-1-2-example.json").read_text("utf-8")))
    for table in profile.tables.values():
        for column in table.columns.values():
            for statistic in column.statistics:
                assert statistic.group_by_link is None and statistic.per == ()


@pytest.mark.parametrize("index", [1, 2])
def test_an_earlier_format_that_holds_an_addition_is_refused(index: int) -> None:
    document = _edit(lambda d: d.update(_schemaURL=SCHEMA_URL_1_2_0))
    del document["references"]
    document["tables"]["payment"]["columns"]["segment"]["statistics"] = []
    for keep in (1, 2):
        if keep != index:
            _amount(document)[keep] = None
    document["tables"]["payment"]["columns"]["amount"]["statistics"] = [
        item for item in _amount(document) if item is not None
    ]
    assert "1-3-0" in _refused(document)


def test_an_earlier_format_that_holds_per_is_refused() -> None:
    document = _edit(lambda d: d.update(_schemaURL=SCHEMA_URL_1_2_0))
    del document["references"]
    document["tables"]["payment"]["columns"]["amount"]["statistics"] = [_amount(document)[0]]
    message = _refused(document)
    assert "per" in message and "1-3-0" in message


@pytest.mark.parametrize(
    ("change", "where"),
    [
        # groupBy on a number column
        (lambda s: s[1].update(groupBy="nope"), "groupBy"),
        (lambda s: s[1].update(groupBy="paid_at"), "groupBy"),
        (lambda s: s[1].update(groupBy="amount"), "groupBy"),
        (lambda s: s[1]["groups"][0].update(group="01"), "groups"),
        (lambda s: s[1]["groups"][0].update(group="1.0"), "groups"),
        (lambda s: s[1]["groups"][0].update(group="1e3"), "groups"),
        (lambda s: s[1]["groups"][0].update(group="-0"), "groups"),
        (lambda s: s[1]["groups"][0].update(group=""), "groups"),
        # groupByLink
        (lambda s: s[2].update(groupBy="attempts"), "at most one of groupBy and groupByLink"),
        (lambda s: s[2]["groupByLink"].update(extra=1), "groupByLink"),
        (lambda s: s[2]["groupByLink"].pop("column"), "groupByLink"),
        (lambda s: s[2]["groupByLink"]["parent"].update(table="nothing"), "parent.table"),
        (lambda s: s[2]["groupByLink"]["parent"].update(columns=["nope"]), "parent.columns"),
        (lambda s: s[2]["groupByLink"]["parent"].update(columns=[]), "groupByLink"),
        (
            lambda s: s[2]["groupByLink"]["parent"].update(columns=["plan_id", "tier"]),
            "same number of columns",
        ),
        (lambda s: s[2]["groupByLink"].update(columns=["nope"]), "groupByLink.columns"),
        (lambda s: s[2]["groupByLink"].update(columns=["plan_id", "plan_id"]), "same number"),
        (lambda s: s[2]["groupByLink"].update(column="nope"), "groupByLink.column"),
        (lambda s: s[2]["groups"][0].update(group=""), "groups"),
        # a statistic with per
        (lambda s: s.append({"extra": 1}), "statistics"),
    ],
)
def test_a_broken_grouping_is_refused_where_it_breaks(
    change: Callable[[Any], None], where: str
) -> None:
    document = _edit(lambda d: change(_amount(d)))
    assert where in _refused(document)


@pytest.mark.parametrize(
    ("change", "where"),
    [
        (lambda s: s.update(per=[]), "per"),
        (lambda s: s.update(per=["segment"]), "own column"),
        (lambda s: s.update(per=["subscriber_id", "subscriber_id"]), "twice"),
        (lambda s: s.update(per=["nope"]), "columns of its table"),
        (lambda s: s.update(per="subscriber_id"), "per"),
        (lambda s: s.update(per=["c%d" % n for n in range(9)]), "1 to 8"),
        (lambda s: s.update(groupBy="plan_id"), "holds no groupBy, groupByLink or groupByBins"),
        (
            lambda s: s.update(
                groupByLink={
                    "parent": {"table": "plan", "columns": ["plan_id"]},
                    "columns": ["plan_id"],
                    "column": "tier",
                }
            ),
            "holds no groupBy, groupByLink or groupByBins",
        ),
    ],
)
def test_a_broken_per_is_refused_where_it_breaks(change: Callable[[Any], None], where: str) -> None:
    document = _edit(
        lambda d: change(d["tables"]["payment"]["columns"]["segment"]["statistics"][0])
    )
    assert where in _refused(document)


def test_per_names_at_most_eight_distinct_columns() -> None:
    document = copy.deepcopy(EXAMPLE)
    columns = document["tables"]["payment"]["columns"]
    names = ["k%d" % n for n in range(8)]
    for name in names:
        columns[name] = {"domain": "category"}
    columns["segment"]["statistics"][0]["per"] = names
    assert parse_profile(document).tables["payment"].columns["segment"].statistics[0].per == tuple(
        names
    )
    columns["k8"] = {"domain": "category"}
    columns["segment"]["statistics"][0]["per"] = names + ["k8"]
    assert "1 to 8" in _refused(document)


def test_a_link_to_a_table_of_the_profile_is_read_as_a_cell_is_written() -> None:
    document = copy.deepcopy(EXAMPLE)
    link = _amount(document)[2]["groupByLink"]
    link["parent"] = {"table": "subscriber", "columns": ["subscriber_id"]}
    link["columns"] = ["subscriber_id"]
    link["column"] = "tenure_months"
    for group, key in zip(_amount(document)[2]["groups"], ("12.5", "6"), strict=True):
        group["group"] = key
    assert parse_profile(document).tables["payment"].columns["amount"].statistics[2].groups
    _amount(document)[2]["groups"][0]["group"] = "12.50"
    assert "keyed by its value" in _refused(document)


def test_a_statistic_that_differs_in_its_link_or_per_is_not_a_repeat() -> None:
    document = copy.deepcopy(EXAMPLE)
    _amount(document).append(copy.deepcopy(_amount(document)[2]))
    assert "repeats" in _refused(document)
    _amount(document)[-1]["groupByLink"]["column"] = "monthly_price"
    assert len(parse_profile(document).tables["payment"].columns["amount"].statistics) == 4
