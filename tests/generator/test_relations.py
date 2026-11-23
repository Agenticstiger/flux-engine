# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC section 8.1: the exposes one contract relates are generated together.

A small star in the shape of a store channel: dates, items, customers, sales
that reference all three, and returns that reference the sale they return
(one to one, through a two-column key) and its item. What is pinned:

* **Integrity.** Every key the contract declares is unique, and every foreign
  key value is a key its parent's rows hold, for every relationship.
* **Shape.** A foreign key that declares a draw keeps the declared shares and
  null fraction; a one-to-one child references each parent row once.
* **Determinism.** Slices, column-first generation and a second pass agree;
  keys do not move with the seed, and the picks a seed draws do.
* **Refusals.** Everything that cannot hold is refused before a row is drawn,
  with FLX-3403 and a message that says what to change.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError, get
from flux_engine.core.seedtree import SEED_TREE_VERSION, SeedTreeRoot, derive_root, digest_bytes
from flux_engine.generator.coverage import coverage_of_plan
from flux_engine.generator.relations import (
    ForeignKey,
    IdentifierKey,
    KeyGrid,
    relate_plans,
    related_columns,
)
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_column,
    generate_row_slice,
    generate_rows,
)
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.keys import compile_keys, mark_key_columns

pytestmark = pytest.mark.unit

SIM_ID = "sim-star"


def _column(name: str, type_: str, required: bool = True) -> Dict[str, Any]:
    return {"name": name, "type": type_, "required": required}


def _expose(
    expose_id: str, schema: List[Dict[str, Any]], semantics: Mapping[str, Any]
) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "contract": {"schema": schema},
        "binding": {"platform": "local", "format": "parquet", "location": {"path": expose_id}},
        "semantics": dict(semantics),
    }


def _primary(name: str, column: str) -> Dict[str, Any]:
    return {"entities": [{"name": name, "type": "primary", "expr": column}]}


def _rel(
    name: str,
    to: str,
    from_columns: Sequence[str],
    to_columns: Sequence[str],
    cardinality: str = "many_to_one",
    **rest: Any,
) -> Dict[str, Any]:
    return {
        "name": name,
        "to": to,
        "fromColumns": list(from_columns),
        "toColumns": list(to_columns),
        "cardinality": cardinality,
        **rest,
    }


def star_contract() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose(
                "date_dim",
                [_column("d_date_sk", "INT"), _column("d_year", "INT")],
                _primary("date", "d_date_sk"),
            ),
            _expose(
                "item",
                [_column("i_item_sk", "INT"), _column("i_brand", "STRING")],
                _primary("item", "i_item_sk"),
            ),
            _expose("customer", [_column("c_id", "STRING")], _primary("customer", "c_id")),
            _expose(
                "store_sales",
                [
                    _column("ss_sold_date_sk", "INT", False),
                    _column("ss_item_sk", "INT"),
                    _column("ss_ticket_number", "INT"),
                    _column("ss_customer_id", "STRING", False),
                    _column("ss_amount", "DECIMAL(10,2)"),
                ],
                {
                    "relationships": [
                        _rel("sold_date", "date_dim", ["ss_sold_date_sk"], ["d_date_sk"]),
                        _rel("item", "item", ["ss_item_sk"], ["i_item_sk"]),
                        _rel("customer", "customer", ["ss_customer_id"], ["c_id"]),
                    ]
                },
            ),
            _expose(
                "store_returns",
                [
                    _column("sr_item_sk", "INT"),
                    _column("sr_ticket_number", "INT"),
                    _column("sr_returned_date_sk", "INT", False),
                ],
                {
                    "relationships": [
                        _rel(
                            "sale",
                            "store_sales",
                            ["sr_item_sk", "sr_ticket_number"],
                            ["ss_item_sk", "ss_ticket_number"],
                            "one_to_one",
                        ),
                        _rel("item", "item", ["sr_item_sk"], ["i_item_sk"]),
                        _rel("returned", "date_dim", ["sr_returned_date_sk"], ["d_date_sk"]),
                    ]
                },
            ),
        ],
    }


