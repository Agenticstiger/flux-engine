# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The projections a Simulation declares: snapshots, change events and periodic rows (ADR 0027).

A Simulation declares them under the top-level ``snapshots``, ``events`` and ``periods`` keys of its
namespace, in the FLUX bundle and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        snapshots:
          - { emit: subscription_current, of: subscription_version, ended: drop }
        events:
          - emit: subscription_change
            of: subscription_version
            date: recorded_at
            at: changed_at
            op: op
        periods:
          - { emit: subscription_month, of: subscription_version, column: month, match: overlap }

An entry names a *projected* emit: its rows follow from the history of the emit its ``of`` names
(ADR 0017), so the emit declares none. Three readers share this module. The ``read_*_block``
functions read what an entry says with no contract in hand: an unknown key at any level is the
reader's *code* (``FLX-3105``), and every other malformed value is its *refusal* code
(``FLX-3403``), each message naming the entry and the key. The ``check_*`` functions read an entry
against the contracts of its emit and of its history's emit (:class:`ProjectionTarget`).

**An entry changes the rows its emit generates**, so it is bound into ``H`` before any row is
drawn: :func:`bound_into_h` is the member the emit's entry for ``H`` gains, present only when the
emit has an entry, so a bundle that declares none hashes exactly as before. The history's emit and
its own entry for ``H`` ride beside it, in ``projects``, so a changed history moves its
projections. No plan document enters ``H``. ``G`` leaves every one of these members out
(:func:`flux_engine.seam.emit.seam_reusable_digest`).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Collection, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError, get
from flux_engine.seam.contract import CompiledColumn, LogicalKind, compile_expose
from flux_engine.seam.counts import ChildShape, CountsEntry
from flux_engine.seam.history import (
    Histogram,
    HistoryEntry,
    _histogram,
    _instant,
    _levels,
    _mapping,
    _optional_text,
    _text,
    _unknown,
    _whole,
)
from flux_engine.seam.keys import window_ends

__all__ = [
    "EVENTS_KEY",
    "GENERATED_PROJECTIONS",
    "PERIODS_KEY",
    "PROJECTION_KEYS",
    "PROJECTS_KEY",
    "SNAPSHOTS_KEY",
    "EventsEntry",
    "PeriodKind",
    "PeriodsEntry",
    "ProjectionTarget",
    "SnapshotEntry",
    "bound_into_h",
    "check_events",
    "check_one_projection_per_emit",
    "check_periods",
    "check_projected_emit",
    "check_snapshot",
    "events_entry_of",
    "periods_entry_of",
    "projection_keys_of",
    "projection_target",
    "projects_member",
    "read_events_block",
    "read_periods_block",
    "read_snapshots_block",
    "refuse_projection",
    "snapshot_entry_of",
]

#: The top-level keys of the namespace that declare projections, and the member an emit's ``H``
#: gains for the one it has.
SNAPSHOTS_KEY = "snapshots"
EVENTS_KEY = "events"
PERIODS_KEY = "periods"
PROJECTION_KEYS = (SNAPSHOTS_KEY, EVENTS_KEY, PERIODS_KEY)
#: The kinds this build generates (ADR 0027 section 3); the others are read and refused at plan time.
GENERATED_PROJECTIONS = (SNAPSHOTS_KEY, EVENTS_KEY, PERIODS_KEY)

#: The member of an emit's entry for ``H`` that holds the history's emit and its own entry.
PROJECTS_KEY = "projects"

_SNAPSHOT_KEYS = ("emit", "of", "ended")
_EVENTS_KEYS = (
    "emit",
    "of",
    "date",
    "at",
    "lag",
    "on",
    "op",
    "type",
    "before",
    "after",
    "when",
)
_PERIODS_KEYS = ("emit", "of", "column", "grain", "match", "kinds")
_KIND_KEYS = ("count", "when", "overrides")
_TYPE_KEYS = ("column", "values")
_LEVEL_KEYS = ("levels", "levelWeights")
_ENDED = ("keep", "drop")
_OPS = ("c", "u", "d")
_GRAINS = ("day", "week", "month", "quarter", "year")
_MATCHES = ("overlap", "start", "end")
_MOST_ENTRIES = 4096
_MOST_ROWS = 1 << 31
#: The most characters an override text may have.
_MOST_TEXT = 64
_UNIT_KINDS = (LogicalKind.DATE, LogicalKind.TIMESTAMP)
_INTEGER = re.compile(r"[+-]?\d+")


@dataclass(frozen=True)
class SnapshotEntry:
    """One emit's snapshot of a history: the entity's last version, one row per entity."""

    emit: str
    of: str
    ended: str = "keep"

    def as_member(self) -> Dict[str, Any]:
        """The entry as the emit's ``snapshots`` member holds it: no ``emit``, the default written."""
        return {"of": self.of, "ended": self.ended}


