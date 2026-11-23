# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A dwell and a dwell family: the draw of a lifetime's end given a state (ADR 0025 section 3).

A dwell is how long a row stays before its end, in whole units of the end column: days for a ``DATE``,
milliseconds for a ``TIMESTAMP`` (``unit`` below, from :func:`flux_engine.generator.dates.unit_of`). It is
a duration histogram (ADR 0024 section 4) or an exponential with a mean and an optional share of rows
that have no end. A dwell family picks the dwell by the row's state text.

This module is pure: it holds no stream and does no I/O. Every draw takes one ``uniform53`` and works on
integers and exact rationals, except the binary64 exponential that KERNEL-SPEC-2 section 5.4 specifies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple, Union

from flux_engine.core.errors import FluxError
from flux_engine.core.kernel2 import exponential_units
from flux_engine.core.rng import FLX_5021
from flux_engine.generator.dates import (
    UNIFORM53,
    CutTable,
    cut_table,
    draw_end,
    duration_table,
    offset_table,
)
from flux_engine.seam.dates import Histogram


def decimal_text(value: Fraction) -> str:
    """The canonical decimal text of *value*: plain digits, no exponent, no trailing zero after the point.

    *value* must be a finite decimal (its reduced denominator has no prime factor other than 2 and 5),
    so the text is exact. ``Fraction(1, 3)`` is refused rather than rounded.
    """
    denominator = value.denominator
    places = 0
    while denominator != 1:
        if denominator % 10 == 0:
            denominator //= 10
        elif denominator % 2 == 0:
            denominator //= 2
        elif denominator % 5 == 0:
            denominator //= 5
        else:
            raise ValueError(f"{value} is not a finite decimal")
        places += 1
    # Each factor of 2 or 5 needs at most one place, so ``places`` bounds the digits; trim afterwards.
    scaled = value * 10**places
    assert scaled.denominator == 1
    digits = str(abs(scaled.numerator)).rjust(places + 1, "0")
    text = digits[: len(digits) - places] + ("." + digits[len(digits) - places :] if places else "")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return ("-" if value < 0 else "") + text


@dataclass(frozen=True)
class HistogramDwell:
    """A duration histogram as ADR 0024 section 4 writes it: bins from 0 up, an optional ``open`` weight.

    The draw is :func:`flux_engine.generator.dates.draw_end` from offset 0.
    """

    histogram: Histogram


@dataclass(frozen=True)
class ExponentialDwell:
    """An exponential dwell: *mean* days above zero, and *open*, the share of rows with no end.

    ``0 <= open < 1``. *mean* must be a finite decimal so that :meth:`DwellFamily.as_document` writes
    it exactly; a mean such as ``1/3`` is refused (``ValueError``), as is a mean not above zero or an
    *open* outside ``[0, 1)``.
    """

    mean: Fraction
    open: Fraction = Fraction(0)

    def __post_init__(self) -> None:
        if not self.mean > 0:
            raise ValueError(f"a dwell mean of {self.mean} is not above zero")
        if not 0 <= self.open < 1:
            raise ValueError(f"a dwell open share of {self.open} is outside [0, 1)")
        decimal_text(self.mean)  # refuses a mean that is not a finite decimal
        decimal_text(self.open)


Dwell = Union[HistogramDwell, ExponentialDwell]


def _dwell_document(dwell: Dwell) -> Dict[str, Any]:
    if isinstance(dwell, HistogramDwell):
        return {"histogram": dwell.histogram.as_member()}
    return {"exponential": {"mean": decimal_text(dwell.mean), "open": decimal_text(dwell.open)}}


@dataclass(frozen=True)
class DwellFamily:
    """A dwell chosen by the row's state.

    ``given`` names the state column; ``None`` means one dwell for every row (``states`` is then empty
    and ``otherwise`` is the dwell). ``states`` pairs a state's key text with its dwell, sorted by
    state text, with distinct texts. ``otherwise`` is the dwell of a row whose state is NULL or not
    listed; without it such a row keeps the end its own cell drew.
    """

    given: Optional[str]
    states: Tuple[Tuple[str, Dwell], ...]
    otherwise: Optional[Dwell]
    _by_state: Dict[str, Dwell] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        texts = [state for state, _ in self.states]
        if texts != sorted(set(texts)):
            raise ValueError("the states of a dwell family are not sorted and distinct")
        if self.given is None and self.states:
            raise ValueError("a dwell family with no given column lists no states")
        object.__setattr__(self, "_by_state", dict(self.states))

    def dwell_for(self, state: Optional[str]) -> Optional[Dwell]:
        """The dwell of a row in *state*: its listed dwell, else ``otherwise`` (also for NULL)."""
        if state is not None:
            listed = self._by_state.get(state)
            if listed is not None:
                return listed
        return self.otherwise

    def as_document(self) -> Dict[str, Any]:
        """The family as canonical-ready JSON for a digest, in a stable key order."""
        return {
            "given": self.given,
            "states": [
                {"state": text, "dwell": _dwell_document(dwell)} for text, dwell in self.states
            ],
            "otherwise": None if self.otherwise is None else _dwell_document(self.otherwise),
        }


