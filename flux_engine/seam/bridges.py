# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The bridge tables a Simulation declares: pairs that never repeat (ADR 0022 section 8).

A Simulation declares them under the top-level ``bridges`` key of its namespace, in the FLUX
bundle and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        bridges:
          - { emit: enrolment, relationships: [student, course] }

An entry names an emitted expose and two distinct relationships it declares. Under kernel 0.2.0
the two are laid out together as one key of two parts (KERNEL-SPEC section 8.2), so no pair of
parent rows repeats and each side names a row its parent holds.

**A bridge changes the rows the emit generates**, so it is bound into ``H`` before any row is
drawn: :func:`bound_into_h` is the member the emit's entry for ``H`` gains, present only when the
emit has entries, so a bundle that declares none hashes exactly as before. ``G`` leaves the member
out, as it leaves out the facts' and the links'.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError

__all__ = [
    "BRIDGES_KEY",
    "BridgeEntry",
    "bound_into_h",
    "bridge_entries_of",
    "read_bridges_block",
]

#: The top-level key of the namespace that declares bridges, and the member an emit's ``H`` gains.
BRIDGES_KEY = "bridges"

_ENTRY_KEYS = ("emit", "relationships")
_MOST_BRIDGES = 4096


@dataclass(frozen=True)
class BridgeEntry:
    """One bridge: the emit and its two relationships, the first of which is the major part."""

    emit: str
    relationships: Tuple[str, str]


def read_bridges_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode
) -> Tuple[BridgeEntry, ...]:
    """Read a ``bridges`` block, or refuse it with *code*, naming where.

    *emits* is every ``exposeId`` the Simulation emits. Refused: a block that is not a list, more
    than 4 096 entries, an entry that is not a mapping or carries a key other than ``emit`` and
    ``relationships``, an emit the Simulation does not emit, a name that is not a non-empty
    string, ``relationships`` that are not two distinct names, and a bridge declared twice.
    Whether the emit's contract declares the relationships is checked where the contract is known
    (:func:`flux_engine.seam.resolve.resolve_emit`), and whether they can be laid out together
    where the plans are related (:func:`flux_engine.generator.relations.relate_plans`).
    """
    if not isinstance(block, Sequence) or isinstance(block, (str, bytes)):
        raise FluxError(code, f"{where} is a list of {{emit, relationships}} entries")
    if len(block) > _MOST_BRIDGES:
        raise FluxError(code, f"{where} holds more than {_MOST_BRIDGES} entries")
    found: Dict[Tuple[str, Tuple[str, str]], BridgeEntry] = {}
    for index, entry in enumerate(block):
        at = f"{where}[{index}]"
        if not isinstance(entry, Mapping):
            raise FluxError(code, f"{at} is a mapping with emit and relationships")
        unknown = sorted(str(key) for key in entry if str(key) not in _ENTRY_KEYS)
        if unknown:
            raise FluxError(code, f"{at} declares {unknown}; a bridge carries {list(_ENTRY_KEYS)}")
        emit = entry.get("emit")
        if not isinstance(emit, str) or not emit:
            raise FluxError(code, f"{at}.emit is a non-empty string")
        if nfc(emit) not in emits:
            raise FluxError(
                code,
                f"{at}.emit names {nfc(emit)!r}, which this Simulation does not emit "
                f"(it emits {sorted(emits)})",
            )
        raw = entry.get("relationships")
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes))
            or len(raw) != 2
            or not all(isinstance(name, str) and name for name in raw)
        ):
            raise FluxError(code, f"{at}.relationships is a list of two relationship names")
        first, second = nfc(raw[0]), nfc(raw[1])
        if first == second:
            raise FluxError(code, f"{at}.relationships names {first!r} twice; a bridge is two")
        key = (nfc(emit), (first, second))
        if key in found:
            raise FluxError(code, f"{at} declares the bridge {first!r}, {second!r} twice")
        found[key] = BridgeEntry(emit=nfc(emit), relationships=(first, second))
    return tuple(found[key] for key in sorted(found))


def bound_into_h(entries: Sequence[BridgeEntry], emit: str) -> Optional[List[Dict[str, Any]]]:
    """The member of *emit*'s entry for ``H`` that its bridges make, or ``None``.

    One ``{"relationships": [a, b]}`` mapping per bridge of *emit*, in declared-name order;
    ``None`` when it has none, so the entry gains no key.
    """
    wanted = nfc(emit)
    member = [
        {"relationships": list(entry.relationships)}
        for entry in sorted(entries, key=lambda item: item.relationships)
        if entry.emit == wanted
    ]
    return member or None


def bridge_entries_of(extension: Optional[Mapping[str, Any]], emit: str) -> Tuple[BridgeEntry, ...]:
    """The bridges an emit's bound extension holds (the member :func:`bound_into_h` made)."""
    raw = extension.get(BRIDGES_KEY) if extension is not None else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    found: List[BridgeEntry] = []
    for item in raw:
        if isinstance(item, Mapping):
            first, second = item["relationships"]
            found.append(BridgeEntry(emit=nfc(emit), relationships=(str(first), str(second))))
    return tuple(found)