@dataclass(frozen=True)
class EventsEntry:
    """One emit's change events of a history: a ``c``, ``u`` or ``d`` per change."""

    emit: str
    of: str
    date: str
    at: Optional[str] = None
    lag: Optional[Histogram] = None
    on: Tuple[str, ...] = _OPS
    op: Optional[str] = None
    type_column: Optional[str] = None
    type_values: Tuple[Tuple[str, str], ...] = ()
    before: Tuple[Tuple[str, str], ...] = ()
    after: Tuple[Tuple[str, str], ...] = ()
    when: Optional[str] = None

    def as_member(self) -> Dict[str, Any]:
        """The entry as the emit's ``events`` member holds it: no ``emit``, each key only when declared."""
        member: Dict[str, Any] = {"of": self.of, "date": self.date}
        if self.at is not None:
            member["at"] = self.at
        if self.lag is not None:
            member["lag"] = self.lag.as_member()
        member["on"] = list(self.on)
        if self.op is not None:
            member["op"] = self.op
        if self.type_column is not None:
            member["type"] = {"column": self.type_column, "values": dict(self.type_values)}
        if self.before:
            member["before"] = dict(self.before)
        if self.after:
            member["after"] = dict(self.after)
        if self.when is not None:
            member["when"] = self.when
        return member


@dataclass(frozen=True)
class PeriodKind:
    """One kind of periodic row: a count per pair, a condition and the columns it overrides."""

    count: Union[int, ChildShape] = 1
    when: Optional[str] = None
    overrides: Tuple[Tuple[str, str], ...] = ()

    def as_member(self) -> Dict[str, Any]:
        """The kind as the entry holds it: the count written, the others only when declared."""
        count = self.count if isinstance(self.count, int) else self.count.as_member()
        member: Dict[str, Any] = {"count": count}
        if self.when is not None:
            member["when"] = self.when
        if self.overrides:
            member["overrides"] = dict(self.overrides)
        return member


@dataclass(frozen=True)
class PeriodsEntry:
    """One emit's periodic rows of a history: the versions each period holds, as rows."""

    emit: str
    of: str
    column: str
    grain: Optional[str] = None
    match: str = "overlap"
    kinds: Tuple[PeriodKind, ...] = ()

    def as_member(self) -> Dict[str, Any]:
        """The entry as the emit's ``periods`` member holds it: no ``emit``, the default match written."""
        member: Dict[str, Any] = {"of": self.of, "column": self.column}
        if self.grain is not None:
            member["grain"] = self.grain
        member["match"] = self.match
        if self.kinds:
            member["kinds"] = [kind.as_member() for kind in self.kinds]
        return member


ProjectionEntry = Union[SnapshotEntry, EventsEntry, PeriodsEntry]


# ---------------------------------------------------------------------------
# Reading what an entry says
# ---------------------------------------------------------------------------


def _text_map(raw: Any, at: str, refusal: ErrorCode) -> Tuple[Tuple[str, str], ...]:
    """A non-empty mapping of non-empty strings to strings, sorted by key."""
    mapping = _mapping(raw, at, "a mapping of names to texts", refusal)
    if not mapping:
        raise FluxError(refusal, f"{at} is a mapping of names to texts, and holds none")
    found = []
    for key, value in mapping.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise FluxError(refusal, f"{at} maps a non-empty name to a text, not {key!r}")
        found.append((nfc(key), value))
    return tuple(sorted(found))


def _lag(raw: Any, at: str, code: ErrorCode, refusal: ErrorCode) -> Histogram:
    """A ``lag``: a histogram whose edges are whole days, from zero, ascending."""
    histogram = _histogram(raw, at, code, refusal, length=False)
    edges = [_whole(edge) for edge in histogram.edges]
    if any(edge is None for edge in edges):
        raise FluxError(refusal, f"{at}.bins holds an edge that is not a whole number of days")
    days = [edge for edge in edges if edge is not None]
    if days[0] < 0 or any(low >= high for low, high in zip(days, days[1:], strict=False)):
        raise FluxError(refusal, f"{at}.bins ascend from zero days or more")
    return histogram


