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

from flux_engine.author.forms import unread_reason
from flux_engine.seam.parameters import render_pointer
from flux_stats.rank_correlation import RANK_IDENTIFIERS

__all__ = ["USED", "UNUSED", "WITHHELD", "Ledger", "LedgerEntry"]

USED = "used"
UNUSED = "unused"
WITHHELD = "withheld"

_FREE = "F"
_RANK_DEFINITIONS = RANK_IDENTIFIERS
_CORRELATION = "correlation/1"

#: Why a statistic of a section no rule reads is unused, by section.
_SECTION_REASONS: Mapping[str, str] = {
    "columnPairs": (
        "a correlation between two number columns has no declaration a generator emit draws from"
    ),
    "periods": "a generator emit draws no mean month by month",
    "relationships": (
        "children per parent are read from an ungrouped histogram or mean of a one-column "
        "relationship the contract declares, and this is neither"
    ),
    "queries": "the count a query matches has no declaration a generator emit draws from",
    "datePairs": "no rule of fluxsim init reads this statistic of the days between two dates",
    "dates": "no rule of fluxsim init reads this statistic of the days between two dates",
    "exposure": "no rule of fluxsim init reads this exposure of a parent's lifetime",
}

#: Why a rank correlation of a pair no copula holds is unused, and why a Pearson correlation is
#: unused now that the copula reads rank correlations.
_RANK_REASON = "no copula of fluxsim init holds this pair, so no declaration reads its value"
_CORRELATION_REASON = (
    "the copula reads rank correlations (kendallTau/1, spearmanRho/1), and no declaration draws "
    "from a Pearson correlation"
)

#: Why a column statistic no rule took up is unused.
_COLUMN_REASON = "no rule of fluxsim init reads this statistic for this column"