def star_declarations() -> Dict[str, Tuple[Dict[str, Any], int]]:
    return {
        "date_dim": (
            {
                "d_date_sk": {"range": {"min": "2450000", "max": "2450999"}},
                "d_year": {"range": {"min": "1990", "max": "2010"}},
            },
            1000,
        ),
        "item": (
            {
                "i_item_sk": {"range": {"min": "1", "max": "200"}},
                "i_brand": {"support": {"values": ["a", "b", "c"]}},
            },
            200,
        ),
        "customer": ({}, 300),
        "store_sales": (
            {
                "ss_sold_date_sk": {
                    "nullFraction": "0.05",
                    "range": {"min": "2450100", "max": "2450400"},
                    "bins": ["2450100", "2450200", "2450400"],
                    "binWeights": ["0.8", "0.2"],
                },
                "ss_ticket_number": {"range": {"min": "1", "max": "500"}},
                "ss_customer_id": {"nullFraction": "0.1"},
                "ss_amount": {"range": {"min": "0", "max": "100"}},
            },
            3000,
        ),
        "store_returns": (
            {"sr_returned_date_sk": {"range": {"min": "2450300", "max": "2450600"}}},
            400,
        ),
    }


def _h(expose_id: str) -> str:
    return "sha256:" + format(sum(map(ord, expose_id)) % 16, "x") * 64


def related_plans(
    contract: Optional[Dict[str, Any]] = None,
    declarations: Optional[Dict[str, Tuple[Dict[str, Any], int]]] = None,
    *,
    only: Optional[Sequence[str]] = None,
    time_bounds: Optional[Mapping[str, str]] = None,
    generator_version: str = GENERATOR_VERSION,
) -> Dict[str, GeneratorPlan]:
    contract = star_contract() if contract is None else contract
    declarations = star_declarations() if declarations is None else declarations
    keys = compile_keys(contract)
    assert keys is not None
    plans: Dict[str, GeneratorPlan] = {}
    for expose in contract["exposes"]:
        expose_id = expose["exposeId"]
        if only is not None and expose_id not in only:
            continue
        expose_keys = keys.expose(expose_id)
        columns, rows = declarations[expose_id]
        plans[expose_id] = build_plan(
            mark_key_columns(compile_expose(expose), expose_keys),
            sim_id=SIM_ID,
            seam_digest=_h(expose_id),
            rows=rows,
            fidelity={"columns": columns},
            time_bounds=time_bounds,
            related=related_columns(expose_keys),
            generator_version=generator_version,
        )
    return relate_plans(plans, keys)


def root_for(seed: int = 42, seed_tree_version: str = SEED_TREE_VERSION) -> SeedTreeRoot:
    return derive_root(
        digest_bytes("sha256:" + "a" * 64), seed, seed_tree_version=seed_tree_version
    )


def generate_all(
    plans: Mapping[str, GeneratorPlan], root: Optional[SeedTreeRoot] = None
) -> Dict[str, Tuple[Tuple[Any, ...], ...]]:
    root = root_for() if root is None else root
    return {expose_id: generate_rows(plan, root=root) for expose_id, plan in plans.items()}


def values(
    plans: Mapping[str, GeneratorPlan],
    data: Mapping[str, Sequence[Tuple[Any, ...]]],
    expose_id: str,
    *columns: str,
) -> List[Tuple[Any, ...]]:
    names = [plan.column.name for plan in plans[expose_id].columns]
    indexes = [names.index(column) for column in columns]
    return [tuple(row[index] for index in indexes) for row in data[expose_id]]


@pytest.fixture(scope="module")
def star() -> Tuple[Dict[str, GeneratorPlan], Dict[str, Tuple[Tuple[Any, ...], ...]]]:
    plans = related_plans()
    return plans, generate_all(plans)


@pytest.fixture(scope="module")
def star_v2() -> Tuple[Dict[str, GeneratorPlan], Dict[str, Tuple[Tuple[Any, ...], ...]]]:
    """The same star under kernel 0.2.0: ``flux-key/2`` keys, ``permute/2`` and its picks."""
    plans = related_plans(generator_version=GENERATOR_VERSION_2)
    return plans, generate_all(plans, root_for(seed_tree_version=kernel2.SEED_TREE_VERSION))


#: The integrity tests run on the star under both kernels.
BOTH_KERNELS = pytest.mark.parametrize("star_of", ["star", "star_v2"])


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


@BOTH_KERNELS
def test_every_key_the_contract_declares_is_unique(
    star_of: str, request: pytest.FixtureRequest
) -> None:
    plans, data = request.getfixturevalue(star_of)
    keys = compile_keys(star_contract())
    assert keys is not None
    checked = 0
    for expose_id, expose_keys in keys.exposes.items():
        for key in (*expose_keys.keys, *expose_keys.instant_keys):
            rows = values(plans, data, expose_id, *key)
            assert len(set(rows)) == len(rows), (expose_id, key)
            checked += 1
    # date, item, customer, the sale's two-column key, the return's one-to-one key.
    assert checked == 5


