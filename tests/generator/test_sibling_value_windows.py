# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``FIRST_VALUE``, ``LAST_VALUE``, ``LAG`` and ``LEAD`` over a row's siblings (EXPR-SPEC-2 section 5).

The four value functions generate as the ranks do (ADR 0022 section 7): the expose is made whole, and
each step is applied over every row. What is pinned, against DuckDB over the generated rows with
``NULLS LAST`` and the whole-partition frame of ``FIRST_VALUE`` and ``LAST_VALUE`` written out:

* every row's value, for offsets 1 and 2, with and without a default, over partitions with NULL
  values and a NULL link, ``DESC`` and ``NULLS FIRST``;
* an argument that reads through a link of the sibling, with a total order the checker requires;
* the column path's cells and the per-row path's agree, and a slice is the whole's slice.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pytest

from flux_engine.generator import rows as generator_rows
from flux_engine.generator.rows import generate_row_slice, generate_rows

from .test_rules_over_links import chain_contract, refused, root_for
from .test_sibling_windows import (
    PARTITION,
    _by_both_paths,
    _columns,
    _rule,
    declared,
    rows_of,
    windowed_plans,
)

duckdb = pytest.importorskip("duckdb")

pytestmark = pytest.mark.unit

FRAME = "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"

#: (column, FLUX text of the call, DuckDB text of the same call over ``t``); ``{o}`` is the order.
CALLS: List[Tuple[str, str, str]] = [
    ("i_first", "FIRST_VALUE(i_duration)", "FIRST_VALUE(i_duration)"),
    ("i_last", "LAST_VALUE(i_duration)", "LAST_VALUE(i_duration)"),
    ("i_lag1", "LAG(i_duration, 1, 0)", "LAG(i_duration, 1, 0)"),
    ("i_lag2", "LAG(i_duration, 2)", "LAG(i_duration, 2)"),
    ("i_lead1", "LEAD(i_duration, 1, -1)", "LEAD(i_duration, 1, -1)"),
    ("i_lead2", "LEAD(i_duration, 2)", "LEAD(i_duration, 2)"),
    ("i_alink", "FIRST_VALUE(account.a_score)", "FIRST_VALUE(a_score)"),
    ("i_blink", "LAG(account.a_score, 1, -1)", "LAG(a_score, 1, -1)"),
]
WHOLE = {"FIRST_VALUE", "LAST_VALUE"}

ORDERS = [
    pytest.param("i_duration, i_id", "i_duration ASC NULLS LAST, i_id ASC NULLS LAST", id="asc"),
    pytest.param(
        "i_duration DESC, i_id", "i_duration DESC NULLS LAST, i_id ASC NULLS LAST", id="desc"
    ),
    pytest.param(
        "i_duration DESC NULLS FIRST, i_id DESC",
        "i_duration DESC NULLS FIRST, i_id DESC NULLS LAST",
        id="desc-nulls-first",
    ),
]


def value_rules(order: str) -> List[Dict[str, Any]]:
    return [
        _rule(f"v_{column}", column, f"{flux} OVER ({PARTITION} ORDER BY {order})")
        for column, flux, _ in CALLS
    ]


def oracle(
    rows: Sequence[Mapping[str, Any]], accounts: Sequence[Mapping[str, Any]], order: str
) -> Dict[str, Dict[str, Any]]:
    """DuckDB's value of every call, by ``i_id`` then column."""
    connection = duckdb.connect()
    connection.execute("CREATE TABLE acct (a_id VARCHAR, a_score INTEGER)")
    connection.executemany(
        "INSERT INTO acct VALUES (?, ?)", [(a["a_id"], a["a_score"]) for a in accounts]
    )
    connection.execute("CREATE TABLE t0 (i_id VARCHAR, i_account VARCHAR, i_duration INTEGER)")
    connection.executemany(
        "INSERT INTO t0 VALUES (?, ?, ?)",
        [(r["i_id"], r["i_account"], r["i_duration"]) for r in rows],
    )
    connection.execute(
        "CREATE TABLE t AS SELECT t0.*, acct.a_score FROM t0 LEFT JOIN acct ON t0.i_account = acct.a_id"
    )
    over = f"PARTITION BY i_account ORDER BY {order}"
    selects = []
    for _, _, sql in CALLS:
        frame = f" {FRAME}" if sql.split("(")[0] in WHOLE else ""
        selects.append(f"{sql} OVER ({over}{frame})")
    found = connection.execute(f"SELECT i_id, {', '.join(selects)} FROM t").fetchall()
    connection.close()
    return {row[0]: {column: row[1 + k] for k, (column, _, _) in enumerate(CALLS)} for row in found}


