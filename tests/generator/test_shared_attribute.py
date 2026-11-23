# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column's ``sharedAttribute`` is generated once and copied along one path (ADR 0022 section 4).

On kernel 0.2.0 the members of an attribute (columns of exposes of one contract that carry it) are read
together. The owner is the member every other member reaches by following relationships, on exactly one
path; it keeps its own draw, and every other member is the copy ``derive: <member>`` over the path to the
owner, named by the attribute (EXPR-SPEC-2 section 9). What is pinned:

* **A chain.** Line to order to customer: line and order hold the customer's value on every row whose path
  reaches a customer row, and NULL where it does not; a member in the owner's own expose copies the owner's
  column within the row.
* **The refusals.** No owner, two paths to it, a path through an expose the Simulation does not generate:
  ``FLX-6210`` naming the attribute and its members. A key or foreign key member stays listed as declared
  and not generated, with its reason.
* **Kernel 0.1.0 changes nothing**, and the column path's cells under the copies are the per-row path's rows.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pytest

from flux_engine.expr2.errors import Expr2Error
from flux_engine.generator import columns
from flux_engine.generator.coverage import coverage_of_plan
from flux_engine.generator.relations import related_columns
from flux_engine.generator.rows import generate_row_slice, generate_rows
from flux_engine.generator.rules import declared_not_generated
from flux_engine.seam.coverage import Origin
from flux_engine.seam.declared import KIND_SHARED_ATTRIBUTE, REASON_KEY_COLUMN
from flux_engine.seam.keys import compile_keys

from .test_relations import _column, _expose, _primary, _rel
from .test_rules_over_links import _by_both_paths
from .test_shared_copies import plans_of as shared_plans_of
from .test_shared_copies import root_for, table

pytestmark = pytest.mark.unit

CUSTOMERS, ORDERS, LINES = 20, 90, 400
LEVELS = {"levels": {"north": "0.5", "south": "0.3", "west": "0.2"}}


def _member(name: str, attribute: str = "region", type_: str = "STRING") -> Dict[str, Any]:
    return {**_column(name, type_, False), "sharedAttribute": attribute}


def chain_contract(
    *, attribute: str = "region", plain: bool = False, extra_line_link: bool = False
) -> Dict[str, Any]:
    """Customer, order and line, each carrying ``region``; customer also carries ``region_alt``."""

    def member(name: str) -> Dict[str, Any]:
        return _column(name, "STRING", False) if plain else _member(name, attribute)

    line_links = [_rel("order", "orders", ["l_order"], ["o_id"])]
    if extra_line_link:
        line_links.append(_rel("buyer", "customer", ["l_buyer"], ["cu_id"]))
    line_columns = [_column("l_id", "INT"), _column("l_order", "INT", False), member("region")]
    if extra_line_link:
        line_columns.append(_column("l_buyer", "INT", False))
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.attribute",
        "exposes": [
            _expose(
                "customer",
                [_column("cu_id", "INT"), member("region"), member("region_alt")],
                _primary("customer", "cu_id"),
            ),
            _expose(
                "orders",
                [_column("o_id", "INT"), _column("o_customer", "INT", False), member("region")],
                {
                    **_primary("orders", "o_id"),
                    "relationships": [_rel("customer", "customer", ["o_customer"], ["cu_id"])],
                },
            ),
            _expose(
                "line",
                line_columns,
                {**_primary("line", "l_id"), "relationships": line_links},
            ),
        ],
    }


def statistics(*, buyer: bool = False) -> Dict[str, Tuple[Dict[str, Any], int]]:
    declared: Dict[str, Tuple[Dict[str, Any], int]] = {
        "customer": ({"region": LEVELS, "region_alt": LEVELS}, CUSTOMERS),
        "orders": (
            {"o_customer": {"nullFraction": "0.1"}, "region": {"levels": {"a": "0.5", "b": "0.5"}}},
            ORDERS,
        ),
        "line": (
            {
                "l_order": {"nullFraction": "0.1"},
                "region": {"levels": {"a": "0.5", "b": "0.5"}},
            },
            LINES,
        ),
    }
    if buyer:
        declared["line"][0]["l_buyer"] = {"nullFraction": "0.1"}
    return declared


def plans_of(contract: Mapping[str, Any], **kwargs: Any) -> Dict[str, Any]:
    names = {c["name"] for e in contract["exposes"] for c in e["contract"]["schema"]}
    return shared_plans_of(contract, declared=statistics(buyer="l_buyer" in names), **kwargs)


@pytest.fixture(scope="module")
def world() -> Dict[str, List[Dict[str, Any]]]:
    plans = plans_of(chain_contract())
    root = root_for()
    return {e: table(p, generate_rows(p, root=root)) for e, p in plans.items()}


