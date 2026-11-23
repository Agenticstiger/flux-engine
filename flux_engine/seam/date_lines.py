# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The facts a Simulation's lifetimes and ``within`` entries make, named and planned (ADR 0024 section 8).

``fluxsim verify`` scores each declaration as a line of its emit's ``rules``:

* ``lifetime:<start>.<end>`` for each lifetime that names an end;
* ``within:<relationship>.<column>`` for each ``within`` entry on a date, with the relationship
  written as the entry writes it (dots between the hops of a chain), and
  ``within:<relationship>.lifetime`` for a period;
* for each chain, the outer level: when an emit's entry on column ``c`` goes through ``r1`` to an
  expose whose lifetime's start is itself set by a ``within`` entry through ``r2`` (a date entry on
  that start, or a period entry), the line ``within:<r1>.<r2>.<c>``, which scores ``c`` against the
  lifetime of the row ``r1.r2`` reaches; and so on along the chain.

Each line is named ``<exposeId>.<ruleId>`` as a fact is. A line with no ``target`` is enforced. An
implied outer level is checked against the sum of its levels' targets, which is zero when every
level is enforced, unless an entry of the emit states that outer level itself: its own line and
target then hold and no second line is made.

This module plans the lines from the declarations alone, with *step* telling it which expose a
relationship reaches. What the lines find in a run's rows is :mod:`flux_engine.concordance.date_facts`.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.seam.dates import MOST_HOPS, LifetimeEntry, WithinEntry
from flux_engine.seam.keys import declared_exposes

__all__ = [
    "DateLine",
    "LIFETIME_RULE_PREFIX",
    "WITHIN_RULE_PREFIX",
    "contract_step",
    "date_lines",
    "date_rule_ids",
]

LIFETIME_RULE_PREFIX = "lifetime:"
WITHIN_RULE_PREFIX = "within:"

#: ``(exposeId, relationship name)`` to the exposeId the relationship reaches, or ``None``.
Step = Callable[[str, str], Optional[str]]

_MOST_HOPS = MOST_HOPS
_ZERO = Fraction(0)

#: What a chain's planning remembers: the emit, column, date, kind, own columns and path.
_State = Tuple[str, Optional[str], Optional[str], bool, Tuple[str, Optional[str]], Tuple[str, ...]]


@dataclass(frozen=True)
class DateLine:
    """One fact over dates: what it reads and the share of violating rows it allows.

    ``kind`` is ``lifetime`` (the end is not before the start), ``date`` (a column within the
    lifetime of the row the path reaches) or ``period`` (the emit's own lifetime within it).
    ``target`` is the percent of covered rows that may violate it, or ``None`` when the line is
    enforced. ``reads`` is every ``(expose, column)`` the line reads at any level of its chain, and
    ``tables`` every expose whose rows it reads, the emit's own first.
    """

    emit: str
    rule_id: str
    kind: str
    column: Optional[str]
    path: Tuple[str, ...]
    own_start: str
    own_end: Optional[str]
    parent: str
    parent_start: str
    parent_end: Optional[str]
    target: Optional[Fraction]
    reads: Tuple[Tuple[str, str], ...]
    tables: Tuple[str, ...]
    tolerance: Optional[Fraction] = None

    @property
    def name(self) -> str:
        """The fact's name, ``<exposeId>.<ruleId>``."""
        return f"{self.emit}.{self.rule_id}"


def contract_step(contract: Mapping[str, object]) -> Step:
    """The *step* of a contract: the exposes' ``semantics.relationships`` by name, NFC as declared."""
    exposes = declared_exposes(contract)

    def step(expose_id: str, relationship: str) -> Optional[str]:
        entry = exposes.get(nfc(expose_id))
        semantics = entry.get("semantics") if entry is not None else None
        raw = semantics.get("relationships") if isinstance(semantics, Mapping) else None
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return None
        for item in raw:
            if (
                isinstance(item, Mapping)
                and isinstance(item.get("name"), str)
                and nfc(item["name"]) == relationship
                and isinstance(item.get("to"), str)
            ):
                return nfc(item["to"])
        return None

    return step


def _walk(step: Step, start: str, path: Sequence[str]) -> Optional[Tuple[str, ...]]:
    """The exposes *path* reaches from *start*, one for each hop, or ``None`` when a hop reaches none."""
    found: List[str] = []
    here = start
    for name in path:
        nxt = step(here, name)
        if nxt is None:
            return None
        found.append(nxt)
        here = nxt
    return tuple(found)


def _percent(text: Optional[str]) -> Optional[Fraction]:
    return None if text is None else Fraction(text)