#: Why a statistic of a table's history no rule took up is unused.
_HISTORY_REASON = "no rule of fluxsim init reads this statistic of a history"
_HISTORY_MEMBERS = (
    "versionsPerEntity",
    "firstStart",
    "versionLength",
    "chain",
    "lastVersion",
    "reasons",
)
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
    #: The noise a statistic was released under (format 1-2-0): mechanism, scale and the
    #: variance of each cell, and read as a point estimate (:mod:`flux_engine.author.noisy`).
    noise: Optional[Mapping[str, str]] = None

    @property
    def pointer(self) -> str:
        return render_pointer(self.tokens)

    def as_document(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {"pointer": self.pointer, "state": self.state}
        if self.definition is not None:
            document["definition"] = self.definition
        document["note"] = self.note
        if self.noise is not None:
            document["noise"] = dict(self.noise)
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

    def __init__(
        self,
        profile: Mapping[str, Any],
        noisy: Optional[Mapping[Tuple[str, ...], Mapping[str, str]]] = None,
    ) -> None:
        self._entries: Dict[Tuple[str, ...], LedgerEntry] = {}
        #: The statistics a profile of the contract would publish in place of the bundle's
        #: assumptions (register format 1-3-0, ``replacedBy``): each statistic's pointer and
        #: definition, with how many assumed numbers it would replace. Set once the register is built.
        self.replacements: List[Tuple[str, Optional[str], int]] = []
        #: The noise each noisy statistic of the profile was released under, by its tokens.
        self._noisy = dict(noisy or {})
        tables = profile.get("tables")
        for name, table in (tables.items() if isinstance(tables, Mapping) else ()):
            if isinstance(table, Mapping):
                self._table(str(name), table)
        for section in ("relationships", "queries"):
            self._listed(section, profile.get(section))

    def _listed(self, section: str, found: Any) -> None:
        """List the statistics of each entry of a profile section, and a relationship's extras."""
        for index, item in enumerate(found if isinstance(found, list) else ()):
            if not isinstance(item, Mapping):
                continue
            self._statistics((section, str(index)), item, _SECTION_REASONS[section])
            if section == "relationships":
                self._relationship_dates(index, item)
                self._relationship_exposure(index, item)

    def _relationship_dates(self, index: int, item: Mapping[str, Any]) -> None:
        """List the statistics of each date pair a relationship publishes."""
        dates = item.get("dates")
        for position, pair in enumerate(dates if isinstance(dates, list) else ()):
            if isinstance(pair, Mapping):
                self._pair(
                    ("relationships", str(index), "dates", str(position)),
                    pair,
                    _SECTION_REASONS["dates"],
                )

    def _relationship_exposure(self, index: int, item: Mapping[str, Any]) -> None:
        """List the children and active days of each exposure a relationship publishes."""
        exposure = item.get("exposure")
        for position, each in enumerate(exposure if isinstance(exposure, list) else ()):
            if not isinstance(each, Mapping):
                continue
            for key in ("children", "activeDays"):
                node = each.get(key)
                if isinstance(node, Mapping):
                    self._add(
                        ("relationships", str(index), "exposure", str(position), key),
                        node,
                        _SECTION_REASONS["exposure"],
                        metric=True,
                    )

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
                tokens,
                definition,
                WITHHELD,
                f"the profile withheld it (status {withheld})",
                self._noisy.get(tokens),
            )
            return
        self._entries[tokens] = LedgerEntry(
            tokens, definition, UNUSED, reason, self._noisy.get(tokens)
        )

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
        self._columns(base, table.get("columns"))
        history = table.get("history")
        if isinstance(history, Mapping):
            self._history(base + ("history",), history)
        self._cliques(base, table.get("cliques"))
        self._calendar(base, table.get("calendar"))
        for section in ("columnPairs", "periods", "datePairs"):
            found = table.get(section)
            for index, item in enumerate(found if isinstance(found, list) else ()):
                if isinstance(item, Mapping):
                    self._pair(base + (section, str(index)), item, _SECTION_REASONS[section])
        self._rank_reasons(base, table.get("columnPairs"))

    def _columns(self, base: Tuple[str, ...], columns: Any) -> None:
        for column, entry in (columns.items() if isinstance(columns, Mapping) else ()):
            if isinstance(entry, Mapping):
                self._column(base + ("columns", str(column)), entry, columns)

    def _column(self, tokens: Tuple[str, ...], entry: Mapping[str, Any], columns: Any) -> None:
        metrics = entry.get("metrics")
        for metric, node in (metrics.items() if isinstance(metrics, Mapping) else ()):
            if isinstance(node, Mapping):
                self._add(tokens + ("metrics", str(metric)), node, _COLUMN_REASON, metric=True)
        statistics = entry.get("statistics")
        for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
            if isinstance(statistic, Mapping):
                reason = unread_reason(statistic, columns) or (
                    _GROUPED_REASON if "groupBy" in statistic else _COLUMN_REASON
                )
                self._add(tokens + ("statistics", str(index)), statistic, reason, metric=False)

    def _cliques(self, base: Tuple[str, ...], cliques: Any) -> None:
        for index, item in enumerate(cliques if isinstance(cliques, list) else ()):
            if isinstance(item, Mapping):
                tokens = base + ("cliques", str(index))
                self._entries[tokens] = LedgerEntry(
                    tokens,
                    _definition(item),
                    UNUSED,
                    "no rule of fluxsim init reads this clique",
                    self._noisy.get(tokens),
                )

    def _calendar(self, base: Tuple[str, ...], calendar: Any) -> None:
        """List the month, day-of-week and hour-of-week histograms of a table's calendar as unused."""
        for index, item in enumerate(calendar if isinstance(calendar, list) else ()):
            for key in ("month", "dayOfWeek", "hourOfWeek"):
                node = item.get(key) if isinstance(item, Mapping) else None
                if isinstance(node, Mapping):
                    self._add(
                        base + ("calendar", str(index), key),
                        node,
                        "no rule of fluxsim init reads this calendar histogram",
                        metric=False,
                    )

    def _pair(self, base: Tuple[str, ...], item: Mapping[str, Any], reason: str) -> None:
        """A section's statistics, and the ``order`` of a date pair when it has one."""
        self._statistics(base, item, reason)
        order = item.get("order")
        if isinstance(order, Mapping):
            self._add(base + ("order",), order, reason, metric=False)

    def _rank_reasons(self, base: Tuple[str, ...], pairs: Any) -> None:
        """Give a rank correlation, and a Pearson correlation, of a column pair its own reason."""
        for index, item in enumerate(pairs if isinstance(pairs, list) else ()):
            statistics = item.get("statistics") if isinstance(item, Mapping) else None
            for position, statistic in enumerate(
                statistics if isinstance(statistics, list) else ()
            ):
                entry = self._entries.get(
                    base + ("columnPairs", str(index), "statistics", str(position))
                )
                if entry is None or entry.state != UNUSED or not isinstance(statistic, Mapping):
                    continue
                if statistic.get("definition") in _RANK_DEFINITIONS:
                    entry.note = _RANK_REASON
                elif statistic.get("definition") == _CORRELATION:
                    entry.note = _CORRELATION_REASON

    def _history(self, base: Tuple[str, ...], history: Mapping[str, Any]) -> None:
        """The statistics of a table's ``history`` (profile format 1-3-0), each unused until read."""
        for key in _HISTORY_MEMBERS:
            node = history.get(key)
            if isinstance(node, Mapping):
                self._add(base + (key,), node, _HISTORY_REASON, metric=False)
        columns = history.get("columns")
        for name, entry in (columns.items() if isinstance(columns, Mapping) else ()):
            for key in ("initial", "transitions"):
                node = entry.get(key) if isinstance(entry, Mapping) else None
                if isinstance(node, Mapping):
                    self._add(
                        base + ("columns", str(name), key), node, _HISTORY_REASON, metric=False
                    )

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

    def record(self, tokens: Tuple[str, ...], how: str) -> None:
        """List a decision about the profile that is no statistic, as used, *how* saying for what.

        A relationship's ``structural`` classes are one: they are not counted into a register and
        no pointer into them is a citation, so the ledger holds them only once a rule has read
        them.
        """
        place = tuple(tokens)
        self._entries[place] = LedgerEntry(place, None, USED, how)

    def unused(self, tokens: Tuple[str, ...], reason: str) -> None:
        """Say why the statistic at *tokens* is unused, unless a rule used it after all."""
        entry = self._owner(tuple(tokens))
        if entry is not None and entry.state == UNUSED:
            entry.note = reason

    def add_unused(self, tokens: Tuple[str, ...], reason: str) -> None:
        """List a part of the profile the statistics do not hold (a ``references`` entry) as unused."""
        self._entries[tuple(tokens)] = LedgerEntry(tuple(tokens), None, UNUSED, reason)

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
