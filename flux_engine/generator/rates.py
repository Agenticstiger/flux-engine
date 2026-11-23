# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Seasonal rates: a date's calendar, drawn by thinning with a fixed number of proposals (ADR 0025 section 2).

A column with a ``rates`` entry weights each value by its month, its day of the week and, for a
TIMESTAMP, its hour of the week, all read in UTC. The intensity of a value ``t`` is the product of the
factors the entry gives, a factor that is not given counting as 1:

    lambda(t) = month[m(t)] * dayOfWeek[d(t)] * hourOfWeek[h(t)]

A DATE value is its day, so it has a month and a weekday and no hour. ``lambda_max`` is the product of each
given factor's largest weight, so no value's intensity exceeds it. Everything here is integers and exact
rationals (:class:`fractions.Fraction`), so a draw is the same in every implementation.

**Thinning** (Lewis and Shedler, 1979) wraps the draw a dates step already makes. That draw is proposal 0
of ``K = 128``. Proposal ``k`` is the step's own sampler called at position ``1 + 2k`` of the column's dates
stream, and it is tested by one ``uniform53``, ``a``, at position ``2 + 2k``: it is accepted when
``a * lambda_max < lambda(value) * 2**53``, compared as integers. The value is the first proposal accepted.
A proposal whose value is NULL has no date to weigh and is accepted at once. When none of the 128 is
accepted the value is proposal 0 and the row counts a rates fallback. A row stops at its first acceptance and
leaves the later positions untouched, so no row's words depend on another row's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

from flux_engine.generator.dates import DAY_MS, KIND_DATE, UNIFORM53

__all__ = [
    "PROPOSALS",
    "Intensity",
    "Rates",
    "Uniform",
    "civil_month",
    "intensity_of",
    "thin",
    "weekday_of",
]

#: How many proposals a thinned row tries: position ``1 + 2k`` holds proposal ``k``'s value and ``2 + 2k`` its test.
PROPOSALS = 128

_HOUR_MS = 3_600_000


def weekday_of(days: int) -> int:
    """The ISO weekday of epoch day *days*, Monday as 0: 1970-01-01 was a Thursday."""
    return (days + 3) % 7


def civil_month(days: int) -> int:
    """The month, 1 to 12, of epoch day *days* in the proleptic Gregorian calendar, for any integer.

    The days-to-civil algorithm on integers, so a day outside the years a ``DATE`` holds still has a
    month (the step that stores it refuses it afterwards).
    """
    shifted = days + 719_468
    era = shifted // 146_097
    day_of_era = shifted - era * 146_097
    year_of_era = (
        day_of_era - day_of_era // 1_460 + day_of_era // 36_524 - day_of_era // 146_096
    ) // 365
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    shifted_month = (5 * day_of_year + 2) // 153
    return shifted_month + 3 if shifted_month < 10 else shifted_month - 9


@dataclass(frozen=True)
class Intensity:
    """The factors of a ``rates`` entry as exact weights, and ``lambda_max``."""

    month: Optional[Tuple[Fraction, ...]]
    day_of_week: Optional[Tuple[Fraction, ...]]
    hour_of_week: Optional[Tuple[Fraction, ...]]
    maximum: Fraction


@dataclass(frozen=True)
class Rates:
    """The seasonal rates of one date column, as the plan holds them.

    ``kind`` is the column's grain, ``DATE`` or ``TIMESTAMP``, and each factor is the weights as written
    (decimal strings), or ``None`` when not given: ``month`` 12 (January first), ``day_of_week`` 7 (Monday
    first), ``hour_of_week`` 168 (Monday 00:00 to 00:59 UTC first, a TIMESTAMP only).
    """

    column: str
    kind: str
    month: Optional[Tuple[str, ...]] = None
    day_of_week: Optional[Tuple[str, ...]] = None
    hour_of_week: Optional[Tuple[str, ...]] = None
    _held: Intensity = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Parse each factor's decimal weights once, so a draw reads exact weights from a table."""
        object.__setattr__(
            self, "_held", _intensity_of(self.month, self.day_of_week, self.hour_of_week)
        )

    def as_document(self) -> Dict[str, Any]:
        """The rates as canonical-ready JSON, for ``generatorDigest``: each factor only when given."""
        document: Dict[str, Any] = {"column": self.column, "kind": self.kind}
        if self.month is not None:
            document["month"] = list(self.month)
        if self.day_of_week is not None:
            document["dayOfWeek"] = list(self.day_of_week)
        if self.hour_of_week is not None:
            document["hourOfWeek"] = list(self.hour_of_week)
        return document

    @property
    def intensity(self) -> Intensity:
        """The factors as exact weights, and ``lambda_max``."""
        return self._held

    def lam(self, value: int) -> Fraction:
        """``lambda`` of *value*, epoch days for a ``DATE`` and epoch milliseconds for a ``TIMESTAMP``."""
        return intensity_of(self, value)

    def accepts(self, a: int, value: int) -> bool:
        """Whether a test ``uniform53`` *a* accepts *value*: ``a * lambda_max < lambda(value) * 2**53``."""
        found = self.lam(value)
        peak = self.intensity.maximum
        return (
            a * peak.numerator * found.denominator < found.numerator * peak.denominator * UNIFORM53
        )


def _intensity_of(
    month: Optional[Tuple[str, ...]],
    day_of_week: Optional[Tuple[str, ...]],
    hour_of_week: Optional[Tuple[str, ...]],
) -> Intensity:
    def weights(raw: Optional[Tuple[str, ...]]) -> Optional[Tuple[Fraction, ...]]:
        return None if raw is None else tuple(Fraction(item) for item in raw)

    parsed = (weights(month), weights(day_of_week), weights(hour_of_week))
    peak = Fraction(1)
    for factor in parsed:
        if factor is not None:
            peak *= max(factor)
    return Intensity(parsed[0], parsed[1], parsed[2], peak)


def intensity_of(rates: Rates, value: int) -> Fraction:
    """``lambda(value)`` in UTC: the month, weekday and hour of week factors the entry gives, multiplied."""
    held = rates.intensity
    if rates.kind == KIND_DATE:
        days, hour = value, 0
    else:
        days, hour = value // DAY_MS, value // _HOUR_MS % 24
    found = Fraction(1)
    if held.month is not None:
        found *= held.month[civil_month(days) - 1]
    if held.day_of_week is not None:
        found *= held.day_of_week[weekday_of(days)]
    if held.hour_of_week is not None:
        found *= held.hour_of_week[weekday_of(days) * 24 + hour]
    return found


class Uniform(Protocol):
    """The sampler a test takes: one ``uniform53`` per call, on the dates stream at the next position."""

    def uniform53(self) -> int:
        """One ``uniform53``."""


def thin(
    rates: Rates, stream: Uniform, first: Optional[int], redraw: Callable[[], Optional[int]]
) -> Tuple[Optional[int], bool]:
    """``(value, fell_back)``: the first proposal *rates* accepts, else proposal 0.

    *first* is proposal 0, which the step has drawn at position 1 of *stream*; *redraw* is the step's own
    sampler called once more, for the next proposal's value, which *stream* then holds at its next position
    (``1 + 2k``). Each test is one ``uniform53`` on *stream* (``2 + 2k``). A proposal that is ``None`` is
    accepted at once and draws no test. After :data:`PROPOSALS` rejections the value is *first*, and the
    row has fallen back.
    """
    value = first
    for proposal in range(PROPOSALS):
        if proposal:
            value = redraw()
        if value is None or rates.accepts(stream.uniform53(), value):
            return value, False
    return first, True
