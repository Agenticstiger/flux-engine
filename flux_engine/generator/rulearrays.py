# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A rule's value for every row of a slice at once, on scaled integers (ADR 0010, step 4).

A contract ``derive`` is evaluated row by row in exact fractions
(:mod:`flux_engine.expr2.check`). Where every number it reads is a whole number of units at
its column's scale, and every intermediate result fits an ``int64``, the same value is an
integer at the result's scale: ``+``, ``-`` and ``*`` are integer arithmetic, a comparison
aligns two scales, and ``ROUND`` is an integer division with its tie decided on the
remainder, so both rounding modes are exact. NULL is a mask beside the integers and follows
the evaluator's own rules: arithmetic with a NULL is NULL, a comparison with a NULL is false,
``COALESCE`` and ``CASE`` pick the first operand that is not NULL or the first condition that
holds.

Nothing here rounds, saturates or wraps. Before an operation runs, the interval its result can
lie in is worked out from the intervals of its operands (the leaves' are the arrays' own
minimum and maximum), and a result that could leave ``int64`` raises :class:`Unfit`. So does
anything this module does not evaluate (division, dates, any other function), and the caller
then takes the exact per-row path for that rule. pyarrow's decimal compute is not used:
arrow-rs rounds a multiply half away from zero, where this one is exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, Inexact, InvalidOperation
from typing import Any, Callable, List, Optional, Sequence, Tuple

import numpy as np

from flux_engine.expr2.ast import (
    And,
    Arith,
    Between,
    Bool,
    Call,
    Case,
    Column,
    Compare,
    In,
    IsNull,
    Neg,
    Node,
    Not,
    Null,
    Number,
    Or,
    String,
)
from flux_engine.expr2.types import ColumnType, Kind

__all__ = ["Evaluator", "NULL", "Num", "Unfit", "decimals_to_units", "units_to_decimals"]

INT64_MAX = (1 << 63) - 1

#: Eighteen decimal places is the most a rounding divisor may drop: 10**18 and twice a
#: remainder below it both fit an int64.
_MAX_DROP = 18

#: Reads a ``Decimal`` as a whole number of units without rounding: any digit it would have to
#: drop is the ``Inexact`` trap, and the precision is wider than any decimal a column holds.
_EXACT = Context(prec=100, traps=[Inexact, InvalidOperation])


class Unfit(Exception):
    """The rule is not this module's: the per-row path evaluates it."""


@dataclass
class Num:
    """Numbers as ``units * 10**-scale`` in an int64 array, NULL where ``valid`` is false."""

    units: "np.ndarray"
    scale: int
    lo: int
    hi: int
    valid: Optional["np.ndarray"] = None


@dataclass
class Text:
    """Strings in an object array, or one string, NULL where ``valid`` is false."""

    values: Any
    valid: Optional["np.ndarray"] = None


class _NullValue:
    """The ``NULL`` literal, which takes its type from the operand beside it."""


_NULL = _NullValue()
NULL = _NULL
Value = Any  # Num | Text | _NullValue


def decimals_to_units(values: Sequence[Any], scale: int) -> List[int]:
    """Each value as a whole number of units at *scale*; a value with more places is ``Unfit``."""
    try:
        return [int(v.scaleb(scale, _EXACT).to_integral_exact(context=_EXACT)) for v in values]
    except Exception as error:
        raise Unfit("a decimal that is not whole units at its scale") from error


def units_to_decimals(units: Sequence[int], scale: int) -> List[Decimal]:
    """Each whole number of units as the ``Decimal`` at *scale* that ``fit`` writes."""
    # The constructor reads the text exactly, so no context rounds it.
    suffix = f"E-{scale}"
    built = {u: Decimal(f"{u}{suffix}") for u in set(units)}
    return [built[u] for u in units]


def _and(a: Optional["np.ndarray"], b: Optional["np.ndarray"]) -> Optional["np.ndarray"]:
    if a is None:
        return b
    if b is None:
        return a
    return a & b


def _fits(value: int) -> bool:
    return -INT64_MAX <= value <= INT64_MAX


def _times(value: int, factor: int) -> int:
    product = value * factor
    if not _fits(product):
        raise Unfit("a result outside int64")
    return product


