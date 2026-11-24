# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The histories a Simulation declares: an entity is a chain of versions (ADR 0017 sections 1 and 2).

A Simulation declares them under the top-level ``history`` key of its namespace, in the FLUX
bundle and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        history:
          - emit: subscription_version
            entities: 1000
            versions: { bins: ["1", "2", "4", "9"], binWeights: ["0.45", "0.35", "0.20"] }
            length: { bins: ["30", "90", "365"], binWeights: ["0.4", "0.6"] }
            until: "2025-12-31"
            tracked:
              - column: plan
                transitions: { basic: { pro: "1" }, pro: { basic: "1" } }

An entry names an emitted expose whose contract declares a validity window (one natural entity
and a start and an end, both DATE or both TIMESTAMP) and lays its rows out as chains of versions.
The entity, the start and the end are read from the contract and never repeated here.

Two readers share this module. :func:`read_history_block` reads what the entry says, with no
contract in hand: an unknown key at any level is the reader's *code* (``FLX-3105``), and every
other malformed value is its *refusal* code (``FLX-3403``), each message naming the entry and the
key. :func:`check_history` reads the entry against what the contract and the Simulation declare
for its emit (:class:`HistoryTarget`): the window, the relationship, the dates and the columns.

**An entry changes the rows the emit generates**, so it is bound into ``H`` before any row is
drawn: :func:`bound_into_h` is the member the emit's entry for ``H`` gains, present only when the
emit has an entry, so a bundle that declares none hashes exactly as before. ``G`` leaves the
member out, as it leaves out the counts' (:func:`flux_engine.seam.emit.seam_reusable_digest`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Any, Collection, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import ErrorCode, FluxError, get
from flux_engine.seam.conform import DATE_PATTERN
from flux_engine.seam.contract import LogicalKind, compile_expose, declared_enum_values
from flux_engine.seam.counts import ChildShape, CountsEntry, _minimum, _shape
from flux_engine.seam.dates import LifetimeEntry, WithinEntry
from flux_engine.seam.hidden import HiddenColumn, with_hidden_columns
from flux_engine.seam.keys import ExposeKeys, Relationship, window_ends
from flux_engine.seam.links import LinkEntry

__all__ = [
    "HISTORY_KEY",
    "Histogram",
    "HistoryEnd",
    "HistoryEntry",
    "HistoryParent",
    "HistoryTarget",
    "HistoryTracked",
    "bound_into_h",
    "check_history",
    "check_history_dates",
    "check_history_sources",
    "history_entries_of",
    "history_target",
    "read_history_block",
]

#: The top-level key of the namespace that declares histories, and the member an emit's ``H``
#: gains.
HISTORY_KEY = "history"

_ENTRY_KEYS = (
    "emit",
    "entities",
    "parent",
    "versions",
    "start",
    "length",
    "until",
    "chain",
    "openEnd",
    "activation",
    "tracked",
    "reasons",
    "end",
)
_PARENT_KEYS = ("relationship", "minChildren", "parentWhen", "children", "lifetime")
_HISTOGRAM_KEYS = ("bins", "binWeights", "closed")
_LENGTH_REFUSED = ("mean", "given", "states", "otherwise", "open")
_LEVEL_KEYS = ("levels", "levelWeights")
_BIN_KEYS = ("bins", "binWeights")
_TRACKED_KEYS = ("column", "initial", "transitions")
_END_KEYS = ("mode", "share", "column", "value")
_CHAINS = ("nextStart", "dayBefore")
_MODES = ("closeLast", "finalVersion")
_CLOSED = ("lower", "upper")
_MOST_ENTRIES = 4096
_MOST_ENTITIES = 1 << 62
_MOST_CLASSES = 4096
_DECIMAL = re.compile(r"\d+(\.\d+)?")
#: The most characters a weight or a bin edge may have.
_MOST_TEXT = 64
_DAY_MS = 86_400_000
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_UNIT_KINDS = (LogicalKind.DATE, LogicalKind.TIMESTAMP)


@dataclass(frozen=True)
class Histogram:
    """A histogram as written: bin edges and one weight per bin, as texts, with ``closed`` if given."""

    edges: Tuple[str, ...]
    weights: Tuple[str, ...]
    closed: Optional[str] = None

    def as_member(self) -> Dict[str, Any]:
        """The histogram as the entry holds it: ``{bins, binWeights}`` and ``closed`` when declared."""
        member: Dict[str, Any] = {"bins": list(self.edges), "binWeights": list(self.weights)}
        if self.closed is not None:
            member["closed"] = self.closed
        return member


@dataclass(frozen=True)
class HistoryParent:
    """The relationship whose parent holds each entity, with the keys a ``counts`` entry reads."""

    relationship: str
    min_children: Optional[int] = None
    parent_when: Optional[str] = None
    children: Optional[ChildShape] = None
    lifetime: bool = False

    def as_member(self) -> Dict[str, Any]:
        """The parent as the entry holds it, each key only when declared."""
        member: Dict[str, Any] = {"relationship": self.relationship}
        if self.min_children is not None:
            member["minChildren"] = self.min_children
        if self.parent_when is not None:
            member["parentWhen"] = self.parent_when
        if self.children is not None:
            member["children"] = self.children.as_member()
        if self.lifetime:
            member["lifetime"] = True
        return member


@dataclass(frozen=True)
class HistoryTracked:
    """A column that changes between versions: its initial levels and its transition rows."""

    column: str
    transitions: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]
    initial: Optional[ChildShape] = None

    def as_member(self) -> Dict[str, Any]:
        """The tracked column as the entry holds it."""
        member: Dict[str, Any] = {"column": self.column}
        if self.initial is not None:
            member["initial"] = self.initial.as_member()
        member["transitions"] = {level: dict(row) for level, row in self.transitions}
        return member


@dataclass(frozen=True)
class HistoryEnd:
    """How an entity's history ends: a ``mode``, a ``share`` and, optionally, a column and its value."""

    mode: str
    share: str
    column: Optional[str] = None
    value: Optional[str] = None

    def as_member(self) -> Dict[str, Any]:
        """The end as the entry holds it, each key only when declared."""
        member: Dict[str, Any] = {"mode": self.mode, "share": self.share}
        if self.column is not None:
            member["column"] = self.column
        if self.value is not None:
            member["value"] = self.value
        return member


