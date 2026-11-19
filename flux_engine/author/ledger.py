# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Which statistics of a profile a bundle rests on, and why the others are not read.

ADR 0014 section 1. :class:`Ledger` lists every statistic and metric of the
profile ``fluxsim init`` is handed, by its JSON Pointer (RFC 6901), and keeps
one state for each:

- ``used``: a number of the bundle rests on it, a rule read it to bound or
  shape one, a property scores against it, or init checked the bundle against
  it;
- ``unused``, with the reason no rule reads it;
- ``withheld``: the profile publishes no value for it (a status other than
  ``F``), so there was nothing to read.

The bundle records the unused ones in its parameter register
(PARAMETER-REGISTER-SPEC, format 1-2-0), and ``--strict`` refuses them.

The shape follows mypy's ``--warn-unused-configs`` and dbt's
``--warn-error-options``: every input that has no effect is named, with what it
is, rather than dropped without a word.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from flux_engine.seam.parameters import render_pointer

__all__ = ["USED", "UNUSED", "WITHHELD", "Ledger", "LedgerEntry"]

USED = "used"
UNUSED = "unused"
WITHHELD = "withheld"

_FREE = "F"

#: Why a statistic of a section no rule reads is unused, by section.
_SECTION_REASONS: Mapping[str, str] = {
    "columnPairs": (
        "a correlation between two number columns has no declaration a generator emit " "draws from"
    ),
    "periods": "a generator emit draws no mean month by month",
    "relationships": (
        "children per parent are read from an ungrouped histogram or mean of a one-column "
        "relationship the contract declares, and this is neither"
    ),
    "queries": "the count a query matches has no declaration a generator emit draws from",
}

#: Why a column statistic no rule took up is unused.
_COLUMN_REASON = "no rule of fluxsim init reads this statistic for this column"
_GROUPED_REASON = (
    "a statistic grouped by another column is not read: the column is drawn from its own "
    "statistics"
)


