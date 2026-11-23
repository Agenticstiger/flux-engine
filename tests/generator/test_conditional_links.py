# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A conditional link exists only where its condition holds (ADR 0022 section 5).

The contract is ``test_shared_copies``'s: ``d`` points at ``x`` and copies ``region`` through it,
``c`` points at ``b`` through a two-column foreign key and copies ``region`` and ``tier``. A
condition on a link clears the relationship's columns on a row where it is not TRUE, and so a
copy through it reads NULL there. Every other row's foreign key is what the bundle without the
condition draws.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.expr2.errors import Expr2Error
from flux_engine.generator import columns, rules
from flux_engine.generator.rows import GeneratorPlan, generate_row_slice, generate_rows
from flux_engine.seam.links import LinkEntry

from .test_rules_over_links import _by_both_paths
from .test_shared_copies import D, plans_of, root_for, shared_contract, table

pytestmark = pytest.mark.unit

SIM_EMIT = "d"
CUT = 100


def entry(emit: str, relationship: str, when: str) -> LinkEntry:
    return LinkEntry(emit=emit, relationship=relationship, when=when)


D_LINK = {"d": [entry("d", "owner", f"d_id > {CUT}")]}
C_LINK = {"c": [entry("c", "link", "c_x > 7")]}


def rows_of(plan: GeneratorPlan) -> List[Dict[str, Any]]:
    return table(plan, generate_rows(plan, root=root_for()))


@pytest.fixture(scope="module")
def plain() -> Dict[str, List[Dict[str, Any]]]:
    return {name: rows_of(plan) for name, plan in plans_of(shared_contract()).items()}


@pytest.fixture(scope="module")
def conditional() -> Dict[str, List[Dict[str, Any]]]:
    plans = plans_of(shared_contract(), links={**D_LINK, **C_LINK})
    return {name: rows_of(plan) for name, plan in plans.items()}


def test_no_row_whose_condition_is_not_true_holds_a_link(
    conditional: Mapping[str, List[Dict[str, Any]]],
) -> None:
    rows = conditional["d"]
    assert len(rows) == D
    for row in rows:
        if row["d_id"] <= CUT:
            assert row["d_x"] is None
    assert any(row["d_x"] is not None for row in rows if row["d_id"] > CUT)
    for row in conditional["c"]:
        if row["c_x"] is None or row["c_x"] <= 7:
            assert row["c_x"] is None and row["c_y"] is None


def test_every_other_rows_foreign_key_is_what_the_bundle_without_the_condition_draws(
    plain: Mapping[str, List[Dict[str, Any]]], conditional: Mapping[str, List[Dict[str, Any]]]
) -> None:
    for expose_id, key in (("d", "d_x"), ("c", "c_x")):
        kept = cleared = 0
        for before, after in zip(plain[expose_id], conditional[expose_id], strict=True):
            if after[key] is not None:
                assert after[key] == before[key]
                kept += 1
            else:
                cleared += 1
        assert kept > 0 and cleared > 0, expose_id
    # A column no link touches is the bundle's without the condition, row for row.
    assert [row["d_id"] for row in plain["d"]] == [row["d_id"] for row in conditional["d"]]
    assert plain["x"] == conditional["x"] and plain["b"] == conditional["b"]


def test_a_copy_through_the_link_is_null_where_the_condition_fails(
    plain: Mapping[str, List[Dict[str, Any]]], conditional: Mapping[str, List[Dict[str, Any]]]
) -> None:
    regions = {row["x_id"]: row["region"] for row in conditional["x"]}
    for row in conditional["d"]:
        if row["d_x"] is None:
            assert row["region"] is None
        else:
            assert row["region"] == regions[row["d_x"]]
    for row in conditional["c"]:
        if row["c_x"] is None:
            assert row["region"] is None and row["tier"] is None
    # Without the condition the same rows copy a value.
    held = [
        (before, after)
        for before, after in zip(plain["d"], conditional["d"], strict=True)
        if after["d_x"] is None and before["d_x"] is not None
    ]
    assert held and all(before["region"] is not None for before, _ in held)