@BOTH_KERNELS
def test_every_foreign_key_names_a_row_its_parent_holds(
    star_of: str, request: pytest.FixtureRequest
) -> None:
    plans, data = request.getfixturevalue(star_of)
    keys = compile_keys(star_contract())
    assert keys is not None
    checked = 0
    for expose_id, expose_keys in keys.exposes.items():
        for relationship in expose_keys.relationships:
            parent = set(values(plans, data, relationship.target, *relationship.to_columns))
            references = [
                row
                for row in values(plans, data, expose_id, *relationship.from_columns)
                if all(value is not None for value in row)
            ]
            assert references, relationship.name
            assert all(row in parent for row in references), (expose_id, relationship.name)
            checked += 1
    assert checked == 6


@BOTH_KERNELS
def test_a_one_to_one_child_references_each_parent_row_once(
    star_of: str, request: pytest.FixtureRequest
) -> None:
    plans, data = request.getfixturevalue(star_of)
    returned = values(plans, data, "store_returns", "sr_item_sk", "sr_ticket_number")
    assert len(set(returned)) == len(returned) == 400


@BOTH_KERNELS
def test_a_relationship_inside_another_holds_through_the_parent_row(
    star_of: str, request: pytest.FixtureRequest
) -> None:
    plans, data = request.getfixturevalue(star_of)
    sales = dict(
        zip(
            values(plans, data, "store_sales", "ss_item_sk", "ss_ticket_number"),
            values(plans, data, "store_sales", "ss_item_sk"),
            strict=True,
        )
    )
    for (item, ticket), (own_item,) in zip(
        values(plans, data, "store_returns", "sr_item_sk", "sr_ticket_number"),
        values(plans, data, "store_returns", "sr_item_sk"),
        strict=True,
    ):
        assert sales[(item, ticket)] == (own_item,)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_a_declared_foreign_key_keeps_its_shares_and_lands_on_parent_keys(star: Any) -> None:
    plans, data = star
    dates = [row[0] for row in values(plans, data, "store_sales", "ss_sold_date_sk")]
    present = [value for value in dates if value is not None]
    share_low = sum(1 for value in present if value < 2450200) / len(present)
    assert abs(share_low - 0.8) < 0.03
    assert set(present) <= {row[0] for row in values(plans, data, "date_dim", "d_date_sk")}
    driver = plans["store_sales"].relations.driver("ss_sold_date_sk")
    assert isinstance(driver, ForeignKey) and driver.pick == "declared"


def test_a_left_relationship_keeps_the_declared_nulls(star: Any) -> None:
    plans, data = star
    dates = [row[0] for row in values(plans, data, "store_sales", "ss_sold_date_sk")]
    customers = [row[0] for row in values(plans, data, "store_sales", "ss_customer_id")]
    assert abs(dates.count(None) / len(dates) - 0.05) < 0.015
    assert abs(customers.count(None) / len(customers) - 0.1) < 0.02


def test_a_foreign_key_with_no_draw_picks_parents_uniformly(star: Any) -> None:
    plans, data = star
    driver = plans["store_sales"].relations.driver("ss_customer_id")
    assert isinstance(driver, ForeignKey) and driver.pick == "uniform"
    counts = Counter(row[0] for row in values(plans, data, "store_sales", "ss_customer_id"))
    counts.pop(None, None)
    # About 2,700 references over 300 customers: nine each on average.
    assert len(counts) > 280
    assert max(counts.values()) < 30