@dataclass
class LedgerEntry:
    """One statistic or metric of the profile, and what the bundle made of it."""

    tokens: Tuple[str, ...]
    definition: Optional[str]
    state: str
    #: How it is used, why it is unused, or how it was withheld: one sentence.
    note: str

    @property
    def pointer(self) -> str:
        return render_pointer(self.tokens)

    def as_document(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {"pointer": self.pointer, "state": self.state}
        if self.definition is not None:
            document["definition"] = self.definition
        document["note"] = self.note
        return document


def _status(node: Mapping[str, Any]) -> Optional[str]:
    status = node.get("status")
    return str(status) if status is not None else None


def _statistic_withheld(statistic: Mapping[str, Any]) -> Optional[str]:
    """The status a statistic is withheld under when no group of it is published, else ``None``."""
    groups = statistic.get("groups")
    if not isinstance(groups, list) or not groups:
        return "no group"
    statuses = [_status(group) if isinstance(group, Mapping) else None for group in groups]
    if any(status == _FREE for status in statuses):
        return None
    shown = sorted({str(status) for status in statuses})
    return ", ".join(shown)


def _definition(node: Mapping[str, Any]) -> Optional[str]:
    definition = node.get("definition")
    return str(definition) if isinstance(definition, str) else None


class Ledger:
    """Every statistic and metric of one profile document, each with its state."""

    def __init__(self, profile: Mapping[str, Any]) -> None:
        self._entries: Dict[Tuple[str, ...], LedgerEntry] = {}
        tables = profile.get("tables")
        for name, table in (tables.items() if isinstance(tables, Mapping) else ()):
            if isinstance(table, Mapping):
                self._table(str(name), table)
        for section in ("relationships", "queries"):
            found = profile.get(section)
            for index, item in enumerate(found if isinstance(found, list) else ()):
                if isinstance(item, Mapping):
                    self._statistics((section, str(index)), item, _SECTION_REASONS[section])

    # -- reading the document ------------------------------------------------

    def _add(
        self, tokens: Tuple[str, ...], node: Mapping[str, Any], reason: str, *, metric: bool
    ) -> None:
        definition = _definition(node)
        if metric:
            status = _status(node)
            withheld = None if status == _FREE else (status or "none")
        else:
            withheld = _statistic_withheld(node)
        if withheld is not None:
            self._entries[tokens] = LedgerEntry(
                tokens, definition, WITHHELD, f"the profile withheld it (status {withheld})"
            )
            return
        self._entries[tokens] = LedgerEntry(tokens, definition, UNUSED, reason)

    def _statistics(self, base: Tuple[str, ...], item: Mapping[str, Any], reason: str) -> None:
        found = item.get("statistics")
        for index, statistic in enumerate(found if isinstance(found, list) else ()):
            if isinstance(statistic, Mapping):
                self._add(base + ("statistics", str(index)), statistic, reason, metric=False)

    def _table(self, name: str, table: Mapping[str, Any]) -> None:
        base = ("tables", name)
        if "rowCount" in table:
            self._entries[base + ("rowCount",)] = LedgerEntry(
                base + ("rowCount",), None, UNUSED, "no rule of fluxsim init reads it"
            )
        columns = table.get("columns")
        for column, entry in (columns.items() if isinstance(columns, Mapping) else ()):
            if not isinstance(entry, Mapping):
                continue
            tokens = base + ("columns", str(column))
            metrics = entry.get("metrics")
            for metric, node in (metrics.items() if isinstance(metrics, Mapping) else ()):
                if isinstance(node, Mapping):
                    self._add(tokens + ("metrics", str(metric)), node, _COLUMN_REASON, metric=True)
            statistics = entry.get("statistics")
            for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
                if isinstance(statistic, Mapping):
                    reason = _GROUPED_REASON if "groupBy" in statistic else _COLUMN_REASON
                    self._add(tokens + ("statistics", str(index)), statistic, reason, metric=False)
        for section in ("columnPairs", "periods"):
            found = table.get(section)
            for index, item in enumerate(found if isinstance(found, list) else ()):
                if isinstance(item, Mapping):
                    self._statistics(base + (section, str(index)), item, _SECTION_REASONS[section])

    # -- what the rules did --------------------------------------------------

    def _owner(self, tokens: Tuple[str, ...]) -> Optional[LedgerEntry]:
        """The entry *tokens* point into: the longest listed prefix of them."""
        for depth in range(len(tokens), 0, -1):
            entry = self._entries.get(tuple(tokens[:depth]))
            if entry is not None:
                return entry
        return None

    def use(self, tokens: Tuple[str, ...], how: str) -> None:
        """Mark the statistic *tokens* point into as used, *how* saying for what.

        The first use is the one recorded; a withheld statistic stays withheld,
        and a pointer into no listed statistic is ignored.
        """
        entry = self._owner(tuple(tokens))
        if entry is None or entry.state == WITHHELD:
            return
        if entry.state != USED:
            entry.state = USED
            entry.note = how

    def unused(self, tokens: Tuple[str, ...], reason: str) -> None:
        """Say why the statistic at *tokens* is unused, unless a rule used it after all."""
        entry = self._owner(tuple(tokens))
        if entry is not None and entry.state == UNUSED:
            entry.note = reason

    def reset_under(self, tokens: Tuple[str, ...], reason: str) -> None:
        """Mark every published statistic under *tokens* unused, used or not: its use fell away."""
        prefix = tuple(tokens)
        for entry in self._entries.values():
            if entry.tokens[: len(prefix)] == prefix and entry.state != WITHHELD:
                entry.state = UNUSED
                entry.note = reason

    def unused_under(self, tokens: Tuple[str, ...], reason: str) -> None:
        """Give every unused statistic under *tokens* the *reason* (a table left out, say)."""
        prefix = tuple(tokens)
        for entry in self._entries.values():
            if entry.tokens[: len(prefix)] == prefix and entry.state == UNUSED:
                entry.note = reason

    # -- reporting -----------------------------------------------------------

    def __iter__(self) -> Iterator[LedgerEntry]:
        return iter(self._entries.values())

    def counts(self) -> Dict[str, int]:
        counts = {USED: 0, UNUSED: 0, WITHHELD: 0}
        for entry in self._entries.values():
            counts[entry.state] += 1
        return counts

    def unused_entries(self) -> List[LedgerEntry]:
        return [entry for entry in self._entries.values() if entry.state == UNUSED]
