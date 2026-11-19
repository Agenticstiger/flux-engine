# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""FLUID SQL/1's numeric and date functions, exact (RFC-data-rules.md §2.6).

Only ``int``, :class:`~fractions.Fraction` and :class:`datetime.date` are
used: no binary float, no :mod:`decimal` context, no clock.

* :func:`round_exact` rounds an exact rational to ``s`` decimal places,
  with ties away from zero (``ROUND(x, s)``, as PostgreSQL ``numeric``,
  Snowflake ``NUMBER``, BigQuery ``NUMERIC`` and DuckDB ``DECIMAL`` round)
  or to the even neighbor (``'HALF_TO_EVEN'``). The two modes are the
  ``TIE_AWAY_FROM_ZERO`` and ``TIE_TO_EVEN`` of Substrait's rounding
  functions. A rational such as one third has no finite decimal expansion,
  so the decision is made on integers: ``x * 10**s`` is split into its
  floor and a remainder, and twice the remainder against the denominator
  says below, above or exactly half.
* :func:`days_between`, :func:`months_between` and :func:`years_between`
  count whole units from ``a`` to ``b``: for ``b`` on or after ``a``, the
  largest ``k`` such that ``a`` moved forward ``k`` units is not after
  ``b``, where moving by months keeps the day of the month or clamps it to
  the month's last day; for ``b`` before ``a``, minus the count from ``b``
  to ``a``. These equal DuckDB's ``date_sub``, which ADR 0008 checked on
  40,864 pairs, and are not ``DATEDIFF``, which counts boundaries crossed.
* :func:`add_days`, :func:`try_add_months` and :func:`add_years` are flux-expr/3's
  date arithmetic (EXPR-SPEC-3 §4), Db2's ``ADD_DAYS``, ``ADD_MONTHS`` and
  ``ADD_YEARS``. A month or year move clamps the day to the target month's last
  day, as PostgreSQL, DuckDB and Db2 do (Oracle's ``ADD_MONTHS`` does not), so
  ``months_between(a, try_add_months(a, n))`` is ``n``: they are the inverse of
  the whole-unit differences. They are total: a result outside 0001-01-01 to
  9999-12-31 is ``None``, never an error.
"""

from __future__ import annotations

from datetime import date, timedelta
from fractions import Fraction
from typing import Optional

__all__ = [
    "add_days",
    "add_months",
    "add_years",
    "days_between",
    "months_between",
    "round_exact",
    "try_add_months",
    "years_between",
]

HALF_AWAY_FROM_ZERO = "HALF_AWAY_FROM_ZERO"
HALF_TO_EVEN = "HALF_TO_EVEN"


def round_exact(value: Fraction, scale: int, mode: str = HALF_AWAY_FROM_ZERO) -> Fraction:
    """*value* rounded to *scale* decimal places, exactly."""
    unit = 10**scale
    shifted = value * unit
    whole, remainder = divmod(shifted.numerator, shifted.denominator)
    twice = 2 * remainder
    if twice < shifted.denominator:
        rounded = whole
    elif twice > shifted.denominator:
        rounded = whole + 1
    elif mode == HALF_TO_EVEN:
        rounded = whole if whole % 2 == 0 else whole + 1
    else:
        # ``whole`` is the floor, so a negative tie is already the value
        # farther from zero, and a positive one is the next integer up.
        rounded = whole + 1 if shifted > 0 else whole
    return Fraction(rounded, unit)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if _is_leap(year) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def add_months(start: date, months: int) -> date:
    """*start* moved *months* forward (or back), the day clamped to the month's last day."""
    total = start.year * 12 + (start.month - 1) + months
    year, index = divmod(total, 12)
    month = index + 1
    return date(year, month, min(start.day, _days_in_month(year, month)))


def add_days(start: date, days: int) -> Optional[date]:
    """*start* moved *days* forward (or back); ``None`` when the result leaves 0001 to 9999."""
    try:
        return start + timedelta(days=days)
    except OverflowError:
        return None


def try_add_months(start: date, months: int) -> Optional[date]:
    """:func:`add_months`, or ``None`` when the result leaves 0001 to 9999."""
    try:
        return add_months(start, months)
    except (ValueError, OverflowError):
        return None


def add_years(start: date, years: int) -> Optional[date]:
    """*start* moved *years* years: twelve times as many months, so 29 February clamps to 28."""
    return try_add_months(start, 12 * years)


def days_between(a: date, b: date) -> int:
    """The whole days from *a* to *b*."""
    return (b - a).days


def months_between(a: date, b: date) -> int:
    """The whole months from *a* to *b*."""
    if b < a:
        return -months_between(b, a)
    months = (b.year - a.year) * 12 + (b.month - a.month)
    if add_months(a, months) > b:
        months -= 1
    return months


def years_between(a: date, b: date) -> int:
    """The whole years from *a* to *b*: whole months divided by 12, toward zero."""
    if b < a:
        return -years_between(b, a)
    return months_between(a, b) // 12
