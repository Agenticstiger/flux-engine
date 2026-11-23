# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The as-of links a Simulation declares: a dated row reads the version valid on its date (ADR 0017 section 8).

A Simulation declares them under the top-level ``asOf`` key of its namespace, in the FLUX bundle
and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        asOf:
          - { emit: invoice, relationship: subscription, date: invoice_date }

An entry names an emitted expose, a to-one relationship of it whose target is a history's emit and
whose ``toColumns`` are that history's natural entity, and a DATE or TIMESTAMP column of the emit.
A path through the relationship then reads the version of the linked entity valid on the row's
``date`` rather than the row the relationship picked.

Two readers share this module. :func:`read_asof_block` reads what the entry says, with no contract
in hand: an unknown key is the reader's *code* (``FLX-3105``) and every other malformed value is its
*refusal* code (``FLX-3403``), each message naming the entry and the key. :func:`check_asof` reads
the entry against the contract and the histories the Simulation declares.

**An entry changes the rows the emit generates**, so it is bound into ``H`` before any row is
drawn: :func:`bound_into_h` is the member the reading emit's entry for ``H`` gains, present only when
the emit has entries, so a bundle that declares none hashes exactly as before. ``G`` leaves the
member out, as it leaves out the history's (:func:`flux_engine.seam.emit.seam_reusable_digest`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.history import HistoryTarget

__all__ = [
    "ASOF_KEY",
    "AsOfEntry",
    "asof_entries_of",
    "bound_into_h",
    "check_asof",
    "read_asof_block",
]

#: The top-level key of the namespace that declares as-of links, and the member an emit's ``H`` gains.
ASOF_KEY = "asOf"

_ENTRY_KEYS = ("emit", "relationship", "date")
_MOST_ENTRIES = 4096
_DATE_KINDS = (LogicalKind.DATE, LogicalKind.TIMESTAMP)


@dataclass(frozen=True)
class AsOfEntry:
    """One as-of link: the relationship a path reads through, and the column that dates the row."""

    emit: str
    relationship: str
    date: str

    def as_member(self) -> Dict[str, Any]:
        """The entry as the emit's ``asOf`` member holds it: no ``emit``."""
        return {"relationship": self.relationship, "date": self.date}


def _text(entry: Mapping[str, Any], key: str, at: str, refusal: ErrorCode) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise FluxError(refusal, f"{at}.{key} is a non-empty string")
    return nfc(value)


def read_asof_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode, refusal: ErrorCode
) -> Tuple[AsOfEntry, ...]:
    """Read an ``asOf`` block, or refuse it, naming where, the entry and the key.

    *emits* is every ``exposeId`` the Simulation emits. A block or an entry that is not a list or a
    mapping, and a key other than ``emit``, ``relationship`` and ``date``, is refused with *code*
    (``FLX-3105``). With *refusal* (``FLX-3403``) are refused: more than 4 096 entries, an emit the
    Simulation does not emit, a name that is not a non-empty string and a relationship declared
    twice for one emit. What needs the contract is checked by :func:`check_asof`.
    """
    if not isinstance(block, Sequence) or isinstance(block, (str, bytes)):
        raise FluxError(code, f"{where} is a list of {{emit, relationship, date}} entries")
    if len(block) > _MOST_ENTRIES:
        raise FluxError(refusal, f"{where} holds more than {_MOST_ENTRIES} entries")
    found: Dict[Tuple[str, str], AsOfEntry] = {}
    for index, raw in enumerate(block):
        at = f"{where}[{index}]"
        if not isinstance(raw, Mapping):
            raise FluxError(code, f"{at} is a mapping with emit, relationship and date")
        unknown = sorted(str(key) for key in raw if str(key) not in _ENTRY_KEYS)
        if unknown:
            raise FluxError(
                code, f"{at} declares {unknown}; an asOf entry carries {list(_ENTRY_KEYS)}"
            )
        emit = _text(raw, "emit", at, refusal)
        relationship = _text(raw, "relationship", at, refusal)
        date = _text(raw, "date", at, refusal)
        if emit not in emits:
            raise FluxError(
                refusal,
                f"{at}.emit names {emit!r}, which this Simulation does not emit "
                f"(it emits {sorted(emits)})",
            )
        if (emit, relationship) in found:
            raise FluxError(
                refusal, f"{at} declares asOf for {emit}.{relationship} twice; a link has one date"
            )
        found[(emit, relationship)] = AsOfEntry(emit, relationship, date)
    return tuple(found[key] for key in sorted(found))


def check_asof(
    entry: AsOfEntry,
    reader: HistoryTarget,
    histories: Mapping[str, HistoryTarget],
    *,
    refusal: ErrorCode,
) -> None:
    """Refuse an entry whose relationship or date cannot be read as of a date (``FLX-3403``).

    *reader* is what the contract says of the entry's emit, and *histories* the same for each emit
    that declares a history, by exposeId. The relationship must be a to-one relationship of the
    emit whose target is one of them and whose ``toColumns`` are that history's natural entity; the
    date must be a DATE or TIMESTAMP column of the emit.
    """
    at = f"asOf ({entry.emit}.{entry.relationship})"
    relationship = reader.relationships.get(entry.relationship)
    if relationship is None or relationship.cardinality not in ("many_to_one", "one_to_one"):
        raise FluxError(
            refusal,
            f"{at}.relationship {entry.relationship!r} is not a to-one relationship of "
            f"{entry.emit!r} (its relationships: {sorted(reader.relationships)})",
        )
    target = histories.get(relationship.target)
    if target is None:
        raise FluxError(
            refusal,
            f"{at}.relationship reaches {relationship.target!r}, which declares no history: "
            f"asOf reads the version of an entity valid on a date (histories: {sorted(histories)})",
        )
    if tuple(relationship.to_columns) != tuple(target.natural):
        raise FluxError(
            refusal,
            f"{at}.relationship reaches the columns {list(relationship.to_columns)} of "
            f"{target.expose_id!r}, which are not its natural entity {list(target.natural)}: "
            "an as-of link is the entity's key",
        )
    kind = reader.columns.get(entry.date)
    if kind not in _DATE_KINDS:
        raise FluxError(
            refusal,
            f"{at}.date {entry.date!r} is not a DATE or TIMESTAMP column of {entry.emit!r}",
        )


def bound_into_h(entries: Sequence[AsOfEntry], emit: str) -> Optional[List[Dict[str, Any]]]:
    """The member of *emit*'s entry for ``H`` that its as-of entries make, or ``None``.

    One ``{"relationship": <name>, "date": <column>}`` mapping per entry of *emit*, in
    relationship order; ``None`` when it has none, so the entry gains no key.
    """
    wanted = nfc(emit)
    member = [
        entry.as_member()
        for entry in sorted(entries, key=lambda item: item.relationship)
        if entry.emit == wanted
    ]
    return member or None


def asof_entries_of(extension: Optional[Mapping[str, Any]], emit: str) -> Tuple[AsOfEntry, ...]:
    """The as-of entries an emit's bound extension holds (the member :func:`bound_into_h` made)."""
    raw = extension.get(ASOF_KEY) if extension is not None else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(
        AsOfEntry(nfc(emit), str(item["relationship"]), str(item["date"]))
        for item in raw
        if isinstance(item, Mapping) and "relationship" in item and "date" in item
    )