def test_coverage_says_how_each_related_column_is_produced(star: Any) -> None:
    plans, _ = star
    rows = {row.column: row for row in coverage_of_plan(plans["store_sales"]).columns}
    assert (rows["ss_sold_date_sk"].origin.value, rows["ss_sold_date_sk"].decided_by) == (
        "declared",
        "relationship",
    )
    assert rows["ss_item_sk"].source == "semantics.relationships.item"
    assert (rows["ss_ticket_number"].origin.value, rows["ss_ticket_number"].decided_by) == (
        "derived",
        "key",
    )
    assert rows["ss_customer_id"].source == "semantics.relationships.customer"
    assert rows["ss_amount"].decided_by == "fidelity"
    key = coverage_of_plan(plans["customer"]).columns[0]
    assert (key.origin.value, key.decided_by, key.source) == (
        "derived",
        "key",
        "semantics+rowIndex",
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_a_slice_and_a_column_hold_what_the_whole_pass_holds(star: Any) -> None:
    plans, data = star
    root = root_for()
    for expose_id in ("store_sales", "store_returns"):
        plan = plans[expose_id]
        assert generate_row_slice(plan, 100, 250, root=root) == data[expose_id][100:250]
        for index, column_plan in enumerate(plan.columns):
            column = generate_column(plan, column_plan.column.name, root=root)
            assert list(column) == [row[index] for row in data[expose_id]], column_plan.column.name


def test_a_second_pass_and_a_second_plan_give_the_same_rows(star: Any) -> None:
    plans, data = star
    again = related_plans()
    assert {e: p.digest() for e, p in again.items()} == {e: p.digest() for e, p in plans.items()}
    assert generate_all(again) == data


def test_keys_do_not_move_with_the_seed_and_the_picks_it_draws_do(star: Any) -> None:
    plans, data = star
    other = generate_all(plans, root_for(seed=43))
    for expose_id, columns in (
        ("date_dim", ("d_date_sk",)),
        ("customer", ("c_id",)),
        ("store_sales", ("ss_item_sk", "ss_ticket_number")),
        ("store_returns", ("sr_item_sk", "sr_ticket_number")),
    ):
        assert values(plans, other, expose_id, *columns) == values(plans, data, expose_id, *columns)
    for expose_id, column in (
        ("store_sales", "ss_sold_date_sk"),
        ("store_sales", "ss_customer_id"),
        ("store_returns", "sr_returned_date_sk"),
    ):
        assert values(plans, other, expose_id, column) != values(plans, data, expose_id, column)


def test_a_plan_with_no_key_and_no_relationship_is_left_as_it_was() -> None:
    contract = star_contract()
    contract["exposes"].append(
        _expose("weather", [_column("w_temp", "INT")], {}),
    )
    declarations = star_declarations()
    declarations["weather"] = ({"w_temp": {"range": {"min": "-5", "max": "30"}}}, 10)
    plans = related_plans(contract, declarations)
    assert plans["weather"].relations is None
    assert "relations" not in plans["weather"].as_document()


def test_the_childs_digest_moves_when_its_parents_key_layout_moves() -> None:
    plans = related_plans()
    declarations = star_declarations()
    declarations["date_dim"][0]["d_date_sk"] = {"range": {"min": "2450000", "max": "2451999"}}
    moved = related_plans(declarations=declarations)
    assert moved["store_sales"].digest() != plans["store_sales"].digest()
    assert moved["item"].digest() == plans["item"].digest()


def test_a_nullable_one_to_one_foreign_key_keeps_its_nulls_and_distinct_parents() -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("u", [_column("u_id", "INT")], _primary("u", "u_id")),
            _expose(
                "p",
                [_column("p_id", "INT"), _column("p_user", "INT", False)],
                {
                    **_primary("p", "p_id"),
                    "relationships": [_rel("user", "u", ["p_user"], ["u_id"], "one_to_one")],
                },
            ),
        ],
    }
    plans = related_plans(
        contract, {"u": ({}, 300), "p": ({"p_user": {"nullFraction": "0.3"}}, 200)}
    )
    data = generate_all(plans)
    users = [row[0] for row in values(plans, data, "p", "p_user")]
    present = [value for value in users if value is not None]
    assert abs(users.count(None) / len(users) - 0.3) < 0.08
    assert len(set(present)) == len(present)
    assert set(present) <= set(range(1, 301))


@pytest.mark.parametrize("composite", [False, True])
def test_a_nullable_one_to_one_key_another_relationship_targets_is_refused(
    composite: bool,
) -> None:
    """A child copies its parent's key, so a key another table points at holds no null."""
    p_columns = [_column("p_id", "INT"), _column("p_user", "INT", False), _column("p_x", "INT")]
    target, source = (
        (["p_user", "p_x"], ["g_user", "g_x"]) if composite else (["p_user"], ["g_user"])
    )
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("u", [_column("u_id", "INT")], _primary("u", "u_id")),
            _expose(
                "p",
                p_columns,
                {
                    **_primary("p", "p_id"),
                    "relationships": [_rel("user", "u", ["p_user"], ["u_id"], "one_to_one")],
                },
            ),
            _expose(
                "g",
                [_column("g_user", "INT"), _column("g_x", "INT")],
                {"relationships": [_rel("p", "p", source, target, joinType="inner")]},
            ),
        ],
    }
    declarations = {
        "u": ({}, 300),
        "p": ({"p_user": {"nullFraction": "0.3"}, "p_x": {"range": {"min": "1", "max": "5"}}}, 200),
        "g": ({"g_x": {"range": {"min": "1", "max": "5"}}}, 100),
    }
    _refused("part of a key", contract=contract, declarations=declarations)


