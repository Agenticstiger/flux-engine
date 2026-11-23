# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A parent's number of children follows its exposure (ADR 0025 section 4).

A ``within`` entry with ``exposure: true`` makes the number of children each parent row has depend
on how long, and how busily, the parent is active inside the run's window. The expected count of a
parent is its **exposure** ``E`` in days: with ``[a, b]`` the parent's lifetime cut to the window
(``spec.timeBounds``), ``E`` is the integral of the child column's intensity ``lambda`` over
``[a, b]``, and ``lambda = 1`` where the column has no ``rates`` entry. The counted layout
(ADR 0016 section 3, :mod:`flux_engine.generator.counts`) then allots the child rows in proportion
to ``E``, or by its rank when the relationship has a ``children`` shape.

The lifetime is read as a ``within`` entry reads a parent's (ADR 0024 section 5). A NULL start has
no lifetime and exposure ``0``. An end that is NULL, or before the start, leaves the lifetime open,
and an open lifetime runs to the window's end. A lifetime wholly outside the window has exposure
``0``, and a parent with exposure ``0`` has no children.

``lambda`` is constant over each day of a DATE column and over each hour of a TIMESTAMP column, so
``E`` is exact. For a DATE column ``E`` is the sum of ``lambda(day)`` over the days ``a`` to ``b``.
For a TIMESTAMP column it is the sum of ``lambda(hour) * overlap_ms / 3_600_000`` over the hours
``[a, b]`` covers, divided by 24, so that ``E`` is in days: the last millisecond of ``b`` counts, as
a lifetime includes its last day (:func:`flux_engine.generator.dates.bound_in_units`). The weights
of the window are summed once for each window and set of rates (:func:`_table`, the last two kept),
as integers over one denominator, and a parent then costs two lookups.

The hook draws nothing and moves no position. Its state is the declaration :meth:`ExposureHook.member`
reads at plan time, kept on the hook object. The counted layout carries the hook that bound the
member inside the plan (``CountedPick.hook``), and a plan is pickled whole to each worker process, so
the declaration is present wherever ``expected`` runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from math import lcm
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError
from flux_engine.generator.counts import register_expected_count_hook
from flux_engine.generator.dates import (
    DAY_MS,
    KIND_DATE,
    days_of_date,
    lifetime_in_units,
    window_in_units,
)
from flux_engine.generator.rates import Rates, civil_month, weekday_of

if TYPE_CHECKING:
    from flux_engine.generator.rows import GeneratorPlan

__all__ = ["EXPOSURE_HOOK", "ExposureHook", "exposure_of"]

_HOUR_MS = 3_600_000
#: The most days (DATE) or hours (TIMESTAMP) a window may hold for its weights to be summed.
_MOST_CELLS = 1_000_000
#: The most declarations kept at plan time (the oldest are dropped first).
_MOST_SPECS = 1024


def _refuse(message: str) -> FluxError:
    """``FLX-3403`` (imported here: relations imports the counted layout)."""
    from flux_engine.generator import relations

    return FluxError(relations.FLX_3403, message)


def _scaled(
    factor: Optional[Tuple[Fraction, ...]],
) -> Tuple[int, Optional[Tuple[int, ...]]]:
    """A factor's weights as integers over one denominator, and that denominator."""
    if factor is None:
        return 1, None
    scale = lcm(*(weight.denominator for weight in factor))
    return scale, tuple(int(weight * scale) for weight in factor)


@dataclass(frozen=True)
class _Table:
    """The window's weights, summed: ``prefix[i]`` is the weight of the first ``i`` cells.

    A cell is a day (DATE) or an hour (TIMESTAMP), ``first`` the epoch day or epoch hour of cell 0,
    and every weight is ``lambda`` times ``scale``.
    """

    first: int
    scale: int
    prefix: Tuple[int, ...]