def _on(raw: Any, at: str, refusal: ErrorCode) -> Tuple[str, ...]:
    """``on``: a non-empty list of ``c``, ``u`` and ``d``, each once."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise FluxError(refusal, f"{at}.on is a non-empty list of {list(_OPS)}")
    if any(item not in _OPS for item in raw) or len(set(raw)) != len(raw):
        raise FluxError(refusal, f"{at}.on holds each of {list(_OPS)} at most once, not {raw!r}")
    return tuple(item for item in _OPS if item in raw)


def _type(
    raw: Any, at: str, code: ErrorCode, refusal: ErrorCode
) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
    """``type``: the column and the text written for each of ``c``, ``d``, ``end`` and a tracked column."""
    mapping = _mapping(raw, at, "a mapping of column and values", refusal)
    _unknown(mapping, _TYPE_KEYS, at, code)
    column = _text(mapping, "column", at, refusal)
    if "values" not in mapping:
        raise FluxError(refusal, f"{at}.values is required")
    return column, _text_map(mapping["values"], f"{at}.values", refusal)


def _read_snapshot(
    raw: Mapping[str, Any], at: str, code: ErrorCode, refusal: ErrorCode
) -> SnapshotEntry:
    ended = raw.get("ended", "keep")
    if ended not in _ENDED:
        raise FluxError(refusal, f"{at}.ended is one of {list(_ENDED)}, not {ended!r}")
    return SnapshotEntry(emit=nfc(str(raw["emit"])), of=_text(raw, "of", at, refusal), ended=ended)


def _read_events(
    raw: Mapping[str, Any], at: str, code: ErrorCode, refusal: ErrorCode
) -> EventsEntry:
    if "date" not in raw:
        raise FluxError(refusal, f"{at}.date is required")
    column, values = (
        _type(raw["type"], f"{at}.type", code, refusal) if "type" in raw else (None, ())
    )
    return EventsEntry(
        emit=nfc(str(raw["emit"])),
        of=_text(raw, "of", at, refusal),
        date=_text(raw, "date", at, refusal),
        at=_optional_text(raw, "at", at, refusal),
        lag=_lag(raw["lag"], f"{at}.lag", code, refusal) if "lag" in raw else None,
        on=_on(raw["on"], at, refusal) if "on" in raw else _OPS,
        op=_optional_text(raw, "op", at, refusal),
        type_column=column,
        type_values=values,
        before=_text_map(raw["before"], f"{at}.before", refusal) if "before" in raw else (),
        after=_text_map(raw["after"], f"{at}.after", refusal) if "after" in raw else (),
        when=_optional_text(raw, "when", at, refusal),
    )


def _count(raw: Any, at: str, code: ErrorCode, refusal: ErrorCode) -> Union[int, ChildShape]:
    """A ``count``: a whole number of rows, or ``levels`` with ``levelWeights`` over whole counts."""
    if isinstance(raw, Mapping):
        if "levels" not in raw:
            raise FluxError(refusal, f"{at} is a whole number or {{levels, levelWeights}}")
        shape = _levels(raw, at, code, refusal)
        wholes = [_whole(level) for level in shape.values]
        if any(whole is None or whole >= _MOST_ROWS for whole in wholes):
            raise FluxError(refusal, f"{at}.levels holds a level that is not a whole number")
        return shape
    whole = _whole(raw)
    if whole is None or not 0 <= whole < _MOST_ROWS:
        raise FluxError(refusal, f"{at} is a whole number of rows, or {{levels, levelWeights}}")
    return whole


def _kind(raw: Any, at: str, code: ErrorCode, refusal: ErrorCode) -> PeriodKind:
    mapping = _mapping(raw, at, "a mapping of count, when and overrides", refusal)
    _unknown(mapping, _KIND_KEYS, at, code)
    return PeriodKind(
        count=_count(mapping["count"], f"{at}.count", code, refusal) if "count" in mapping else 1,
        when=_optional_text(mapping, "when", at, refusal),
        overrides=(
            _text_map(mapping["overrides"], f"{at}.overrides", refusal)
            if "overrides" in mapping
            else ()
        ),
    )


def _kinds(raw: Any, at: str, code: ErrorCode, refusal: ErrorCode) -> Tuple[PeriodKind, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise FluxError(refusal, f"{at}.kinds is a non-empty list of {{count, when, overrides}}")
    if len(raw) > _MOST_ENTRIES:
        raise FluxError(refusal, f"{at}.kinds holds more than {_MOST_ENTRIES} entries")
    return tuple(
        _kind(item, f"{at}.kinds[{index}]", code, refusal) for index, item in enumerate(raw)
    )


def _read_periods(
    raw: Mapping[str, Any], at: str, code: ErrorCode, refusal: ErrorCode
) -> PeriodsEntry:
    grain = raw.get("grain")
    if "grain" in raw and grain not in _GRAINS:
        raise FluxError(refusal, f"{at}.grain is one of {list(_GRAINS)}, not {grain!r}")
    match = raw.get("match", "overlap")
    if match not in _MATCHES:
        raise FluxError(refusal, f"{at}.match is one of {list(_MATCHES)}, not {match!r}")
    return PeriodsEntry(
        emit=nfc(str(raw["emit"])),
        of=_text(raw, "of", at, refusal),
        column=_text(raw, "column", at, refusal),
        grain=grain,
        match=match,
        kinds=_kinds(raw["kinds"], at, code, refusal) if "kinds" in raw else (),
    )


def _read_block(
    block: Any,
    reader: Any,
    keys: Sequence[str],
    *,
    emits: FrozenSet[str],
    where: str,
    code: ErrorCode,
    refusal: ErrorCode,
) -> Tuple[Any, ...]:
    """The entries of one projection block, read with *reader*, or refused."""
    if not isinstance(block, Sequence) or isinstance(block, (str, bytes)):
        raise FluxError(code, f"{where} is a list of {{emit, of, ...}} entries")
    if len(block) > _MOST_ENTRIES:
        raise FluxError(code, f"{where} holds more than {_MOST_ENTRIES} entries")
    found: Dict[str, Any] = {}
    for index, raw in enumerate(block):
        at = f"{where}[{index}]"
        if not isinstance(raw, Mapping):
            raise FluxError(code, f"{at} is a mapping with an emit and an of")
        _unknown(raw, keys, at, code)
        emit = _text(raw, "emit", at, refusal)
        named = f"{at} (emit {emit!r})"
        if emit not in emits:
            raise FluxError(
                refusal,
                f"{named}.emit names {emit!r}, which this Simulation does not emit "
                f"(it emits {sorted(emits)})",
            )
        if emit in found:
            raise FluxError(refusal, f"{named} is a second entry for {emit!r} in this block")
        entry = reader(raw, named, code, refusal)
        if entry.of not in emits:
            raise FluxError(
                refusal, f"{named}.of names {entry.of!r}, which this Simulation does not emit"
            )
        found[emit] = entry
    return tuple(found[key] for key in sorted(found))


def read_snapshots_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode, refusal: ErrorCode
) -> Tuple[SnapshotEntry, ...]:
    """Read a ``snapshots`` block, or refuse it, naming where, the entry and the key.

    An unknown key and a block or an entry of the wrong shape are *code* (``FLX-3105``). With
    *refusal* (``FLX-3403``): an emit or an ``of`` the Simulation does not emit, a second entry
    for one emit, and an ``ended`` other than ``keep`` or ``drop``.
    """
    return _read_block(
        block, _read_snapshot, _SNAPSHOT_KEYS, emits=emits, where=where, code=code, refusal=refusal
    )


def read_events_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode, refusal: ErrorCode
) -> Tuple[EventsEntry, ...]:
    """Read an ``events`` block, or refuse it, naming where, the entry and the key.

    An unknown key at any level, and a ``lag`` that is not a histogram, are *code*
    (``FLX-3105``). With *refusal* (``FLX-3403``): a missing ``date``, a ``lag`` whose edges are
    not ascending whole days from zero, an ``on`` that is not a list of ``c``, ``u`` and ``d``,
    and a ``type`` without its ``column`` and ``values``. What needs the contracts is checked by
    :func:`check_events`.
    """
    return _read_block(
        block, _read_events, _EVENTS_KEYS, emits=emits, where=where, code=code, refusal=refusal
    )


def read_periods_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode, refusal: ErrorCode
) -> Tuple[PeriodsEntry, ...]:
    """Read a ``periods`` block, or refuse it, naming where, the entry and the key.

    An unknown key at any level is *code* (``FLX-3105``). With *refusal* (``FLX-3403``): a
    missing ``column``, a ``grain`` or ``match`` outside its list, a ``count`` that is neither a
    whole number nor ``levels`` over whole counts, and a ``kinds`` that is not a list of kinds.
    What needs the contracts is checked by :func:`check_periods`.
    """
    return _read_block(
        block, _read_periods, _PERIODS_KEYS, emits=emits, where=where, code=code, refusal=refusal
    )


def snapshot_entry_of(
    extension: Optional[Mapping[str, Any]], emit: str, *, refusal: Optional[ErrorCode] = None
) -> Optional[SnapshotEntry]:
    """The snapshot entry an emit's bound extension holds, or ``None`` when it holds none.

    The entry is read as :func:`read_snapshots_block` reads one: an unknown key is ``FLX-3105``
    and any other malformed value is *refusal*, `FLX-3403` by default. The contracts are not checked here.
    """
    raw = extension.get(SNAPSHOTS_KEY) if extension is not None else None
    if not isinstance(raw, Mapping):
        return None
    return _read_snapshot(
        {"emit": emit, **raw}, f"snapshots ({emit})", get("FLX-3105"), refusal or get("FLX-3403")
    )


def events_entry_of(
    extension: Optional[Mapping[str, Any]], emit: str, *, refusal: Optional[ErrorCode] = None
) -> Optional[EventsEntry]:
    """The events entry an emit's bound extension holds, or ``None`` when it holds none.

    The entry is read as :func:`read_events_block` reads one: an unknown key is ``FLX-3105`` and
    any other malformed value is *refusal*, `FLX-3403` by default. The contracts are not checked here.
    """
    raw = extension.get(EVENTS_KEY) if extension is not None else None
    if not isinstance(raw, Mapping):
        return None
    return _read_events(
        {"emit": emit, **raw}, f"events ({emit})", get("FLX-3105"), refusal or get("FLX-3403")
    )


def periods_entry_of(
    extension: Optional[Mapping[str, Any]], emit: str, *, refusal: Optional[ErrorCode] = None
) -> Optional[PeriodsEntry]:
    """The periods entry an emit's bound extension holds, or ``None`` when it holds none.

    The entry is read as :func:`read_periods_block` reads one: an unknown key is ``FLX-3105`` and
    any other malformed value is *refusal*, `FLX-3403` by default. The contracts are not checked here.
    """
    raw = extension.get(PERIODS_KEY) if extension is not None else None
    if not isinstance(raw, Mapping):
        return None
    return _read_periods(
        {"emit": emit, **raw}, f"periods ({emit})", get("FLX-3105"), refusal or get("FLX-3403")
    )


def check_one_projection_per_emit(
    snapshots: Sequence[SnapshotEntry],
    events: Sequence[EventsEntry],
    periods: Sequence[PeriodsEntry],
    *,
    refusal: ErrorCode,
) -> None:
    """Refuse (*refusal*, ``FLX-3403``) an emit that more than one projection entry names."""
    named: Dict[str, str] = {}
    for key, entries in ((SNAPSHOTS_KEY, snapshots), (EVENTS_KEY, events), (PERIODS_KEY, periods)):
        for entry in entries:
            if entry.emit in named:
                raise FluxError(
                    refusal,
                    f"{key} ({entry.emit}) is a second projection entry for {entry.emit!r}, "
                    f"which {named[entry.emit]} names already: an emit has one projection",
                )
            named[entry.emit] = key


# ---------------------------------------------------------------------------
# Binding into H
# ---------------------------------------------------------------------------


def bound_into_h(entries: Sequence[ProjectionEntry], emit: str) -> Optional[Dict[str, Any]]:
    """The member of *emit*'s entry for ``H`` that its projection makes, or ``None``.

    The entry as its ``as_member`` writes it, with no ``emit``; ``None`` when *emit* has no entry,
    so the entry gains no key.
    """
    wanted = nfc(emit)
    for entry in entries:
        if entry.emit == wanted:
            return entry.as_member()
    return None


def projects_member(of: str, history_extension: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The ``projects`` member: the history's emit and that emit's own entry for ``H``.

    The entry holds the history entry, so a changed history moves the projection. No plan
    document enters it: a plan document holds the window, and the seed tree's root folds in every
    ``H``.
    """
    return {"emit": nfc(of), "entry": copy.deepcopy(dict(history_extension or {}))}


