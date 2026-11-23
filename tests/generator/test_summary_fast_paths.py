# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a summary over children does per row, and the exact arithmetic it rests on.

The work is held to counts (gcd reductions, layouts worked out), never to a clock, and each
fast path is held to the plain computation it replaces, value for value and refusal for refusal.
"""

from __future__ import annotations

import fractions
import random
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from flux_engine.expr2.check import _aggregate_function, _exact_sum
from flux_engine.expr2.errors import Expr2Error
from flux_engine.expr2.evaluate import _number
from flux_engine.expr2.types import ColumnType, Kind, column_type, fit
from flux_engine.generator.rules import _ChildRows, _Pass


def _terms(rng: random.Random, count: int, denominators: List[int]) -> List[Fraction]:
    return [Fraction(rng.randint(-(10**9), 10**9), rng.choice(denominators)) for _ in range(count)]


@pytest.mark.parametrize("denominators", [[1], [100], [1, 2, 4, 5, 10, 100], [3, 7, 9, 12, 1000]])
def test_the_exact_sum_is_the_running_sum(denominators: List[int]) -> None:
    rng = random.Random(11)
    for size in (1, 2, 3, 17, 200):
        found = _terms(rng, size, denominators)
        assert _exact_sum(found) == sum(found, Fraction(0))
        assert type(_exact_sum(found)) is Fraction


def test_the_exact_sum_of_terms_that_are_not_fractions_is_the_running_sum() -> None:
    found: List[Any] = [Fraction(1, 3), 2, Fraction(1, 6)]
    assert _exact_sum(found) == Fraction(5, 2)


def test_summing_reduces_once_however_many_terms() -> None:
    found = _terms(random.Random(3), 1000, [1, 4, 100])
    calls = []
    real = fractions.math.gcd

    def counting(*args: int) -> int:
        calls.append(args)
        return real(*args)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fractions.math, "gcd", counting)
        _exact_sum(found)
    assert len(calls) <= 2
    calls.clear()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fractions.math, "gcd", counting)
        sum(found, Fraction(0))
    assert len(calls) > 1000  # what the running sum cost


def test_sum_and_average_over_rows_are_the_running_sum_and_its_quotient() -> None:
    values = _terms(random.Random(5), 50, [1, 10, 100])
    rows = list(range(len(values)))
    arg = lambda child, session: values[child]  # noqa: E731
    total = _aggregate_function("SUM", arg)(rows, None)  # type: ignore[arg-type]
    average = _aggregate_function("AVG", arg)(rows, None)  # type: ignore[arg-type]
    assert total == sum(values, Fraction(0))
    assert average == sum(values, Fraction(0)) / len(values)
    assert _aggregate_function("SUM", arg)([], None) is None  # type: ignore[arg-type]


def _reference_fit(value: Any, column: ColumnType, *, column_name: str, row: Optional[int]) -> Any:
    """``fit`` as it was written before the integer arithmetic: the oracle."""
    from flux_engine.expr2.errors import FLX_6112

    if value is None or column.kind is not Kind.NUMBER:
        return value
    where = f"column {column_name!r}" + ("" if row is None else f", row {row}")
    if column.scale is not None:
        scaled = value * 10**column.scale
        if scaled.denominator != 1:
            raise Expr2Error(
                FLX_6112, f"{where}: the value has more decimal places than scale {column.scale}"
            )
        units = scaled.numerator
        if column.precision is not None and abs(units) >= 10**column.precision:
            raise Expr2Error(
                FLX_6112,
                f"{where}: the value needs more than the {column.precision - column.scale} "
                f"integer digits that {column.declared!r} holds",
            )
        if column.integer_range is not None:
            low, high = column.integer_range
            if not low <= units <= high:
                raise Expr2Error(
                    FLX_6112,
                    f"{where}: the value is outside the integer range of {column.declared!r}",
                )
            return units
        sign, digits, _ = Decimal(units).as_tuple()
        return Decimal((sign, digits, -column.scale))
    return value


def _outcome(call: Any) -> Any:
    try:
        value = call()
    except Expr2Error as error:
        return ("refused", error.code, str(error))
    return (type(value), value, getattr(value, "as_tuple", lambda: None)())


@pytest.mark.parametrize(
    "declared",
    [
        "INT",
        "BIGINT",
        "SMALLINT",
        "TINYINT",
        "DECIMAL(6,2)",
        "DECIMAL(10,0)",
        "DECIMAL(5,5)",
        "DOUBLE",
    ],
)
def test_fit_agrees_with_the_plain_computation_value_and_refusal(declared: str) -> None:
    column = column_type(declared)
    rng = random.Random(7)
    values: List[Optional[Fraction]] = [
        None,
        Fraction(0),
        Fraction(-1),
        Fraction(127),
        Fraction(128),
    ]
    for _ in range(400):
        values.append(
            Fraction(rng.randint(-(10**8), 10**8), rng.choice([1, 2, 3, 10, 100, 1000, 7]))
        )
    for value in values:
        got = _outcome(lambda value=value: fit(value, column, column_name="c", row=4))
        want = _outcome(lambda value=value: _reference_fit(value, column, column_name="c", row=4))
        assert got == want, (declared, value)
    assert _outcome(lambda: fit(Fraction(1, 3), column, column_name="c")) == _outcome(
        lambda: _reference_fit(Fraction(1, 3), column, column_name="c", row=None)
    )


@pytest.mark.parametrize(
    "value",
    [
        0,
        7,
        -3,
        True,
        Decimal("1.50"),
        Decimal("-0.001"),
        Fraction(1, 3),
        2.5,
        Decimal("NaN"),
        Decimal("Infinity"),
        "x",
        None,
    ],
)
def test_a_number_is_read_as_before_and_a_refusal_keeps_its_message(value: Any) -> None:
    def old() -> Any:
        if value is None:
            return None
        if isinstance(value, bool):
            raise TypeError("a number column holds a number, not a bool")
        if isinstance(value, Fraction):
            return value
        if isinstance(value, int):
            return Fraction(value)
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise TypeError("a number column holds a finite number")
            return Fraction(value)
        if isinstance(value, float):
            return Fraction(Decimal(repr(float(value))))
        raise TypeError(
            f"a number column holds int, Decimal, Fraction or float, not {type(value).__name__}"
        )

    def outcome(call: Any) -> Any:
        try:
            return call()
        except TypeError as error:
            return ("refused", str(error))

    assert outcome(lambda: _number(value)) == outcome(old)


def _pass(layouts: List[int]) -> Any:
    """A pass over a plan that sums two child exposes, with a reader that counts its layouts."""
    first = SimpleNamespace(expose=SimpleNamespace(expose_id="line"), rows=0)
    second = SimpleNamespace(expose=SimpleNamespace(expose_id="note"), rows=0)
    duplicate = SimpleNamespace(expose=SimpleNamespace(expose_id="line"), rows=0)
    plan = SimpleNamespace(
        sums=[
            ("line", "invoice", first),
            ("note", "invoice", second),
            ("line", "invoice", duplicate),
        ]
    )
    context = _Pass(plan, None)  # type: ignore[arg-type]

    class Reader:
        def children_of(self, child: Any, relationship: str, row: int) -> range:
            layouts.append(row)
            return range(row, row + 3)

    context._reader = Reader()
    context.first = first  # type: ignore[attr-defined]
    return context


def test_the_children_of_a_row_are_laid_out_once_for_every_summary_of_that_row() -> None:
    layouts: List[int] = []
    context = _pass(layouts)
    for _ in range(3):
        assert context.children(5, "line", "invoice")._run == range(5, 8)
    assert layouts == [5]
    context.children(5, "note", "invoice")
    assert layouts == [5, 5]
    context.children(6, "line", "invoice")
    context.children(6, "line", "invoice")
    assert layouts == [5, 5, 6]
    assert context.children(6, "line", "invoice")._plan is context.first  # the first match reads


def test_a_summary_over_an_expose_the_plan_does_not_carry_is_a_key_error() -> None:
    context = _pass([])
    with pytest.raises(KeyError):
        context.children(1, "missing", "invoice")


def test_iterating_the_children_reads_the_rows_indexing_reads() -> None:
    read: List[int] = []

    class Context:
        def child(self, plan: Any, row: int) -> Any:
            read.append(row)
            return ("row", row)

    children = _ChildRows(Context(), None, range(10, 14))  # type: ignore[arg-type]
    assert list(children) == [children[i] for i in range(len(children))]
    assert read[:4] == [10, 11, 12, 13]
    with pytest.raises(TypeError):
        children[0:2]