def test_a_declared_foreign_key_another_column_is_drawn_given_keeps_both() -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("s", [_column("s_id", "INT")], _primary("s", "s_id")),
            _expose(
                "v",
                [_column("v_store", "INT"), _column("v_kind", "STRING")],
                {"relationships": [_rel("store", "s", ["v_store"], ["s_id"])]},
            ),
        ],
    }
    declarations = {
        "s": ({}, 10),
        "v": (
            {
                "v_store": {"levels": {"2": "0.5", "7": "0.5"}},
                "v_kind": {
                    "given": "v_store",
                    "levelWeightsGiven": {
                        "2": {"a": "0.95", "b": "0.05"},
                        "7": {"a": "0.05", "b": "0.95"},
                    },
                },
            },
            2000,
        ),
    }
    plans = related_plans(contract, declarations)
    driver = plans["v"].relations.driver("v_store")
    assert isinstance(driver, ForeignKey) and driver.pick == "declared"
    data = generate_all(plans)
    rows = values(plans, data, "v", "v_store", "v_kind")
    assert {store for store, _ in rows} == {2, 7}
    agree = sum(1 for store, kind in rows if (store, kind) in ((2, "a"), (7, "b")))
    assert agree / len(rows) > 0.9
    root = root_for()
    for index, name in enumerate(("v_store", "v_kind")):
        assert list(generate_column(plans["v"], name, root=root)) == [
            row[index] for row in data["v"]
        ]


def test_related_date_columns_bounded_by_the_simulation_report_their_producer() -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("d", [_column("d_day", "DATE")], _primary("d", "d_day")),
            _expose(
                "e",
                [_column("e_day", "DATE", False)],
                {"relationships": [_rel("day", "d", ["e_day"], ["d_day"])]},
            ),
        ],
    }
    bounds = {"start": "2026-07-01T00:00:00Z", "end": "2026-09-30T23:59:59Z"}
    plans = related_plans(contract, {"d": ({}, 60), "e": ({}, 100)}, time_bounds=bounds)
    key = coverage_of_plan(plans["d"]).columns[0]
    foreign = coverage_of_plan(plans["e"]).columns[0]
    assert (key.origin.value, key.decided_by) == ("derived", "key")
    assert (foreign.origin.value, foreign.decided_by) == ("derived", "relationship")
    data = generate_all(plans)
    days = {row[0] for row in values(plans, data, "d", "d_day")}
    assert len(days) == 60
    assert {row[0] for row in values(plans, data, "e", "e_day")} <= days


# ---------------------------------------------------------------------------
# Key layouts
# ---------------------------------------------------------------------------


def test_an_ordered_key_is_laid_out_ascending_over_its_range(star: Any) -> None:
    plans, data = star
    dates = [row[0] for row in values(plans, data, "date_dim", "d_date_sk")]
    assert dates == list(range(2450000, 2451000))
    grid = plans["date_dim"].relations.driver("d_date_sk")
    assert isinstance(grid, KeyGrid)


def _grid(**declared: Any) -> KeyGrid:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [_expose("t", [_column("k", "INT")], _primary("t", "k"))],
    }
    rows = declared.pop("rows")
    plans = related_plans(contract, {"t": ({"k": declared} if declared else {}, rows)})
    grid = plans["t"].relations.driver("k")
    assert isinstance(grid, KeyGrid)
    return grid


def test_a_sparse_range_spreads_its_rows_evenly_without_repeats() -> None:
    grid = _grid(range={"min": "10", "max": "19"}, rows=4)
    assert [grid.value_at(row) for row in range(4)] == [10, 12, 15, 17]


def test_bins_take_their_rows_by_weight() -> None:
    grid = _grid(
        range={"min": "0", "max": "99"},
        bins=["0", "50", "99"],
        binWeights=["0.25", "0.75"],
        rows=20,
    )
    laid = [grid.value_at(row) for row in range(20)]
    assert sum(1 for value in laid if value < 50) == 5
    assert laid == sorted(set(laid))


