# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A query's rate (ADR 0028 section 4): rows over unit rows, as an exact fraction.

For a query with a unit, ``rows`` is the number of rows the query returns and ``unit_rows`` the
number of rows of its unit. The rate is ``rows / unit_rows``, a :class:`~fractions.Fraction`, so
two runs that count the same rows state the same rate to the last digit and no float is involved.

The status says what the pair of counts supports:

``measured``
    The unit has rows and the query returned no more than it. The rate is a fraction from 0 to 1.
``noRows``
    The unit has no rows and the query returned none, so there is no rate to state.
``notARate``
    The query returned more rows than its unit holds, so it is not a share of the unit's records
    and is never compared with a target. It takes this status whenever ``rows`` pass ``unit_rows``,
    an empty unit included.

With ``groupBy`` the same rule is applied to each group. A group the query has no rows for has
``rows`` 0; a group only the query holds has ``unit_rows`` 0 and so ``notARate``.

This module imports the standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Iterable, Optional, Tuple

__all__ = [
    "MEASURED",
    "NOT_A_RATE",
    "NO_ROWS",
    "GroupRate",
    "Rate",
    "group_rates",
    "ordered_counts",
    "rate_of",
]

MEASURED = "measured"
NO_ROWS = "noRows"
NOT_A_RATE = "notARate"


@dataclass(frozen=True)
class Rate:
    """One pair of counts, its status and, for ``measured``, its exact rate."""

    rows: int
    unit_rows: int
    status: str
    value: Optional[Fraction]


@dataclass(frozen=True)
class GroupRate:
    """The :class:`Rate` of one group. A null group is its own key (``None``), never a level."""

    key: Optional[str]
    rate: Rate


def _whole(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} is a whole number from 0, got {value!r}")
    return value


def rate_of(rows: int, unit_rows: int) -> Rate:
    """The status and the exact rate of *rows* over *unit_rows*."""
    rows = _whole(rows, "rows")
    unit_rows = _whole(unit_rows, "unit_rows")
    if rows > unit_rows:
        return Rate(rows, unit_rows, NOT_A_RATE, None)
    if unit_rows == 0:
        return Rate(rows, unit_rows, NO_ROWS, None)
    return Rate(rows, unit_rows, MEASURED, Fraction(rows, unit_rows))


def _order(key: Optional[str]) -> Tuple[int, str]:
    """Null first, then the levels by code point, so an order never depends on the engine's."""
    return (0, "") if key is None else (1, key)


def group_rates(
    rows: Iterable[Tuple[Optional[str], int]], unit_rows: Iterable[Tuple[Optional[str], int]]
) -> Tuple[GroupRate, ...]:
    """The rate of every group either side holds, null group first, then by level.

    *rows* and *unit_rows* are ``(key, count)`` pairs, a key at most once on each side.
    """
    counted = dict(ordered_counts(rows))
    unit = dict(ordered_counts(unit_rows))
    keys = sorted(set(counted) | set(unit), key=_order)
    return tuple(GroupRate(key, rate_of(counted.get(key, 0), unit.get(key, 0))) for key in keys)


def ordered_counts(
    pairs: Iterable[Tuple[Optional[str], int]],
) -> Tuple[Tuple[Optional[str], int], ...]:
    """*pairs* with the null group first and then the levels by code point.

    A group named twice is a ``ValueError``: DuckDB's own order never reaches a result.
    """
    seen: Dict[Optional[str], int] = {}
    for key, count in pairs:
        if key in seen:
            raise ValueError(f"the group {key!r} is counted twice")
        seen[key] = count
    return tuple((key, seen[key]) for key in sorted(seen, key=_order))