@dataclass(frozen=True)
class HistoryEntry:
    """One emit's history, as declared."""

    emit: str
    entities: int
    length: Histogram
    until: str
    parent: Optional[HistoryParent] = None
    versions: Optional[ChildShape] = None
    start: Optional[Histogram] = None
    chain: str = "nextStart"
    open_end: Optional[str] = None
    activation: Optional[str] = None
    tracked: Tuple[HistoryTracked, ...] = ()
    reasons: Optional[ChildShape] = None
    end: Optional[HistoryEnd] = None

    def as_member(self) -> Dict[str, Any]:
        """The entry as the emit's ``history`` member holds it: no ``emit``, no absent key."""
        member: Dict[str, Any] = {"entities": self.entities}
        if self.parent is not None:
            member["parent"] = self.parent.as_member()
        if self.versions is not None:
            member["versions"] = self.versions.as_member()
        if self.start is not None:
            member["start"] = self.start.as_member()
        member["length"] = self.length.as_member()
        member["until"] = self.until
        member["chain"] = self.chain
        if self.open_end is not None:
            member["openEnd"] = self.open_end
        if self.activation is not None:
            member["activation"] = self.activation
        if self.tracked:
            member["tracked"] = [tracked.as_member() for tracked in self.tracked]
        if self.reasons is not None:
            member["reasons"] = self.reasons.as_member()
        if self.end is not None:
            member["end"] = self.end.as_member()
        return member


# ---------------------------------------------------------------------------
# Reading what the entry says
# ---------------------------------------------------------------------------


def _unknown(raw: Mapping[str, Any], allowed: Sequence[str], at: str, code: ErrorCode) -> None:
    """Refuse (*code*) a key of *raw* that is not in *allowed*."""
    unknown = sorted(str(key) for key in raw if str(key) not in allowed)
    if unknown:
        raise FluxError(code, f"{at} declares {unknown}; it carries {list(allowed)}")


def _mapping(raw: Any, at: str, what: str, code: ErrorCode) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise FluxError(code, f"{at} is {what}")
    return raw


def _text(raw: Mapping[str, Any], key: str, at: str, refusal: ErrorCode) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise FluxError(refusal, f"{at}.{key} is a non-empty string")
    return nfc(value)


def _whole(raw: Any) -> Optional[int]:
    """A whole number written as an integer, as decimal text or as an integral decimal."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 19:
        return int(raw)
    if isinstance(raw, Decimal) and raw.is_finite() and raw == raw.to_integral_value():
        return int(raw)
    return None


def _decimal(raw: Any) -> Optional[str]:
    """A non-negative decimal as text: decimal text, an integer or a decimal; never a float."""
    if isinstance(raw, str):
        return raw if _DECIMAL.fullmatch(raw) and len(raw) <= _MOST_TEXT else None
    if isinstance(raw, bool) or isinstance(raw, float):
        return None
    if isinstance(raw, int):
        return str(raw) if 0 <= raw < _MOST_ENTITIES else None
    if isinstance(raw, Decimal) and raw.is_finite() and raw >= 0:
        text = format(raw, "f")
        return text if len(text) <= _MOST_TEXT else None
    return None


def _bounded(text: str, what: str, refusal: ErrorCode) -> None:
    """Refuse (*refusal*) a number's *text* longer than 64 characters, quoting a short prefix."""
    if len(text) > _MOST_TEXT:
        raise FluxError(
            refusal, f"{what} of {len(text)} characters, {text[:16]!r}...: at most {_MOST_TEXT}"
        )


def _weights(
    weights: Sequence[str], classes: int, at: str, refusal: ErrorCode, *, what: str
) -> None:
    """Refuse (*refusal*) weights that are not *classes* decimals of 0 or more with a positive total."""
    if len(weights) != classes:
        raise FluxError(
            refusal, f"{at} holds {len(weights)} weights; {what} needs {classes}, one for each"
        )
    for text in weights:
        _bounded(text, f"{at} holds a weight", refusal)
    parsed = [Fraction(text) if _DECIMAL.fullmatch(text) else None for text in weights]
    if any(value is None for value in parsed):
        raise FluxError(refusal, f"{at} holds a weight that is not a decimal such as '0.25'")
    if sum(value for value in parsed if value is not None) <= 0:
        raise FluxError(refusal, f"{at} weights total zero")


def _texts(raw: Any, at: str, refusal: ErrorCode) -> Tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise FluxError(refusal, f"{at} is a non-empty list of strings")
    if len(raw) > _MOST_CLASSES:
        raise FluxError(refusal, f"{at} holds more than {_MOST_CLASSES} entries")
    if not all(isinstance(item, str) and item for item in raw):
        raise FluxError(refusal, f"{at} holds an entry that is not a string: write each as '2'")
    return tuple(raw)


def _levels(raw: Any, at: str, code: ErrorCode, refusal: ErrorCode) -> ChildShape:
    """A ``{levels, levelWeights}`` mapping with one weight per level."""
    mapping = _mapping(raw, at, "a mapping of levels and levelWeights", refusal)
    _unknown(mapping, _LEVEL_KEYS, at, code)
    shape = _shape(mapping, at, refusal)
    if shape.kind != "levels":
        raise FluxError(refusal, f"{at} is a mapping of levels and levelWeights")
    _weights(shape.weights, len(shape.values), f"{at}.levelWeights", refusal, what="the levels")
    return shape


def _count_shape(raw: Any, at: str, code: ErrorCode, refusal: ErrorCode, least: int) -> ChildShape:
    """A shape over whole counts of at least *least*: ``levels`` or ascending ``bins``, weighted."""
    mapping = _mapping(
        raw, at, "a mapping of levels and levelWeights, or of bins and binWeights", refusal
    )
    _unknown(mapping, _LEVEL_KEYS + _BIN_KEYS, at, code)
    shape = _shape(mapping, at, refusal)
    counts = [_whole(text) for text in shape.values]
    if any(count is None for count in counts):
        raise FluxError(
            refusal, f"{at} holds a class that is not a whole number: write each as '2'"
        )
    values = [count for count in counts if count is not None]
    if any(count < least for count in values):
        raise FluxError(refusal, f"{at} holds a class below {least}")
    if shape.kind == "bins":
        if len(values) < 2 or any(
            low >= high for low, high in zip(values, values[1:], strict=False)
        ):
            raise FluxError(refusal, f"{at}.bins ascend and hold at least two edges")
        classes = len(values) - 1
    else:
        classes = len(values)
    key = "binWeights" if shape.kind == "bins" else "levelWeights"
    _weights(shape.weights, classes, f"{at}.{key}", refusal, what="the classes")
    return shape


