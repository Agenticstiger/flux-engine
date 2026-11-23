# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A relationship's ``sharedColumns`` is generated as the copy it states (ADR 0022 section 4).

On kernel 0.2.0 each listed column is the rule ``derive: <column>``, ``expr: <relationship>.<column>``
with no ``when`` (EXPR-SPEC-2 section 9), so a row copies the value the row its link names ends with
and a row whose key is NULL takes NULL. What is pinned:

* **The copy equals its source** on every row whose key is not NULL, for a one-column foreign key, a
  several-column one, and a relationship absorbed into a several-column key, and is NULL elsewhere.
* **What stays listed.** A listed key or foreign key column is not generated and is listed with its
  reason; kernel 0.1.0 changes nothing, neither the listing nor the plan.
* **One source per value.** A copied column that another source also sets is ``FLX-6203``.
* **Two paths, one answer.** The column path's cells under the copies equal the per-row path's rows.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from flux_engine.core import kernel2
from flux_engine.core.seedtree import SeedTreeRoot, derive_root, digest_bytes
from flux_engine.expr2.errors import Expr2Error
from flux_engine.generator import columns
from flux_engine.generator.coverage import coverage_of_plan
from flux_engine.generator.relations import relate_plans, related_columns
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_row_slice,
    generate_rows,
)
from flux_engine.generator.rules import declared_not_generated, plan_rules
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.coverage import Origin
from flux_engine.seam.declared import KIND_SHARED_COLUMNS, REASON_KEY_COLUMN, REASON_SELF_LINK
from flux_engine.seam.keys import compile_keys, mark_key_columns

from .test_relations import _column, _expose, _h, _primary, _rel
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit

SIM_ID = "sim-shared"
X, Y, B, C, D = 14, 9, 90, 240, 300


def shared_contract(
    *,
    d_shared: Sequence[str] = ("region",),
    b_shared: Sequence[str] = ("region",),
    c_shared: Sequence[str] = ("region", "tier"),
) -> Dict[str, Any]:
    """``d`` points at ``x`` (one column), ``b`` is keyed by ``x`` and ``y``, ``c`` points at ``b``.

    ``d`` copies ``region`` from ``x``; ``b`` copies it through ``bx``, a relationship that lies inside
    the several-column key of ``b``; ``c`` copies ``region`` and ``tier`` through ``link``, a
    several-column foreign key.
    """
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.shared",
        "exposes": [
            _expose(
                "x",
                [
                    _column("x_id", "INT"),
                    _column("region", "STRING", False),
                    _column("tier", "STRING", False),
                ],
                _primary("x", "x_id"),
            ),
            _expose("y", [_column("y_id", "INT")], _primary("y", "y_id")),
            _expose(
                "b",
                [
                    _column("b_x", "INT"),
                    _column("b_y", "INT"),
                    _column("region", "STRING", False),
                    _column("tier", "STRING", False),
                ],
                {
                    "relationships": [
                        _rel("bx", "x", ["b_x"], ["x_id"], sharedColumns=list(b_shared)),
                        _rel("by", "y", ["b_y"], ["y_id"]),
                    ]
                },
            ),
            _expose(
                "c",
                [
                    _column("c_x", "INT", False),
                    _column("c_y", "INT", False),
                    _column("region", "STRING", False),
                    _column("tier", "STRING", False),
                ],
                {
                    "relationships": [
                        _rel(
                            "link",
                            "b",
                            ["c_x", "c_y"],
                            ["b_x", "b_y"],
                            sharedColumns=list(c_shared),
                        )
                    ]
                },
            ),
            _expose(
                "d",
                [
                    _column("d_id", "INT"),
                    _column("d_x", "INT", False),
                    _column("region", "STRING", False),
                ],
                {
                    **_primary("d", "d_id"),
                    "relationships": [
                        _rel("owner", "x", ["d_x"], ["x_id"], sharedColumns=list(d_shared))
                    ],
                },
            ),
        ],
    }