@pytest.mark.parametrize("chunk", [5, 64])
def test_the_column_paths_cells_under_a_condition_are_the_per_row_paths_rows(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    plans = plans_of(shared_contract(), links={**D_LINK, **C_LINK})
    root = root_for()
    for expose_id, plan in plans.items():
        by_columns, by_rows, taken = _by_both_paths(
            monkeypatch, lambda plan=plan: generate_rows(plan, root=root)
        )
        assert by_columns == by_rows, expose_id
        assert taken and all(taken), expose_id


def test_the_array_path_leaves_a_condition_to_the_rows_and_the_two_agree() -> None:
    plan = plans_of(shared_contract(), links=D_LINK)["d"]
    root = root_for()
    cells = generate_row_slice(plan, 0, 40, root=root)
    assert rules._apply_on_arrays(plan, cells, 0, root) is None
    assert generate_row_slice(plan, 0, 40, root=root) == generate_rows(plan, root=root)[:40]


@pytest.mark.parametrize(("start", "stop"), [(0, 1), (3, 40), (99, 102), (290, 300)])
def test_a_slice_of_a_conditional_plan_is_the_whole_tables_slice(start: int, stop: int) -> None:
    plan = plans_of(shared_contract(), links=D_LINK)["d"]
    root = root_for()
    assert (
        generate_row_slice(plan, start, stop, root=root)
        == generate_rows(plan, root=root)[start:stop]
    )


def test_the_plan_lists_a_condition_only_when_it_declares_one_and_runs_it_before_the_copy() -> None:
    without = plans_of(shared_contract())["d"]
    declared = plans_of(shared_contract(), links=D_LINK)["d"]
    assert "links" not in without.rules.as_document()  # type: ignore[union-attr]
    document = declared.rules.as_document()  # type: ignore[union-attr]
    assert document["links"] == [{"relationship": "owner", "when": f"d_id > {CUT}", "order": 0}]
    assert declared.rules.order == without.rules.order  # type: ignore[union-attr]
    assert declared.as_document() != without.as_document()


def test_a_condition_on_a_plan_with_no_other_rule_still_makes_a_rule_plan() -> None:
    contract = shared_contract(d_shared=())
    assert plans_of(contract)["d"].rules is None
    plan = plans_of(contract, links=D_LINK)["d"]
    assert plan.rules is not None and not plan.rules.order and plan.rules.links
    assert all(row["d_x"] is None for row in rows_of(plan) if row["d_id"] <= CUT)


def test_a_condition_that_reads_a_column_a_rule_sets_waits_for_that_rule() -> None:
    contract = shared_contract()
    contract["exposes"][4]["contract"]["schema"].append(
        {"name": "flag", "type": "INT", "required": False}
    )
    contract["exposes"][4]["contract"]["invariants"] = [
        {"id": "flag_rule", "derive": "flag", "expr": "CASE WHEN d_id > 150 THEN 1 ELSE 0 END"}
    ]
    declared = {"d": ({"d_x": {"nullFraction": "0.1"}, "region": {"levels": {"a": "1"}}}, D)}
    from .test_shared_copies import statistics

    statistics_ = {**statistics(), **declared}
    plan = plans_of(
        contract,
        declared=statistics_,
        links={"d": [entry("d", "owner", "flag = 1")]},
    )["d"]
    assert plan.rules is not None
    assert plan.rules.order.index("flag") < plan.rules.order.index("region")
    (link,) = plan.rules.links
    assert plan.rules.order[link.after - 1] == "flag" and plan.rules.order[link.after] == "region"
    for row in rows_of(plan):
        if row["d_id"] <= 150:
            assert row["d_x"] is None and row["region"] is None


def test_a_condition_that_reads_what_the_link_feeds_is_a_cycle() -> None:
    with pytest.raises(Expr2Error) as caught:
        plans_of(shared_contract(), links={"d": [entry("d", "owner", "region = 'north'")]})
    assert caught.value.code.code == "FLX-6208"


def test_a_condition_that_reads_beyond_the_row_is_refused() -> None:
    with pytest.raises(Expr2Error) as caught:
        plans_of(shared_contract(), links={"d": [entry("d", "owner", "owner.region = 'north'")]})
    assert caught.value.code.code == "FLX-6210"


def test_kernel_0_1_0_refuses_a_condition_and_names_the_kernel() -> None:
    with pytest.raises(FluxError) as caught:
        plans_of(shared_contract(), counter_based=False, links=D_LINK)
    assert caught.value.code.code == "FLX-3105" and "0.2.0" in str(caught.value)


def test_an_emit_with_no_fidelity_block_refuses_a_condition() -> None:
    with pytest.raises(Expr2Error) as caught:
        plans_of(shared_contract(), links=D_LINK, has_fidelity=False)
    assert caught.value.code.code == "FLX-6210"


def _refuse(contract: Mapping[str, Any], links: Mapping[str, Sequence[LinkEntry]]) -> str:
    with pytest.raises(Exception) as caught:
        plans_of(contract, links=links)
    assert caught.value.code.code == "FLX-3403", str(caught.value)  # type: ignore[attr-defined]
    return str(caught.value)


def test_a_condition_on_an_inner_relationship_is_refused() -> None:
    contract = shared_contract()
    contract["exposes"][4]["semantics"]["relationships"][0]["joinType"] = "inner"
    declared = {"d": ({"region": {"levels": {"a": "1"}}}, D)}
    from .test_shared_copies import statistics

    with pytest.raises(Exception) as caught:
        plans_of(contract, declared={**statistics(), **declared}, links=D_LINK)
    assert caught.value.code.code == "FLX-3403" and "inner" in str(caught.value)  # type: ignore[attr-defined]


def test_a_condition_on_a_relationship_whose_columns_are_a_key_is_refused() -> None:
    contract = shared_contract()
    contract["exposes"][4]["semantics"]["relationships"][0]["cardinality"] = "one_to_one"
    from .test_shared_copies import statistics

    declared = {"d": ({"region": {"levels": {"a": "1"}}}, 9)}
    with pytest.raises(Exception) as caught:
        plans_of(contract, declared={**statistics(), **declared}, links=D_LINK)
    assert caught.value.code.code == "FLX-3403" and "key" in str(caught.value)  # type: ignore[attr-defined]


def test_a_condition_on_a_member_of_a_several_column_key_is_refused() -> None:
    message = _refuse(shared_contract(), {"b": [entry("b", "bx", "b_y > 1")]})
    assert "key" in message and "bx" in message


def test_a_condition_on_a_relationship_that_implies_another_is_refused() -> None:
    contract = shared_contract()
    contract["exposes"][3]["semantics"]["relationships"].append(
        {
            "name": "c_x_only",
            "to": "x",
            "fromColumns": ["c_x"],
            "toColumns": ["x_id"],
            "cardinality": "many_to_one",
        }
    )
    contract["exposes"][2]["semantics"]["relationships"][0]["toColumns"] = ["x_id"]
    message = _refuse(contract, {"c": [entry("c", "link", "c_y > 1")]})
    assert "c_x_only" in message and "implies" in message


def test_a_condition_on_a_relationship_another_implies_is_refused() -> None:
    contract = shared_contract()
    contract["exposes"][3]["semantics"]["relationships"].append(
        {
            "name": "c_x_only",
            "to": "x",
            "fromColumns": ["c_x"],
            "toColumns": ["x_id"],
            "cardinality": "many_to_one",
        }
    )
    contract["exposes"][2]["semantics"]["relationships"][0]["toColumns"] = ["x_id"]
    message = _refuse(contract, {"c": [entry("c", "c_x_only", "c_y > 1")]})
    assert "c_x_only" in message