def _line_name(path: Sequence[str], column: str) -> str:
    return f"{WITHIN_RULE_PREFIX}{'.'.join(path)}.{column}"


def _sum(levels: Sequence[Optional[Fraction]]) -> Optional[Fraction]:
    """The summed target of a chain's levels, or ``None`` (enforced) when it is zero."""
    total = sum((level for level in levels if level is not None), _ZERO)
    return total if total != _ZERO else None


def _lifetime_line(life: LifetimeEntry, end: str) -> DateLine:
    """The line of an emit's own lifetime: its end is not before its start."""
    return DateLine(
        emit=life.emit,
        rule_id=f"{LIFETIME_RULE_PREFIX}{life.start}.{end}",
        kind="lifetime",
        column=None,
        path=(),
        own_start=life.start,
        own_end=end,
        parent=life.emit,
        parent_start=life.start,
        parent_end=end,
        target=_percent(life.target),
        reads=((life.emit, life.start), (life.emit, end)),
        tables=(life.emit,),
    )


def _entry_line(
    entry: WithinEntry,
    mine: Optional[LifetimeEntry],
    hops: Tuple[str, ...],
    parent: LifetimeEntry,
) -> Tuple[DateLine, List[Tuple[str, str]]]:
    """The line of one ``within`` entry whose parent holds a lifetime, and the columns it reads."""
    own = mine if entry.lifetime else None
    own_start = own.start if own is not None else str(entry.date)
    own_end = own.end if own is not None else None
    reads = [(entry.emit, own_start)]
    if own_end is not None:
        reads.append((entry.emit, own_end))
    reads.append((hops[-1], parent.start))
    if parent.end is not None:
        reads.append((hops[-1], parent.end))
    line = DateLine(
        emit=entry.emit,
        rule_id=_line_name(entry.relationship, entry.column),
        kind="period" if entry.lifetime else "date",
        column=None if entry.lifetime else entry.date,
        path=entry.relationship,
        own_start=own_start,
        own_end=own_end,
        parent=hops[-1],
        parent_start=parent.start,
        parent_end=parent.end,
        target=_percent(entry.target),
        reads=tuple(reads),
        tables=(entry.emit, *hops),
    )
    return line, reads


def date_lines(
    lifetimes: Sequence[LifetimeEntry], within: Sequence[WithinEntry], step: Step
) -> Tuple[DateLine, ...]:
    """Every line the declarations make, in declared order, each emit's lifetime line first.

    An entry whose relationships *step* cannot follow makes no line (the plan refused it already).
    """
    held: Dict[str, LifetimeEntry] = {entry.emit: entry for entry in lifetimes}
    by_emit: Dict[str, List[WithinEntry]] = {}
    for entry in within:
        by_emit.setdefault(entry.emit, []).append(entry)
    stated: Dict[Tuple[str, str, Tuple[str, ...]], Optional[Fraction]] = {}
    for entry in within:
        # The first entry of a level states its target.
        stated.setdefault((entry.emit, entry.column, entry.relationship), _percent(entry.target))
    lines: List[DateLine] = []
    seen: Set[Tuple[str, str]] = set()
    visited: Dict[_State, int] = {}

    def add(line: DateLine) -> None:
        if (line.emit, line.rule_id) not in seen:
            seen.add((line.emit, line.rule_id))
            lines.append(line)

    for life in lifetimes:
        if life.end is not None:
            add(_lifetime_line(life, life.end))
    for entry in within:
        hops = _walk(step, entry.emit, entry.relationship)
        mine = held.get(entry.emit)
        parent = held.get(hops[-1]) if hops else None
        if hops is None or parent is None or (entry.lifetime and mine is None):
            continue
        line, reads = _entry_line(entry, mine, hops, parent)
        add(line)
        own_start, own_end = line.own_start, line.own_end
        _extend(
            add=add,
            step=step,
            held=held,
            by_emit=by_emit,
            stated=stated,
            base=entry,
            mine=(own_start, own_end),
            path=entry.relationship,
            hops=hops,
            levels=[_percent(entry.target)],
            reads=reads,
            depth=0,
            visited=visited,
        )
    return tuple(lines)