def statistics() -> Dict[str, Tuple[Dict[str, Any], int]]:
    own = {"levels": {"north": "0.5", "south": "0.3", "west": "0.2"}}
    other = {"levels": {"a": "0.5", "b": "0.5"}}
    return {
        "x": ({"region": own, "tier": {"levels": {"gold": "0.4", "silver": "0.6"}}}, X),
        "y": ({}, Y),
        "b": ({"region": other, "tier": other}, B),
        "c": ({"c_x": {"nullFraction": "0.15"}, "region": other, "tier": other}, C),
        "d": ({"d_x": {"nullFraction": "0.1"}, "region": own}, D),
    }


def plans_of(
    contract: Mapping[str, Any],
    *,
    counter_based: bool = True,
    declared: Optional[Dict[str, Tuple[Dict[str, Any], int]]] = None,
    mappings: Optional[Mapping[str, Mapping[str, Any]]] = None,
    flux_rules: Optional[Mapping[str, Any]] = None,
    relate: bool = True,
    only: Optional[Sequence[str]] = None,
    has_fidelity: bool = True,
    links: Optional[Mapping[str, Sequence[Any]]] = None,
    bridges: Optional[Mapping[str, Sequence[Tuple[str, str]]]] = None,
    copies: bool = True,
) -> Dict[str, GeneratorPlan]:
    declared = statistics() if declared is None else declared
    keys = compile_keys(contract)
    assert keys is not None
    version = GENERATOR_VERSION_2 if counter_based else GENERATOR_VERSION
    plans: Dict[str, GeneratorPlan] = {}
    for expose in contract["exposes"]:
        expose_id = expose["exposeId"]
        if only is not None and expose_id not in only:
            continue
        expose_keys = keys.expose(expose_id)
        columns_, count = declared[expose_id]
        related = related_columns(expose_keys)
        rule_plan = plan_rules(
            contract_document=contract,
            expose_id=expose_id,
            settings=None,
            related=related,
            mapping=(mappings or {}).get(expose_id),
            has_fidelity=has_fidelity,
            strict=False,
            flux_rules=(flux_rules or {}).get(expose_id),
            keys=expose_keys,
            rows=count,
            null_fractions={
                name: block["nullFraction"]
                for name, block in columns_.items()
                if "nullFraction" in block
            },
            counter_based=counter_based,
            links=(links or {}).get(expose_id, ()),
            copies=copies,
        )
        plans[expose_id] = build_plan(
            mark_key_columns(compile_expose(expose), expose_keys),
            sim_id=SIM_ID,
            seam_digest=_h(expose_id),
            rows=count,
            fidelity={"columns": columns_},
            related=related,
            rules=rule_plan,
            generator_version=version,
        )
    return relate_plans(plans, keys, bridges) if relate else plans


def root_for(seed: int = 5) -> SeedTreeRoot:
    return derive_root(
        digest_bytes("sha256:" + "a" * 64), seed, seed_tree_version=kernel2.SEED_TREE_VERSION
    )


def table(plan: GeneratorPlan, rows: Sequence[Tuple[Any, ...]]) -> List[Dict[str, Any]]:
    names = [column_plan.column.name for column_plan in plan.columns]
    return [dict(zip(names, row, strict=True)) for row in rows]


@pytest.fixture(scope="module")
def world() -> Dict[str, List[Dict[str, Any]]]:
    plans = plans_of(shared_contract())
    root = root_for()
    return {
        expose_id: table(plan, generate_rows(plan, root=root)) for expose_id, plan in plans.items()
    }


# ---------------------------------------------------------------------------
# The copy equals its source
# ---------------------------------------------------------------------------


def _by(rows: Sequence[Mapping[str, Any]], *names: str) -> Dict[Tuple[Any, ...], Mapping[str, Any]]:
    return {tuple(row[name] for name in names): row for row in rows}


