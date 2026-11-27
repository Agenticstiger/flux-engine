# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``history`` entry ``fluxsim init`` writes from a profile table's ``history`` (ADR 0017 section 13).

A table whose contract declares a validity window may publish how its versions behave
(``tables.<id>.history``, profile format 1-3-0). For such a table :func:`write_history` writes the
Simulation's ``history`` entry:

* ``entities`` is the total of ``versionsPerEntity`` and ``versions`` its classes, as ADR 0016
  section 7 writes ``children`` (:func:`flux_engine.author.children.count_children`);
* ``start`` and ``length`` are the histograms of the first start and of a version's length;
* ``until`` is the window's end the bundle writes;
* ``chain`` is the level of ``chain`` that holds on at least :data:`CHAIN_HOLDS_AT_LEAST` of
  consecutive versions. When none does, no entry is written and every published member is unused;
* ``tracked`` is each candidate column that moves in at least one published change, with its
  ``initial`` and ``transitions``; the others are carried. A candidate that is a foreign key into a
  public reference table (one the profile releases whole) declares the table's keys as its levels,
  at equal weights, so it can be tracked; with a key not published, it is carried;
* ``reasons`` is the histogram of the changes by the column they move, without ``several``;
* ``end`` is ``closeLast`` with the closed share of ``lastVersion``, when that share is above zero.
  A profile does not say which value marks an end, so ``finalVersion`` is written only from the
  contract: the natural entity's ``lifecycle.endMarker`` (:func:`declared_lifecycle`) names the
  column and the value. An end marker ends an entity with a marked version and closes no last
  version, so the share of entities that end is not the closed share but the ended entities over
  the entry's ``entities`` (ADR 0017, the amendment of 2026-11-27): the marker column's published
  first values plus its changes into the marker where every one of those cells is published, else
  the rows its histogram puts on the marker, else no end is written;
* ``activation`` is the natural entity's ``lifecycle.activation`` when the expose has that column
  at the window's type. A ``lifecycle.currentFlag`` is written nowhere: no construct of the
  engine puts a value on an entity's last version alone, so the column keeps its own draw;
* ``parent`` is written when the history's emit has a to-one relationship to another expose that the
  history can lay its entities under (:func:`_parent`). A profile counts children per parent in rows,
  which are versions here, so the published shape is kept and its total is the entities' (the
  ledger says so); a relationship on the natural entity's column is never the parent, since that
  would make the natural key a foreign key, which the generator refuses.

Every published member that is not used is returned with the reason it is not, as ADR 0014 requires.
:func:`declare_date_ranges` gives each date column of the emit that declares no range the window as
its range, since a history's emit is refused a date that would default to the window.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.author.basis import Label, assumed
from flux_engine.author.catalogue import published_levels
from flux_engine.author.children import count_children, declare_counted
from flux_engine.author.columns import ColumnPlan, cell_weights, count_cells, share_texts
from flux_engine.author.date_spread import DateSpread, declared_spread
from flux_engine.author.gap_fit import GAP_FIT_ITERATIONS, GapFit, Interval, fit_gap
from flux_engine.core.canonical import nfc
from flux_engine.core.clock import format_rfc3339_ms, parse_rfc3339_ms
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.keys import ExposeKeys, window_ends
from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM, MEAN

__all__ = [
    "CHAIN_HOLDS_AT_LEAST",
    "HistoryReads",
    "HistorySource",
    "HistoryWritten",
    "ParentCandidate",
    "Published",
    "catalogue_columns",
    "declare_date_ranges",
    "decline_history",
    "parent_candidates",
    "profile_relationship",
    "release_relationship",
    "restate_assumptions",
    "structural_record",
    "write_history",
]

#: The share of consecutive versions a ``chain`` level must hold on for the history to take it.
CHAIN_HOLDS_AT_LEAST = Fraction(9, 10)

_FREE = "F"
_MARGINAL = MARGINAL_HISTOGRAM.identifier
_DAY_MS = 86_400_000
_EPOCH = date(1970, 1, 1)
_UNITS = (LogicalKind.DATE, LogicalKind.TIMESTAMP)
_CHAINS = ("nextStart", "dayBefore")
_MEMBERS = ("versionsPerEntity", "firstStart", "versionLength", "chain", "lastVersion", "reasons")

#: A pointer into the profile, with how it was used or why it was not.
_Note = Tuple[Tuple[str, ...], str]


@dataclass(frozen=True)
class ParentCandidate:
    """A to-one relationship of the history's emit that its entities could be laid out under.

    ``column`` is the foreign key, ``parent`` the parent expose and ``parent_rows`` its rows.
    ``entry`` is the profile's ``relationships`` entry for it, ``tokens`` its pointer, both ``None``
    when the profile publishes none.
    """

    name: str
    column: str
    parent: str
    parent_rows: int
    entry: Optional[Mapping[str, Any]] = None
    tokens: Optional[Tuple[str, ...]] = None


@dataclass(frozen=True)
class HistorySource:
    """What :func:`write_history` reads of one expose: the profile table, the plans and the contract.

    ``rows`` is the expose's versions. ``plans`` holds each column's plan, by name, and ``kinds``
    its type. ``set_by_rules`` is the columns a rule, a dependency or a copy sets, which a history
    never tracks, and ``dependents`` those of them a contract ``dependency`` alone sets.
    ``window`` is ``timeBounds`` as RFC 3339 text.
    """

    expose_id: str
    table: Mapping[str, Any]
    rows: int
    plans: Mapping[str, ColumnPlan]
    kinds: Mapping[str, LogicalKind]
    contract_expose: Mapping[str, Any]
    keys: Optional[ExposeKeys]
    window: Tuple[str, str]
    minimum_cell: int
    fractional_bits: int
    set_by_rules: FrozenSet[str] = frozenset()
    dependents: FrozenSet[str] = frozenset()
    parents: Tuple[ParentCandidate, ...] = ()
    #: Each foreign key of the emit into a reference expose, by column: the expose and the keys the
    #: profile publishes for it, or ``None`` when they are not all published.
    catalogues: Mapping[str, Tuple[str, Optional[Tuple[str, ...]]]] = field(default_factory=dict)


@dataclass
class HistoryWritten:
    """What :func:`write_history` made of one table's history."""

    #: The ``history`` entry, or ``None`` when none is written.
    entry: Optional[Dict[str, Any]] = None
    #: The ``start`` column the entry reads, for the plan-time check.
    start_column: Optional[str] = None
    #: The columns the history sets, a key holds or a rule sets: none of them is drawn in the window.
    exempt: FrozenSet[str] = frozenset()
    used: List[_Note] = field(default_factory=list)
    unused: List[_Note] = field(default_factory=list)
    #: What is assumed, by subject: a sentence a person reads.
    notes: List[Tuple[str, str]] = field(default_factory=list)
    #: The relationship the entry's ``parent`` names, with its pointer into the profile when it has
    #: one: no ``counts`` entry and no spread may lay the same relationship out.
    parent_of: Optional[Tuple[str, Optional[Tuple[str, ...]]]] = None
    #: The decisions about the parent's relationship that no statistic holds, listed as used.
    records: List[_Note] = field(default_factory=list)
    #: The members of the entry that no statistic of the profile gives, by their position below the
    #: entry, with the sentence that says what they rest on: each is assumed.
    derived: Dict[Tuple[str, ...], str] = field(default_factory=dict)
    #: The end column, and how far the ends the entry's length gives lie from its published
    #: histogram (total variation), when the length is derived from the published histograms.
    fitted_end: Optional[Tuple[str, Fraction]] = None


_TIE_READ = "it shows the column on the start's day, so the history is not written"


class _Skip(Exception):
    """The history is not written, and every published member is unused for this reason."""


class _LostTie(_Skip):
    """The history is not written because a date pair of the table shows a tie it would cut.

    The pair's order is read by the decision, so it is used: a profile with it left out would
    write the history.
    """

    def __init__(self, reason: str, pair: Tuple[str, ...]) -> None:
        super().__init__(reason)
        self.pair = pair


def _group(statistic: Any) -> Optional[Mapping[str, Any]]:
    """The one published group of an ungrouped statistic, or ``None``."""
    if not isinstance(statistic, Mapping) or "groupBy" in statistic:
        return None
    groups = statistic.get("groups")
    if not isinstance(groups, list) or len(groups) != 1:
        return None
    group = groups[0]
    return group if isinstance(group, Mapping) and group.get("status") == _FREE else None