def _day_weight(day: int, month: Optional[Tuple[int, ...]], week: Optional[Tuple[int, ...]]) -> int:
    """The month and weekday weights of one civil day, multiplied (``1`` for a factor not given)."""
    return (1 if month is None else month[civil_month(day) - 1]) * (
        1 if week is None else week[weekday_of(day)]
    )


def _date_weights(
    low: int, high: int, month: Optional[Tuple[int, ...]], week: Optional[Tuple[int, ...]]
) -> List[int]:
    """The weight of each day ``low`` to ``high``; more than :data:`_MOST_CELLS` days is ``FLX-3403``."""
    if high - low + 1 > _MOST_CELLS:
        raise _refuse(f"the window holds {high - low + 1} days, more than {_MOST_CELLS}")
    return [_day_weight(day, month, week) for day in range(low, high + 1)]


def _hour_weights(
    first: int,
    last: int,
    month: Optional[Tuple[int, ...]],
    week: Optional[Tuple[int, ...]],
    hours: Optional[Tuple[int, ...]],
) -> List[int]:
    """The weight of each hour ``first`` to ``last``; more than :data:`_MOST_CELLS` hours is ``FLX-3403``."""
    if last - first + 1 > _MOST_CELLS:
        raise _refuse(f"the window holds {last - first + 1} hours, more than {_MOST_CELLS}")
    weights: List[int] = []
    for day in range(first // 24, last // 24 + 1):
        base = _day_weight(day, month, week)
        week_hour = weekday_of(day) * 24
        for hour in range(max(first - day * 24, 0), min(last - day * 24, 23) + 1):
            weights.append(base * (1 if hours is None else hours[week_hour + hour]))
    return weights


@lru_cache(maxsize=2)
def _table(kind: str, low: int, high: int, rates: Rates) -> _Table:
    """The prefix sums of ``lambda`` over the window ``[low, high]`` of *rates*' column, once per window and rates.

    The last two tables are kept: a table holds a weight per cell, so the cache is bounded in
    tables as well as :data:`_MOST_CELLS` bounds each one.
    """
    held = rates.intensity
    month_scale, month = _scaled(held.month)
    week_scale, week = _scaled(held.day_of_week)
    hour_scale, hours = _scaled(held.hour_of_week)
    scale = month_scale * week_scale * (hour_scale if kind != KIND_DATE else 1)
    if kind == KIND_DATE:
        first, weights = low, _date_weights(low, high, month, week)
    else:
        first = low // _HOUR_MS
        weights = _hour_weights(first, high // _HOUR_MS, month, week, hours)
    prefix = [0]
    for weight in weights:
        prefix.append(prefix[-1] + weight)
    return _Table(first=first, scale=scale, prefix=tuple(prefix))


def _integral(
    kind: str, a: int, b: int, window: Tuple[int, int], rates: Optional[Rates]
) -> Fraction:
    """The integral of ``lambda`` over ``[a, b]``, both inside *window*, in days, exactly."""
    if kind == KIND_DATE:
        if rates is None:
            return Fraction(b - a + 1)
        table = _table(kind, window[0], window[1], rates)
        return Fraction(
            table.prefix[b - table.first + 1] - table.prefix[a - table.first], table.scale
        )
    if rates is None:
        return Fraction(b - a + 1, DAY_MS)
    table = _table(kind, window[0], window[1], rates)
    first_hour, last_hour = a // _HOUR_MS, b // _HOUR_MS

    def weight(hour: int) -> int:
        return table.prefix[hour - table.first + 1] - table.prefix[hour - table.first]

    if first_hour == last_hour:
        total = weight(first_hour) * (b - a + 1)
    else:
        total = (
            weight(first_hour) * ((first_hour + 1) * _HOUR_MS - a)
            + (table.prefix[last_hour - table.first] - table.prefix[first_hour + 1 - table.first])
            * _HOUR_MS
            + weight(last_hour) * (b - last_hour * _HOUR_MS + 1)
        )
    return Fraction(total, table.scale * DAY_MS)


def exposure_of(
    kind: str,
    parent_kind: str,
    start: Optional[int],
    end: Optional[int],
    bounds: Tuple[int, int],
    rates: Optional[Rates] = None,
) -> Fraction:
    """The exposure ``E`` in days of a parent whose lifetime is ``start`` to ``end``, exact.

    *kind* is the grain of the child column (``DATE`` or ``TIMESTAMP``), *parent_kind* the grain of
    the parent's lifetime columns, *start* and *end* the lifetime in epoch days or epoch
    milliseconds as *parent_kind* holds them (``None`` for a NULL), *bounds* the run's
    ``spec.timeBounds`` in epoch milliseconds, and *rates* the child column's seasonal rates.
    """
    lifetime = lifetime_in_units(start, end, parent_kind, kind)
    window = window_in_units(kind, bounds)
    if lifetime is None or window is None:
        return Fraction(0)
    low, high = lifetime
    a = max(low, window[0])
    b = window[1] if high is None else min(high, window[1])
    if a > b:
        return Fraction(0)
    return _integral(kind, a, b, window, rates)


@dataclass(frozen=True)
class _Spec:
    """One ``within`` entry's exposure, as the plan holds it."""

    child: str
    relationship: str
    column: str
    kind: str
    start: str
    end: Optional[str]
    parent_kind: str
    rates: Optional[Rates]

    def as_member(self) -> Dict[str, Any]:
        """What binds into the child plan's digest: the entry's declaration and what it reads."""
        member: Dict[str, Any] = {
            "by": "exposure",
            "relationship": self.relationship,
            "column": self.column,
            "start": self.start,
            "end": self.end,
        }
        if self.rates is not None:
            member["rates"] = self.rates.as_document()
        return member


def _epoch(value: Any, kind: str) -> int:
    """A cell of a parent's final row as epoch days (``DATE``) or epoch milliseconds (``TIMESTAMP``)."""
    return days_of_date(value) if kind == KIND_DATE else int(value)


def _entries(namespace: Mapping[str, Any], emit: str) -> Sequence[Mapping[str, Any]]:
    raw = namespace.get("within")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return [
        item
        for item in raw
        if isinstance(item, Mapping)
        and item.get("exposure") is True
        and nfc(str(item.get("emit", ""))) == emit
    ]


class ExposureHook:
    """The expected-count hook of ``within`` entries with ``exposure: true`` (ADR 0025 section 4).

    A declaration is kept under ``(sim id, relationship, parent expose id)``, the three things
    :meth:`expected` knows of a parent. Each :meth:`member_to` call overwrites its key, so the plan
    made last is the one the process counts by, and at most :data:`_MOST_SPECS` are kept, the oldest
    dropped. Two declarations of one plan under one key could not be told apart by ``expected``, so
    the plan refuses them (:func:`flux_engine.generator.relations.plan_relations`, ``FLX-3403``).
    """

    def __init__(self) -> None:
        self._specs: Dict[Tuple[str, str, str], _Spec] = {}
        self._found: Dict[Tuple[str, str, str], Tuple[_Spec, int, Optional[int]]] = {}

    def __getstate__(self) -> Dict[str, Any]:
        return {"_specs": self._specs}

    def __setstate__(self, state: Mapping[str, Any]) -> None:
        self._specs = dict(state["_specs"])
        self._found = {}

    def member(
        self, namespace: Mapping[str, Any], child: "GeneratorPlan", relationship: str
    ) -> Optional[Mapping[str, Any]]:
        """:meth:`member_to` for a relationship whose target is not known: the declaration is kept under no parent."""
        return self.member_to(namespace, child, relationship, "")

    def member_to(
        self,
        namespace: Mapping[str, Any],
        child: "GeneratorPlan",
        relationship: str,
        parent: str,
    ) -> Optional[Mapping[str, Any]]:
        """The member of *child*'s ``relationship`` to the expose *parent* when a ``within`` entry over it carries ``exposure: true``, else ``None``.

        The entry's date column is found among the child's planned levels, whose parent lifetime
        columns and grains the plan holds. Without ``spec.timeBounds`` there is no window to measure
        in: ``FLX-3105``. A level the plan does not hold is ``FLX-3403``.
        """
        emit = nfc(child.expose.expose_id)
        entry = next(
            (
                item
                for item in _entries(namespace, emit)
                if str(item.get("relationship")) == relationship
            ),
            None,
        )
        if entry is None:
            return None
        if child.time_bounds is None:
            from flux_engine.seam.resolve import FLX_3105

            raise FluxError(
                FLX_3105,
                f"the within entry of {emit!r} through {relationship!r} declares exposure, which "
                "is measured inside the run's window, and the Simulation declares no spec.timeBounds",
            )
        lifetime = entry.get("lifetime") is True
        date = None if lifetime else str(entry.get("date"))
        levels = [
            rule.within
            for rule in (child.rules.rules if child.rules is not None else ())
            if rule.within is not None
            and rule.within.relationship == (relationship,)
            and (rule.within.period if lifetime else rule.within.column == date)
        ]
        if not levels:
            raise _refuse(
                f"the within entry of {emit!r} through {relationship!r} declares exposure, and the "
                "plan holds no such entry for its date"
            )
        level = levels[0]
        rates = None
        if child.rules is not None:
            rates = next((item for item in child.rules.rates if item.column == level.column), None)
        spec = _Spec(
            child=emit,
            relationship=relationship,
            column=level.column,
            kind=level.kind,
            start=level.start,
            end=level.end,
            parent_kind=level.parent_kind,
            rates=rates,
        )
        self._keep((child.sim_id, relationship, parent), spec)
        return spec.as_member()

    def _keep(self, key: Tuple[str, str, str], spec: _Spec) -> None:
        """Hold *spec* under *key*, overwriting what the key held, and drop the oldest beyond :data:`_MOST_SPECS`."""
        self._specs.pop(key, None)
        self._specs[key] = spec
        while len(self._specs) > _MOST_SPECS:
            del self._specs[next(iter(self._specs))]
        self._found.clear()

    def _spec_for(
        self, parent: "GeneratorPlan", relationship: str
    ) -> Tuple[_Spec, int, Optional[int]]:
        """The declaration that reads *parent*'s lifetime columns for *relationship*, and their positions in its rows."""
        key = (parent.sim_id, relationship, parent.expose.expose_id)
        found = self._found.get(key)
        if found is not None:
            return found
        spec = self._specs.get(key) or self._specs.get((parent.sim_id, relationship, ""))
        names = [plan.column.name for plan in parent.columns]
        if (
            spec is None
            or spec.start not in names
            or (spec.end is not None and spec.end not in names)
        ):
            raise _refuse(
                f"relationship {relationship!r} counts the children of "
                f"{parent.expose.expose_id!r} by exposure, and no exposure declaration reads its "
                "lifetime columns"
            )
        found = (
            spec,
            names.index(spec.start),
            None if spec.end is None else names.index(spec.end),
        )
        self._found[key] = found
        return found

    def expected(
        self, parent: "GeneratorPlan", row: int, final_row: Tuple[Any, ...], relationship: str
    ) -> Fraction:
        """The exposure ``E`` of parent row *row* in days (:func:`exposure_of`), from its final row."""
        spec, start_at, end_at = self._spec_for(parent, relationship)
        if parent.time_bounds is None:
            raise _refuse(
                f"relationship {relationship!r} counts by exposure, and the plan of "
                f"{parent.expose.expose_id!r} has no window"
            )
        start = final_row[start_at]
        end = None if end_at is None else final_row[end_at]
        return exposure_of(
            spec.kind,
            spec.parent_kind,
            None if start is None else _epoch(start, spec.parent_kind),
            None if end is None else _epoch(end, spec.parent_kind),
            parent.time_bounds,
            spec.rates,
        )


EXPOSURE_HOOK = ExposureHook()
register_expected_count_hook(EXPOSURE_HOOK)
