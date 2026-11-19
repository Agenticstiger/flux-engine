# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How one column of a profiled expose is declared, and what each of its numbers rests on.

The rules run from the most the profile says to the least. A statistic the
profile publishes for the column comes first (a histogram over bins, the counts
per level, the members of a categorical column). Then the column metrics:
``min``, the quartiles and ``max`` become histogram bins, ``distinctCount``
the number of levels of a categorical column. Then what the contract fixes: an
``enum`` type's levels, a ``required`` column's nulls, a key. Whatever is left
is an assumption, stated with its reason.

**Only published and contract facts are declared where the check reads them.**
A column's ``range`` and ``support`` are what the fidelity check holds a run and
a committed slice to, and a value outside them is refused (SEAM-FIDELITY-SPEC
§5). So they are declared only from a published ``min`` and ``max``, a
published statistic, or the contract. A shape that rests on an assumption is
declared with the keys only the generator reads (``bins`` and ``binWeights``,
``levels``), so the check never refuses data for disagreeing with a guess. The
one exception is a table none of whose columns has such a fact: a fidelity
block needs one property, so one column's assumed span or levels is declared
for the check to read, and said to be an assumption (:func:`assumed_check`).

A count withheld as a small one (status ``A``) lies below the disclosure rule's
minimum cell, so it is assumed to be half of it, the midpoint reading users of
suppressed tables take (Cox, "Disclosure risk for tabular economic data", FCSM
2009). Any other status (another primary suppression, or a secondary one)
says nothing of a count's size, so a cell withheld under one is assumed to be
the mean of the cells its table publishes. Like every assumption here, each is
written down with its reason.

The numbers are the ones the generator draws from: a bin is half-open and drawn
evenly over its grid points, so a histogram's mean is worked out on the
column's grid (:func:`shape_to_mean`), and an integer a run's rows cannot carry
exactly (beyond 2^53, ``FLX-1003``) is never declared.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from itertools import pairwise
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.author.basis import Basis, Label, assumed, from_contract, from_profile
from flux_engine.core.canonical import JCS_MAX_SAFE_INTEGER
from flux_engine.core.clock import format_rfc3339_ms
from flux_engine.seam.contract import CompiledColumn, LogicalKind
from flux_engine.seam.coverage import printable
from flux_stats.column_metrics import MAX, MIN, NULL_COUNT, QUANTILES
from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM, MEAN
from flux_stats.definitions import SUPPORT as SUPPORT_STATISTIC

__all__ = [
    "DEFAULT_TOLERANCES",
    "MOST_PLACES",
    "OPAQUE_LEVEL_CAP",
    "ColumnContext",
    "ColumnPlan",
    "PropertyPlan",
    "assumed_check",
    "declared_span",
    "draw_within_key",
    "drawn_span",
    "plan_column",
    "shape_to_mean",
    "share_texts",
    "widen_key_span",
]

#: The registered identifiers the rules read, named where flux-stats defines them.
_HISTOGRAM = MARGINAL_HISTOGRAM.identifier
_LEVELS = LEVEL_HISTOGRAM.identifier
_SUPPORT = SUPPORT_STATISTIC.identifier
_MEAN = MEAN.identifier
_MIN = MIN.identifier
_MAX = MAX.identifier
_QUANTILES = QUANTILES.identifier
_NULL_COUNT = NULL_COUNT.identifier

#: How many opaque levels a string column whose values the profile does not
#: publish may draw, at most.
OPAQUE_LEVEL_CAP = 1000

#: The tolerance each property is written with. Every one is an assumption the
#: author is asked to replace with the margin they accept.
DEFAULT_TOLERANCES: Mapping[str, str] = {
    "mean": "0.10",
    "distance": "0.10",
    "quantile": "0.10",
}

#: Why a tolerance holds: one reason, shared by every property this module writes.
TOLERANCE_REASON = (
    "A default tolerance written by fluxsim init: replace it with the margin you accept."
)

#: Places a share is written to when it is not an exact decimal.
_SHARE_PLACES = 9

_DAY_MS = 86_400_000
_FREE = "F"
#: SDMX ``CL_CONF_STATUS``'s primary suppression for small counts (PROFILE-SPEC section 3):
#: the one status that bounds a withheld count, below the minimum cell.
_SMALL = "A"


@dataclass(frozen=True)
class ColumnContext:
    """Everything one column is declared from."""

    column: CompiledColumn
    #: The column's place in the contract: ``exposes[expose_index].contract.schema[schema_index]``.
    expose_index: int
    schema_index: int
    product_ref: str
    #: Where the profile describes the column: ``/tables/<exposeId>/columns/<name>``.
    profile_tokens: Tuple[str, ...]
    #: The profile's entry for the column, as published, or ``None``.
    entry: Optional[Mapping[str, Any]]
    rows: int
    #: The disclosure rule's minimum cell, for counts the profile withheld.
    minimum_cell: int
    #: The contract makes the column a whole key of its expose on its own.
    key: bool = False
    #: The expose a relationship of the contract makes this column refer to, if any.
    foreign: Optional[str] = None
    #: The column is part of a key the contract declares (on its own, with others, or as
    #: a foreign key that references each parent row at most once): it holds a value on
    #: every row, and a key layout reads only its range or bins.
    keyed: bool = False
    #: The column is a foreign key of an ``inner`` relationship: every row references a
    #: parent, so it holds no null.
    inner: bool = False
    #: The column is a later column of a composite foreign key: the first column's nulls
    #: decide the relationship's, so it declares none of its own.
    nulls_follow: bool = False
    #: The grid the profile's encoded statistics are on (its ``encoding.fractionalBits``).
    fractional_bits: int = 16

    @property
    def contract_tokens(self) -> Tuple[str, ...]:
        return (
            "exposes",
            str(self.expose_index),
            "contract",
            "schema",
            str(self.schema_index),
        )


@dataclass
class PropertyPlan:
    """A property a column can carry, and whether the profile can score it."""

    document: Dict[str, Any]
    labels: List[Label]
    #: ``True`` when the profile holds the statistic the property compares with.
    scorable: bool
    #: The statistics of the profile the property is scored against, each with how: the
    #: ledger marks them used when the block carries the property (ADR 0014).
    reads: List[Tuple[Tuple[str, ...], str]] = field(default_factory=list)


@dataclass
class ColumnPlan:
    """One column's declaration, the bases of its numbers, and how it reads to a person."""

    name: str
    declaration: Dict[str, Any] = field(default_factory=dict)
    #: Positions below ``fidelity/columns/<name>``.
    labels: List[Label] = field(default_factory=list)
    #: One line: how the column is drawn, and from what.
    how: str = ""
    #: Each assumption this column makes, as a line a person reads.
    assumptions: List[str] = field(default_factory=list)
    properties: List[PropertyPlan] = field(default_factory=list)
    #: The bounds the check reads, when the column declares a published range.
    published_range: Optional[Tuple[Decimal, Decimal]] = None
    #: The column's time span in milliseconds, for a date or timestamp column with a range.
    time_span: Optional[Tuple[int, int]] = None
    #: The declaration is a placeholder: the profile published nothing it could rest on.
    placeholder: bool = False
    #: A number key whose span is assumed, so the foreign keys that reference it may
    #: widen it (:func:`widen_key_span`).
    key_span: bool = False
    #: Statistics of the profile the rules read without citing them, each with how they
    #: shaped, bounded or checked the declaration (the ledger marks them used).
    reads: List[Tuple[Tuple[str, ...], str]] = field(default_factory=list)
    #: Statistics the rules looked at and passed over, each with why (ADR 0014).
    passed: List[Tuple[Tuple[str, ...], str]] = field(default_factory=list)
    #: The assumptions that are choices rather than gaps: how to meet what the profile
    #: does publish, or where the contract overrules it. ``--strict`` refuses every other
    #: assumption (ADR 0014 section 2).
    choices: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading the profile entry
# ---------------------------------------------------------------------------


def _metric(entry: Optional[Mapping[str, Any]], name: str) -> Optional[Mapping[str, Any]]:
    metrics = entry.get("metrics") if isinstance(entry, Mapping) else None
    found = metrics.get(name) if isinstance(metrics, Mapping) else None
    return found if isinstance(found, Mapping) else None


def _published(entry: Optional[Mapping[str, Any]], name: str) -> Any:
    """A metric's value when the profile published it, else ``None``."""
    metric = _metric(entry, name)
    if metric is None or metric.get("status") != _FREE:
        return None
    return metric.get("values") if name == "quantiles" else metric.get("value")


def _withheld_note(entry: Optional[Mapping[str, Any]], name: str) -> str:
    """How a metric reads when it is missing: withheld with its code, or not in the profile."""
    metric = _metric(entry, name)
    if metric is None:
        return f"The profile has no {name}"
    return f"The profile withheld {name} (status {printable(str(metric.get('status')))})"


