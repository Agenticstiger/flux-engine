# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A reference expose: the rows a profile publishes whole (ADR 0023 section 2, KERNEL-SPEC section 8.2).

A catalogue (``product``) is planned from published rows and a table of lines points at it. What is
pinned, on kernel 0.2.0 (the only kernel that generates it):

* **The expose is its published rows**, in published order, on every seed, and its ``generatorDigest``
  moves with a published value and with nothing else.
* **Children pick published rows**: every foreign key is a published key and copies the row's other
  columns, uniformly or, for a ``declared`` pick, at the row whose key equals the drawn level.
* **Refusals** are ``FLX-3403``, before a row exists: a column the contract declares and the rows
  do not publish, a published column the contract does not declare, a cell that is not its column's
  type, a key that repeats or is NULL, a relationship that points at a row nothing publishes, and a
  declared level that is no key.
* **Two paths, one answer**: the column path's rows equal the per-row path's.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SeedTreeRoot, derive_root, digest_bytes
from flux_engine.generator import columns
from flux_engine.generator.reference import ReferenceRows, build_reference_plan
from flux_engine.generator.relations import relate_plans, related_columns
from flux_engine.generator.rows import (
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_row_slice,
    generate_rows,
)
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.keys import compile_keys, mark_key_columns

from .test_relations import _column, _expose, _h, _primary, _rel

pytestmark = pytest.mark.unit

SIM_ID = "sim-reference"
LINES = 300

PRODUCTS = ReferenceRows(
    columns=("sku", "pname", "price", "kind"),
    rows=(
        ("A-1", "Anvil", "12.5", "tool"),
        ("B-2", "Bolt", "0.25", None),
        ("C-3", "Crate", "30", "bulk"),
        ("D-4", "Dowel", "1.1", "tool"),
        ("E-5", "Eyelet", "0.05", None),
    ),
)


def contract(*, price: str = "DECIMAL(10,2)") -> Dict[str, Any]:
    product = _expose(
        "product",
        [
            _column("sku", "STRING"),
            _column("pname", "STRING"),
            _column("price", price),
            _column("kind", "STRING", False),
        ],
        _primary("product", "sku"),
    )
    line = _expose(
        "line",
        [
            _column("l_id", "STRING"),
            _column("l_sku", "STRING", False),
            _column("l_qty", "INT", False),
        ],
        {
            **_primary("line", "l_id"),
            "relationships": [_rel("product", "product", ["l_sku"], ["sku"])],
        },
    )
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.catalogue",
        "exposes": [product, line],
    }


def root_for(seed: int = 5) -> SeedTreeRoot:
    return derive_root(
        digest_bytes("sha256:" + "a" * 64), seed, seed_tree_version=kernel2.SEED_TREE_VERSION
    )


def plans_of(
    *,
    table: ReferenceRows = PRODUCTS,
    document: Optional[Dict[str, Any]] = None,
    line_columns: Optional[Dict[str, Any]] = None,
    relate: bool = True,
) -> Dict[str, GeneratorPlan]:
    document = contract() if document is None else document
    keys = compile_keys(document)
    assert keys is not None
    expose = {e["exposeId"]: e for e in document["exposes"]}
    product = mark_key_columns(compile_expose(expose["product"]), keys.expose("product"))
    line = mark_key_columns(compile_expose(expose["line"]), keys.expose("line"))
    plans = {
        "product": build_reference_plan(
            product,
            sim_id=SIM_ID,
            seam_digest=_h("product"),
            table=table,
            generator_version=GENERATOR_VERSION_2,
            where="reference expose 'product'",
        ),
        "line": build_plan(
            line,
            sim_id=SIM_ID,
            seam_digest=_h("line"),
            rows=LINES,
            fidelity={
                "columns": {"l_qty": {"range": {"min": "1", "max": "9"}}, **(line_columns or {})}
            },
            related=related_columns(keys.expose("line")),
            generator_version=GENERATOR_VERSION_2,
        ),
    }
    return relate_plans(plans, keys) if relate else plans


def rows_of(plan: GeneratorPlan, seed: int = 5) -> List[Dict[str, Any]]:
    names = [column_plan.column.name for column_plan in plan.columns]
    return [dict(zip(names, row, strict=True)) for row in generate_rows(plan, root=root_for(seed))]


def refused(change: Callable[[], Any]) -> str:
    with pytest.raises(FluxError) as caught:
        change()
    assert caught.value.code.code == "FLX-3403"
    return str(caught.value)


def test_the_expose_is_its_published_rows_in_published_order_on_every_seed() -> None:
    plans = plans_of()
    assert plans["product"].rows == 5
    first = rows_of(plans["product"], 1)
    assert first == rows_of(plans["product"], 2)
    assert [row["sku"] for row in first] == ["A-1", "B-2", "C-3", "D-4", "E-5"]
    assert [str(row["price"]) for row in first] == ["12.50", "0.25", "30.00", "1.10", "0.05"]
    assert [row["kind"] for row in first] == ["tool", None, "bulk", "tool", None]


