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
import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from itertools import pairwise
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypeGuard, Union

from flux_engine.author.basis import Basis, Label, assumed, from_contract, from_profile
from flux_engine.author.fill import drawn_mean as _drawn_mean
from flux_engine.author.fill import part_mean, tilted_fill
from flux_engine.author.forms import SUPERSEDED, grouped
from flux_engine.author.ranges import RangeRule, grid_bounds
from flux_engine.core.canonical import JCS_MAX_SAFE_INTEGER
from flux_engine.core.clock import format_rfc3339_ms, parse_rfc3339_ms
from flux_engine.generator.statistics import integer_width_bounds
from flux_engine.seam.contract import CompiledColumn, LogicalKind
from flux_engine.seam.coverage import printable
from flux_stats.column_metrics import MAX, MIN, NULL_COUNT, QUANTILES
from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM, MEAN, QUANTILE_BINNED
from flux_stats.definitions import SUPPORT as SUPPORT_STATISTIC
from flux_stats.spelling import MOST_DIGITS

__all__ = [
    "DEFAULT_TOLERANCES",
    "MOST_INT_LEVELS",
    "MOST_PLACES",
    "OPAQUE_LEVEL_CAP",
    "ColumnContext",
    "ColumnPlan",
    "PropertyPlan",
    "assumed_check",
    "cell_weights",
    "count_cells",
    "declared_span",
    "copy_column",
    "determine_column",
    "draw_within_key",
    "PerEntity",
    "few_int_levels",
    "per_entity",
    "drawn_span",
    "plan_column",
    "recheck_key",
    "shape_to_mean",
    "SHAPED_REASON",
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
_QUANTILE_BINNED = QUANTILE_BINNED.identifier
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
    #: The disclosure rule's rounding base: a published count is off by less than this.
    rounding_base: int = 1
    #: The range the contract's ``validationRules`` declare for the column, if any.
    range_rule: Optional[RangeRule] = None
    #: An INT column drawn as a categorical over its few integer levels (:func:`few_int_levels`),
    #: so that another column can be drawn given it.
    int_levels: bool = False
    #: The levels consumer queries filter on (ADR 0028 section 5), each with the ids of the queries
    #: that do, sorted. A categorical column whose levels come from the profile or the contract
    #: declares them too, so a level the profile withholds still has its share (ADR 0014 section 3).
    asked_levels: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()

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
    #: A number key's checks against its layout were run (:func:`_key_range`), and the
    #: bundle runs them again once the key's span is final (:func:`recheck_key`).
    key_checked: bool = False
    #: Whether the key's published mean is checked against the span the bundle declares.
    key_mean: bool = False
    #: What those checks wrote to ``reads`` (True) and ``passed`` (False), to replace.
    key_notes: List[Tuple[bool, Tuple[str, ...], str]] = field(default_factory=list)
    #: The assumptions that are choices rather than gaps: how to meet what the profile
    #: does publish, or where the contract overrules it. ``--strict`` refuses every other
    #: assumption (ADR 0014 section 2).
    choices: List[str] = field(default_factory=list)
    #: The levels a categorical column draws, when they are real values (the contract's
    #: enum, the profile's levels); ``None`` for opaque levels and for other columns.
    level_names: Optional[Tuple[str, ...]] = None
    #: Why the column is never drawn given another, nor another given it: a key, a
    #: foreign key, an identifier the engine derives. ``None`` for every other column.
    fixed: Optional[str] = None
    #: The sentences that speak of the column's own weights, which a ``given`` replaces.
    weight_notes: List[str] = field(default_factory=list)
    #: An INT column drawn as a categorical over integer levels, written as canonical decimal text.
    int_levels: bool = False
    #: The profile publishes an ungrouped ``mean/1`` of the column, which a ``mean`` property scores.
    mean_published: bool = False


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
        if statistic.get("definition") != definition or grouped(statistic):
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


def count_cells(group: Mapping[str, Any], key: str) -> List["_Cell"]:
    """A count table's cells, each with its count when published (:func:`_cells`)."""
    return _cells(group, key)


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

    A timestamp is RFC 3339 text. A date is ``YYYY-MM-DD``, the one spelling the
    generator and the check both read for a DATE column; the profile holds a
    date as milliseconds since the epoch, so the day is the one they fall in. A
    number keeps its exact decimal text.
    """
    if kind is LogicalKind.TIMESTAMP:
        return format_rfc3339_ms(int(value))
    if kind is LogicalKind.DATE:
        return (_EPOCH + timedelta(days=int(value) // _DAY_MS)).isoformat()
    return format(value, "f")


_EPOCH = date(1970, 1, 1)


def _temporal(kind: LogicalKind) -> bool:
    return kind in (LogicalKind.DATE, LogicalKind.TIMESTAMP)


# ---------------------------------------------------------------------------
# The column, by kind
# ---------------------------------------------------------------------------


def plan_column(ctx: ColumnContext) -> ColumnPlan:
    """Declare one column from its profile entry and its contract declaration."""
    kind = ctx.column.logical_type.kind
    plan = ColumnPlan(name=ctx.column.name, mean_published=_ungrouped(ctx.entry, _MEAN) is not None)
    if ctx.key or ctx.keyed:
        plan.fixed = "a key the contract declares"
    elif ctx.foreign is not None:
        plan.fixed = "a foreign key"
    if ctx.key and kind is LogicalKind.STRING:
        plan.how = "an identifier: the contract makes it a key, and the engine derives it"
        return plan
    if kind is LogicalKind.STRING or (ctx.int_levels and kind is LogicalKind.INT):
        _categorical(ctx, plan)
        plan.int_levels = kind is LogicalKind.INT
    elif kind is LogicalKind.BOOL:
        _boolean(ctx, plan)
    else:
        _ordered(ctx, plan)
        _within_range(ctx, plan)
    if "unique" in plan.declaration:
        plan.fixed = "an identifier the engine derives"
    if "unique" not in plan.declaration and not (ctx.keyed or ctx.inner or ctx.nulls_follow):
        _null_fraction(ctx, plan)
    return plan


def _required_nulls(ctx: ColumnContext, plan: ColumnPlan, nulls: Any, count: Any) -> None:
    """What a ``required`` column makes of its published null count and count: checks only.

    It declares no ``nullFraction``. A published null count above zero is the
    contract and the data disagreeing, and the contract wins: the count is
    passed over, and the person is told.
    """
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


def _null_fraction(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """``nullFraction``: the published null count, or none assumed.

    A column the contract makes ``required`` declares none: it can hold no
    null, and the seam refuses a ``nullFraction`` on it (``FLX-3402``). If the
    profile counts nulls there, the contract and the data disagree, and the
    contract wins.

    A categorical column never declares one the profile does not give, nor a
    published share of zero: an absent ``nullFraction`` already draws no null,
    and a declared one, even ``'0'``, keeps the column from being the column
    another is drawn given (``FLX-3402``, ADR 0014 section 3).
    """
    categorical = ctx.int_levels or ctx.column.logical_type.kind in (
        LogicalKind.STRING,
        LogicalKind.BOOL,
    )
    nulls = _published(ctx.entry, "nullCount")
    count = _published(ctx.entry, "count")
    tokens = ("nullFraction",)
    if ctx.column.required:
        _required_nulls(ctx, plan, nulls, count)
        return
    if isinstance(nulls, int) and not isinstance(nulls, bool):
        total = nulls + count if isinstance(count, int) else ctx.rows
        if isinstance(count, int):
            _read(ctx, plan, "count", "with nullCount, the share of rows that hold a null")
        share = min(Fraction(nulls, total), Fraction(1)) if total else Fraction(0)
        if categorical and share == 0:
            plan.reads.append(
                (
                    _metric_tokens(ctx, "nullCount"),
                    "checked: the column holds no null, so it declares none",
                )
            )
            return
        plan.declaration["nullFraction"] = _share_text(share)
        plan.labels.append(
            Label(tokens, from_profile(ctx.profile_tokens + ("metrics", "nullCount"), _NULL_COUNT))
        )
        return
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
    if not categorical:
        plan.declaration["nullFraction"] = "0"
        plan.labels.append(Label(tokens, assumed(reason)))
    plan.assumptions.append(reason)


# --- categorical columns ----------------------------------------------------


_Found = Optional[Tuple[int, Mapping[str, Any], Mapping[str, Any]]]


def _scored_support(ctx: ColumnContext, plan: ColumnPlan, support_stat: _Found) -> None:
    """A ``support`` property, scored against the published ``support/1`` when there is one."""
    plan.properties.append(_support_property(ctx, scorable=support_stat is not None))
    if support_stat is not None:
        plan.properties[-1].reads.append(
            (
                ctx.profile_tokens + ("statistics", str(support_stat[0])),
                "the support property is scored against it",
            )
        )


def _enum_column(
    ctx: ColumnContext,
    plan: ColumnPlan,
    enum: List[str],
    levels_stat: _Found,
    support_stat: _Found,
) -> None:
    """A STRING column whose type is an ``enum``: its levels, weighted by what is published.

    A level histogram over exactly the enum weighs the levels and is the
    property the run is scored on; one over some of them weighs them too, the
    others sharing what its counts leave (:func:`_level_weights`); with none,
    the levels are drawn at equal weights.
    """
    plan.level_names = tuple(enum)
    plan.declaration["support"] = "declared"
    plan.declaration["maxCardinality"] = len(enum)
    plan.labels.append(
        Label(("maxCardinality",), from_contract(ctx.product_ref, ctx.contract_tokens + ("type",)))
    )
    listed = [str(level) for level in levels_stat[1].get("levels", ())] if levels_stat else []
    weighted = f"{len(enum)} levels the contract declares, weighted by the profile's counts"
    if levels_stat is not None and sorted(listed) == sorted(enum):
        _level_weights(ctx, plan, levels_stat)
        plan.how = weighted
        return
    if levels_stat is not None and set(listed) & set(enum):
        _level_weights(ctx, plan, levels_stat, declared=enum)
        plan.how = weighted
        _scored_support(ctx, plan, support_stat)
        return
    plan.how = f"{len(enum)} levels the contract declares, at equal weights"
    plan.assumptions.append(
        f"The profile publishes no counts for the {len(enum)} levels the contract "
        "declares, so they are drawn at equal weights."
    )
    plan.weight_notes.append(plan.assumptions[-1])
    if levels_stat is not None:
        plan.passed.append(
            (
                ctx.profile_tokens + ("statistics", str(levels_stat[0])),
                "its levels are not the levels the contract declares",
            )
        )
    _scored_support(ctx, plan, support_stat)


def _asked_how(extra: Sequence[str]) -> str:
    """What a column's one-line description adds for the levels only consumer queries name."""
    if not extra:
        return ""
    return f", and {_count_of(len(extra), 'level')} the consumer queries filter on"


def _asked_cardinality(ctx: ColumnContext, extra: Sequence[str], listed: int) -> str:
    """The reason of a column's ``maxCardinality`` when consumer queries add levels to it."""
    names = {query for level, queries in ctx.asked_levels if level in extra for query in queries}
    return (
        f"{asked_sentence(sorted(names))} The profile lists {listed} levels and the queries ask "
        f"for {len(extra)} more, so the column declares {listed + len(extra)}."
    )


def _categorical(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """A STRING column: declared levels, a published statistic, an identifier, or opaque levels."""
    enum = ctx.column.enum_values
    levels_stat = _ungrouped(ctx.entry, _LEVELS)
    support_stat = _ungrouped(ctx.entry, _SUPPORT)
    if enum:
        _enum_column(ctx, plan, list(enum), levels_stat, support_stat)
        return
    if levels_stat is not None:
        index, statistic, _group = levels_stat
        levels = [str(level) for level in statistic.get("levels", ())]
        extra = [level for level, _ in ctx.asked_levels if level not in levels]
        plan.level_names = tuple(levels + extra)
        plan.declaration["support"] = {"values": levels + extra}
        plan.declaration["maxCardinality"] = len(levels) + len(extra)
        plan.labels.append(
            Label(
                ("maxCardinality",),
                (
                    from_profile(ctx.profile_tokens + ("statistics", str(index), "levels"), _LEVELS)
                    if not extra
                    else assumed(_asked_cardinality(ctx, extra, len(levels)))
                ),
            )
        )
        _level_weights(ctx, plan, levels_stat, declared=levels + extra if extra else None)
        plan.how = f"{len(levels)} levels and their counts, from the profile" + _asked_how(extra)
        return
    if support_stat is not None:
        index, _statistic, group = support_stat
        members = [str(member) for member in group.get("support", ())]
        if members:
            listed_count = len(members)
            extra = [level for level, _ in ctx.asked_levels if level not in members]
            members = members + extra
            plan.level_names = tuple(members)
            plan.declaration["support"] = {"values": members}
            plan.declaration["maxCardinality"] = len(members)
            plan.labels.append(
                Label(
                    ("maxCardinality",),
                    (
                        from_profile(
                            ctx.profile_tokens
                            + ("statistics", str(index), "groups", "0", "support"),
                            _SUPPORT,
                        )
                        if not extra
                        else assumed(_asked_cardinality(ctx, extra, listed_count))
                    ),
                )
            )
            plan.how = f"{len(members)} levels the profile lists, at equal weights" + (
                _asked_how(extra)
            )
            plan.assumptions.append(
                "The profile lists the levels but not their counts, so they are drawn at "
                "equal weights."
            )
            plan.weight_notes.append(plan.assumptions[-1])
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


#: The most integer levels an INT column may be drawn over as the column another is drawn
#: given: the 64 distinct levels the generator's family table allows a categorical.
MOST_INT_LEVELS = 64


def few_int_levels(
    entry: Optional[Mapping[str, Any]], range_rule: Optional[RangeRule] = None
) -> Optional[Tuple[str, ...]]:
    """The integer levels an INT column's profile entry publishes, when there are few of them.

    A ``levelHistogram/1`` over the column, else a ``support/1``, lists its values. They
    are the levels only when there are at most :data:`MOST_INT_LEVELS`, each is a whole number
    written as canonical decimal text (no sign on zero, no leading zero, no plus), and each
    lies inside the range the contract declares for the column.
    """
    listed: Optional[List[str]] = None
    found = _ungrouped(entry, _LEVELS)
    if found is not None:
        listed = [str(level) for level in found[1].get("levels", ())]
    else:
        support = _ungrouped(entry, _SUPPORT)
        if support is not None:
            listed = [str(member) for member in support[2].get("support", ())]
    if not listed or len(listed) > MOST_INT_LEVELS or len(set(listed)) != len(listed):
        return None
    for text in listed:
        if re.fullmatch(r"0|-?[1-9][0-9]*", text) is None:
            return None
        value = int(text)
        if range_rule is not None and not _inside(range_rule, value):
            return None
    return tuple(listed)


def _inside(rule: RangeRule, value: int) -> bool:
    """Whether a whole number meets a contract's range rule."""
    if rule.low is not None and (value < rule.low or (rule.low_strict and value == rule.low)):
        return False
    return rule.high is None or not (value > rule.high or (rule.high_strict and value == rule.high))


@dataclass(frozen=True)
class PerEntity:
    """A column entry read per entity: the statistics a dependency's draw is made from."""

    #: The entry with each chosen per-entity statistic read as an ungrouped one, and the
    #: per-row statistics it replaces set aside.
    entry: Mapping[str, Any]
    #: The indexes of the per-entity statistics read.
    used: Tuple[int, ...]
    #: The indexes of the per-row statistics set aside.
    superseded: Tuple[int, ...]


def per_entity(entry: Optional[Mapping[str, Any]], keys: Sequence[str]) -> Optional[PerEntity]:
    """Read a column's statistics ``per`` *keys* in place of its per-row ones, when it has any.

    A contract ``dependency`` draws its ``to`` columns once per value of its ``from`` columns
    (EXPR-SPEC-2 section 8), so a statistic counted per those columns (PROFILE-SPEC.md section
    3, format 1-3-0) is the distribution the draw follows. Each such statistic that a rule
    can read once it is counted as an ungrouped one replaces the ungrouped statistics of its
    definition, which count rows. The statistics keep their places in the list, so a pointer
    into the profile still names the same one.
    """
    found = entry.get("statistics") if isinstance(entry, Mapping) else None
    if not isinstance(entry, Mapping) or not isinstance(found, list):
        return None
    statistics: List[Any] = list(found)
    used: List[int] = []
    superseded: List[int] = []
    for index, statistic in enumerate(found):
        if (
            not isinstance(statistic, Mapping)
            or "groupBy" in statistic
            or "groupByLink" in statistic
            or set(map(str, statistic.get("per", ()))) != set(keys)
            or not statistic.get("per")
        ):
            continue
        definition = statistic.get("definition")
        trial = list(statistics)
        replaced = [
            other
            for other, item in enumerate(statistics)
            if other != index
            and isinstance(item, Mapping)
            and item.get("definition") == definition
            and not grouped(item)
        ]
        for other in replaced:
            trial[other] = {**statistics[other], SUPERSEDED: True}
        trial[index] = {key: value for key, value in statistic.items() if key != "per"}
        probe = _ungrouped({**entry, "statistics": trial}, str(definition))
        if probe is None or probe[0] != index:
            continue
        statistics = trial
        used.append(index)
        superseded += replaced
    if not used:
        return None
    return PerEntity({**entry, "statistics": statistics}, tuple(used), tuple(sorted(superseded)))


def _group_total(group: Mapping[str, Any]) -> Optional[int]:
    """How many records a published group counts (``k``), when it says."""
    total = group.get("k")
    return total if isinstance(total, int) and not isinstance(total, bool) and total > 0 else None


def _level_weights(
    ctx: ColumnContext,
    plan: ColumnPlan,
    found: Tuple[int, Mapping[str, Any], Mapping[str, Any]],
    *,
    with_property: bool = True,
    declared: Optional[Sequence[str]] = None,
) -> None:
    """``levels`` from a published ``levelHistogram/1``, and, *with_property*, its property.

    With *declared* levels (a contract's ``enum``, or ``false`` and ``true``) the
    column draws exactly those: a declared level the histogram does not list
    shares the remainder with the withheld cells, and a listed level the contract
    does not declare is not drawn. The ``levelHistogram`` property needs the
    histogram's levels to be the declared ones, so it is written only then.
    """
    index, statistic, group = found
    listed = [str(level) for level in statistic.get("levels", ())]
    cells = _cells(group, "levelCounts")
    stat_tokens = ctx.profile_tokens + ("statistics", str(index))
    total = _group_total(group)
    absent = [level for level in declared if level not in listed] if declared else []
    outside = [level for level in listed if level not in declared] if declared else []
    asked = dict(ctx.asked_levels)
    absent_asked = [level for level in absent if level in asked]
    weights, bases = cell_weights(
        ctx.minimum_cell,
        cells,
        stat_tokens,
        "levelCounts",
        _LEVELS,
        total=total,
        absent=len(absent),
    )
    found_levels = listed + absent
    levels = list(declared) if declared else listed
    by_level = dict(zip(found_levels, zip(weights, bases, strict=True), strict=True))
    texts = share_texts([by_level[level][0] for level in levels])
    plan.declaration["levels"] = dict(zip(levels, texts, strict=True))
    for level in levels:
        basis = by_level[level][1]
        if level in absent_asked:
            basis = assumed(f"{asked_sentence(asked[level])} {basis.reason}")
        plan.labels.append(Label(("levels", level), basis))
    notes = _withheld_lines(
        ctx,
        cells,
        "level count",
        total=total,
        absent=[level for level in absent if level not in asked],
    )
    if absent_asked:
        notes.insert(0, _absent_line(absent_asked, total, by="the consumer queries ask for"))
    plan.assumptions.extend(notes)
    plan.weight_notes.extend(notes)
    if total is not None and (absent or any(cell.count is None for cell in cells)):
        plan.reads.append(
            (stat_tokens + ("groups", "0", "k"), "the records the withheld counts share")
        )
    if outside:
        names = ", ".join(printable(level) for level in outside)
        plan.assumptions.append(
            f"The profile counts levels the contract does not declare ({names}): they are not "
            "drawn."
        )
        plan.choices.append(plan.assumptions[-1])
    if not with_property or absent or outside:
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
                    "sampling": True,
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


def cell_weights(
    minimum_cell: int,
    cells: Sequence[_Cell],
    stat_tokens: Tuple[str, ...],
    key: str,
    definition: str,
    *,
    total: Optional[int] = None,
    absent: int = 0,
    group: str = "0",
) -> Tuple[List[Fraction], List[Basis]]:
    """Each cell's weight and basis, then one for each of *absent* levels the table does not list.

    A published cell is its count. The others, every withheld cell and every
    absent level, share what the published counts leave of the group's *total*
    records (``k``), so a level is never assumed to be zero (ADR 0014 section 3):
    a cell withheld as small (status ``A``) takes at most half the minimum cell,
    and the rest share the remainder evenly. When the remainder is none, or
    rounding left it below zero, each takes half a record. Only when *total* is
    unknown does the older rule hold: ``A`` is half the minimum cell, and any
    other withheld cell or absent level the mean of the published cells (or half
    the minimum cell when there are none).
    """
    published = [cell.count for cell in cells if cell.count is not None]
    unknown = [cell for cell in cells if cell.count is None] + [_Cell(None, _ABSENT)] * absent
    shares = _unknown_shares(minimum_cell, unknown, published, total)
    weights: List[Fraction] = []
    bases: List[Basis] = []
    filled = iter(shares)
    for position, cell in enumerate(cells):
        if cell.count is not None:
            weights.append(Fraction(cell.count))
            bases.append(
                from_profile(stat_tokens + ("groups", group, key, str(position)), definition)
            )
            continue
        weight, reason = next(filled)
        weights.append(weight)
        bases.append(assumed(reason))
    for weight, reason in filled:
        weights.append(weight)
        bases.append(assumed(reason))
    return weights, bases


#: The status an absent level is filled under: it is not a cell of the table at all.
_ABSENT = "absent"


def _unknown_shares(
    minimum_cell: int,
    unknown: Sequence[_Cell],
    published: Sequence[int],
    total: Optional[int],
) -> List[Tuple[Fraction, str]]:
    """The weight and reason of each cell the table does not publish, in order."""
    if not unknown:
        return []
    if total is not None:
        remainder = Fraction(total - sum(published))
        if remainder <= 0:
            return [(Fraction(1, 2), _NOTHING_LEFT_REASON)] * len(unknown)
        small = [cell for cell in unknown if cell.status == _SMALL]
        each = remainder / len(unknown)
        small_each = min(Fraction(minimum_cell, 2), each)
        others = len(unknown) - len(small)
        rest = (remainder - small_each * len(small)) / others if others else Fraction(0)
        shares: List[Tuple[Fraction, str]] = []
        for cell in unknown:
            if cell.status == _SMALL:
                shares.append((small_each, _SMALL_SHARE_REASON))
            else:
                shares.append(
                    (rest, _ABSENT_REASON if cell.status == _ABSENT else _REMAINDER_REASON)
                )
        return shares
    typical = _typical([_Cell(count, _FREE) for count in published])
    shares = []
    for cell in unknown:
        status = printable(cell.status)
        if cell.status == _SMALL:
            shares.append(
                (
                    Fraction(minimum_cell, 2),
                    f"The profile withheld this count as a small one (status {status}): it lies "
                    f"below the minimum cell ({minimum_cell}), so it is assumed to be half "
                    "of it.",
                )
            )
        elif typical is not None:
            shares.append(
                (
                    Fraction(typical),
                    "The profile does not publish this count, nor how many records the table "
                    "holds, so it is assumed to be the mean of the counts the table publishes, "
                    "to the nearest whole count.",
                )
            )
        else:
            shares.append(
                (
                    Fraction(minimum_cell, 2),
                    "The profile does not publish this count, nor how many records the table "
                    f"holds, nor any other count: it is assumed to be half the minimum cell "
                    f"({minimum_cell}).",
                )
            )
    return shares


_REMAINDER_REASON = (
    "The profile withheld this count: it shares, evenly with the other counts the table does "
    "not publish, the records the published counts leave of the table's total, an estimate "
    "since a rule may round each count and the total on their own."
)
_ABSENT_REASON = (
    "The profile's table does not list this level: it shares, evenly with the counts the "
    "table does not publish, the records the published counts leave of the table's total, an "
    "estimate since a rule may round each count and the total on their own."
)
_SMALL_SHARE_REASON = (
    "The profile withheld this count as a small one (status A): it takes an even share of the "
    "records the published counts leave of the table's total, and no more than half the "
    "minimum cell."
)
_NOTHING_LEFT_REASON = (
    "The profile does not publish this count, and the published counts leave no record of "
    "the table's total, so it is assumed to be half a record: a level is never assumed to "
    "be zero."
)


def asked_sentence(queries: Sequence[str]) -> str:
    """The first words of a reason that names the consumer queries filtering on a level."""
    shown = ", ".join(repr(printable(name)) for name in queries[:3])
    more = f" and {len(queries) - 3} more" if len(queries) > 3 else ""
    noun = "query" if len(queries) == 1 else "queries"
    return (
        f"The consumer {noun} {shown}{more} filter{'s' if len(queries) == 1 else ''} on this level."
    )


def _absent_line(
    absent: Sequence[str], total: Optional[int], by: str = "the contract declares"
) -> str:
    """What a person is told about the declared levels a count table does not list."""
    names = ", ".join(printable(level) for level in absent)
    many = len(absent) != 1
    if total is not None:
        what = ("they share" if many else "it shares") + (
            " what the published counts leave of the table's records."
        )
    else:
        what = ("each is" if many else "it is") + " assumed to be the mean of the published counts."
    return f"The profile's counts do not list {names}, which {by}: {what}"


def _withheld_lines(
    ctx: ColumnContext,
    cells: Sequence[_Cell],
    noun: str,
    *,
    total: Optional[int] = None,
    absent: Sequence[str] = (),
) -> List[str]:
    """What a person is told about a count table's withheld cells and absent levels."""
    lines = [_absent_line(absent, total)] if absent else []
    small = sum(1 for cell in cells if cell.count is None and cell.status == _SMALL)
    other = sum(1 for cell in cells if cell.count is None and cell.status != _SMALL)
    if total is not None:
        if small or other:
            withheld = small + other
            lines.append(
                f"The profile withheld {_count_of(withheld, noun)}; "
                + ("they share" if withheld != 1 else "it shares")
                + " what the published counts leave of the table's records"
                + (", a small one no more than half the minimum cell." if small else ".")
            )
        return lines
    return lines + _status_lines(ctx, cells, noun, small, other)


def _status_lines(
    ctx: ColumnContext, cells: Sequence[_Cell], noun: str, small: int, other: int
) -> List[str]:
    """The 0.7.0 rule's lines, for a table that publishes no total: by withheld status."""
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
    listed = [str(level) for level in found[1].get("levels", ())] if found is not None else []
    plan.level_names = ("false", "true")
    if found is not None and listed == ["false", "true"]:
        _level_weights(ctx, plan, found, with_property=False)
        plan.how = "false and true, weighted by the profile's counts"
        return
    if found is not None and set(listed) & {"false", "true"}:
        _level_weights(ctx, plan, found, with_property=False, declared=["false", "true"])
        plan.how = "false and true, weighted by the profile's counts"
        return
    plan.level_names = ("false", "true")
    reason = "The profile publishes no counts of false and true: each is drawn half the time."
    plan.declaration["levels"] = {"false": "0.5", "true": "0.5"}
    plan.labels.append(Label(("levels",), assumed(reason)))
    plan.how = "false and true, at equal weights"
    plan.assumptions.append(reason)
    plan.weight_notes.append(reason)


# --- ordered columns: numbers, dates and timestamps -------------------------


@dataclass(frozen=True)
class _Point:
    """A published order statistic: its level, its value and where the profile holds it."""

    level: Fraction
    value: Decimal
    tokens: Tuple[str, ...]
    definition: str
    #: A binned quantile's point: the statistic's edge ladder and this edge's place on it.
    ladder: Tuple[Decimal, ...] = ()
    index: int = -1
    #: A binned quantile's point: the records below the value, of the group's ``k``.
    below: int = -1
    total: int = -1


def _is_count(value: Any) -> TypeGuard[int]:
    """Whether *value* is a JSON integer: a bool is not a count."""
    return isinstance(value, int) and not isinstance(value, bool)


def _binned_points(ctx: ColumnContext) -> List[_Point]:
    """The two points of every published ``quantile/2`` group of the column.

    A group says ``binBelow`` of its ``k`` records lie below edge ``bins[i]`` and
    ``binThrough`` below ``bins[i + 1]``, so it is two points of the distribution
    (PROFILE-SPEC section 4). The ladder's first and last edges are skipped: they
    are the type's extremes and say nothing of where the values lie. A key keeps its
    own rules and reads none; a foreign key reads them as any column does.
    """
    if ctx.key or ctx.keyed:
        return []
    return _binned_points_of(ctx.entry, ctx.profile_tokens)


def _binned_points_of(
    entry: Optional[Mapping[str, Any]], profile_tokens: Tuple[str, ...]
) -> List[_Point]:
    """The points of every published ``quantile/2`` group of a column's *entry*."""
    points: List[_Point] = []
    for index, statistic in _statistics(entry):
        if statistic.get("definition") != _QUANTILE_BINNED or grouped(statistic):
            continue
        groups, bins = statistic.get("groups"), statistic.get("bins")
        if not isinstance(groups, list) or len(groups) != 1 or not isinstance(bins, list):
            continue
        group = groups[0]
        if not isinstance(group, Mapping) or group.get("status") != _FREE:
            continue
        try:
            ladder = tuple(Decimal(str(edge)) for edge in bins)
        except ArithmeticError:
            continue
        k, current, below, through = (
            group.get(key) for key in ("k", "quantileBin", "binBelow", "binThrough")
        )
        if not (_is_count(k) and _is_count(current) and _is_count(below) and _is_count(through)):
            continue
        if not (
            len(ladder) >= 2
            and all(low < high for low, high in pairwise(ladder))
            and k > 0
            and 0 <= current < len(ladder) - 1
            and 0 <= below <= through <= k
        ):
            continue
        tokens = profile_tokens + ("statistics", str(index))
        for edge, count in ((current, below), (current + 1, through)):
            if 0 < edge < len(ladder) - 1:
                points.append(
                    _Point(
                        Fraction(count, k),
                        ladder[edge],
                        tokens + ("bins", str(edge)),
                        _QUANTILE_BINNED,
                        ladder,
                        edge,
                        count,
                        k,
                    )
                )
    return points


def binned_cuts(
    entry: Optional[Mapping[str, Any]], profile_tokens: Tuple[str, ...]
) -> List[Tuple[Decimal, Fraction, Tuple[str, ...]]]:
    """The cuts of a column's binned quantiles: a value, the share of rows below it, its place.

    The points :func:`_points` reads from ``quantile/2``, put through the histogram helper:
    ascending, one per distinct value, no share below an earlier one. A foreign key reads
    them too, as its own buckets. Fewer than two cuts bound no bucket: they give none.
    """
    points = _binned_points_of(entry, profile_tokens)
    if len({point.value for point in points}) < 2:
        return []
    edges, _ = _histogram(points)
    return [(edge.value, edge.level, edge.tokens) for edge in edges]


def _points(ctx: ColumnContext) -> List[_Point]:
    """The published ``min``, quartiles, ``max`` and binned quantiles, in level order."""
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
    points.extend(_within(ctx, _binned_points(ctx), low, high))
    points.sort(key=lambda point: point.level)
    return points


def _within(ctx: ColumnContext, binned: Sequence[_Point], low: Any, high: Any) -> List[_Point]:
    """The binned points that say where the values lie, kept to what a row can hold.

    A published ``min`` or ``max`` is exact: a ladder edge beyond it only says
    that no record lies past it, and is dropped. An edge past the values the
    column's type carries (the ladder always has one at a DECIMAL's ``10^(p-s)``)
    bounds them all the same, so it becomes the last value the type holds.
    """
    lowest = Decimal(str(low)) if low is not None else None
    highest = Decimal(str(high)) if high is not None else None
    limit = _limit(ctx)
    kept: List[_Point] = []
    for point in binned:
        if (lowest is not None and point.value < lowest) or (
            highest is not None and point.value > highest
        ):
            continue
        if limit is not None and abs(Fraction(point.value)) > limit:
            end = _on_grid(limit, _grid(ctx))
            point = replace(point, value=end if point.value > 0 else -end)
        kept.append(point)
    return kept


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
        _placeholder(ctx, plan, _BEYOND_REASON)
        plan.how = (
            "a placeholder, evenly from 0: the profile's values are beyond what a row carries"
        )
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
    if ctx.foreign is not None and numeric:
        _foreign_spread(ctx, plan, points)
        return
    if points and _temporal(kind):
        _one_value(ctx, plan, points[-1])
        return
    if points and _one_bound(ctx, plan, points):
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
    "The profile publishes values further from zero than the column's type holds (the "
    "integers a run's rows carry exactly stop at 2^53, a DECIMAL at its digits), so none of "
    "them is declared: the values are a placeholder, drawn evenly over the bins declared "
    "here, from 0."
)

#: How many grid values a placeholder spans when nothing says how many.
_PLACEHOLDER_VALUES = 100


def _limit(ctx: ColumnContext) -> Optional[Fraction]:
    """How far from zero a value of the column may lie: a row's integers, a decimal's digits.

    An integer column also keeps to the width its contract declares (``smallint``, ``integer``).
    """
    logical = ctx.column.logical_type
    if logical.kind is LogicalKind.INT:
        width = integer_width_bounds(ctx.column)
        if width is None:
            return Fraction(JCS_MAX_SAFE_INTEGER)
        return Fraction(min(JCS_MAX_SAFE_INTEGER, width[1], -width[0]))
    if logical.kind is LogicalKind.DECIMAL and isinstance(logical.precision, int):
        digits = logical.precision - _scale(ctx)
        if digits > MOST_DIGITS:
            # No number a fidelity block may spell reaches it, and 10^digits is no
            # number to work out for a type declared with a precision of millions.
            return None
        return Fraction(10) ** digits - Fraction(_grid(ctx))
    return None


def _span_bins(
    ctx: ColumnContext, start: Fraction, values: int, *, upward: bool = True
) -> Tuple[Fraction, Fraction]:
    """*values* grid values from *start*, away from it, kept within the column's type."""
    step = Fraction(_grid(ctx))
    far = start + (values - 1) * step if upward else start - (values - 1) * step
    limit = _limit(ctx)
    if limit is not None:
        far = max(min(far, limit), -limit)
    # At the type's limit the span keeps to the bound: (start, start), one value.
    return (start, far) if upward else (far, start)


def _even_bins(
    ctx: ColumnContext, plan: ColumnPlan, low: Fraction, high: Fraction, basis: Basis
) -> None:
    """Declare one bin from *low* to *high*, drawn evenly, both resting on *basis*.

    A span the column's type leaves no room for (*low* equal to *high*, a bound
    at the type's limit) is that one value.
    """
    step = _grid(ctx)
    if high <= low:
        plan.declaration["levels"] = {format(_on_grid(low, step), "f"): "1"}
        plan.labels.append(Label(("levels",), basis))
        return
    plan.declaration["bins"] = [format(_on_grid(low, step), "f"), format(_on_grid(high, step), "f")]
    plan.declaration["binWeights"] = ["1"]
    plan.labels.append(Label(("bins",), basis))
    plan.labels.append(Label(("binWeights",), basis))


def _placeholder(ctx: ColumnContext, plan: ColumnPlan, reason: str, *, sized: bool = True) -> None:
    """A spread the profile gives nothing for: evenly over values from 0, never one value.

    As many grid values as a published ``distinctCount`` says, or a hundred, kept
    within what the column's type holds (ADR 0014 section 3). The evidence lists
    it with every other assumption, and a foreign key placed under its parent
    later draws over the parent's keys instead (:func:`draw_within_key`).
    """
    distinct = _published(ctx.entry, "distinctCount") if sized else None
    values = distinct if isinstance(distinct, int) and distinct >= 2 else _PLACEHOLDER_VALUES
    if values != _PLACEHOLDER_VALUES:
        _read(ctx, plan, "distinctCount", "how many values the placeholder spans")
    low, high = _span_bins(ctx, Fraction(0), min(values, _PLACEHOLDER_VALUES * 10))
    _even_bins(ctx, plan, low, high, assumed(reason))
    plan.how = "a placeholder, evenly from 0"
    plan.assumptions.append(reason)
    plan.placeholder = True


#: Why a foreign key's own metrics do not shape its draw.
FOREIGN_OWN = (
    "a foreign key the profile gives no spread for is drawn evenly over the keys of its "
    "parent; this statistic of its own values does not shape that draw"
)


def _foreign_spread(ctx: ColumnContext, plan: ColumnPlan, points: Sequence[_Point]) -> None:
    """A number foreign key with no histogram and no two order statistics: over its parent's keys.

    A lone ``min`` or ``max`` of a foreign key, or its mean, says where the busiest
    parents lie, not how the children spread over them, so the key is never one
    value: it is a placeholder the bundle then lays over the span of the key it
    references (:func:`draw_within_key`), and its own statistics are passed over.
    """
    for point in points:
        plan.passed.append((point.tokens, FOREIGN_OWN))
    for name in ("sum", "distinctCount"):
        if _published(ctx.entry, name) is not None:
            plan.passed.append((_metric_tokens(ctx, name), FOREIGN_OWN))
    found = _ungrouped(ctx.entry, _MEAN)
    if found is not None:
        plan.passed.append((ctx.profile_tokens + ("statistics", str(found[0])), FOREIGN_OWN))
    _placeholder(ctx, plan, _PLACEHOLDER_REASON, sized=False)
    plan.how = "a foreign key, drawn over the span of the key it references"


#: Why a column with one published bound and a mean is drawn as it is.
_FILL_REASON = (
    "The profile publishes one bound and the mean, and no other order statistic: the values "
    "are drawn by the distribution of largest entropy with that bound and that mean, a "
    "geometric tilt over the bins declared here, out to where less than half a row lies "
    "beyond them (ADR 0014)."
)
_SPAN_FROM_BOUND = (
    "The profile publishes one bound and the number of distinct values, and no mean: the "
    "values are drawn evenly over that many grid values from the bound."
)
_PLACEHOLDER_FROM_BOUND = (
    "The profile publishes one bound and no mean or count of distinct values: the values are "
    "a placeholder, drawn evenly over the bins declared here, from the bound."
)
_ONE_VALUE_MEAN = (
    "The profile publishes a bound and a mean equal to it, so every value is that bound."
)


def _one_bound(ctx: ColumnContext, plan: ColumnPlan, points: Sequence[_Point]) -> bool:
    """A number column with one published value: a bound, combined with the mean and distinctCount.

    A ``min`` and a ``max`` that are one value, or a mean equal to the one bound,
    say every value is that value. A lone ``min`` or ``max`` with a mean on its
    far side is filled by maximum entropy (:func:`~flux_engine.author.fill.tilted_fill`);
    with ``distinctCount`` and no mean, that many grid values from it are drawn
    evenly; with neither, a placeholder starts there. A lone quantile bounds
    nothing: ``False``, and the column is drawn from its mean.
    """
    levels = {point.level for point in points}
    if levels >= {Fraction(0), Fraction(1)}:
        _one_value(ctx, plan, next(point for point in points if point.level == 1))
        return True
    if not levels & {Fraction(0), Fraction(1)}:
        for quantile in points:
            plan.passed.append((quantile.tokens, "a lone quantile bounds no span"))
        return False
    point = next(point for point in points if point.level in (Fraction(0), Fraction(1)))
    for other in points:
        if other is not point:
            plan.reads.append((other.tokens, "checked: equal to the published bound"))
    upward = point.level == 0  # a published minimum: the values lie above it
    bound = Fraction(point.value)
    mean = _published_mean(ctx)
    step = Fraction(_grid(ctx))
    counted = _published(ctx.entry, "distinctCount")
    if counted == 1:
        _one_value(ctx, plan, point)
        _read(ctx, plan, "distinctCount", "one distinct value: every row takes the bound")
        return True
    distinct = counted if isinstance(counted, int) and counted >= 2 else None
    edge = from_profile(point.tokens, point.definition)
    if mean is not None and mean == bound:
        plan.declaration["levels"] = {format(_on_grid(bound, _grid(ctx)), "f"): "1"}
        plan.labels.append(Label(("levels",), assumed(_ONE_VALUE_MEAN)))
        _read_mean(ctx, plan, "equal to the published bound: every value is the bound")
        plan.reads.append((point.tokens, "every value is this bound"))
        plan.how = "one value on every row: the published mean is the published bound"
        plan.assumptions.append(_ONE_VALUE_MEAN)
        return True
    if mean is not None and (mean > bound) == upward:
        filled = tilted_fill(
            bound,
            mean,
            step,
            ctx.rows,
            distinct=distinct,
            limit=_limit(ctx),
            bounds=_range_bounds(ctx),
        )
        if filled is not None:
            edges, weights = filled
            _declare_fill(
                ctx, plan, edges, weights, edge if upward else None, edge if not upward else None
            )
            _read_mean(ctx, plan, "the mean the fill is tilted to")
            if distinct is not None:
                spanned = (edges[-1] - edges[0]) / step + 1
                if spanned >= distinct:
                    _read(ctx, plan, "distinctCount", "the fewest grid values the fill spans")
                else:
                    plan.passed.append(
                        (
                            _metric_tokens(ctx, "distinctCount"),
                            "the mean lies too near the bound for a fill of that many values",
                        )
                    )
            plan.how = "the largest-entropy spread from the published bound with its mean"
            plan.assumptions.append(_FILL_REASON)
            return True
    if mean is not None:
        for tokens in _mean_sources(ctx):
            plan.passed.append((tokens, "the mean lies beyond the published bound"))
    if distinct is not None:
        low, high = _span_bins(ctx, bound, distinct, upward=upward)
        reason = _SPAN_FROM_BOUND
        _read(ctx, plan, "distinctCount", "how many grid values the span holds")
    else:
        low, high = _span_bins(ctx, bound, _PLACEHOLDER_VALUES, upward=upward)
        reason = _PLACEHOLDER_FROM_BOUND
    _even_bins(ctx, plan, low, high, assumed(reason))
    plan.labels = [label for label in plan.labels if label.tokens != ("bins",)]
    plan.labels.append(Label(("bins", "0" if upward else "1"), edge))
    plan.labels.append(Label(("bins", "1" if upward else "0"), assumed(reason)))
    plan.how = "evenly from the published bound"
    plan.assumptions.append(reason)
    return True


def _declare_fill(
    ctx: ColumnContext,
    plan: ColumnPlan,
    edges: Sequence[Fraction],
    weights: Sequence[Fraction],
    low: Optional[Basis],
    high: Optional[Basis],
) -> None:
    """Declare a fill's bins and weights: a published bound cited, every other number assumed."""
    step = _grid(ctx)
    plan.declaration["bins"] = [format(_on_grid(edge, step), "f") for edge in edges]
    plan.declaration["binWeights"] = share_texts(list(weights))
    for position in range(len(edges)):
        if position == 0 and low is not None:
            basis = low
        elif position == len(edges) - 1 and high is not None:
            basis = high
        else:
            basis = assumed(_FILL_REASON)
        plan.labels.append(Label(("bins", str(position)), basis))
    plan.labels.append(Label(("binWeights",), assumed(_FILL_REASON)))


def _beyond_plan(ctx: ColumnContext) -> bool:
    """Whether a number column publishes a value its rows cannot carry.

    A run writes its rows as canonical JSON, whose integers stop at 2^53 - 1
    (RFC 8785), so an INT value drawn beyond it is refused (``FLX-1003``); a
    DECIMAL value with more integer digits than its precision leaves is refused
    too (``FLX-3003``). Either way no published value of the column is declared.
    """
    limit = _limit(ctx)
    if ctx.column.logical_type.kind not in (LogicalKind.INT, LogicalKind.DECIMAL) or limit is None:
        return False
    values = [Fraction(point.value) for point in _points(ctx)]
    found = _ungrouped(ctx.entry, _HISTOGRAM)
    if found is not None:
        values += [Fraction(Decimal(str(edge))) for edge in found[1].get("bins", ())]
    mean = _published_mean(ctx)
    if mean is not None:
        values.append(mean)
    return any(abs(value) > limit for value in values)


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
    total = _group_total(group)
    weights, bases = cell_weights(
        ctx.minimum_cell, cells, stat_tokens, "binCounts", _HISTOGRAM, total=total
    )
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
    plan.assumptions.extend(_withheld_lines(ctx, cells, "bin count", total=total))
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
                        "sampling": True,
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

    Every ordered kind can: a profile holds a DATE column as a timestamp in
    milliseconds, and the check reads those as days for a DATE column whose
    bounds are spelled ``YYYY-MM-DD`` (:func:`_spell`).
    """
    return kind in (LogicalKind.INT, LogicalKind.DECIMAL, LogicalKind.DATE, LogicalKind.TIMESTAMP)


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
    # Counts are rounded, so two points can disagree: a level never falls.
    for position in range(1, len(edges)):
        floor = edges[position - 1].level
        if edges[position].level < floor:
            edges[position] = replace(edges[position], level=floor)
    weights = [high.level - low.level for low, high in pairwise(edges)]
    weights[0] += edges[0].level
    weights[-1] += 1 - edges[-1].level
    return edges, weights


def _sum_mean(ctx: ColumnContext) -> Optional[Fraction]:
    """The mean ``sum / count`` gives, or ``None`` when the profile publishes either alone."""
    total, count = _published(ctx.entry, "sum"), _published(ctx.entry, "count")
    if total is not None and isinstance(count, int) and count > 0:
        return Fraction(Decimal(str(total))) / count
    return None


def _stated_mean(ctx: ColumnContext) -> Optional[Tuple[int, Fraction]]:
    """The ungrouped ``mean/1`` statistic's index and mean on the profile's grid, or ``None``."""
    found = _ungrouped(ctx.entry, _MEAN)
    if found is None:
        return None
    group = found[2]
    k, total_on_grid = group.get("k"), group.get("Sx")
    if isinstance(k, int) and k > 0 and isinstance(total_on_grid, str):
        return found[0], Fraction(int(total_on_grid), k * 2**ctx.fractional_bits)
    return None


def _stated_mean_leads(ctx: ColumnContext) -> bool:
    """Whether the ``mean/1`` statistic, not ``sum / count``, is the mean the column is drawn to.

    It leads when it is the one published, or when it differs from ``sum / count`` by
    more than a grid step. A ``mean/1`` is what the ``mean`` property scores, and an
    overlay that restates it is a what-if on the mean, so a run drawn to the other
    number would fail its own check.
    """
    stated, by_sum = _stated_mean(ctx), _sum_mean(ctx)
    if stated is None:
        return False
    return by_sum is None or abs(stated[1] - by_sum) > Fraction(1, 2**ctx.fractional_bits)


def _published_mean(ctx: ColumnContext) -> Optional[Fraction]:
    """The column's published mean: ``sum / count``, else a ``mean/1`` statistic, else ``None``.

    A ``mean/1`` that differs from ``sum / count`` by more than a grid step leads, so the
    number the ``mean`` property scores is the number the column is drawn to.
    """
    stated = _stated_mean(ctx)
    if stated is not None and _stated_mean_leads(ctx):
        return stated[1]
    return _sum_mean(ctx)


def _mean_sources(ctx: ColumnContext) -> List[Tuple[str, ...]]:
    """Where the published mean is read: ``sum`` and ``count``, a ``mean/1``, or both when they agree.

    :func:`_published_mean` takes the ``mean/1`` when it disagrees with ``sum / count``,
    and ``sum / count`` otherwise; an agreeing ``mean/1`` beside ``sum / count`` is read
    too, so either way the ledger says what became of each.
    """
    stated = _stated_mean(ctx)
    cited = ctx.profile_tokens + ("statistics", str(stated[0])) if stated is not None else None
    if cited is not None and _stated_mean_leads(ctx):
        return [cited]
    sources: List[Tuple[str, ...]] = []
    if _sum_mean(ctx) is not None:
        sources += [_metric_tokens(ctx, "sum"), _metric_tokens(ctx, "count")]
    if cited is not None:
        sources.append(cited)
    return sources


def _read_mean(ctx: ColumnContext, plan: ColumnPlan, how: str) -> None:
    """Record that the rules read the column's published mean, wherever it came from."""
    plan.reads.extend((tokens, how) for tokens in _mean_sources(ctx))


#: How many halvings find the share that shapes a histogram to its mean: the share is
#: then known to 2^-64, far finer than one grid step of any bin.
_BISECTIONS = 64


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


#: How the part beyond a binned quantile's outermost point is closed, per end (ADR 0014).
_LADDER_REASON = (
    "The profile withheld {end}: the rows {side} its outermost binned quantile are assumed to "
    "lie in the next bin of the edge ladder the profile counts over, drawn evenly."
)
_TAIL_REASON = (
    "The profile withheld {end} and publishes the mean: the rows {side} its outermost binned "
    "quantile are drawn by the distribution of largest entropy with the mean the rest leaves, "
    "a geometric tilt over the bins declared here, out to where less than half a row lies "
    "beyond them (ADR 0014)."
)


@dataclass
class _Closed:
    """The bins over the published points, with the ends a profile withheld closed."""

    values: List[Decimal]
    weights: List[Fraction]
    edge_bases: List[Basis]
    #: A basis per bin's weight, or ``None`` where every weight rests on one basis.
    weight_bases: Optional[List[Optional[Basis]]] = None
    #: The mean was used to fill a tail.
    mean_used: bool = False
    #: The sentences of the ends closed, each an assumption.
    notes: List[str] = field(default_factory=list)
    low_closed: bool = False
    high_closed: bool = False


def _tail_fill(
    ctx: ColumnContext, closed: _Closed, edges: Sequence[_Point], high: bool, mean: Fraction
) -> Optional[_Closed]:
    """The part beyond the outermost point, as the largest-entropy fill with the mean the rest leaves."""
    point = edges[-1] if high else edges[0]
    share = (1 - point.level) if high else point.level
    step = Fraction(_grid(ctx))
    if share <= 0:
        return None
    raw = Fraction(point.value)
    snapped = (math.ceil(raw / step) if high else math.floor(raw / step)) * step
    bounds = [Fraction(value) for value in closed.values]
    bounds[-1 if high else 0] = snapped
    if any(low >= up for low, up in pairwise(bounds)):
        return None
    weights = list(closed.weights)
    weights[-1 if high else 0] -= share
    last = len(weights) - 1
    inner = sum(
        weight * part_mean(low, up, step, position == last and not high)
        for position, (weight, low, up) in enumerate(
            zip(weights, bounds[:-1], bounds[1:], strict=True)
        )
    )
    rest = (mean - inner) / share
    if (rest <= snapped) if high else (rest >= snapped):
        return None
    filled = tilted_fill(
        snapped, rest, step, ctx.rows, limit=_limit(ctx), bounds=_range_bounds(ctx)
    )
    if filled is None:
        return None
    fill_edges, fill_weights = filled
    total = sum(fill_weights, Fraction(0))
    scaled = [weight * share / total for weight in fill_weights]
    if high:
        all_edges, all_weights = bounds + fill_edges[1:], weights + scaled
    else:
        all_edges, all_weights = fill_edges[:-1] + bounds, scaled + weights
    if abs(_drawn_mean(all_edges, all_weights, step) - mean) > step:
        return None
    grid = _grid(ctx)
    exact = [_on_grid(edge, grid) for edge in fill_edges]
    grown = len(fill_edges) - 1
    reason = _TAIL_REASON.format(end="max" if high else "min", side="above" if high else "below")
    assumption = assumed(reason)
    kept: List[Optional[Basis]] = list(closed.weight_bases or [None] * len(closed.weights))
    new: List[Optional[Basis]] = [assumption] * (len(fill_edges) - 1)
    if high:
        values = closed.values[:-1] + exact
        edge_bases = closed.edge_bases + [assumption] * grown
        weight_bases = kept + new
    else:
        values = exact + closed.values[1:]
        edge_bases = [assumption] * grown + closed.edge_bases
        weight_bases = new + kept
    return replace(
        closed,
        values=values,
        weights=all_weights,
        edge_bases=edge_bases,
        weight_bases=weight_bases,
        mean_used=True,
        notes=closed.notes + [reason],
    )


def _ladder_bin(
    ctx: ColumnContext, closed: _Closed, edges: Sequence[_Point], high: bool
) -> Optional[_Closed]:
    """The part beyond the outermost point, in the next bin out of the profile's own ladder."""
    point = edges[-1] if high else edges[0]
    share = (1 - point.level) if high else point.level
    nearer = point.index + 1 if high else point.index - 1
    if share <= 0 or not 0 < nearer < len(point.ladder) - 1:
        return None
    edge = point.ladder[nearer]
    limit = _limit(ctx)
    numeric = ctx.column.logical_type.kind in (LogicalKind.INT, LogicalKind.DECIMAL)
    if numeric and limit is not None and abs(Fraction(edge)) > limit:
        return None
    bounds = _range_bounds(ctx)
    if bounds is not None and (
        (bounds[0] is not None and Fraction(edge) < bounds[0])
        or (bounds[1] is not None and Fraction(edge) > bounds[1])
    ):
        return None
    reason = _LADDER_REASON.format(end="max" if high else "min", side="above" if high else "below")
    assumption = assumed(reason)
    weights = list(closed.weights)
    weights[-1 if high else 0] -= share
    bases: List[Optional[Basis]] = list(closed.weight_bases or [None] * len(weights))
    one: List[Optional[Basis]] = [assumption]
    if high:
        return replace(
            closed,
            values=closed.values + [edge],
            weights=weights + [share],
            edge_bases=closed.edge_bases + [assumption],
            weight_bases=bases + one,
            notes=closed.notes + [reason],
        )
    return replace(
        closed,
        values=[edge] + closed.values,
        weights=[share] + weights,
        edge_bases=[assumption] + closed.edge_bases,
        weight_bases=one + bases,
        notes=closed.notes + [reason],
    )


def _close_ends(
    ctx: ColumnContext,
    edges: Sequence[_Point],
    weights: Sequence[Fraction],
    missing: Sequence[bool],
) -> _Closed:
    """Close each end the profile withheld whose outermost point is a binned quantile.

    *missing* says whether the low and the high end are withheld. A number column
    with a published mean and only one end withheld fills that part by maximum
    entropy; otherwise the part lies in the next bin of the ladder; an end neither
    closes is left to the outermost bin.
    """
    closed = _Closed(
        [edge.value for edge in edges],
        list(weights),
        [from_profile(edge.tokens, edge.definition) for edge in edges],
    )
    mean = _published_mean(ctx)
    numeric = ctx.column.logical_type.kind in (LogicalKind.INT, LogicalKind.DECIMAL)
    for high in (False, True):
        point = edges[-1] if high else edges[0]
        if not missing[1 if high else 0] or point.definition != _QUANTILE_BINNED:
            continue
        # The end beyond the point is withheld: its share is not in the outermost bin.
        grown = None
        if numeric and mean is not None and sum(missing) == 1:
            grown = _tail_fill(ctx, closed, edges, high, mean)
        if grown is None:
            grown = _ladder_bin(ctx, closed, edges, high)
        if grown is not None:
            closed = replace(grown, high_closed=True) if high else replace(grown, low_closed=True)
    return closed


def _read_binned(ctx: ColumnContext, plan: ColumnPlan, binned: Sequence[_Point]) -> None:
    """Record every ``quantile/2`` statistic whose points the bins are laid over."""
    seen: List[Tuple[str, ...]] = []
    for point in binned:
        statistic = point.tokens[:-2]
        if statistic not in seen:
            seen.append(statistic)
            plan.reads.append((statistic, "points of the distribution the bins are laid over"))


def _check_distinct(ctx: ColumnContext, plan: ColumnPlan, values: Sequence[Decimal]) -> None:
    """A published ``distinctCount`` no rule read: the final bins must span that many values."""
    counted = _published(ctx.entry, "distinctCount")
    tokens = _metric_tokens(ctx, "distinctCount")
    if not isinstance(counted, int) or isinstance(counted, bool) or counted < 1:
        return
    if any(held == tokens for held, _ in plan.reads + plan.passed):
        return
    kind = ctx.column.logical_type.kind
    width = Fraction(values[-1]) - Fraction(values[0])
    if kind is LogicalKind.DATE:
        span = width / _DAY_MS + 1
    elif kind is LogicalKind.TIMESTAMP:
        span = width + 1
    else:
        span = width / Fraction(_grid(ctx)) + 1
    if span >= counted:
        _read(ctx, plan, "distinctCount", "checked: the bins span that many values")
    else:
        plan.passed.append((tokens, "the bins span fewer values than the profile counts"))


def _from_order_statistics(ctx: ColumnContext, plan: ColumnPlan, points: List[_Point]) -> None:
    """Bins over the published ``min``, quartiles, binned quantiles and ``max``.

    That is what ``fluid profile`` writes. With both ends and a mean published, the
    bins are shaped to the mean (:func:`shape_to_mean`), and the mean becomes a
    property when the profile publishes ``mean/1``, one it can score.
    """
    kind = ctx.column.logical_type.kind
    edges, weights = _histogram(points)
    has_low = any(point.level == 0 and point.definition != _QUANTILE_BINNED for point in points)
    has_high = any(point.level == 1 and point.definition != _QUANTILE_BINNED for point in points)
    has_quantiles = any(point.definition == _QUANTILES for point in points)
    binned = [point for point in points if point.definition == _QUANTILE_BINNED]
    closed = _close_ends(ctx, edges, weights, (not has_low, not has_high))
    spelled = [_spell(kind, value) for value in closed.values]
    clipped = [
        name
        for name, held in (
            ("min", has_low or closed.low_closed),
            ("max", has_high or closed.high_closed),
        )
        if not held
    ]
    if clipped:
        weights_basis = assumed(
            f"The profile withheld {' and '.join(clipped)}: the rows beyond the outermost "
            "published value are put in the outermost bin."
        )
    elif has_quantiles:
        weights_basis = from_profile(ctx.profile_tokens + ("metrics", "quantiles"), _QUANTILES)
    elif binned:
        weights_basis = from_profile(binned[0].tokens[:-2], _QUANTILE_BINNED)
    else:
        weights_basis = assumed(
            "Only min and max are published: the values are drawn evenly between them."
        )
    if weights_basis.reason is not None:
        plan.assumptions.append(weights_basis.reason)
    plan.assumptions.extend(closed.notes)
    edge_bases = closed.edge_bases
    weights = closed.weights
    mean = _published_mean(ctx)
    shaped = None
    if (
        not clipped
        and not closed.mean_used
        and mean is not None
        and kind
        in (
            LogicalKind.INT,
            LogicalKind.DECIMAL,
        )
    ):
        shaped = shape_to_mean(closed.values, weights, mean, _grid(ctx)) if _shapes(ctx) else None
        _read_mean(
            ctx,
            plan,
            (
                "the bins are shaped to its mean"
                if shaped is not None
                else "checked against the bins, which keep it as near as they allow"
            ),
        )
    elif closed.mean_used:
        _read_mean(ctx, plan, "the mean the tail beyond the published quantiles is tilted to")
    per_bin: Union[Basis, List[Basis]] = weights_basis
    if closed.weight_bases is not None:
        per_bin = [basis or weights_basis for basis in closed.weight_bases]
    if shaped is None:
        plan.declaration["bins"] = spelled
        plan.declaration["binWeights"] = [_decimal_text(weight) for weight in weights]
        for position, basis in enumerate(edge_bases):
            plan.labels.append(Label(("bins", str(position)), basis))
        if isinstance(per_bin, Basis):
            plan.labels.append(Label(("binWeights",), per_bin))
        else:
            for position, basis in enumerate(per_bin):
                plan.labels.append(Label(("binWeights", str(position)), basis))
        plan.how = f"a histogram over {len(edges)} published order statistics"
        if closed.mean_used:
            plan.how += ", its tail filled to the published mean"
    else:
        _shaped_bins(ctx, plan, shaped, edge_bases, per_bin)
    _read_binned(ctx, plan, binned)
    _check_distinct(ctx, plan, closed.values)
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
                        "sampling": True,
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
SHAPED_REASON = _SHAPED_REASON = (
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
    When one end is withheld and a mean is published, the keys are the rows'
    consecutive grid values laid evenly, so they are centred on the mean. Otherwise
    the span is an assumption, 1 to the row count, which the bundle then moves
    (:func:`widen_key_span`): out to the foreign keys that reference the key, so each
    of their values lies inside it, and into the key it references itself, when it is a
    foreign key too. Whichever it is, the key's own count, distinct count and binned
    quantiles are checked against the layout (ADR 0014 section 3).
    """
    kind = ctx.column.logical_type.kind
    low, high = _published(ctx.entry, "min"), _published(ctx.entry, "max")
    missing = [name for name, value in (("min", low), ("max", high)) if value is None]
    if missing:
        why = f"whose {' and '.join(missing)} the profile does not publish"
        if not beyond and _centre_on_mean(ctx, plan, low, high):
            _check_key_layout(ctx, plan)
            return
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
            _check_key_mean(ctx, plan, Fraction(first.value), Fraction(last.value))
            _check_key_layout(ctx, plan)
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
    _check_key_layout(ctx, plan)
    _check_key_mean_declared(ctx, plan)


#: How a key centred on its published mean draws.
_KEY_CENTRED = (
    "its values are the row count's consecutive grid values, laid evenly and centred on the "
    "published mean."
)


def _centre_on_mean(ctx: ColumnContext, plan: ColumnPlan, low: Any, high: Any) -> bool:
    """Centre a key whose min or max is withheld on its published mean; whether it did.

    The keys are the rows' consecutive grid values, so the lowest is the mean less half
    the span of the other rows, on the grid. A published end that is not where that puts
    it is left unused, with what disagrees.
    """
    mean = _published_mean(ctx)
    if mean is None or ctx.rows < 2:
        return False
    kind = ctx.column.logical_type.kind
    step = _grid(ctx)
    grid = Fraction(step)
    first = round((mean - Fraction(ctx.rows - 1, 2) * grid) / grid) * grid
    last = first + (ctx.rows - 1) * grid
    limit = _limit(ctx)
    if limit is not None and max(abs(first), abs(last)) > limit:
        return False
    sources = _mean_sources(ctx)
    cited = sources[0] if sources else None
    if cited is None:
        return False
    definition = _MEAN if cited[-2:-1] == ("statistics",) or "statistics" in cited else "sum/1"
    layout = f"{_key_noun(ctx)}: {_KEY_CENTRED}"
    plan.declaration["bins"] = [
        _spell(kind, _on_grid(first, step)),
        _spell(kind, _on_grid(last, step)),
    ]
    plan.declaration["binWeights"] = ["1"]
    basis = from_profile(cited, definition)
    plan.labels.append(Label(("bins", "0"), basis))
    plan.labels.append(Label(("bins", "1"), basis))
    plan.labels.append(Label(("binWeights",), assumed(layout)))
    _read_mean(ctx, plan, "the mean the keys are centred on")
    plan.how = "a key the contract declares, centred on its published mean"
    plan.assumptions.append(layout)
    plan.choices.append(layout)
    for name, value, edge in (("min", low, first), ("max", high, last)):
        if value is None:
            continue
        tokens = _metric_tokens(ctx, name)
        stated = Fraction(Decimal(str(value)))
        if abs(stated - edge) <= grid:
            plan.reads.append((tokens, "checked: the even layout holds it"))
        else:
            plan.passed.append(
                (
                    tokens,
                    f"the keys centred on the published mean run from {_exact_text(first)} to "
                    f"{_exact_text(last)}, and the published {name} is {_exact_text(stated)}",
                )
            )
    return True


def _key_note(plan: ColumnPlan, read: bool, tokens: Tuple[str, ...], text: str) -> None:
    """Record a result of a key check, so a later run of the check can replace it."""
    (plan.reads if read else plan.passed).append((tokens, text))
    plan.key_notes.append((read, tokens, text))


def _check_key_mean(ctx: ColumnContext, plan: ColumnPlan, low: Fraction, high: Fraction) -> None:
    """Check a published mean against the even layout over a key's first and last value."""
    plan.key_mean = True
    mean = _published_mean(ctx)
    sources = _mean_sources(ctx)
    if mean is None or not sources:
        return
    middle = (low + high) / 2
    tolerance = max(Fraction(_grid(ctx)), (high - low) / max(ctx.rows, 1))
    if abs(mean - middle) <= tolerance:
        for tokens in sources:
            _key_note(plan, True, tokens, "checked: the even layout holds it")
        return
    why = (
        f"the published mean is {_exact_text(mean)}, and keys laid evenly from the first "
        f"value the bundle declares to the last have mean {_exact_text(middle)}"
    )
    for tokens in sources:
        _key_note(plan, False, tokens, why)


def _key_counts_below(plan: ColumnPlan, rows: int, edge: Fraction) -> Optional[Fraction]:
    """How many of a key's *rows* values lie below *edge* when they are laid evenly."""
    bins = plan.declaration.get("bins")
    if not isinstance(bins, list) or len(bins) < 2 or rows < 1:
        return None
    low, high = Fraction(Decimal(str(bins[0]))), Fraction(Decimal(str(bins[-1])))
    if rows == 1 or high <= low:
        return Fraction(1 if low < edge else 0)
    spacing = (high - low) / (rows - 1)
    return Fraction(min(max(math.ceil((edge - low) / spacing), 0), rows))


def _check_key_layout(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """Check a key's published counts and binned quantiles against how its values are laid."""
    plan.key_checked = True
    for name in ("count", "distinctCount"):
        if _published(ctx.entry, name) == ctx.rows:
            _key_note(
                plan, True, _metric_tokens(ctx, name), "checked: one value of the key per row"
            )
    by_statistic: Dict[Tuple[str, ...], List[_Point]] = {}
    for point in _binned_points_of(ctx.entry, ctx.profile_tokens):
        by_statistic.setdefault(point.tokens[:-2], []).append(point)
    slack = ctx.rounding_base + 1
    for tokens, points in by_statistic.items():
        disagree: Optional[str] = None
        for point in points:
            expected = _key_counts_below(plan, ctx.rows, Fraction(point.value))
            if expected is None:
                disagree = "the bundle declares no span for the key"
                break
            scaled = expected * point.total / ctx.rows
            if abs(scaled - point.below) > slack:
                disagree = (
                    f"the profile counts {point.below} of {point.total} records below "
                    f"{_exact_text(Fraction(point.value))}, and keys laid evenly over the span "
                    f"the bundle declares leave {_exact_text(scaled)} there"
                )
                break
        if disagree is None:
            _key_note(plan, True, tokens, "checked: the even layout holds it")
        else:
            _key_note(plan, False, tokens, disagree)


def _check_key_mean_declared(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """Check a published mean against the even layout over the span the key is declared over."""
    bins = plan.declaration.get("bins")
    if isinstance(bins, list) and len(bins) >= 2:
        _check_key_mean(
            ctx, plan, Fraction(Decimal(str(bins[0]))), Fraction(Decimal(str(bins[-1])))
        )


def recheck_key(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """Run a number key's checks again on the span the bundle ended with (ADR 0014 section 3).

    The key's quantiles, mean, count and distinct count are checked when the key is planned,
    against the span it is first declared over. A foreign key that references it, or the key
    it references, can move that span (:func:`widen_key_span`, :func:`draw_within_key`), so
    the bundle calls this once every key's span is final and the result replaces the first.
    A key centred on its published mean keeps the mean it was centred on as its reading.
    """
    if not plan.key_checked:
        return
    for read, tokens, text in plan.key_notes:
        held = plan.reads if read else plan.passed
        if (tokens, text) in held:
            held.remove((tokens, text))
    plan.key_notes = []
    _check_key_layout(ctx, plan)
    if plan.key_mean:
        _check_key_mean_declared(ctx, plan)


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
    """No order statistic: a span with the published mean, or a placeholder.

    A span as wide as the published ``distinctCount``, centred on the mean,
    keeps a query's totals and averages. With a mean and no ``distinctCount``,
    the values are filled by maximum entropy from 0, on the side of zero the mean
    lies on (ADR 0014 section 3). With nothing at all, a placeholder spread. Each
    is an assumption, and said to be one.
    """
    mean = _published_mean(ctx)
    if mean is None:
        _placeholder(ctx, plan, _PLACEHOLDER_REASON)
        return
    step = _grid(ctx)
    grid = Fraction(step)
    distinct = _published(ctx.entry, "distinctCount")
    if distinct == 1:
        # The profile shows one value, and the mean says which: every row takes it.
        plan.declaration["levels"] = {format(_on_grid(round(mean / grid) * grid, step), "f"): "1"}
        plan.labels.append(Label(("levels",), assumed(_ONE_DISTINCT_REASON)))
        _read_mean(ctx, plan, "the one value every row takes")
        _read(ctx, plan, "distinctCount", "one distinct value: every row takes the mean")
        plan.how = "one value on every row: the profile counts one distinct value"
        plan.assumptions.append(_ONE_DISTINCT_REASON)
        return
    if isinstance(distinct, int) and distinct > 1:
        # Exact arithmetic on the grid: a decimal context would round a long value or a
        # fine scale (DECIMAL(38,30), say) before it reached the grid.
        half = grid * (distinct - 1) / 2
        low = math.floor((mean - half) / grid) * grid
        high = math.ceil((mean + half) / grid) * grid
        limit = _limit(ctx)
        if limit is not None:
            # Kept within what the type holds, shifted rather than cut where it can be.
            if high > limit:
                low, high = max(low - (high - limit), -limit), limit
            if low < -limit:
                low, high = -limit, min(high + (-limit - low), limit)
        _even_bins(ctx, plan, low, high, assumed(_CENTRED_REASON))
        _read_mean(ctx, plan, "the mean the span is centred on")
        _read(ctx, plan, "distinctCount", "how many values wide the span is")
        plan.how = "evenly around the published mean"
        plan.assumptions.append(_CENTRED_REASON)
        return
    if mean != 0:
        filled = tilted_fill(
            Fraction(0), mean, grid, ctx.rows, limit=_limit(ctx), bounds=_range_bounds(ctx)
        )
        if filled is not None:
            edges, weights = filled
            _declare_fill(ctx, plan, edges, weights, None, None)
            plan.labels = [
                (
                    label
                    if label.tokens[:1] != ("bins",)
                    else Label(label.tokens, assumed(_FILL_FROM_ZERO))
                )
                for label in plan.labels
            ]
            _read_mean(ctx, plan, "the mean the fill is tilted to")
            plan.how = "the largest-entropy spread from 0 with the published mean"
            plan.assumptions.append(_FILL_FROM_ZERO)
            return
    low, high = -(_PLACEHOLDER_VALUES // 2) * grid, (_PLACEHOLDER_VALUES // 2) * grid
    _even_bins(ctx, plan, low, high, assumed(_CENTRED_ZERO))
    _read_mean(ctx, plan, "the mean the span is centred on")
    plan.how = "evenly around the published mean of 0"
    plan.assumptions.append(_CENTRED_ZERO)


_CENTRED_REASON = (
    "The profile publishes a mean and the number of distinct values but no order statistic: "
    "the values are drawn evenly over a span centred on the mean, as many values wide as "
    "distinctCount."
)
_FILL_FROM_ZERO = (
    "The profile publishes a mean and no order statistic or count of distinct values: the "
    "values are assumed to lie on the side of 0 the mean does, and are drawn by the "
    "distribution of largest entropy with that mean, a geometric tilt over the bins declared "
    "here (ADR 0014)."
)
_ONE_DISTINCT_REASON = (
    "The profile counts one distinct value and publishes its mean, so every row takes the "
    "mean, on the column's grid."
)
_CENTRED_ZERO = (
    "The profile publishes a mean of 0 and no order statistic or count of distinct values: "
    "the values are drawn evenly over a span centred on 0."
)


#: Why a number column the profile says nothing of takes a placeholder spread.
_PLACEHOLDER_REASON = (
    "The profile publishes no order statistic and no mean: the values are a placeholder, "
    "drawn evenly over the bins declared here, from 0."
)


# ---------------------------------------------------------------------------
# A column a rule of the contract sets
# ---------------------------------------------------------------------------

#: The keys a column a rule sets declares none of: it is not drawn.
_DRAW_KEYS = (
    "bins",
    "binWeights",
    "binWeightsGiven",
    "levels",
    "levelWeightsGiven",
    "given",
    "nullFraction",
    "range",
    "unique",
)

_DETERMINED_REASON = (
    "A rule of the contract sets the column, so it is not drawn; the range the check reads is "
    "the exact bounds the rule gives over the ranges of the columns it reads (ADR 0014)."
)
_DETERMINED_PAST_RANGE = (
    "A rule of the contract sets the column, and the values it gives can pass the range the "
    "contract declares for the column: the rule's values are not clipped to it."
)
_DETERMINED_UNBOUNDED = (
    "A rule of the contract sets the column, so it is not drawn, and its range cannot be "
    "bounded from the rule: no property reads it."
)


def copy_column(ctx: ColumnContext, plan: ColumnPlan, *, how: str, reason: str) -> None:
    """Make *plan* a column a ``sharedColumns`` or ``sharedAttribute`` copies from another row.

    Kernel 0.2.0 plans the copy as a rule on every row (ADR 0022 section 4), so the column declares
    no draw and no range of its own, as a column a contract ``derive`` sets (ADR 0014 section 6).
    *how* is what the bundle says of the column, and *reason* the choice it records.
    """
    determine_column(ctx, plan, (), None)
    plan.assumptions = [item for item in plan.assumptions if item != _DETERMINED_UNBOUNDED]
    plan.choices = [item for item in plan.choices if item != _DETERMINED_UNBOUNDED]
    plan.assumptions.append(reason)
    plan.choices.append(reason)
    plan.fixed = how.split(" (", 1)[0]
    plan.how = how


def determine_column(
    ctx: ColumnContext,
    plan: ColumnPlan,
    rule_tokens: Tuple[str, ...],
    interval: Optional[Tuple[Fraction, Fraction]],
) -> None:
    """Make *plan* the column a contract rule at *rule_tokens* sets (ADR 0014 section 6).

    The column declares no draw and never its own profiled range. With exact
    *interval* bounds of the rule, a number column declares a range the rule's
    values always lie in: the edges of its published histogram when they hold
    the bounds, else the bounds moved out to the column's grid. Its own
    statistics then become properties, so ``verify`` checks them through the rule.
    """
    kind = ctx.column.logical_type.kind
    for key in _DRAW_KEYS:
        plan.declaration.pop(key, None)
    plan.labels = [label for label in plan.labels if label.tokens[:1] in (("maxCardinality",),)]
    plan.properties = []
    plan.reads, plan.passed = [], []
    plan.assumptions, plan.choices, plan.weight_notes = [], [], []
    plan.placeholder = plan.key_span = plan.key_checked = plan.key_mean = False
    plan.key_notes = []
    plan.published_range = plan.time_span = None
    plan.fixed = "set by a rule of the contract"
    plan.how = "set by a rule of the contract, not drawn"
    if ctx.column.required and _published(ctx.entry, "count") == ctx.rows:
        _read(ctx, plan, "count", "checked: a required column holds a value per row")
    if interval is None or kind not in (LogicalKind.INT, LogicalKind.DECIMAL):
        plan.assumptions.append(_DETERMINED_UNBOUNDED)
        plan.choices.append(_DETERMINED_UNBOUNDED)
        _check_quantile_bins(ctx, plan, None)
        return
    low, high = interval
    found = _ungrouped(ctx.entry, _HISTOGRAM)
    edges = [Fraction(Decimal(str(edge))) for edge in found[1].get("bins", ())] if found else []
    if len(edges) >= 2 and edges[0] <= low and high <= edges[-1]:
        assert found is not None
        _from_histogram(ctx, plan, found)
        for key in ("bins", "binWeights"):
            plan.declaration.pop(key, None)
        plan.labels = [
            label for label in plan.labels if label.tokens[:1] not in (("bins",), ("binWeights",))
        ]
        # The histogram's own notes (a withheld bin count, its shaping to the mean) speak of
        # weights the column no longer declares.
        plan.assumptions, plan.choices = [], []
        # A histogram property's bins would be the column's draw for the generator, which
        # a rule's column has none of (FLX-3402): its edges are the range, and the mean the
        # property the rule's values are scored on.
        plan.properties = [
            prop for prop in plan.properties if prop.document["property"] != "marginalHistogram"
        ]
        plan.reads.append(
            (
                ctx.profile_tokens + ("statistics", str(found[0])),
                "its edges are the range the check holds the rule's values to",
            )
        )
    else:
        step = _grid(ctx)
        grid = Fraction(step)
        rule = from_contract(ctx.product_ref, rule_tokens)
        ends: Dict[str, Tuple[Fraction, Basis]] = {
            "min": (math.floor(low / grid) * grid, rule),
            "max": (math.ceil(high / grid) * grid, rule),
        }
        # The rule's bounds rest on the spans of the columns it reads, which can be
        # assumptions; the check reads the profile's own values of the column against the
        # range too (FLX-5005), so a published end beyond the bounds widens the range.
        for name in ("min", "max"):
            value = _published(ctx.entry, name)
            if value is None:
                continue
            point = Fraction(Decimal(str(value)))
            if point < ends[name][0] if name == "min" else point > ends[name][0]:
                ends[name] = (point, from_profile(_metric_tokens(ctx, name), _ENDS[name]))
        wide_low, wide_high = ends["min"][0], ends["max"][0]
        if wide_high <= wide_low:
            wide_high = wide_low + grid
        plan.declaration["range"] = {
            "min": format(_on_grid(wide_low, step), "f"),
            "max": format(_on_grid(wide_high, step), "f"),
        }
        plan.labels.append(Label(("range", "min"), ends["min"][1]))
        plan.labels.append(Label(("range", "max"), ends["max"][1]))
        plan.published_range = (_on_grid(wide_low, step), _on_grid(wide_high, step))
        _mean_property(ctx, plan)
    _check_quantile_bins(ctx, plan, plan.published_range)
    _check_distinct_range(ctx, plan)
    for name in ("min", "max"):
        value = _published(ctx.entry, name)
        if value is not None:
            inside = low <= Fraction(Decimal(str(value))) <= high
            plan.reads.append(
                (
                    _metric_tokens(ctx, name),
                    (
                        "checked: within the bounds the rule gives"
                        if inside
                        else "checked: it lies outside the bounds the rule gives over the spans the "
                        "bundle has for the columns it reads, so the range holds it too"
                    ),
                )
            )
    if any(prop.document["property"] == "mean" for prop in plan.properties):
        _read_mean(ctx, plan, "the mean property scores the rule's values against it")
    plan.assumptions.append(_DETERMINED_REASON)
    plan.choices.append(_DETERMINED_REASON)
    held = _range_bounds(ctx)
    if held is not None and (
        (held[0] is not None and low < held[0]) or (held[1] is not None and high > held[1])
    ):
        plan.assumptions.append(_DETERMINED_PAST_RANGE)
        plan.choices.append(_DETERMINED_PAST_RANGE)
    plan.how = "set by a rule of the contract, not drawn; its range is the rule's exact bounds"


def _published_ladders(
    ctx: ColumnContext,
) -> List[Tuple[int, Mapping[str, Any], Tuple[Decimal, ...]]]:
    """Each published, ungrouped ``quantile/2`` of the column: its index, itself, its edge ladder."""
    found: List[Tuple[int, Mapping[str, Any], Tuple[Decimal, ...]]] = []
    for index, statistic in _statistics(ctx.entry):
        if statistic.get("definition") != _QUANTILE_BINNED or grouped(statistic):
            continue
        groups, bins = statistic.get("groups"), statistic.get("bins")
        if not isinstance(groups, list) or len(groups) != 1 or not isinstance(bins, list):
            continue
        group = groups[0]
        if not isinstance(group, Mapping) or group.get("status") != _FREE:
            continue
        try:
            ladder = tuple(Decimal(str(edge)) for edge in bins)
        except ArithmeticError:
            continue
        if len(ladder) >= 2 and all(low < high for low, high in pairwise(ladder)):
            found.append((index, statistic, ladder))
    return found


def _check_quantile_bins(
    ctx: ColumnContext, plan: ColumnPlan, declared: Optional[Tuple[Decimal, Decimal]]
) -> None:
    """Check each published ``quantile/2`` of a rule's column against the range it declares.

    The bin the profile says holds the quantile must overlap the range the rule gives, or
    the profile and the rule cannot describe the same column. The rule's values are not
    scored on the bin itself: they are measured through inputs drawn from their own bins.
    """
    held = _range_bounds(ctx)
    for index, statistic, ladder in _published_ladders(ctx):
        position = statistic["groups"][0].get("quantileBin")
        if not isinstance(position, int) or isinstance(position, bool):
            continue
        if not 0 <= position < len(ladder) - 1:
            continue
        tokens = ctx.profile_tokens + ("statistics", str(index))
        if declared is None:
            plan.passed.append(
                (
                    tokens,
                    "no range can be declared for the column, so there is none to hold the bin "
                    "that holds this quantile to",
                )
            )
            continue
        # The rule's values are not clipped to the contract's range, so the bin has to meet
        # both: the values the rule gives that the contract's range allows.
        lowest, highest = Fraction(declared[0]), Fraction(declared[1])
        narrowed = False
        if held is not None:
            if held[0] is not None and held[0] > lowest:
                lowest, narrowed = held[0], True
            if held[1] is not None and held[1] < highest:
                highest, narrowed = held[1], True
        if (
            lowest <= highest
            and Fraction(ladder[position]) <= highest
            and Fraction(ladder[position + 1]) > lowest
        ):
            plan.reads.append(
                (
                    tokens,
                    "checked: the bin that holds this quantile lies within the range "
                    "the rule gives" + (", held to the contract's range" if narrowed else ""),
                )
            )
        else:
            plan.passed.append(
                (
                    tokens,
                    "the bin the profile says holds this quantile lies outside the range the "
                    "rule gives"
                    + (", held to the contract's range for the column" if narrowed else ""),
                )
            )


def _check_distinct_range(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """Check a published ``distinctCount`` against the grid values the declared range holds."""
    distinct = _published(ctx.entry, "distinctCount")
    declared = plan.declaration.get("range")
    if (
        not isinstance(distinct, int)
        or isinstance(distinct, bool)
        or not isinstance(declared, dict)
    ):
        return
    if ctx.column.logical_type.kind not in (LogicalKind.INT, LogicalKind.DECIMAL):
        return
    values = (
        Fraction(Decimal(str(declared["max"]))) - Fraction(Decimal(str(declared["min"])))
    ) / Fraction(_grid(ctx)) + 1
    if values >= distinct:
        _read(
            ctx,
            plan,
            "distinctCount",
            "checked: the declared range holds at least that many grid values",
        )
    else:
        plan.passed.append(
            (
                _metric_tokens(ctx, "distinctCount"),
                f"the declared range holds {math.floor(values)} grid values, fewer than the "
                f"{distinct} distinct values the profile counts",
            )
        )


#: The metric definition of each end of a range, for citing a published end.
_ENDS: Mapping[str, str] = {"min": _MIN, "max": _MAX}


# --- the range a contract declares ------------------------------------------

#: Why bins or levels the contract's range leaves no grid value in are dropped.
_RANGE_LOST = (
    "The contract's range for the column leaves no value in {count} of the profile's bins or "
    "levels: they are dropped and the weights of the rest are scaled to add up to one."
)
_RANGE_FOLDED = (
    "The contract's range for the column leaves one grid value in the profile's last bin, and "
    "a bin cannot be drawn from one value without an edge past the range: it is drawn with "
    "the bin before it, and the weights of the two are added."
)
_RANGE_ONE_SIDED = (
    "The contract's range for the column has one bound: the values are a placeholder, drawn "
    "evenly over a hundred grid values from it."
)


def _range_bounds(
    ctx: ColumnContext,
) -> Optional[Tuple[Optional[Fraction], Optional[Fraction]]]:
    """The lowest and highest value the contract's range lets the column hold, on its grid.

    ``None`` for a column with no range rule, one that is not a plain number, or rules no
    grid value satisfies (they declare nothing).
    """
    if ctx.range_rule is None or ctx.column.logical_type.kind not in (
        LogicalKind.INT,
        LogicalKind.DECIMAL,
    ):
        return None
    return grid_bounds(ctx.range_rule, Fraction(_grid(ctx)))


def _exact_text(value: Fraction) -> str:
    """*value* as an exact decimal text: a bound or a half step has a finite expansion.

    Exact at any length: the denominator is a product of twos and fives, so *value*
    times ten to the larger of their counts is a whole number.
    """
    rest, twos, fives = value.denominator, 0, 0
    while rest % 2 == 0:
        rest, twos = rest // 2, twos + 1
    while rest % 5 == 0:
        rest, fives = rest // 5, fives + 1
    if rest != 1:  # no finite expansion: the nearest 200 significant digits
        with localcontext() as context:
            context.prec = 200
            return format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    places = max(twos, fives)
    units = value.numerator * 10**places // value.denominator
    digits = str(abs(units)).rjust(places + 1, "0")
    text = digits if places == 0 else f"{digits[:-places]}.{digits[-places:]}"
    return f"-{text}" if units < 0 else text


#: What the draw does when the contract's range lies beyond what the column's type holds.
_RANGE_BEYOND_TYPE = (
    "The contract's range for the column lies beyond what its type holds, so no value can "
    "meet it: the draw keeps to {what}."
)
#: What the profile's values leave of a range that holds none of them.
_RANGE_OUTSIDE = (
    "The profile's values lie outside the range the contract declares for the column, so "
    "none of them shapes the draw: the values are drawn evenly over the contract's range."
)
_OUTSIDE_REASON = (
    "the profile's values lie outside the range the contract declares, so none of them "
    "shapes the draw"
)
#: What the draw does when the profile puts no row inside the contract's range.
_RANGE_NO_ROW = (
    "The profile puts no row inside the range the contract declares for the column: the "
    "values are drawn evenly within the part of the profile's span that range leaves."
)
#: The positions of a declaration that make up a number column's draw.
_DRAW_POSITIONS = (("bins",), ("binWeights",), ("levels",), ("range",))


def _within_range(ctx: ColumnContext, plan: ColumnPlan) -> None:
    """Keep a drawn number column inside the range its contract declares (ADR 0014 section 3).

    A column the profile published nothing for is drawn evenly over the declared range
    instead of the placeholder. Any other draw is clipped to the range: its bin edges and its
    levels. A bin left with no grid value is dropped, and the loss of weight is an assumption.
    A draw none of whose values lies inside the range is replaced by an even draw over it.
    The range the check reads and a property's bins keep the profile's edges: the check
    compares the profile's statistic over exactly those (``FLX-5037``), and a value drawn
    inside the contract's range lies inside them.
    """
    if ctx.key or ctx.keyed or ctx.foreign is not None:
        return
    bounds = _range_bounds(ctx)
    if bounds is None:
        return
    low, high = bounds
    basis = from_contract(ctx.product_ref, ctx.contract_tokens + ("validationRules",))
    if plan.placeholder:
        _declared_range_draw(ctx, plan, low, high, basis)
        return
    step = Fraction(_grid(ctx))
    before = _draw_of(plan)
    lost_bins = _clip_bins(plan, low, high, basis, step)
    lost_levels = _clip_levels(plan, low, high) if lost_bins is not None else None
    if lost_bins is None or lost_levels is None:
        _outside_range(ctx, plan, low, high, basis)
        return
    if _draw_of(plan) != before:
        _drop_unscorable_histogram(ctx, plan, step)
    lost = lost_bins + lost_levels
    if lost:
        reason = _RANGE_LOST.format(count=lost)
        plan.assumptions.append(reason)
        plan.choices.append(reason)


def _draw_of(plan: ColumnPlan) -> Tuple[Any, ...]:
    """The bins, weights and levels a column's declaration draws from."""
    return tuple(repr(plan.declaration.get(key)) for key in ("bins", "binWeights", "levels"))


#: Why a column's histogram is no longer a property.
_NO_PUBLISHED_CELL = (
    "the range the contract declares leaves no value in a cell the profile published, so the "
    "check has no published cell to score the draw by"
)


def _drop_unscorable_histogram(ctx: ColumnContext, plan: ColumnPlan, step: Fraction) -> None:
    """Drop the histogram property when the clipped draw lands in no cell the profile published.

    The check scores a histogram over the cells the profile published and refuses a run that
    puts no record in them (``FLX-5005``), so a draw clipped to cells the profile withheld
    could not be scored. The statistic is then unused, with the reason.
    """
    found = _ungrouped(ctx.entry, _HISTOGRAM)
    if found is None:
        return
    index, statistic, group = found
    edges = [Fraction(Decimal(str(edge))) for edge in statistic.get("bins", ())]
    cells = _cells(group, "binCounts")
    if len(edges) != len(cells) + 1:
        return
    runs: List[Tuple[Fraction, Fraction]] = []
    bins, weights = plan.declaration.get("bins"), plan.declaration.get("binWeights")
    if isinstance(bins, list) and isinstance(weights, list) and len(weights) == len(bins) - 1:
        points = [Fraction(Decimal(str(edge))) for edge in bins]
        for position, weight in enumerate(weights):
            if Fraction(Decimal(str(weight))) > 0:
                first = math.ceil(points[position] / step) * step
                last = points[position + 1]
                final = (
                    math.floor(last / step) * step
                    if position == len(weights) - 1
                    else math.ceil(last / step) * step - step
                )
                runs.append((first, final))
    levels = plan.declaration.get("levels")
    if isinstance(levels, dict):
        runs += [(Fraction(Decimal(str(text))),) * 2 for text in levels]
    for position, cell in enumerate(cells):
        if cell.count is None:
            continue
        top, closed = edges[position + 1], position == len(cells) - 1
        if any(
            (first <= top if closed else first < top) and final >= edges[position]
            for first, final in runs
        ):
            return
    kept = [prop for prop in plan.properties if prop.document["property"] != "marginalHistogram"]
    if len(kept) == len(plan.properties):
        return
    plan.properties = kept
    plan.passed.append((ctx.profile_tokens + ("statistics", str(index)), _NO_PUBLISHED_CELL))


def _declared_span(
    ctx: ColumnContext, low: Optional[Fraction], high: Optional[Fraction], basis: Basis
) -> Optional[Tuple[Fraction, Fraction, Basis]]:
    """The values a draw over the contract's range spans: its first, its last, and what they rest on.

    The range is held to what the column's type carries. ``None`` when the type holds no
    value of it, or the range gives no bound.
    """
    limit = _limit(ctx)
    if limit is not None:
        if (low is not None and low > limit) or (high is not None and high < -limit):
            return None
        low = max(low, -limit) if low is not None else None
        high = min(high, limit) if high is not None else None
    if low is not None and high is not None:
        return (low, high, basis) if low <= high else None
    if low is not None:
        first, last = _span_bins(ctx, low, _PLACEHOLDER_VALUES)
    elif high is not None:
        first, last = _span_bins(ctx, high, _PLACEHOLDER_VALUES, upward=False)
    else:
        return None
    return first, last, assumed(_RANGE_ONE_SIDED)


def _declared_range_draw(
    ctx: ColumnContext,
    plan: ColumnPlan,
    low: Optional[Fraction],
    high: Optional[Fraction],
    basis: Basis,
) -> bool:
    """Draw evenly over the declared range, in place of a placeholder; whether it did.

    A range the column's type holds none of leaves the placeholder, and says so.
    """
    if low is None and high is None:
        return False
    span = _declared_span(ctx, low, high, basis)
    if span is None:
        reason = _RANGE_BEYOND_TYPE.format(what="the placeholder")
        plan.assumptions.append(reason)
        plan.choices.append(reason)
        return False
    first, last, used = span
    _draw_evenly(ctx, plan, first, last, used)
    if used is not basis:
        plan.assumptions.append(_RANGE_ONE_SIDED)
        plan.choices.append(_RANGE_ONE_SIDED)
        plan.how = "a placeholder over a hundred values from the contract's one bound"
    else:
        plan.how = "evenly over the range the contract declares"
    plan.placeholder = False
    return True


def _draw_evenly(
    ctx: ColumnContext, plan: ColumnPlan, first: Fraction, last: Fraction, used: Basis
) -> None:
    """Replace the column's draw by one bin from *first* to *last*, drawn evenly."""
    plan.declaration.pop("levels", None)
    plan.declaration.pop("bins", None)
    plan.declaration.pop("binWeights", None)
    shape = (("bins",), ("binWeights",), ("levels",))
    plan.labels = [label for label in plan.labels if label.tokens[:1] not in shape]
    plan.assumptions = [
        sentence
        for sentence in plan.assumptions
        if "placeholder" not in sentence and sentence != _BEYOND_REASON
    ]
    plan.reads = [read for read in plan.reads if "placeholder spans" not in read[1]]
    _even_bins(ctx, plan, first, last, used)


def _outside_range(
    ctx: ColumnContext,
    plan: ColumnPlan,
    low: Optional[Fraction],
    high: Optional[Fraction],
    basis: Basis,
) -> None:
    """A draw that holds no value the contract's range allows: draw evenly over the range.

    None of the profile's values lies inside the range, so no statistic of the column
    shapes the draw: each the draw read becomes unused, with the reason, and the properties
    that scored the profile's values go. A range the column's type holds none of leaves the
    draw as it is, and says so.
    """
    span = _declared_span(ctx, low, high, basis)
    if span is None:
        reason = _RANGE_BEYOND_TYPE.format(what="what the profile gives")
        plan.assumptions.append(reason)
        plan.choices.append(reason)
        return
    first, last, used = span
    cited: List[Tuple[str, ...]] = [tokens for tokens, _ in plan.reads]
    for prop in plan.properties:
        cited.extend(tokens for tokens, _ in prop.reads)
        cited.extend(
            label.basis.statistic[0] for label in prop.labels if label.basis.statistic is not None
        )
    cited.extend(
        label.basis.statistic[0]
        for label in plan.labels
        if label.tokens[:1] in _DRAW_POSITIONS and label.basis.statistic is not None
    )
    held = [tokens for tokens, _ in plan.passed]
    for tokens in cited:
        if tokens not in held:
            held.append(tokens)
            plan.passed.append((tokens, _OUTSIDE_REASON))
    plan.reads = []
    plan.properties = []
    plan.declaration.pop("range", None)
    plan.published_range = plan.time_span = None
    plan.labels = [label for label in plan.labels if label.tokens[:1] not in _DRAW_POSITIONS]
    plan.assumptions, plan.choices, plan.weight_notes = [], [], []
    _draw_evenly(ctx, plan, first, last, used)
    plan.assumptions.append(_RANGE_OUTSIDE)
    plan.choices.append(_RANGE_OUTSIDE)
    if used is not basis:
        plan.assumptions.append(_RANGE_ONE_SIDED)
        plan.choices.append(_RANGE_ONE_SIDED)
    plan.how = "evenly over the range the contract declares"


def _wholly_outside(
    lower: Fraction,
    upper: Fraction,
    closed: bool,
    low: Optional[Fraction],
    high: Optional[Fraction],
) -> bool:
    """Whether a bin's edge interval lies wholly outside the range, grid value or not.

    A bin is half open, ``[lower, upper)``; the last one is closed at ``upper``.
    """
    if low is not None and (upper < low or (upper == low and not closed)):
        return True
    return high is not None and lower > high


def _clip_bins(
    plan: ColumnPlan,
    low: Optional[Fraction],
    high: Optional[Fraction],
    basis: Basis,
    step: Fraction,
) -> Optional[int]:
    """Clip the declared bins to the range; returns how many bins it dropped.

    ``None`` when no bin holds a grid value inside the range, and the draw is left as
    it is. A range that leaves no weight, or one grid value, is drawn evenly over what it
    leaves, or as that one value.
    """
    bins, weights = plan.declaration.get("bins"), plan.declaration.get("binWeights")
    if not isinstance(bins, list) or len(bins) < 2 or not isinstance(weights, list):
        return 0
    count = len(bins) - 1
    if len(weights) != count:
        return 0
    edges = [Fraction(Decimal(str(edge))) for edge in bins]
    share = [Fraction(Decimal(str(weight))) for weight in weights]
    # Each bin as the grid values it draws, [first, final], and what the range leaves of them.
    cells: List[Tuple[int, Fraction, Fraction, Fraction, Fraction]] = []
    for index in range(count):
        first = math.ceil(edges[index] / step) * step
        if index == count - 1:
            final = math.floor(edges[index + 1] / step) * step
        else:
            final = math.ceil(edges[index + 1] / step) * step - step
        kept_low = max(first, low) if low is not None else first
        kept_high = min(final, high) if high is not None else final
        if first <= final and kept_low > kept_high:
            continue
        if _wholly_outside(edges[index], edges[index + 1], index == count - 1, low, high):
            continue
        cells.append((index, first, final, kept_low, kept_high))
    live = [cell for cell in cells if cell[1] <= cell[2]]
    if not live:
        return None
    if len(cells) == count and all(c[1] == c[3] and c[2] == c[4] for c in cells):
        return 0
    dropped = count - len(cells)
    drawn = [cell for cell in live if share[cell[0]] > 0]
    if not drawn:
        _draw_clipped(plan, live[0][3], live[-1][4], basis, step)
        plan.assumptions.append(_RANGE_NO_ROW)
        plan.choices.append(_RANGE_NO_ROW)
        return 0
    if len(drawn) == 1 and drawn[0][3] == drawn[0][4]:
        _draw_clipped(plan, drawn[0][3], drawn[0][4], basis, step)
        return dropped
    first_cell, last_cell = cells[0], cells[-1]
    old: Dict[int, Basis] = {
        int(label.tokens[1]): label.basis
        for label in plan.labels
        if label.tokens[:1] == ("bins",) and len(label.tokens) == 2
    }
    old_weights: Dict[int, Basis] = {
        int(label.tokens[1]): label.basis
        for label in plan.labels
        if label.tokens[:1] == ("binWeights",) and len(label.tokens) == 2
    }
    folded = False
    moved = first_cell[3] != first_cell[1]
    new_edges: List[Fraction] = [first_cell[3] if moved else edges[first_cell[0]]]
    new_bases: List[Optional[Basis]] = [basis if moved else old.get(first_cell[0])]
    for cell in cells[1:]:
        new_edges.append(edges[cell[0]])
        new_bases.append(old.get(cell[0]))
    if last_cell[0] == count - 1 and last_cell[4] == last_cell[2]:
        new_edges.append(edges[count])
        new_bases.append(old.get(count))
    elif last_cell[4] > new_edges[-1]:
        new_edges.append(last_cell[4])
        new_bases.append(basis)
    elif len(cells) > 1:
        # One grid value is all the last bin holds, and an edge above it would leave the
        # range, or sit between two grid values. The edges already end at that value, so the
        # bin before it closes there and takes its weight.
        folded = True
    else:  # pragma: no cover  (one cell holding one value is drawn by _draw_clipped above)
        _draw_clipped(plan, last_cell[3], last_cell[4], basis, step)
        return dropped
    plan.declaration["bins"] = [_exact_text(edge) for edge in new_edges]
    kept = [share[cell[0]] for cell in cells]
    if folded:
        kept = [*kept[:-2], kept[-2] + kept[-1]]
        cells = cells[:-1]
        plan.assumptions.append(_RANGE_FOLDED)
        plan.choices.append(_RANGE_FOLDED)
    total = sum(kept, Fraction(0))
    scaled = (dropped > 0 or folded) and total > 0
    plan.declaration["binWeights"] = (
        share_texts([weight / total for weight in kept])
        if scaled
        else [str(weights[cell[0]]) for cell in cells]
    )
    _read_shaped(plan, _RANGE_SHAPED)
    plan.labels = [
        label
        for label in plan.labels
        if label.tokens[:1] not in (("bins",), ("binWeights",)) or len(label.tokens) != 2
    ]
    for position, edge_basis in enumerate(new_bases):
        if edge_basis is not None:
            plan.labels.append(Label(("bins", str(position)), edge_basis))
    for position, cell in enumerate(cells):
        if scaled:
            plan.labels.append(
                Label(
                    ("binWeights", str(position)),
                    assumed(_RANGE_LOST.format(count=dropped) if dropped else _RANGE_FOLDED),
                )
            )
        elif cell[0] in old_weights:
            plan.labels.append(Label(("binWeights", str(position)), old_weights[cell[0]]))
    return dropped


#: Why a statistic a cut or a clip replaced the label of is still read.
_WINDOW_SHAPED = "its value places an edge or a weight of the bins the run's time window then cuts"
_RANGE_SHAPED = "its value places an edge or a weight of the draw the declared range then clips"


def _read_shaped(plan: ColumnPlan, how: str) -> None:
    """Record as read each statistic a bin or weight label cites, before a cut replaces those labels.

    A cut or a clip keeps what the bins held inside it, so the statistics that placed them
    (a published maximum that closed the ladder's top, a bin count) still shape the draw
    although the label that cited them is gone (ADR 0014 section 1).
    """
    held = {tokens for tokens, _ in plan.reads}
    for label in plan.labels:
        if label.tokens[:1] in (("bins",), ("binWeights",)) and label.basis.statistic is not None:
            tokens = label.basis.statistic[0]
            if tokens not in held:
                held.add(tokens)
                plan.reads.append((tokens, how))


_WINDOW_CLIPPED = (
    "The profile's bins for the column reach beyond the run's time window: they are cut at "
    "the window, a bin left with no time inside it is dropped, and its weight goes to the "
    "bin beside it."
)


def bound_to_window(plan: ColumnPlan, kind: LogicalKind, window: Tuple[str, str]) -> None:
    """Cut a date or timestamp column's bins at the run's time window.

    A bin whose edge lies beyond the window (the bin a profile's quantile ladder adds
    past an outermost published point is one) draws values the run does not cover. The
    edges are cut at the window, a bin left with no time inside it is dropped, and its
    weight goes to the bin beside it, on the side that stays inside. A column whose bins
    lie inside the window is left as it is, so its declaration does not change.
    """
    bins, weights = plan.declaration.get("bins"), plan.declaration.get("binWeights")
    if not _temporal(kind) or not isinstance(bins, list) or not isinstance(weights, list):
        return
    if len(bins) < 2 or len(weights) != len(bins) - 1 or "range" in plan.declaration:
        return  # a declared range is part of the window, and a property reads its bins
    unit = _DAY_MS if kind is LogicalKind.DATE else 1
    low, high = (parse_rfc3339_ms(text) // unit for text in window)
    edges = [
        (
            (date.fromisoformat(str(edge)) - _EPOCH).days
            if kind is LogicalKind.DATE
            else parse_rfc3339_ms(str(edge))
        )
        for edge in bins
    ]
    if low <= edges[0] and edges[-1] <= high:
        return
    share = [Fraction(Decimal(str(weight))) for weight in weights]
    kept: List[Tuple[int, int, Fraction]] = []
    carry_low = Fraction(0)
    for index, weight in enumerate(share):
        first, last = max(edges[index], low), min(edges[index + 1], high)
        if first >= last:
            if edges[index + 1] <= low and not kept:
                carry_low += weight
            elif kept:
                kept[-1] = (kept[-1][0], kept[-1][1], kept[-1][2] + weight)
            continue
        kept.append((first, last, weight + carry_low))
        carry_low = Fraction(0)
    if not kept or sum(weight for _, _, weight in kept) <= 0:
        return
    plan.declaration["bins"] = [_spell(kind, Decimal(kept[0][0] * unit))] + [
        _spell(kind, Decimal(last * unit)) for _, last, _ in kept
    ]
    total = sum((weight for _, _, weight in kept), Fraction(0))
    plan.declaration["binWeights"] = share_texts([weight / total for _, _, weight in kept])
    _read_shaped(plan, _WINDOW_SHAPED)
    plan.labels = [
        label
        for label in plan.labels
        if label.tokens[:1] not in (("bins",), ("binWeights",)) or len(label.tokens) != 2
    ]
    plan.labels.append(Label(("bins",), assumed(_WINDOW_CLIPPED)))
    plan.labels.append(Label(("binWeights",), assumed(_WINDOW_CLIPPED)))
    plan.assumptions.append(_WINDOW_CLIPPED)
    plan.choices.append(_WINDOW_CLIPPED)


def _draw_clipped(
    plan: ColumnPlan, first: Fraction, last: Fraction, basis: Basis, step: Fraction
) -> None:
    """Replace the bins by one drawn evenly from *first* to *last*, or by the value they share."""
    for key in ("bins", "binWeights", "levels"):
        plan.declaration.pop(key, None)
    _read_shaped(plan, _RANGE_SHAPED)
    plan.labels = [label for label in plan.labels if label.tokens[:1] not in _DRAW_POSITIONS[:3]]
    if first >= last:
        plan.declaration["levels"] = {_exact_text(first): "1"}
        plan.labels.append(Label(("levels",), basis))
        return
    plan.declaration["bins"] = [_exact_text(first), _exact_text(last)]
    plan.declaration["binWeights"] = ["1"]
    plan.labels.append(Label(("bins",), basis))
    plan.labels.append(Label(("binWeights",), assumed(_RANGE_NO_ROW)))


def _clip_levels(
    plan: ColumnPlan, low: Optional[Fraction], high: Optional[Fraction]
) -> Optional[int]:
    """Clip the declared levels to the range; returns how many it dropped.

    ``None`` when no level lies inside the range, and the draw is left as it is.
    """
    levels = plan.declaration.get("levels")
    if not isinstance(levels, dict) or not levels:
        return 0
    inside: Dict[str, Fraction] = {}
    dropped = 0
    for text, weight in levels.items():
        value = Fraction(Decimal(str(text)))
        if (low is not None and value < low) or (high is not None and value > high):
            dropped += 1
            continue
        inside[text] = Fraction(Decimal(str(weight)))
    if not dropped:
        return 0
    if not inside:
        return None
    total = sum(inside.values(), Fraction(0))
    if total <= 0:
        inside = {text: Fraction(1) for text in inside}
        total = Fraction(len(inside))
    texts = share_texts([weight / total for weight in inside.values()])
    plan.declaration["levels"] = dict(zip(inside, texts, strict=True))
    return dropped


# ---------------------------------------------------------------------------
# A table with nothing published for the check to read
# ---------------------------------------------------------------------------

#: Why a table's one checked range or level list is an assumption.
ASSUMED_CHECK_REASON = (
    "No column of this table publishes a range or a list of levels, and a fidelity block "
    "needs one property, so the check reads this assumed one: a committed slice outside it "
    "is refused. Replace it with what you know of the column."
)


def assumed_check(
    plan: ColumnPlan, kind: LogicalKind, *, scorable_only: bool = False
) -> Optional[PropertyPlan]:
    """Make a column's assumed shape the one the check reads, for a table that has no other.

    A property reads a column's ``range`` or ``support``. When no column of a
    table has a published one, the first that can takes its assumed span (a number) or its
    assumed levels (a categorical) as a declaration the check reads, stated as
    an assumption with :data:`ASSUMED_CHECK_REASON`. ``None`` when the column
    has neither. With *scorable_only*, ``None`` too unless the profile publishes
    what the property is scored against (an ungrouped ``mean/1``), and nothing
    of the plan changes.
    """
    bins = plan.declaration.get("bins")
    if kind in (LogicalKind.INT, LogicalKind.DECIMAL) and isinstance(bins, list) and bins:
        if scorable_only and not plan.mean_published:
            return None
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
            scorable=plan.mean_published,
        )
    if scorable_only:
        return None  # a support property is scored against a support/1 the profile may not hold
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
