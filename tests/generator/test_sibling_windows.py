# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Windows over a row's siblings: ``ROW_NUMBER``, ``RANK`` and ``DENSE_RANK`` (EXPR-SPEC-2 section 5).

On kernel 0.2.0 a FLUX-side ``derive`` whose value holds a window is generated (ADR 0022 section 7):
the expose is made whole, then each rule step is applied to every row before the next. What is
pinned:

* **DuckDB is the oracle.** Every row's rank equals DuckDB's for the same query over the generated
  rows, with ``NULLS LAST`` and the partition written out as EXPR-SPEC-2 section 11 says. The
  fixture has partitions of different sizes, ties in the sort key, a partition of rows with a NULL
  link, a sort key with NULLs, ``DESC`` and ``NULLS FIRST``.
* **A later step sees the window.** A rule that reads a window's result, and a window that orders by
  one, read the final values.
* **One answer on every path.** The column path's cells and the per-row path's cells give the same
  rows, a slice is the whole's slice, and the steps applied over all rows give a plan with no
  window what the per-row application gives.
* **Planning.** Kernel 0.1.0 refuses the window and says which kernel generates it, and the plan lists a
  sort key's paths.
"""

from __future__ import annotations

import copy
import pickle
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pytest

from flux_engine.generator import columns, rules
from flux_engine.generator import rows as generator_rows
from flux_engine.generator.rows import generate_row_slice, generate_rows

from .test_relations import _column
from .test_rules_over_links import (
    chain_contract,
    plans_of,
    refused,
    root_for,
    statistics,
    table,
)

duckdb = pytest.importorskip("duckdb")

pytestmark = pytest.mark.unit

#: The window's order and the same order as DuckDB is told it, ``NULLS LAST`` written out.
ORDERS = [
    pytest.param("i_duration", "i_duration ASC NULLS LAST", id="asc-nulls-last"),
    pytest.param("i_duration DESC", "i_duration DESC NULLS LAST", id="desc-nulls-last"),
    pytest.param(
        "i_duration DESC NULLS FIRST", "i_duration DESC NULLS FIRST", id="desc-nulls-first"
    ),
]
PARTITION = "PARTITION BY account"


def _columns(extra: Sequence[str] = ("i_rn", "i_rk", "i_dr")) -> List[Dict[str, Any]]:
    base = chain_contract()["exposes"][2]["contract"]["schema"]
    return [*copy.deepcopy(base), *(_column(name, "INT", False) for name in extra)]


def _rule(rule_id: str, column: str, expr: str) -> Dict[str, Any]:
    return {"id": rule_id, "language": "flux-expr/2", "derive": column, "expr": expr}


def window_rules(order: str) -> List[Dict[str, Any]]:
    """The three ranks over the sort key *order*, ``ROW_NUMBER`` made total by the key."""
    return [
        _rule("rn", "i_rn", f"ROW_NUMBER() OVER ({PARTITION} ORDER BY {order}, i_id)"),
        _rule("rk", "i_rk", f"RANK() OVER ({PARTITION} ORDER BY {order})"),
        _rule("dr", "i_dr", f"DENSE_RANK() OVER ({PARTITION} ORDER BY {order})"),
    ]


def declared() -> Dict[str, Tuple[Dict[str, Any], int]]:
    """The fixture's statistics, with few durations (ties) and NULLs among them."""
    found = copy.deepcopy(statistics())
    found["invoice"][0]["i_duration"] = {
        "range": {"min": "1", "max": "6"},
        "nullFraction": "0.2",
    }
    return found


def windowed_plans(
    flux_rules: Sequence[Mapping[str, Any]],
    invariants: Sequence[Mapping[str, Any]] = (),
    **kwargs: Any,
) -> Dict[str, Any]:
    """The plans of the chain fixture whose invoices (or *flux_on*) hold *flux_rules*."""
    on_invoice = kwargs.get("flux_on", "invoice") == "invoice"
    targets = [rule["derive"] for rule in (*flux_rules, *invariants) if "derive" in rule]
    targets = targets if on_invoice else [rule["derive"] for rule in invariants]
    contract = chain_contract(invariants, invoice_columns=_columns(targets))
    return plans_of(contract, flux_rules=list(flux_rules), declared=declared(), **kwargs)