def _histogram(
    raw: Any, at: str, code: ErrorCode, refusal: ErrorCode, *, length: bool
) -> Histogram:
    """A ``{bins, binWeights, closed}`` histogram; a duration's weight for ``open`` is refused."""
    mapping = _mapping(raw, at, "a mapping of bins and binWeights", refusal)
    if length:
        spelled = [key for key in _LENGTH_REFUSED if key in mapping]
        if spelled:
            raise FluxError(
                code,
                f"{at}.{spelled[0]} is not read: a length is {{bins, binWeights, closed}}, "
                "and whether a version ends is the chain's to say, not the draw's",
            )
    _unknown(mapping, _HISTOGRAM_KEYS, at, code)
    if set(_BIN_KEYS) - {str(key) for key in mapping}:
        raise FluxError(refusal, f"{at} carries {list(_BIN_KEYS)}")
    edges = _texts(mapping["bins"], f"{at}.bins", refusal)
    weights = _texts(mapping["binWeights"], f"{at}.binWeights", refusal)
    if len(edges) < 2:
        raise FluxError(refusal, f"{at}.bins hold at least two edges")
    _weights(weights, len(edges) - 1, f"{at}.binWeights", refusal, what="the bins")
    closed = mapping.get("closed")
    if "closed" in mapping and closed not in _CLOSED:
        raise FluxError(refusal, f"{at}.closed is one of {list(_CLOSED)}")
    if length:
        _length_edges(edges, at, refusal)
    return Histogram(edges=edges, weights=weights, closed=closed)


def _length_edges(edges: Sequence[str], at: str, refusal: ErrorCode) -> None:
    """Refuse a ``length`` bin that is not a number, is below one day, or does not ascend."""
    values: List[Fraction] = []
    for text in edges:
        _bounded(text, f"{at}.bins holds an edge", refusal)
        if not _DECIMAL.fullmatch(text):
            raise FluxError(refusal, f"{at}.bins holds {text!r}, which is not a number of days")
        values.append(Fraction(text))
    if any(value < 1 for value in values):
        raise FluxError(
            refusal, f"{at}.bins holds a bin below one day: a version lasts a day or more"
        )
    if any(low >= high for low, high in zip(values, values[1:], strict=False)):
        raise FluxError(refusal, f"{at}.bins ascend")


def _entities(raw: Any, at: str, refusal: ErrorCode) -> int:
    count = _whole(raw)
    if count is None or not 0 < count <= _MOST_ENTITIES:
        raise FluxError(refusal, f"{at}.entities is a whole number above zero, such as 1000")
    return count


def _parent(raw: Any, at: str, code: ErrorCode, refusal: ErrorCode) -> HistoryParent:
    mapping = _mapping(raw, at, "a mapping with a relationship", refusal)
    _unknown(mapping, _PARENT_KEYS, at, code)
    relationship = _text(mapping, "relationship", at, refusal)
    minimum = _minimum(mapping["minChildren"], at, refusal) if "minChildren" in mapping else None
    condition = mapping.get("parentWhen")
    if "parentWhen" in mapping and (not isinstance(condition, str) or not condition.strip()):
        raise FluxError(refusal, f"{at}.parentWhen is a FLUID SQL/1 predicate text")
    children: Optional[ChildShape] = None
    if "children" in mapping:
        shape = _mapping(
            mapping["children"], f"{at}.children", "a mapping of levels or bins", refusal
        )
        _unknown(shape, _LEVEL_KEYS + _BIN_KEYS, f"{at}.children", code)
        children = _shape(shape, f"{at}.children", refusal)
    lifetime = mapping.get("lifetime", False)
    if not isinstance(lifetime, bool):
        raise FluxError(refusal, f"{at}.lifetime is true or false")
    return HistoryParent(
        relationship=relationship,
        min_children=minimum,
        parent_when=nfc(condition) if isinstance(condition, str) else None,
        children=children,
        lifetime=lifetime,
    )


def _transitions(
    raw: Any, at: str, refusal: ErrorCode
) -> Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]:
    """A ``transitions`` mapping as rows of ``(level, ((level, weight), ...))``, in declared order."""
    if not isinstance(raw, Mapping) or not raw:
        raise FluxError(
            refusal, f"{at} is a non-empty mapping of a level to the weights it moves by"
        )
    rows: List[Tuple[str, Tuple[Tuple[str, str], ...]]] = []
    for level, row in raw.items():
        where = f"{at}.{level}"
        if not isinstance(row, Mapping) or not row:
            raise FluxError(refusal, f"{where} is a non-empty mapping of levels to weights")
        moves: List[Tuple[str, str]] = []
        for target, weight in row.items():
            text = _decimal(weight)
            if text is None:
                raise FluxError(refusal, f"{where}.{target} is a weight such as '0.5'")
            if str(target) == str(level):
                raise FluxError(
                    refusal, f"{where} moves {level!r} to itself; a change always moves"
                )
            moves.append((nfc(str(target)), text))
        _weights([text for _, text in moves], len(moves), where, refusal, what="the row")
        rows.append((nfc(str(level)), tuple(moves)))
    return tuple(rows)


