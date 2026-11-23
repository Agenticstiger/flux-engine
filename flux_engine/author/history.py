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
  ``initial`` and ``transitions``; the others are carried;
* ``reasons`` is the histogram of the changes by the column they move, without ``several``;
* ``end`` is ``closeLast`` with the closed share of ``lastVersion``, when that share is above zero.
  A profile does not say which value marks an end, so ``finalVersion`` is never written;
* ``parent`` is never written: a profile publishes children per parent over rows, which are versions
  here, and an entity names one column, so a relationship on the natural entity's column would make
  the natural key a foreign key, which the generator refuses.

Every published member that is not used is returned with the reason it is not, as ADR 0014 requires.
:func:`declare_date_ranges` gives each date column of the emit that declares no range the window as
its range, since a history's emit is refused a date that would default to the window.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.author.basis import Label, assumed
from flux_engine.author.children import count_children
from flux_engine.author.columns import ColumnPlan, cell_weights, count_cells, share_texts
from flux_engine.core.clock import format_rfc3339_ms, parse_rfc3339_ms
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.keys import ExposeKeys, window_ends
from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM, MEAN

__all__ = [
    "CHAIN_HOLDS_AT_LEAST",
    "HistorySource",
    "HistoryWritten",
    "declare_date_ranges",
    "decline_history",
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
class HistorySource:
    """What :func:`write_history` reads of one expose: the profile table, the plans and the contract.

    ``rows`` is the expose's versions. ``plans`` holds each column's plan, by name, and ``kinds``
    its type. ``set_by_rules`` is the columns a rule, a dependency or a copy sets, which a history
    never tracks. ``window`` is ``timeBounds`` as RFC 3339 text.
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


class _Skip(Exception):
    """The history is not written, and every published member is unused for this reason."""


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


def _end(node: Any, pointer: Tuple[str, ...], source: HistorySource) -> Optional[Dict[str, Any]]:
    """The ``end`` of the entry: ``closeLast`` with the closed share, or ``None`` when none ends."""
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
    return {"mode": "closeLast", "share": share_texts([closed, total - closed])[0]}


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
) -> Optional[Dict[str, Fraction]]:
    """The weight of each level of a level histogram's group; the level *own* is known to be zero."""
    levels = node.get("levels")
    cells = count_cells(group, "levelCounts")
    if not isinstance(levels, list) or len(cells) != len(levels):
        return None
    if own in levels:
        position = levels.index(own)
        cells[position] = replace(cells[position], count=0)
    weights, _ = cell_weights(
        source.minimum_cell, cells, pointer, "levelCounts", LEVEL_HISTOGRAM.identifier,
        total=_whole(group.get("k")),
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
    node: Mapping[str, Any], pointer: Tuple[str, ...], source: HistorySource
) -> Dict[str, Dict[str, str]]:
    """Each level's weights of the levels it moves to, for each group that moves."""
    rows: Dict[str, Dict[str, str]] = {}
    for group in node.get("groups") or ():
        if not isinstance(group, Mapping) or group.get("status") != _FREE:
            continue
        level = str(group.get("group"))
        weights = _level_weights(node, group, pointer, source, own=level)
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
    node: Any, pointer: Tuple[str, ...], source: HistorySource
) -> Optional[Dict[str, Any]]:
    group = _group(node)
    weights = _level_weights(node, group, pointer, source) if group is not None else None
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
    if plan is None or _levels_of(plan) is None:
        return "the emit declares no levels for the column, so it is carried"
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
) -> Dict[str, Any]:
    """The ``tracked`` item of one column, or :class:`_Skip` with why it is carried."""
    moves = entry.get("transitions")
    if not isinstance(moves, Mapping) or not _moves(moves):
        raise _Skip("no published change moves the column, so it is carried")
    rows = _rows(moves, base + ("transitions",), source)
    initial = _initial(entry.get("initial"), base + ("initial",), source)
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


def _track(
    history: Mapping[str, Any], base: Tuple[str, ...], source: HistorySource, fixed: FrozenSet[str]
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
        reason = _refusal_of(name, source.plans.get(name), source, fixed, given)
        try:
            if reason is not None:
                raise _Skip(reason)
            plan = source.plans[name]
            declared = _levels_of(plan) or ()
            item = _track_one(name, candidates[name], where, source, declared)
        except _Skip as skipped:
            result.unused += [(where + (key,), str(skipped)) for key in ("initial", "transitions")]
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


def _required_end(source: HistorySource, end: str) -> None:
    from flux_engine.seam.contract import compile_expose

    required = {
        column.name for column in compile_expose(source.contract_expose).columns if column.required
    }
    if end in required:
        raise _Skip(
            f"the end column {end!r} is required, and an open version needs an openEnd, "
            "which init does not write"
        )


def _until(source: HistorySource, kind: LogicalKind) -> str:
    _, high = _window_units(source, kind)
    return _spell(kind, high)


def _build(source: HistorySource, written: HistoryWritten) -> None:
    """Fill *written* from the table's history, or raise :class:`_Skip` with why none is written."""
    history = source.table["history"]
    base = ("tables", source.expose_id, "history")
    kind, natural, start, end = _validity(source)
    _required_end(source, end)
    level, share = _chain_level(history.get("chain"), kind)
    entities, versions, used, unused, notes = _versions(history, base, source)
    written.used += used
    written.unused += unused
    written.notes += notes
    entry: Dict[str, Any] = {"emit": source.expose_id, "entities": entities}
    if versions is not None:
        entry["versions"] = versions
    until = _window_units(source, kind)[1]
    try:
        entry["start"] = _start_histogram(
            history.get("firstStart"), base + ("firstStart",), kind, until, source
        )
        written.used.append(
            (base + ("firstStart",), "the histogram of first starts, as the entry's start")
        )
    except _Skip as skipped:
        written.unused.append((base + ("firstStart",), str(skipped)))
    entry["length"], raised = _length_histogram(
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
    entry["until"] = _until(source, kind)
    entry["chain"] = level
    written.used.append(
        (
            base + ("chain",),
            f"{level} holds on {_percent(share)} of consecutive versions, which sets the entry's chain",
        )
    )
    written.exempt = _fixed(source, (start, end, *natural)) | source.set_by_rules
    _tracked_and_end(history, base, source, entry, written)
    written.entry = entry
    written.start_column = start


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


def _tracked_and_end(
    history: Mapping[str, Any],
    base: Tuple[str, ...],
    source: HistorySource,
    entry: Dict[str, Any],
    written: HistoryWritten,
) -> None:
    tracking = _track(history, base, source, written.exempt - source.set_by_rules)
    written.used += tracking.used
    written.unused += tracking.unused
    names = [str(item["column"]) for item in tracking.columns]
    if tracking.columns:
        entry["tracked"] = tracking.columns
    _reasons_entry(history, base, source, entry, written, names)
    end = _end(history.get("lastVersion"), base + ("lastVersion",), source)
    pointer = base + ("lastVersion",)
    if end is not None:
        entry["end"] = end
        written.used.append(
            (pointer, "the closed share of last versions, as the entry's end share under closeLast")
        )
    elif "lastVersion" in history:
        written.used.append(
            (pointer, "no entity ends: no last version is closed, so the entry writes no end")
        )


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
        return HistoryWritten(
            unused=[(pointer, str(skipped)) for pointer in _pointers(base, history)]
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
