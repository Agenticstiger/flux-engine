# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Kernel 0.2.0's rules, set on scaled integers, hold the rows the per-row path holds.

:func:`flux_engine.generator.rules.apply_rules` sets a slice's determined columns a rule at a
time on arrays where :mod:`flux_engine.generator.rulearrays` evaluates the rule, and a
dependency's draw once per distinct key. The per-row path (``_apply_by_rows``) is the oracle:
each test generates a plan both ways and compares the rows by their ``repr``, so a decimal at
another exponent or an int where a decimal belongs fails as a changed byte would.

The evaluator is also held to the exact evaluator itself, ``Checked.evaluate``, over
generated data: edge values, NULLs, several scales, and results that leave int64 (which it must
refuse rather than wrap).
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any, Callable, Dict, List, Sequence, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.core import kernel2
from flux_engine.core.seedtree import derive_root, digest_bytes
from flux_engine.expr2 import VALUE, Rows, check, parse
from flux_engine.expr2.parser import Side
from flux_engine.generator import columns, rulearrays, rules
from flux_engine.generator.rows import generate_row_slice, generate_rows
from flux_engine.generator.rulearrays import Evaluator, Num, Unfit

from . import test_dependencies as deps
from . import test_rules
from .test_columns import _ruled_plan

pytestmark = pytest.mark.unit


def _root(seed: int = 5) -> Any:
    return derive_root(
        digest_bytes(test_rules.SEAM), seed, seed_tree_version=kernel2.SEED_TREE_VERSION
    )


class _Paths:
    """What :func:`_both` saw the array path do."""

    def __init__(self) -> None:
        self.taken: List[bool] = []
        self.by_rows_steps = 0


def _both(monkeypatch: pytest.MonkeyPatch, make: Callable[[], Any]) -> Tuple[str, str, _Paths]:
    """``repr`` of *make*'s rows by the array path and by the per-row path."""
    seen = _Paths()
    real, real_step = rules._apply_on_arrays, rules._derive_by_rows

    def spy(*args: Any) -> Any:
        done = real(*args)
        seen.taken.append(done is not None)
        return done

    def spy_step(*args: Any) -> Any:
        seen.by_rows_steps += 1
        return real_step(*args)

    with monkeypatch.context() as patch:
        patch.setattr(rules, "_apply_on_arrays", spy)
        patch.setattr(rules, "_derive_by_rows", spy_step)
        by_arrays = repr(make())
    with monkeypatch.context() as patch:
        patch.setattr(rules, "_apply_on_arrays", lambda *args: None)
        by_rows = repr(make())
    return by_arrays, by_rows, seen


# ---------------------------------------------------------------------------
# An invoice made of rules
# ---------------------------------------------------------------------------

_INVOICE_SCHEMA = [
    test_rules._column("qty", "INT"),
    test_rules._column("price", "DECIMAL(12,2)"),
    test_rules._column("disc", "DECIMAL(5,3)"),
    test_rules._column("tier", "STRING"),
    test_rules._column("small", "SMALLINT"),
    test_rules._column("net", "DECIMAL(14,2)"),
    test_rules._column("off", "DECIMAL(14,2)"),
    test_rules._column("vat", "DECIMAL(14,2)"),
    test_rules._column("big", "INT"),
    test_rules._column("unit", "DECIMAL(14,2)"),
    test_rules._column("wide", "DECIMAL(30,10)"),
    test_rules._column("odd", "DECIMAL(14,2)"),
    test_rules._column("fixed", "DECIMAL(9,1)"),
]

_INVOICE_RULES: List[Dict[str, Any]] = [
    {"id": "net", "derive": "net", "expr": "price * qty"},
    {"id": "off", "derive": "off", "expr": "ROUND(net * COALESCE(disc, 0), 2, 'HALF_TO_EVEN')"},
    {
        "id": "vat",
        "derive": "vat",
        "expr": (
            "CASE WHEN tier = 'gold' THEN ROUND(net * 0.2, 2) "
            "WHEN tier IN ('silver', 'bronze') THEN ROUND((net - off) * 0.15, 2) ELSE NULL END"
        ),
    },
    {
        "id": "big",
        "derive": "big",
        "expr": "CASE WHEN net > 100 AND NOT qty < 2 OR tier IS NULL THEN 1 ELSE 0 END",
    },
    # A division, which the per-row path alone evaluates exactly; qty 0 gives NULL.
    {"id": "unit", "derive": "unit", "expr": "ROUND(net / qty, 2)"},
    # Past int64 at its scale: a product of two wide scales.
    {"id": "wide", "derive": "wide", "expr": "price * price * price * qty"},
    {"id": "odd", "derive": "odd", "expr": "ROUND(price * disc, 2) - small"},
    {"id": "fixed", "derive": "fixed", "expr": "ROUND(price, 1, 'HALF_TO_EVEN')"},
]

