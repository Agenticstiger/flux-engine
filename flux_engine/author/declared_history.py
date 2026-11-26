# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The file ``fluxsim init --history FILE`` reads: a declared history, its projections and the window.

The file is one YAML document, a mapping, read as an overlay file is
(:func:`flux_engine.seam.overlay.read_overlay_file`): a regular file within the size a bundle file
may have, UTF-8, exactly one document. Its keys are a closed set: ``history`` (required, a
non-empty list), ``snapshots``, ``events``, ``periods`` and ``asOf`` (lists) and ``timeBounds``
(``start`` and ``end``, read as ``--window`` is). The entries are written as the Simulation's
namespace holds them (ADR 0017 for ``history``, ADR 0027 for the projections and ``asOf``), so the
file is the bundle's own spelling and the plan checks the entries themselves.

:func:`read_history_file` reads it. :func:`check_entries` holds each entry's ``emit`` and ``of`` to
the exposes the bundle writes, and what the bundle writes around the entries (the counted layouts
it leaves out, the columns it exempts from a range, the weight of an ended status) is read from
the entries by the other functions here.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

from flux_engine.author.window import WINDOW_END_LIMIT_MS, WindowError, instant_ms, parse_window
from flux_engine.core.errors import FluxError
from flux_engine.seam.coverage import printable

__all__ = [
    "HISTORY_FILE_KEYS",
    "DeclaredHistory",
    "DeclaredHistoryError",
    "check_entries",
    "declared_spans",
    "parent_relationships",
    "projected_emits",
    "read_history_file",
]

#: The keys of a history file; every other key is refused.
HISTORY_FILE_KEYS = ("history", "snapshots", "events", "periods", "asOf", "timeBounds")

_LISTS = ("snapshots", "events", "periods", "asOf")
_PROJECTIONS = ("snapshots", "events", "periods")


class DeclaredHistoryError(Exception):
    """The history file, or an entry of it, cannot be used; the message names the file."""


@dataclass(frozen=True)
class DeclaredHistory:
    """The contents of one history file, as the namespace holds them."""

    #: The file's name, printable.
    name: str
    history: Tuple[Mapping[str, Any], ...]
    snapshots: Tuple[Mapping[str, Any], ...] = ()
    events: Tuple[Mapping[str, Any], ...] = ()
    periods: Tuple[Mapping[str, Any], ...] = ()
    #: The ``asOf`` entries when the file has the key, else ``None`` (init writes its own).
    as_of: Optional[Tuple[Mapping[str, Any], ...]] = None
    #: The window the file gives, RFC 3339 texts, or ``None``.
    time_bounds: Optional[Tuple[str, str]] = None

    def history_entries(self) -> List[Dict[str, Any]]:
        """A copy of the ``history`` entries, as given."""
        return [copy.deepcopy(dict(entry)) for entry in self.history]

    def projection_entries(self, key: str) -> List[Dict[str, Any]]:
        """A copy of the ``snapshots``, ``events`` or ``periods`` entries, as given."""
        return [copy.deepcopy(dict(entry)) for entry in getattr(self, key)]

    def as_of_entries(self) -> Optional[List[Dict[str, Any]]]:
        """A copy of the ``asOf`` entries, or ``None`` when the file gives none."""
        if self.as_of is None:
            return None
        return [copy.deepcopy(dict(entry)) for entry in self.as_of]