def _whole(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _pointers(base: Tuple[str, ...], history: Mapping[str, Any]) -> List[Tuple[str, ...]]:
    """Every statistic the table's history publishes, as a pointer into the profile."""
    found = [base + (key,) for key in _MEMBERS if key in history]
    columns = history.get("columns")
    for name in sorted(columns if isinstance(columns, Mapping) else ()):
        found += [base + ("columns", str(name), key) for key in ("initial", "transitions")]
    return found


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def _unit(kind: LogicalKind, milliseconds: int) -> int:
    """An instant in the chain's unit: whole days for a DATE, milliseconds for a TIMESTAMP."""
    return milliseconds // _DAY_MS if kind is LogicalKind.DATE else milliseconds


def _spell(kind: LogicalKind, value: int) -> str:
    if kind is LogicalKind.DATE:
        return (_EPOCH + timedelta(days=value)).isoformat()
    return format_rfc3339_ms(value)


def _window_units(source: HistorySource, kind: LogicalKind) -> Tuple[int, int]:
    low, high = (_unit(kind, parse_rfc3339_ms(text)) for text in source.window)
    return low, high


def _edges_ms(bins: Any) -> Optional[List[int]]:
    """A histogram's edges as whole milliseconds, or ``None`` when one is not."""
    edges: List[int] = []
    for edge in bins if isinstance(bins, list) else ():
        try:
            value = Fraction(Decimal(str(edge)))
        except ArithmeticError:
            return None
        if value.denominator != 1:
            return None
        edges.append(int(value))
    return edges if len(edges) >= 2 else None


def _merged(
    edges: Sequence[Any], weights: Sequence[Fraction]
) -> Optional[Tuple[List[Any], List[Fraction]]]:
    """The edges that ascend, a bin that collapses giving its weight to the next one that stays."""
    kept: List[Any] = [edges[0]]
    held: List[Fraction] = []
    pending = Fraction(0)
    for index, weight in enumerate(weights):
        if edges[index + 1] <= kept[-1]:
            pending += weight
            continue
        kept.append(edges[index + 1])
        held.append(weight + pending)
        pending = Fraction(0)
    if not held:
        return None
    held[-1] += pending
    return kept, held


def _bin_weights(
    node: Mapping[str, Any],
    group: Mapping[str, Any],
    pointer: Tuple[str, ...],
    source: HistorySource,
) -> Optional[List[Fraction]]:
    """Each bin's weight, a withheld count filled from what the published ones leave."""
    cells = count_cells(group, "binCounts")
    edges = node.get("bins")
    if len(cells) != len(edges if isinstance(edges, list) else ()) - 1:
        return None
    weights, _ = cell_weights(
        source.minimum_cell, cells, pointer, "binCounts", _MARGINAL, total=_whole(group.get("k"))
    )
    return weights if sum(weights, Fraction(0)) > 0 else None


def _start_histogram(
    node: Any, pointer: Tuple[str, ...], kind: LogicalKind, until: int, source: HistorySource
) -> Dict[str, Any]:
    """The ``start`` of the entry: the histogram of first starts, its edges inside the window."""
    group = _group(node)
    ms = _edges_ms(node.get("bins")) if group is not None else None
    if group is None or ms is None:
        raise _Skip(
            "the histogram is not one init reads: it is withheld or its edges are not instants"
        )
    weights = _bin_weights(node, group, pointer, source)
    if weights is None:
        raise _Skip("its cells hold no count")
    edges = [_unit(kind, value) for value in ms]
    low, _ = _window_units(source, kind)
    if any(edge < low or edge > until for edge in edges):
        raise _Skip("a bin edge lies outside [timeBounds.start, until]")
    merged = _merged(edges, weights)
    if merged is None:
        raise _Skip("its bins collapse to nothing at the chain's unit")
    kept, held = merged
    return {"bins": [_spell(kind, edge) for edge in kept], "binWeights": share_texts(held)}


def _days(value: Fraction) -> str:
    if value.denominator == 1:
        return str(int(value))
    return format(Decimal(value.numerator) / Decimal(value.denominator), "f")


def _length_histogram(
    node: Any, pointer: Tuple[str, ...], source: HistorySource
) -> Tuple[Dict[str, Any], bool]:
    """The ``length`` of the entry, and whether a bin edge below one day was raised to one."""
    group = _group(node)
    if group is None or not isinstance(node.get("bins"), list):
        raise _Skip("the histogram of version lengths is withheld or has no bins")
    try:
        days = [Fraction(Decimal(str(edge))) for edge in node["bins"]]
    except ArithmeticError:
        raise _Skip("its edges are not numbers of days") from None
    weights = _bin_weights(node, group, pointer, source)
    if weights is None or len(days) < 2:
        raise _Skip("its cells hold no count")
    raised = any(edge < 1 for edge in days)
    merged = _merged([max(edge, Fraction(1)) for edge in days], weights)
    if merged is None:
        raise _Skip("every bin of it lies below one day, and a version lasts a day or more")
    kept, held = merged
    return {
        "bins": [_days(edge) for edge in kept],
        "binWeights": share_texts(held),
        "closed": "lower",
    }, raised


# ---------------------------------------------------------------------------
# A history whose lengths the release withholds
# ---------------------------------------------------------------------------


def _withheld(node: Any) -> bool:
    """Whether a histogram of the table's history is withheld: no group of it is published, or no bins."""
    return (
        not isinstance(node, Mapping)
        or _group(node) is None
        or not isinstance(node.get("bins"), list)
    )


def _entities_of_key(source: HistorySource, natural: str) -> Optional[int]:
    """The entities, from the natural entity's published distinct count, or ``None``."""
    columns = source.table.get("columns")
    column = columns.get(natural) if isinstance(columns, Mapping) else None
    metrics = column.get("metrics") if isinstance(column, Mapping) else None
    metric = metrics.get("distinctCount") if isinstance(metrics, Mapping) else None
    if not isinstance(metric, Mapping) or metric.get("status") != _FREE:
        return None
    value = metric.get("value")
    count = (
        _whole(value) if not isinstance(value, str) else (int(value) if value.isdigit() else None)
    )
    return count if count is not None and 1 <= count <= source.rows else None


def _column_statistics(
    source: HistorySource, column: str
) -> List[Tuple[Mapping[str, Any], Tuple[str, ...]]]:
    """Each statistic of a column of the profile table, with its pointer."""
    columns = source.table.get("columns")
    found = columns.get(column) if isinstance(columns, Mapping) else None
    statistics = found.get("statistics") if isinstance(found, Mapping) else None
    return [
        (node, ("tables", source.expose_id, "columns", column, "statistics", str(index)))
        for index, node in enumerate(statistics if isinstance(statistics, list) else ())
        if isinstance(node, Mapping)
    ]


def _column_start(
    source: HistorySource, start: str, kind: LogicalKind, until: int
) -> Optional[Tuple[Dict[str, Any], Tuple[str, ...]]]:
    """The ``start`` of the entry from the start column's own histogram, and where it is read, or ``None``."""
    for node, pointer in _column_statistics(source, start):
        if node.get("definition") != _MARGINAL:
            continue
        try:
            return _start_histogram(node, pointer, kind, until, source), pointer
        except _Skip:
            continue
    return None


@dataclass(frozen=True)
class _Derived:
    """The length and the closed share read from the published histograms of the start and the end."""

    #: The ``length`` of the entry, with its derivation: ``None`` when the gaps are even.
    fit: Optional[GapFit]
    length: Dict[str, Any]
    #: The share of the end column's rows that are closed (end before ``until``), or ``None``.
    closed: Optional[Fraction]
    how: str


def _spread_days(spread: DateSpread) -> List[Interval]:
    scale = _DAY_MS if spread.kind is LogicalKind.TIMESTAMP else 1
    return [
        (Fraction(low, scale), Fraction(high, scale), share) for low, high, share in spread.bins
    ]


def _closed_ends(spread: DateSpread, until: Fraction) -> Tuple[List[Interval], Fraction]:
    """The part of the ends' histogram before *until*, shares adding up to 1, and its share of all rows."""
    found: List[Interval] = []
    closed = Fraction(0)
    for low, high, share in _spread_days(spread):
        if low >= until:
            continue
        part = share if high <= until else share * (until - low) / (high - low)
        found.append((low, min(high, until), part))
        closed += part
    if closed <= 0:
        return [], Fraction(0)
    return [(low, high, part / closed) for low, high, part in found], closed * (1 - spread.nulls)


def _derive_length(
    source: HistorySource, kind: LogicalKind, start: str, end: str, until: int
) -> Optional[_Derived]:
    """The length the entry draws and the closed share, from the published histograms of *start* and *end*.

    ``None`` when the end is drawn from no histogram, so nothing says how many versions close. The
    gaps are the histogram that turns the starts into the closed ends
    (:func:`~flux_engine.author.gap_fit.fit_gap`), or an even spread over the run's window where
    the starts are not drawn from a histogram.
    """
    plans = source.plans
    ends = declared_spread(plans[end], kind) if end in plans else None
    if ends is None:
        return None
    scale = 1 if kind is LogicalKind.DATE else _DAY_MS
    low_unit, _ = _window_units(source, kind)
    window_days = (until - low_unit) // scale
    if window_days <= 1:
        raise _Skip(
            f"the run's window holds {max(window_days, 0)} days, too few to draw a version's length in"
        )
    closed_bins, closed = _closed_ends(ends, Fraction(until, scale))
    starts = declared_spread(plans[start], kind) if start in plans else None
    fit: Optional[GapFit] = None
    if starts is not None and closed_bins:
        fit = fit_gap(_spread_days(starts), closed_bins, 1, window_days)
    if fit is None or fit.even:
        length = {"bins": ["1", str(window_days)], "binWeights": ["1"], "closed": "lower"}
        how = (
            f"the days of a version, which the release withholds: drawn evenly from 1 to "
            f"{window_days} days, the most the run's window allows"
        )
        return _Derived(None, length, closed if closed_bins else None, how)
    length = {
        "bins": [str(edge) for edge in fit.edges],
        "binWeights": share_texts(list(fit.weights)),
        "closed": "lower",
    }
    how = (
        f"the days of a version, which the release withholds: derived from the published "
        f"histograms of {start!r} and {end!r}, as the histogram of gaps that turns the first into "
        f"the closed part of the second (each bin spread evenly, {GAP_FIT_ITERATIONS} "
        "multiplicative updates from an even start)"
    )
    return _Derived(fit, length, closed, how)


# ---------------------------------------------------------------------------
# The chain, the end, the entities and their versions
# ---------------------------------------------------------------------------


def _chain_level(node: Any, kind: LogicalKind) -> Tuple[str, Fraction]:
    """The level of ``chain`` that holds on at least :data:`CHAIN_HOLDS_AT_LEAST`, and its share."""
    group = _group(node)
    total = _whole(group.get("k")) if group is not None else None
    levels = node.get("levels") if isinstance(node, Mapping) else None
    if group is None or not total or not isinstance(levels, list):
        raise _Skip("the versions do not chain: the profile publishes no share of how they meet")
    cells = count_cells(group, "levelCounts")
    for level in _CHAINS:
        if level not in levels or levels.index(level) >= len(cells):
            continue
        count = cells[levels.index(level)].count
        if count is None or Fraction(count, total) < CHAIN_HOLDS_AT_LEAST:
            continue
        if level == "dayBefore" and kind is not LogicalKind.DATE:
            raise _Skip("the versions chain by dayBefore, which a TIMESTAMP column cannot take")
        return level, Fraction(count, total)
    raise _Skip(
        "the versions do not chain: neither nextStart nor dayBefore holds on "
        f"{_percent(CHAIN_HOLDS_AT_LEAST)} of consecutive versions"
    )


def _percent(share: Fraction) -> str:
    return f"{float(share * 100):g} %"


def _closed_share(
    node: Any, pointer: Tuple[str, ...], source: HistorySource
) -> Optional[Tuple[Fraction, Fraction]]:
    """The closed weight and the total weight of ``lastVersion``, or ``None`` when none is closed."""
    group = _group(node)
    levels = node.get("levels") if isinstance(node, Mapping) else None
    if group is None or not isinstance(levels, list) or "closed" not in levels:
        return None
    cells = count_cells(group, "levelCounts")
    if len(cells) != len(levels):
        return None
    weights, _ = cell_weights(
        source.minimum_cell, cells, pointer, "levelCounts", LEVEL_HISTOGRAM.identifier,
        total=_whole(group.get("k")),
    )  # fmt: skip
    total = sum(weights, Fraction(0))
    closed = weights[levels.index("closed")]
    if total <= 0 or closed <= 0:
        return None
    return closed, total


def _end(node: Any, pointer: Tuple[str, ...], source: HistorySource) -> Optional[Dict[str, Any]]:
    """The ``end`` of the entry: ``closeLast`` with the closed share, or ``None`` when none ends."""
    found = _closed_share(node, pointer, source)
    if found is None:
        return None
    closed, total = found
    return {"mode": "closeLast", "share": share_texts([closed, total - closed])[0]}


def _final_end(marker: Tuple[str, str], share: Fraction) -> Dict[str, Any]:
    """The ``finalVersion`` end of the contract's end marker, with the share of entities that end.

    The share is of every entity (ADR 0017, the amendment of 2026-11-24). Under an end marker it is
    the ended entities over the entities (the amendment of 2026-11-27), since no last version of a
    profile is closed there.
    """
    column, value = marker
    return {
        "mode": "finalVersion",
        "share": share_texts([share, 1 - share])[0],
        "column": column,
        "value": value,
    }


def _versions(
    table: Mapping[str, Any], base: Tuple[str, ...], source: HistorySource
) -> Tuple[int, Optional[Dict[str, Any]], List[_Note], List[_Note], List[Tuple[str, str]]]:
    """The entities, and the ``versions`` shape when the layout can read the histogram.

    The classes are those :func:`~flux_engine.author.children.count_children` writes for a
    relationship, the entities being the parents and the versions the children, so the open last
    class takes the upper edge whose even spread holds the total of versions.
    """
    pointer = base + ("versionsPerEntity",)
    node = table.get("versionsPerEntity")
    group = _group(node)
    entities = _whole(group.get("k")) if group is not None else None
    if group is None or not entities or entities < 1:
        raise _Skip("versionsPerEntity is withheld or counts no entity")
    if entities > source.rows:
        raise _Skip(f"it counts {entities} entities and the table holds {source.rows} versions")
    total = {"definition": MEAN.identifier, "groups": [
        {"group": "", "k": entities, "Sx": str(source.rows * 2**source.fractional_bits), "status": _FREE}
    ]}  # fmt: skip
    relationship = {"statistics": [node, total]}
    counted = count_children(
        relationship, base, source.rows, entities,
        minimum_cell=source.minimum_cell, fractional_bits=source.fractional_bits,
    )  # fmt: skip
    edges = _edges_ms(node.get("bins")) if isinstance(node, Mapping) else None
    if counted is None or counted.why_not is not None or edges is None or edges[0] < 1:
        why = "the histogram of versions per entity is not one the layout can read"
        return entities, None, [], [(pointer, why)], []
    shape = dict(counted.members["children"])
    notes = [("history versions", counted.assumption)] if counted.assumption else []
    used = [(pointer, "the entities, and the classes of the entry's versions with their weights")]
    return entities, shape, used, [], notes


# ---------------------------------------------------------------------------
# Tracked columns
# ---------------------------------------------------------------------------


def _levels_of(plan: ColumnPlan) -> Optional[Tuple[str, ...]]:
    declared = plan.declaration.get("levels")
    if isinstance(declared, Mapping) and declared:
        return tuple(str(level) for level in declared)
    return plan.level_names or None


def _level_weights(
    node: Mapping[str, Any],
    group: Mapping[str, Any],
    pointer: Tuple[str, ...],
    source: HistorySource,
    *,
    own: Optional[str] = None,
    ended: Optional[str] = None,
    dropped: Sequence[str] = (),
) -> Optional[Dict[str, Fraction]]:
    """The weight of each level of a level histogram's group; the levels *own* and *ended* are zero.

    *own* is a level known to be zero (a group's own level in a transition row), *ended* the value
    the history's ending version holds, which no other version takes.
    """
    levels = node.get("levels")
    cells = count_cells(group, "levelCounts")
    if not isinstance(levels, list) or len(cells) != len(levels):
        return None
    total = _whole(group.get("k"))
    for zeroed in (own, ended, *dropped):
        if zeroed is not None and zeroed in levels:
            position = levels.index(zeroed)
            if total is not None:
                # What the level held leaves the records the withheld cells share.
                total = max(0, total - (cells[position].count or 0))
            cells[position] = replace(cells[position], count=0)
    weights, _ = cell_weights(
        source.minimum_cell, cells, pointer, "levelCounts", LEVEL_HISTOGRAM.identifier,
        total=total,
    )  # fmt: skip
    return {str(level): weight for level, weight in zip(levels, weights, strict=True)}


def _moves(node: Any) -> bool:
    """Whether the column moves in at least one published change: a count off the diagonal."""
    groups = node.get("groups") if isinstance(node, Mapping) else None
    levels = node.get("levels") if isinstance(node, Mapping) else None
    if not isinstance(groups, list) or not isinstance(levels, list):
        return False
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        for level, cell in zip(levels, count_cells(group, "levelCounts"), strict=False):
            if str(level) != str(group.get("group")) and (cell.count or 0) > 0:
                return True
    return False


def _rows(
    node: Mapping[str, Any],
    pointer: Tuple[str, ...],
    source: HistorySource,
    ended: Optional[str] = None,
    dropped: Sequence[str] = (),
) -> Dict[str, Dict[str, str]]:
    """Each level's weights of the levels it moves to, for each group that moves.

    The level *ended*, when given, is no source and no target: only the ending version holds it.
    """
    rows: Dict[str, Dict[str, str]] = {}
    for group in node.get("groups") or ():
        if not isinstance(group, Mapping) or group.get("status") != _FREE:
            continue
        level = str(group.get("group"))
        if level == ended or level in dropped:
            continue
        weights = _level_weights(
            node, group, pointer, source, own=level, ended=ended, dropped=dropped
        )
        if weights is None:
            continue
        moved = {
            target: weight for target, weight in weights.items() if target != level and weight > 0
        }
        if moved:
            rows[level] = dict(zip(moved, share_texts(list(moved.values())), strict=True))
    return rows


def _reachable(start: Sequence[str], rows: Mapping[str, Mapping[str, str]]) -> List[str]:
    """The levels a chain can reach from *start* that have no row to leave by."""
    seen, pending, missing = set(start), list(start), []
    while pending:
        level = pending.pop()
        if level not in rows:
            missing.append(level)
            continue
        for target in rows[level]:
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return sorted(missing)


def _initial(
    node: Any,
    pointer: Tuple[str, ...],
    source: HistorySource,
    ended: Optional[str] = None,
    dropped: Sequence[str] = (),
) -> Optional[Dict[str, Any]]:
    group = _group(node)
    weights = (
        _level_weights(node, group, pointer, source, ended=ended, dropped=dropped)
        if group is not None
        else None
    )
    if not weights:
        return None
    kept = {level: weight for level, weight in weights.items() if weight > 0}
    if not kept:
        return None
    return {"levels": list(kept), "levelWeights": share_texts(list(kept.values()))}


@dataclass
class _Tracking:
    """The columns of a history that are tracked, and the notes that come of reading them."""

    columns: List[Dict[str, Any]] = field(default_factory=list)
    used: List[_Note] = field(default_factory=list)
    unused: List[_Note] = field(default_factory=list)


def _refusal_of(
    name: str,
    plan: Optional[ColumnPlan],
    source: HistorySource,
    fixed: FrozenSet[str],
    given: FrozenSet[str],
) -> Optional[str]:
    """Why a candidate column cannot be tracked in this emit, or ``None``."""
    if plan is not None and plan.levels_refused is not None and _levels_of(plan) is None:
        return plan.levels_refused
    if plan is None or _levels_of(plan) is None:
        return "the emit declares no levels for the column, so it is carried"
    return _held_back(name, plan, source, fixed, given)


def _held_back(
    name: str,
    plan: ColumnPlan,
    source: HistorySource,
    fixed: FrozenSet[str],
    given: FrozenSet[str],
) -> Optional[str]:
    """What else keeps a column that has levels from being tracked: the history, a rule or a draw owns it.

    A column that is a foreign key into a reference table has the table's keys as its levels, which
    :func:`_declare_keys` declares once it is tracked, so only this is asked of it.
    """
    if name in fixed:
        return "the column is a key, or a validity column, which the history sets"
    if name in source.set_by_rules:
        return "a rule, a dependency or a copy sets the column, so it is carried"
    if "nullFraction" in plan.declaration:
        return "the column declares a nullFraction, and a change cannot be NULL, so it is carried"
    if name in given:
        return "another column is drawn given it, and a carried column keeps its first value"
    return None


def _track_one(
    name: str,
    entry: Mapping[str, Any],
    base: Tuple[str, ...],
    source: HistorySource,
    declared: Tuple[str, ...],
    ended: Optional[str] = None,
    dropped: Sequence[str] = (),
) -> Dict[str, Any]:
    """The ``tracked`` item of one column, or :class:`_Skip` with why it is carried.

    *ended* is the value the entry's ending version holds in this column, which no tracked change
    or first value takes; without a published first value the column's own weights would take it,
    so the column is carried.
    """
    moves = entry.get("transitions")
    if not isinstance(moves, Mapping) or not _moves(moves):
        raise _Skip("no published change moves the column, so it is carried")
    rows = _rows(moves, base + ("transitions",), source, ended, dropped)
    initial = _initial(entry.get("initial"), base + ("initial",), source, ended, dropped)
    if ended is not None and initial is None:
        raise _Skip(
            f"no first value is published, and the column's own weights would give {ended!r}, "
            "which only the ending version holds, so it is carried"
        )
    named = set(rows) | {target for row in rows.values() for target in row}
    named |= set(initial["levels"]) if initial else set()
    if not named <= set(declared):
        raise _Skip("it names a level the column does not declare, so it is carried")
    start = initial["levels"] if initial else list(declared)
    missing = _reachable(start, rows)
    if missing:
        raise _Skip(f"no published transition leaves the level {missing[0]!r}, so it is carried")
    item: Dict[str, Any] = {"column": name}
    if initial is not None:
        item["initial"] = initial
    item["transitions"] = rows
    return item


_KEYS_REASON = (
    "The history tracks this foreign key into {target}, a table the profile releases whole, so the "
    "column declares {target}'s {count} keys as its levels, at equal weights: a version's value is "
    "a key, and the levels it first takes and moves between are the history's own."
)


def _catalogue_keys(
    name: str, plan: Optional[ColumnPlan], source: HistorySource
) -> Union[Tuple[str, ...], str, None]:
    """The keys of the reference table a column without levels points at; a sentence when they are not all published; else ``None``."""
    if plan is None or _levels_of(plan) is not None or name not in source.catalogues:
        return None
    target, keys = source.catalogues[name]
    if not keys:
        return (
            f"the column is a foreign key into {target}, whose keys are not all published, so "
            "no level is guessed and it is carried"
        )
    return keys


def _declare_keys(plan: ColumnPlan, keys: Sequence[str], target: str) -> None:
    """Declare the keys of the reference table as the levels of the tracked foreign key."""
    reason = _KEYS_REASON.format(target=target, count=len(keys))
    texts = share_texts([Fraction(1)] * len(keys))
    plan.declaration["levels"] = dict(zip(keys, texts, strict=True))
    plan.labels.append(Label(("levels",), assumed(reason)))
    plan.assumptions = [reason]
    plan.choices = []
    plan.how = f"a foreign key to {target}, tracked by the history over its {len(keys)} keys"
    plan.passed = [
        (
            place,
            (
                "its own histogram of values adds no bound: the column's levels are the "
                "reference table's keys, which the history moves between"
                if "not levels of the catalogue's keys" in why
                else why
            ),
        )
        for place, why in plan.passed
    ]


def _track_candidate(
    name: str,
    entry: Mapping[str, Any],
    where: Tuple[str, ...],
    source: HistorySource,
    held: Tuple[FrozenSet[str], FrozenSet[str]],
    end_marker: Optional[Tuple[str, str]] = None,
) -> Union[Dict[str, Any], str]:
    """The ``tracked`` item of candidate column *name*, or the sentence that says why it is carried.

    *held* is the columns the history sets or a key holds, and the columns another is drawn given.
    """
    fixed, given = held
    plan = source.plans.get(name)
    keys = _catalogue_keys(name, plan, source)
    if isinstance(keys, str):
        return keys
    if keys and plan is not None:
        reason = _held_back(name, plan, source, fixed, given)
    else:
        reason = _refusal_of(name, plan, source, fixed, given)
    if reason is not None:
        return reason
    assert plan is not None
    try:
        ended = end_marker[1] if end_marker is not None and end_marker[0] == name else None
        item = _track_one(
            name, entry, where, source, keys or _levels_of(plan) or (), ended, plan.dropped_levels
        )
    except _Skip as skipped:
        return str(skipped)
    if keys:
        _declare_keys(plan, keys, source.catalogues[name][0])
    return item


def _track(
    history: Mapping[str, Any],
    base: Tuple[str, ...],
    source: HistorySource,
    fixed: FrozenSet[str],
    end_marker: Optional[Tuple[str, str]] = None,
) -> _Tracking:
    """Each candidate column that moves, with its ``initial`` and ``transitions``; the others are carried."""
    result = _Tracking()
    columns = history.get("columns")
    candidates = columns if isinstance(columns, Mapping) else {}
    given = frozenset(
        str(name)
        for other, plan in source.plans.items()
        if other not in candidates
        for name in _given_names(plan)
    )
    for name in sorted(str(key) for key in candidates):
        where = base + ("columns", name)
        item = _track_candidate(name, candidates[name], where, source, (fixed, given), end_marker)
        if isinstance(item, str):
            result.unused += [(where + (key,), item) for key in ("initial", "transitions")]
            continue
        result.columns.append(item)
        result.used.append(
            (where + ("transitions",), "the moves between levels of the tracked column, per level")
        )
        if "initial" in item:
            result.used.append((where + ("initial",), "the level a first version takes"))
        else:
            result.unused.append(
                (
                    where + ("initial",),
                    "withheld or empty, so the column's own levels give its first value",
                )
            )
    return result


def _given_names(plan: ColumnPlan) -> List[str]:
    declared = plan.declaration.get("given")
    names = [declared] if isinstance(declared, str) else declared
    return [str(name) for name in names] if isinstance(names, list) else []


def _reasons(
    node: Any, pointer: Tuple[str, ...], tracked: Sequence[str], source: HistorySource
) -> Optional[Dict[str, Any]]:
    """The ``reasons`` of the entry: the changes by the tracked column they move, without ``several``."""
    group = _group(node)
    weights = _level_weights(node, group, pointer, source) if group is not None else None
    if not weights:
        return None
    kept = {name: weights.get(name, Fraction(0)) for name in tracked}
    if sum(kept.values(), Fraction(0)) <= 0:
        return None
    named = {name: weight for name, weight in kept.items() if weight > 0}
    return {"levels": list(named), "levelWeights": share_texts(list(named.values()))}


# ---------------------------------------------------------------------------
# The parent
# ---------------------------------------------------------------------------

_PARENT_REASON = (
    "The history lays its entities out under the parent rows of this relationship (ADR 0017 "
    "section 3), so every version of an entity copies its parent's key and the foreign key "
    "declares no draw of its own."
)
_PARENT_HOW = "a foreign key, set by the history's layout of its entities under their parent"
_PARENT_SETS = (
    "the history sets the foreign key from its parent rows, so this statistic of its own values "
    "adds no bound to it"
)
_PARENT_ROWS = (
    "The profile counts children per parent in version rows, not in entities. The shape of the "
    "published classes is kept as the shape of entities per parent, and its total is the "
    "entities'."
)
_DRAWN_GIVEN = ("given", "binWeightsGiven", "levelWeightsGiven")


def _parent_hold(source: HistorySource, candidate: ParentCandidate, fixed: FrozenSet[str]) -> str:
    """Why the entities cannot be laid out under *candidate*, or an empty text when they can."""
    plan = source.plans.get(candidate.column)
    if plan is None:
        return "its column is not one the bundle plans"
    if candidate.column in fixed:
        return "its column is a key, or a validity column, which the history sets"
    if candidate.column in source.set_by_rules and not _held_by_layout(source, candidate):
        return "a rule, a dependency or a copy sets its column"
    nulls = plan.declaration.get("nullFraction")
    if nulls is not None and Decimal(str(nulls)) != 0:
        return "its column declares a nullFraction, and the layout gives every entity a parent"
    if any(key in plan.declaration for key in _DRAWN_GIVEN):
        return "its column is drawn given another"
    if candidate.parent_rows < 1:
        return "its parent holds no row"
    return ""


def _held_by_layout(source: HistorySource, candidate: ParentCandidate) -> bool:
    """Whether the only rule that sets *candidate*'s column is a dependency the layout holds.

    The layout gives every version of an entity one parent row, so a contract ``dependency`` from
    columns that include the natural entity to the parent's foreign key alone holds by it, and the
    plan draws nothing for it (ADR 0017, amendment of 2026-11-25). Any other dependency, or a
    derive or copy of the column, keeps the column from the layout.
    """
    keys = source.keys
    if keys is None or candidate.column not in source.dependents:
        return False
    natural = {entity.column for entity in keys.entities if entity.kind == "natural"}
    if len(natural) != 1:
        return False
    contract = source.contract_expose.get("contract")
    invariants = contract.get("invariants") if isinstance(contract, Mapping) else None
    naming = [
        rule
        for rule in (
            entry.get("dependency") if isinstance(entry, Mapping) else None
            for entry in (invariants if isinstance(invariants, list) else ())
        )
        if isinstance(rule, Mapping)
        and isinstance(rule.get("to"), list)
        and candidate.column in rule["to"]
    ]
    return bool(naming) and all(
        set(rule["to"]) == {candidate.column}
        and isinstance(rule.get("from"), list)
        and natural <= set(rule["from"])
        for rule in naming
    )


def _publishes_children(candidate: ParentCandidate) -> bool:
    statistics = candidate.entry.get("statistics") if candidate.entry is not None else None
    return any(
        isinstance(statistic, Mapping)
        and statistic.get("definition") == _MARGINAL
        and _group(statistic) is not None
        for statistic in (statistics if isinstance(statistics, list) else ())
    )


def _pick_parent(
    source: HistorySource, fixed: FrozenSet[str], written: HistoryWritten
) -> Optional[ParentCandidate]:
    """The relationship the history's entities are laid out under, or ``None``.

    The candidates are the to-one relationships of the emit into another expose, each held by what
    :func:`_parent_hold` says. With one candidate, it is the parent. With several, the parent is
    the one the profile publishes children per parent for, when exactly one is; otherwise none is
    written, since two parents would be a guess, and the note names them.
    """
    held = [(item, _parent_hold(source, item, fixed)) for item in source.parents]
    free = [item for item, why in held if not why]
    for item, why in held:
        if why:
            written.notes.append(
                (f"history parent {item.name}", f"not the parent of the entities: {why}")
            )
    if len(free) <= 1:
        return free[0] if free else None
    published = [item for item in free if _publishes_children(item)]
    if len(published) == 1:
        written.notes.append(
            (
                "history parent",
                f"The emit has {len(free)} relationships to another expose "
                f"({', '.join(item.name for item in free)}); the entities are laid out under "
                f"{published[0].name}, the only one the profile publishes children per parent for.",
            )
        )
        return published[0]
    written.notes.append(
        (
            "history parent",
            f"The emit has {len(free)} relationships to another expose "
            f"({', '.join(item.name for item in free)}) and "
            + (
                "the profile publishes children per parent for none of them"
                if not published
                else "the profile publishes children per parent for more than one"
            )
            + ", so no parent is written: choosing one would be a guess.",
        )
    )
    return None


def _as_entities(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    """The relationship with each published mean set aside: a mean of version rows is not the entities'."""
    statistics = [
        (
            {"definition": MEAN.identifier, "groups": []}
            if isinstance(statistic, Mapping) and statistic.get("definition") == MEAN.identifier
            else statistic
        )
        for statistic in entry.get("statistics") or ()
    ]
    return {**entry, "statistics": statistics}


def structural_record(entry: Mapping[str, Any], minimum: bool, otherwise: str) -> str:
    """What the bundle does with a relationship's ``orphan`` and ``childless`` classes, one sentence.

    A ``childless`` class is written as ``minChildren 1`` on the history's parent when *minimum*,
    and otherwise not written, for the reason *otherwise*.
    """
    structural = entry.get("structural")
    classes = structural if isinstance(structural, list) else []
    parts: List[str] = []
    if "orphan" in classes:
        parts.append(
            "orphan: checked, since the bundle builds every foreign key so that it finds its parent"
        )
    if "childless" in classes:
        parts.append(
            "childless: written as minChildren 1 on the history's parent"
            if minimum
            else f"childless: not written, because {otherwise}"
        )
    return "; ".join(parts)


def _parent(
    source: HistorySource, candidate: ParentCandidate, entities: int, written: HistoryWritten
) -> Dict[str, Any]:
    """The ``parent`` of the entry for *candidate*, with what is read of its statistics."""
    parent: Dict[str, Any] = {"relationship": candidate.name}
    subject = f"history parent {candidate.name}"
    if candidate.entry is None or candidate.tokens is None:
        written.notes.append(
            (
                subject,
                "The profile publishes no children per parent for it, so the entities are spread "
                "over the parent rows as alike as whole counts allow.",
            )
        )
        return parent
    tokens, entry = candidate.tokens, candidate.entry
    _list_unused(
        entry,
        tokens,
        written,
        means=True,
        text="a mean of version rows per parent, not of entities, so it sets no total for the "
        "history's parent",
    )
    counted = count_children(
        _as_entities(entry), tokens, entities, candidate.parent_rows,
        minimum_cell=source.minimum_cell, fractional_bits=source.fractional_bits,
    )  # fmt: skip
    if counted is None or counted.why_not is not None:
        why = (counted.why_not if counted is not None else None) or (
            "the histogram of children per parent is not one the layout can read"
        )
        text = f"no children for the history's parent: {why}"
        _list_unused(entry, tokens, written, means=False, text=text)
        written.notes.append((subject, f"No children per parent is written: {why}."))
        return parent
    parent.update(counted.members)
    written.used += [
        (pointer, "the classes of the history parent's children, read as entities per parent")
        for pointer, _ in counted.used
    ]
    written.unused += list(counted.unused)
    written.notes.append((subject, _PARENT_ROWS))
    if counted.assumption:
        written.notes.append((subject, counted.assumption))
    record = structural_record(
        entry, "minChildren" in parent, "the children per parent are not laid out"
    )
    if record:
        written.records.append((tokens + ("structural",), record))
    return parent


def _statistics_of(entry: Mapping[str, Any]) -> List[Tuple[int, bool]]:
    """Each published statistic of a profile relationship by its place, with whether it is a mean."""
    statistics = entry.get("statistics")
    return [
        (
            index,
            isinstance(statistic, Mapping) and statistic.get("definition") == MEAN.identifier,
        )
        for index, statistic in enumerate(statistics if isinstance(statistics, list) else ())
    ]


def _list_unused(
    entry: Mapping[str, Any],
    tokens: Tuple[str, ...],
    written: HistoryWritten,
    *,
    means: bool,
    text: str,
) -> None:
    """List each published statistic of the relationship that is a mean (*means*), or is none, as unused with *text*."""
    written.unused += [
        (tokens + ("statistics", str(index)), text)
        for index, is_mean in _statistics_of(entry)
        if is_mean == means
    ]


def parent_candidates(
    expose_id: str,
    keys: ExposeKeys,
    rows_of: Mapping[str, int],
    relationships: Any,
) -> Tuple[ParentCandidate, ...]:
    """The to-one relationships of *expose_id* that its entities could be laid out under.

    Each is a many-to-one relationship of one column into another written expose (*rows_of* holds
    the rows of each) that is no key of the emit. A profile ``relationships`` entry between the
    same tables and columns is attached to it, with its pointer.
    """
    key_sets = {frozenset(key) for key in (*keys.keys, *keys.instant_keys)}
    published = relationships if isinstance(relationships, list) else []
    found: List[ParentCandidate] = []
    for relationship in keys.relationships:
        if (
            relationship.cardinality != "many_to_one"
            or len(relationship.from_columns) != 1
            or relationship.target == expose_id
            or relationship.target not in rows_of
            or frozenset(relationship.from_columns) in key_sets
        ):
            continue
        entry, tokens = profile_relationship(published, expose_id, relationship)
        found.append(
            ParentCandidate(
                relationship.name,
                relationship.from_columns[0],
                relationship.target,
                rows_of[relationship.target],
                entry,
                tokens,
            )
        )
    return tuple(found)


def profile_relationship(
    published: Sequence[Any], child: str, declared: Any
) -> Tuple[Optional[Mapping[str, Any]], Optional[Tuple[str, ...]]]:
    """The profile's ``relationships`` entry for the relationship *declared* of *child*, with its pointer."""
    for index, item in enumerate(published):
        if _is_relationship(item, child, declared):
            return item, ("relationships", str(index))
    return None, None


def _is_relationship(item: Any, child: str, declared: Any) -> bool:
    """Whether a profile ``relationships`` entry is *declared*, read from *child*."""
    if not isinstance(item, Mapping):
        return False
    parent, held = item.get("parent"), item.get("child")
    if not isinstance(parent, Mapping) or not isinstance(held, Mapping):
        return False
    return (
        parent.get("table") == declared.target
        and held.get("table") == child
        and tuple(map(str, parent.get("columns") or ())) == tuple(declared.to_columns)
        and tuple(map(str, held.get("columns") or ())) == tuple(declared.from_columns)
    )


def catalogue_columns(
    keys: ExposeKeys, reference_ids: FrozenSet[str], references: Any
) -> Dict[str, Tuple[str, Optional[Tuple[str, ...]]]]:
    """Each foreign key of the emit into a reference expose, by column, with the keys published for it.

    The relationships are to-one and of one column on each side. *references* is the profile's
    ``references`` section; the keys are ``None`` when its table does not publish them all.
    """
    found: Dict[str, Tuple[str, Optional[Tuple[str, ...]]]] = {}
    for relationship in keys.relationships:
        if (
            relationship.cardinality != "many_to_one"
            or len(relationship.from_columns) != 1
            or len(relationship.to_columns) != 1
            or relationship.target not in reference_ids
        ):
            continue
        table = references.get(relationship.target) if isinstance(references, Mapping) else None
        published = (
            published_levels(table, relationship.to_columns[0])
            if isinstance(table, Mapping)
            else None
        )
        found[relationship.from_columns[0]] = (relationship.target, published)
    return found


def release_relationship(
    lists: Tuple[List[Any], List[Any], List[Any], List[Any], List[Any]],
    emit: str,
    relationship: str,
    tokens: Optional[Tuple[str, ...]],
) -> None:
    """Take a relationship out of the counted layout or the spread that another reader now sets.

    *lists* are the ``counts`` entries and the used, unused, record and note lists the layout made
    of the profile's relationships; each is changed in place. The history's ``parent`` reads the
    relationship's statistics itself, so what the layout said of them is dropped.
    """
    entries, used, unused, records, notes = lists
    entries[:] = [
        item
        for item in entries
        if not (item.get("emit") == emit and item.get("relationship") == relationship)
    ]
    if tokens is not None:
        for held in (used, unused, records):
            held[:] = [item for item in held if tuple(item[0][: len(tokens)]) != tokens]
    notes[:] = [item for item in notes if item[0] != f"{emit}.{relationship}"]


@dataclass(frozen=True)
class Published:
    """What the profile publishes beyond a history's own table, which an entry or a projection reads.

    ``relationships`` and ``references`` are the profile's sections of those names. ``spread`` is
    what the counted layout made of the relationships: its ``counts`` entries and the used,
    unused, record and note lists, which a history's ``parent`` or a projected emit takes
    statistics from and changes in place.
    """

    relationships: Any
    references: Any
    spread: Any

    def lists(self) -> Tuple[List[Any], List[Any], List[Any], List[Any], List[Any]]:
        """The ``counts`` entries, then the used, unused, record and note lists of the spread."""
        spread = self.spread
        return (spread.entries, spread.used, spread.unused, spread.records, spread.notes)


class HistoryReads:
    """What the histories of the written *exposes* read of the other exposes and of the profile."""

    def __init__(self, exposes: Sequence[Any], published: Published) -> None:
        self.published = published
        self.rows_of = {
            item.expose_id: item.rows
            for item in exposes
            if item.skipped is None and not item.reference
        }
        self.reference_ids = frozenset(item.expose_id for item in exposes if item.reference)

    def candidates(self, expose_id: str, held: Optional[ExposeKeys]) -> Tuple[ParentCandidate, ...]:
        """The relationships the entities of *expose_id* could be laid out under (:func:`parent_candidates`)."""
        if held is None:
            return ()
        return parent_candidates(expose_id, held, self.rows_of, self.published.relationships)

    def catalogues(
        self, held: Optional[ExposeKeys]
    ) -> Dict[str, Tuple[str, Optional[Tuple[str, ...]]]]:
        """The foreign keys of an emit into a reference expose, with their keys (:func:`catalogue_columns`)."""
        if held is None:
            return {}
        return catalogue_columns(held, self.reference_ids, self.published.references)

    def release(self, expose_id: str, written: HistoryWritten) -> None:
        """Take the relationship the history's ``parent`` reads off the counted layout."""
        if written.parent_of is not None:
            release_relationship(self.published.lists(), expose_id, *written.parent_of)
            self.published.spread.records.extend(written.records)


def restate_assumptions(expose: Any) -> None:
    """Take the assumptions of the authored *expose*'s columns from what their plans say now.

    A history changes how some columns are drawn after the exposes were planned, so the sentences
    a column's earlier plan assumed are replaced by the ones its plan assumes now.
    """
    names = {plan.name for plan in expose.columns}
    expose.assumptions = [item for item in expose.assumptions if item[0] not in names] + [
        (plan.name, sentence) for plan in expose.columns for sentence in plan.assumptions
    ]


# ---------------------------------------------------------------------------
# The entry
# ---------------------------------------------------------------------------


def _validity(source: HistorySource) -> Tuple[LogicalKind, Tuple[str, ...], str, str]:
    """The type of the chain, the natural entity's columns, the start and the end, or :class:`_Skip`."""
    ends = window_ends(source.contract_expose.get("semantics"))
    natural = tuple(
        entity.column
        for entity in (source.keys.entities if source.keys is not None else ())
        if entity.kind == "natural"
    )
    start, end = ends.get("start"), ends.get("end")
    if len(natural) != 1 or start is None or end is None:
        raise _Skip("the contract declares no validity window for the expose")
    kinds = (source.kinds.get(start), source.kinds.get(end))
    if kinds[0] not in _UNITS or kinds[0] is not kinds[1] or kinds[0] is None:
        raise _Skip("the validity window is not two columns of one DATE or TIMESTAMP type")
    return kinds[0], natural, start, end


def declared_end(contract_expose: Mapping[str, Any], end: str) -> Mapping[str, Any]:
    """The ``validityParams`` the contract declares on its validity window's end, or ``{}``.

    FLUID 0.7.7 declares a versioned table's conventions on the ``isEnd`` dimension: ``openEnd``,
    the value an open version's end holds (``null`` or a literal of the end column's type), and
    ``chain``, how a closed version's end meets the same entity's next start.
    """
    semantics = contract_expose.get("semantics")
    dimensions = semantics.get("dimensions") if isinstance(semantics, Mapping) else None
    for dimension in dimensions if isinstance(dimensions, list) else ():
        if not isinstance(dimension, Mapping) or not isinstance(dimension.get("name"), str):
            continue
        params = dimension.get("typeParams")
        validity = params.get("validityParams") if isinstance(params, Mapping) else None
        if (
            isinstance(validity, Mapping)
            and validity.get("isEnd")
            and nfc(dimension["name"]) == end
        ):
            return validity
    return {}


def _open_end(source: HistorySource, end: str, declared: Mapping[str, Any]) -> Optional[str]:
    """The value an open version's end holds: the contract's ``openEnd``, or ``None`` for NULL.

    Raises :class:`_Skip` when no version can be left open as the contract says: the end column is
    required and the contract declares no open end, or a NULL one, or one that is not text. The
    value is never read from the profile: an end the data holds is not a declaration of it.
    """
    from flux_engine.seam.contract import compile_expose

    required = end in {
        column.name for column in compile_expose(source.contract_expose).columns if column.required
    }
    if "openEnd" not in declared or declared["openEnd"] is None:
        if required:
            declared_null = "openEnd" in declared
            raise _Skip(
                f"the end column {end!r} is required, and the contract declares "
                + ("a NULL open end" if declared_null else "no open end")
                + ": declare the value an open version's end holds as openEnd on the validity "
                "window's end dimension"
            )
        return None
    value = declared["openEnd"]
    if not isinstance(value, str):
        raise _Skip(f"the contract's openEnd {value!r} is not a literal of the end column's type")
    return value


def _declared_chain(declared: Mapping[str, Any], kind: LogicalKind) -> Optional[str]:
    """The chain the contract declares on its validity window's end, or ``None`` when it declares none.

    Raises :class:`_Skip` for a chain the engine does not generate: ``none`` (versions that need
    not meet), and ``dayBefore`` on TIMESTAMP columns.
    """
    chain = declared.get("chain")
    if chain is None:
        return None
    if chain == "none":
        raise _Skip(
            "the contract declares chain none, versions that need not meet, which the engine does "
            "not generate"
        )
    if chain not in _CHAINS:
        raise _Skip(f"the contract declares chain {chain!r}, which the engine does not generate")
    if chain == "dayBefore" and kind is not LogicalKind.DATE:
        raise _Skip("the contract declares chain dayBefore, which a TIMESTAMP column cannot take")
    return str(chain)


def _chain_reason(history: Mapping[str, Any], kind: LogicalKind, level: str) -> str:
    """What the profile's share of how versions meet says, beside the chain the contract declares."""
    declared = f"the contract declares the chain {level}, which sets the entry's chain"
    try:
        found, share = _chain_level(history.get("chain"), kind)
    except _Skip:
        return declared
    if found == level:
        return f"{declared}; it holds on {_percent(share)} of consecutive versions"
    return f"{declared}; the versions meet by {found} on {_percent(share)} of consecutive versions"


_LIFECYCLE_KEYS = ("activation", "endMarker", "currentFlag")


def declared_lifecycle(contract_expose: Mapping[str, Any], natural: str) -> Mapping[str, Any]:
    """The ``lifecycle`` the contract declares on the natural entity keyed by column *natural*, or ``{}``.

    FLUID 0.7.7 declares how the versions of an entity begin and end on the model's natural entity:
    ``activation`` (a column holding the entity's first start on every version), ``endMarker`` (a
    column and the value its ending version holds) and ``currentFlag`` (a column and the value its
    last version holds).
    """
    semantics = contract_expose.get("semantics")
    entities = semantics.get("entities") if isinstance(semantics, Mapping) else None
    for entity in entities if isinstance(entities, list) else ():
        if not isinstance(entity, Mapping) or entity.get("type") != "natural":
            continue
        expr = entity.get("expr", entity.get("name"))
        if isinstance(expr, str) and nfc(expr) == natural:
            lifecycle = entity.get("lifecycle")
            return lifecycle if isinstance(lifecycle, Mapping) else {}
    return {}


@dataclass
class _Lifecycle:
    """What the contract's lifecycle gives the entry, and why each member it declares is not used."""

    activation: Optional[str] = None
    #: The column and the value (as text) an ending version holds.
    marker: Optional[Tuple[str, str]] = None
    notes: List[Tuple[str, str]] = field(default_factory=list)

    def decline(self, member: str, why: str) -> None:
        self.notes.append(
            (
                f"history lifecycle {member}",
                f"The contract declares lifecycle.{member}, which init does not write: {why}.",
            )
        )


def _spelled(value: Any) -> str:
    """A ``columnValue`` as the text of a level."""
    return str(value).lower() if isinstance(value, bool) else str(value)


def _column_and_value(raw: Any) -> Optional[Tuple[str, Any]]:
    column = raw.get("column") if isinstance(raw, Mapping) else None
    value = raw.get("value") if isinstance(raw, Mapping) else None
    if (
        not isinstance(column, str)
        or not column
        or isinstance(value, (Mapping, list))
        or value is None
    ):
        return None
    return nfc(column), value


def _activation(
    source: HistorySource,
    declared: Mapping[str, Any],
    kind: LogicalKind,
    held: FrozenSet[str],
    lifecycle: _Lifecycle,
) -> None:
    """Take ``lifecycle.activation`` when the expose has that column at the window's type."""
    raw = declared.get("activation")
    if raw is None:
        return
    column = nfc(raw) if isinstance(raw, str) and raw else None
    found = source.kinds.get(column) if column is not None else None
    if column is None or found is None:
        named = f"{column!r}" if column is not None else repr(raw)
        lifecycle.decline(
            "activation",
            f"{named} is not a column of the expose, so the column keeps its own draw "
            f"(columns: {sorted(source.kinds)})",
        )
    elif found is not kind:
        lifecycle.decline(
            "activation",
            f"{column!r} is a {found.value} column and the validity window is {kind.value}: "
            "it holds the first start, so it must be of the window's type",
        )
    elif column in held:
        lifecycle.decline(
            "activation",
            f"{column!r} is a key, a validity column, or set by a rule, a dependency or a copy, "
            "so the history cannot set it as well",
        )
    else:
        lifecycle.activation = column


def _levels_weighted(plan: ColumnPlan) -> Optional[Dict[str, Fraction]]:
    declared = plan.declaration.get("levels")
    if not isinstance(declared, Mapping) or not declared:
        return None
    try:
        return {str(name): Fraction(Decimal(str(weight))) for name, weight in declared.items()}
    except ArithmeticError:
        return None


def _end_marker(
    source: HistorySource,
    declared: Mapping[str, Any],
    held: FrozenSet[str],
    lifecycle: _Lifecycle,
) -> None:
    """Take ``lifecycle.endMarker`` when its column declares the value as a level an ending version can hold."""
    if "endMarker" not in declared:
        return
    read = _column_and_value(declared["endMarker"])
    if read is None:
        lifecycle.decline("endMarker", "it is not a column and a literal of the column's type")
        return
    column, value = read
    text = _spelled(value)
    plan = source.plans.get(column)
    why: Optional[str] = None
    if plan is None:
        why = f"{column!r} is not a column of the expose"
    elif (levels := _levels_weighted(plan)) is None:
        why = f"{column!r} declares no levels, so it is not categorical"
    elif text not in levels:
        why = f"{text!r} is not a level of {column!r} (levels: {sorted(levels)})"
    elif sum((w for name, w in levels.items() if name != text), Fraction(0)) <= 0:
        why = f"{text!r} is the only level of {column!r} with a weight"
    elif column in held:
        why = f"{column!r} is a key, a validity column, or set by a rule, a dependency or a copy"
    elif "nullFraction" in plan.declaration or "given" in plan.declaration:
        why = f"{column!r} declares a nullFraction or is drawn given another column"
    elif any(column in _given_names(other) for other in source.plans.values()):
        why = f"another column is drawn given {column!r}"
    if why is not None:
        lifecycle.decline("endMarker", f"{why}, so the end stays closeLast")
        return
    lifecycle.marker = (column, text)


def _read_lifecycle(
    source: HistorySource, natural: str, kind: LogicalKind, held: FrozenSet[str]
) -> _Lifecycle:
    """The members of the contract's lifecycle that the entry writes, and the reason for each it does not."""
    declared = declared_lifecycle(source.contract_expose, natural)
    lifecycle = _Lifecycle()
    _activation(source, declared, kind, held, lifecycle)
    _end_marker(source, declared, held, lifecycle)
    if "currentFlag" in declared:
        lifecycle.decline(
            "currentFlag",
            "no construct of the engine puts a value on the last version of an entity alone, so "
            "the column keeps its own draw and may hold the value on other versions",
        )
    for key in sorted(str(name) for name in declared if name not in _LIFECYCLE_KEYS):
        lifecycle.decline(key, "the engine does not read it")
    return lifecycle


def _until(source: HistorySource, kind: LogicalKind) -> str:
    _, high = _window_units(source, kind)
    return _spell(kind, high)


def _write_parent(
    source: HistorySource, entry: Dict[str, Any], entities: int, written: HistoryWritten
) -> None:
    """Add the ``parent`` the entities are laid out under to *entry*, when the history has one."""
    chosen = _pick_parent(source, written.exempt - source.set_by_rules, written)
    if chosen is None:
        return
    entry["parent"] = _parent(source, chosen, entities, written)
    written.exempt |= {chosen.column}
    written.parent_of = (chosen.name, chosen.tokens)
    plan = source.plans[chosen.column]
    declare_counted(plan, None, reason=_PARENT_REASON, how=_PARENT_HOW, sets=_PARENT_SETS)
    plan.passed = [
        (place, _PARENT_SETS if why.startswith("the counted layout sets") else why)
        for place, why in plan.passed
    ]


def _same_day_other(item: Any, start: str) -> Optional[str]:
    """The other column of a date pair of *start* that holds as the same day on nearly every row, or ``None``."""
    if not isinstance(item, Mapping) or start not in (item.get("start"), item.get("end")):
        return None
    other = item.get("end") if item.get("start") == start else item.get("start")
    order = item.get("order")
    group = _group(order)
    levels = order.get("levels") if isinstance(order, Mapping) else None
    total = _whole(group.get("k")) if group is not None else None
    if not isinstance(other, str) or group is None or not total or not isinstance(levels, list):
        return None
    if "same" not in levels:
        return None
    cells = count_cells(group, "levelCounts")
    position = levels.index("same")
    held = position < len(cells) and (cells[position].count or 0) * 10 >= total * 9
    return other if held else None


def _refuse_a_lost_tie(
    source: HistorySource, start: str, held: FrozenSet[str], lifecycle: _Lifecycle
) -> None:
    """Raise :class:`_Skip` when the release publishes a column on the day each version starts and the entry would not keep it.

    A date pair that holds as the same day on nearly every row, between the start and a column the
    history does not set, is a tie the history would cut: the column is drawn apart from the start.
    ``lifecycle.activation`` is how a contract keeps such a column with the first start.
    """
    pairs = source.table.get("datePairs")
    for index, item in enumerate(pairs if isinstance(pairs, list) else ()):
        other = _same_day_other(item, start)
        if other is not None and other not in held and other != lifecycle.activation:
            raise _LostTie(
                f"the release publishes {other!r} on the same day as {start!r}, and a history "
                "written from withheld lengths would draw them apart: declare "
                f"lifecycle.activation as {other!r} to keep them together",
                ("tables", source.expose_id, "datePairs", str(index), "order"),
            )


def _chain_of(
    history: Mapping[str, Any], kind: LogicalKind, declared: Mapping[str, Any], withheld: bool
) -> Tuple[str, str, bool]:
    """The chain level, the sentence that says why, and whether the release withholds how versions meet.

    The contract's chain wins, then the published share. Where the lengths are withheld and so is
    the share, the entry's default stands and the third value is set.
    """
    level = _declared_chain(declared, kind)
    if level is not None:
        return level, _chain_reason(history, kind, level), False
    try:
        level, share = _chain_level(history.get("chain"), kind)
    except _Skip:
        if not (withheld and _withheld_levels(history.get("chain"))):
            raise
        return _CHAINS[0], "", True
    reason = (
        f"{level} holds on {_percent(share)} of consecutive versions, "
        "which sets the entry's chain"
    )
    return level, reason, False


def _entities_of(
    history: Mapping[str, Any],
    base: Tuple[str, ...],
    source: HistorySource,
    natural: str,
    withheld: bool,
    written: HistoryWritten,
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """The entities and the ``versions`` shape; with the lengths withheld, the key's distinct count stands in."""
    try:
        entities, versions, used, unused, notes = _versions(history, base, source)
    except _Skip:
        entities_of_key = _entities_of_key(source, natural) if withheld else None
        if entities_of_key is None:
            raise
        written.derived[("entities",)] = _KEY_ENTITIES
        if entities_of_key < source.rows:
            written.notes.append(("history versions", _EVEN_VERSIONS))
        return entities_of_key, None
    written.used += used
    written.unused += unused
    written.notes += notes
    return entities, versions


def _first_starts(
    history: Mapping[str, Any],
    base: Tuple[str, ...],
    source: HistorySource,
    start: str,
    kind: LogicalKind,
    until: int,
    written: HistoryWritten,
    withheld: bool,
) -> Optional[Dict[str, Any]]:
    """The entry's ``start``: the first starts' histogram, else (lengths withheld) the start column's own."""
    try:
        found = _start_histogram(
            history.get("firstStart"), base + ("firstStart",), kind, until, source
        )
    except _Skip as skipped:
        written.unused.append((base + ("firstStart",), str(skipped)))
        column = _column_start(source, start, kind, until) if withheld else None
        if column is None:
            return None
        written.used.append((column[1], _START_FROM_COLUMN))
        written.derived[("start",)] = _START_FROM_COLUMN
        written.notes.append(("history start", _START_FROM_COLUMN))
        return column[0]
    written.used.append(
        (base + ("firstStart",), "the histogram of first starts, as the entry's start")
    )
    return found


def _length_of(
    history: Mapping[str, Any],
    base: Tuple[str, ...],
    source: HistorySource,
    window: Tuple[LogicalKind, str, str, int],
    held_and_lifecycle: Tuple[FrozenSet[str], "_Lifecycle"],
    written: HistoryWritten,
) -> Tuple[Dict[str, Any], Optional["_Derived"]]:
    """The entry's ``length``, read from the published histogram or derived where the release withholds it."""
    kind, start, end, until = window
    if not _withheld(history.get("versionLength")):
        length, raised = _length_histogram(
            history.get("versionLength"), base + ("versionLength",), source
        )
        written.used.append(
            (base + ("versionLength",), "the histogram of version lengths, as the entry's length")
        )
        if raised:
            written.notes.append(
                (
                    "history length",
                    "A bin of the published version lengths starts below one day; its lower edge is raised to one day, since a version lasts a day or more.",
                )
            )
        return length, None
    _refuse_a_lost_tie(source, start, *held_and_lifecycle)
    derived = _derive_length(source, kind, start, end, until)
    if derived is None:
        raise _Skip(
            "the histogram of version lengths is withheld, and the histogram of "
            f"{end!r} is not one the bundle draws from, so no length can be derived"
        )
    written.unused.append(
        (
            base + ("versionLength",),
            "the release withholds it, so the length is derived: " + derived.how,
        )
    )
    written.derived[("length",)] = derived.how
    written.notes.append(("history length", derived.how[0].upper() + derived.how[1:] + "."))
    if derived.fit is not None:
        written.fitted_end = (end, derived.fit.distance)
    return derived.length, derived


def _build(source: HistorySource, written: HistoryWritten) -> None:
    """Fill *written* from the table's history, or raise :class:`_Skip` with why none is written."""
    history = source.table["history"]
    base = ("tables", source.expose_id, "history")
    kind, natural, start, end = _validity(source)
    declared = declared_end(source.contract_expose, end)
    open_end = _open_end(source, end, declared)
    withheld = _withheld(history.get("versionLength"))
    level, chain_reason, chain_withheld = _chain_of(history, kind, declared, withheld)
    entities, versions = _entities_of(history, base, source, natural[0], withheld, written)
    entry: Dict[str, Any] = {"emit": source.expose_id, "entities": entities}
    if versions is not None:
        entry["versions"] = versions
    until = _window_units(source, kind)[1]
    held = _fixed(source, (start, end, *natural)) | source.set_by_rules
    lifecycle = _read_lifecycle(source, natural[0], kind, held)
    first = _first_starts(history, base, source, start, kind, until, written, withheld)
    if first is not None:
        entry["start"] = first
    entry["length"], derived = _length_of(
        history, base, source, (kind, start, end, until), (held, lifecycle), written
    )
    entry["until"] = _until(source, kind)
    entry["chain"] = level
    if open_end is not None:
        entry["openEnd"] = open_end
    if chain_withheld:
        written.unused.append((base + ("chain",), _CHAIN_WITHHELD))
        if entities < source.rows:
            written.notes.append(("history chain", _CHAIN_ASSUMED))
            written.derived[("chain",)] = _CHAIN_ASSUMED
    elif "chain" in history:
        written.used.append((base + ("chain",), chain_reason))
    if lifecycle.activation is not None:
        entry["activation"] = lifecycle.activation
    written.exempt = held | ({lifecycle.activation} if lifecycle.activation else set())
    _write_parent(source, entry, entities, written)
    _tracked_and_end(history, base, source, entry, written, lifecycle, derived)
    written.notes += lifecycle.notes
    written.entry = entry
    written.start_column = start


_KEY_ENTITIES = (
    "the entities: the release withholds the versions per entity, so they are the natural "
    "entity's published distinct count"
)
_EVEN_VERSIONS = (
    "The release withholds the number of versions per entity, so the versions are spread over the "
    "entities as evenly as whole counts allow."
)
_START_FROM_COLUMN = (
    "the histogram of first starts, which the release withholds: the start column's own "
    "histogram of starts is used, which is the first starts' where each entity has one version"
)
_CHAIN_WITHHELD = (
    "the release withholds how the versions meet, so the chain is the entry's default, nextStart"
)
_CHAIN_ASSUMED = (
    "The release withholds how consecutive versions meet, and the contract declares no chain: "
    "each version ends where the next one starts (nextStart)."
)


def _withheld_levels(node: Any) -> bool:
    """Whether a level histogram of the table's history is withheld: no group of it is published."""
    return not isinstance(node, Mapping) or _group(node) is None


def _fixed(source: HistorySource, window: Tuple[str, ...]) -> FrozenSet[str]:
    """The columns the history sets or a key holds: the natural entity, the keys and the window."""
    fixed = set(window)
    keys = source.keys
    if keys is None:
        return frozenset(fixed)
    for key in (*keys.keys, *keys.instant_keys):
        fixed.update(key)
    fixed.update(entity.column for entity in keys.entities)
    return frozenset(fixed)


def _derived_closed(
    source: HistorySource,
    entry: Mapping[str, Any],
    derived: Optional[_Derived],
    lifecycle: _Lifecycle,
) -> Optional[Tuple[Fraction, Fraction]]:
    """The share of entities that end when the release withholds the closed share of last versions.

    Without an end marker, the open versions of the end column's histogram are the entities that
    have not ended, so the ended share of the entities follows from them. Under an end marker no
    last version is closed: an entity ends with a marked version, so the rows the marker's column
    holds on its value are the ended entities, one row each, and the share is theirs over the
    entities, not over the rows.
    """
    entities, rows = int(entry["entities"]), source.rows
    if lifecycle.marker is not None:
        plan = source.plans.get(lifecycle.marker[0])
        levels = _levels_weighted(plan) if plan is not None else None
        total = sum(levels.values(), Fraction(0)) if levels else Fraction(0)
        if levels and lifecycle.marker[1] in levels and total > 0:
            # Whole entities: the weights are spelled to nine places.
            whole = round(levels[lifecycle.marker[1]] / total * rows)
            if whole > 0:
                return min(Fraction(whole, entities), Fraction(1)), Fraction(1)
        return None
    if derived is not None and derived.closed is not None:
        # open rows = rows * (1 - closed share of rows); each entity not ended keeps one open version.
        open_rows = rows * (1 - derived.closed)
        ended = 1 - open_rows / entities
        if ended <= 0:
            return None
        return min(ended, Fraction(1)), Fraction(1)
    return None


_UNREAD_MARKER = (
    "no entity ends by the marker in the profile (its first values and moves, and its column's "
    "histogram, put none on it), so the entry writes no end"
)
_MARKER_CELLS = (
    "the entities that end under the contract's end marker: those that begin on the marker plus "
    "every change into it, which the tracked column's first values and moves publish, over the "
    "entities"
)
_MARKER_ROWS = (
    "the entities that end under the contract's end marker: each holds it on one version, so the "
    "rows its column holds on the marker, over the entities (the column's first values and moves "
    "do not publish every change into it)"
)
_MARKER_LAST_VERSIONS = (
    "the contract's end marker ends an entity with a marked version and closes no last version, so "
    "the closed share of last versions gives no end share"
)


def _marker_first(first: Any, value: str) -> Optional[int]:
    """The entities whose first version holds *value*, when that cell is published."""
    group = _group(first)
    levels = first.get("levels") if isinstance(first, Mapping) else None
    if group is None or not isinstance(levels, list) or value not in levels:
        return None
    return count_cells(group, "levelCounts")[levels.index(value)].count


def _marker_moves(moves: Any, value: str) -> Optional[int]:
    """The changes into *value* from every other level, when every one of those cells is published."""
    targets = moves.get("levels") if isinstance(moves, Mapping) else None
    groups = moves.get("groups") if isinstance(moves, Mapping) else None
    if not isinstance(targets, list) or not isinstance(groups, list) or value not in targets:
        return None
    by_level = {
        str(item.get("group")): item
        for item in groups
        if isinstance(item, Mapping) and item.get("status") == _FREE
    }
    total = 0
    for level in targets:
        if str(level) == value:
            continue
        counts = count_cells(by_level.get(str(level), {}), "levelCounts")
        if len(counts) != len(targets) or counts[targets.index(value)].count is None:
            return None
        total += counts[targets.index(value)].count or 0
    return total


def _marker_cells(
    history: Mapping[str, Any], base: Tuple[str, ...], marker: Tuple[str, str]
) -> Optional[Tuple[int, List[Tuple[str, ...]]]]:
    """The entities that end by the marker, from the tracked column's first values and moves.

    An entity that ends holds the marker on one version: its first, or the one it moves to, since
    no change leaves the marker. The count is the first values on the marker plus the changes into
    it from every other level, and is read only when each of those cells is published.
    """
    column, value = marker
    columns = history.get("columns")
    found = columns.get(column) if isinstance(columns, Mapping) else None
    if not isinstance(found, Mapping):
        return None
    first = _marker_first(found.get("initial"), value)
    moved = _marker_moves(found.get("transitions"), value)
    if first is None or moved is None:
        return None
    where = base + ("columns", column)
    return first + moved, [where + ("initial",), where + ("transitions",)]


def _level_statistic(source: HistorySource, column: str) -> Optional[Tuple[str, ...]]:
    """The pointer of a column's own level histogram, or ``None``."""
    for node, pointer in _column_statistics(source, column):
        if node.get("definition") == LEVEL_HISTOGRAM.identifier and "groupBy" not in node:
            return pointer
    return None


def _marker_share(
    history: Mapping[str, Any],
    base: Tuple[str, ...],
    source: HistorySource,
    entry: Mapping[str, Any],
    lifecycle: _Lifecycle,
) -> Optional[Tuple[Fraction, str, List[_Note]]]:
    """The share of entities that end by the contract's end marker, how it was read, and what gave it.

    The tracked column's published first values and moves give it where every cell is published;
    else the column's histogram does (:func:`_derived_closed`); else it is ``None``. Either way the
    entry's share is assumed from them in the register, and the notes list what was read.
    """
    assert lifecycle.marker is not None
    cells = _marker_cells(history, base, lifecycle.marker)
    if cells is not None and cells[0] > 0:
        share = min(Fraction(cells[0], int(entry["entities"])), Fraction(1))
        why = "the entities that end by the contract's end marker, as the entry's end share"
        return share, _MARKER_CELLS, [(pointer, why) for pointer in cells[1]]
    closed = _derived_closed(source, entry, None, lifecycle)
    if closed is None:
        return None
    where = _level_statistic(source, lifecycle.marker[0])
    why = "the rows on the contract's end marker, as the entry's end share"
    return closed[0], _MARKER_ROWS, [(where, why)] if where is not None else []


def _tracked_and_end(
    history: Mapping[str, Any],
    base: Tuple[str, ...],
    source: HistorySource,
    entry: Dict[str, Any],
    written: HistoryWritten,
    lifecycle: _Lifecycle,
    derived: Optional[_Derived] = None,
) -> None:
    pointer = base + ("lastVersion",)
    marker = lifecycle.marker
    found = None
    if marker is not None:
        found = _marker_share(history, base, source, entry, lifecycle)
        if found is None:
            lifecycle.decline("endMarker", _UNREAD_MARKER)
            marker = None
        closed = None
    else:
        closed = _closed_share(history.get("lastVersion"), pointer, source)
    withheld_share = (
        lifecycle.marker is None
        and closed is None
        and derived is not None
        and _withheld_levels(history.get("lastVersion"))
    )
    if withheld_share:
        closed = _derived_closed(source, entry, derived, lifecycle)
    tracking = _track(history, base, source, written.exempt - source.set_by_rules, marker)
    written.used += tracking.used
    written.unused += tracking.unused
    names = [str(item["column"]) for item in tracking.columns]
    if tracking.columns:
        entry["tracked"] = tracking.columns
    _reasons_entry(history, base, source, entry, written, names)
    if lifecycle.marker is not None:
        last = pointer if "lastVersion" in history else None
        _marker_end(entry, written, lifecycle.marker, found, last)
        return
    if withheld_share:
        _withheld_end(source, entry, written, pointer, closed, derived)
        return
    end = _end(history.get("lastVersion"), pointer, source)
    if end is not None:
        entry["end"] = end
        written.used.append(
            (pointer, "the closed share of last versions, as the entry's end share under closeLast")
        )
    elif "lastVersion" in history:
        written.used.append(
            (pointer, "no entity ends: no last version is closed, so the entry writes no end")
        )


def _marker_end(
    entry: Dict[str, Any],
    written: HistoryWritten,
    marker: Tuple[str, str],
    found: Optional[Tuple[Fraction, str, List[_Note]]],
    last_versions: Optional[Tuple[str, ...]],
) -> None:
    """The entry's ``end`` under the contract's end marker, and what the ledger says of each member."""
    if found is not None:
        share, how, gave = found
        entry["end"] = _final_end(marker, share)
        written.derived[("end", "share")] = how
        listed = {note[0] for note in written.used}
        written.used += [note for note in gave if note[0] not in listed]
    if last_versions is not None:
        written.unused.append((last_versions, _MARKER_LAST_VERSIONS))


def _withheld_end(
    source: HistorySource,
    entry: Dict[str, Any],
    written: HistoryWritten,
    pointer: Tuple[str, ...],
    closed: Optional[Tuple[Fraction, Fraction]],
    derived: Optional[_Derived],
) -> None:
    """The entry's ``end`` when the release withholds the last versions' closed share, from what else it publishes."""
    if closed is None:
        written.unused.append(
            (
                pointer,
                "the release withholds it, and nothing else it publishes says how many entities end",
            )
        )
        return
    how = (
        "the closed share of last versions, which the release withholds: the entities that have "
        "not ended are the open versions of the histogram of "
        f"{source.table['history'].get('end')!r}, so the rest ended"
    )
    written.derived[("end", "share")] = how
    written.unused.append((pointer, how))
    entry["end"] = {
        "mode": "closeLast",
        "share": share_texts([closed[0], closed[1] - closed[0]])[0],
    }


def _reasons_entry(
    history: Mapping[str, Any],
    base: Tuple[str, ...],
    source: HistorySource,
    entry: Dict[str, Any],
    written: HistoryWritten,
    names: Sequence[str],
) -> None:
    pointer = base + ("reasons",)
    if "reasons" not in history:
        return
    if len(names) < 2:
        why = (
            "no column is tracked, so no change has a reason to share"
            if not names
            else "one tracked column makes every change, so its share is not written"
        )
        written.unused.append((pointer, why))
        return
    reasons = _reasons(history["reasons"], pointer, names, source)
    if reasons is None:
        written.unused.append((pointer, "it gives no weight to a tracked column, or is withheld"))
        return
    entry["reasons"] = reasons
    written.used.append(
        (
            pointer,
            "the share of changes each tracked column makes, without the changes that move several",
        )
    )


def write_history(source: HistorySource) -> HistoryWritten:
    """The ``history`` entry of one expose, and what was used and unused of its table's history.

    A table that publishes no ``history`` returns an empty result. When no entry is written, every
    published member is listed as unused with the one reason that stopped it.
    """
    written = HistoryWritten()
    history = source.table.get("history")
    if not isinstance(history, Mapping):
        return written
    base = ("tables", source.expose_id, "history")
    try:
        _build(source, written)
    except _Skip as skipped:
        read = (
            [(skipped.pair, _TIE_READ)] if isinstance(skipped, _LostTie) else []
        )  # a pair the decision read is used
        return HistoryWritten(
            used=read, unused=[(pointer, str(skipped)) for pointer in _pointers(base, history)]
        )
    return written


def decline_history(source: HistorySource, reason: str) -> HistoryWritten:
    """No entry for *source*'s table: each statistic of its history is unused, *reason* saying why."""
    history = source.table.get("history")
    if not isinstance(history, Mapping):
        return HistoryWritten()
    base = ("tables", source.expose_id, "history")
    why = f"the bundle written with this history is refused, so none is written: {reason}"
    return HistoryWritten(unused=[(pointer, why) for pointer in _pointers(base, history)])


# ---------------------------------------------------------------------------
# Date columns
# ---------------------------------------------------------------------------

_RANGE_REASON = (
    "The profile publishes no range for the column, and a history's emit declares every date "
    "range so that its rows do not change with the window: it spans the run's window."
)


def declare_date_ranges(
    plans: Mapping[str, ColumnPlan],
    kinds: Mapping[str, LogicalKind],
    window: Tuple[str, str],
    exempt: FrozenSet[str],
) -> List[str]:
    """Give each DATE or TIMESTAMP column with no range and no bins the window as its range.

    A history's emit is refused such a column, since it would be drawn inside the window and change
    with it. The columns in *exempt* (the history's own, the keys, and the columns a rule sets) are
    left alone. Returns the columns it gave a range, in column order.
    """
    changed: List[str] = []
    for name, plan in plans.items():
        kind = kinds.get(name)
        declared = plan.declaration
        if kind not in _UNITS or name in exempt or "range" in declared or "bins" in declared:
            continue
        low, high = (_spell(kind, _unit(kind, parse_rfc3339_ms(text))) for text in window)
        declared["bins"] = [low, high]
        declared["binWeights"] = ["1"]
        declared["range"] = {"min": low, "max": high}
        plan.labels.append(Label(("range",), assumed(_RANGE_REASON)))
        plan.assumptions.append(_RANGE_REASON)
        plan.choices.append(_RANGE_REASON)
        changed.append(name)
    return changed
