# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What evaluating a checked flux-expr/2 text needs from the rows (EXPR-SPEC-2 §3, §5).

A text reads one row and what its names reach: the rows its relationships
point to, the entity's previous version, the children that point to it,
and the siblings that share its partition. :class:`RowView` is that
interface. The generator supplies its own views; :mod:`.rows` holds an
in-memory one for the vectors and the tests.

Values are exact. A number column's value is read as a
:class:`~fractions.Fraction`: an ``int``, a :class:`~decimal.Decimal` or a
``Fraction`` converts exactly, and a ``float`` (a float column) is read as
the shortest decimal that converts back to the same double, which is its
``repr`` (RFC-data-rules.md §2.3). A timestamp is its milliseconds since
the epoch. A value of the wrong class is a ``TypeError``: the rows are the
engine's own, so that is a bug, never a diagnostic.

A :class:`Session` holds one pass's aggregate and window results, so each
is computed once per row of its scope and reused wherever that row needs
it again (EXPR-SPEC-2 §5).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from functools import cmp_to_key
from typing import Any, Callable, Dict, Hashable, List, Optional, Protocol, Sequence, Tuple

from flux_engine.expr2.types import ColumnType, Kind

__all__ = [
    "Decoder",
    "RowView",
    "Session",
    "decoder",
    "key_value",
    "sort_rows",
]

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MILLISECOND = timedelta(milliseconds=1)

Decoder = Callable[[Any], Any]


class RowView(Protocol):
    """One row, and the rows its names can reach."""

    @property
    def key(self) -> Hashable:
        """An identity for this row, unique across every expose of the pass."""

    def get(self, column: str) -> Any:
        """The stored value of *column*, ``None`` for NULL."""

    def follow(self, relationship: str) -> Optional["RowView"]:
        """The row *relationship* reaches, or ``None`` (a NULL key, or one that matches nothing)."""

    def previous(self) -> Optional["RowView"]:
        """This entity's previous version, or ``None`` on its first version."""

    def children(self, expose: str, relationship: str) -> Sequence["RowView"]:
        """The rows of *expose* whose *relationship* points to this row."""

    def siblings(self, relationship: str) -> Sequence["RowView"]:
        """This row's partition: the rows of its expose with equal *relationship* columns.

        NULL equals NULL here, as in SQL's PARTITION BY, and numbers are equal
        by value. The order is the rows' own order; this row is included.
        """


class Session:
    """The aggregate and window results of one evaluation pass."""

    def __init__(self) -> None:
        self.aggregates: Dict[Tuple[object, Hashable], Any] = {}
        self.windows: Dict[Tuple[object, Hashable], Dict[Hashable, Any]] = {}


def _number(value: Any) -> Optional[Fraction]:
    if value is None:
        return None
    kind = type(value)
    if kind is int:  # the exact types first; a bool or a subclass takes the checks below
        return Fraction(value)
    if kind is Decimal and value.is_finite():
        return Fraction(value)
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
        if value != value or value in (float("inf"), float("-inf")):
            raise TypeError("a number column holds a finite number")
        # repr(float(...)): a float subclass (numpy's float64) has a repr of its own.
        return Fraction(Decimal(repr(float(value))))
    raise TypeError(
        f"a number column holds int, Decimal, Fraction or float, not {type(value).__name__}"
    )


def _string(value: Any) -> Optional[str]:
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"a string column holds str, not {type(value).__name__}")


def _bool(value: Any) -> Optional[bool]:
    if value is None or isinstance(value, bool):
        return value
    raise TypeError(f"a bool column holds bool, not {type(value).__name__}")


def _date(value: Any) -> Optional[date]:
    if value is None or (isinstance(value, date) and not isinstance(value, datetime)):
        return value
    raise TypeError(f"a date column holds datetime.date, not {type(value).__name__}")


def _timestamp(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return (moment - _EPOCH) // _MILLISECOND
    raise TypeError(
        f"a timestamp column holds int milliseconds or datetime, not {type(value).__name__}"
    )


_DECODERS: Dict[Kind, Decoder] = {
    Kind.NUMBER: _number,
    Kind.STRING: _string,
    Kind.BOOL: _bool,
    Kind.DATE: _date,
    Kind.TIMESTAMP: _timestamp,
    Kind.TIMESTAMPTZ: _timestamp,
}


def decoder(column: ColumnType) -> Decoder:
    """The function that reads a stored value of *column* as flux-expr/2 compares it."""
    if column.kind is None:
        return lambda value: value
    return _DECODERS[column.kind]


def key_value(column: ColumnType, value: Any) -> Any:
    """*value* as a partition key compares it: numbers by value, everything else as stored."""
    return decoder(column)(value)


def _compare(left: Any, right: Any, desc: bool, nulls_first: bool) -> int:
    if left is None and right is None:
        return 0
    if left is None:
        return -1 if nulls_first else 1
    if right is None:
        return 1 if nulls_first else -1
    if left == right:
        return 0
    order = -1 if left < right else 1
    return -order if desc else order


def sort_rows(
    rows: Sequence[RowView], keys: List[List[Any]], directions: Sequence[Tuple[bool, bool]]
) -> List[int]:
    """The positions of *rows* in SQL ORDER BY order; ties keep their given order.

    ``keys[i]`` is row ``i``'s sort-key values and ``directions[j]`` the
    ``(desc, nulls_first)`` of key ``j``. NULL sorts last in both directions
    unless ``nulls_first`` (EXPR-SPEC-2 §5). Python's sort is stable, so
    rows that tie keep the order the view gave them.
    """

    def by_keys(i: int, j: int) -> int:
        for left, right, (desc, nulls_first) in zip(keys[i], keys[j], directions, strict=True):
            order = _compare(left, right, desc, nulls_first)
            if order:
                return order
        return 0

    return sorted(range(len(rows)), key=cmp_to_key(by_keys))


def peers(keys: List[List[Any]], first: int, second: int) -> bool:
    """Whether two rows tie on every sort key (NULL ties with NULL)."""
    return all(
        (a is None and b is None) or (a is not None and b is not None and a == b)
        for a, b in zip(keys[first], keys[second], strict=True)
    )