@pytest.mark.parametrize(
    ("child", "keys", "parent", "parent_keys", "columns_"),
    [
        pytest.param("d", ("d_x",), "x", ("x_id",), ("region",), id="one-column-foreign-key"),
        pytest.param(
            "c", ("c_x", "c_y"), "b", ("b_x", "b_y"), ("region", "tier"), id="several-column-key"
        ),
        pytest.param("b", ("b_x",), "x", ("x_id",), ("region",), id="inside-a-several-column-key"),
    ],
)
def test_every_copy_equals_its_source_and_is_null_where_the_key_is(
    world: Mapping[str, List[Dict[str, Any]]],
    child: str,
    keys: Tuple[str, ...],
    parent: str,
    parent_keys: Tuple[str, ...],
    columns_: Tuple[str, ...],
) -> None:
    parents = _by(world[parent], *parent_keys)
    present = absent = 0
    for row in world[child]:
        key = tuple(row[name] for name in keys)
        if None in key:
            assert all(row[name] is None for name in columns_), row
            absent += 1
            continue
        for name in columns_:
            assert row[name] == parents[key][name], (child, row)
            assert row[name] is not None
        present += 1
    assert present > 50
    assert absent > 0 or child == "b"
    for name in columns_:
        assert len({row[name] for row in world[child]}) > 1


def test_a_copy_is_the_parents_final_value_not_a_draw_of_its_own() -> None:
    """The child declares a draw for the column and it is replaced: no row differs from its parent."""
    world_ = plans_of(shared_contract())
    root = root_for(11)
    rows = {e: table(p, generate_rows(p, root=root)) for e, p in world_.items()}
    parents = _by(rows["x"], "x_id")
    chance = sum(
        1 for row in rows["d"] if row["d_x"] is not None and row["region"] == "north"
    ) / sum(1 for row in rows["d"] if row["d_x"] is not None)
    assert 0 < chance < 1
    assert all(row["region"] == parents[(row["d_x"],)]["region"] for row in rows["d"] if row["d_x"])


# ---------------------------------------------------------------------------
# The plan: the rule, its order, its coverage and what stays listed
# ---------------------------------------------------------------------------


def test_each_listed_column_is_a_copy_rule_named_by_its_relationship_and_column() -> None:
    plans = plans_of(shared_contract())
    (rule,) = plans["d"].rules.rules
    assert (rule.id, rule.form, rule.text, rule.column) == (
        "owner.region",
        "derive",
        "owner.region",
        "region",
    )
    assert (rule.origin, rule.when, rule.paths, rule.whole_path) == (
        "shared",
        None,
        (("owner",),),
        True,
    )
    assert [r.id for r in plans["c"].rules.rules] == ["link.region", "link.tier"]
    document = plans["d"].rules.as_document()["rules"][0]
    assert document["origin"] == "shared"
    assert "language" not in document
    # A column the copy sets on every row needs no draw of its own.
    assert plans["d"].rules.covers_every_row == frozenset({"region"})
    # The copying plan reads the plan of the expose it copies from.
    assert [name for name, _ in plans["d"].reads] == ["x"]


def test_the_coverage_names_a_copy_by_its_relationship_and_column() -> None:
    plans = plans_of(shared_contract())
    by_column = {row.column: row for row in coverage_of_plan(plans["c"]).columns}
    for name in ("region", "tier"):
        row = by_column[name]
        assert (row.origin, row.decided_by) == (Origin.COPIED, "rule")
        assert row.source == f"semantics.relationships.link.sharedColumns.{name}"


def test_a_bundle_that_lists_no_shared_column_keeps_its_plan_document_and_digest() -> None:
    plain = plans_of(shared_contract(d_shared=(), b_shared=(), c_shared=()))
    assert all(plan.rules is None for plan in plain.values())
    assert all(not plan.reads for plan in plain.values())