def _plain(value: Any) -> Any:
    """*value* with each exact decimal as its text, which is how a namespace spells a weight."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _entries(
    document: Mapping[str, Any], key: str, name: str, *, required: bool
) -> Tuple[Mapping[str, Any], ...]:
    value = document.get(key)
    if key not in document:
        return ()
    if not isinstance(value, list) or (required and not value):
        raise DeclaredHistoryError(
            f"{name}: {key} is "
            + ("a non-empty list of entries" if required else "a list of entries")
            + (", and this is empty" if isinstance(value, list) and not value else "")
        )
    for position, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise DeclaredHistoryError(f"{name}: {key}[{position}] is not a mapping")
        if key != "asOf" and not isinstance(entry.get("emit"), str):
            raise DeclaredHistoryError(f"{name}: {key}[{position}].emit is not the name of an emit")
        if key in _PROJECTIONS and not isinstance(entry.get("of"), str):
            raise DeclaredHistoryError(
                f"{name}: {key}[{position}].of is not the name of a history's emit"
            )
    return tuple(value)


def _time_bounds(document: Mapping[str, Any], name: str) -> Optional[Tuple[str, str]]:
    if "timeBounds" not in document:
        return None
    bounds = document["timeBounds"]
    if not isinstance(bounds, dict) or set(bounds) != {"start", "end"}:
        raise DeclaredHistoryError(f"{name}: timeBounds holds start and end, and nothing else")
    try:
        return parse_window(bounds["start"], bounds["end"])
    except WindowError as error:
        raise DeclaredHistoryError(
            f"{name}: timeBounds: " + str(error).replace("--window ", "")
        ) from None


def read_history_file(path: Path) -> DeclaredHistory:
    """The history file at *path*, or :class:`DeclaredHistoryError` naming it.

    A file that is not a regular file, is past the size a bundle file may have, is not UTF-8, holds
    no document or several, or holds one that is not a mapping of the keys above is refused. An
    entry that is not a mapping, or has no ``emit`` text (and ``of`` text, for a projection), is
    refused naming its position, as ``history[0].emit``.
    """
    from flux_engine.seam.resolve import read_bundle_file
    from flux_stats.exact_yaml import load_documents

    name = printable(Path(path).name)
    try:
        data = read_bundle_file(Path(path))
    except FluxError as error:
        raise DeclaredHistoryError(printable(error.detail or str(error))) from None
    except OSError as error:
        raise DeclaredHistoryError(f"cannot read {name}: {error.strerror or error}") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise DeclaredHistoryError(f"{name}: it is not UTF-8") from None
    try:
        documents = load_documents(text, name=name)
    except FluxError as error:
        raise DeclaredHistoryError(
            f"{name}: it is not read as YAML: {printable(error.detail or str(error))}"
        ) from None
    if len(documents) != 1:
        raise DeclaredHistoryError(
            f"{name}: a history file holds one document, and this holds {len(documents)}"
        )
    document = _plain(documents[0])
    if not isinstance(document, dict):
        raise DeclaredHistoryError(f"{name}: a history file holds a mapping")
    for key in document:
        if key not in HISTORY_FILE_KEYS:
            raise DeclaredHistoryError(
                f"{name}: {printable(str(key))} is no key of a history file "
                f"({', '.join(HISTORY_FILE_KEYS)})"
            )
    if "history" not in document:
        raise DeclaredHistoryError(f"{name}: a history file holds history, a non-empty list")
    lists = {key: _entries(document, key, name, required=False) for key in _LISTS}
    history = _entries(document, "history", name, required=True)
    _refuse_the_year_9999(history, name)
    return DeclaredHistory(
        name=name,
        history=history,
        snapshots=lists["snapshots"],
        events=lists["events"],
        periods=lists["periods"],
        as_of=lists["asOf"] if "asOf" in document else None,
        time_bounds=_time_bounds(document, name),
    )


def _refuse_the_year_9999(history: Tuple[Mapping[str, Any], ...], name: str) -> None:
    """Refuse an entry's ``until`` or ``start.bins`` edge at or after :data:`WINDOW_END_LIMIT_MS`.

    The year 9999 spells an open end, which gives a run's window nothing, as in a declared range;
    the window would end there to hold the history, and a run's periods step past the end of its
    window, into a year no date holds. Each value is refused naming its place, as
    ``history[0].until``. A value that reads as no instant is left to the plan's check.
    """
    for position, entry in enumerate(history):
        start = entry.get("start")
        bins = start.get("bins") if isinstance(start, Mapping) else None
        places = [
            (f"start.bins[{index}]", edge)
            for index, edge in enumerate(bins if isinstance(bins, list) else ())
        ]
        places.append(("until", entry.get("until")))
        for place, value in places:
            found = instant_ms(value)
            if found is not None and found >= WINDOW_END_LIMIT_MS:
                raise DeclaredHistoryError(
                    f"{name}: history[{position}].{place} {printable(str(value))!r} is in the year "
                    "9999, which spells an open end rather than an instant the history runs to; "
                    "give the instant its versions are written up to"
                )


def projected_emits(declared: DeclaredHistory) -> FrozenSet[str]:
    """The emits the file's snapshots, events and periods name."""
    return frozenset(str(entry["emit"]) for key in _PROJECTIONS for entry in getattr(declared, key))