def _by(rows: Sequence[Mapping[str, Any]], name: str) -> Dict[Any, Mapping[str, Any]]:
    return {row[name]: row for row in rows}


# ---------------------------------------------------------------------------
# A chain: line to order to customer
# ---------------------------------------------------------------------------


def test_line_and_order_copy_the_customers_value_on_every_row_whose_path_reaches_a_customer(
    world: Mapping[str, List[Dict[str, Any]]],
) -> None:
    customers, orders = _by(world["customer"], "cu_id"), _by(world["orders"], "o_id")
    reached = cut_at_order = cut_at_customer = 0
    for row in world["orders"]:
        if row["o_customer"] is None:
            assert row["region"] is None
        else:
            assert row["region"] == customers[row["o_customer"]]["region"] is not None
    for row in world["line"]:
        if row["l_order"] is None:
            assert row["region"] is None
            cut_at_order += 1
            continue
        order = orders[row["l_order"]]
        if order["o_customer"] is None:
            assert row["region"] is None
            cut_at_customer += 1
            continue
        assert row["region"] == customers[order["o_customer"]]["region"] is not None
        reached += 1
    assert reached > 250 and cut_at_order > 10 and cut_at_customer > 10
    assert len({row["region"] for row in world["line"]}) > 1


def test_the_owner_keeps_its_own_draw_and_a_member_of_its_expose_copies_it_within_the_row(
    world: Mapping[str, List[Dict[str, Any]]],
) -> None:
    rows = world["customer"]
    assert {row["region"] for row in rows} <= set(LEVELS["levels"])
    assert len({row["region"] for row in rows}) > 1
    assert all(row["region_alt"] == row["region"] for row in rows)


def test_each_member_is_the_copy_rule_over_its_path_and_the_owner_has_none() -> None:
    plans = plans_of(chain_contract())
    assert plans["customer"].rules is not None
    (alt,) = plans["customer"].rules.rules
    assert (alt.id, alt.column, alt.text, alt.paths, alt.origin) == (
        "region",
        "region_alt",
        "region",
        (),
        "shared",
    )
    (order_rule,) = plans["orders"].rules.rules
    assert (order_rule.id, order_rule.text, order_rule.paths, order_rule.language) == (
        "region",
        "customer.region",
        (("customer",),),
        None,
    )
    (line_rule,) = plans["line"].rules.rules
    assert (line_rule.id, line_rule.text, line_rule.paths, line_rule.language) == (
        "region",
        "order.customer.region",
        (("order", "customer"),),
        "flux-expr/2",
    )
    assert plans["line"].rules.as_document()["rules"][0]["language"] == "flux-expr/2"
    # The line reads the plans of both exposes along its path.
    assert sorted(name for name, _ in plans["line"].reads) == ["customer", "orders"]
    # The owner's column is no copy: it keeps its draw.
    assert "region" not in plans["customer"].rules.determined


def test_the_coverage_names_a_copy_by_its_attribute() -> None:
    plans = plans_of(chain_contract())
    for expose_id, column in (("orders", "region"), ("line", "region"), ("customer", "region_alt")):
        row = {r.column: r for r in coverage_of_plan(plans[expose_id]).columns}[column]
        assert (row.origin, row.decided_by) == (Origin.COPIED, "rule")
        assert row.source == "contract.sharedAttribute.region"
    owner = {r.column: r for r in coverage_of_plan(plans["customer"]).columns}["region"]
    assert owner.origin is Origin.DECLARED


# ---------------------------------------------------------------------------
# Refusals, and what stays listed
# ---------------------------------------------------------------------------


def _refused(contract: Mapping[str, Any], **kwargs: Any) -> str:
    with pytest.raises(Expr2Error) as caught:
        plans_of(contract, **kwargs)
    assert caught.value.code.code == "FLX-6210", str(caught.value)
    return str(caught.value)


def test_an_attribute_with_no_owner_is_refused_naming_the_members() -> None:
    contract = chain_contract()
    # The customer no longer reaches or is reached by the line: drop the order's link and the line's.
    contract["exposes"][1]["semantics"]["relationships"] = []
    message = _refused(contract)
    assert "sharedAttribute 'region'" in message and "no owner" in message
    assert "customer.region" in message and "orders.region" in message and "line.region" in message


def test_an_attribute_reached_on_two_paths_is_refused_naming_the_paths() -> None:
    message = _refused(chain_contract(extra_line_link=True))
    assert "sharedAttribute 'region'" in message
    assert "'line' reaches the owner 'customer' on more than one path" in message
    assert "buyer" in message and "order.customer" in message