def _withheld_small(entry: Optional[Mapping[str, Any]], name: str) -> bool:
    """Whether a metric was withheld as a small count, the one status that bounds it."""
    metric = _metric(entry, name)
    return metric is not None and metric.get("status") == _SMALL


def _metric_tokens(ctx: "ColumnContext", name: str) -> Tuple[str, ...]:
    return ctx.profile_tokens + ("metrics", name)


def _read(ctx: "ColumnContext", plan: "ColumnPlan", name: str, how: str) -> None:
    """Record that the rules read the column's metric *name* when it is published."""
    if _published(ctx.entry, name) is not None:
        plan.reads.append((_metric_tokens(ctx, name), how))


def _statistics(entry: Optional[Mapping[str, Any]]) -> List[Tuple[int, Mapping[str, Any]]]:
    found = entry.get("statistics") if isinstance(entry, Mapping) else None
    if not isinstance(found, list):
        return []
    return [(index, item) for index, item in enumerate(found) if isinstance(item, Mapping)]


def _ungrouped(
    entry: Optional[Mapping[str, Any]], definition: str
) -> Optional[Tuple[int, Mapping[str, Any], Mapping[str, Any]]]:
    """The first published, ungrouped statistic of *definition*: its index, itself, its group."""
    for index, statistic in _statistics(entry):
        if statistic.get("definition") != definition or "groupBy" in statistic:
            continue
        groups = statistic.get("groups")
        if not isinstance(groups, list) or len(groups) != 1:
            continue
        group = groups[0]
        if not isinstance(group, Mapping) or group.get("status") != _FREE:
            continue
        key = _COUNT_TABLES.get(definition)
        if key is not None and not _has_mass(group, key):
            continue
        return index, statistic, group
    return None


#: The count table each histogram definition publishes per group.
_COUNT_TABLES: Mapping[str, str] = {
    _HISTOGRAM: "binCounts",
    _LEVELS: "levelCounts",
}


def _has_mass(group: Mapping[str, Any], key: str) -> bool:
    """Whether a count table can weigh a draw: a published count above zero, or a withheld one.

    A table whose every published count is zero, and which withholds none, gives
    no weights to draw with, so it is passed over as if it were not published.
    """
    return any(cell.count is None or cell.count > 0 for cell in _cells(group, key))


@dataclass(frozen=True)
class _Cell:
    """One cell of a count table: its count when published, else ``None``, and its status."""

    count: Optional[int]
    status: str


def _cells(group: Mapping[str, Any], key: str) -> List[_Cell]:
    """A count table's cells, each with its count when it was published."""
    counts = group.get(key)
    statuses = group.get("cellStatus")
    cells: List[_Cell] = []
    for position, count in enumerate(counts if isinstance(counts, list) else ()):
        status = (
            statuses[position] if isinstance(statuses, list) and position < len(statuses) else _FREE
        )
        published = status == _FREE and isinstance(count, int) and not isinstance(count, bool)
        cells.append(_Cell(count if published else None, str(status)))
    return cells


# ---------------------------------------------------------------------------
# Spelling numbers
# ---------------------------------------------------------------------------


def _decimal_text(value: Fraction) -> str:
    """An exact decimal spelling of *value*, which must be one."""
    text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _share_text(value: Fraction) -> str:
    """*value* exactly when it is a finite decimal, else rounded half-even to nine places."""
    denominator = value.denominator
    while denominator % 2 == 0:
        denominator //= 2
    while denominator % 5 == 0:
        denominator //= 5
    if denominator == 1:
        return _decimal_text(value)
    rounded = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
        Decimal(1).scaleb(-_SHARE_PLACES), rounding=ROUND_HALF_EVEN
    )
    return _decimal_text(Fraction(rounded))


def share_texts(weights: Sequence[Fraction]) -> List[str]:
    """Weights as shares that add up to exactly one, as decimal text.

    Each share is exact when it can be, and rounded to nine places when it
    cannot; what the rounding leaves over goes to the largest share (the first
    of equals), so the declared levels add up to one as the generator requires.
    """
    total = sum(weights, Fraction(0))
    if total <= 0:
        raise ValueError("weights that sum to zero have no shares")
    texts = [_share_text(weight / total) for weight in weights]
    residue = Fraction(1) - sum((Fraction(Decimal(text)) for text in texts), Fraction(0))
    if residue:
        largest = max(range(len(weights)), key=lambda position: (weights[position], -position))
        texts[largest] = _decimal_text(Fraction(Decimal(texts[largest])) + residue)
    return texts


def _spell(kind: LogicalKind, value: Decimal) -> Any:
    """A bound or a bin edge as the fidelity block reads it for a column of *kind*.

    A timestamp is RFC 3339 text. A date is whole days since the epoch: the
    generator reads a DATE bound as ``YYYY-MM-DD`` or integer days and the check
    as a decimal or an RFC 3339 instant, and an integer is the one spelling both
    read. A number keeps its exact decimal text.
    """
    if kind is LogicalKind.TIMESTAMP:
        return format_rfc3339_ms(int(value))
    if kind is LogicalKind.DATE:
        return int(value) // _DAY_MS
    return format(value, "f")


def _temporal(kind: LogicalKind) -> bool:
    return kind in (LogicalKind.DATE, LogicalKind.TIMESTAMP)


# ---------------------------------------------------------------------------
# The column, by kind
# ---------------------------------------------------------------------------


def plan_column(ctx: ColumnContext) -> ColumnPlan:
    """Declare one column from its profile entry and its contract declaration."""
    kind = ctx.column.logical_type.kind
    plan = ColumnPlan(name=ctx.column.name)
    if ctx.key and kind is LogicalKind.STRING:
        plan.how = "an identifier: the contract makes it a key, and the engine derives it"
        return plan
    if kind is LogicalKind.STRING:
        _categorical(ctx, plan)
    elif kind is LogicalKind.BOOL:
        _boolean(ctx, plan)
    else:
        _ordered(ctx, plan)
    if "unique" not in plan.declaration and not (ctx.keyed or ctx.inner or ctx.nulls_follow):
        _null_fraction(ctx, plan)
    return plan