def test_the_plan_names_the_layout_the_rows_and_a_digest_not_the_values() -> None:
    plans = plans_of()
    document = plans["product"].as_document()
    entry = document["relations"]["price"]
    assert entry["layout"] == "reference/1" and entry["rows"] == 5
    assert entry["digest"].startswith("sha256:")
    assert "12.5" not in str(document)
    moved = ReferenceRows(PRODUCTS.columns, (("A-1", "Anvil", "12.6", "tool"), *PRODUCTS.rows[1:]))
    assert plans_of(table=moved)["product"].digest() != plans["product"].digest()
    assert plans_of()["product"].digest() == plans["product"].digest()


def test_the_coverage_says_the_profile_decides_every_column_of_a_reference_expose() -> None:
    from flux_engine.generator.coverage import coverage_of_plan
    from flux_engine.seam.coverage import Origin

    for row in coverage_of_plan(plans_of()["product"]).columns:
        assert (row.origin, row.decided_by) == (Origin.PROFILE, "reference")
        assert row.source == f"references.product.{row.column}"
        assert row.detail.startswith("the value the pinned profile publishes")


def test_a_child_picks_only_published_rows_and_copies_their_keys() -> None:
    plans = plans_of()
    lines = rows_of(plans["line"])
    assert len(lines) == LINES
    skus = Counter(row["l_sku"] for row in lines)
    assert set(skus) <= {"A-1", "B-2", "C-3", "D-4", "E-5"} and len(skus) == 5
    # every row is as likely as every other: a uniform pick
    assert max(skus.values()) < 3 * min(skus.values())


def test_a_declared_pick_takes_the_row_whose_key_equals_the_drawn_level() -> None:
    plans = plans_of(line_columns={"l_sku": {"levels": {"A-1": "0.5", "C-3": "0.3", "E-5": "0.2"}}})
    skus = Counter(row["l_sku"] for row in rows_of(plans["line"]))
    assert set(skus) == {"A-1", "C-3", "E-5"}
    assert skus["A-1"] > skus["C-3"] > skus["E-5"]
    assert plans["line"].relations is not None
    driver = plans["line"].relations.driver("l_sku")
    assert driver is not None and driver.pick == "declared"  # type: ignore[union-attr]


def test_a_declared_level_that_is_no_key_is_refused_at_plan_time() -> None:
    message = refused(
        lambda: plans_of(line_columns={"l_sku": {"levels": {"A-1": "0.5", "Z-9": "0.5"}}})
    )
    assert "no key" in message


def test_a_declared_draw_that_is_not_levels_is_refused() -> None:
    refused(lambda: plans_of(line_columns={"l_sku": {"range": {"min": "1", "max": "9"}}}))


@pytest.mark.parametrize(
    "table",
    [
        ReferenceRows(("sku", "pname", "price"), (("A-1", "Anvil", "1"),)),
        ReferenceRows(
            ("sku", "pname", "price", "kind", "extra"), (("A-1", "Anvil", "1", None, "x"),)
        ),
        ReferenceRows(PRODUCTS.columns, (("A-1", "Anvil", "abc", None),)),
        ReferenceRows(
            PRODUCTS.columns, (("A-1", "Anvil", "1.5", None), ("A-1", "Bolt", "2", None))
        ),
        ReferenceRows(PRODUCTS.columns, (("A-1", "Anvil", "1.50", None),)),
        ReferenceRows(PRODUCTS.columns, (("A-1", "Anvil", "1.005", None),)),
        ReferenceRows(PRODUCTS.columns, ((None, "Anvil", "1", None),)),
        ReferenceRows(PRODUCTS.columns, (("A-1", None, "1", None),)),
    ],
    ids=[
        "a-contract-column-is-not-published",
        "a-published-column-is-not-a-contract-column",
        "a-cell-is-not-a-number",
        "a-key-repeats",
        "a-number-is-not-canonical",
        "a-number-is-finer-than-the-scale",
        "a-key-is-null",
        "a-required-cell-is-null",
    ],
)
def test_what_the_rows_cannot_hold_is_refused_before_a_row_exists(table: ReferenceRows) -> None:
    refused(lambda: plans_of(table=table))


def _priced(value: str) -> ReferenceRows:
    return ReferenceRows(PRODUCTS.columns, (("A-1", "Anvil", value, None),))


def test_a_legal_wide_decimal_cell_is_read_whole() -> None:
    wide = "1" + "0" * 29 + ".25"  # 31 digits, legal for DECIMAL(38,2)
    plans = plans_of(table=_priced(wide), document=contract(price="DECIMAL(38,2)"), relate=False)
    row = generate_rows(plans["product"], root=root_for())[0]
    assert str(row[2]) == wide