def test_a_copied_column_that_declares_a_null_fraction_is_refused_naming_the_rule() -> None:
    """EXPR-SPEC-2 section 6: the column's own NULLs come first, and a copy allows no violating row."""
    declared = statistics()
    columns_, count = declared["d"]
    declared["d"] = ({**columns_, "region": {**columns_["region"], "nullFraction": "0.2"}}, count)
    with pytest.raises(Exception) as caught:
        plans_of(shared_contract(), declared=declared)
    assert caught.value.code.code == "FLX-6204", str(caught.value)  # type: ignore[attr-defined]
    message = str(caught.value)
    assert "emit 'd'" in message and "'region'" in message and "'owner.region'" in message
    assert "semantics.relationships.owner.sharedColumns.region" in message
    # Without the declaration, or with kernel 0.1.0 (which makes no copy), the plan is made.
    plans_of(shared_contract())
    plans_of(shared_contract(), declared=declared, counter_based=False)


def _declared(
    contract: Mapping[str, Any],
    expose: str,
    *,
    counter_based: bool,
    has_fidelity: bool = True,
    copies: bool = True,
) -> Tuple[Any, ...]:
    keys = compile_keys(contract)
    assert keys is not None
    return declared_not_generated(
        contract_document=contract,
        expose_id=expose,
        settings=None,
        counter_based=counter_based,
        has_fidelity=has_fidelity,
        related=related_columns(keys.expose(expose)),
        copies=copies,
    )


def test_kernel_0_2_0_lists_only_a_key_or_foreign_key_column_with_its_reason() -> None:
    contract = shared_contract(d_shared=("region", "d_x", "d_id"))
    (row,) = _declared(contract, "d", counter_based=True)
    assert (row.kind, row.locator) == (
        KIND_SHARED_COLUMNS,
        "semantics.relationships.owner.sharedColumns",
    )
    assert row.names == ("d_x", "d_id") and row.reason == REASON_KEY_COLUMN
    assert row.as_document()["reason"] == REASON_KEY_COLUMN
    assert REASON_KEY_COLUMN in row.line()
    # Only the other column generates.
    (rule,) = plans_of(contract)["d"].rules.rules
    assert rule.column == "region"
    # Nothing is left when every column is copied.
    assert _declared(shared_contract(), "d", counter_based=True) == ()


def test_kernel_0_1_0_and_an_emit_without_statistics_keep_every_declaration_listed() -> None:
    contract = shared_contract(d_shared=("region", "d_x"))
    for kwargs in ({"counter_based": False}, {"counter_based": True, "has_fidelity": False}):
        (row,) = _declared(contract, "d", **kwargs)
        assert row.names == ("region", "d_x") and row.reason == ""
        assert "reason" not in row.as_document()


def test_a_listed_column_the_parent_does_not_hold_stays_listed_without_a_reason() -> None:
    (row,) = _declared(shared_contract(d_shared=("region", "nothing")), "d", counter_based=True)
    assert row.names == ("nothing",) and row.reason == ""
    plans = plans_of(shared_contract(d_shared=("region", "nothing")))
    assert [r.column for r in plans["d"].rules.rules] == ["region"]


def _self_linked_contract() -> Dict[str, Any]:
    """``e`` points at itself through ``manager`` and lists ``region`` among its shared columns."""
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.shared.self",
        "exposes": [
            _expose(
                "e",
                [
                    _column("e_id", "INT"),
                    _column("e_manager", "INT", False),
                    _column("region", "STRING", False),
                ],
                {
                    **_primary("e", "e_id"),
                    "relationships": [
                        _rel("manager", "e", ["e_manager"], ["e_id"], sharedColumns=["region"])
                    ],
                },
            )
        ],
    }


def test_a_copy_through_a_relationship_to_its_own_expose_stays_listed_and_the_bundle_plans() -> (
    None
):
    """It would read a row of its own expose: kept as declared, not refused, as before copies."""
    contract = _self_linked_contract()
    (row,) = _declared(contract, "e", counter_based=True)
    assert row.names == ("region",) and row.reason == REASON_SELF_LINK
    declared = {
        "e": (
            {
                "e_manager": {"nullFraction": "0.2"},
                "region": {"levels": {"north": "0.5", "south": "0.5"}},
            },
            40,
        )
    }
    plans = plans_of(contract, declared=declared)
    assert plans["e"].rules is None
    rows = table(plans["e"], generate_rows(plans["e"], root=root_for()))
    assert len(rows) == 40


