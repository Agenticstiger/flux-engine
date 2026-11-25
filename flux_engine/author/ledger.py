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

The register lists what an owner can leave out, and the profile reader refuses a few entries of
format 1-3-0 without some of their members (a calendar entry with none of its histograms, a date
pair with no ``order``, an exposure with one of its two counts). So :meth:`Ledger.unused_entries`
names such an entry whole when none of its statistics is read, and lists no member the reader
needs beside one that is.

The shape follows mypy's ``--warn-unused-configs`` and dbt's
``--warn-error-options``: every input that has no effect is named, with what it
is, rather than dropped without a word.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from flux_engine.author.forms import unread_reason
from flux_engine.seam.parameters import MAX_REASON_LENGTH, render_pointer
from flux_stats.definitions import CORRELATION
from flux_stats.rank_correlation import RANK_IDENTIFIERS

__all__ = ["USED", "UNUSED", "WITHHELD", "Ledger", "LedgerEntry"]

USED = "used"
UNUSED = "unused"
WITHHELD = "withheld"

_FREE = "F"
_RANK_DEFINITIONS = RANK_IDENTIFIERS
_CORRELATION = CORRELATION.identifier

#: Why a statistic of a section no rule reads is unused, by section.
_SECTION_REASONS: Mapping[str, str] = {
    "columnPairs": (
        "a correlation between two number columns has no declaration a generator emit draws from"
    ),
    "periods": "a generator emit draws no mean month by month",
    "relationships": (
        "children per parent are read from an ungrouped histogram or mean of a one-column "
        "relationship the contract declares, or from a histogram grouped by a column of its parent "
        "that the layout can be given, and this is none of those"
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


def _pair_statistics(pairs: Any) -> Iterator[Tuple[Tuple[str, ...], Mapping[str, Any]]]:
    """Each statistic of each ``columnPairs`` entry, with the tokens that point at it below the table."""
    for index, item in enumerate(pairs if isinstance(pairs, list) else ()):
        statistics = item.get("statistics") if isinstance(item, Mapping) else None
        for position, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
            if isinstance(statistic, Mapping):
                yield ("columnPairs", str(index), "statistics", str(position)), statistic


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


@dataclass(frozen=True)
class _Group:
    """An entry of a format 1-3-0 profile, which the reader refuses without some of its members.

    *members* are the statistics and metrics the ledger holds of it, by their tokens. *needed* are
    the ones the reader requires of the entry (a date pair's ``order``, an exposure's two counts),
    so one of them leaves only with the whole entry. An entry with none needed requires at least
    one member, whichever (a calendar's three histograms): the members of one that is all unused
    leave as the whole entry. *held_by* is a member that has to leave with this entry or not be
    there: a history lists its ``reasons`` over its tracked columns, and one of those cannot leave
    while the reasons are kept.
    """

    tokens: Tuple[str, ...]
    members: Tuple[Tuple[str, ...], ...]
    needed: Tuple[Tuple[str, ...], ...] = ()
    held_by: Optional[Tuple[str, ...]] = None


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
        #: The entries of format 1-3-0 whose members the reader does not let leave one by one.
        self._groups: List[_Group] = []
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
                    dated=True,
                )

    def _relationship_exposure(self, index: int, item: Mapping[str, Any]) -> None:
        """List the children and active days of each exposure a relationship publishes."""
        exposure = item.get("exposure")
        for position, each in enumerate(exposure if isinstance(exposure, list) else ()):
            if not isinstance(each, Mapping):
                continue
            base = ("relationships", str(index), "exposure", str(position))
            for key in ("children", "activeDays"):
                node = each.get(key)
                if isinstance(node, Mapping):
                    self._add(base + (key,), node, _SECTION_REASONS["exposure"], metric=True)
            # The reader needs both counts of an exposure.
            counts = tuple(base + (key,) for key in ("children", "activeDays"))
            self._group(base, counts, needed=counts)

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

    def _statistics(
        self, base: Tuple[str, ...], item: Mapping[str, Any], reason: str
    ) -> List[Tuple[str, ...]]:
        """List the statistics of *item*, and say where each is."""
        found = item.get("statistics")
        listed: List[Tuple[str, ...]] = []
        for index, statistic in enumerate(found if isinstance(found, list) else ()):
            if isinstance(statistic, Mapping):
                tokens = base + ("statistics", str(index))
                self._add(tokens, statistic, reason, metric=False)
                listed.append(tokens)
        return listed

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
                    self._pair(
                        base + (section, str(index)),
                        item,
                        _SECTION_REASONS[section],
                        dated=section == "datePairs",
                    )
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
            held: List[Tuple[str, ...]] = []
            for key in ("month", "dayOfWeek", "hourOfWeek"):
                node = item.get(key) if isinstance(item, Mapping) else None
                if isinstance(node, Mapping):
                    tokens = base + ("calendar", str(index), key)
                    self._add(
                        tokens,
                        node,
                        "no rule of fluxsim init reads this calendar histogram",
                        metric=False,
                    )
                    held.append(tokens)
            # The reader needs one of the three histograms, whichever.
            self._group(base + ("calendar", str(index)), tuple(held))

    def _pair(
        self, base: Tuple[str, ...], item: Mapping[str, Any], reason: str, *, dated: bool = False
    ) -> None:
        """A section's statistics, and the ``order`` of a date pair when it has one.

        A date pair (*dated*) is one entry the reader refuses without its ``order``; its statistics
        may all be gone.
        """
        listed = self._statistics(base, item, reason)
        order = item.get("order")
        if isinstance(order, Mapping):
            self._add(base + ("order",), order, reason, metric=False)
            if dated:
                self._group(base, tuple(listed) + (base + ("order",),), needed=(base + ("order",),))

    def _rank_reasons(self, base: Tuple[str, ...], pairs: Any) -> None:
        """Give a rank correlation, and a Pearson correlation, of a column pair its own reason."""
        for tokens, statistic in _pair_statistics(pairs):
            entry = self._entries.get(base + tokens)
            if entry is None or entry.state != UNUSED:
                continue
            definition = statistic.get("definition")
            if definition in _RANK_DEFINITIONS:
                entry.note = _RANK_REASON
            elif definition == _CORRELATION:
                entry.note = _CORRELATION_REASON

    def _history(self, base: Tuple[str, ...], history: Mapping[str, Any]) -> None:
        """The statistics of a table's ``history`` (profile format 1-3-0), each unused until read.

        The reader needs five of them and, of each tracked column, both its statistics, and it
        refuses ``reasons`` over a column the history does not track.
        """
        listed: List[Tuple[str, ...]] = []
        for key in _HISTORY_MEMBERS:
            node = history.get(key)
            if isinstance(node, Mapping):
                self._add(base + (key,), node, _HISTORY_REASON, metric=False)
                listed.append(base + (key,))
        reasons = base + ("reasons",)
        columns = history.get("columns")
        for name, entry in (columns.items() if isinstance(columns, Mapping) else ()):
            tracked: List[Tuple[str, ...]] = []
            for key in ("initial", "transitions"):
                node = entry.get(key) if isinstance(entry, Mapping) else None
                if isinstance(node, Mapping):
                    tokens = base + ("columns", str(name), key)
                    self._add(tokens, node, _HISTORY_REASON, metric=False)
                    tracked.append(tokens)
            listed.extend(tracked)
            self._group(
                base + ("columns", str(name)),
                tuple(tracked),
                needed=tuple(tracked),
                held_by=reasons if reasons in self._entries else None,
            )
        needed = tuple(base + (key,) for key in _HISTORY_MEMBERS if key != "reasons")
        self._group(base, tuple(listed), needed=needed)

    def _group(
        self,
        tokens: Tuple[str, ...],
        members: Tuple[Tuple[str, ...], ...],
        *,
        needed: Tuple[Tuple[str, ...], ...] = (),
        held_by: Optional[Tuple[str, ...]] = None,
    ) -> None:
        """Note an entry of the profile at *tokens* the reader refuses without some of *members*."""
        held = tuple(member for member in members if member in self._entries)
        if held:
            self._groups.append(
                _Group(
                    tokens,
                    held,
                    tuple(member for member in needed if member in self._entries),
                    held_by,
                )
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
        """What an owner can leave out of the profile, each as one pointer, with its reason.

        An unused statistic is listed alone, except where the profile reader refuses an entry
        without some of its members (:class:`_Group`). An entry no member of which the bundle
        reads is listed whole, by its own pointer, in place of its members. In an entry with a
        member the bundle reads, a member is listed alone only if the reader lets it leave alone,
        and one it needs is not listed. A profile with exactly the listed pointers dropped is one
        the reader accepts. The inventory (:meth:`__iter__`) still holds each statistic, with the
        state the rules gave it, so the unused ones of it are not this list: an entry the reader
        holds whole has each of its members there. ``init --json`` carries this list as
        ``ledger.unused``.
        """
        listing: Dict[Tuple[str, ...], Optional[LedgerEntry]] = dict(self._entries)
        for group in self._groups:
            self._settle(group, listing)
        return [entry for entry in listing.values() if entry is not None and entry.state == UNUSED]

    def _settle(self, group: _Group, listing: Dict[Tuple[str, ...], Optional[LedgerEntry]]) -> None:
        """Replace the members of *group* in *listing* by what an owner can leave out of it."""
        members = [self._entries[tokens] for tokens in group.members]
        if any(member.state == USED for member in members):
            for tokens in group.needed:
                listing[tokens] = None
            return
        unused = [member for member in members if member.state == UNUSED]
        if not unused:
            return
        for tokens in group.members:
            listing[tokens] = None
        if group.held_by is not None and self._entries[group.held_by].state != UNUSED:
            return
        listing[group.members[0]] = LedgerEntry(
            group.tokens, None, UNUSED, _joined_reasons([member.note for member in unused])
        )


def _joined_reasons(notes: Sequence[str]) -> str:
    """The reasons of the members of an entry, each said once, as the one reason of the entry."""
    distinct = list(dict.fromkeys(notes))
    joined = "; ".join(distinct)
    if len(joined) <= MAX_REASON_LENGTH:
        return joined
    more = len(distinct) - 1
    return f"{distinct[0]}; and {more} other reason{'s' if more != 1 else ''} for its other members"