def _tracked(raw: Any, at: str, code: ErrorCode, refusal: ErrorCode) -> Tuple[HistoryTracked, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise FluxError(refusal, f"{at} is a non-empty list of {{column, transitions}} entries")
    found: List[HistoryTracked] = []
    for index, item in enumerate(raw):
        where = f"{at}[{index}]"
        mapping = _mapping(item, where, "a mapping with a column and transitions", refusal)
        _unknown(mapping, _TRACKED_KEYS, where, code)
        column = _text(mapping, "column", where, refusal)
        if any(seen.column == column for seen in found):
            raise FluxError(refusal, f"{where}.column {column!r} is tracked twice")
        initial = (
            _levels(mapping["initial"], f"{where}.initial", code, refusal)
            if "initial" in mapping
            else None
        )
        found.append(
            HistoryTracked(
                column=column,
                transitions=_transitions(
                    mapping.get("transitions"), f"{where}.transitions", refusal
                ),
                initial=initial,
            )
        )
    return tuple(found)


def _end(raw: Any, at: str, code: ErrorCode, refusal: ErrorCode) -> HistoryEnd:
    mapping = _mapping(raw, at, "a mapping of mode, share, column and value", refusal)
    _unknown(mapping, _END_KEYS, at, code)
    mode = mapping.get("mode")
    if mode not in _MODES:
        raise FluxError(refusal, f"{at}.mode is one of {list(_MODES)}, not {mode!r}")
    share = _decimal(mapping.get("share"))
    if share is None or Fraction(share) > 1:
        raise FluxError(refusal, f"{at}.share is a decimal from 0 to 1, such as '0.12'")
    column = _text(mapping, "column", at, refusal) if "column" in mapping else None
    value = _text(mapping, "value", at, refusal) if "value" in mapping else None
    if mode == "finalVersion" and column is None:
        raise FluxError(
            refusal, f"{at}.column is required by the finalVersion mode, which writes it"
        )
    if (column is None) != (value is None):
        raise FluxError(
            refusal,
            f"{at} gives column and value together: the ending version takes one in the other",
        )
    return HistoryEnd(mode=str(mode), share=share, column=column, value=value)


def _optional_text(
    entry: Mapping[str, Any], key: str, at: str, refusal: ErrorCode
) -> Optional[str]:
    if key not in entry:
        return None
    value = entry[key]
    if not isinstance(value, str) or not value:
        raise FluxError(refusal, f"{at}.{key} is a non-empty string")
    return nfc(value)


def _chain(entry: Mapping[str, Any], at: str, refusal: ErrorCode) -> str:
    chain = entry.get("chain", "nextStart")
    if chain not in _CHAINS:
        raise FluxError(refusal, f"{at}.chain is one of {list(_CHAINS)}, not {chain!r}")
    return str(chain)


def _read_entry(
    entry: Mapping[str, Any], at: str, code: ErrorCode, refusal: ErrorCode
) -> HistoryEntry:
    """One entry, read without its contract; *at* is the place its refusals name."""
    _unknown(entry, _ENTRY_KEYS, at, code)
    for key in ("length", "until"):
        if key not in entry:
            raise FluxError(refusal, f"{at}.{key} is required")
    until = entry["until"]
    if not isinstance(until, str) or not until:
        raise FluxError(
            refusal, f"{at}.until is the history's horizon, a value of the start column's type"
        )
    tracked = (
        _tracked(entry["tracked"], f"{at}.tracked", code, refusal) if "tracked" in entry else ()
    )
    reasons = (
        _levels(entry["reasons"], f"{at}.reasons", code, refusal) if "reasons" in entry else None
    )
    if len(tracked) > 1 and reasons is None:
        raise FluxError(refusal, f"{at}.reasons is required when more than one column is tracked")
    return HistoryEntry(
        emit=nfc(str(entry["emit"])),
        entities=_entities(entry.get("entities"), at, refusal),
        parent=(
            _parent(entry["parent"], f"{at}.parent", code, refusal) if "parent" in entry else None
        ),
        versions=(
            _count_shape(entry["versions"], f"{at}.versions", code, refusal, 1)
            if "versions" in entry
            else None
        ),
        start=(
            _histogram(entry["start"], f"{at}.start", code, refusal, length=False)
            if "start" in entry
            else None
        ),
        length=_histogram(entry["length"], f"{at}.length", code, refusal, length=True),
        until=until,
        chain=_chain(entry, at, refusal),
        open_end=_optional_text(entry, "openEnd", at, refusal),
        activation=_optional_text(entry, "activation", at, refusal),
        tracked=tracked,
        reasons=reasons,
        end=_end(entry["end"], f"{at}.end", code, refusal) if "end" in entry else None,
    )


def read_history_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode, refusal: ErrorCode
) -> Tuple[HistoryEntry, ...]:
    """Read a ``history`` block, or refuse it, naming where, the entry and the key.

    *emits* is every ``exposeId`` the Simulation emits. An unknown key at any level, and a block
    or an entry that is not a list or a mapping, is refused with *code* (``FLX-3105``), and so is
    a ``length`` that holds ``mean``, ``given``, ``states``, ``otherwise`` or ``open``. With
    *refusal* (``FLX-3403``) are refused: an emit the Simulation does not emit, a second entry for
    one emit, ``entities`` that is not a whole number above zero, a ``versions`` class below 1 or a
    shape that is not ``levels`` or ``bins`` with one weight each, a missing ``until`` or
    ``length``, a ``length`` bin below one day, a ``chain`` other than
    ``nextStart`` or ``dayBefore``, a ``transitions`` level that moves to itself or a row that
    totals zero, ``reasons`` missing when more than one column is tracked, an ``end`` whose
    ``mode`` is not ``closeLast`` or ``finalVersion``, a ``finalVersion`` without ``column`` and a
    ``share`` outside 0 to 1. What needs the contract (a level, a date, a column) is checked by
    :func:`check_history`.
    """
    if not isinstance(block, Sequence) or isinstance(block, (str, bytes)):
        raise FluxError(code, f"{where} is a list of {{emit, entities, length, until}} entries")
    if len(block) > _MOST_ENTRIES:
        raise FluxError(code, f"{where} holds more than {_MOST_ENTRIES} entries")
    found: Dict[str, HistoryEntry] = {}
    for index, raw in enumerate(block):
        at = f"{where}[{index}]"
        if not isinstance(raw, Mapping):
            raise FluxError(
                code, f"{at} is a mapping with an emit, entities, a length and an until"
            )
        _unknown(raw, _ENTRY_KEYS, at, code)
        emit = _text(raw, "emit", at, refusal)
        named = f"{at} (emit {emit!r})"
        if emit not in emits:
            raise FluxError(
                refusal,
                f"{named}.emit names {emit!r}, which this Simulation does not emit "
                f"(it emits {sorted(emits)})",
            )
        if emit in found:
            raise FluxError(
                refusal, f"{named} is a second entry for {emit!r}; an emit has one history"
            )
        found[emit] = _read_entry(raw, named, code, refusal)
    return tuple(found[key] for key in sorted(found))


# ---------------------------------------------------------------------------
# Binding into H, and reading the member back
# ---------------------------------------------------------------------------


def bound_into_h(entries: Sequence[HistoryEntry], emit: str) -> Optional[Dict[str, Any]]:
    """The member of *emit*'s entry for ``H`` that its history makes, or ``None``.

    The entry as :meth:`HistoryEntry.as_member` writes it, with no ``emit``, each key only when
    declared; ``None`` when *emit* has no entry, so the entry gains no key.
    """
    wanted = nfc(emit)
    for entry in entries:
        if entry.emit == wanted:
            return entry.as_member()
    return None


def history_entries_of(
    extension: Optional[Mapping[str, Any]], emit: str
) -> Tuple[HistoryEntry, ...]:
    """The history an emit's bound extension holds (the member :func:`bound_into_h` made), or ``()``."""
    raw = extension.get(HISTORY_KEY) if extension is not None else None
    if not isinstance(raw, Mapping):
        return ()
    entry = _read_entry(
        {"emit": nfc(emit), **raw},
        f"history ({emit})",
        get("FLX-3105"),
        get("FLX-3403"),
    )
    return (entry,)


