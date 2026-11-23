# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The counted layouts a Simulation declares: children per parent are counted (ADR 0016 section 1).

A Simulation declares them under the top-level ``counts`` key of its namespace, in the FLUX
bundle and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        counts:
          - emit: invoice_line
            relationship: invoice
            minChildren: 1
            parentWhen: "status <> 'void'"
            children: { bins: ["1", "2", "4", "10"], binWeights: ["0.40", "0.35", "0.25"] }
            tolerance: "0.10"
          - emit: invoice
            relationship: account
            children: { bins: ["0", "1", "3", "10"], binWeights: ["0.2", "0.5", "0.3"] }
            given: segment
            childrenGiven:
              business: ["0.05", "0.35", "0.60"]

An entry names an emitted expose and a relationship it declares, and puts that relationship on the
counted layout (KERNEL-SPEC-2 section 8.1): the child's rows take their parents from a layout that
gives each parent a whole number of children, in place of a pick for each row. ``minChildren``,
a whole number (default 0), is the least number of children of every parent that may have any.
``parentWhen`` is a FLUID SQL/1 predicate over the parent's own columns: only a parent whose
final row makes it TRUE has children. ``children`` is the
shape of the number of children per parent, in the fidelity block's own spelling: ``levels`` with
``levelWeights``, or ``bins`` with ``binWeights``, over whole counts. ``tolerance`` is the largest
total variation distance the children-per-parent check allows. ``given`` names a column of the
parent expose, a ``category`` column with declared levels or a column drawn from bins, and
``childrenGiven`` maps each class of it (a level, or the text of a bin's lower edge) to the weights
of the classes ``children`` declares, in the same order; a class with no list takes ``children``'s
weights (KERNEL-SPEC-2 section 8.1). ``children`` is required with ``given``.

**An entry changes the rows the emit generates**, so it is bound into ``H`` before any row is
drawn: :func:`bound_into_h` is the member the emit's entry for ``H`` gains, present only when the
emit has entries, so a bundle that declares none hashes exactly as before. ``G`` leaves the member
out, as it leaves out the links' (:func:`flux_engine.seam.emit.seam_reusable_digest`).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError
from flux_engine.seam.scorecard import _operand

__all__ = [
    "COUNTS_KEY",
    "ChildShape",
    "CountsEntry",
    "bound_into_h",
    "count_entries_of",
    "read_counts_block",
]

#: The top-level key of the namespace that declares counted layouts, and the member an emit's
#: ``H`` gains.
COUNTS_KEY = "counts"

_ENTRY_KEYS = (
    "emit",
    "relationship",
    "minChildren",
    "parentWhen",
    "children",
    "tolerance",
    "given",
    "childrenGiven",
)
_MOST_MINIMUM = 1 << 62
_MOST_COUNTS = 4096
_MOST_CLASSES = 4096
_LEVEL_KEYS = ("levels", "levelWeights")
_BIN_KEYS = ("bins", "binWeights")


@dataclass(frozen=True)
class ChildShape:
    """The shape of the number of children per parent, as written: texts, not yet numbers.

    ``kind`` is ``levels`` or ``bins``; ``values`` are the levels or the bin edges and
    ``weights`` the class weights. Whether they are whole counts in ascending order, and weights
    that sum to 1, is checked where the plans are related
    (:func:`flux_engine.generator.counts.classes_of`).
    """

    kind: str
    values: Tuple[str, ...]
    weights: Tuple[str, ...]

    def as_member(self) -> Dict[str, Any]:
        """The shape as the entry holds it: ``{levels, levelWeights}`` or ``{bins, binWeights}``."""
        keys = _LEVEL_KEYS if self.kind == "levels" else _BIN_KEYS
        return {keys[0]: list(self.values), keys[1]: list(self.weights)}


@dataclass(frozen=True)
class CountsEntry:
    """One relationship's counted layout, as declared."""

    emit: str
    relationship: str
    children: Optional[ChildShape] = None
    #: ``minChildren``: the least number of children of every parent; ``None`` when not declared.
    min_children: Optional[int] = None
    #: ``parentWhen``: a FLUID SQL/1 predicate over the parent's own columns, as written (NFC).
    parent_when: Optional[str] = None
    #: The largest total variation distance the children-per-parent check allows, as written.
    tolerance: Optional[str] = None
    #: ``given``: the column of the parent expose whose class sets the weights of the shape (NFC).
    given: Optional[str] = None
    #: ``childrenGiven``: a weight list over the shape's classes for each class of the given
    #: column, as written, sorted by class; ``None`` when not declared.
    children_given: Optional[Tuple[Tuple[str, Tuple[str, ...]], ...]] = None

    def as_member(self) -> Dict[str, Any]:
        """The entry as the emit's ``counts`` member holds it: no ``emit``, no absent key."""
        member: Dict[str, Any] = {"relationship": self.relationship}
        if self.min_children is not None:
            member["minChildren"] = self.min_children
        if self.parent_when is not None:
            member["parentWhen"] = self.parent_when
        if self.children is not None:
            member["children"] = self.children.as_member()
        if self.tolerance is not None:
            member["tolerance"] = self.tolerance
        if self.given is not None:
            member["given"] = self.given
        if self.children_given is not None:
            member["childrenGiven"] = {key: list(weights) for key, weights in self.children_given}
        return member


def _texts(raw: Any, at: str, code: ErrorCode) -> Tuple[str, ...]:
    """A non-empty list of strings (at most 4 096), as a tuple."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise FluxError(code, f"{at} is a non-empty list of decimal strings")
    if len(raw) > _MOST_CLASSES:
        raise FluxError(code, f"{at} holds more than {_MOST_CLASSES} entries")
    if not all(isinstance(item, str) and item for item in raw):
        raise FluxError(code, f"{at} holds an entry that is not a string: write each as '2'")
    return tuple(raw)


def _shape(raw: Any, at: str, code: ErrorCode) -> ChildShape:
    """A ``children`` mapping: exactly one of ``{levels, levelWeights}`` and ``{bins, binWeights}``."""
    if not isinstance(raw, Mapping):
        raise FluxError(
            code, f"{at} is a mapping of levels and levelWeights, or of bins and binWeights"
        )
    keys = {str(key) for key in raw}
    if keys == set(_LEVEL_KEYS):
        kind, names = "levels", _LEVEL_KEYS
    elif keys == set(_BIN_KEYS):
        kind, names = "bins", _BIN_KEYS
    else:
        raise FluxError(
            code,
            f"{at} declares {sorted(keys)}; it carries exactly {list(_LEVEL_KEYS)} or "
            f"{list(_BIN_KEYS)}",
        )
    return ChildShape(
        kind=kind,
        values=_texts(raw[names[0]], f"{at}.{names[0]}", code),
        weights=_texts(raw[names[1]], f"{at}.{names[1]}", code),
    )


def _minimum(raw: Any, at: str, code: ErrorCode) -> int:
    """``minChildren``: a whole number of at least 0, as an integer or as decimal text."""
    value: Optional[int] = None
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 19:
        value = int(raw)
    elif isinstance(raw, Decimal) and raw.is_finite() and raw == raw.to_integral_value():
        value = int(raw)
    if value is None or not 0 <= value <= _MOST_MINIMUM:
        raise FluxError(code, f"{at}.minChildren is a whole number of 0 or more, such as 1")
    return value


def _name(entry: Mapping[str, Any], key: str, at: str, code: ErrorCode) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise FluxError(code, f"{at}.{key} is a non-empty string")
    return nfc(value)


def read_counts_block(
    block: Any, *, emits: FrozenSet[str], where: str, code: ErrorCode
) -> Tuple[CountsEntry, ...]:
    """Read a ``counts`` block, or refuse it with *code*, naming where.

    *emits* is every ``exposeId`` the Simulation emits. Refused: a block that is not a list,
    more than 4 096 entries, an entry that is not a mapping or carries a key other than ``emit``,
    ``relationship``, ``minChildren``, ``parentWhen``, ``children``, ``tolerance``, ``given`` and
    ``childrenGiven``, an emit the Simulation does not emit, a
    name that is not a non-empty string, a relationship declared twice, a ``children`` that is
    not exactly one of ``{levels, levelWeights}`` and ``{bins, binWeights}`` over lists of
    strings, a ``minChildren`` that is not a whole number of 0 or more, a ``parentWhen`` that is not a non-empty text, and a ``tolerance`` that is not a decimal text between 0 and 1. Whether the emit's
    contract declares the relationship is checked where the contract is known
    (:func:`flux_engine.seam.resolve.resolve_emit`), and whether the shape and the relationship
    can be laid out where the plans are related
    (:func:`flux_engine.generator.relations.relate_plans`).
    """
    if not isinstance(block, Sequence) or isinstance(block, (str, bytes)):
        raise FluxError(code, f"{where} is a list of {{emit, relationship, children}} entries")
    if len(block) > _MOST_COUNTS:
        raise FluxError(code, f"{where} holds more than {_MOST_COUNTS} entries")
    found: Dict[Tuple[str, str], CountsEntry] = {}
    for index, entry in enumerate(block):
        read = _read_entry(entry, f"{where}[{index}]", emits, code)
        if (read.emit, read.relationship) in found:
            raise FluxError(
                code,
                f"{where}[{index}] declares the relationship {read.emit}.{read.relationship} twice",
            )
        found[(read.emit, read.relationship)] = read
    return tuple(found[key] for key in sorted(found))


def _read_entry(entry: Any, at: str, emits: FrozenSet[str], code: ErrorCode) -> CountsEntry:
    """Read one ``counts`` entry found at *at*, or refuse it with *code*."""
    if not isinstance(entry, Mapping):
        raise FluxError(code, f"{at} is a mapping with emit, relationship and a layout")
    unknown = sorted(str(key) for key in entry if str(key) not in _ENTRY_KEYS)
    if unknown:
        raise FluxError(
            code, f"{at} declares {unknown}; a counts entry carries {list(_ENTRY_KEYS)}"
        )
    emit = _name(entry, "emit", at, code)
    relationship = _name(entry, "relationship", at, code)
    if emit not in emits:
        raise FluxError(
            code,
            f"{at}.emit names {emit!r}, which this Simulation does not emit "
            f"(it emits {sorted(emits)})",
        )
    return _read_entry_body(entry, at, emit, relationship, code)


def _read_entry_body(
    entry: Mapping[str, Any], at: str, emit: str, relationship: str, code: ErrorCode
) -> CountsEntry:
    """The shape, minimum, parent condition and tolerance of one entry, each only when declared."""
    children = _shape(entry["children"], f"{at}.children", code) if "children" in entry else None
    minimum = _minimum(entry["minChildren"], at, code) if "minChildren" in entry else None
    condition = entry.get("parentWhen")
    if "parentWhen" in entry and (not isinstance(condition, str) or not condition.strip()):
        raise FluxError(code, f"{at}.parentWhen is a FLUID SQL/1 predicate text")
    tolerance: Optional[str] = None
    if "tolerance" in entry:
        _, tolerance = _operand(entry["tolerance"], f"{at}.tolerance", code)
    return CountsEntry(
        emit=emit,
        relationship=relationship,
        children=children,
        min_children=minimum,
        parent_when=nfc(condition) if isinstance(condition, str) else None,
        tolerance=tolerance,
        given=_given(entry["given"], at, code) if "given" in entry else None,
        children_given=(
            _children_given(entry["childrenGiven"], f"{at}.childrenGiven", code)
            if "childrenGiven" in entry
            else None
        ),
    )


def _given(raw: Any, at: str, code: ErrorCode) -> str:
    """``given``: the name of one column of the parent expose."""
    if not isinstance(raw, str) or not raw:
        raise FluxError(code, f"{at}.given is the name of one column of the parent expose")
    return nfc(raw)


def _children_given(raw: Any, at: str, code: ErrorCode) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """``childrenGiven``: a mapping from a class of the given column to a list of weight texts."""
    if not isinstance(raw, Mapping):
        raise FluxError(
            code, f"{at} is a mapping from each class of the given column to its weights"
        )
    if len(raw) > _MOST_CLASSES:
        raise FluxError(code, f"{at} holds more than {_MOST_CLASSES} classes")
    found: Dict[str, Tuple[str, ...]] = {}
    for key, weights in raw.items():
        if not isinstance(key, str) or not key:
            raise FluxError(code, f"{at} names a class that is not a non-empty string")
        name = nfc(key)
        if name in found:
            raise FluxError(code, f"{at} names the class {name!r} twice")
        # An empty list is read here and refused where the classes are known (no weight).
        empty = (
            isinstance(weights, Sequence) and not isinstance(weights, (str, bytes)) and not weights
        )
        found[name] = () if empty else _texts(weights, f"{at}.{name}", code)
    return tuple(sorted(found.items()))


def bound_into_h(entries: Sequence[CountsEntry], emit: str) -> Optional[List[Dict[str, Any]]]:
    """The member of *emit*'s entry for ``H`` that its counts entries make, or ``None``.

    One ``{"relationship": <name>, "minChildren": <int>, "parentWhen": <text>, "children": {...}, "tolerance": <text>, "given": <column>, "childrenGiven": {...}}`` mapping per entry
    of *emit*, in relationship order, each key only when declared; ``None`` when it has none, so
    the entry gains no key.
    """
    wanted = nfc(emit)
    member = [
        entry.as_member()
        for entry in sorted(entries, key=lambda item: item.relationship)
        if entry.emit == wanted
    ]
    return member or None


def count_entries_of(extension: Optional[Mapping[str, Any]], emit: str) -> Tuple[CountsEntry, ...]:
    """The counts entries an emit's bound extension holds (the member :func:`bound_into_h` made)."""
    raw = extension.get(COUNTS_KEY) if extension is not None else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    found: List[CountsEntry] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        minimum = item.get("minChildren")
        condition = item.get("parentWhen")
        tolerance = item.get("tolerance")
        given = item.get("given")
        found.append(
            CountsEntry(
                emit=nfc(emit),
                relationship=str(item["relationship"]),
                children=_bound_shape(item.get("children")),
                min_children=minimum if isinstance(minimum, int) else None,
                parent_when=condition if isinstance(condition, str) else None,
                tolerance=tolerance if isinstance(tolerance, str) else None,
                given=given if isinstance(given, str) else None,
                children_given=_bound_children_given(item.get("childrenGiven")),
            )
        )
    return tuple(found)


def _bound_children_given(raw: Any) -> Optional[Tuple[Tuple[str, Tuple[str, ...]], ...]]:
    """The ``childrenGiven`` a bound entry holds, or ``None`` when it has none."""
    if not isinstance(raw, Mapping):
        return None
    return tuple(
        sorted((str(key), tuple(str(text) for text in weights)) for key, weights in raw.items())
    )


def _bound_shape(children: Any) -> Optional[ChildShape]:
    """The child shape a bound entry's ``children`` mapping holds, or ``None`` when it has none."""
    if not isinstance(children, Mapping):
        return None
    kind, names = ("levels", _LEVEL_KEYS) if "levels" in children else ("bins", _BIN_KEYS)
    return ChildShape(
        kind=kind,
        values=tuple(str(text) for text in children[names[0]]),
        weights=tuple(str(text) for text in children[names[1]]),
    )