def test_kernel_0_1_0_plans_no_copy_and_gives_the_rows_it_gave() -> None:
    plain = plans_of(shared_contract(d_shared=(), b_shared=(), c_shared=()), counter_based=False)
    shared = plans_of(shared_contract(), counter_based=False)
    assert all(plan.rules is None for plan in shared.values())
    for expose_id, plan in shared.items():
        assert plan.digest() == plain[expose_id].digest()
    root = derive_root(digest_bytes("sha256:" + "a" * 64), 5)
    assert generate_rows(shared["c"], root=root) == generate_rows(plain["c"], root=root)


def test_an_emit_whose_listed_columns_are_all_keys_has_no_rule_plan() -> None:
    plans = plans_of(shared_contract(d_shared=("d_x", "d_id")))
    assert plans["d"].rules is None and not plans["d"].reads


def test_an_emit_with_no_fidelity_block_plans_no_copy() -> None:
    plans = plans_of(shared_contract(), has_fidelity=False, relate=False)
    assert all(plan.rules is None for plan in plans.values())


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _refused(contract: Mapping[str, Any], code: str, **kwargs: Any) -> str:
    with pytest.raises((Expr2Error, Exception)) as caught:
        plans_of(contract, **kwargs)
    assert caught.value.code.code == code, str(caught.value)  # type: ignore[attr-defined]
    return str(caught.value)


@pytest.mark.parametrize(
    "setter",
    [
        pytest.param(
            lambda c: c["exposes"][4]["contract"].__setitem__(
                "invariants", [{"id": "own", "derive": "region", "expr": "'north'"}]
            ),
            id="a-contract-derive",
        ),
        pytest.param(
            lambda c: c["exposes"][4]["contract"].__setitem__(
                "invariants",
                [{"id": "dep", "dependency": {"from": ["d_id"], "to": ["region"]}}],
            ),
            id="a-dependency",
        ),
    ],
)
def test_a_copied_column_another_rule_also_sets_is_two_sources(setter: Any) -> None:
    contract = shared_contract()
    setter(contract)
    message = _refused(contract, "FLX-6203")
    assert "region" in message and "owner.region" in message


def test_a_copied_column_a_mapping_also_sets_is_two_sources() -> None:
    message = _refused(shared_contract(), "FLX-6203", mappings={"d": {"region": "something"}})
    assert "region" in message and "owner.region" in message


def test_a_column_two_relationships_copy_is_two_sources() -> None:
    contract = shared_contract()
    contract["exposes"][1]["contract"]["schema"].append(_column("region", "STRING", False))
    contract["exposes"][2]["semantics"]["relationships"][1]["sharedColumns"] = ["region"]
    declared = statistics()
    declared["y"] = ({"region": {"levels": {"a": "0.5", "b": "0.5"}}}, Y)
    message = _refused(contract, "FLX-6203", declared=declared)
    assert "bx.region" in message and "by.region" in message


def test_a_copy_from_an_expose_the_simulation_does_not_generate_is_refused() -> None:
    message = _refused(shared_contract(), "FLX-6210", only=("d",))
    assert "x" in message and "fidelity" in message


# ---------------------------------------------------------------------------
# The column path and the per-row path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chunk", [5, 64])
def test_the_column_paths_cells_under_the_copies_are_the_per_row_paths_rows(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    plans = plans_of(shared_contract())
    root = root_for()
    for expose_id, plan in plans.items():
        by_columns, by_rows, taken = _by_both_paths(
            monkeypatch, lambda plan=plan: generate_rows(plan, root=root)
        )
        assert by_columns == by_rows, expose_id
        assert taken and all(taken), expose_id


@pytest.mark.parametrize(("start", "stop"), [(0, 1), (3, 40), (117, 118), (230, 240)])
def test_a_slice_of_a_copying_plan_is_the_whole_tables_slice(start: int, stop: int) -> None:
    plan = plans_of(shared_contract())["c"]
    root = root_for()
    assert (
        generate_row_slice(plan, start, stop, root=root)
        == generate_rows(plan, root=root)[start:stop]
    )