def _null_fraction(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """``nullFraction``: the published null count, or none assumed.

    A column the contract makes ``required`` declares none: it can hold no
    null, and the seam refuses a ``nullFraction`` on it (``FLX-3402``). If the
    profile counts nulls there, the contract and the data disagree, and the
    contract wins.
    """
    nulls = _published(ctx.entry, "nullCount")
    count = _published(ctx.entry, "count")
    tokens = ("nullFraction",)
    if ctx.column.required:
        if isinstance(nulls, int) and not isinstance(nulls, bool) and nulls > 0:
            plan.assumptions.append(
                "The profile counts nulls, and the contract makes the column required, so it "
                "is drawn with none."
            )
            plan.choices.append(plan.assumptions[-1])
            plan.passed.append(
                (
                    _metric_tokens(ctx, "nullCount"),
                    "the contract makes the column required, so it is drawn with no null",
                )
            )
        elif isinstance(nulls, int) and not isinstance(nulls, bool):
            plan.reads.append(
                (_metric_tokens(ctx, "nullCount"), "checked: a required column holds no null")
            )
        if isinstance(count, int) and not isinstance(count, bool) and count == ctx.rows:
            plan.reads.append(
                (_metric_tokens(ctx, "count"), "checked: a required column holds a value per row")
            )
        return
    if isinstance(nulls, int) and not isinstance(nulls, bool):
        total = nulls + count if isinstance(count, int) else ctx.rows
        if isinstance(count, int):
            _read(ctx, plan, "count", "with nullCount, the share of rows that hold a null")
        share = min(Fraction(nulls, total), Fraction(1)) if total else Fraction(0)
        plan.declaration["nullFraction"] = _share_text(share)
        plan.labels.append(
            Label(tokens, from_profile(ctx.profile_tokens + ("metrics", "nullCount"), _NULL_COUNT))
        )
        return
    plan.declaration["nullFraction"] = "0"
    if _metric(ctx.entry, "nullCount") is None:
        reason = "The profile has no nullCount, so no nulls are drawn."
    elif _withheld_small(ctx.entry, "nullCount"):
        reason = (
            f"{_withheld_note(ctx.entry, 'nullCount')}: a count withheld as small is zero or "
            "below the minimum cell, so no nulls are drawn."
        )
    else:
        reason = (
            f"{_withheld_note(ctx.entry, 'nullCount')}, which does not say how many: no nulls "
            "are drawn."
        )
    plan.labels.append(Label(tokens, assumed(reason)))
    plan.assumptions.append(reason)


# --- categorical columns ----------------------------------------------------


def _categorical(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """A STRING column: declared levels, a published statistic, an identifier, or opaque levels."""
    enum = ctx.column.enum_values
    levels_stat = _ungrouped(ctx.entry, _LEVELS)
    support_stat = _ungrouped(ctx.entry, _SUPPORT)
    if enum:
        plan.declaration["support"] = "declared"
        plan.declaration["maxCardinality"] = len(enum)
        plan.labels.append(
            Label(
                ("maxCardinality",),
                from_contract(ctx.product_ref, ctx.contract_tokens + ("type",)),
            )
        )
        if levels_stat is not None and sorted(levels_stat[1].get("levels", ())) == sorted(enum):
            _level_weights(ctx, plan, levels_stat)
            plan.how = f"{len(enum)} levels the contract declares, weighted by the profile's counts"
        else:
            plan.how = f"{len(enum)} levels the contract declares, at equal weights"
            plan.assumptions.append(
                f"The profile publishes no counts for the {len(enum)} levels the contract "
                "declares, so they are drawn at equal weights."
            )
            if levels_stat is not None:
                plan.passed.append(
                    (
                        ctx.profile_tokens + ("statistics", str(levels_stat[0])),
                        "its levels are not the levels the contract declares",
                    )
                )
            plan.properties.append(_support_property(ctx, scorable=support_stat is not None))
            if support_stat is not None:
                plan.properties[-1].reads.append(
                    (
                        ctx.profile_tokens + ("statistics", str(support_stat[0])),
                        "the support property is scored against it",
                    )
                )
        return
    if levels_stat is not None:
        index, statistic, _group = levels_stat
        levels = [str(level) for level in statistic.get("levels", ())]
        plan.declaration["support"] = {"values": levels}
        plan.declaration["maxCardinality"] = len(levels)
        plan.labels.append(
            Label(
                ("maxCardinality",),
                from_profile(ctx.profile_tokens + ("statistics", str(index), "levels"), _LEVELS),
            )
        )
        _level_weights(ctx, plan, levels_stat)
        plan.how = f"{len(levels)} levels and their counts, from the profile"
        return
    if support_stat is not None:
        index, _statistic, group = support_stat
        members = [str(member) for member in group.get("support", ())]
        if members:
            plan.declaration["support"] = {"values": members}
            plan.declaration["maxCardinality"] = len(members)
            plan.labels.append(
                Label(
                    ("maxCardinality",),
                    from_profile(
                        ctx.profile_tokens + ("statistics", str(index), "groups", "0", "support"),
                        _SUPPORT,
                    ),
                )
            )
            plan.how = f"{len(members)} levels the profile lists, at equal weights"
            plan.assumptions.append(
                "The profile lists the levels but not their counts, so they are drawn at "
                "equal weights."
            )
            plan.properties.append(_support_property(ctx, scorable=True))
            return
    distinct = _published(ctx.entry, "distinctCount")
    count = _published(ctx.entry, "count")
    if (
        isinstance(distinct, int)
        and isinstance(count, int)
        and distinct == count
        and count > 0
        and ctx.foreign is None
        and not ctx.keyed
    ):
        plan.declaration["unique"] = True
        plan.how = f"an identifier: {distinct} distinct values in {count}, so the engine derives one per row"
        _read(ctx, plan, "distinctCount", "as many distinct values as values: an identifier")
        _read(ctx, plan, "count", "as many values as distinct values: an identifier")
        return
    _opaque_levels(ctx, plan, distinct)
    if ctx.foreign is not None:
        plan.how += f"; a foreign key to {printable(ctx.foreign)}, whose values these do not match"


def _level_weights(
    ctx: ColumnContext,
    plan: ColumnPlan,
    found: Tuple[int, Mapping[str, Any], Mapping[str, Any]],
    *,
    with_property: bool = True,
) -> None:
    """``levels`` from a published ``levelHistogram/1``, and, *with_property*, its property."""
    index, statistic, group = found
    levels = [str(level) for level in statistic.get("levels", ())]
    cells = _cells(group, "levelCounts")
    stat_tokens = ctx.profile_tokens + ("statistics", str(index))
    weights, bases = _cell_weights(ctx, cells, stat_tokens, "levelCounts", _LEVELS)
    texts = share_texts(weights)
    plan.declaration["levels"] = dict(zip(levels, texts, strict=True))
    for level, basis in zip(levels, bases, strict=True):
        plan.labels.append(Label(("levels", level), basis))
    plan.assumptions.extend(_withheld_lines(ctx, cells, "level count"))
    if not with_property:
        return
    plan.properties.append(
        PropertyPlan(
            {
                "property": "levelHistogram",
                "column": ctx.column.name,
                "tolerance": {
                    "type": "distance",
                    "measure": "totalVariation",
                    "max": DEFAULT_TOLERANCES["distance"],
                },
            },
            [Label(("tolerance", "max"), assumed(TOLERANCE_REASON))],
            scorable=True,
        )
    )


def _half(ctx: ColumnContext) -> Fraction:
    return Fraction(ctx.minimum_cell, 2)


def _half_text(ctx: ColumnContext) -> str:
    return _decimal_text(_half(ctx))


def _count_of(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _typical(cells: Sequence[_Cell]) -> Optional[int]:
    """The mean of a count table's published cells, to the nearest whole count; ``None`` if none."""
    published = [cell.count for cell in cells if cell.count is not None]
    if not published:
        return None
    return max(1, round(Fraction(sum(published), len(published))))


def _cell_weights(
    ctx: ColumnContext,
    cells: Sequence[_Cell],
    stat_tokens: Tuple[str, ...],
    key: str,
    definition: str,
) -> Tuple[List[Fraction], List[Basis]]:
    """Each cell's weight and basis: its published count, or an assumed one and its reason.

    A cell withheld as small (status ``A``) is half the minimum cell. Any other
    status says nothing of the count's size: the cell is the mean of the
    published ones, or half the minimum cell when the table publishes none.
    """
    typical = _typical(cells)
    weights: List[Fraction] = []
    bases: List[Basis] = []
    for position, cell in enumerate(cells):
        if cell.count is not None:
            weights.append(Fraction(cell.count))
            bases.append(
                from_profile(stat_tokens + ("groups", "0", key, str(position)), definition)
            )
            continue
        status = printable(cell.status)
        if cell.status == _SMALL:
            weights.append(_half(ctx))
            reason = (
                f"The profile withheld this count as a small one (status {status}): it lies "
                f"below the minimum cell ({ctx.minimum_cell}), so it is assumed to be half of it."
            )
        elif typical is not None:
            weights.append(Fraction(typical))
            reason = (
                f"The profile withheld this count (status {status}), which does not say how "
                "large it is, so it is assumed to be the mean of the counts the table "
                "publishes, to the nearest whole count."
            )
        else:
            weights.append(_half(ctx))
            reason = (
                f"The profile withheld this count (status {status}), which does not say how "
                f"large it is, and publishes no other: it is assumed to be half the minimum "
                f"cell ({ctx.minimum_cell})."
            )
        bases.append(assumed(reason))
    return weights, bases


def _withheld_lines(ctx: ColumnContext, cells: Sequence[_Cell], noun: str) -> List[str]:
    """What a person is told about a count table's withheld cells, one line per rule used."""
    small = sum(1 for cell in cells if cell.count is None and cell.status == _SMALL)
    other = sum(1 for cell in cells if cell.count is None and cell.status != _SMALL)
    lines: List[str] = []
    if small:
        lines.append(
            f"The profile withheld {_count_of(small, noun)} as small (status {_SMALL}); each is "
            f"assumed to be {_half_text(ctx)}, half the rule's minimum cell."
        )
    if other and _typical(cells) is not None:
        lines.append(
            f"The profile withheld {_count_of(other, noun)} under a status that does not say "
            "how large; each is assumed to be the mean of the published ones."
        )
    elif other:
        lines.append(
            f"The profile withheld {_count_of(other, noun)} under a status that does not say "
            f"how large, and publishes none; each is assumed to be {_half_text(ctx)}, half the "
            "rule's minimum cell."
        )
    return lines


def _support_property(ctx: ColumnContext, *, scorable: bool) -> PropertyPlan:
    return PropertyPlan(
        {
            "property": "support",
            "column": ctx.column.name,
            "tolerance": {"type": "count", "max": 0},
        },
        [Label(("tolerance", "max"), assumed(TOLERANCE_REASON))],
        scorable=scorable,
    )


def _opaque_levels(ctx: ColumnContext, plan: ColumnPlan, distinct: Any) -> None:
    """Levels named ``<column>-<n>``: the profile publishes how many values, never which."""
    name = ctx.column.name
    if ctx.keyed and not (isinstance(distinct, int) and distinct > 0):
        # A column of a key of several columns: as many values as rows, so that the key
        # can hold one value per row.
        drawn = min(max(ctx.rows, 1), OPAQUE_LEVEL_CAP)
        reason = (
            "A column of a key the contract declares, whose values the profile does not "
            f"publish: as many opaque levels as the table has rows, at most {OPAQUE_LEVEL_CAP}, "
            f"named {printable(name)}-<n>, at equal weights, so the key can hold a value per row."
        )
    elif isinstance(distinct, int) and distinct > 0:
        # The reason names no number of the profile (PARAMETER-REGISTER-SPEC section 3):
        # how many levels there are is in the declaration itself.
        drawn = min(distinct, OPAQUE_LEVEL_CAP)
        _read(ctx, plan, "distinctCount", "how many opaque levels are drawn")
        capped = f", more than {OPAQUE_LEVEL_CAP}," if drawn < distinct else ""
        reason = (
            f"The profile publishes how many distinct values the column holds{capped} and "
            f"neither the values nor their counts: as many opaque levels, named "
            f"{printable(name)}-<n>, at equal weights."
        )
    elif _metric(ctx.entry, "distinctCount") is not None:
        # Half the minimum cell, as for a withheld cell of a count table.
        drawn = max(1, ctx.minimum_cell // 2)
        why = (
            "a count withheld as small lies below the minimum cell, so half of it is assumed"
            if _withheld_small(ctx.entry, "distinctCount")
            else "which does not say how many, so half the minimum cell is assumed"
        )
        reason = (
            f"{_withheld_note(ctx.entry, 'distinctCount')}: {why}, as many opaque levels, "
            f"named {printable(name)}-<n>, at equal weights."
        )
    else:
        drawn = 1
        reason = (
            f"The profile has no distinctCount and no values: one opaque level, "
            f"{printable(name)}-1."
        )
    width = len(str(drawn))
    names = [f"{name}-{position:0{width}d}" for position in range(1, drawn + 1)]
    texts = share_texts([Fraction(1)] * drawn)
    plan.declaration["levels"] = dict(zip(names, texts, strict=True))
    plan.labels.append(Label(("levels",), assumed(reason)))
    plan.how = f"{_count_of(drawn, 'opaque level')}, at equal weights"
    plan.assumptions.append(reason)


def _boolean(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """A BOOL column: the profile's counts of ``false`` and ``true``, or equal weights.

    The counts shape the draw and nothing the check reads: the check reads a
    categorical column's values as text, and a BOOL column holds booleans
    (``FLX-5005`` in a run, ``FLX-5035`` in a slice). So the column declares no
    ``support`` and carries no property.
    """
    found = _ungrouped(ctx.entry, _LEVELS)
    if found is not None and list(found[1].get("levels", ())) == ["false", "true"]:
        _level_weights(ctx, plan, found, with_property=False)
        plan.how = "false and true, weighted by the profile's counts"
        return
    reason = "The profile publishes no counts of false and true: each is drawn half the time."
    plan.declaration["levels"] = {"false": "0.5", "true": "0.5"}
    plan.labels.append(Label(("levels",), assumed(reason)))
    plan.how = "false and true, at equal weights"
    plan.assumptions.append(reason)


# --- ordered columns: numbers, dates and timestamps -------------------------


@dataclass(frozen=True)
class _Point:
    """A published order statistic: its level, its value and where the profile holds it."""

    level: Fraction
    value: Decimal
    tokens: Tuple[str, ...]
    definition: str


def _points(ctx: ColumnContext) -> List[_Point]:
    """The published ``min``, quartiles and ``max``, in level order."""
    points: List[_Point] = []
    metrics = ctx.profile_tokens + ("metrics",)
    low = _published(ctx.entry, "min")
    if low is not None:
        points.append(_Point(Fraction(0), Decimal(str(low)), metrics + ("min",), _MIN))
    quantiles = _published(ctx.entry, "quantiles")
    if isinstance(quantiles, Mapping):
        for level, value in sorted(quantiles.items(), key=lambda item: Fraction(item[0])):
            points.append(
                _Point(
                    Fraction(level),
                    Decimal(str(value)),
                    metrics + ("quantiles", "values", str(level)),
                    _QUANTILES,
                )
            )
    high = _published(ctx.entry, "max")
    if high is not None:
        points.append(_Point(Fraction(1), Decimal(str(high)), metrics + ("max",), _MAX))
    return points


def _ordered(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """A number, date or timestamp column, from the best of what the profile published."""
    kind = ctx.column.logical_type.kind
    beyond = _beyond_plan(ctx)
    found = _ungrouped(ctx.entry, _HISTOGRAM)
    if not beyond and found is not None and len(found[1].get("bins", ())) >= 2:
        _from_histogram(ctx, plan, found)
        return
    numeric = kind in (LogicalKind.INT, LogicalKind.DECIMAL)
    if ctx.key and numeric:
        _key_range(ctx, plan, beyond=beyond)
        return
    if beyond and ctx.keyed and numeric:
        _key_range(ctx, plan, beyond=beyond)
        return
    if beyond:
        plan.declaration["levels"] = {"0": "1"}
        plan.labels.append(Label(("levels",), assumed(_BEYOND_REASON)))
        plan.how = (
            "a placeholder, 0 on every row: the profile's values are beyond what a row carries"
        )
        plan.assumptions.append(_BEYOND_REASON)
        plan.placeholder = True
        return
    points = _points(ctx)
    distinct_values = sorted({point.value for point in points})
    if len(distinct_values) >= 2:
        _from_order_statistics(ctx, plan, points)
        return
    if ctx.keyed and numeric:
        # A column of a key the contract declares with several columns: never one value or
        # a placeholder every row shares, or the key could not hold one value per row.
        _key_range(ctx, plan, beyond=beyond)
        return
    if points:
        _one_value(ctx, plan, points[-1])
        return
    if _temporal(kind):
        plan.how = "uniform within the run's time window: the profile publishes no order statistic"
        plan.assumptions.append(
            "The profile withheld every order statistic, so it is drawn within the run's "
            "time window (spec.timeBounds)."
        )
        return
    _around_mean(ctx, plan)


#: Why an integer column with values a run's rows cannot carry takes a placeholder.
_BEYOND_REASON = (
    "The profile publishes values further from zero than the integers a run's rows carry "
    "exactly (2^53), so none of them is declared: every row takes 0, a placeholder."
)


def _beyond_plan(ctx: ColumnContext) -> bool:
    """Whether an integer column publishes a value a run's rows cannot carry exactly.

    A run writes its rows as canonical JSON, whose integers stop at 2^53 - 1
    (RFC 8785), so an INT value drawn beyond it is refused (``FLX-1003``).
    """
    if ctx.column.logical_type.kind is not LogicalKind.INT:
        return False
    values = [Fraction(point.value) for point in _points(ctx)]
    found = _ungrouped(ctx.entry, _HISTOGRAM)
    if found is not None:
        values += [Fraction(Decimal(str(edge))) for edge in found[1].get("bins", ())]
    mean = _published_mean(ctx)
    if mean is not None:
        values.append(mean)
    return any(abs(value) > JCS_MAX_SAFE_INTEGER for value in values)


def _range(ctx: ColumnContext, plan: ColumnPlan, low: _Point, high: _Point) -> None:
    """The ``range`` the check reads, from a published low and high value."""
    kind = ctx.column.logical_type.kind
    plan.declaration["range"] = {"min": _spell(kind, low.value), "max": _spell(kind, high.value)}
    plan.labels.append(Label(("range", "min"), from_profile(low.tokens, low.definition)))
    plan.labels.append(Label(("range", "max"), from_profile(high.tokens, high.definition)))
    plan.published_range = (low.value, high.value)
    if _temporal(kind):
        plan.time_span = (int(low.value), int(high.value))


def _from_histogram(
    ctx: ColumnContext,
    plan: ColumnPlan,
    found: Tuple[int, Mapping[str, Any], Mapping[str, Any]],
) -> None:
    """Bins and weights from a published ``marginalHistogram/1``, and its properties.

    The range is the histogram's first and last edge, since the bins of a
    property over the column partition its range (``FLX-5032``): a published
    ``min`` and ``max`` lie inside it. The draw keeps to the published ``min``
    and ``max`` (:func:`_drawn_edges`), so a histogram whose outer edges are
    round numbers beyond the data never draws a value it holds none of. With a
    mean published, the bins are shaped to it as the quartile bins are
    (:func:`shape_to_mean`), which keeps every bin's count.
    """
    kind = ctx.column.logical_type.kind
    index, statistic, group = found
    stat_tokens = ctx.profile_tokens + ("statistics", str(index))
    edges = [Decimal(str(edge)) for edge in statistic.get("bins", ())]
    spelled = [_spell(kind, edge) for edge in edges]
    edge_bases = [
        from_profile(stat_tokens + ("bins", str(position)), _HISTOGRAM)
        for position in range(len(edges))
    ]
    drawn, drawn_bases = _drawn_edges(ctx, plan, edges, edge_bases)
    cells = _cells(group, "binCounts")
    weights, bases = _cell_weights(ctx, cells, stat_tokens, "binCounts", _HISTOGRAM)
    mean = _published_mean(ctx)
    shaped = None
    if mean is not None and kind in (LogicalKind.INT, LogicalKind.DECIMAL) and _shapes(ctx):
        shaped = shape_to_mean(drawn, weights, mean, _grid(ctx))
        _read_mean(
            ctx,
            plan,
            (
                "the histogram's bins are shaped to its mean"
                if shaped is not None
                else "checked against the histogram, which keeps it as near as its bins allow"
            ),
        )
    if shaped is None:
        plan.declaration["bins"] = [_spell(kind, edge) for edge in drawn]
        plan.declaration["binWeights"] = [_decimal_text(weight) for weight in weights]
        for position, basis in enumerate(drawn_bases):
            plan.labels.append(Label(("bins", str(position)), basis))
        for position, basis in enumerate(bases):
            plan.labels.append(Label(("binWeights", str(position)), basis))
        plan.how = f"a histogram over {len(edges) - 1} bins, from the profile"
    else:
        _shaped_bins(ctx, plan, shaped, drawn_bases, bases)
        plan.how = f"a histogram over {len(edges) - 1} bins, from the profile, shaped to its mean"
    low = _Point(Fraction(0), edges[0], stat_tokens + ("bins", "0"), _HISTOGRAM)
    high = _Point(Fraction(1), edges[-1], stat_tokens + ("bins", str(len(edges) - 1)), _HISTOGRAM)
    if low.value < high.value:
        _range(ctx, plan, low, high)
    plan.assumptions.extend(_withheld_lines(ctx, cells, "bin count"))
    if "range" in plan.declaration and _scores_order(kind):
        plan.properties.append(
            PropertyPlan(
                {
                    "property": "marginalHistogram",
                    "column": ctx.column.name,
                    "bins": list(spelled),
                    "tolerance": {
                        "type": "distance",
                        "measure": "totalVariation",
                        "max": DEFAULT_TOLERANCES["distance"],
                    },
                },
                [
                    *(
                        Label(("bins", str(position)), basis)
                        for position, basis in enumerate(edge_bases)
                    ),
                    Label(("tolerance", "max"), assumed(TOLERANCE_REASON)),
                ],
                scorable=True,
            )
        )
    _mean_property(ctx, plan)


def _scores_order(kind: LogicalKind) -> bool:
    """Whether a property over a column of *kind* can be scored against a profile.

    A profile reads a DATE column as a timestamp in milliseconds, and the block
    declares its bounds in days (:func:`_spell`), so the check refuses every
    property over one (``FLX-5037``): a DATE column carries none.
    """
    return kind is not LogicalKind.DATE


def _drawn_edges(
    ctx: ColumnContext, plan: ColumnPlan, edges: Sequence[Decimal], bases: Sequence[Basis]
) -> Tuple[List[Decimal], List[Basis]]:
    """A histogram's edges for the draw: the outer two moved in to a published ``min`` and ``max``.

    An outer edge moves only when the published value lies strictly inside its
    bin, so every bin keeps a width and its count; the property's bins, and the
    range the check reads, stay the histogram's own.
    """
    drawn, drawn_bases = list(edges), list(bases)
    metrics = ctx.profile_tokens + ("metrics",)
    low, high = _published(ctx.entry, "min"), _published(ctx.entry, "max")
    for name, value, edge in (("min", low, edges[0]), ("max", high, edges[-1])):
        if value is not None and Decimal(str(value)) == edge:
            plan.reads.append((metrics + (name,), "checked: it lies on the histogram's outer edge"))
    if low is not None and edges[0] < Decimal(str(low)) < edges[1]:
        drawn[0], drawn_bases[0] = Decimal(str(low)), from_profile(metrics + ("min",), _MIN)
    if high is not None and edges[-2] < Decimal(str(high)) < edges[-1]:
        drawn[-1], drawn_bases[-1] = Decimal(str(high)), from_profile(metrics + ("max",), _MAX)
    kind = ctx.column.logical_type.kind
    order = [int(edge) // _DAY_MS if kind is LogicalKind.DATE else edge for edge in drawn]
    if any(below >= above for below, above in pairwise(order)):
        return list(edges), list(bases)
    return drawn, drawn_bases


def _mean_property(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """A ``mean`` property when the profile publishes ``mean/1`` over a ranged number column.

    The tolerance is a tenth of the published mean: relative when the declared
    range keeps away from zero, and otherwise the same margin written as an
    absolute one, since a relative tolerance needs a range that does not span
    zero (``FLX-5002``). A mean of zero has no tenth to take, so it gives no
    property, and neither does a date or a timestamp, whose tenth would be one
    of the time since 1970.
    """
    if (
        ctx.column.logical_type.kind not in (LogicalKind.INT, LogicalKind.DECIMAL)
        or "range" not in plan.declaration
        or plan.published_range is None
        or _ungrouped(ctx.entry, _MEAN) is None
    ):
        return
    mean = _published_mean(ctx)
    if mean is None or mean == 0:
        return
    low, high = plan.published_range
    if low > 0 or high < 0:
        tolerance = {"type": "relative", "value": DEFAULT_TOLERANCES["mean"]}
    else:
        # A tenth of the mean, rounded up to the column's grid (at least the cent), exactly.
        step = Decimal((0, (1,), -max(_scale(ctx), 2)))
        margin = abs(mean) * Fraction(DEFAULT_TOLERANCES["mean"])
        units = max(math.ceil(margin / Fraction(step)), 1)
        tolerance = {
            "type": "absolute",
            "value": format(_on_grid(units * Fraction(step), step), "f"),
        }
    found = _ungrouped(ctx.entry, _MEAN)
    plan.properties.append(
        PropertyPlan(
            {"property": "mean", "column": ctx.column.name, "tolerance": tolerance},
            [Label(("tolerance", "value"), assumed(TOLERANCE_REASON))],
            scorable=True,
            reads=(
                [
                    (
                        ctx.profile_tokens + ("statistics", str(found[0])),
                        "the mean property is scored against it",
                    )
                ]
                if found is not None
                else []
            ),
        )
    )


def _histogram(points: Sequence[_Point]) -> Tuple[List[_Point], List[Fraction]]:
    """Edges and bin weights from published order statistics.

    The edges are the distinct values, in order, each named by the point that
    reaches it at the highest level. A bin's weight is the share of rows between
    its edges; the share at or below the first edge and above the last one (a
    withheld ``min`` or ``max``, or a point mass on an edge) goes to the first and
    last bins, so the weights add up to one.
    """
    reach: Dict[Decimal, _Point] = {}
    for point in points:
        held = reach.get(point.value)
        if held is None or point.level >= held.level:
            reach[point.value] = point
    edges = [reach[value] for value in sorted(reach)]
    weights = [high.level - low.level for low, high in pairwise(edges)]
    weights[0] += edges[0].level
    weights[-1] += 1 - edges[-1].level
    return edges, weights


def _published_mean(ctx: ColumnContext) -> Optional[Fraction]:
    """The column's published mean: ``sum / count``, else a ``mean/1`` statistic, else ``None``."""
    total, count = _published(ctx.entry, "sum"), _published(ctx.entry, "count")
    if total is not None and isinstance(count, int) and count > 0:
        return Fraction(Decimal(str(total))) / count
    found = _ungrouped(ctx.entry, _MEAN)
    if found is not None:
        group = found[2]
        k, total_on_grid = group.get("k"), group.get("Sx")
        if isinstance(k, int) and k > 0 and isinstance(total_on_grid, str):
            return Fraction(int(total_on_grid), k * 2**ctx.fractional_bits)
    return None


def _mean_sources(ctx: ColumnContext) -> List[Tuple[str, ...]]:
    """Where :func:`_published_mean` reads the mean: ``sum`` and ``count``, else ``mean/1``."""
    total, count = _published(ctx.entry, "sum"), _published(ctx.entry, "count")
    if total is not None and isinstance(count, int) and count > 0:
        return [_metric_tokens(ctx, "sum"), _metric_tokens(ctx, "count")]
    found = _ungrouped(ctx.entry, _MEAN)
    if found is not None:
        return [ctx.profile_tokens + ("statistics", str(found[0]))]
    return []


def _read_mean(ctx: ColumnContext, plan: ColumnPlan, how: str) -> None:
    """Record that the rules read the column's published mean, wherever it came from."""
    plan.reads.extend((tokens, how) for tokens in _mean_sources(ctx))


#: How many halvings find the share that shapes a histogram to its mean: the share is
#: then known to 2^-64, far finer than one grid step of any bin.
_BISECTIONS = 64


def _part_mean(low: Fraction, high: Fraction, step: Fraction, last: bool) -> Fraction:
    """The mean of one bin as the generator draws it: evenly over its grid points.

    A bin is half-open, its top grid point one step below its upper edge, and
    the last bin is closed (``_bin_top`` in ``generator/rows.py``). A bin
    narrower than one step holds its lower edge alone.
    """
    top = high if last else high - step
    if top <= low:
        return low
    return low + step * ((top - low) // step) / 2


def _drawn_mean(edges: Sequence[Fraction], weights: Sequence[Fraction], step: Fraction) -> Fraction:
    """A histogram's mean as the generator draws it: each bin's mean, by the bin's weight."""
    last = len(weights) - 1
    parts = zip(weights, edges[:-1], edges[1:], strict=True)
    total = sum(
        (weight * _part_mean(low, high, step, position == last))
        for position, (weight, low, high) in enumerate(parts)
    )
    return Fraction(total) / sum(weights, Fraction(0))


def _cut(
    edges: Sequence[Fraction],
    weights: Sequence[Fraction],
    share: Fraction,
    rise: bool,
    step: Fraction,
) -> Tuple[List[Fraction], List[Fraction], List[bool]]:
    """Each bin cut on the grid at *share* of its width from the side the mean leaves.

    That side is left empty. The part kept holds one grid point at least, so a
    cut that would leave it none is moved to the nearest that leaves one; a cut
    on an edge, and a bin with no weight, are left whole.
    """
    new_edges: List[Fraction] = [edges[0]]
    new_weights: List[Fraction] = []
    inserted: List[bool] = [False]
    for low, high, weight in zip(edges[:-1], edges[1:], weights, strict=True):
        width = high - low
        cut = round((low + share * width if rise else high - share * width) / step) * step
        cut = min(cut, high - step) if rise else max(cut, low + step)
        if weight <= 0 or not low < cut < high:
            new_edges.append(high)
            new_weights.append(weight)
            inserted.append(False)
            continue
        new_edges += [cut, high]
        new_weights += [Fraction(0), weight] if rise else [weight, Fraction(0)]
        inserted += [True, False]
    return new_edges, new_weights, inserted


def _on_grid(value: Fraction, step: Decimal) -> Decimal:
    """*value*, a multiple of *step*, as the exact decimal on that step's exponent."""
    exponent = int(step.as_tuple().exponent)
    units = int(value / Fraction(10) ** exponent)
    digits = tuple(int(digit) for digit in str(abs(units)))
    return Decimal((0 if units >= 0 else 1, digits, exponent))


def shape_to_mean(
    edges: Sequence[Decimal], weights: Sequence[Fraction], mean: Fraction, step: Decimal
) -> Optional[Tuple[List[Decimal], List[Fraction], List[bool]]]:
    """Move each bin's values toward one side so the histogram's mean, as drawn, is *mean*.

    The generator draws a bin evenly over its grid points (*step* apart), so a
    histogram has one mean (:func:`_drawn_mean`). When the published mean
    differs, each bin keeps its mass but draws only from the part of it on the
    mean's side: the same share of every bin's width is left empty, below the
    values when the mean must rise and above them when it must fall. Every bin,
    and so every published quantile, keeps its mass.

    The cuts lie on the grid, so the mean moves in steps as the share grows,
    and only one way. The share is found by bisection, and the cuts kept are
    the ones whose mean is nearest *mean*.

    A mean the cuts cannot reach takes the furthest they go, every bin drawn
    from its last grid points on the mean's side.

    Returns the new edges, their weights, and which edges are new cuts, or
    ``None`` when no cut brings the mean nearer, or the mean lies outside the
    histogram's edges, where no data can put it.
    """
    total = sum(weights, Fraction(0))
    if total <= 0 or len(edges) != len(weights) + 1:
        return None
    if not Fraction(edges[0]) <= mean <= Fraction(edges[-1]):
        return None
    grid = Fraction(step)
    bounds = [Fraction(edge) for edge in edges]
    unshaped = _drawn_mean(bounds, weights, grid)
    if unshaped == mean:
        return None
    rise = mean > unshaped

    def drawn(share: Fraction) -> Fraction:
        cut_edges, cut_weights, _ = _cut(bounds, weights, share, rise, grid)
        return _drawn_mean(cut_edges, cut_weights, grid)

    def reached(share: Fraction) -> bool:
        return drawn(share) >= mean if rise else drawn(share) <= mean

    share = Fraction(1)
    if reached(share):
        below, above = Fraction(0), Fraction(1)
        for _ in range(_BISECTIONS):
            middle = (below + above) / 2
            if reached(middle):
                above = middle
            else:
                below = middle
        share = min((below, above), key=lambda candidate: abs(drawn(candidate) - mean))
    if abs(drawn(share) - mean) >= abs(unshaped - mean):
        return None
    cut_edges, cut_weights, inserted = _cut(bounds, weights, share, rise, grid)
    if not any(inserted):
        return None
    originals = iter(edges)
    spelled = [
        _on_grid(edge, step) if new else Decimal(next(originals))
        for edge, new in zip(cut_edges, inserted, strict=True)
    ]
    return spelled, cut_weights, inserted


def _from_order_statistics(ctx: ColumnContext, plan: ColumnPlan, points: List[_Point]) -> None:
    """Bins over the published ``min``, quartiles and ``max``: what ``fluid profile`` writes.

    With both ends and a mean published, the bins are shaped to the mean
    (:func:`shape_to_mean`), and the mean becomes a property when the profile
    publishes ``mean/1``, one it can score.
    """
    kind = ctx.column.logical_type.kind
    edges, weights = _histogram(points)
    has_low = any(point.level == 0 for point in points)
    has_high = any(point.level == 1 for point in points)
    has_quantiles = any(point.definition == _QUANTILES for point in points)
    spelled = [_spell(kind, edge.value) for edge in edges]
    clipped = [name for name, held in (("min", has_low), ("max", has_high)) if not held]
    if clipped:
        weights_basis = assumed(
            f"The profile withheld {' and '.join(clipped)}: the rows beyond the outermost "
            "published value are put in the outermost bin."
        )
    elif has_quantiles:
        weights_basis = from_profile(ctx.profile_tokens + ("metrics", "quantiles"), _QUANTILES)
    else:
        weights_basis = assumed(
            "Only min and max are published: the values are drawn evenly between them."
        )
    if weights_basis.reason is not None:
        plan.assumptions.append(weights_basis.reason)
    edge_bases = [from_profile(edge.tokens, edge.definition) for edge in edges]
    mean = _published_mean(ctx)
    shaped = None
    if not clipped and mean is not None and kind in (LogicalKind.INT, LogicalKind.DECIMAL):
        shaped = (
            shape_to_mean([edge.value for edge in edges], weights, mean, _grid(ctx))
            if _shapes(ctx)
            else None
        )
        _read_mean(
            ctx,
            plan,
            (
                "the bins are shaped to its mean"
                if shaped is not None
                else "checked against the bins, which keep it as near as they allow"
            ),
        )
    if shaped is None:
        plan.declaration["bins"] = spelled
        plan.declaration["binWeights"] = [_decimal_text(weight) for weight in weights]
        for position, basis in enumerate(edge_bases):
            plan.labels.append(Label(("bins", str(position)), basis))
        plan.labels.append(Label(("binWeights",), weights_basis))
        plan.how = f"a histogram over {len(edges)} published order statistics"
    else:
        _shaped_bins(ctx, plan, shaped, edge_bases, weights_basis)
    if has_low and has_high:
        # The published ends, unless a quantile lies beyond one: the property's bins
        # run over every published value, and they partition the range (FLX-5032).
        low = next(point for point in points if point.level == 0)
        high = next(point for point in points if point.level == 1)
        low = low if low.value <= edges[0].value else edges[0]
        high = high if high.value >= edges[-1].value else edges[-1]
        if low.value < high.value:
            _range(ctx, plan, low, high)
    if "range" in plan.declaration and _scores_order(kind):
        plan.properties.append(
            PropertyPlan(
                {
                    "property": "marginalHistogram",
                    "column": ctx.column.name,
                    "bins": list(spelled),
                    "tolerance": {
                        "type": "distance",
                        "measure": "totalVariation",
                        "max": DEFAULT_TOLERANCES["distance"],
                    },
                },
                [
                    *(
                        Label(("bins", str(position)), basis)
                        for position, basis in enumerate(edge_bases)
                    ),
                    Label(("tolerance", "max"), assumed(TOLERANCE_REASON)),
                ],
                scorable=False,
            )
        )
        _mean_property(ctx, plan)


#: Why a histogram was shaped to the published mean.
_SHAPED_REASON = (
    "Drawn evenly within its bins, the histogram would give a mean other than the one the "
    "profile publishes, so within each bin the values are drawn from the part on the mean's "
    "side: each bin keeps its share of the rows, so every published quantile or bin count "
    "holds, and the mean is the published one, or as near to it as the bins allow on the "
    "column's grid."
)


def _shaped_bins(
    ctx: ColumnContext,
    plan: ColumnPlan,
    shaped: Tuple[List[Decimal], List[Fraction], List[bool]],
    edge_bases: Sequence[Basis],
    weights_basis: Union[Basis, Sequence[Basis]],
) -> None:
    """Declare bins shaped to the published mean: each cut and each emptied part assumed.

    *weights_basis* is one basis for every published bin's weight, or one per
    original bin, in order; a bin's kept part takes its basis.
    """
    kind = ctx.column.logical_type.kind
    edges, weights, inserted = shaped
    plan.declaration["bins"] = [_spell(kind, edge) for edge in edges]
    plan.declaration["binWeights"] = [_decimal_text(weight) for weight in weights]
    published = iter(edge_bases)
    for position, cut in enumerate(inserted):
        basis = assumed(_SHAPED_REASON) if cut else next(published)
        plan.labels.append(Label(("bins", str(position)), basis))
    per_bin = (
        iter(weights_basis)
        if not isinstance(weights_basis, Basis)
        else iter([weights_basis] * len(edge_bases))
    )
    original: Optional[Basis] = None
    for position, weight in enumerate(weights):
        if not inserted[position]:
            original = next(per_bin)  # this part starts one of the published bins
        assert original is not None
        split = inserted[position] or inserted[position + 1]
        basis = assumed(_SHAPED_REASON) if split and weight == 0 else original
        plan.labels.append(Label(("binWeights", str(position)), basis))
    plan.how = (
        f"a histogram over {len(edge_bases)} published order statistics, shaped to the "
        "published mean"
    )
    plan.assumptions.append(_SHAPED_REASON)
    plan.choices.append(_SHAPED_REASON)


def _one_value(ctx: ColumnContext, plan: ColumnPlan, point: _Point) -> None:
    """One published value: every number takes it; a date or timestamp spans its day."""
    kind = ctx.column.logical_type.kind
    plan.reads.append((point.tokens, "the one value the profile publishes"))
    if _temporal(kind):
        end = point.value + _DAY_MS
        plan.declaration["bins"] = [_spell(kind, point.value), _spell(kind, end)]
        plan.declaration["binWeights"] = ["1"]
        reason = (
            f"The profile publishes one value ({point.definition}) and no other order "
            "statistic: the values are drawn within the day that starts there."
        )
        plan.labels.append(Label(("bins",), assumed(reason)))
        plan.labels.append(Label(("binWeights",), assumed(reason)))
        plan.how = "within one day, from the one value the profile publishes"
        plan.assumptions.append(reason)
        return
    plan.declaration["levels"] = {_spell(kind, point.value): "1"}
    reason = (
        f"The profile publishes one value ({point.definition}) and no other order "
        "statistic: every row takes it."
    )
    plan.labels.append(Label(("levels",), assumed(reason)))
    plan.how = "one value on every row, the one the profile publishes"
    plan.assumptions.append(reason)


#: How an assumed key span reads, and how it reads once the foreign keys referencing it,
#: or the key it references, move it (:func:`widen_key_span` swaps one for another).
_KEY_SPAN_ROWS = "its values are assumed to span 1 to the row count."
_KEY_SPAN_FOREIGN = (
    "its values are assumed to span the values the foreign keys that reference it are "
    "declared over, and at least as many values as rows."
)
_KEY_SPAN_PARENT = "its values are assumed to span the values of the key it references."
_KEY_SPANS = (_KEY_SPAN_ROWS, _KEY_SPAN_FOREIGN, _KEY_SPAN_PARENT)
#: How a key with a published min and max draws.
_KEY_LAYOUT = "its values are drawn evenly between the published min and max."


def _key_noun(ctx: ColumnContext) -> str:
    """A key the contract declares, or one column of a key of several."""
    return "A key the contract declares" if ctx.key else "A column of a key the contract declares"


def _key_reason(ctx: ColumnContext, why: str) -> str:
    """Why a key's span is assumed: *why* says what the profile left out."""
    return f"{_key_noun(ctx)}, {why}: {_KEY_SPAN_ROWS}"


def _key_range(ctx: ColumnContext, plan: ColumnPlan, *, beyond: bool) -> None:
    """A number key: the span its values are drawn over.

    Between the published ``min`` and ``max`` when they hold one value per row
    and a row can carry them (*beyond* says it cannot), cited to the profile.
    Otherwise the span is an assumption, 1 to the row count, which the bundle
    then moves (:func:`widen_key_span`): out to the foreign keys that reference
    the key, so each of their values lies inside it, and into the key it
    references itself, when it is a foreign key too.
    """
    kind = ctx.column.logical_type.kind
    low, high = _published(ctx.entry, "min"), _published(ctx.entry, "max")
    missing = [name for name, value in (("min", low), ("max", high)) if value is None]
    if missing:
        why = f"whose {' and '.join(missing)} the profile does not publish"
    elif beyond:
        why = "whose published min and max lie beyond the integers a row carries exactly"
    else:
        metrics = ctx.profile_tokens + ("metrics",)
        first = _Point(Fraction(0), Decimal(str(low)), metrics + ("min",), _MIN)
        last = _Point(Fraction(1), Decimal(str(high)), metrics + ("max",), _MAX)
        span = (Fraction(last.value) - Fraction(first.value)) / Fraction(_grid(ctx))
        if first.value < last.value and span + 1 >= ctx.rows:
            plan.declaration["bins"] = [_spell(kind, first.value), _spell(kind, last.value)]
            plan.declaration["binWeights"] = ["1"]
            plan.labels.append(Label(("bins", "0"), from_profile(first.tokens, _MIN)))
            plan.labels.append(Label(("bins", "1"), from_profile(last.tokens, _MAX)))
            layout = f"{_key_noun(ctx)}: {_KEY_LAYOUT}"
            plan.labels.append(Label(("binWeights",), assumed(layout)))
            _range(ctx, plan, first, last)
            plan.how = "a key the contract declares, between its published min and max"
            plan.assumptions.append(layout)
            plan.choices.append(layout)
            return
        why = "whose published min and max do not hold one value per row"
    reason = _key_reason(ctx, why)
    plan.declaration["bins"] = ["1", str(ctx.rows)] if ctx.rows > 1 else ["1", "2"]
    plan.declaration["binWeights"] = ["1"]
    plan.labels.append(Label(("bins",), assumed(reason)))
    plan.labels.append(Label(("binWeights",), assumed(reason)))
    plan.how = "a key the contract declares, spanning 1 to the row count"
    plan.assumptions.append(reason)
    plan.key_span = True


def declared_span(plan: ColumnPlan) -> List[Decimal]:
    """The numbers a column is declared over: its range's ends, its bin edges, its levels.

    What a key that the column references has to hold. A placeholder gives none:
    it says nothing about where the values lie. Text, such as a timestamp bound,
    is not a number and is left out.
    """
    if plan.placeholder:
        return []
    declaration = plan.declaration
    values: List[Any] = list((declaration.get("range") or {}).values())
    values += list(declaration.get("bins") or ())
    values += list(declaration.get("levels") or ())
    return _numbers(values)


def drawn_span(plan: ColumnPlan) -> List[Decimal]:
    """The numbers a column is drawn over: its bin edges or its levels, else its range.

    What a column that references this one has to keep to. It can be narrower
    than :func:`declared_span`: a histogram's draw keeps to the published ``min``
    and ``max`` while its range is the histogram's own edges.
    """
    if plan.placeholder:
        return []
    declaration = plan.declaration
    values = list(declaration.get("bins") or ()) or list(declaration.get("levels") or ())
    return _numbers(values or list((declaration.get("range") or {}).values()))


def _numbers(values: Sequence[Any]) -> List[Decimal]:
    numbers: List[Decimal] = []
    for value in values:
        try:
            number = Decimal(str(value))
        except ArithmeticError:
            continue
        if number.is_finite():
            numbers.append(number)
    return numbers


def widen_key_span(
    plan: ColumnPlan, referenced: Sequence[Decimal], rows: int, *, bound: Optional[int] = None
) -> None:
    """Let an assumed key span the values the foreign keys referencing it are declared over.

    The span starts at the smallest of them and ends at the largest, and holds
    at least as many values as the key has rows, so each value can be one row.
    The rule is the AWS harness's (``e2e/aws/flux_e2e/bundle.py``). A span
    reaching further from zero than *bound* is not taken.
    """
    if not plan.key_span or not referenced:
        return
    low = min(referenced)
    high = max(max(referenced), _exact_sum(low, rows - 1))
    if bound is not None and max(abs(low), abs(high)) > bound:
        return
    plan.declaration["bins"] = [format(low, "f"), format(high, "f")]
    _restate_span(plan, ("bins", "binWeights"), _KEY_SPAN_FOREIGN)
    plan.how = "a key the contract declares, spanning the foreign keys that reference it"


def _exact_sum(value: Decimal, offset: int) -> Decimal:
    """*value* plus *offset*, in as many digits as the sum needs, never rounded."""
    sign, digits, exponent = value.as_tuple()
    with localcontext() as context:
        # The digits the value spans once its exponent is written out, and the offset's.
        context.prec = len(digits) + max(int(exponent), 0) + len(str(abs(offset))) + 2
        return value + offset


def _respanned(reason: str, suffix: str) -> str:
    """*reason* with the span it states replaced by *suffix*."""
    for span in _KEY_SPANS:
        reason = reason.replace(span, suffix)
    return reason


def _restate_span(plan: ColumnPlan, tokens: Sequence[str], suffix: str) -> None:
    """Say, in the labels at *tokens* and in the assumptions, that the key's span is *suffix*."""
    plan.labels = [
        (
            Label(label.tokens, assumed(_respanned(label.basis.reason, suffix)))
            if label.tokens[:1] in [(token,) for token in tokens] and label.basis.reason is not None
            else label
        )
        for label in plan.labels
    ]
    plan.assumptions = [_respanned(sentence, suffix) for sentence in plan.assumptions]


#: Why a foreign key the profile gives nothing to declare draws where it does.
_WITHIN_KEY_REASON = (
    "A foreign key the profile gives nothing to declare: it is drawn evenly over the span of "
    "the key it references, so each value lies inside that span."
)


def draw_within_key(
    plan: ColumnPlan, parent: ColumnPlan, kind: LogicalKind, rows: Optional[int] = None
) -> bool:
    """Put a column whose span is assumed inside the span its parent is drawn over.

    Two columns take it. A placeholder foreign key, which the profile gave
    nothing to declare, is drawn over the parent's span instead of a value no
    parent row holds. An assumed key that is itself a foreign key of a key whose
    span stands takes that span, so its values are the parent's. An integer
    column keeps to the whole numbers of the span, and never to one a row could
    not carry; an integer key, never to one holding fewer values than its *rows*.
    Returns whether the column was placed.
    """
    if not (plan.placeholder or plan.key_span):
        return False
    span = drawn_span(parent)
    if not span:
        return False
    low, high = min(span), max(span)
    if kind is LogicalKind.INT:
        low, high = low.to_integral_value(ROUND_CEILING), high.to_integral_value(ROUND_FLOOR)
        if max(abs(low), abs(high)) > JCS_MAX_SAFE_INTEGER:
            return False  # a span an integer row could not carry (FLX-1003)
    if high < low or (plan.key_span and high == low):
        return False
    if plan.key_span and kind is LogicalKind.INT and rows is not None and high - low + 1 < rows:
        return False  # the key could not hold one value per row inside it
    if plan.key_span:
        plan.declaration["bins"] = [format(low, "f"), format(high, "f")]
        _restate_span(plan, ("bins", "binWeights"), _KEY_SPAN_PARENT)
        plan.how = "a key the contract declares, spanning the key it references"
        return True
    drawn: Dict[str, Any]
    if high > low:
        drawn = {"bins": [format(low, "f"), format(high, "f")], "binWeights": ["1"]}
    else:
        drawn = {"levels": {format(low, "f"): "1"}}
    plan.declaration.pop("levels", None)
    plan.declaration.update(drawn)
    shape = [("bins",), ("binWeights",), ("levels",)]
    kept = [label for label in plan.labels if label.tokens[:1] not in shape]
    plan.labels = kept + [Label((key,), assumed(_WITHIN_KEY_REASON)) for key in drawn]
    plan.assumptions = [
        sentence
        for sentence in plan.assumptions
        if sentence not in (_PLACEHOLDER_REASON, _BEYOND_REASON)
    ] + [_WITHIN_KEY_REASON]
    plan.how = "a foreign key, drawn over the span of the key it references"
    plan.placeholder = False
    return True


#: The most decimal places a DECIMAL column of a bundle may have: SQL's largest precision.
#: The rules work in exact fractions on the column's grid, and a finer grid would only
#: cost time for a column the generator could not draw anyway (more than 2^64 values).
MOST_PLACES = 38


def _shapes(ctx: ColumnContext) -> bool:
    """Whether the column's grid is one :func:`shape_to_mean` works on."""
    return _scale(ctx) <= MOST_PLACES


def _scale(ctx: ColumnContext) -> int:
    scale = ctx.column.logical_type.scale
    return scale if isinstance(scale, int) and scale > 0 else 0


def _grid(ctx: ColumnContext) -> Decimal:
    """The step between the values the generator draws for a number column: 1, or 10^-scale.

    Built from its digits, so no decimal context bounds the scale.
    """
    return Decimal((0, (1,), -_scale(ctx)))


def _around_mean(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """No order statistic, but perhaps ``sum / count``: a span with that mean, or a placeholder.

    A span as wide as the published ``distinctCount`` keeps a query's totals and
    averages; the spread itself is an assumption, and is said to be one.
    """
    total = _published(ctx.entry, "sum")
    count = _published(ctx.entry, "count")
    if total is not None and isinstance(count, int) and count > 0:
        _read_mean(ctx, plan, "the mean the span is centred on")
        _read(ctx, plan, "distinctCount", "how many values wide the span is")
        # Exact arithmetic on the grid: a decimal context would round a long value or a
        # fine scale (DECIMAL(38,30), say) before it reached the grid.
        mean = Fraction(Decimal(str(total))) / count
        step = _grid(ctx)
        grid = Fraction(step)
        distinct = _published(ctx.entry, "distinctCount")
        half = grid * (distinct - 1) / 2 if isinstance(distinct, int) and distinct > 1 else 0
        low = _on_grid(math.floor((mean - half) / grid) * grid, step)
        high = _on_grid(math.ceil((mean + half) / grid) * grid, step)
        if high > low:
            plan.declaration["bins"] = [format(low, "f"), format(high, "f")]
            plan.declaration["binWeights"] = ["1"]
            reason = (
                "The profile publishes sum and count but no order statistic: the values are "
                "drawn evenly over a span centred on the mean (sum / count), as many values "
                "wide as distinctCount."
            )
            plan.labels.append(Label(("bins",), assumed(reason)))
            plan.labels.append(Label(("binWeights",), assumed(reason)))
            plan.how = "evenly around the published mean"
        else:
            plan.declaration["levels"] = {
                format(_on_grid(round(mean / grid) * grid, step), "f"): "1"
            }
            reason = (
                "The profile publishes sum and count but no order statistic or spread: every "
                "row takes the mean (sum / count)."
            )
            plan.labels.append(Label(("levels",), assumed(reason)))
            plan.how = "the published mean on every row"
        plan.assumptions.append(reason)
        return
    plan.declaration["levels"] = {"0": "1"}
    plan.labels.append(Label(("levels",), assumed(_PLACEHOLDER_REASON)))
    plan.how = "a placeholder, 0 on every row"
    plan.assumptions.append(_PLACEHOLDER_REASON)
    plan.placeholder = True


#: Why a number column the profile says nothing of takes 0 on every row.
_PLACEHOLDER_REASON = (
    "The profile publishes no order statistic and no sum: every row takes 0, a placeholder."
)


# ---------------------------------------------------------------------------
# A table with nothing published for the check to read
# ---------------------------------------------------------------------------

#: Why a table's one checked range or level list is an assumption.
ASSUMED_CHECK_REASON = (
    "No column of this table publishes a range or a list of levels, and a fidelity block "
    "needs one property, so the check reads this assumed one: a committed slice outside it "
    "is refused. Replace it with what you know of the column."
)


def assumed_check(plan: ColumnPlan, kind: LogicalKind) -> Optional[PropertyPlan]:
    """Make a column's assumed shape the one the check reads, for a table that has no other.

    A fidelity block needs one property (``FLX-5034``), and a property reads a
    column's ``range`` or ``support``. When no column of a table has a
    published one, the first that can takes its assumed span (a number) or its
    assumed levels (a categorical) as a declaration the check reads, stated as
    an assumption with :data:`ASSUMED_CHECK_REASON`. ``None`` when the column
    has neither.
    """
    bins = plan.declaration.get("bins")
    if kind in (LogicalKind.INT, LogicalKind.DECIMAL) and isinstance(bins, list) and bins:
        low, high = Decimal(str(bins[0])), Decimal(str(bins[-1]))
        if high <= low:
            return None
        if "range" not in plan.declaration:  # a key's published range is checked as it is
            plan.declaration["range"] = {"min": str(bins[0]), "max": str(bins[-1])}
            plan.labels.append(Label(("range",), assumed(ASSUMED_CHECK_REASON)))
            plan.assumptions.append(ASSUMED_CHECK_REASON)
        share = Decimal(DEFAULT_TOLERANCES["mean"])
        return PropertyPlan(
            {
                "property": "mean",
                "column": plan.name,
                "tolerance": {"type": "absolute", "value": format((high - low) * share, "f")},
            },
            [Label(("tolerance", "value"), assumed(TOLERANCE_REASON))],
            scorable=False,
        )
    levels = plan.declaration.get("levels")
    # Text only: the check refuses a BOOL column's values as categories (see _boolean).
    if kind is LogicalKind.STRING and isinstance(levels, dict) and levels:
        plan.declaration["support"] = {"values": list(levels)}
        plan.declaration["maxCardinality"] = len(levels)
        plan.labels.append(Label(("maxCardinality",), assumed(ASSUMED_CHECK_REASON)))
        plan.assumptions.append(ASSUMED_CHECK_REASON)
        return PropertyPlan(
            {
                "property": "support",
                "column": plan.name,
                "tolerance": {"type": "count", "max": 0},
            },
            [Label(("tolerance", "max"), assumed(TOLERANCE_REASON))],
            scorable=False,
        )
    return None