_INVOICE_STATISTICS: Dict[str, Any] = {
    "qty": {"range": {"min": "0", "max": "40"}},
    "price": {"range": {"min": "-500", "max": "9999.99"}},
    "disc": {"range": {"min": "0", "max": "0.5"}, "nullFraction": "0.3"},
    "tier": {
        "levels": {"gold": "0.3", "silver": "0.3", "bronze": "0.2", "none": "0.2"},
        "nullFraction": "0.1",
    },
    "small": {"range": {"min": "-3", "max": "3"}},
}


def _invoice(rows: int = 500) -> Any:
    contract = test_rules._contract(test_rules._expose("invoice", _INVOICE_SCHEMA, _INVOICE_RULES))
    return _ruled_plan(contract, "invoice", _INVOICE_STATISTICS, rows=rows), contract


def test_an_invoice_made_of_rules_is_set_on_arrays_as_the_rows_set_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _ = _invoice()
    by_arrays, by_rows, seen = _both(monkeypatch, lambda: generate_rows(plan, root=_root()))
    assert by_arrays == by_rows
    assert seen.taken == [True]
    # The division and the product past int64 are the per-row path's, step by step; every
    # other rule ran on arrays.
    assert seen.by_rows_steps == 2
    data = test_rules.table(plan, generate_rows(plan, root=_root()))
    assert any(row["tier"] is None for row in data) and any(row["disc"] is None for row in data)
    assert any(row["vat"] is None for row in data)
    assert any(row["unit"] is None for row in data)


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
@pytest.mark.parametrize(("start", "stop"), [(0, 1), (17, 18), (3, 211), (0, 500)])
def test_a_slice_of_ruled_rows_is_the_per_row_slice(
    monkeypatch: pytest.MonkeyPatch, seed: int, start: int, stop: int
) -> None:
    plan, _ = _invoice()
    by_arrays, by_rows, seen = _both(
        monkeypatch, lambda: generate_row_slice(plan, start, stop, root=_root(seed))
    )
    assert by_arrays == by_rows
    assert seen.taken == [True]


def test_every_rule_holds_on_the_rows_the_arrays_set() -> None:
    plan, contract = _invoice(200)
    data = test_rules.table(plan, generate_rows(plan, root=_root()))
    assert test_rules.assert_every_rule_holds(contract, "invoice", data) >= 0


def test_a_value_that_does_not_fit_its_column_raises_what_the_rows_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = test_rules._contract(
        test_rules._expose(
            "t",
            [test_rules._column("a", "INT"), test_rules._column("b", "DECIMAL(4,2)")],
            [{"id": "b", "derive": "b", "expr": "a * 7"}],
        )
    )
    plan = _ruled_plan(contract, "t", {"a": {"range": {"min": "0", "max": "999"}}}, rows=60)

    def attempt() -> str:
        try:
            generate_rows(plan, root=_root())
        except Exception as error:  # noqa: BLE001 - the two paths' errors are compared
            return f"{type(error).__name__}: {error}"
        return "no error"

    by_arrays, by_rows, seen = _both(monkeypatch, attempt)
    assert by_arrays == by_rows
    assert "FLX-6112" in by_rows
    assert not any(seen.taken)  # the array path met the error and left it to the rows


# ---------------------------------------------------------------------------
# Dependencies: one draw per key
# ---------------------------------------------------------------------------


def test_a_dependency_draws_once_per_key_as_the_rows_draw(monkeypatch: pytest.MonkeyPatch) -> None:
    contract = deps.star_with(deps.TICKET)
    plans = deps.star_plans(contract, version=kernel2.SEED_TREE_VERSION)
    root = deps.root_for(kernel2.SEED_TREE_VERSION)
    plan = plans["store_sales"]
    by_arrays, by_rows, seen = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_arrays == by_rows
    # The ticket dependency sets foreign keys, which the per-row path alone reads: the
    # whole slice is its own.
    assert seen.taken == [False]


