# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The links a Simulation configures: conditional links (ADR 0022 section 5).

A Simulation declares them under the top-level ``links`` key of its namespace, in the FLUX bundle
and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        links:
          - { emit: invoice, relationship: account, when: "i_amount > 100" }

An entry names an emitted expose and a relationship that expose declares, and configures it with
at least one of ``when``, ``sentinels`` and ``weight``. ``when`` is a FLUID SQL/1 predicate over
the emit's own columns: on a row where it is not TRUE the relationship's columns are NULL, so no
link exists there. ``sentinels`` maps a column the relationship copies to the text of the value
the copy writes on a row whose link reaches no row, in place of NULL (ADR 0022 section 6).
``weight`` names a number column of the relationship's target: the parent row of each row is
picked by the weights of the parent's final rows (ADR 0023 section 4).

**A link entry changes the rows the emit generates**, so it is bound into ``H`` before any row is
drawn: :func:`bound_into_h` is the member the emit's entry for ``H`` gains, present only when the
emit has entries, so a bundle that declares none hashes exactly as before. ``G`` leaves the member
out, as it leaves out the facts' (:func:`flux_engine.seam.emit.seam_reusable_digest`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError

__all__ = [
    "LINKS_KEY",
    "LinkEntry",
    "bound_into_h",
    "link_entries_of",
    "read_links_block",
]

#: The top-level key of the namespace that configures links, and the member an emit's ``H`` gains.
LINKS_KEY = "links"

_ENTRY_KEYS = ("emit", "relationship", "when", "sentinels", "weight")
_MOST_LINKS = 4096
_MOST_SENTINELS = 1024


@dataclass(frozen=True)
class LinkEntry:
    """One relationship's configuration: its condition and its sentinels, as written."""

    emit: str
    relationship: str
    when: Optional[str] = None
    #: ``(column, text)`` pairs of the declared sentinels, sorted by column.
    sentinels: Tuple[Tuple[str, str], ...] = ()
    #: The number column of the relationship's target that weighs the parents.
    weight: Optional[str] = None

    def as_member(self) -> Dict[str, Any]:
        """The entry as the emit's ``links`` member holds it: no ``emit``, no absent key."""
        member: Dict[str, Any] = {"relationship": self.relationship}
        if self.when is not None:
            member["when"] = self.when
        if self.sentinels:
            member["sentinels"] = dict(self.sentinels)
        if self.weight is not None:
            member["weight"] = self.weight
        return member


def _name(entry: Mapping[str, Any], key: str, at: str, code: ErrorCode) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise FluxError(code, f"{at}.{key} is a non-empty string")
    return nfc(value)


def _sentinels(raw: Any, at: str, code: ErrorCode) -> Tuple[Tuple[str, str], ...]:
    """A ``sentinels`` mapping as ``(column, text)`` pairs sorted by column."""
    if not isinstance(raw, Mapping) or not raw:
        raise FluxError(code, f"{at} is a non-empty mapping of column names to sentinel texts")
    if len(raw) > _MOST_SENTINELS:
        raise FluxError(code, f"{at} holds more than {_MOST_SENTINELS} sentinels")
    found: Dict[str, str] = {}
    for column, text in raw.items():
        if not isinstance(column, str) or not column:
            raise FluxError(code, f"{at} names a column that is not a non-empty string")
        if not isinstance(text, str):
            raise FluxError(
                code, f"{at}.{column} is the sentinel's text: write it as a string, as '0' or 'n/a'"
            )
        if nfc(column) in found:
            raise FluxError(code, f"{at} declares the column {nfc(column)!r} twice")
        found[nfc(column)] = text
    return tuple(sorted(found.items()))