@pytest.mark.parametrize(("flux_order", "duckdb_order"), ORDERS)
def test_every_value_function_equals_duckdbs_over_the_generated_rows(
    flux_order: str, duckdb_order: str
) -> None:
    plans = windowed_plans(value_rules(flux_order))
    rows = rows_of(plans)
    accounts = rows_of(plans, "account")
    expected = oracle(rows, accounts, duckdb_order)
    assert len(expected) == len(rows)
    for row in rows:
        for column, _, _ in CALLS:
            assert row[column] == expected[row["i_id"]][column], (column, row["i_id"])
    # Partitions with NULL values, a NULL link, and values that are neither NULL nor the default.
    assert any(row["i_duration"] is None for row in rows)
    assert any(row["i_account"] is None for row in rows)
    for column, _, _ in CALLS:
        assert len({row[column] for row in rows}) > 3, column
    assert any(row["i_lag1"] == 0 for row in rows) and any(row["i_lead1"] == -1 for row in rows)
    assert any(row["i_lag2"] is None for row in rows)


def test_a_value_function_reads_the_siblings_final_value_of_a_column_an_earlier_rule_set() -> None:
    # i_dur2 is set by a contract rule; the window reads every sibling's final i_dur2.
    rule = {"id": "dur2", "derive": "i_dur2", "expr": "i_duration * 2"}
    flux = _rule("first2", "i_f2", f"FIRST_VALUE(i_dur2) OVER ({PARTITION} ORDER BY i_id)")
    plans = windowed_plans([flux], [rule])
    rows = rows_of(plans)
    first: Dict[Any, Any] = {}
    for row in sorted(rows, key=lambda r: r["i_id"]):
        first.setdefault(row["i_account"], row["i_dur2"])
    for row in rows:
        assert row["i_f2"] == first[row["i_account"]]
    assert any(row["i_dur2"] is None for row in rows)


def test_the_plan_lists_the_paths_of_an_argument_that_reads_through_a_link() -> None:
    plans = windowed_plans(value_rules("i_duration, i_id"))
    by_id = {rule.id: rule for rule in plans["invoice"].rules.rules}
    assert by_id["v_i_alink"].paths == (("account",),)
    assert by_id["v_i_first"].paths == ()
    assert "i_account" in by_id["v_i_alink"].reads and "i_duration" in by_id["v_i_alink"].reads
    assert sorted(dict(plans["invoice"].reads)) == ["account"]


def test_the_column_path_and_the_per_row_path_give_the_same_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = windowed_plans(value_rules("i_duration DESC NULLS FIRST, i_id"))["invoice"]
    root = root_for()
    by_columns, by_rows, taken = _by_both_paths(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows and taken and all(taken)


@pytest.mark.parametrize(("start", "stop"), [(0, 1), (3, 40), (390, 400)])
def test_a_slice_is_the_wholes_slice(start: int, stop: int) -> None:
    plan = windowed_plans(value_rules("i_duration, i_id"))["invoice"]
    root = root_for()
    generator_rows.clear_memos()
    part = generate_row_slice(plan, start, stop, root=root)
    assert part == generate_rows(plan, root=root)[start:stop]


def test_the_orders_must_still_be_total_for_every_value_function() -> None:
    for call in ("FIRST_VALUE(i_duration)", "LAST_VALUE(i_duration)", "LAG(i_duration)"):
        rule = _rule("v", "i_first", f"{call} OVER ({PARTITION} ORDER BY i_duration)")
        message = refused(
            chain_contract(invoice_columns=_columns(["i_first"])),
            "FLX-6108",
            flux_rules=[rule],
            declared=declared(),
        )
        assert "not total" in message


def test_kernel_0_1_0_refuses_a_value_function_and_says_which_kernel_generates_it() -> None:
    rule = _rule("v", "i_first", f"LEAD(i_duration) OVER ({PARTITION} ORDER BY i_id)")
    message = refused(
        chain_contract(invoice_columns=_columns(["i_first"])),
        flux_rules=[rule],
        declared=declared(),
        counter_based=False,
    )
    assert "Kernel 0.2.0 generates" in message
