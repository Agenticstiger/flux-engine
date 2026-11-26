# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The columns something other than their own statistics sets, and a contract range on them (ADR 0024).

A contract's date range cuts what a column is drawn from (:mod:`flux_engine.generator.declared_range`).
A column a history's layout, an events or periods entry, a snapshot's source, a rule or a relationship
sets is not drawn from its statistics, so the cut leaves it alone and ``plan`` says so once;
``verify`` still counts its rows outside the range. :func:`uncut_columns` names the ones the plan
itself would otherwise cut; :func:`range_setter_notes` is the sentence for each such column that
declares a range.

A history's validity columns, the projected columns that copy a version's start or end, and a periods
entry's period column are held to their ranges (ADR 0024, the amendment of 2026-11-25):
:func:`held_columns` names them, :func:`validity_range` is the instants a history's versions keep to,
and such a column is noted no more.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, FrozenSet, NamedTuple, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.generator.declared_range import range_phrase, range_text
from flux_engine.generator.projections import projected_cell_names, projection_of
from flux_engine.seam.history import history_entries_of
from flux_engine.seam.keys import declared_exposes, window_ends
from flux_engine.seam.projections import EVENTS_KEY, PERIODS_KEY, SNAPSHOTS_KEY
from flux_engine.seam.range_rules import instant_ms

if TYPE_CHECKING:
    from flux_engine.seam.resolve import ResolvedEmit

__all__ = ["SetBy", "held_columns", "range_setter_notes", "uncut_columns", "validity_range"]


def _column_names(resolved: "ResolvedEmit") -> Tuple[str, ...]:
    return tuple(column.name for column in resolved.compile_planned().columns)


def _validity_columns(resolved: "ResolvedEmit") -> Dict[str, str]:
    """The ``start`` and ``end`` columns the contract's semantics name for *resolved*'s window."""
    semantics = (
        declared_exposes(resolved.contract_document).get(resolved.expose_id, {}).get("semantics")
    )
    return {role: nfc(column) for role, column in window_ends(semantics).items()}


def _source_of(of: str, emits: Sequence["ResolvedEmit"]) -> Optional["ResolvedEmit"]:
    return next((other for other in emits if nfc(other.expose_id) == nfc(of)), None)


def _copies(resolved: "ResolvedEmit", emits: Sequence["ResolvedEmit"]) -> FrozenSet[str]:
    """The columns of a projected *resolved* that hold a version's start or end instant.

    A column named as the history's validity start or end column, an events entry's ``at`` and,
    with no ``lag``, its ``date``: each holds a start or a closed end of the versions it is about.
    """
    held = projection_of(resolved.extension, resolved.expose_id)
    source = None if held is None else _source_of(held[1], emits)
    if held is None or source is None:
        return frozenset()
    key, _of, entry = held
    validity = set(_validity_columns(source).values())
    found = {nfc(name) for name in _column_names(resolved) if nfc(name) in validity}
    if key == EVENTS_KEY:
        found.update(
            nfc(name) for name in (entry.at, None if entry.lag else entry.date) if name is not None
        )
    return frozenset(found)


def held_columns(resolved: "ResolvedEmit", emits: Sequence["ResolvedEmit"]) -> FrozenSet[str]:
    """The columns of *resolved* whose setter keeps to the contract's range (ADR 0024, amendment of 2026-11-25).

    A history's validity columns, which every version's start and closed end keeps to, but an end
    column whose ``openEnd`` lies outside its range; a projected column that copies a version's
    start or end (:func:`_copies`); and a periods entry's period column, whose periods are cut to it.
    """
    found = set(_copies(resolved, emits))
    entries = history_entries_of(resolved.extension, resolved.expose_id)
    if entries:
        validity = _validity_columns(resolved)
        found.update(validity.values())
        if entries[0].open_end is not None and "end" in validity:
            if _outside(resolved, validity["end"], entries[0].open_end):
                found.discard(validity["end"])
    held = projection_of(resolved.extension, resolved.expose_id)
    if held is not None and held[0] == PERIODS_KEY:
        found.add(nfc(held[2].column))
    return frozenset(found)


def _declared(resolved: "ResolvedEmit") -> Dict[str, Tuple[Optional[int], Optional[int]]]:
    """Each column of *resolved* that declares a date range: its ends in milliseconds."""
    return {
        nfc(column.name): column.declared_range
        for column in resolved.compile_planned().columns
        if column.declared_range is not None
    }