_SMALLEST_RATE = Fraction(1, 2**900)
_LARGEST_RATE = Fraction(2**900)


@lru_cache(maxsize=4096)
def _rate_of(mean: Fraction, unit: int) -> Decimal:
    """The rate ``1 / (mean * unit)`` rounded to binary64 once, as its exact decimal.

    The exact rate is held as a ``Fraction`` and checked against KERNEL-SPEC-2 section 5.4's bounds
    ``[2**-900, 2**900]`` before any ``float()``, so a mean too small or too large for them is
    refused with ``FLX-5021`` and never overflows. The result is cached per ``(mean, unit)``, so the
    check and the conversion run once per dwell, not once per row.
    """
    rate = Fraction(1) / (mean * unit)
    if not _SMALLEST_RATE <= rate <= _LARGEST_RATE:
        raise FluxError(
            FLX_5021,
            f"a dwell mean of {mean} in units of {unit} gives a rate outside [2**-900, 2**900], "
            "which kernel 0.2.0's binary64 draws cannot hold",
        )
    return Decimal(float(rate))


def _exponential_value(dwell: ExponentialDwell, u53: int, unit: int) -> Optional[int]:
    cut = (dwell.open.numerator * UNIFORM53) // dwell.open.denominator
    if u53 < cut:
        return None
    v = (u53 - cut) * UNIFORM53 // (UNIFORM53 - cut)
    return exponential_units(v, _rate_of(dwell.mean, unit), 0)


def draw_dwell(dwell: Dwell, u53: int, unit: int) -> Optional[int]:
    """Whole units from the start that *u53* picks, in an end column of *unit*; ``None`` for no end.

    *u53* is one ``uniform53``, below ``2**53``. A histogram dwell is ``draw_end`` from offset 0. An
    exponential dwell takes the share ``open`` first (``u53 < floor(2**53 * open)`` is no end), then
    KERNEL-SPEC-2 section 5.4's exponential at scale 0 with rate ``1 / (mean * unit)``. Its refusals
    are that section's (``FLX-5021``).
    """
    if isinstance(dwell, HistogramDwell):
        return draw_end(duration_table(dwell.histogram), 0, u53, unit)
    return _exponential_value(dwell, u53, unit)


def draw_family(
    family: DwellFamily, state: Optional[str], u53: int, unit: int
) -> Tuple[bool, Optional[int]]:
    """The draw of *family* for a row in *state*: ``(applies, units-or-None)``.

    ``(False, None)`` when no member applies, and the row keeps the end its own cell drew. Otherwise
    ``(True, value)`` with ``value`` as :func:`draw_dwell` gives it.
    """
    dwell = family.dwell_for(state)
    if dwell is None:
        return False, None
    return True, draw_dwell(dwell, u53, unit)


# ---------------------------------------------------------------------------
# A dwell cut to the end column's own declared range (ADR 0024 section 4)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4096)
def _histogram_cut(histogram: Histogram, low: int, high: int, unit: int) -> Optional[CutTable]:
    """*histogram* cut to the dwell units *low* to *high* from the start, its ``open`` weight whole.

    The cut table of an ``offset`` (:func:`flux_engine.generator.dates.cut_table`) from a start of
    0: bin day ``d`` holds the dwells ``d * unit`` to ``(d + 1) * unit - 1``.
    """
    open_weight = Fraction(histogram.open) if histogram.open is not None else Fraction(0)
    return cut_table(offset_table(histogram), 0, (low, high), unit, open_weight)


def _least_reaching(rate: Decimal, n: int) -> int:
    """The least ``uniform53`` ``v`` whose exponential value is at least *n*, or ``2**53`` when none is.

    :func:`~flux_engine.core.kernel2.exponential_units` is monotone in ``v`` (a larger ``v`` is a
    larger ``-log(1 - v / 2**53)``, rounded the same way), so the values that reach *n* are the ``v``
    from this one up. A binary64 estimate of where that is starts the search; galloping out from it
    and halving the bracket finds the exact boundary, whatever the estimate's error.
    """
    if n <= 0:
        return 0
    top = UNIFORM53 - 1

    def reaches(v: int) -> bool:
        return exponential_units(v, rate, 0) >= n

    if not reaches(top):
        return UNIFORM53
    estimate = int((1.0 - math.exp(-(n - 0.5) * float(rate))) * UNIFORM53)
    guess = min(max(estimate, 0), top)
    step = 1
    if reaches(guess):
        high = guess
        low = guess - step
        while low >= 0 and reaches(low):
            high = low
            step *= 2
            low = high - step
        low = max(low, -1)  # v = 0 gives 0 and n is above 0, so 0 never reaches
    else:
        low = guess
        high = guess + step
        while high < top and not reaches(high):
            low = high
            step *= 2
            high = low + step
        high = min(high, top)
    while high - low > 1:
        middle = (low + high) // 2
        if reaches(middle):
            high = middle
        else:
            low = middle
    return high