def rows_of(
    plans: Mapping[str, Any], expose: str = "invoice", seed: int = 5
) -> List[Dict[str, Any]]:
    plan = plans[expose]
    return table(plan, generate_rows(plan, root=root_for(seed)))


def oracle(rows: Sequence[Mapping[str, Any]], order: str) -> Dict[str, List[Tuple[Any, ...]]]:
    """DuckDB's ``ROW_NUMBER``, ``RANK`` and ``DENSE_RANK`` over *rows*, by ``i_id``."""
    connection = duckdb.connect()
    connection.execute("CREATE TABLE t (i_id VARCHAR, i_account VARCHAR, i_duration INTEGER)")
    connection.executemany(
        "INSERT INTO t VALUES (?, ?, ?)",
        [(row["i_id"], row["i_account"], row["i_duration"]) for row in rows],
    )
    over = "PARTITION BY i_account ORDER BY"
    found = connection.execute(
        "SELECT i_id,"
        f" ROW_NUMBER() OVER ({over} {order}, i_id ASC NULLS LAST),"
        f" RANK() OVER ({over} {order}),"
        f" DENSE_RANK() OVER ({over} {order})"
        " FROM t"
    ).fetchall()
    connection.close()
    return {
        "rn": [(i, n) for i, n, _, _ in found],
        "rk": [(i, k) for i, _, k, _ in found],
        "dr": [(i, d) for i, _, _, d in found],
    }


@pytest.mark.parametrize(("flux_order", "duckdb_order"), ORDERS)
def test_every_rank_equals_duckdbs_over_the_generated_rows(
    flux_order: str, duckdb_order: str
) -> None:
    plans = windowed_plans(window_rules(flux_order))
    rows = rows_of(plans)
    expected = oracle(rows, duckdb_order)
    by_id = {row["i_id"]: row for row in rows}
    for key, column in (("rn", "i_rn"), ("rk", "i_rk"), ("dr", "i_dr")):
        assert len(expected[key]) == len(rows) == len(by_id)
        for row_id, value in expected[key]:
            assert by_id[row_id][column] == value, (column, row_id)
    # The fixture exercises what the brief names: ties, NULL sort keys, a NULL-link partition and
    # partitions of several sizes.
    assert any(row["i_duration"] is None for row in rows)
    assert any(row["i_account"] is None for row in rows)
    assert any(row["i_rk"] != row["i_rn"] for row in rows)
    assert any(row["i_dr"] != row["i_rk"] for row in rows)
    sizes: Dict[Any, int] = {}
    for row in rows:
        sizes[row["i_account"]] = sizes.get(row["i_account"], 0) + 1
    assert len(set(sizes.values())) > 3


def test_a_rule_that_reads_a_windows_result_reads_the_final_value() -> None:
    # A later step reads the window's result on its own row ...
    late = {"id": "late", "derive": "i_late", "expr": "i_rn * 2"}
    flux_rules = [
        *window_rules("i_duration"),
        # ... and a window over the siblings orders by the earlier window's result.
        _rule(
            "over",
            "i_over",
            f"DENSE_RANK() OVER ({PARTITION} ORDER BY i_rn DESC)",
        ),
    ]
    rows = rows_of(windowed_plans(flux_rules, [late]))
    assert all(row["i_late"] == row["i_rn"] * 2 for row in rows)
    connection = duckdb.connect()
    connection.execute("CREATE TABLE t (i_id VARCHAR, i_account VARCHAR, i_rn INTEGER)")
    connection.executemany(
        "INSERT INTO t VALUES (?, ?, ?)", [(r["i_id"], r["i_account"], r["i_rn"]) for r in rows]
    )
    found = dict(
        connection.execute(
            "SELECT i_id, DENSE_RANK() OVER (PARTITION BY i_account ORDER BY i_rn DESC NULLS LAST)"
            " FROM t"
        ).fetchall()
    )
    connection.close()
    assert {row["i_id"]: row["i_over"] for row in rows} == found


def test_a_window_over_the_whole_partition_of_a_null_link_is_one_partition() -> None:
    rows = rows_of(windowed_plans(window_rules("i_duration")))
    orphans = [row for row in rows if row["i_account"] is None]
    assert len(orphans) > 3
    assert sorted(row["i_rn"] for row in orphans) == list(range(1, len(orphans) + 1))