# ---------------------------------------------------------------------------
# Reading the entry against its contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryTarget:
    """What the contract and the Simulation say about one history's emit.

    ``columns`` is each column's kind; ``required`` the columns the contract requires. ``fixed``
    is the columns a history never sets: every key, the natural entity and the validity
    columns. ``levels`` is the levels of each categorical column, as ``(level, weight)`` pairs
    (the weight is ``None`` when only the type declares the level), and ``nullable`` the columns
    whose fidelity declares a ``nullFraction``. ``window`` is ``timeBounds`` as milliseconds.
    ``counted`` is the relationships a ``counts`` entry lays out and ``links`` the link entries of
    the emit, by relationship. ``primary`` is the columns of the primary entities, and ``given``
    the columns each column is drawn given, for the columns that declare one.
    """

    expose_id: str
    columns: Mapping[str, LogicalKind]
    required: FrozenSet[str]
    fixed: FrozenSet[str]
    natural: Tuple[str, ...]
    start: Optional[str]
    end: Optional[str]
    relationships: Mapping[str, Relationship]
    levels: Mapping[str, Tuple[Tuple[str, Optional[str]], ...]]
    nullable: FrozenSet[str]
    window: Optional[Tuple[int, int]]
    counted: FrozenSet[str] = frozenset()
    links: Mapping[str, LinkEntry] = field(default_factory=dict)
    primary: Tuple[str, ...] = ()
    given: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)

    def link_of(self, relationship: str) -> Optional[LinkEntry]:
        """The link entry for *relationship*, if the emit has one."""
        return self.links.get(relationship)


def _levels_of(
    declared_types: Mapping[str, str],
    fidelity_columns: Mapping[str, Any],
) -> Dict[str, Tuple[Tuple[str, Optional[str]], ...]]:
    """The levels of each column that declares some: a fidelity ``levels`` mapping, or an enum type."""
    found: Dict[str, Tuple[Tuple[str, Optional[str]], ...]] = {}
    for name, declared in declared_types.items():
        try:
            enum = declared_enum_values(declared)
        except FluxError:
            enum = ()
        if enum:
            found[name] = tuple((str(level), None) for level in enum)
        block = fidelity_columns.get(name)
        levels = block.get("levels") if isinstance(block, Mapping) else None
        if isinstance(levels, Mapping) and levels:
            found[name] = tuple((nfc(str(level)), _decimal(w)) for level, w in levels.items())
    return found


def _given_of(columns: Mapping[str, Any]) -> Dict[str, Tuple[str, ...]]:
    """The columns each fidelity column is drawn given, for the columns that declare a ``given``."""
    found: Dict[str, Tuple[str, ...]] = {}
    for name, block in columns.items():
        declared = block.get("given") if isinstance(block, Mapping) else None
        names = [declared] if isinstance(declared, str) else declared
        if isinstance(names, list) and names:
            found[nfc(str(name))] = tuple(nfc(str(item)) for item in names)
    return found


def history_target(
    expose: Mapping[str, Any],
    keys: Optional[ExposeKeys],
    *,
    fidelity: Optional[Mapping[str, Any]],
    time_bounds: Optional[Mapping[str, Any]],
    counts: Sequence[CountsEntry],
    links: Sequence[LinkEntry],
) -> HistoryTarget:
    """What the contract and the Simulation say about *expose*, for :func:`check_history`.

    *expose* is the contract's ``exposes[]`` entry, *keys* its keys, *fidelity* the emit's
    ``fidelity`` block as declared, *time_bounds* the Simulation's ``spec.timeBounds``, and
    *counts* and *links* the entries the Simulation declares for the emit.
    """
    compiled = compile_expose(expose)
    ends = window_ends(expose.get("semantics"))
    held = keys if keys is not None else ExposeKeys(expose_id=compiled.expose_id)
    fixed = {entity.column for entity in held.entities}
    fixed.update(column for key in held.keys + held.instant_keys for column in key)
    fixed.update(ends.values())
    columns = fidelity.get("columns") if isinstance(fidelity, Mapping) else None
    columns = columns if isinstance(columns, Mapping) else {}
    nullable = frozenset(
        nfc(str(name))
        for name, block in columns.items()
        if isinstance(block, Mapping) and "nullFraction" in block
    )
    return HistoryTarget(
        expose_id=compiled.expose_id,
        columns={column.name: column.logical_type.kind for column in compiled.columns},
        required=frozenset(column.name for column in compiled.columns if column.required),
        fixed=frozenset(fixed),
        natural=tuple(entity.column for entity in held.entities if entity.kind == "natural"),
        start=ends.get("start"),
        end=ends.get("end"),
        relationships={relationship.name: relationship for relationship in held.relationships},
        levels=_levels_of(compiled.column_types, columns),
        nullable=nullable,
        window=_window_ms(time_bounds),
        counted=frozenset(entry.relationship for entry in counts),
        links={entry.relationship: entry for entry in links},
        primary=tuple(entity.column for entity in held.entities if entity.kind == "primary"),
        given=_given_of(columns),
    )


def _window_ms(bounds: Optional[Mapping[str, Any]]) -> Optional[Tuple[int, int]]:
    if not isinstance(bounds, Mapping):
        return None
    start, end = bounds.get("start"), bounds.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    return parse_rfc3339_ms(start), parse_rfc3339_ms(end)


def _instant(text: str, kind: LogicalKind) -> Optional[int]:
    """*text* as an instant in the chain's unit (days or milliseconds since the epoch), or ``None``."""
    if kind is LogicalKind.DATE:
        if DATE_PATTERN.match(text) is None:
            return None
        try:
            return date.fromisoformat(text).toordinal() - _EPOCH_ORDINAL
        except ValueError:
            return None
    try:
        return parse_rfc3339_ms(text)
    except FluxError:
        return None


def _in_unit(milliseconds: int, kind: LogicalKind) -> int:
    return milliseconds // _DAY_MS if kind is LogicalKind.DATE else milliseconds


def _check_window(
    entry: HistoryEntry, target: HistoryTarget, at: str, refusal: ErrorCode
) -> LogicalKind:
    """Refuse an emit with no validity window, or with a start and an end of different types."""
    if len(target.natural) != 1 or target.start is None or target.end is None:
        raise FluxError(
            refusal,
            f"{at}.emit {entry.emit!r} declares no validity window: it needs one natural entity "
            "and a start and an end dimension with validityParams",
        )
    kinds = (target.columns.get(target.start), target.columns.get(target.end))
    if kinds[0] not in _UNIT_KINDS or kinds[1] not in _UNIT_KINDS:
        raise FluxError(
            refusal, f"{at}.emit {entry.emit!r} has a validity window that is not DATE or TIMESTAMP"
        )
    if kinds[0] is not kinds[1]:
        raise FluxError(
            refusal,
            f"{at}.emit {entry.emit!r} has a start of type {kinds[0].value if kinds[0] else None} "
            f"and an end of type {kinds[1].value if kinds[1] else None}; they are both DATE or both TIMESTAMP",
        )
    assert kinds[0] is not None
    shared = [column for column in target.primary if column in target.natural]
    if shared:
        raise FluxError(
            refusal,
            f"{at}.emit {entry.emit!r} has the primary entity {shared[0]!r}, which is also its "
            "natural entity: the versions of an entity would share a key",
        )
    return kinds[0]