def read_links_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode
) -> Tuple[LinkEntry, ...]:
    """Read a ``links`` block, or refuse it with *code*, naming where.

    *emits* is every ``exposeId`` the Simulation emits. Refused: a block that is not a list, more
    than 4 096 entries, an entry that is not a mapping or carries a key other than ``emit``,
    ``relationship``, ``when``, ``sentinels`` and ``weight``, an emit the Simulation does not emit,
    a name that is not a non-empty string, a relationship configured twice, an entry that
    configures nothing, a ``when`` that is not a non-empty text, a ``sentinels`` that is not a
    non-empty mapping of column names to texts (at most 1 024), and a ``weight`` that is not a
    non-empty column name. Whether the emit's contract declares the relationship, and the
    relationship copies each sentinel's column, is checked where the contract is known.
    """
    if not isinstance(block, Sequence) or isinstance(block, (str, bytes)):
        raise FluxError(code, f"{where} is a list of {{emit, relationship, when}} entries")
    if len(block) > _MOST_LINKS:
        raise FluxError(code, f"{where} holds more than {_MOST_LINKS} entries")
    found: Dict[Tuple[str, str], LinkEntry] = {}
    for index, entry in enumerate(block):
        at = f"{where}[{index}]"
        if not isinstance(entry, Mapping):
            raise FluxError(code, f"{at} is a mapping with emit, relationship and a configuration")
        unknown = sorted(str(key) for key in entry if str(key) not in _ENTRY_KEYS)
        if unknown:
            raise FluxError(
                code, f"{at} declares {unknown}; a link entry carries {list(_ENTRY_KEYS)}"
            )
        emit = _name(entry, "emit", at, code)
        relationship = _name(entry, "relationship", at, code)
        if emit not in emits:
            raise FluxError(
                code,
                f"{at}.emit names {emit!r}, which this Simulation does not emit "
                f"(it emits {sorted(emits)})",
            )
        if "when" not in entry and "sentinels" not in entry and "weight" not in entry:
            raise FluxError(
                code,
                f"{at} configures nothing: declare when (a condition on the link), sentinels "
                "or weight (a number column of the target that weighs its rows)",
            )
        weight = nfc(_name(entry, "weight", at, code)) if "weight" in entry else None
        when = entry.get("when")
        if "when" in entry and (not isinstance(when, str) or not when.strip()):
            raise FluxError(code, f"{at}.when is a FLUID SQL/1 predicate text")
        sentinels = (
            _sentinels(entry.get("sentinels"), f"{at}.sentinels", code)
            if ("sentinels" in entry)
            else ()
        )
        if (emit, relationship) in found:
            raise FluxError(code, f"{at} configures the relationship {emit}.{relationship} twice")
        found[(emit, relationship)] = LinkEntry(
            emit=emit, relationship=relationship, when=when, sentinels=sentinels, weight=weight
        )
    return tuple(found[key] for key in sorted(found))


def bound_into_h(entries: Sequence[LinkEntry], emit: str) -> Optional[List[Dict[str, Any]]]:
    """The member of *emit*'s entry for ``H`` that its link entries make, or ``None``.

    One ``{"relationship": <name>, "when": <text>}`` mapping per entry of *emit*, in relationship
    order; ``None`` when it has none, so the entry gains no key.
    """
    wanted = nfc(emit)
    member = [
        entry.as_member()
        for entry in sorted(entries, key=lambda item: item.relationship)
        if entry.emit == wanted
    ]
    return member or None


def link_entries_of(extension: Optional[Mapping[str, Any]], emit: str) -> Tuple[LinkEntry, ...]:
    """The link entries an emit's bound extension holds (the member :func:`bound_into_h` made)."""
    raw = extension.get(LINKS_KEY) if extension is not None else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    found: List[LinkEntry] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        sentinels = item.get("sentinels")
        found.append(
            LinkEntry(
                emit=nfc(emit),
                relationship=str(item["relationship"]),
                when=item.get("when"),
                sentinels=(
                    tuple(sorted(dict(sentinels).items())) if isinstance(sentinels, Mapping) else ()
                ),
                weight=item.get("weight") if isinstance(item.get("weight"), str) else None,
            )
        )
    return tuple(found)