def test_a_sort_key_that_reads_through_a_link_is_planned_and_generated() -> None:
    flux_rules = [
        _rule("rk", "i_rk", f"RANK() OVER ({PARTITION} ORDER BY account.a_score DESC NULLS LAST)"),
    ]
    plans = windowed_plans(flux_rules)
    rule = plans["invoice"].rules.rules[0]
    assert rule.paths == (("account",),) and rule.windowed
    assert "i_account" in rule.reads
    assert sorted(dict(plans["invoice"].reads)) == ["account"]
    rows = rows_of(plans)
    # Every row of a partition reads one account, so the siblings tie: rank 1 everywhere.
    assert {row["i_rk"] for row in rows} == {1}


def test_a_window_on_the_account_partitions_by_its_customer() -> None:
    flux_rules = [
        _rule(
            "ord",
            "a_score",
            "ROW_NUMBER() OVER (PARTITION BY customer ORDER BY a_channel DESC, a_id)",
        )
    ]
    plans = windowed_plans(flux_rules, flux_on="account")
    rows = rows_of(plans, "account")
    connection = duckdb.connect()
    connection.execute("CREATE TABLE t (a_id VARCHAR, a_customer VARCHAR, a_channel VARCHAR)")
    connection.executemany(
        "INSERT INTO t VALUES (?, ?, ?)",
        [(r["a_id"], r["a_customer"], r["a_channel"]) for r in rows],
    )
    found = dict(
        connection.execute(
            "SELECT a_id, ROW_NUMBER() OVER (PARTITION BY a_customer"
            " ORDER BY a_channel DESC NULLS LAST, a_id ASC NULLS LAST) FROM t"
        ).fetchall()
    )
    connection.close()
    assert {row["a_id"]: row["a_score"] for row in rows} == found


# ---------------------------------------------------------------------------
# One answer on every path
# ---------------------------------------------------------------------------


def _by_both_paths(monkeypatch: pytest.MonkeyPatch, make: Any) -> Tuple[Any, Any, List[bool]]:
    taken: List[bool] = []
    column_rows = columns.column_rows

    def spy(*args: Any) -> Any:
        computed = column_rows(*args)
        taken.append(computed is not None)
        return computed

    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", spy)
        generator_rows.clear_memos()
        by_columns = make()
    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", lambda *args: None)
        generator_rows.clear_memos()
        by_rows = make()
    return by_columns, by_rows, taken