def parent_relationships(declared: DeclaredHistory) -> FrozenSet[Tuple[str, str]]:
    """Each ``(emit, relationship)`` a declared history's ``parent`` lays its entities out over."""
    found: Set[Tuple[str, str]] = set()
    for entry in declared.history:
        parent = entry.get("parent")
        if isinstance(parent, Mapping) and isinstance(parent.get("relationship"), str):
            found.add((str(entry["emit"]), str(parent["relationship"])))
    return frozenset(found)


def declared_spans(declared: DeclaredHistory) -> List[Tuple[int, int]]:
    """The spans (milliseconds) the histories' own dates give the run's window.

    Each ``start.bins`` edge and each ``until`` that is a date or an RFC 3339 timestamp; an edge
    that reads as neither is left to the plan's check.
    """
    spans: List[Tuple[int, int]] = []
    for entry in declared.history:
        edges: List[int] = []
        start = entry.get("start")
        bins = start.get("bins") if isinstance(start, Mapping) else None
        for edge in bins if isinstance(bins, list) else ():
            found = instant_ms(edge)
            if found is not None:
                edges.append(found)
        until = instant_ms(entry.get("until"))
        if until is not None:
            edges.append(until)
        if edges:
            spans.append((min(edges), max(edges)))
    return spans


def check_entries(declared: DeclaredHistory, written: Set[str], versioned: Set[str]) -> None:
    """Hold each entry's ``emit`` and ``of`` to the exposes the bundle writes.

    *written* is the exposes init writes (not left out, not a reference table), *versioned* those
    among them whose contract declares a validity window. A history's ``emit`` must be versioned;
    a projection's ``emit`` must be written and no history's, and its ``of`` a declared history's
    ``emit``. Raises :class:`DeclaredHistoryError` naming the file and the entry.
    """
    histories = {str(entry["emit"]) for entry in declared.history}
    for position, entry in enumerate(declared.history):
        emit = str(entry["emit"])
        if emit not in written:
            raise DeclaredHistoryError(
                f"{declared.name}: history[{position}].emit {printable(emit)!r} names no expose "
                "init writes (it is not in the contract, is left out, or is a reference table)"
            )
        if emit not in versioned:
            raise DeclaredHistoryError(
                f"{declared.name}: history[{position}].emit {printable(emit)!r} names an expose "
                "whose contract declares no validity window"
            )
    for key in _PROJECTIONS:
        for position, entry in enumerate(getattr(declared, key)):
            emit, of = str(entry["emit"]), str(entry["of"])
            where = f"{declared.name}: {key}[{position}]"
            if emit not in written:
                raise DeclaredHistoryError(
                    f"{where}.emit {printable(emit)!r} names no expose init writes (it is not in "
                    "the contract, is left out, or is a reference table)"
                )
            if emit in histories:
                raise DeclaredHistoryError(
                    f"{where}.emit {printable(emit)!r} names a history's emit, which is not "
                    "projected from a history"
                )
            if of not in histories:
                raise DeclaredHistoryError(
                    f"{where}.of {printable(of)!r} names no emit the file declares a history for"
                )