def _extend(
    *,
    add: Callable[[DateLine], None],
    step: Step,
    held: Mapping[str, LifetimeEntry],
    by_emit: Mapping[str, Sequence[WithinEntry]],
    stated: Mapping[Tuple[str, str, Tuple[str, ...]], Optional[Fraction]],
    base: WithinEntry,
    mine: Tuple[str, Optional[str]],
    path: Tuple[str, ...],
    hops: Tuple[str, ...],
    levels: List[Optional[Fraction]],
    reads: List[Tuple[str, str]],
    depth: int,
    visited: Dict[_State, int],
) -> None:
    """Add the implied outer levels of *base* beyond *path*, each checked against its levels' targets.

    The expose *path* reaches has a period set by a ``within`` entry of its own (``lifetime:
    true``); the line through that entry's relationship follows, and the chain goes on from there,
    through period ties only. A tie on the expose's start alone keeps the row after the outer
    start and not before its end, so it makes no implied line (ADR 0024 section 8). An outer level some entry of the emit states itself
    makes no line: that entry's own line and target hold, and the chain goes on from it.
    """
    here = hops[-1]
    life = held.get(here)
    if life is None or depth >= _MOST_HOPS:
        return
    # The names of the lines beyond a state depend on the emit, the column, the kind and the path
    # alone, and the lines further out only on how many levels are left (the levels and reads only
    # fill a line's content, and the first line of a name is the one kept). A state reached again,
    # by another composition of the same chain or from another entry of the same emit and column,
    # at no smaller depth than before adds nothing new, so each is planned once per depth it is
    # first reached at: the work follows the lines the chain makes, not the ways of composing it.
    state = (base.emit, base.column, base.date, base.lifetime, mine, path)
    # Each level of a chain reaches an expose the chain has not passed, so no chain is deeper than
    # the lifetimes held: when those are within the bound the depth cannot cut a chain short and a
    # state needs planning once, whatever its depth.
    left = depth if len(held) > _MOST_HOPS else 0
    if visited.get(state, _MOST_HOPS + 1) <= left:
        return
    visited[state] = left
    for inner in by_emit.get(here, ()):
        if not inner.lifetime:
            continue
        more = _walk(step, here, inner.relationship)
        if more is None or _cycles(base.emit, hops, more):
            continue  # a cycle of exposes sets no further level
        outer = (*path, *inner.relationship)
        outer_life = held.get(more[-1])
        if outer_life is None:
            continue
        outer_hops = (*hops, *more)
        outer_reads = _reads_through(reads, more[-1], outer_life)
        stated_key = (base.emit, base.column, outer)
        if stated_key in stated:
            outer_levels = [stated[stated_key]]
        else:
            outer_levels = [*levels, _percent(inner.target)]
            add(
                _implied_line(
                    base, mine, outer, more[-1], outer_life, outer_levels, outer_reads, outer_hops
                )
            )
        _extend(
            add=add,
            step=step,
            held=held,
            by_emit=by_emit,
            stated=stated,
            base=base,
            mine=mine,
            path=outer,
            hops=outer_hops,
            levels=outer_levels,
            reads=outer_reads,
            depth=depth + 1,
            visited=visited,
        )


def _reads_through(
    reads: Sequence[Tuple[str, str]], expose: str, life: LifetimeEntry
) -> List[Tuple[str, str]]:
    """*reads* and the lifetime columns *life* holds on *expose*."""
    found = [*reads, (expose, life.start)]
    if life.end is not None:
        found.append((expose, life.end))
    return found


def _cycles(emit: str, hops: Sequence[str], more: Sequence[str]) -> bool:
    """Whether the exposes *more* reach the emit itself or one *hops* already passed."""
    return emit in more or any(hop in hops for hop in more)


def _implied_line(
    base: WithinEntry,
    mine: Tuple[str, Optional[str]],
    outer: Tuple[str, ...],
    parent: str,
    outer_life: LifetimeEntry,
    levels: Sequence[Optional[Fraction]],
    reads: Sequence[Tuple[str, str]],
    hops: Tuple[str, ...],
) -> DateLine:
    """The line of an outer level no entry states: the summed targets of its *levels*."""
    return DateLine(
        emit=base.emit,
        rule_id=_line_name(outer, base.column),
        kind="period" if base.lifetime else "date",
        column=None if base.lifetime else base.date,
        path=outer,
        own_start=mine[0],
        own_end=mine[1],
        parent=parent,
        parent_start=outer_life.start,
        parent_end=outer_life.end,
        target=_sum(levels),
        reads=tuple(reads),
        tables=(base.emit, *hops),
    )


def date_rule_ids(
    lifetimes: Sequence[LifetimeEntry],
    within: Sequence[WithinEntry],
    step: Step,
    emit: str,
) -> Tuple[str, ...]:
    """The rule ids the declarations make for *emit*, in declared order."""
    wanted = nfc(emit)
    return tuple(
        line.rule_id for line in date_lines(lifetimes, within, step) if line.emit == wanted
    )