@pytest.mark.parametrize("declared", ["DECIMAL(2,2)", "DECIMAL(5,5)", "DECIMAL(5,2)"])
def test_a_zero_cell_is_read_in_a_decimal_whose_precision_equals_its_scale(declared: str) -> None:
    plans = plans_of(table=_priced("0"), document=contract(price=declared), relate=False)
    row = generate_rows(plans["product"], root=root_for())[0]
    assert row[2] == 0


def test_a_cell_with_more_digits_than_a_scale_only_decimal_holds_is_still_refused() -> None:
    message = refused(
        lambda: plans_of(table=_priced("1"), document=contract(price="DECIMAL(2,2)"), relate=False)
    )
    assert "'price'" in message


@pytest.mark.parametrize("value", ["1" + "0" * 40, "1E+1000000", "9" * 4000])
def test_a_decimal_cell_out_of_range_is_refused_not_crashed(value: str) -> None:
    message = refused(lambda: plans_of(table=_priced(value), relate=False))
    assert "'price'" in message


@pytest.mark.parametrize("seed", [1, 7])
@pytest.mark.parametrize("declared", [False, True])
def test_the_column_path_gives_the_per_row_path_rows(
    monkeypatch: pytest.MonkeyPatch, seed: int, declared: bool
) -> None:
    plans = plans_of(
        line_columns=(
            {"l_sku": {"levels": {"A-1": "0.5", "C-3": "0.3", "E-5": "0.2"}}} if declared else None
        )
    )
    taken: List[bool] = []
    column_rows = columns.column_rows

    def spy(*args: Any) -> Any:
        computed = column_rows(*args)
        taken.append(computed is not None)
        return computed

    monkeypatch.setattr(columns, "CHUNK_ROWS", 64)
    outcome: Dict[str, Tuple[Any, ...]] = {}
    for label, patched in (("columns", spy), ("rows", lambda *args: None)):
        with monkeypatch.context() as patch:
            patch.setattr(columns, "column_rows", patched)
            outcome[label] = (
                generate_rows(plans["product"], root=root_for(seed)),
                generate_rows(plans["line"], root=root_for(seed)),
                generate_row_slice(plans["line"], 17, 131, root=root_for(seed)),
            )
    assert outcome["columns"] == outcome["rows"]
    assert taken and all(taken)


def _chain(published: Tuple[Optional[str], ...]) -> Dict[str, GeneratorPlan]:
    """``category`` and ``product`` are both reference exposes; ``product`` names a category."""
    category = _expose(
        "category",
        [_column("cat_id", "STRING"), _column("cat_name", "STRING")],
        _primary("category", "cat_id"),
    )
    product = _expose(
        "product",
        [_column("sku", "STRING"), _column("sku_cat", "STRING", False)],
        {
            **_primary("product", "sku"),
            "relationships": [_rel("category", "category", ["sku_cat"], ["cat_id"])],
        },
    )
    document = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.chain",
        "exposes": [category, product],
    }
    keys = compile_keys(document)
    assert keys is not None
    plans: Dict[str, GeneratorPlan] = {}
    tables = {
        "category": ReferenceRows(("cat_id", "cat_name"), (("c1", "Tools"), ("c2", "Bulk"))),
        "product": ReferenceRows(
            ("sku", "sku_cat"), tuple((f"p{i}", cat) for i, cat in enumerate(published))
        ),
    }
    for expose in (category, product):
        expose_id = expose["exposeId"]
        plans[expose_id] = build_reference_plan(
            mark_key_columns(compile_expose(expose), keys.expose(expose_id)),
            sim_id=SIM_ID,
            seam_digest=_h(expose_id),
            table=tables[expose_id],
            generator_version=GENERATOR_VERSION_2,
            where=f"reference expose {expose_id!r}",
        )
    return relate_plans(plans, keys)


def test_a_reference_expose_may_point_at_another_whose_keys_it_uses() -> None:
    plans = _chain(("c1", "c2", None, "c1"))
    assert [row["sku_cat"] for row in rows_of(plans["product"])] == ["c1", "c2", None, "c1"]


def test_a_reference_expose_that_uses_a_key_nothing_publishes_is_refused() -> None:
    message = refused(lambda: _chain(("c1", "c9")))
    assert "does not publish" in message


def test_a_reference_expose_pointing_at_a_generated_one_is_refused() -> None:
    document = contract()
    keys = compile_keys(document)
    assert keys is not None
    plans = plans_of(relate=False)
    # The line expose (generated) names the product; swap the roles: product -> line.
    expose = {e["exposeId"]: e for e in document["exposes"]}
    expose["product"]["semantics"]["relationships"] = [_rel("line", "line", ["kind"], ["l_id"])]
    keys = compile_keys(document)
    assert keys is not None
    refused(lambda: relate_plans(plans, keys))