@lru_cache(maxsize=4096)
def _exponential_span(rate: Decimal, low: int, high: int) -> Optional[Tuple[int, int]]:
    """The least and the greatest ``uniform53`` whose exponential value lies in ``[low, high]``; ``None`` for none."""
    first = _least_reaching(rate, low)
    last = _least_reaching(rate, high + 1) - 1
    if first >= UNIFORM53 or last < first:
        return None
    return first, last


def _exponential_within(
    dwell: ExponentialDwell, u53: int, unit: int, low: int, high: int
) -> Tuple[bool, Optional[int]]:
    """``(fits, units)`` of an exponential cut to the dwell units *low* to *high* (``None`` units for no end).

    The share ``open`` is taken first, as :func:`draw_dwell` takes it. The rest of ``u53`` is then
    mapped onto the span of ``uniform53`` whose exponential value lies in the interval, exactly
    (:func:`_exponential_span`), in place of the whole ``[0, 2**53)``.
    """
    cut = (dwell.open.numerator * UNIFORM53) // dwell.open.denominator
    if u53 < cut:
        return True, None
    rate = _rate_of(dwell.mean, unit)
    span = _exponential_span(rate, low, high) if high >= low else None
    if span is None:
        return False, None
    first, last = span
    v = first + (u53 - cut) * (last - first + 1) // (UNIFORM53 - cut)
    return True, exponential_units(v, rate, 0)


def draw_dwell_within(
    dwell: Dwell, u53: int, unit: int, low: int, high: int
) -> Tuple[bool, Optional[int]]:
    """``(fits, units)``: *dwell* cut to the dwell units *low* to *high* from the start.

    *low* and *high* are the end column's declared range less the row's start, with *low* not
    below 0. A histogram is cut as an ``offset`` is, in one ``uniform53``; an exponential is cut
    exactly. ``fits`` is ``False`` when no value of the dwell lies inside, and no ``open`` share
    stands for a row with no end.
    """
    if isinstance(dwell, HistogramDwell):
        reach = dwell_span(dwell, unit)
        if reach is None:
            low, high = 1, 0  # no bin has a weight: only an ``open`` slot can stand
        else:
            assert reach[1] is not None
            # The cut depends on the interval only where it truncates a bin, so rows whose
            # intervals cover the whole histogram share one table.
            low = 0 if low <= reach[0] else low
            high = min(high, reach[1])
        found = _histogram_cut(dwell.histogram, low, high, unit)
        if found is None:
            return False, None
        return True, found.end(u53)
    return _exponential_within(dwell, u53, unit, low, high)


def draw_family_within(
    family: DwellFamily, state: Optional[str], u53: int, unit: int, low: int, high: int
) -> Tuple[bool, bool, Optional[int]]:
    """``(applies, fits, units)`` for a row in *state*: :func:`draw_family` cut to the dwell units *low* to *high*.

    ``applies`` is ``False`` when no member applies, as in :func:`draw_family`.
    """
    dwell = family.dwell_for(state)
    if dwell is None:
        return False, True, None
    fits, units = draw_dwell_within(dwell, u53, unit, low, high)
    return True, fits, units


def dwell_span(dwell: Dwell, unit: int) -> Optional[Tuple[int, Optional[int]]]:
    """The shortest and the longest end *dwell* can draw, in the end column's units; the longest is ``None`` if unbounded.

    A histogram's are those of its bins with a weight above zero (``None`` when it has none); an
    exponential's shortest is 0 and it has no longest.
    """
    if isinstance(dwell, HistogramDwell):
        table = offset_table(dwell.histogram)
        held = [days for weight, days in zip(table.weights, table.days, strict=True) if weight > 0]
        if not held:
            return None
        return (
            min(first for first, _ in held) * unit,
            (max(last for _, last in held) + 1) * unit - 1,
        )
    return 0, None


def family_span(family: DwellFamily, unit: int) -> Optional[Tuple[int, Optional[int]]]:
    """The shortest and the longest end any dwell of *family* can draw (:func:`dwell_span`), or ``None`` for no dwell."""
    members = [dwell for _, dwell in family.states]
    if family.otherwise is not None:
        members.append(family.otherwise)
    spans = [span for span in (dwell_span(member, unit) for member in members) if span is not None]
    if not spans:
        return None
    longest = (
        None
        if any(span[1] is None for span in spans)
        else max(span[1] for span in spans if span[1] is not None)
    )
    return min(span[0] for span in spans), longest