def test_a_key_with_no_declaration_counts_from_one() -> None:
    grid = _grid(rows=5)
    assert [grid.value_at(row) for row in range(5)] == [1, 2, 3, 4, 5]


def test_a_value_moves_to_the_nearest_key_and_the_lower_one_on_a_tie() -> None:
    grid = _grid(range={"min": "10", "max": "19"}, rows=4)  # keys 10, 12, 15, 17
    assert [grid.value_at(grid.row_near(v)) for v in (9, 10, 11, 13, 14, 16, 18, 40)] == [
        10,
        10,
        10,
        12,
        15,
        15,
        17,
        17,
    ]


def test_a_short_identifier_key_never_repeats() -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [_expose("t", [_column("k", "STRING")], _primary("t", "k"))],
    }
    plans = related_plans(contract, {"t": ({"k": {"identifierLength": 2}}, 256)})
    driver = plans["t"].relations.driver("k")
    assert isinstance(driver, IdentifierKey)
    laid = [row[0] for row in generate_rows(plans["t"], root=root_for())]
    assert sorted(laid) == sorted(format(value, "02x") for value in range(256))
    with pytest.raises(FluxError, match="longer identifierLength"):
        related_plans(contract, {"t": ({"k": {"identifierLength": 2}}, 257)})


def test_a_uuid_key_is_a_version_8_uuid_that_never_repeats() -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [_expose("t", [_column("k", "uuid")], _primary("t", "k"))],
    }
    plans = related_plans(contract, {"t": ({}, 500)})
    laid = [row[0] for row in generate_rows(plans["t"], root=root_for())]
    assert len(set(laid)) == 500
    assert all(value[14] == "8" and value[19] in "89ab" and len(value) == 36 for value in laid)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _refused(match: str, **kwargs: Any) -> None:
    with pytest.raises(FluxError, match=match) as caught:
        related_plans(**kwargs)
    assert caught.value.code.code == "FLX-3403"


def _mutated(change: Any) -> Dict[str, Any]:
    contract = star_contract()
    change(contract)
    return contract


def _semantics(contract: Dict[str, Any], expose_id: str) -> Dict[str, Any]:
    for expose in contract["exposes"]:
        if expose["exposeId"] == expose_id:
            return expose["semantics"]
    raise KeyError(expose_id)


def test_a_target_the_simulation_does_not_generate_is_refused() -> None:
    _refused(
        "does not generate from statistics",
        only=("item", "customer", "store_sales", "store_returns"),
    )


def test_a_null_on_an_inner_relationship_is_refused() -> None:
    def inner(contract: Dict[str, Any]) -> None:
        _semantics(contract, "store_sales")["relationships"][0]["joinType"] = "inner"

    _refused("joinType inner", contract=_mutated(inner))


def test_a_null_on_a_key_column_is_refused() -> None:
    declarations = star_declarations()
    declarations["date_dim"][0]["d_date_sk"]["nullFraction"] = "0.1"
    with pytest.raises(FluxError, match="part of a key"):
        related_plans(
            contract=_mutated(
                lambda c: c["exposes"][0]["contract"]["schema"][0].update(required=False)
            ),
            declarations=declarations,
        )


def test_a_key_that_would_repeat_values_is_refused() -> None:
    declarations = star_declarations()
    declarations["date_dim"][0]["d_date_sk"] = {"levels": {"1": "0.5", "2": "0.5"}}
    _refused("would repeat values", declarations=declarations)


def test_a_key_range_narrower_than_its_rows_is_refused() -> None:
    declarations = star_declarations()
    declarations["item"][0]["i_item_sk"] = {"range": {"min": "1", "max": "199"}}
    _refused("holds 199 values but takes 200", declarations=declarations)


def test_a_one_to_one_child_larger_than_its_parent_is_refused() -> None:
    declarations = star_declarations()
    declarations["store_returns"] = (declarations["store_returns"][0], 3001)
    _refused("at most once", declarations=declarations)


def test_two_relationships_that_share_a_column_without_implying_each_other_are_refused() -> None:
    def other(contract: Dict[str, Any]) -> None:
        _semantics(contract, "store_returns")["relationships"][2] = _rel(
            "returned", "date_dim", ["sr_ticket_number"], ["d_date_sk"]
        )

    _refused("both set", contract=_mutated(other))