class Evaluator:
    """Evaluates checked syntax trees over one slice, a column at a time.

    *columns* names each column's type, and *read* gives a column's current values for every
    row as a sequence. A column is converted once and kept until :meth:`forget`.
    """

    def __init__(
        self,
        rows: int,
        columns: Callable[[str], ColumnType],
        read: Callable[[str], Sequence[Any]],
    ) -> None:
        self.rows = rows
        self._type_of = columns
        self._read = read
        self._cache: dict[str, Value] = {}

    def forget(self, column: str) -> None:
        self._cache.pop(column, None)

    # ------------------------------------------------------------------ leaves

    def _column(self, name: str) -> Value:
        if name in self._cache:
            return self._cache[name]
        column = self._type_of(name)
        values = self._read(name)
        value: Value
        if column.kind is Kind.NUMBER and column.scale is not None and not column.is_float:
            missing = np.fromiter((v is None for v in values), dtype=bool, count=self.rows)
            valid = None if not missing.any() else ~missing
            if column.scale == 0 and _all_ints(values):
                filled = [0 if v is None else v for v in values]
                units = np.array(filled, dtype=np.int64)
            else:
                units = np.array(
                    decimals_to_units(
                        [Decimal(0) if v is None else v for v in values], column.scale
                    ),
                    dtype=np.int64,
                )
            lo, hi = (int(units.min()), int(units.max())) if self.rows else (0, 0)
            if not (_fits(lo) and _fits(hi)):
                raise Unfit("a value outside int64")
            value = Num(units, column.scale, lo, hi, valid)
        elif column.kind is Kind.STRING:
            if not all(v is None or isinstance(v, str) for v in values):
                raise Unfit("a string column holds something else")
            missing = np.fromiter((v is None for v in values), dtype=bool, count=self.rows)
            array = np.empty(self.rows, dtype=object)
            array[:] = list(values)
            value = Text(array, None if not missing.any() else ~missing)
        else:
            raise Unfit(f"a {column.declared} column is read per row")
        self._cache[name] = value
        return value

    def _number(self, node: Number) -> Num:
        units = int(node.value * 10**node.scale)
        if not _fits(units):
            raise Unfit("a literal outside int64")
        return Num(np.full(self.rows, units, dtype=np.int64), node.scale, units, units)

    # ------------------------------------------------------------------- values

    def value(self, node: Node) -> Value:
        """The value of *node* on every row."""
        if isinstance(node, Number):
            return self._number(node)
        if isinstance(node, String):
            return Text(node.value)
        if isinstance(node, Null):
            return _NULL
        if isinstance(node, Column) and node.relationship is None:
            return self._column(node.name)
        if isinstance(node, Neg):
            operand = self.value(node.operand)
            if operand is _NULL:
                return _NULL
            num = _number(operand)
            return Num(-num.units, num.scale, -num.hi, -num.lo, num.valid)
        if isinstance(node, Arith):
            return self._arith(node)
        if isinstance(node, Case):
            return self._case(node)
        if isinstance(node, Call):
            if node.name == "COALESCE":
                return self._coalesce([self.value(arg) for arg in node.args])
            if node.name == "ROUND":
                return self._round(node)
        raise Unfit(f"{type(node).__name__} is evaluated per row")

    def _arith(self, node: Arith) -> Value:
        if node.op not in ("+", "-", "*"):
            raise Unfit("division is exact on fractions only")
        left, right = self.value(node.left), self.value(node.right)
        if left is _NULL or right is _NULL:
            return _NULL
        a, b = _number(left), _number(right)
        valid = _and(a.valid, b.valid)
        if node.op == "*":
            corners = [a.lo * b.lo, a.lo * b.hi, a.hi * b.lo, a.hi * b.hi]
            if not all(_fits(c) for c in corners):
                raise Unfit("a product outside int64")
            return Num(a.units * b.units, a.scale + b.scale, min(corners), max(corners), valid)
        scale = max(a.scale, b.scale)
        a, b = _align(a, scale), _align(b, scale)
        if node.op == "+":
            lo, hi = a.lo + b.lo, a.hi + b.hi
            units = a.units + b.units
        else:
            lo, hi = a.lo - b.hi, a.hi - b.lo
            units = a.units - b.units
        if not (_fits(lo) and _fits(hi)):
            raise Unfit("a sum outside int64")
        return Num(units, scale, lo, hi, valid)

    def _round(self, node: Call) -> Value:
        operand = self.value(node.args[0])
        if operand is _NULL:
            return _NULL
        num = _number(operand)
        scale_node = node.args[1]
        assert isinstance(scale_node, Number)
        scale = int(scale_node.text)
        if scale >= num.scale:
            return _align(num, scale)
        drop = num.scale - scale
        if drop > _MAX_DROP:
            raise Unfit("a rounding that drops more than 18 places")
        divisor = 10**drop
        whole, remainder = np.divmod(num.units, divisor)
        twice = 2 * remainder
        up = twice > divisor
        tie = twice == divisor
        if (node.mode or "HALF_AWAY_FROM_ZERO") == "HALF_TO_EVEN":
            up = up | (tie & (whole % 2 != 0))
        else:
            up = up | (tie & (num.units > 0))
        units = whole + up
        return Num(units, scale, num.lo // divisor, num.hi // divisor + 1, num.valid)

    def _coalesce(self, args: Sequence[Value]) -> Value:
        everywhere = np.ones(self.rows, dtype=bool)
        return self._select([(everywhere, arg) for arg in args], _NULL, coalesce=True)

    def _case(self, node: Case) -> Value:
        branches = [(self.predicate(when), self.value(then)) for when, then in node.whens]
        otherwise = _NULL if node.otherwise is None else self.value(node.otherwise)
        return self._select(branches, otherwise, coalesce=False)

    def _select(
        self, entries: Sequence[Tuple["np.ndarray", Value]], tail: Value, *, coalesce: bool
    ) -> Value:
        """The first entry that decides each row, else *tail*.

        ``CASE``: the first branch whose condition holds decides, whatever its value, NULL
        included. ``COALESCE``: the first value that is not NULL decides.
        """
        n = self.rows
        operands = [value for _, value in entries if value is not _NULL]
        if tail is not _NULL:
            operands.append(tail)
        if not operands:
            return _NULL
        text = all(isinstance(v, Text) for v in operands)
        if text:
            units: Any = np.empty(n, dtype=object)
            scale = 0
        else:
            scale = max(_number(v).scale for v in operands)
            units = np.zeros(n, dtype=np.int64)
        valid = np.zeros(n, dtype=bool)
        decided = np.zeros(n, dtype=bool)
        lo = hi = 0
        for mask, value in [*entries, (np.ones(n, dtype=bool), tail)]:
            if value is _NULL:
                if not coalesce:
                    decided |= mask
                continue
            if text:
                assert isinstance(value, Text)
                present, data = _text_valid(value, n), _text_array(value, n)
            else:
                number = _align(_number(value), scale)
                present, data = _valid_of(number, n), number.units
                lo, hi = min(lo, number.lo), max(hi, number.hi)
            take = ~decided & (mask & present if coalesce else mask)
            units = np.where(take, data, units)
            valid = np.where(take, present, valid)
            decided |= take
        if text:
            return Text(units, valid)
        return Num(units, scale, lo, hi, valid)

    # --------------------------------------------------------------- predicates

    def predicate(self, node: Node) -> "np.ndarray":
        """Whether the text holds on each row: true only where it is ``True``, never NULL."""
        n = self.rows
        if isinstance(node, Bool):
            return np.full(n, node.value, dtype=bool)
        if isinstance(node, And):
            return self.predicate(node.left) & self.predicate(node.right)
        if isinstance(node, Or):
            return self.predicate(node.left) | self.predicate(node.right)
        if isinstance(node, Not):
            return ~self.predicate(node.operand)
        if isinstance(node, IsNull):
            value = self.value(node.value)
            missing = _missing(value, n)
            return ~missing if node.negated else missing
        if isinstance(node, Compare):
            return self._compare(node)
        if isinstance(node, Between):
            return self._between(node)
        if isinstance(node, In):
            return self._in(node)
        raise Unfit(f"{type(node).__name__} is evaluated per row")

    def _compare(self, node: Compare) -> "np.ndarray":
        left, right = self.value(node.left), self.value(node.right)
        n = self.rows
        if left is _NULL or right is _NULL:
            return np.zeros(n, dtype=bool)
        if isinstance(left, Text) and isinstance(right, Text):
            if node.op not in ("=", "<>"):
                raise Unfit("strings are ordered per row")
            equal = np.asarray(left.values == right.values, dtype=bool)
            if equal.shape != (n,):
                equal = np.full(n, bool(equal), dtype=bool)
            both = _and(left.valid, right.valid)
            result = equal if node.op == "=" else ~equal
            return result if both is None else result & both
        a, b = _number(left), _number(right)
        scale = max(a.scale, b.scale)
        a, b = _align(a, scale), _align(b, scale)
        result = _ORDER[node.op](a.units, b.units)
        both = _and(a.valid, b.valid)
        return result if both is None else result & both

    def _between(self, node: Between) -> "np.ndarray":
        value, low, high = self.value(node.value), self.value(node.low), self.value(node.high)
        n = self.rows
        if value is _NULL or low is _NULL or high is _NULL:
            raise Unfit("BETWEEN with a NULL literal is evaluated per row")
        v, lo, hi = _number(value), _number(low), _number(high)
        scale = max(v.scale, lo.scale, hi.scale)
        v, lo, hi = _align(v, scale), _align(lo, scale), _align(hi, scale)
        vv, lv, hv = (_valid_of(x, n) for x in (v, lo, hi))
        if node.negated:
            below = vv & lv & (v.units < lo.units)
            above = vv & hv & (v.units > hi.units)
            return below | above
        return vv & lv & hv & (lo.units <= v.units) & (v.units <= hi.units)

    def _in(self, node: In) -> "np.ndarray":
        value = self.value(node.value)
        n = self.rows
        if value is _NULL:
            return np.zeros(n, dtype=bool)
        hit = np.zeros(n, dtype=bool)
        for item in node.items:
            if not isinstance(item, (Number, String)):
                raise Unfit("IN over anything but literals is evaluated per row")
            other = self.value(item)
            if isinstance(value, Text):
                hit |= np.asarray(value.values == other.values, dtype=bool)
            else:
                a, b = _number(value), _number(other)
                scale = max(a.scale, b.scale)
                a, b = _align(a, scale), _align(b, scale)
                hit |= a.units == b.units
        present = ~_missing(value, n)
        hit &= present
        return ~hit & present if node.negated else hit


_ORDER = {
    "=": np.equal,
    "<>": np.not_equal,
    "<": np.less,
    "<=": np.less_equal,
    ">": np.greater,
    ">=": np.greater_equal,
}


def _all_ints(values: Sequence[Any]) -> bool:
    return all(v is None or (isinstance(v, int) and not isinstance(v, bool)) for v in values)


def _number(value: Value) -> Num:
    if not isinstance(value, Num):
        raise Unfit("a string where a number is read")
    return value


def _align(num: Num, scale: int) -> Num:
    """*num* at a scale of at least its own, as the same numbers in finer units."""
    if num.scale == scale:
        return num
    factor = 10 ** (scale - num.scale)
    lo, hi = _times(num.lo, factor), _times(num.hi, factor)
    return Num(num.units * factor, scale, lo, hi, num.valid)


def _valid_of(value: Num, rows: int) -> "np.ndarray":
    return np.ones(rows, dtype=bool) if value.valid is None else value.valid


def _text_valid(value: Text, rows: int) -> "np.ndarray":
    return np.ones(rows, dtype=bool) if value.valid is None else value.valid


def _text_array(value: Text, rows: int) -> "np.ndarray":
    if isinstance(value.values, np.ndarray):
        return value.values
    out = np.empty(rows, dtype=object)
    out[:] = value.values
    return out


def _missing(value: Value, rows: int) -> "np.ndarray":
    if value is _NULL:
        return np.ones(rows, dtype=bool)
    valid = value.valid
    return np.zeros(rows, dtype=bool) if valid is None else ~valid