def _outside(resolved: "ResolvedEmit", column: str, value: str) -> bool:
    """Whether the date or timestamp *value* lies outside *column*'s declared range."""
    held = _declared(resolved).get(column)
    if held is None:
        return False
    moment = instant_ms(value)
    low, high = held
    return (
        moment is None or (low is not None and moment < low) or (high is not None and moment > high)
    )


def validity_range(
    history: "ResolvedEmit", emits: Sequence["ResolvedEmit"]
) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """The instants, in milliseconds, every start and closed end of *history*'s versions keeps to.

    The intersection of the ranges declared on the history's validity columns and on every
    projected column that copies a version's start or end; ``None`` when none declares a range.
    An open end the entry spells with ``openEnd`` is no closed end and keeps to nothing.
    """
    ranges = [
        held
        for name, held in _declared(history).items()
        if name in _validity_columns(history).values()
    ]
    for other in emits:
        found = projection_of(other.extension, other.expose_id)
        if found is None or nfc(found[1]) != nfc(history.expose_id):
            continue
        declared = _declared(other)
        ranges.extend(declared[name] for name in _copies(other, emits) if name in declared)
    if not ranges:
        return None
    lows = [low for low, _ in ranges if low is not None]
    highs = [high for _, high in ranges if high is not None]
    return (max(lows) if lows else None, min(highs) if highs else None)


def _planned_setters(resolved: "ResolvedEmit", emits: Sequence["ResolvedEmit"]) -> Dict[str, str]:
    """The columns of *resolved* that a history or an events or periods entry sets, by what sets them."""
    found: Dict[str, str] = {}
    if history_entries_of(resolved.extension, resolved.expose_id):
        semantics = (
            declared_exposes(resolved.contract_document)
            .get(resolved.expose_id, {})
            .get("semantics")
        )
        for column in window_ends(semantics).values():
            found.setdefault(nfc(column), "its history")
    held = projection_of(resolved.extension, resolved.expose_id)
    if held is None or held[0] == SNAPSHOTS_KEY:
        return found
    key, of, entry = held
    source = next((other for other in emits if nfc(other.expose_id) == nfc(of)), None)
    if source is None:
        return found
    label = "its periods entry" if key == PERIODS_KEY else "its events entry"
    names = projected_cell_names(key, entry, _column_names(source), _column_names(resolved))
    for name in names:
        found.setdefault(nfc(name), label)
    return found


def uncut_columns(resolved: "ResolvedEmit", emits: Sequence["ResolvedEmit"]) -> FrozenSet[str]:
    """The columns of *resolved* the cut leaves alone because a history or a projection sets them.

    A history's validity start and end, and the cells an events or periods entry sets. A column a
    rule or a relationship sets, and every column of a snapshot, are left alone by the plan already.
    """
    return frozenset(_planned_setters(resolved, emits))


class SetBy(NamedTuple):
    """What else sets an emit's columns: its keys and relationships, and whether setters hold to ranges.

    ``related`` names the columns a key or a relationship sets; with ``holds`` the run keeps the
    setters of :func:`held_columns` to their ranges (ADR 0024, the amendment of 2026-11-25).
    """

    related: FrozenSet[str] = frozenset()
    holds: bool = False


def range_setter_notes(
    resolved: "ResolvedEmit", plan: Any, emits: Sequence["ResolvedEmit"], set_by: SetBy
) -> Tuple[str, ...]:
    """One sentence for each column of *resolved* that declares a range and that something else sets.

    Advice only, as the cut's own notes are: a refusal would block a bundle whose layout writes
    inside the range. *plan* is the emit's :class:`~flux_engine.generator.rows.GeneratorPlan`.
    A column :func:`held_columns` names is not noted when *set_by* says the run holds it.
    """
    setters = _planned_setters(resolved, emits)
    kept = held_columns(resolved, emits) if set_by.holds else frozenset()
    if plan.projected:
        for item in plan.columns:
            setters.setdefault(nfc(item.column.name), "the history it is projected from")
    rules = plan.rules
    for name in () if rules is None else sorted(rules.covers_every_row):
        setters.setdefault(nfc(name), "a rule of the emit")
    for name in sorted(set_by.related):
        setters.setdefault(nfc(name), "its key or relationship")
    notes = []
    for item in plan.columns:
        held: Optional[Tuple[str, str]] = range_text(item.column)
        setter = setters.get(nfc(item.column.name))
        if held is None or setter is None or nfc(item.column.name) in kept:
            continue
        notes.append(
            f"note: {resolved.expose_id}.{item.column.name} is not drawn inside the contract's "
            f"range {range_phrase(*held)}: {setter} sets the column, and verify counts the rows "
            "outside the range"
        )
    return tuple(notes)