def test_a_dependency_on_plain_columns_is_set_on_arrays(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = deps.single_plan(deps.chain_contract(), version=kernel2.SEED_TREE_VERSION)
    root = deps.root_for(kernel2.SEED_TREE_VERSION)
    by_arrays, by_rows, seen = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_arrays == by_rows
    assert seen.taken == [True]


def test_rows_with_a_null_from_value_keep_their_own_value(monkeypatch: pytest.MonkeyPatch) -> None:
    contract = test_rules._contract(
        test_rules._expose(
            "t",
            [
                test_rules._column("k", "INT"),
                test_rules._column("v", "DECIMAL(9,2)"),
                test_rules._column("w", "INT"),
            ],
            [{"id": "dep", "dependency": {"from": ["k"], "to": ["v", "w"]}}],
        )
    )
    stats = {
        "k": {"range": {"min": "1", "max": "9"}, "nullFraction": "0.25"},
        "v": {"range": {"min": "0", "max": "99"}},
        "w": {"range": {"min": "0", "max": "5"}},
    }
    plan = _ruled_plan(contract, "t", stats, rows=400)
    by_arrays, by_rows, seen = _both(monkeypatch, lambda: generate_rows(plan, root=_root()))
    assert by_arrays == by_rows
    assert seen.taken == [True]
    data = test_rules.table(plan, generate_rows(plan, root=_root()))
    assert any(row["k"] is None for row in data)


# ---------------------------------------------------------------------------
# The evaluator against the exact one
# ---------------------------------------------------------------------------

_EVAL_SCHEMA = [
    test_rules._column("a", "DECIMAL(18,3)"),
    test_rules._column("b", "DECIMAL(10,2)"),
    test_rules._column("n", "INT"),
    test_rules._column("s", "STRING"),
]

_EXPRESSIONS = [
    "a + b",
    "a - b",
    "a * b",
    "a * n",
    "-a + n",
    "a + 1.5",
    "ROUND(a, 2)",
    "ROUND(a, 0)",
    "ROUND(a, 1, 'HALF_TO_EVEN')",
    "ROUND(a * b, 2, 'HALF_TO_EVEN')",
    "ROUND(a * b, 3)",
    "ROUND(b, 5)",
    "COALESCE(a, b, 0)",
    "COALESCE(a, n)",
    "COALESCE(s, 'none')",
    "CASE WHEN a > b THEN a WHEN a = b THEN n ELSE b * 2 END",
    "CASE WHEN a > 0 THEN b END",
    "CASE WHEN a IS NULL THEN NULL ELSE b END",
    "CASE WHEN s = 'x' THEN a + b WHEN s <> 'x' THEN n ELSE 0 END",
    "CASE WHEN a BETWEEN b AND n THEN 1 ELSE 0 END",
    "CASE WHEN a NOT BETWEEN b AND n THEN 1 ELSE 0 END",
    "CASE WHEN n IN (1, 2, 3) THEN 1 WHEN n NOT IN (4, 5) THEN 2 ELSE 3 END",
    "CASE WHEN s IN ('x', 'y') OR s IS NULL THEN 1 ELSE 0 END",
    "CASE WHEN a >= b AND (n <= 2 OR NOT s = 'y') THEN a - b ELSE n END",
    "CASE WHEN a <> 0 THEN n END",
]

_MODEL = test_rules.model_from_contract(test_rules._contract(test_rules._expose("e", _EVAL_SCHEMA)))
_CHECKED = {
    text: check(parse(text, side=Side.CORE), _MODEL, "e", position=VALUE) for text in _EXPRESSIONS
}

_EDGES_A = [
    Decimal(x)
    for x in ("0", "0.001", "-0.001", "1.5", "-1.5", "2.5", "-2.5", "999999999999999.999")
]
_EDGES_B = [Decimal(x) for x in ("0", "0.01", "-0.01", "0.05", "-0.05", "0.25", "99999999.99")]


def _optional(strategy: Any) -> Any:
    return st.one_of(st.none(), strategy)


_A = _optional(
    st.one_of(
        st.sampled_from(_EDGES_A),
        st.integers(-(10**15), 10**15).map(lambda u: Decimal(u).scaleb(-3)),
    )
)
_B = _optional(
    st.one_of(
        st.sampled_from(_EDGES_B), st.integers(-(10**9), 10**9).map(lambda u: Decimal(u).scaleb(-2))
    )
)
_N = _optional(st.one_of(st.integers(-5, 6), st.sampled_from([0, 10**6, -(10**6), 2**40])))
_S = _optional(st.sampled_from(["x", "y", "z", ""]))
_ROWS = st.lists(st.tuples(_A, _B, _N, _S), min_size=1, max_size=40)


def _decimal_at(value: Decimal, scale: int) -> Decimal:
    return Decimal((value.as_tuple().sign, value.as_tuple().digits, -scale))


def _expected(data: Sequence[Tuple[Any, ...]], text: str) -> List[Any]:
    names = ["a", "b", "n", "s"]
    rows = [dict(zip(names, row, strict=True)) for row in data]
    memory = Rows(_MODEL, {"e": rows})
    return [_CHECKED[text].evaluate(row) for row in memory.rows("e")]


def _evaluate(data: Sequence[Tuple[Any, ...]], text: str) -> List[Any]:
    cols = list(zip(*data, strict=True))
    index = {"a": 0, "b": 1, "n": 2, "s": 3}
    scope = _MODEL.exposes["e"]
    evaluator = Evaluator(
        len(data), lambda name: scope.columns[name], lambda name: cols[index[name]]
    )
    result = evaluator.value(_CHECKED[text].parsed.tree)
    if result is rulearrays.NULL:
        return [None] * len(data)
    if isinstance(result, Num):
        valid = [True] * len(data) if result.valid is None else result.valid.tolist()
        return [
            Fraction(int(u), 10**result.scale) if ok else None
            for u, ok in zip(result.units.tolist(), valid, strict=True)
        ]
    valid = [True] * len(data) if result.valid is None else result.valid.tolist()
    return [v if ok else None for v, ok in zip(result.values.tolist(), valid, strict=True)]


def _same(got: Sequence[Any], want: Sequence[Any]) -> bool:
    return all(
        (g == w) and (isinstance(g, str) == isinstance(w, str)) and (g is None) == (w is None)
        for g, w in zip(got, want, strict=True)
    )


@pytest.mark.property
@pytest.mark.parametrize("text", _EXPRESSIONS)
@settings(max_examples=60, deadline=None)
@given(data=_ROWS)
def test_the_evaluator_agrees_with_the_exact_one(text: str, data: List[Tuple[Any, ...]]) -> None:
    try:
        got = _evaluate(data, text)
    except Unfit:
        return  # the rows' own path takes it
    assert _same(got, _expected(data, text)), (text, data)


@pytest.mark.parametrize("text", _EXPRESSIONS)
def test_moderate_data_is_never_refused(text: str) -> None:
    data = [
        (Decimal("12.345"), Decimal("1.05"), 3, "x"),
        (Decimal("-0.5"), Decimal("-2.25"), None, "y"),
        (None, Decimal("0.00"), 0, None),
        (Decimal("2.500"), None, 2, "z"),
    ]
    assert _same(_evaluate(data, text), _expected(data, text))


def test_a_result_that_would_leave_int64_is_refused_not_wrapped() -> None:
    big = Decimal(10**15).scaleb(-3) * 9
    data = [(big, Decimal("99999999.99"), 2**40, "x")]
    with pytest.raises(Unfit):
        _evaluate(data, "a * b")
    with pytest.raises(Unfit):
        _evaluate(data, "a * n")


def test_a_decimal_with_more_places_than_its_scale_is_refused() -> None:
    with pytest.raises(Unfit):
        _evaluate([(Decimal("1.2345"), Decimal("1"), 1, "x")], "a + b")


@pytest.mark.parametrize("mode", ["HALF_AWAY_FROM_ZERO", "HALF_TO_EVEN"])
def test_a_tie_rounds_as_the_exact_rounding_does(mode: str) -> None:
    text = f"ROUND(a, 0, '{mode}')"
    checked = check(parse(text, side=Side.CORE), _MODEL, "e", position=VALUE)
    data = [(Decimal(x), None, None, None) for x in ("0.5", "1.5", "2.5", "-0.5", "-1.5", "-2.5")]
    cols = list(zip(*data, strict=True))
    scope = _MODEL.exposes["e"]
    evaluator = Evaluator(
        len(data), lambda name: scope.columns[name], lambda name: cols["abns".index(name)]
    )
    result = evaluator.value(checked.parsed.tree)
    assert isinstance(result, Num)
    rows = Rows(_MODEL, {"e": [dict(zip("abns", row, strict=True)) for row in data]})
    want = [checked.evaluate(row) for row in rows.rows("e")]
    assert [Fraction(int(u), 10**result.scale) for u in result.units.tolist()] == want


def test_the_column_path_and_the_rules_together_are_the_per_row_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both fast paths on, then both off: the same rows."""
    plan, _ = _invoice(300)
    root = _root(9)
    fast = repr(generate_rows(plan, root=root))
    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", lambda *args: None)
        patch.setattr(rules, "_apply_on_arrays", lambda *args: None)
        slow = repr(generate_rows(plan, root=root))
    assert fast == slow
