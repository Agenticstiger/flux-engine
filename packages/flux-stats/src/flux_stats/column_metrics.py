# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The column metrics a profile carries, named as OpenLineage names them.

OpenLineage's ``DataQualityMetricsInputDatasetFacet`` (facet schema 1-0-0)
keeps, per column, ``nullCount``, ``distinctCount``, ``sum``, ``count``,
``min``, ``max`` and ``quantiles``. A profile borrows those seven names, one
registered definition each (``count/1`` ... ``quantiles/1``), and not their
values: OpenLineage writes them as JSON numbers, which are binary floating
point to most readers. Here a count is a JSON integer and every other value
is a decimal string (:mod:`flux_stats.spelling`).

A metric is measured over a column's values as the fidelity check reads
them: an exact decimal for a number column, integer milliseconds since the
epoch for a timestamp column, an NFC string for a categorical one, and
``None`` for a null. ``sum`` applies to numbers only; ``min``, ``max`` and
``quantiles`` to numbers and timestamps; the three counts to every column.
A quantile is the order statistic the fidelity check takes, index
``ceil(level * n)`` among the non-null values (``order_statistic_index``), so
a profile's median and a check's median are one number.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, FrozenSet, List, Mapping, Sequence, Tuple

from flux_stats.fixedpoint import order_statistic_index
from flux_stats.registry import MetricDefinition, register_metric
from flux_stats.spelling import read_count, read_decimal, refuse, render_exact

__all__ = [
    "CATEGORY",
    "COUNT",
    "DISTINCT_COUNT",
    "DOMAINS",
    "MAX",
    "MIN",
    "NULL_COUNT",
    "NUMBER",
    "QUANTILES",
    "SUM",
    "TIMESTAMP",
]

#: How a column's values are read, and so which metrics apply to it.
NUMBER = "number"
TIMESTAMP = "timestamp"
CATEGORY = "category"
DOMAINS: Tuple[str, ...] = (CATEGORY, NUMBER, TIMESTAMP)

_EVERY = frozenset(DOMAINS)
_ORDERED = frozenset({NUMBER, TIMESTAMP})


def _present(values: Sequence[Any]) -> List[Any]:
    return [value for value in values if value is not None]


def _decimal(value: Fraction) -> str:
    spelled = render_exact(value)
    if "/" in spelled:
        raise ValueError(f"{spelled} is not a terminating decimal")
    return spelled


def _spell_count(value: Any) -> Dict[str, Any]:
    return {"value": value}


def _read_count_value(document: Mapping[str, Any], where: str) -> Any:
    return read_count(document.get("value"), f"{where}.value")


def _spell_decimal(value: Any) -> Dict[str, Any]:
    return {"value": _decimal(value)}


def _read_decimal_value(document: Mapping[str, Any], where: str) -> Any:
    return read_decimal(document.get("value"), f"{where}.value")


def _measure_count(values: Sequence[Any], levels: Tuple[Fraction, ...]) -> Any:
    return len(_present(values))


def _measure_null_count(values: Sequence[Any], levels: Tuple[Fraction, ...]) -> Any:
    return sum(1 for value in values if value is None)


def _measure_distinct_count(values: Sequence[Any], levels: Tuple[Fraction, ...]) -> Any:
    return len(set(_present(values)))


def _measure_sum(values: Sequence[Any], levels: Tuple[Fraction, ...]) -> Any:
    return sum(_present(values), Fraction(0))


def _measure_min(values: Sequence[Any], levels: Tuple[Fraction, ...]) -> Any:
    present = _present(values)
    return min(present) if present else None


def _measure_max(values: Sequence[Any], levels: Tuple[Fraction, ...]) -> Any:
    present = _present(values)
    return max(present) if present else None


def _measure_quantiles(values: Sequence[Any], levels: Tuple[Fraction, ...]) -> Any:
    ordered = sorted(_present(values))
    if not ordered:
        return {}
    return {level: ordered[order_statistic_index(len(ordered), level) - 1] for level in levels}


def _spell_quantiles(value: Any) -> Dict[str, Any]:
    return {"values": {_decimal(level): _decimal(found) for level, found in sorted(value.items())}}


def _read_quantiles(document: Mapping[str, Any], where: str) -> Any:
    raw = document.get("values")
    if not isinstance(raw, Mapping) or not raw:
        raise refuse(f"{where}.values", "quantiles is a non-empty mapping of level to value")
    found: Dict[Fraction, Fraction] = {}
    for key in sorted(raw):
        level = read_decimal(key, f"{where}.values key")
        if key.startswith("-") or not 0 <= level <= 1:
            raise refuse(f"{where}.values", "a quantile level lies in [0, 1], written with no sign")
        if level in found:
            raise refuse(f"{where}.values", "names one quantile level twice")
        found[level] = read_decimal(raw[key], f"{where}.values[{key!r}]")
    return found


def _metric(
    name: str,
    summary: str,
    domains: FrozenSet[str],
    measure: Any,
    spell: Any,
    read: Any,
) -> MetricDefinition:
    return register_metric(
        MetricDefinition(
            identifier=f"{name}/1",
            name=name,
            summary=summary,
            domains=domains,
            measure=measure,
            spell=spell,
            read=read,
        )
    )


COUNT = _metric(
    "count",
    "the number of non-null values",
    _EVERY,
    _measure_count,
    _spell_count,
    _read_count_value,
)
NULL_COUNT = _metric(
    "nullCount",
    "the number of null values",
    _EVERY,
    _measure_null_count,
    _spell_count,
    _read_count_value,
)
DISTINCT_COUNT = _metric(
    "distinctCount",
    "the number of distinct non-null values",
    _EVERY,
    _measure_distinct_count,
    _spell_count,
    _read_count_value,
)
SUM = _metric(
    "sum",
    "the exact sum of the non-null values",
    frozenset({NUMBER}),
    _measure_sum,
    _spell_decimal,
    _read_decimal_value,
)
MIN = _metric(
    "min",
    "the smallest non-null value",
    _ORDERED,
    _measure_min,
    _spell_decimal,
    _read_decimal_value,
)
MAX = _metric(
    "max",
    "the largest non-null value",
    _ORDERED,
    _measure_max,
    _spell_decimal,
    _read_decimal_value,
)
QUANTILES = _metric(
    "quantiles",
    "the order statistic at each declared level, ceil(level * n) among the non-null values",
    _ORDERED,
    _measure_quantiles,
    _spell_quantiles,
    _read_quantiles,
)