def test_the_column_path_and_the_per_row_path_give_the_same_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = windowed_plans(window_rules("i_duration DESC NULLS FIRST"))
    root = root_for()
    plan = plans["invoice"]
    by_columns, by_rows, taken = _by_both_paths(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows
    assert taken and all(taken)


@pytest.mark.parametrize(("start", "stop"), [(0, 1), (3, 40), (117, 118), (390, 400)])
def test_a_slice_is_the_wholes_slice_in_any_order_of_generation(start: int, stop: int) -> None:
    plans = windowed_plans(window_rules("i_duration"))
    plan, root = plans["invoice"], root_for()
    generator_rows.clear_memos()
    first = generate_row_slice(plan, start, stop, root=root)
    whole = generate_rows(plan, root=root)
    assert first == whole[start:stop]
    generator_rows.clear_memos()
    assert generate_row_slice(plan, 390, 400, root=root) == whole[390:400]
    assert generate_row_slice(plan, start, stop, root=root) == whole[start:stop]


def test_the_whole_is_kept_per_plan_and_root_and_dropped_with_the_memos() -> None:
    plans = windowed_plans(window_rules("i_duration"))
    plan = plans["invoice"]
    generator_rows.clear_memos()
    a = generate_rows(plan, root=root_for(5))
    b = generate_rows(plan, root=root_for(6))
    assert a != b  # two roots never share a kept whole
    assert generate_rows(plan, root=root_for(5)) == a
    assert len(generator_rows._WHOLE) == 2
    other = windowed_plans(window_rules("i_duration DESC"))["invoice"]
    assert generate_rows(other, root=root_for(5)) != a  # nor do two plans
    # A copy of the plan, as a worker receives it, finds the same whole.
    again = pickle.loads(pickle.dumps(plan))
    assert generate_rows(again, root=root_for(5)) == a
    generator_rows.clear_memos()
    assert generator_rows._WHOLE == []


def test_the_steps_over_all_rows_give_a_plan_with_no_window_what_the_per_row_path_gives() -> None:
    # The invoice's copies read the account and its dependency-free rules; no window.
    from .test_rules_over_links import COPIES, DUE, SEGMENT

    plans = plans_of(chain_contract([*COPIES, DUE]), flux_rules=[SEGMENT])
    root = root_for()
    for expose, plan in plans.items():
        cells = generator_rows._cell_rows(
            plan, 0, plan.rows, generator_rows._stream_source(plan, root, None)
        )
        if plan.rules is None or not plan.rules.order:
            continue
        by_rows = rules._apply_by_rows(plan, cells, 0, root)
        assert rules.apply_rules_whole(plan, cells, root) == by_rows, expose


TIER = {"id": "tier_of_i_region", "dependency": {"from": ["i_region"], "to": ["i_tier"]}}


def test_a_dependency_keeps_each_rows_key_when_the_steps_run_over_all_rows() -> None:
    contract = chain_contract([TIER], invoice_columns=_columns(()))
    plain = plans_of(contract, declared=declared(), settings=None)["invoice"]
    root = root_for()
    cells = generator_rows._cell_rows(
        plain, 0, plain.rows, generator_rows._stream_source(plain, root, None)
    )
    assert rules.apply_rules_whole(plain, cells, root) == rules._apply_by_rows(
        plain, cells, 0, root
    )
    # With a window beside it, the dependency still sets one tier per region.
    windowed = windowed_plans(window_rules("i_duration"), [TIER], settings=None)
    rows = rows_of(windowed)
    tiers: Dict[Any, Any] = {}
    for row in rows:
        assert tiers.setdefault(row["i_region"], row["i_tier"]) == row["i_tier"]
    assert len(tiers) > 1 and all(row["i_rn"] is not None for row in rows)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_kernel_0_1_0_refuses_a_window_and_says_which_kernel_generates_it() -> None:
    message = refused(
        chain_contract(invoice_columns=_columns()),
        flux_rules=window_rules("i_duration")[:1],
        counter_based=False,
        declared=declared(),
    )
    assert "siblings" in message and "Kernel 0.2.0 generates" in message


def test_a_total_order_is_still_required_for_row_number() -> None:
    rule = _rule("rn", "i_rn", f"ROW_NUMBER() OVER ({PARTITION} ORDER BY i_duration)")
    refused(
        chain_contract(invoice_columns=_columns()),
        "FLX-6108",
        flux_rules=[rule],
        declared=declared(),
    )


def test_a_window_rule_reads_its_partition_and_sort_columns() -> None:
    plans = windowed_plans(window_rules("i_duration"))
    by_id = {rule.id: rule for rule in plans["invoice"].rules.rules}
    assert by_id["rn"].reads == ("i_account", "i_duration", "i_id")
    assert by_id["rk"].reads == ("i_account", "i_duration")
    assert by_id["rn"].windowed and by_id["rn"].paths == ()
    order = plans["invoice"].rules.order
    assert order == ("i_rn", "i_rk", "i_dr")


def test_a_plan_with_no_window_is_unmarked_and_a_window_plan_pickles() -> None:
    from .test_rules_over_links import COPIES

    plain = plans_of(chain_contract(COPIES[:1]))["invoice"]
    assert plain.rules is not None and not plain.rules.windowed
    plans = windowed_plans(window_rules("i_duration"))
    again = pickle.loads(pickle.dumps(plans["invoice"]))
    assert again.digest() == plans["invoice"].digest()
    assert generate_row_slice(again, 0, 20, root=root_for()) == generate_row_slice(
        plans["invoice"], 0, 20, root=root_for()
    )


def test_apply_rules_refuses_a_slice_of_a_windowed_plan() -> None:
    plans = windowed_plans(window_rules("i_duration"))
    plan = plans["invoice"]
    cells = generator_rows._cell_rows(
        plan, 0, 5, generator_rows._stream_source(plan, root_for(), None)
    )
    with pytest.raises(ValueError, match="whole expose"):
        rules.apply_rules(plan, cells, 0, root_for())