def test_a_path_through_an_expose_the_simulation_does_not_generate_is_refused() -> None:
    message = _refused(chain_contract(), only=("orders", "line"))
    assert "sharedAttribute 'region'" in message and "line.region" in message
    assert "fidelity" in message


def test_a_member_that_declares_a_null_fraction_is_refused_as_its_nulls_would_violate_the_copy() -> (
    None
):
    """EXPR-SPEC-2 section 6: the member's own NULLs come first, and a copy allows no violating row."""
    declared = statistics()
    members, rows = declared["line"]
    declared["line"] = ({**members, "region": {"nullFraction": "0.2"}}, rows)
    with pytest.raises(Expr2Error) as caught:
        shared_plans_of(chain_contract(), declared=declared)
    assert caught.value.code.code == "FLX-6204", str(caught.value)
    message = str(caught.value)
    assert "emit 'line'" in message and "'region'" in message and "'region'" in message
    assert "NULLs come first" in message


def test_a_key_or_foreign_key_member_stays_listed_with_its_reason() -> None:
    contract = chain_contract(plain=True)
    contract["exposes"][0]["contract"]["schema"][0]["sharedAttribute"] = "who"
    contract["exposes"][1]["contract"]["schema"][1]["sharedAttribute"] = "who"
    keys = compile_keys(contract)
    assert keys is not None
    found = declared_not_generated(
        contract_document=contract,
        expose_id="orders",
        settings=None,
        counter_based=True,
        has_fidelity=True,
        copies=True,
        related=related_columns(keys.expose("orders")),
    )
    (row,) = found
    assert (row.kind, row.locator, row.names) == (
        KIND_SHARED_ATTRIBUTE,
        "contract.schema.o_customer.sharedAttribute",
        ("who",),
    )
    assert row.reason == REASON_KEY_COLUMN
    plans = plans_of(contract)
    assert plans["orders"].rules is None  # nothing else to copy: the emit has no rule
    # The owner of the attribute is no declaration left out.
    assert not declared_not_generated(
        contract_document=contract,
        expose_id="customer",
        settings=None,
        counter_based=True,
        has_fidelity=True,
        copies=True,
        related=related_columns(keys.expose("customer")),
    )


def test_an_attribute_with_one_member_has_nothing_to_copy_and_nothing_is_listed() -> None:
    contract = chain_contract(plain=True)
    contract["exposes"][0]["contract"]["schema"][1]["sharedAttribute"] = "alone"
    plans = plans_of(contract)
    assert all(plan.rules is None for plan in plans.values())
    keys = compile_keys(contract)
    assert keys is not None
    assert not declared_not_generated(
        contract_document=contract,
        expose_id="customer",
        settings=None,
        counter_based=True,
        has_fidelity=True,
        copies=True,
        related=related_columns(keys.expose("customer")),
    )


def test_kernel_0_1_0_and_a_plain_emit_keep_every_attribute_listed() -> None:
    contract = chain_contract()
    keys = compile_keys(contract)
    assert keys is not None
    for kwargs in (
        {"counter_based": False, "has_fidelity": True},
        {"counter_based": True},
        {"counter_based": True, "has_fidelity": True},  # kernel 0.2.0 without copies: true
    ):
        found = declared_not_generated(
            contract_document=contract,
            expose_id="line",
            settings=None,
            related=related_columns(keys.expose("line")),
            **kwargs,
        )
        (row,) = found
        assert (row.kind, row.names, row.reason) == (KIND_SHARED_ATTRIBUTE, ("region",), "")


def test_kernel_0_1_0_plans_no_copy_and_gives_the_plans_it_gave() -> None:
    shared = plans_of(chain_contract(), counter_based=False)
    plain = plans_of(chain_contract(plain=True), counter_based=False)
    assert all(plan.rules is None for plan in shared.values())
    for expose_id, plan in shared.items():
        assert plan.digest() == plain[expose_id].digest()


# ---------------------------------------------------------------------------
# The column path and the per-row path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chunk", [5, 64])
def test_the_column_paths_cells_under_the_copies_are_the_per_row_paths_rows(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    plans = plans_of(chain_contract())
    root = root_for()
    for expose_id, plan in plans.items():
        by_columns, by_rows, taken = _by_both_paths(
            monkeypatch, lambda plan=plan: generate_rows(plan, root=root)
        )
        assert by_columns == by_rows, expose_id
        assert taken and all(taken), expose_id


@pytest.mark.parametrize(("start", "stop"), [(0, 1), (3, 40), (117, 118), (390, 400)])
def test_a_slice_of_a_copying_plan_is_the_whole_tables_slice(start: int, stop: int) -> None:
    plan = plans_of(chain_contract())["line"]
    root = root_for()
    whole = generate_rows(plan, root=root)
    assert generate_row_slice(plan, start, stop, root=root) == whole[start:stop]
