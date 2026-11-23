# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copy from a missing link is NULL or its declared sentinel (ADR 0022 section 6).

The contract is ``test_shared_copies``'s with one more column, ``score``, which ``x`` declares
and ``d`` copies through ``owner`` by a ``derive`` over the path (a ``sharedColumns`` copy allows no
violating row, so its column declares no ``nullFraction``; this rule has a ``rows`` band that admits
the column's own NULLs, which come first). A row whose path reaches no row (a NULL key, or a condition
that cleared the link) holds the sentinel; a row whose path reaches a row whose value is NULL
keeps NULL, and so does a row whose own value is NULL.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator import columns
from flux_engine.generator.rows import GeneratorPlan, generate_rows
from flux_engine.seam.links import LinkEntry

from .test_conditional_links import rows_of
from .test_rules_over_links import _by_both_paths
from .test_shared_copies import plans_of, root_for, shared_contract, statistics, table

pytestmark = pytest.mark.unit

CUT = 100


def contract() -> Dict[str, Any]:
    found = shared_contract(d_shared=("region",))
    found["exposes"][0]["contract"]["schema"].append(
        {"name": "score", "type": "INT", "required": False}
    )
    found["exposes"][4]["contract"]["schema"].append(
        {"name": "score", "type": "INT", "required": False}
    )
    found["exposes"][4]["contract"]["invariants"] = [
        {
            "id": "score_copy",
            "derive": "score",
            "expr": "owner.score",
            "mustBeLessThan": 100000,
            "unit": "rows",
        }
    ]
    return found


def declared() -> Dict[str, Any]:
    found = statistics()
    x_columns, x_rows = found["x"]
    found["x"] = (
        {**x_columns, "score": {"range": {"min": "0", "max": "9"}, "nullFraction": "0.3"}},
        x_rows,
    )
    d_columns, d_rows = found["d"]
    found["d"] = ({**d_columns, "score": {"nullFraction": "0.2"}}, d_rows)
    return found


def entry(
    sentinels: Mapping[str, str], when: Optional[str] = f"d_id > {CUT}", relationship: str = "owner"
) -> Dict[str, Sequence[LinkEntry]]:
    return {
        "d": [
            LinkEntry(
                emit="d",
                relationship=relationship,
                when=when,
                sentinels=tuple(sorted(sentinels.items())),
            )
        ]
    }


SENTINELS = {"region": "none", "score": "-1"}


def planned(links: Optional[Mapping[str, Sequence[LinkEntry]]] = None) -> Dict[str, GeneratorPlan]:
    return plans_of(contract(), declared=declared(), links=links)


@pytest.fixture(scope="module")
def world() -> Dict[str, List[Dict[str, Any]]]:
    plans = planned(entry(SENTINELS))
    root = root_for()
    return {name: table(plan, generate_rows(plan, root=root)) for name, plan in plans.items()}


def test_a_row_whose_link_reaches_no_row_holds_the_sentinel(
    world: Mapping[str, List[Dict[str, Any]]],
) -> None:
    parents = {row["x_id"]: row for row in world["x"]}
    from_key = from_condition = linked = 0
    for row in world["d"]:
        if row["d_x"] is None:
            if row["d_id"] > CUT:
                from_key += 1  # the foreign key itself drew NULL
            else:
                from_condition += 1
            assert row["region"] == "none"
        else:
            assert row["region"] == parents[row["d_x"]]["region"]
            linked += 1
    assert from_key > 0 and from_condition > 0 and linked > 0


def test_a_parent_whose_value_is_null_and_a_row_whose_own_value_is_null_keep_null(
    world: Mapping[str, List[Dict[str, Any]]],
) -> None:
    parents = {row["x_id"]: row for row in world["x"]}
    parent_null = own_null = missing = 0
    for row in world["d"]:
        if row["d_x"] is not None:
            assert row["score"] == parents[row["d_x"]]["score"] or row["score"] is None
            parent_null += row["score"] is None and parents[row["d_x"]]["score"] is None
        elif row["score"] is None:
            own_null += 1  # NULLs first: the row's own NULL stands over the sentinel
        else:
            assert row["score"] == -1
            missing += 1
    assert parent_null > 0 and own_null > 0 and missing > 0


def test_without_a_sentinel_the_copy_is_null_as_before() -> None:
    plain = rows_of(planned(entry({}, when=f"d_id > {CUT}"))["d"])
    assert all(row["region"] is None for row in plain if row["d_x"] is None)


def test_the_plan_records_a_sentinel_only_when_it_declares_one() -> None:
    without = planned(entry({}))["d"].rules
    with_ = planned(entry(SENTINELS))["d"].rules
    assert without is not None and with_ is not None
    assert all("sentinel" not in rule for rule in without.as_document()["rules"])
    recorded = {rule["id"]: rule.get("sentinel") for rule in with_.as_document()["rules"]}
    assert recorded == {"owner.region": "none", "score_copy": "-1"}


@pytest.mark.parametrize("chunk", [5, 64])
def test_the_column_paths_cells_under_sentinels_are_the_per_row_paths_rows(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    root = root_for()
    for expose_id, plan in planned(entry(SENTINELS)).items():
        by_columns, by_rows, taken = _by_both_paths(
            monkeypatch, lambda plan=plan: generate_rows(plan, root=root)
        )
        assert by_columns == by_rows, expose_id
        assert taken and all(taken), expose_id


def test_a_sentinel_without_a_condition_covers_a_null_key() -> None:
    links = {"d": [LinkEntry(emit="d", relationship="owner", sentinels=(("region", "none"),))]}
    rows = rows_of(planned(links)["d"])
    assert any(row["d_x"] is None for row in rows)
    assert all(row["region"] == "none" for row in rows if row["d_x"] is None)


def _refused(links: Mapping[str, Sequence[LinkEntry]], **kwargs: Any) -> str:
    with pytest.raises(FluxError) as caught:
        plans_of(contract(), declared=declared(), links=links, **kwargs)
    assert caught.value.code.code == "FLX-3105", str(caught.value)
    return str(caught.value)


@pytest.mark.parametrize(
    ("sentinels", "problem"),
    [
        ({"d_id": "0"}, "does not copy"),
        ({"nowhere": "0"}, "does not copy"),
        ({"score": "abc"}, "not a number"),
        ({"score": "1.5"}, "decimal places"),
        ({"score": "99999999999999999999"}, "range"),
    ],
)
def test_a_sentinel_must_name_a_copied_column_and_read_as_its_type(
    sentinels: Mapping[str, str], problem: str
) -> None:
    assert problem in _refused(entry(sentinels))


def test_a_sentinel_on_a_column_a_chain_copies_through_another_link_is_the_other_links() -> None:
    owner_only = {"c": [LinkEntry("c", "link", None, (("region", "none"),))]}
    plans_of(contract(), declared=declared(), links=owner_only)  # c copies region through link
    wrong = {"c": [LinkEntry("c", "link", None, (("c_x", "1"),))]}
    assert "does not copy" in _refused(wrong)


def test_kernel_0_1_0_refuses_a_sentinel_and_names_the_kernel() -> None:
    assert "0.2.0" in _refused(entry(SENTINELS), counter_based=False)