def _check_parent(entry: HistoryEntry, target: HistoryTarget, at: str, refusal: ErrorCode) -> None:
    """Refuse a ``parent`` that is not a to-one relationship, or that a counts or links entry configures."""
    parent = entry.parent
    if parent is None:
        return
    where = f"{at}.parent.relationship"
    relationship = target.relationships.get(parent.relationship)
    if relationship is None or relationship.cardinality not in ("many_to_one", "one_to_one"):
        raise FluxError(
            refusal,
            f"{where} {parent.relationship!r} is not a to-one relationship of {entry.emit!r} "
            f"(its relationships: {sorted(target.relationships)})",
        )
    if parent.relationship in target.counted:
        raise FluxError(
            refusal,
            f"{where} {parent.relationship!r} is also laid out by a counts entry: "
            "one relationship has one layout",
        )
    link = target.link_of(parent.relationship)
    if link is not None and (link.when is not None or link.weight is not None):
        raise FluxError(
            refusal,
            f"{where} {parent.relationship!r} has a links entry with a when or a weight, "
            "which would pick the parent a second way",
        )


def _check_dates(
    entry: HistoryEntry, target: HistoryTarget, kind: LogicalKind, at: str, refusal: ErrorCode
) -> None:
    """Refuse an ``until`` or a ``start`` bin edge the window, or the type, does not hold."""
    until = _instant(entry.until, kind)
    if until is None:
        raise FluxError(
            refusal,
            f"{at}.until {entry.until!r} is not a {kind.value} (a DATE is written 2025-12-31)",
        )
    if target.window is None:
        raise FluxError(refusal, f"{at}.until needs the Simulation's spec.timeBounds")
    low, high = (_in_unit(edge, kind) for edge in target.window)
    if until > high:
        raise FluxError(refusal, f"{at}.until {entry.until!r} is after timeBounds.end")
    if until <= low:
        raise FluxError(refusal, f"{at}.until {entry.until!r} is not after timeBounds.start")
    if entry.start is None:
        return
    for edge in entry.start.edges:
        value = _instant(edge, kind)
        if value is None:
            raise FluxError(refusal, f"{at}.start.bins holds {edge!r}, which is not a {kind.value}")
        if not low <= value <= until:
            raise FluxError(
                refusal,
                f"{at}.start.bins holds {edge!r}, outside [timeBounds.start, until {entry.until!r}]",
            )


def _check_ends(
    entry: HistoryEntry, target: HistoryTarget, kind: LogicalKind, at: str, refusal: ErrorCode
) -> None:
    """Refuse a ``chain`` or an ``openEnd`` the end column cannot take."""
    if entry.chain == "dayBefore" and kind is not LogicalKind.DATE:
        raise FluxError(
            refusal, f"{at}.chain dayBefore needs DATE columns; {entry.emit!r} has {kind.value}"
        )
    assert target.end is not None
    if entry.open_end is None:
        if target.end in target.required:
            raise FluxError(
                refusal,
                f"{at}.openEnd is required: the end column {target.end!r} is required, "
                "so an open version cannot leave it NULL",
            )
        return
    if _instant(entry.open_end, kind) is None:
        raise FluxError(
            refusal,
            f"{at}.openEnd {entry.open_end!r} is not a {kind.value} (the end column's type)",
        )


def _check_column(
    column: str,
    target: HistoryTarget,
    at: str,
    key: str,
    refusal: ErrorCode,
    parent_key: Optional[str],
) -> None:
    """Refuse a column the emit lacks, or that is a key, a validity column or the parent's foreign key."""
    if column not in target.columns:
        raise FluxError(
            refusal,
            f"{at}.{key} names {column!r}, which {target.expose_id!r} does not declare "
            f"(columns: {sorted(target.columns)})",
        )
    if column in target.fixed or column == parent_key:
        raise FluxError(
            refusal,
            f"{at}.{key} names {column!r}, a key, a validity column or the parent's foreign key: "
            "the history sets those",
        )


def _check_categorical(
    column: str, target: HistoryTarget, at: str, key: str, refusal: ErrorCode
) -> Tuple[Tuple[str, Optional[str]], ...]:
    """The levels of *column*, or a refusal: it declares none, or declares a ``nullFraction``."""
    levels = target.levels.get(column)
    if not levels:
        raise FluxError(
            refusal, f"{at}.{key} names {column!r}, which is not categorical: it declares no levels"
        )
    if column in target.nullable:
        raise FluxError(
            refusal,
            f"{at}.{key} names {column!r}, which declares a nullFraction: a change cannot be NULL",
        )
    return levels


def _check_tracked_one(
    tracked: HistoryTracked,
    levels: Sequence[Tuple[str, Optional[str]]],
    at: str,
    refusal: ErrorCode,
) -> None:
    """Refuse a transition or an initial level the column does not declare."""
    names = {level for level, _ in levels}
    if tracked.initial is not None:
        unknown = [level for level in tracked.initial.values if nfc(level) not in names]
        if unknown:
            raise FluxError(
                refusal,
                f"{at}.initial names the level {unknown[0]!r}, which {tracked.column!r} does not "
                f"declare (levels: {sorted(names)})",
            )
    for level, row in tracked.transitions:
        for named in (level, *(target for target, _ in row)):
            if named not in names:
                raise FluxError(
                    refusal,
                    f"{at}.transitions names the level {named!r}, which {tracked.column!r} does not "
                    f"declare (levels: {sorted(names)})",
                )


def _check_tracked(
    entry: HistoryEntry,
    target: HistoryTarget,
    at: str,
    refusal: ErrorCode,
    parent_key: Optional[str],
) -> None:
    for index, tracked in enumerate(entry.tracked):
        where = f"{at}.tracked[{index}]"
        _check_column(tracked.column, target, where, "column", refusal, parent_key)
        levels = _check_categorical(tracked.column, target, where, "column", refusal)
        _check_tracked_one(tracked, levels, where, refusal)
    if entry.reasons is not None:
        named = {tracked.column for tracked in entry.tracked}
        stray = [level for level in entry.reasons.values if level not in named]
        if stray:
            raise FluxError(
                refusal,
                f"{at}.reasons names {stray[0]!r}, which is not a tracked column "
                f"(tracked: {sorted(named)})",
            )