def test_a_foreign_key_of_another_type_than_its_target_is_refused() -> None:
    def retyped(contract: Dict[str, Any]) -> None:
        contract["exposes"][3]["contract"]["schema"][3]["type"] = "INT"

    _refused("one type", contract=_mutated(retyped))


def test_a_key_that_holds_part_of_a_relationship_is_refused() -> None:
    def partial(contract: Dict[str, Any]) -> None:
        _semantics(contract, "store_returns")["relationships"][0]["cardinality"] = "many_to_one"
        _semantics(contract, "store_sales")["relationships"].append(
            _rel(
                "pair",
                "store_returns",
                ["ss_ticket_number", "ss_customer_id"],
                ["sr_ticket_number", "sr_returned_date_sk"],
            )
        )

    _refused("reaches outside it", contract=_mutated(partial))


def test_keys_that_take_their_values_from_each_other_are_refused() -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose(
                "a",
                [_column("a_id", "INT"), _column("a_b", "INT")],
                {"relationships": [_rel("b", "b", ["a_b"], ["b_id"], "one_to_one")]},
            ),
            _expose(
                "b",
                [_column("b_id", "INT"), _column("b_a", "INT")],
                {"relationships": [_rel("a", "a", ["b_a"], ["a_id"], "one_to_one")]},
            ),
        ],
    }
    contract["exposes"][0]["semantics"]["relationships"].append(
        _rel("self", "b", ["a_id"], ["b_a"], "one_to_one")
    )
    _refused("round to the first", contract=contract, declarations={"a": ({}, 10), "b": ({}, 10)})


def test_a_date_key_with_no_range_and_no_time_bounds_is_refused() -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [_expose("t", [_column("k", "DATE")], _primary("t", "k"))],
    }
    _refused("declares no range", contract=contract, declarations={"t": ({}, 5)})


def test_a_parent_with_no_rows_is_refused_for_a_child_with_rows() -> None:
    declarations = star_declarations()
    declarations["customer"] = ({}, 0)
    _refused("declares no rows", declarations=declarations)


def test_a_column_drawn_given_a_picked_foreign_key_is_refused() -> None:
    def brand(contract: Dict[str, Any]) -> None:
        contract["exposes"][3]["contract"]["schema"].append(_column("ss_flag", "STRING"))

    declarations = star_declarations()
    sales = declarations["store_sales"][0]
    sales["ss_customer_id"] = {"support": {"values": ["x", "y"]}}
    sales["ss_flag"] = {
        "given": "ss_customer_id",
        "levelWeightsGiven": {"x": {"p": "0.5", "q": "0.5"}, "y": {"p": "0.2", "q": "0.8"}},
    }
    _refused("not drawn", contract=_mutated(brand), declarations=declarations)


def test_a_several_column_key_with_too_few_values_is_refused() -> None:
    declarations = star_declarations()
    declarations["store_sales"][0]["ss_ticket_number"] = {"range": {"min": "1", "max": "10"}}
    _refused("distinct values", declarations=declarations)


def test_a_key_column_nothing_produces_is_refused_before_any_row() -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose(
                "sales",
                [_column("ticket", "INT"), _column("item", "INT")],
                _primary("sales", "ticket"),
            ),
            _expose(
                "rets",
                [_column("r_ticket", "INT"), _column("r_item", "INT")],
                {
                    "relationships": [
                        _rel("sale", "sales", ["r_ticket", "r_item"], ["ticket", "item"])
                    ]
                },
            ),
        ],
    }
    _refused(
        "declares nothing", contract=contract, declarations={"sales": ({}, 100), "rets": ({}, 50)}
    )
    declared = {"sales": ({"item": {"range": {"min": "1", "max": "9"}}}, 100), "rets": ({}, 50)}
    plans = related_plans(contract, declared)
    data = generate_all(plans)
    sold = set(values(plans, data, "sales", "ticket", "item"))
    assert set(values(plans, data, "rets", "r_ticket", "r_item")) <= sold


def test_a_decimal_foreign_key_of_another_precision_is_refused() -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("p", [_column("k", "DECIMAL(10,2)")], _primary("p", "k")),
            _expose(
                "c",
                [_column("fk", "DECIMAL(5,2)")],
                {"relationships": [_rel("r", "p", ["fk"], ["k"])]},
            ),
        ],
    }
    declarations = {"p": ({"k": {"range": {"min": "100000", "max": "200000"}}}, 10), "c": ({}, 5)}
    _refused("one type", contract=contract, declarations=declarations)


