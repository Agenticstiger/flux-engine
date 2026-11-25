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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, FrozenSet, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.generator.declared_range import range_phrase, range_text
from flux_engine.generator.projections import projected_cell_names, projection_of
from flux_engine.seam.history import history_entries_of
from flux_engine.seam.keys import declared_exposes, window_ends
from flux_engine.seam.projections import PERIODS_KEY, SNAPSHOTS_KEY

if TYPE_CHECKING:
    from flux_engine.seam.resolve import ResolvedEmit

__all__ = ["range_setter_notes", "uncut_columns"]


def _column_names(resolved: "ResolvedEmit") -> Tuple[str, ...]:
    return tuple(column.name for column in resolved.compile_planned().columns)


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


def range_setter_notes(
    resolved: "ResolvedEmit", plan: Any, emits: Sequence["ResolvedEmit"], related: FrozenSet[str]
) -> Tuple[str, ...]:
    """One sentence for each column of *resolved* that declares a range and that something else sets.

    Advice only, as the cut's own notes are: a refusal would block a bundle whose layout writes
    inside the range. *plan* is the emit's :class:`~flux_engine.generator.rows.GeneratorPlan`.
    """
    setters = _planned_setters(resolved, emits)
    if plan.projected:
        for item in plan.columns:
            setters.setdefault(nfc(item.column.name), "the history it is projected from")
    rules = plan.rules
    for name in () if rules is None else sorted(rules.covers_every_row):
        setters.setdefault(nfc(name), "a rule of the emit")
    for name in sorted(related):
        setters.setdefault(nfc(name), "its key or relationship")
    notes = []
    for item in plan.columns:
        held: Optional[Tuple[str, str]] = range_text(item.column)
        setter = setters.get(nfc(item.column.name))
        if held is None or setter is None:
            continue
        notes.append(
            f"note: {resolved.expose_id}.{item.column.name} is not drawn inside the contract's "
            f"range {range_phrase(*held)}: {setter} sets the column, and verify counts the rows "
            "outside the range"
        )
    return tuple(notes)