def _initial_weight(
    tracked: HistoryTracked, level: str, levels: Sequence[Tuple[str, Optional[str]]]
) -> bool:
    """Whether the column's first value can be *level*: its ``initial``, or else its own weights."""
    if tracked.initial is not None:
        pairs = zip(tracked.initial.values, tracked.initial.weights, strict=True)
        return any(nfc(name) == level and Fraction(weight) > 0 for name, weight in pairs)
    return any(
        name == level and (weight is None or Fraction(weight) > 0) for name, weight in levels
    )


def _check_end(
    entry: HistoryEntry,
    target: HistoryTarget,
    at: str,
    refusal: ErrorCode,
    parent_key: Optional[str],
) -> None:
    """Refuse an ``end`` column that is not categorical, or whose value a chain can reach early."""
    end = entry.end
    if end is None or end.column is None or end.value is None:
        return
    where = f"{at}.end"
    _check_column(end.column, target, where, "column", refusal, parent_key)
    levels = _check_categorical(end.column, target, where, "column", refusal)
    if end.value not in {level for level, _ in levels}:
        raise FluxError(
            refusal,
            f"{where}.value {end.value!r} is not a level of {end.column!r} "
            f"(levels: {sorted(level for level, _ in levels)})",
        )
    for tracked in entry.tracked:
        if tracked.column != end.column:
            continue
        reached = _initial_weight(tracked, end.value, levels) or any(
            target_level == end.value and Fraction(weight) > 0
            for _, row in tracked.transitions
            for target_level, weight in row
        )
        if reached:
            raise FluxError(
                refusal,
                f"{where}.value {end.value!r} gets a weight above zero from the initial or the "
                f"transitions of {tracked.column!r}: the ended status sits on the last version only",
            )


def _check_reachable(
    entry: HistoryEntry, target: HistoryTarget, at: str, refusal: ErrorCode
) -> None:
    """Refuse a tracked level a chain can reach that has no ``transitions`` row to leave it by."""
    for index, tracked in enumerate(entry.tracked):
        levels = target.levels.get(tracked.column, ())
        rows = {level: row for level, row in tracked.transitions}
        reached = [level for level, _ in levels if _initial_weight(tracked, level, levels)]
        seen = set(reached)
        while reached:
            level = reached.pop()
            if level not in rows:
                raise FluxError(
                    refusal,
                    f"{at}.tracked[{index}].transitions has no row for the level {level!r}, "
                    "which a chain can reach: every reachable level needs a row",
                )
            for moved, weight in rows[level]:
                if Fraction(weight) > 0 and moved not in seen:
                    seen.add(moved)
                    reached.append(moved)


def _check_carried(
    entry: HistoryEntry,
    target: HistoryTarget,
    at: str,
    refusal: ErrorCode,
) -> None:
    """Refuse a column drawn given a tracked column, and an end value a carried column can draw."""
    tracked = {item.column for item in entry.tracked}
    for column, names in sorted(target.given.items()):
        stray = [name for name in names if name in tracked]
        if stray and column not in tracked:
            raise FluxError(
                refusal,
                f"{at} tracks {stray[0]!r}, and {column!r} is drawn given it: a carried column "
                "keeps its first value, so it cannot follow a column that changes",
            )
    end = entry.end
    if end is None or end.column is None or end.value is None or end.column in tracked:
        return
    levels = target.levels.get(end.column, ())
    if any(
        name == end.value and (weight is None or Fraction(weight) > 0) for name, weight in levels
    ):
        raise FluxError(
            refusal,
            f"{at}.end.value {end.value!r} gets a weight above zero from the statistics of "
            f"{end.column!r}: the ended status sits on the last version only",
        )


def _check_activation(
    entry: HistoryEntry,
    target: HistoryTarget,
    kind: LogicalKind,
    at: str,
    refusal: ErrorCode,
    parent_key: Optional[str],
) -> None:
    column = entry.activation
    if column is None:
        return
    _check_column(column, target, at, "activation", refusal, parent_key)
    if target.columns[column] is not kind:
        raise FluxError(
            refusal,
            f"{at}.activation names {column!r}, a {target.columns[column].value}; "
            f"it holds the first start, a {kind.value}",
        )


def check_history(entry: HistoryEntry, target: HistoryTarget, *, refusal: ErrorCode) -> None:
    """Read *entry* against its emit's contract and the Simulation, or refuse with *refusal* (``FLX-3403``).

    Refused, each message naming the entry and the key: an emit with no validity window or with a
    start and an end of different types; a ``parent`` that is not a to-one relationship of the
    emit, that a ``counts`` entry also names, or that a ``links`` entry gives a ``when`` or a
    ``weight``; an ``until`` that is not a value of the start's type, is after ``timeBounds.end``
    or is not after ``timeBounds.start``; a ``start`` bin edge outside ``[timeBounds.start,
    until]``; ``dayBefore`` on TIMESTAMP columns; an ``openEnd`` that does not read as the end
    column's type, or none on an end column the contract requires; an ``activation`` column not of
    the start's type; a tracked column that is not categorical or declares a ``nullFraction``; a
    level a ``transitions`` or ``initial`` names that the column does not declare; ``reasons``
    naming a column that is not tracked; an ``end`` ``value`` that is not a level of its column or
    that the chain's ``initial`` or ``transitions`` can reach, or that a carried end column's own
    statistics can draw; a reachable tracked level with no ``transitions`` row; a column drawn
    ``given`` a tracked column; a primary entity that is also the natural entity; and an
    activation, tracked or end column that is a key, a validity column or the parent's foreign
    key.
    """
    at = f"history ({entry.emit})"
    kind = _check_window(entry, target, at, refusal)
    _check_parent(entry, target, at, refusal)
    parent_key = _parent_key(entry, target)
    _check_dates(entry, target, kind, at, refusal)
    _check_ends(entry, target, kind, at, refusal)
    _check_activation(entry, target, kind, at, refusal, parent_key)
    _check_tracked(entry, target, at, refusal, parent_key)
    _check_end(entry, target, at, refusal, parent_key)
    _check_reachable(entry, target, at, refusal)
    _check_carried(entry, target, at, refusal)


