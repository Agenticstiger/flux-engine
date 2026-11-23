# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""ADR 0022 section 2: every key pick records its parent row.

For each way a foreign key picks its parent (``declared``, ``uniform``, ``distinct``), with and
without a ``nullFraction``, under both kernels: the parent's key at ``parent_row`` is the
child's foreign key value on every row, and ``None`` is the answer exactly where the key is
NULL. The record is a cache, never a source: a reader that has seen nothing, a reader that
saw the rows through the per-row path and a reader the kernel 0.2.0 prefetch filled all agree.
"""

from __future__ import annotations

import pickle
from dataclasses import replace
from typing import Any, Dict, List, Tuple

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.core.digest import digest_of_document
from flux_engine.core.seedtree import SEED_TREE_VERSION
from flux_engine.generator import columns
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.relations import (
    CompositeMember,
    ForeignKey,
    RelationReader,
    Relations,
    owner_of,
)
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_2,
    GeneratorPlan,
    generate_row_slice,
    generate_rows,
)

from .test_dependencies import (
    CHAIN_STATISTICS,
    TICKET,
    chain_contract,
    single_plan,
    star_plans,
    star_with,
)
from .test_dependencies import root_for as dep_root
from .test_relations import (
    _column,
    _expose,
    _primary,
    _rel,
    related_plans,
    root_for,
)

pytestmark = pytest.mark.unit

KERNELS = pytest.mark.parametrize(
    ("version", "tree"),
    [(GENERATOR_VERSION, SEED_TREE_VERSION), (GENERATOR_VERSION_2, kernel2.SEED_TREE_VERSION)],
    ids=["kernel-0.1.0", "kernel-0.2.0"],
)

PARENT_ROWS = 50


def _pick_contract(pick: str) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("u", [_column("u_id", "INT")], _primary("u", "u_id")),
            _expose(
                "c",
                [_column("c_id", "INT"), _column("c_u", "INT", False)],
                {
                    **_primary("c", "c_id"),
                    "relationships": [
                        _rel(
                            "owner",
                            "u",
                            ["c_u"],
                            ["u_id"],
                            "one_to_one" if pick == "distinct" else "many_to_one",
                        )
                    ],
                },
            ),
        ],
    }


def _pick_declarations(pick: str, nulls: bool) -> Dict[str, Tuple[Dict[str, Any], int]]:
    child: Dict[str, Any] = {}
    if pick == "declared":
        child["range"] = {"min": "1", "max": str(PARENT_ROWS)}
    if nulls:
        child["nullFraction"] = "0.3"
    return {"u": ({}, PARENT_ROWS), "c": ({"c_u": child}, 40 if pick == "distinct" else 400)}


def _plans(pick: str, nulls: bool, version: str) -> Dict[str, GeneratorPlan]:
    plans = related_plans(
        _pick_contract(pick), _pick_declarations(pick, nulls), generator_version=version
    )
    driver = _relations(plans["c"]).driver("c_u")
    assert isinstance(driver, ForeignKey) and driver.pick == pick
    return plans


def _relations(plan: GeneratorPlan) -> Relations:
    assert plan.relations is not None
    relations: Relations = plan.relations
    return relations


def _column_of(plan: GeneratorPlan, rows: Any, name: str) -> List[Any]:
    index = [column_plan.column.name for column_plan in plan.columns].index(name)
    return [row[index] for row in rows]


def _assert_names_parent_keys(
    reader: RelationReader,
    plans: Dict[str, GeneratorPlan],
    data: Dict[str, Any],
    relationship: str,
    child: str,
    parent: str,
    from_column: str,
    to_column: str,
    *,
    with_values: bool,
) -> None:
    keys = _column_of(plans[parent], data[parent], to_column)
    for row, value in enumerate(data[child]):
        found = reader.parent_row(
            plans[child], relationship, row, values=value if with_values else None
        )
        held = _column_of(plans[child], [value], from_column)[0]
        if held is None:
            assert found is None, row
        else:
            assert found is not None and keys[found] == held, row


@KERNELS
@pytest.mark.parametrize("nulls", [False, True], ids=["no-null", "nullable"])
@pytest.mark.parametrize("pick", ["declared", "uniform", "distinct"])
@pytest.mark.parametrize("with_values", [False, True], ids=["record", "values"])
def test_the_parent_row_a_key_took_holds_the_key_value(
    pick: str, nulls: bool, version: str, tree: str, with_values: bool
) -> None:
    plans = _plans(pick, nulls, version)
    root = root_for(seed_tree_version=tree)
    data = {expose_id: generate_rows(plan, root=root) for expose_id, plan in plans.items()}
    fresh = RelationReader(root)
    _assert_names_parent_keys(
        fresh, plans, data, "owner", "c", "u", "c_u", "u_id", with_values=with_values
    )
    held = _column_of(plans["c"], data["c"], "c_u")
    assert (None in held) is nulls
    # A second reading answers from the record, with the same rows.
    again = [fresh.parent_row(plans["c"], "owner", row) for row in range(plans["c"].rows)]
    assert again == [
        RelationReader(root).parent_row(plans["c"], "owner", r) for r in range(len(again))
    ]
    # Reading through the cell path first records the same rows a fresh reader computes.
    walked = RelationReader(root)
    owner = owner_of(plans["c"])
    driver = _relations(plans["c"]).driver("c_u")
    assert driver is not None
    for row in range(plans["c"].rows):
        walked.cell(owner, plans["c"].column_plan("c_u"), driver, row)
    assert [walked.parent_row(plans["c"], "owner", r) for r in range(len(again))] == again


@pytest.mark.parametrize("nulls", [False, True], ids=["no-null", "nullable"])
@pytest.mark.parametrize("pick", ["declared", "uniform", "distinct"])
def test_the_prefetch_of_a_column_slice_records_what_a_fresh_reader_computes(
    pick: str, nulls: bool
) -> None:
    """Kernel 0.2.0: the column path fills ``_ready``; ``parent_row`` reads it, row for row."""
    plans = _plans(pick, nulls, GENERATOR_VERSION_2)
    plan = plans["c"]
    root = root_for(seed_tree_version=kernel2.SEED_TREE_VERSION)
    # Over a kernel 0.2.0 run root the slice goes through the column path.
    streams = CellStreams(root, plan.sim_id, plan.expose.expose_id)
    by_columns = columns.column_rows(plan, 0, plan.rows, streams)
    assert by_columns is not None
    rows = generate_row_slice(plan, 0, plan.rows, root=root)
    assert rows == by_columns
    reader = RelationReader(root)
    owner = owner_of(plan)
    driver = _relations(plan).driver("c_u")
    assert driver is not None
    reader.prefetch(
        owner,
        [plan.column_plan("c_u")],
        [driver],
        np.arange(plan.rows, dtype=np.int64),
    )
    assert reader._ready, "the prefetch fills the null and parent row of every row"
    fresh = RelationReader(root)
    keys = _column_of(plans["u"], generate_rows(plans["u"], root=root), "u_id")
    held = _column_of(plan, rows, "c_u")
    for row in range(plan.rows):
        found = reader.parent_row(plan, "owner", row)
        assert found == fresh.parent_row(plan, "owner", row), row
        assert (found is None) == (held[row] is None), row
        if found is not None:
            assert keys[found] == held[row], row


@KERNELS
def test_the_star_names_its_parent_rows_for_every_relationship(version: str, tree: str) -> None:
    plans = related_plans(generator_version=version)
    root = root_for(seed_tree_version=tree)
    data = {expose_id: generate_rows(plan, root=root) for expose_id, plan in plans.items()}
    reader = RelationReader(root)
    sales = (
        ("sold_date", "ss_sold_date_sk", "date_dim", "d_date_sk"),
        ("customer", "ss_customer_id", "customer", "c_id"),
    )
    for relationship, from_column, parent, to_column in sales:
        _assert_names_parent_keys(
            reader,
            plans,
            data,
            relationship,
            "store_sales",
            parent,
            from_column,
            to_column,
            with_values=True,
        )
    # The sale a return names is a distinct pick over two columns.
    _assert_names_parent_keys(
        reader,
        plans,
        data,
        "sale",
        "store_returns",
        "store_sales",
        "sr_item_sk",
        "ss_item_sk",
        with_values=False,
    )


def _given_plans(version: str) -> Dict[str, GeneratorPlan]:
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
            300,
        ),
    }
    return related_plans(contract, declarations, generator_version=version)


@KERNELS
def test_a_key_whose_level_another_column_reads_needs_the_rows_values(
    version: str, tree: str
) -> None:
    plans = _given_plans(version)
    root = root_for(seed_tree_version=tree)
    rows = generate_rows(plans["v"], root=root)
    reader = RelationReader(root)
    with pytest.raises(ValueError, match="needs the row's values"):
        reader.parent_row(plans["v"], "store", 0)
    for row, values in enumerate(rows):
        found = reader.parent_row(plans["v"], "store", row, values=values)
        assert found is not None and found + 1 == values[0], row


def test_a_relationship_with_no_key_of_its_own_names_the_missing_one() -> None:
    plans = related_plans(generator_version=GENERATOR_VERSION_2)
    reader = RelationReader(root_for(seed_tree_version=kernel2.SEED_TREE_VERSION))
    with pytest.raises(KeyError, match="nothing"):
        reader.parent_row(plans["store_sales"], "nothing", 0)
    # `item` of a return lies inside its `sale` key: it has no key of its own.
    with pytest.raises(KeyError, match="no foreign key or key part of its own"):
        reader.parent_row(plans["store_returns"], "item", 0)


def _bridge_contract() -> Dict[str, Any]:
    """``b`` is keyed by two foreign keys (a several-column key of two parts); ``c`` points at it."""
    return {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("x", [_column("x_id", "INT")], _primary("x", "x_id")),
            _expose("y", [_column("y_id", "INT")], _primary("y", "y_id")),
            _expose(
                "b",
                [_column("b_x", "INT"), _column("b_y", "INT")],
                {
                    "relationships": [
                        _rel("bx", "x", ["b_x"], ["x_id"]),
                        _rel("by", "y", ["b_y"], ["y_id"]),
                    ]
                },
            ),
            _expose(
                "c",
                [_column("c_x", "INT"), _column("c_y", "INT")],
                {"relationships": [_rel("link", "b", ["c_x", "c_y"], ["b_x", "b_y"])]},
            ),
        ],
    }


def _bridge_plans(version: str) -> Dict[str, GeneratorPlan]:
    return related_plans(
        _bridge_contract(),
        {"x": ({}, 12), "y": ({}, 9), "b": ({}, 80), "c": ({}, 200)},
        generator_version=version,
    )


@KERNELS
@pytest.mark.parametrize("with_values", [False, True], ids=["record", "values"])
def test_a_several_column_key_names_the_parent_rows_of_its_parts(
    version: str, tree: str, with_values: bool
) -> None:
    plans = _bridge_plans(version)
    assert isinstance(_relations(plans["b"]).driver("b_x"), CompositeMember)
    root = root_for(seed_tree_version=tree)
    data = {expose_id: generate_rows(plan, root=root) for expose_id, plan in plans.items()}
    reader = RelationReader(root)
    for relationship, column, parent, key in (
        ("bx", "b_x", "x", "x_id"),
        ("by", "b_y", "y", "y_id"),
    ):
        _assert_names_parent_keys(
            reader, plans, data, relationship, "b", parent, column, key, with_values=with_values
        )
    # The key is unique, so no two rows of b name the same pair of parent rows.
    pairs = {
        (reader.parent_row(plans["b"], "bx", row), reader.parent_row(plans["b"], "by", row))
        for row in range(plans["b"].rows)
    }
    assert len(pairs) == plans["b"].rows


@pytest.mark.parametrize("version_tree", [(GENERATOR_VERSION_2, kernel2.SEED_TREE_VERSION)])
def test_the_prefetch_of_a_several_column_key_gives_the_same_parent_rows(
    version_tree: Tuple[str, str],
) -> None:
    version, tree = version_tree
    plans = _bridge_plans(version)
    plan = plans["b"]
    root = root_for(seed_tree_version=tree)
    reader = RelationReader(root)
    drivers = [_relations(plan).driver(column_plan.column.name) for column_plan in plan.columns]
    reader.prefetch(owner_of(plan), plan.columns, drivers, np.arange(plan.rows, dtype=np.int64))
    assert reader._points
    fresh = RelationReader(root)
    keys = _column_of(plans["x"], generate_rows(plans["x"], root=root), "x_id")
    held = _column_of(plan, generate_row_slice(plan, 0, plan.rows, root=root), "b_x")
    for row in range(plan.rows):
        for relationship in ("bx", "by"):
            assert reader.parent_row(plan, relationship, row) == fresh.parent_row(
                plan, relationship, row
            )
        found = reader.parent_row(plan, "bx", row)
        assert found is not None and keys[found] == held[row]


@KERNELS
def test_a_key_pointing_at_a_several_column_key_names_a_row_of_it(version: str, tree: str) -> None:
    plans = _bridge_plans(version)
    root = root_for(seed_tree_version=tree)
    data = {expose_id: generate_rows(plan, root=root) for expose_id, plan in plans.items()}
    reader = RelationReader(root)
    pairs = list(
        zip(
            _column_of(plans["b"], data["b"], "b_x"),
            _column_of(plans["b"], data["b"], "b_y"),
            strict=True,
        )
    )
    for row, (cx, cy) in enumerate(
        zip(
            _column_of(plans["c"], data["c"], "c_x"),
            _column_of(plans["c"], data["c"], "c_y"),
            strict=True,
        )
    ):
        found = reader.parent_row(plans["c"], "link", row)
        assert found is not None and pairs[found] == (cx, cy), row


# ---------------------------------------------------------------------------
# final_row: a row as the expose's own pass writes it (ADR 0022 section 3)
# ---------------------------------------------------------------------------

DEP_KERNELS = pytest.mark.parametrize(
    "version", [SEED_TREE_VERSION, kernel2.SEED_TREE_VERSION], ids=["kernel-0.1.0", "kernel-0.2.0"]
)


def _chain_given_plan(version: str) -> GeneratorPlan:
    """A derive over its own row, a dependency, and a column drawn given another."""
    contract = chain_contract()
    schema = contract["exposes"][0]["contract"]["schema"]
    schema.append({"name": "cat", "type": "STRING", "required": False})
    schema.append({"name": "kind", "type": "STRING", "required": False})
    statistics = {
        **CHAIN_STATISTICS,
        "cat": {"levels": {"x": "0.5", "y": "0.5"}},
        "kind": {
            "given": "cat",
            "levelWeightsGiven": {"x": {"p": "0.9", "q": "0.1"}, "y": {"p": "0.1", "q": "0.9"}},
        },
    }
    return single_plan(contract, statistics, rows=300, version=version)


@DEP_KERNELS
def test_final_row_is_the_row_the_expose_writes_with_rules_a_dependency_and_a_given(
    version: str,
) -> None:
    plan = _chain_given_plan(version)
    assert plan.rules is not None and plan.rules.order
    root = dep_root(version)
    expected = generate_rows(plan, root=root)
    reader = RelationReader(root)
    assert [reader.final_row(plan, row) for row in range(plan.rows)] == list(expected)
    # A row kept is the row asked for, in any order.
    assert reader.final_row(plan, 41) == expected[41]
    assert RelationReader(root).final_row(plan, 7) == expected[7]


@DEP_KERNELS
def test_final_row_is_the_row_of_a_related_expose_with_a_dependency(version: str) -> None:
    plans = star_plans(star_with(TICKET), version=version)
    root = dep_root(version)
    reader = RelationReader(root)
    for expose_id in ("store_sales", "store_returns", "item"):
        plan = plans[expose_id]
        expected = generate_rows(plan, root=root)
        found = [reader.final_row(plan, row) for row in range(0, plan.rows, 7)]
        assert found == [expected[row] for row in range(0, plan.rows, 7)], expose_id


@DEP_KERNELS
def test_final_row_of_a_foreign_key_another_column_reads(version: str) -> None:
    tree = SEED_TREE_VERSION if version == SEED_TREE_VERSION else kernel2.SEED_TREE_VERSION
    plans = _given_plans(GENERATOR_VERSION if version == SEED_TREE_VERSION else GENERATOR_VERSION_2)
    root = root_for(seed_tree_version=tree)
    expected = generate_rows(plans["v"], root=root)
    reader = RelationReader(root)
    assert [reader.final_row(plans["v"], row) for row in range(plans["v"].rows)] == list(expected)


def test_a_plan_that_reads_nothing_keeps_its_document_and_digest() -> None:
    for plans in (related_plans(), _bridge_plans(GENERATOR_VERSION)):
        for plan in plans.values():
            assert plan.reads == ()
            document = plan.as_document()
            assert "reads" not in document
            assert plan.digest() == digest_of_document(document, subject="generatorDigest")


def test_a_plan_that_reads_another_carries_its_digest_in_its_document() -> None:
    plans = related_plans()
    parent, child = plans["item"], plans["store_sales"]
    reading = replace(child, reads=(("item", parent),))
    document = reading.as_document()
    assert document["reads"] == {"item": parent.digest().value}
    assert {key: value for key, value in document.items() if key != "reads"} == (
        child.as_document()
    )
    assert reading.digest() != child.digest()
    # A moved parent moves its reader.
    moved = replace(parent, rows=parent.rows + 1)
    assert moved.digest() != parent.digest()
    assert replace(child, reads=(("item", moved),)).digest() != reading.digest()
    # Several reads are listed by exposeId, in any order given.
    both = replace(child, reads=(("item", parent), ("date_dim", plans["date_dim"])))
    assert list(both.as_document()["reads"]) == ["date_dim", "item"]


def test_a_plan_with_reads_pickles_and_keeps_its_document() -> None:
    plans = related_plans(generator_version=GENERATOR_VERSION_2)
    reading = replace(plans["store_sales"], reads=(("item", plans["item"]),))
    again = pickle.loads(pickle.dumps(reading))
    assert again.as_document() == reading.as_document()
    assert again.digest() == reading.digest()
    assert again.reads[0][0] == "item"
