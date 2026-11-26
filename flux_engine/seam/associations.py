# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The association tables a Simulation declares: two relationships paired stub by stub (ADR 0039).

A Simulation declares them under the top-level ``associations`` key of its namespace, in the FLUX
bundle and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        associations:
          - { emit: enrolment, relationships: [student, course], distinct: true }

An entry names an emitted expose and two distinct relationships it declares. Each side's number of
rows per parent is that relationship's ``counts`` entry's shape (an even spread when it has none),
both sides totalling the emit's rows; the rows take the first side's parents in order and the
second side's through a keyed permutation of its stubs, the bipartite configuration model.
``distinct`` (default ``true``) says that no pair of parent rows repeats: the layout is then
capped, checked against the Gale-Ryser condition and repaired (:mod:`flux_engine.generator.associations`).

**An entry changes the rows the emit generates**, so it is bound into ``H`` before any row is
drawn: :func:`bound_into_h` is the member the emit's entry for ``H`` gains, present only when the
emit has entries, so a bundle that declares none hashes exactly as before. ``G`` leaves the member
out, as it leaves out the counts' and the bridges'.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError

__all__ = [
    "ASSOCIATIONS_KEY",
    "AssociationEntry",
    "association_entries_of",
    "bound_into_h",
    "read_associations_block",
]

#: The top-level key of the namespace that declares association tables, and the member an emit's
#: ``H`` gains.
ASSOCIATIONS_KEY = "associations"

_ENTRY_KEYS = ("emit", "relationships", "distinct")
_MOST_ASSOCIATIONS = 4096


@dataclass(frozen=True)
class AssociationEntry:
    """One association: the emit, its two relationships (the rows follow the first) and ``distinct``."""

    emit: str
    relationships: Tuple[str, str]
    distinct: bool = True


def _relationships(raw: Any, at: str, code: ErrorCode) -> Tuple[str, str]:
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes))
        or len(raw) != 2
        or not all(isinstance(name, str) and name for name in raw)
    ):
        raise FluxError(code, f"{at}.relationships is a list of two relationship names")
    first, second = nfc(raw[0]), nfc(raw[1])
    if first == second:
        raise FluxError(code, f"{at}.relationships names {first!r} twice; an association is two")
    return first, second


def _read_entry(entry: Any, at: str, emits: FrozenSet[str], code: ErrorCode) -> AssociationEntry:
    if not isinstance(entry, Mapping):
        raise FluxError(code, f"{at} is a mapping with emit and relationships")
    unknown = sorted(str(key) for key in entry if str(key) not in _ENTRY_KEYS)
    if unknown:
        raise FluxError(
            code, f"{at} declares {unknown}; an association carries {list(_ENTRY_KEYS)}"
        )
    emit = entry.get("emit")
    if not isinstance(emit, str) or not emit:
        raise FluxError(code, f"{at}.emit is a non-empty string")
    if nfc(emit) not in emits:
        raise FluxError(
            code,
            f"{at}.emit names {nfc(emit)!r}, which this Simulation does not emit "
            f"(it emits {sorted(emits)})",
        )
    distinct = entry.get("distinct", True)
    if not isinstance(distinct, bool):
        raise FluxError(code, f"{at}.distinct is true or false")
    return AssociationEntry(
        emit=nfc(emit),
        relationships=_relationships(entry.get("relationships"), at, code),
        distinct=distinct,
    )


def read_associations_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode
) -> Tuple[AssociationEntry, ...]:
    """Read an ``associations`` block, or refuse it with *code*, naming where.

    *emits* is every ``exposeId`` the Simulation emits. Refused: a block that is not a list, more
    than 4 096 entries, an entry that is not a mapping or carries a key other than ``emit``,
    ``relationships`` and ``distinct``, an emit the Simulation does not emit, ``relationships``
    that are not two distinct names, a ``distinct`` that is not a boolean, and an emit named by
    two entries (an emit is one association at most). Whether the emit's contract declares the relationships is
    checked where the contract is known (:func:`flux_engine.seam.resolve.resolve_emit`), and
    whether they can be laid out together where the plans are related
    (:func:`flux_engine.generator.relations.relate_plans`).
    """
    if not isinstance(block, Sequence) or isinstance(block, (str, bytes)):
        raise FluxError(code, f"{where} is a list of {{emit, relationships}} entries")
    if len(block) > _MOST_ASSOCIATIONS:
        raise FluxError(code, f"{where} holds more than {_MOST_ASSOCIATIONS} entries")
    found: Dict[str, AssociationEntry] = {}
    for index, raw in enumerate(block):
        at = f"{where}[{index}]"
        entry = _read_entry(raw, at, emits, code)
        if entry.emit in found:
            raise FluxError(code, f"{at} declares a second association of {entry.emit!r}")
        found[entry.emit] = entry
    return tuple(found[emit] for emit in sorted(found))


def bound_into_h(entries: Sequence[AssociationEntry], emit: str) -> Optional[List[Dict[str, Any]]]:
    """The member of *emit*'s entry for ``H`` that its association makes, or ``None``."""
    wanted = nfc(emit)
    member = [
        {"relationships": list(entry.relationships), "distinct": entry.distinct}
        for entry in entries
        if entry.emit == wanted
    ]
    return member or None


def association_entries_of(
    extension: Optional[Mapping[str, Any]], emit: str
) -> Tuple[AssociationEntry, ...]:
    """The association an emit's bound extension holds (the member :func:`bound_into_h` made).

    The names are read in NFC, as :func:`read_associations_block` reads them, so an entry taken
    from the namespace as authored names the relationships the contract declares.
    """
    raw = extension.get(ASSOCIATIONS_KEY) if extension is not None else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    found: List[AssociationEntry] = []
    for item in raw:
        if isinstance(item, Mapping):
            first, second = item["relationships"]
            found.append(
                AssociationEntry(
                    emit=nfc(emit),
                    relationships=(nfc(str(first)), nfc(str(second))),
                    distinct=bool(item.get("distinct", True)),
                )
            )
    return tuple(found)