def projection_keys_of(extension: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    """The projection keys an emit's bound extension holds (the members :func:`bound_into_h` made)."""
    if extension is None:
        return ()
    return tuple(key for key in PROJECTION_KEYS if key in extension)


def refuse_projection(
    extension: Optional[Mapping[str, Any]], *, counter_based: bool, where: str, target: str
) -> None:
    """Refuse a projection the plan cannot generate: ``FLX-3105`` on kernel 0.1.0, else ``FLX-6210``.

    Kernel 0.1.0 has no history to project, so it refuses every key and names the kernel. On
    kernel 0.2.0 only a kind without a generator is refused, :data:`GENERATED_PROJECTIONS` being
    the kinds that have one.
    """
    keys = projection_keys_of(extension)
    if not keys:
        return
    prefix = f"{where}: " if where else ""
    if not counter_based:
        raise FluxError(
            get("FLX-3105"),
            f"{prefix}emit {target!r} configures {keys[0]}, which needs kernel 0.2.0; this run "
            "is kernel 0.1.0",
        )
    for key in keys:
        if key not in GENERATED_PROJECTIONS:
            raise FluxError(
                get("FLX-6210"),
                f"{prefix}emit {target!r} declares {key}: the key is read and not yet generated "
                "in this build",
            )


# ---------------------------------------------------------------------------
# Reading an entry against its contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectionTarget:
    """What the contracts say about a projected emit and the history it projects.

    ``columns`` and ``of_columns`` are the compiled columns of the projected emit and of the
    history's emit. ``start`` and ``end`` are the history's validity columns, ``history`` its
    entry and ``contract`` the history's contract document. ``granularity`` is the
    ``timeGranularity`` each time dimension of the projected emit declares.
    """

    emit: str
    of: str
    history: HistoryEntry
    columns: Mapping[str, CompiledColumn]
    of_columns: Mapping[str, CompiledColumn]
    start: Optional[str]
    end: Optional[str]
    contract: Mapping[str, Any] = field(compare=False, repr=False)
    granularity: Mapping[str, str] = field(default_factory=dict)

    def kind_of(self, column: str) -> LogicalKind:
        """The logical kind of a column of the projected emit."""
        return self.columns[column].logical_type.kind


def _granularities(expose: Mapping[str, Any]) -> Dict[str, str]:
    semantics = expose.get("semantics")
    dimensions = semantics.get("dimensions") if isinstance(semantics, Mapping) else None
    found: Dict[str, str] = {}
    for dimension in dimensions if isinstance(dimensions, Sequence) else ():
        params = dimension.get("typeParams") if isinstance(dimension, Mapping) else None
        grain = params.get("timeGranularity") if isinstance(params, Mapping) else None
        if isinstance(grain, str) and isinstance(dimension.get("name"), str):
            found[nfc(dimension["name"])] = grain
    return found


def projection_target(
    expose: Mapping[str, Any],
    history_expose: Mapping[str, Any],
    history_contract: Mapping[str, Any],
    history: HistoryEntry,
) -> ProjectionTarget:
    """What the contracts say, for the ``check_*`` functions.

    *expose* is the projected emit's ``exposes[]`` entry, *history_expose* the history emit's,
    *history_contract* the contract document that holds it and *history* its entry.
    """
    mine = compile_expose(expose)
    theirs = compile_expose(history_expose)
    ends = window_ends(history_expose.get("semantics"))
    return ProjectionTarget(
        emit=mine.expose_id,
        of=theirs.expose_id,
        history=history,
        columns={column.name: column for column in mine.columns},
        of_columns={column.name: column for column in theirs.columns},
        start=ends.get("start"),
        end=ends.get("end"),
        contract=history_contract,
        granularity=_granularities(expose),
    )


def check_projected_emit(
    key: str,
    entry: ProjectionEntry,
    *,
    histories: Sequence[HistoryEntry],
    counts: Sequence[CountsEntry],
    extension: Optional[Mapping[str, Any]],
    refusal: ErrorCode,
) -> HistoryEntry:
    """Refuse a projected emit that cannot take its rows from a history; the history entry otherwise.

    Refused with *refusal* (``FLX-3403``), naming the entry: an ``of`` that names no emit with a
    ``history`` entry or the emit itself, and a projected emit that has a ``history`` of its own,
    a ``reference`` block, a ``counts`` entry or a declared ``rows``.
    """
    at = f"{key} ({entry.emit})"
    held = {history.emit: history for history in histories}
    if entry.of == entry.emit:
        raise FluxError(refusal, f"{at}.of names the projected emit itself")
    if entry.of not in held:
        raise FluxError(
            refusal,
            f"{at}.of names {entry.of!r}, which declares no history "
            f"(emits with a history: {sorted(held)})",
        )
    for what, present in (
        ("a history of its own", entry.emit in held),
        ("a counts entry", any(item.emit == entry.emit for item in counts)),
        ("a reference block", (extension or {}).get("reference") is not None),
        ("declared rows: its rows follow from the history", "rows" in (extension or {})),
    ):
        if present:
            raise FluxError(refusal, f"{at}: the emit {entry.emit!r} is projected and has {what}")
    return held[entry.of]


def _column(
    name: str, target: ProjectionTarget, at: str, key: str, refusal: ErrorCode
) -> CompiledColumn:
    """The column *name* of the projected emit, or refuse (*refusal*), naming *at* and *key*."""
    column = target.columns.get(name)
    if column is None:
        raise FluxError(
            refusal,
            f"{at}.{key} names {name!r}, which {target.emit!r} does not declare "
            f"(columns: {sorted(target.columns)})",
        )
    return column


def _of_column(name: str, target: ProjectionTarget, at: str, key: str, refusal: ErrorCode) -> None:
    if name not in target.of_columns:
        raise FluxError(
            refusal,
            f"{at}.{key} names {name!r}, which the history's emit {target.of!r} does not declare "
            f"(columns: {sorted(target.of_columns)})",
        )


def check_snapshot(entry: SnapshotEntry, target: ProjectionTarget, *, refusal: ErrorCode) -> None:
    """Refuse (*refusal*, ``FLX-3403``) a snapshot column that is no column of the history's emit.

    Each column of the snapshot's contract is a column of the history's emit with the same name
    and the same type.
    """
    at = f"snapshots ({entry.emit})"
    for name, column in target.columns.items():
        held = target.of_columns.get(name)
        if held is None:
            raise FluxError(
                refusal,
                f"{at}: the column {name!r} of {entry.emit!r} is no column of {entry.of!r}, so "
                "a snapshot cannot hold the entity's last value",
            )
        if str(held.logical_type) != str(column.logical_type):
            raise FluxError(
                refusal,
                f"{at}: the column {name!r} is {column.logical_type} in {entry.emit!r} and "
                f"{held.logical_type} in {entry.of!r}; a snapshot column has the history's type",
            )


def _wrap_contract(target: ProjectionTarget) -> Mapping[str, Any]:
    """The history's contract, its emit gaining the projected emit's columns it lacks, for a ``when``.

    The columns resolve, so a ``when`` that reads one is refused for reading it, as ADR 0027
    says, and not for naming a column nothing declares.
    """
    extra = [name for name in target.columns if name not in target.of_columns]
    if not extra:
        return target.contract
    document = copy.deepcopy(dict(target.contract))
    for expose in document.get("exposes", ()):
        if isinstance(expose, dict) and expose.get("exposeId") == target.of:
            expose["contract"]["schema"].extend(
                {"name": name, "type": target.columns[name].declared_type, "required": False}
                for name in extra
            )
    return document


def _check_when(
    text: str,
    target: ProjectionTarget,
    row_only: Collection[str],
    at: str,
    refusal: ErrorCode,
) -> None:
    """Refuse (*refusal*) a ``when`` that reads anything but the version's own columns.

    It reads the history's tracked columns, its validity and its activation, and through
    ``previous.`` those of the version before; *row_only* is the columns that only the row
    reads, never ``previous.`` (a period column). A text that does not parse or check is refused
    as any rule's is.
    """
    from flux_engine.expr2.check import PREDICATE, check
    from flux_engine.expr2.parser import Side, parse
    from flux_engine.generator.rules import model_from_contract

    model = model_from_contract(_wrap_contract(target))
    checked = check(parse(text, side=Side.FLUX), model, target.of, position=PREDICATE)
    history = target.history
    version = {item.column for item in history.tracked}
    version.update(name for name in (target.start, target.end, history.activation) if name)
    for read in checked.reads:
        allowed = version | (set(row_only) if read.reach == "row" else set())
        if read.reach not in ("row", "previous") or read.column not in allowed:
            raise FluxError(
                refusal,
                f"{at}.when reads {read.column!r} ({read.reach}); it reads only the version's "
                f"tracked columns {sorted(version)}, its validity and activation, and through "
                "previous. those of the version before"
                + (f", and {sorted(row_only)}" if row_only else ""),
            )


def _check_instants(
    entry: EventsEntry, target: ProjectionTarget, at: str, refusal: ErrorCode
) -> None:
    kind = target.kind_of(_column(entry.date, target, at, "date", refusal).name)
    names = [("date", entry.date)] + ([("at", entry.at)] if entry.at is not None else [])
    for key, name in names:
        found = target.kind_of(_column(name, target, at, key, refusal).name)
        if found not in _UNIT_KINDS:
            raise FluxError(
                refusal, f"{at}.{key} names {name!r}, a {found.value}, not a DATE or TIMESTAMP"
            )
        if found is not kind:
            raise FluxError(
                refusal,
                f"{at}: at and date are of one type, and {name!r} is a {found.value} "
                f"beside a {kind.value}",
            )


def _check_text_column(
    name: Optional[str], target: ProjectionTarget, at: str, key: str, refusal: ErrorCode
) -> None:
    if name is None:
        return
    column = _column(name, target, at, key, refusal)
    if column.logical_type.kind is not LogicalKind.STRING:
        raise FluxError(
            refusal, f"{at}.{key} names {name!r}, a {column.logical_type}, not a text column"
        )


def _check_type(entry: EventsEntry, target: ProjectionTarget, at: str, refusal: ErrorCode) -> None:
    """Refuse a ``type`` that names no text for a kept op, a reason or an ending."""
    if entry.type_column is None:
        return
    _check_text_column(entry.type_column, target, at, "type.column", refusal)
    values = dict(entry.type_values)
    history = target.history
    tracked = [item.column for item in history.tracked]
    ending = history.end is not None and history.end.column is not None
    known = {"c", "d", "end", *tracked}
    stray = sorted(set(values) - known)
    if stray:
        raise FluxError(
            refusal,
            f"{at}.type.values names {stray[0]!r}; it holds c, d, end and the tracked columns "
            f"{tracked}",
        )
    needed: List[str] = [op for op in ("c", "d") if op in entry.on]
    needed += tracked if "u" in entry.on else []
    needed += ["end"] if ending else []
    missing = [name for name in needed if name not in values]
    if missing:
        raise FluxError(
            refusal,
            f"{at}.type.values has no text for {missing[0]!r}: every kept op, every reason and "
            "the end need one",
        )


def _check_mapped(
    mapped: Tuple[Tuple[str, str], ...],
    key: str,
    keeps: str,
    entry: EventsEntry,
    target: ProjectionTarget,
    at: str,
    refusal: ErrorCode,
) -> None:
    """Refuse a ``before`` or ``after`` column that names no column, or must be NULL on a kept op."""
    for name, held in mapped:
        column = _column(name, target, at, key, refusal)
        _of_column(held, target, at, f"{key}.{name}", refusal)
        if column.required and keeps in entry.on:
            raise FluxError(
                refusal,
                f"{at}.{key} maps {name!r}, which {target.emit!r} requires, and the events "
                f"keep {keeps!r}, which write NULL there",
            )


def check_events(entry: EventsEntry, target: ProjectionTarget, *, refusal: ErrorCode) -> None:
    """Read *entry* against the contracts, or refuse with *refusal* (``FLX-3403``).

    Refused, each message naming the entry and the key: a ``date``, ``at``, ``op`` or ``type``
    column the emit lacks or of the wrong type (``at`` and ``date`` are one of DATE and TIMESTAMP);
    a kept op, a reason or the end that ``type`` gives no text; a ``before`` or ``after`` that names
    no column of the emit or of the history's emit; a required ``before`` column when ``on``
    keeps ``c`` and a required ``after`` column when it keeps ``d``; and a ``when`` that reads
    anything but the version's tracked columns, validity and activation.
    """
    at = f"events ({entry.emit})"
    _check_instants(entry, target, at, refusal)
    _check_text_column(entry.op, target, at, "op", refusal)
    _check_type(entry, target, at, refusal)
    _check_mapped(entry.before, "before", "c", entry, target, at, refusal)
    _check_mapped(entry.after, "after", "d", entry, target, at, refusal)
    if entry.when is not None:
        _check_when(entry.when, target, (), at, refusal)


def _grain_of(entry: PeriodsEntry, target: ProjectionTarget, at: str, refusal: ErrorCode) -> str:
    grain = entry.grain if entry.grain is not None else target.granularity.get(entry.column)
    if grain is None:
        raise FluxError(
            refusal,
            f"{at}.grain is required: {entry.column!r} declares no timeGranularity to default to",
        )
    if grain not in _GRAINS:
        raise FluxError(
            refusal,
            f"{at}.grain is one of {list(_GRAINS)}; the timeGranularity of {entry.column!r} is "
            f"{grain!r}",
        )
    return grain


def _prefix(text: str) -> str:
    """*text* quoted, cut to a short prefix when it is long."""
    return repr(text) if len(text) <= _MOST_TEXT else f"{text[:16]!r}... ({len(text)} characters)"


def _reads_as(text: str, column: CompiledColumn) -> bool:
    """Whether *text* reads as a value of *column*'s type."""
    kind = column.logical_type.kind
    if kind is LogicalKind.STRING:
        return not column.enum_values or text in column.enum_values
    if kind is LogicalKind.INT:
        return (
            len(text) <= _MOST_TEXT and _INTEGER.fullmatch(text) is not None and _fits_int64(text)
        )
    if kind is LogicalKind.BOOL:
        return text in ("true", "false")
    if kind in _UNIT_KINDS:
        return _instant(text, kind) is not None
    if kind is LogicalKind.DECIMAL:
        return _reads_as_decimal(text, column)
    return False  # a JSON override never generates, so it is refused here


def _fits_int64(text: str) -> bool:
    return -(1 << 63) <= int(text) < (1 << 63)


def _reads_as_decimal(text: str, column: CompiledColumn) -> bool:
    """Whether *text* is a finite decimal that fits the column's precision and scale."""
    try:
        value = Decimal(text)
    except InvalidOperation:
        return False
    if not value.is_finite():
        return False
    scale = column.logical_type.scale or 0
    precision = column.logical_type.precision or 38
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -scale:
        return False
    return value == 0 or value.adjusted() + 1 <= precision - scale


def _check_kind(
    kind: PeriodKind, entry: PeriodsEntry, target: ProjectionTarget, at: str, refusal: ErrorCode
) -> None:
    for name, text in kind.overrides:
        column = _column(name, target, at, "overrides", refusal)
        if name == entry.column:
            raise FluxError(refusal, f"{at}.overrides names the period column {name!r}")
        if not _reads_as(text, column):
            raise FluxError(
                refusal,
                f"{at}.overrides.{name} {_prefix(text)} does not read as a {column.logical_type}, "
                f"the type of {name!r}",
            )
    if kind.when is not None:
        _check_when(kind.when, target, (entry.column,), at, refusal)


def check_periods(
    entry: PeriodsEntry, target: ProjectionTarget, *, refusal: ErrorCode
) -> PeriodsEntry:
    """Read *entry* against the contracts, or refuse with *refusal*; the entry with its grain written.

    Refused (``FLX-3403``), each message naming the entry and the key: a ``column`` the emit lacks,
    that is not a DATE or that the history's emit also declares; a ``grain`` that neither the
    entry nor the column's ``timeGranularity`` gives, or that is not ``day``, ``week``, ``month``,
    ``quarter`` or ``year``; an ``overrides`` that names no column of the emit or is no value of
    its column's type; and a kind's ``when`` that reads anything but the version's tracked
    columns, validity and activation, and the period column. The entry returned has its grain
    written, so the contract's default and an explicit grain bind alike.
    """
    at = f"periods ({entry.emit})"
    column = _column(entry.column, target, at, "column", refusal)
    if column.logical_type.kind is not LogicalKind.DATE:
        raise FluxError(
            refusal, f"{at}.column names {entry.column!r}, a {column.logical_type}, not a DATE"
        )
    if entry.column in target.of_columns:
        raise FluxError(
            refusal,
            f"{at}.column {entry.column!r} is also a column of {entry.of!r}: a period's first "
            "day cannot be a version's column",
        )
    grain = _grain_of(entry, target, at, refusal)
    for index, kind in enumerate(entry.kinds):
        _check_kind(kind, entry, target, f"{at}.kinds[{index}]", refusal)
    return replace(entry, grain=grain)