def check_history_sources(
    entry: HistoryEntry,
    target: HistoryTarget,
    lifetimes: Sequence[LifetimeEntry],
    within: Sequence[WithinEntry],
    *,
    refusal: ErrorCode,
) -> None:
    """Refuse a ``lifetimes`` or ``within`` entry that sets a column the history sets (ADR 0017 section 2).

    A history sets its emit's validity start, its validity end and its ``activation`` column, so
    a ``lifetimes`` entry whose ``start`` or ``end`` is one of them, and a ``within`` entry whose
    ``date`` is one, would be a second source for the column. Refused with *refusal*
    (``FLX-6203``), naming the column, the history and the entry. Only the entries of the
    history's own emit are read.
    """
    sets = _history_sets(entry, target)
    _check_lifetime_sources(entry, lifetimes, sets, refusal)
    _check_within_sources(entry, within, sets, refusal)
    _check_parent_lifetime(entry, target, lifetimes)


def _check_parent_lifetime(
    entry: HistoryEntry, target: HistoryTarget, lifetimes: Sequence[LifetimeEntry]
) -> None:
    """Refuse (``FLX-3403``) ``parent.lifetime`` where there is nothing to keep to (ADR 0017, amendment of 2026-11-24).

    The key needs ``parent.relationship``, a to-one relationship of the emit whose target emit
    declares a ``lifetimes`` entry: its start, and optionally its end, are the columns the history
    keeps to. The message names the key.
    """
    parent = entry.parent
    if parent is None or not parent.lifetime:
        return
    at = f"history ({entry.emit}).parent.lifetime"
    relationship = target.relationships.get(parent.relationship)
    if relationship is None:
        return  # check_history refuses a parent that is no relationship of the emit
    held = nfc(relationship.target)
    if not any(nfc(found.emit) == held for found in lifetimes):
        raise FluxError(
            get("FLX-3403"),
            f"{at} needs a lifetimes entry on {relationship.target!r}, the emit relationship "
            f"{parent.relationship!r} reaches: it names the start, and the end, the history "
            "keeps to",
        )


def _history_sets(entry: HistoryEntry, target: HistoryTarget) -> Dict[str, str]:
    """The columns a history sets (NFC-normalized), each with the role it sets it in."""
    sets: Dict[str, str] = {}
    for column, role in (
        (target.start, "validity start"),
        (target.end, "validity end"),
        (entry.activation, "activation column"),
    ):
        if column is not None:
            sets.setdefault(nfc(column), role)
    return sets


def _check_lifetime_sources(
    entry: HistoryEntry,
    lifetimes: Sequence[LifetimeEntry],
    sets: Mapping[str, str],
    refusal: ErrorCode,
) -> None:
    """Refuse a ``lifetimes`` entry of the history's emit whose ``start`` or ``end`` is a column in *sets*."""
    for lifetime in lifetimes:
        if lifetime.emit != entry.emit:
            continue
        for key, column in (("start", lifetime.start), ("end", lifetime.end)):
            if column is not None and nfc(column) in sets:
                raise FluxError(
                    refusal,
                    f"column {column!r} has more than one source: the history ({entry.emit}) sets "
                    f"it as its {sets[nfc(column)]} and the lifetimes entry with {key} "
                    f"{column!r} sets it too",
                )


def _check_within_sources(
    entry: HistoryEntry,
    within: Sequence[WithinEntry],
    sets: Mapping[str, str],
    refusal: ErrorCode,
) -> None:
    """Refuse a ``within`` entry of the history's emit whose ``date`` is a column in *sets*."""
    for placed in within:
        if placed.emit != entry.emit or placed.date is None:
            continue
        if nfc(placed.date) in sets:
            raise FluxError(
                refusal,
                f"column {placed.date!r} has more than one source: the history ({entry.emit}) "
                f"sets it as its {sets[nfc(placed.date)]} and the within entry on relationship "
                f"{'.'.join(placed.relationship)!r} with date {placed.date!r} sets it too",
            )


def check_history_dates(
    entry: HistoryEntry,
    expose: Mapping[str, Any],
    fidelity: Optional[Mapping[str, Any]],
    target: HistoryTarget,
    *,
    refusal: ErrorCode,
    other_sourced: Collection[str] = (),
    hidden: Sequence[HiddenColumn] = (),
) -> None:
    """Refuse a date of a history's emit outside ``timeBounds`` (ADR 0017 section 11).

    Refused with *refusal* (``FLX-3403``): a DATE or TIMESTAMP column whose declared range or bins
    leave ``spec.timeBounds``, and one that declares no range and no bins, which would be drawn
    inside the window and so change with it, unless the history sets it (its start, its end, its
    activation or a key) or another source does: *other_sourced* is the columns a rule, a
    dependency or a copy sets, which are never drawn (EXPR-SPEC-2 section 9). A history that sets a column still refuses a range it declares outside
    the window. ``openEnd`` is no declared range and is not read. Every other emit keeps
    the warning of ``fluxsim validate``. *hidden* is the emit's hidden columns: the expose is
    compiled with them, as the plan compiles it, so a statistic for one is not refused as
    naming a column the expose lacks.
    """
    from flux_engine.generator.statistics import statistics_from_fidelity

    if target.window is None:
        return
    compiled = with_hidden_columns(compile_expose(expose), hidden)
    statistics = statistics_from_fidelity(compiled, fidelity)
    at = f"history ({entry.emit})"
    sets = set(target.fixed) | set(other_sourced)
    if entry.activation is not None:
        sets.add(entry.activation)
    for column in compiled.columns:
        kind = column.logical_type.kind
        if kind not in _UNIT_KINDS:
            continue
        declared = statistics[column.name]
        low, high = (_in_unit(edge, kind) for edge in target.window)
        edges = [v for v in (declared.range_min, declared.range_max) if v is not None]
        edges += list(declared.bins or ())
        if not edges and column.name not in sets:
            raise FluxError(
                refusal,
                f"{at}: the {kind.value} column {column.name!r} declares no range and no bins, so "
                "its rows would be drawn inside timeBounds and change when the window does; "
                "declare its range, or let the history set it",
            )
        if any(edge < low or edge > high for edge in edges):
            raise FluxError(
                refusal,
                f"{at}: the declared range or bins of {column.name!r} leave spec.timeBounds, so "
                "rows would carry dates outside the window the run covers",
            )


def _parent_key(entry: HistoryEntry, target: HistoryTarget) -> Optional[str]:
    """The foreign key column of the entry's parent relationship, when it has one column."""
    if entry.parent is None:
        return None
    relationship = target.relationships.get(entry.parent.relationship)
    if relationship is None or len(relationship.from_columns) != 1:
        return None
    return relationship.from_columns[0]