def test_key_levels_that_spell_one_value_twice_are_refused() -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("p", [_column("a", "INT"), _column("b", "INT")], {}),
            _expose(
                "c",
                [_column("ca", "INT"), _column("cb", "INT")],
                {"relationships": [_rel("r", "p", ["ca", "cb"], ["a", "b"])]},
            ),
        ],
    }
    declarations = {
        "p": (
            {
                "a": {"levels": {"1": "0.5", "01": "0.5"}},
                "b": {"range": {"min": "1", "max": "3"}},
            },
            6,
        ),
        "c": ({}, 4),
    }
    _refused("spell one value more than once", contract=contract, declarations=declarations)


def _pair_contract(**rel: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("p", [_column("a", "INT"), _column("b", "INT")], {}),
            _expose(
                "c",
                [_column("ca", "INT", False), _column("cb", "INT", False)],
                {"relationships": [_rel("r", "p", ["ca", "cb"], ["a", "b"], **rel)]},
            ),
        ],
    }


_PAIR_PARENT = (
    {"a": {"range": {"min": "1", "max": "20"}}, "b": {"range": {"min": "1", "max": "20"}}},
    50,
)


def test_a_composite_foreign_key_is_null_on_all_its_columns_or_none() -> None:
    plans = related_plans(
        _pair_contract(), {"p": _PAIR_PARENT, "c": ({"ca": {"nullFraction": "0.5"}}, 400)}
    )
    data = generate_all(plans)
    pairs = values(plans, data, "c", "ca", "cb")
    assert {(a is None, b is None) for a, b in pairs} == {(True, True), (False, False)}
    parent = set(values(plans, data, "p", "a", "b"))
    assert all(pair in parent for pair in pairs if pair[0] is not None)
    _refused(
        "null on all its columns or none",
        contract=_pair_contract(),
        declarations={"p": _PAIR_PARENT, "c": ({"cb": {"nullFraction": "0.5"}}, 400)},
    )


def test_a_null_in_a_key_a_smaller_key_keeps_unique_is_refused() -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose(
                "p",
                [_column("x", "INT"), _column("y", "INT", False)],
                _primary("p", "x"),
            ),
            _expose(
                "c",
                [_column("a", "INT"), _column("b", "INT")],
                {"relationships": [_rel("r", "p", ["a", "b"], ["x", "y"])]},
            ),
        ],
    }
    declarations = {
        "p": ({"y": {"nullFraction": "0.3", "range": {"min": "1", "max": "5"}}}, 20),
        "c": ({}, 10),
    }
    _refused("part of a key", contract=contract, declarations=declarations)


def test_an_inner_relationship_never_copies_a_value_that_can_be_null() -> None:
    def nullable_holder(contract: Dict[str, Any]) -> None:
        relationships = _semantics(contract, "store_returns")["relationships"]
        relationships[0]["cardinality"] = "many_to_one"
        relationships[1]["joinType"] = "inner"
        _semantics(contract, "store_sales")["relationships"][1]["joinType"] = "inner"
        contract["exposes"][4]["contract"]["schema"][0]["required"] = False

    declarations = star_declarations()
    declarations["store_returns"][0]["sr_item_sk"] = {"nullFraction": "0.1"}
    _refused("may be null", contract=_mutated(nullable_holder), declarations=declarations)

    def inner_on_a_left_parent(contract: Dict[str, Any]) -> None:
        _semantics(contract, "store_returns")["relationships"][1]["joinType"] = "inner"

    _refused("both set", contract=_mutated(inner_on_a_left_parent))


def test_the_refusal_for_a_key_that_references_its_parent_says_why() -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("p", [_column("p_id", "INT")], _primary("p", "p_id")),
            _expose(
                "c",
                [_column("c_id", "INT")],
                {**_primary("c", "c_id"), "relationships": [_rel("r", "p", ["c_id"], ["p_id"])]},
            ),
        ],
    }
    _refused(
        "its columns are a key of this expose",
        contract=contract,
        declarations={"p": ({}, 50), "c": ({}, 60)},
    )


def test_the_refusal_code_is_registered_with_its_page_text() -> None:
    entry = get("FLX-3403")
    assert "related tables" in entry.title
    assert "joinType is inner" in entry.explanation
