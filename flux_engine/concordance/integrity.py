# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Referential integrity, scored for every declared relationship.

A relationship a contract declares (``semantics.relationships``, FLUID 0.7.7, read at
:mod:`flux_engine.seam.keys`) says that the ``fromColumns`` of a child row equal the
``toColumns`` of a row of the parent. Generation builds the keys so that this holds
(KERNEL-SPEC section 8), and until now nothing read the rows back to say so. For each
relationship this module counts, as a share that holds (:class:`~flux_engine.concordance.shares.Holds`):

* **found**: each child key is found among the parent's keys. A child row whose key has a NULL
  reaches nothing and is not counted here.
* **unique**: parent keys are unique. Counted over the parent rows whose key has no NULL, as the
  distinct keys over those rows. A versioned parent repeats a key by design, so it is not counted.
* **nulls**: NULLs appear only where ``joinType: left`` allows them. Under ``inner`` a child's
  key has no NULL, counted over the child's rows. Under ``left`` nothing is counted.
* **one to one**: a ``one_to_one`` parent has at most one child, counted over the parent keys that
  have a child. A ``many_to_one`` relationship counts nothing.

A relationship's score is the unweighted mean of the shares of the checks that cover at least one
row, as an exact fraction; it is ``None`` when none does. The definition is SDMetrics'
``ReferentialIntegrity`` (sdv-dev/SDMetrics 0.33.0, MIT): the share of child keys that exist in the
parent, used here as a pattern and not as a dependency, with the other three checks the contract's
declarations add.

Nothing here quotes a key: the result is counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, Hashable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from flux_engine.concordance.decoded import DecodedRows
from flux_engine.concordance.shares import Holds
from flux_engine.seam.keys import Relationship
from flux_stats.reading import ValueDomain

__all__ = [
    "CHECKS",
    "KeyColumns",
    "LinkIntegrity",
    "key_columns",
    "link_integrity",
    "link_integrity_columns",
]

#: The four checks, in the order a relationship reports them.
CHECKS: Tuple[str, ...] = ("found", "unique", "nulls", "oneToOne")


@dataclass(frozen=True)
class LinkIntegrity:
    """One declared relationship's shares. A check that does not apply is ``None``."""

    name: str
    child: str
    parent: str
    from_columns: Tuple[str, ...]
    to_columns: Tuple[str, ...]
    cardinality: str
    join_type: str
    found: Holds
    unique: Optional[Holds]
    nulls: Optional[Holds]
    one_to_one: Optional[Holds]
    #: How many parents have each number of children, as a mapping from a number of children to the
    #: parents with that many (only numbers some parent has; a parent whose key has no NULL), counted from the child rows that found their
    #: parent. ``None`` when the parent is versioned, which repeats a key by design. Not part of
    #: the relationship's document: the scorecard's children-per-parent row reads it.
    parents_by_children: Optional[Mapping[int, int]] = None

    @property
    def checks(self) -> Tuple[Tuple[str, Holds], ...]:
        """The checks that apply, by name."""
        every = (
            ("found", self.found),
            ("unique", self.unique),
            ("nulls", self.nulls),
            ("oneToOne", self.one_to_one),
        )
        return tuple((name, holds) for name, holds in every if holds is not None)

    @property
    def score(self) -> Optional[Fraction]:
        """The mean of the shares of the checks that cover a row, or ``None`` when none does."""
        shares = [holds.share for _, holds in self.checks if holds.share is not None]
        return sum(shares, Fraction(0)) / len(shares) if shares else None

    @property
    def whole(self) -> bool:
        """Whether every check holds on every row it covers."""
        return all(holds.whole for _, holds in self.checks)

    def to_document(self) -> Dict[str, Any]:
        """The relationship's row in a verify result and in the scorecard."""
        return {
            "relationship": self.name,
            "child": self.child,
            "parent": self.parent,
            "fromColumns": list(self.from_columns),
            "toColumns": list(self.to_columns),
            "cardinality": self.cardinality,
            "joinType": self.join_type,
            "checks": {name: holds.to_document() for name, holds in self.checks},
        }

    def line(self) -> str:
        """``store_sales.item (child store_sales, parent item): found 4000 of 4000 (100.00%); ...``."""
        parts = "; ".join(f"{name} {holds.text()}" for name, holds in self.checks)
        return f"{self.child}.{self.name} -> {self.parent}: {parts}"


def _positions(names: Sequence[str], columns: Sequence[str]) -> List[int]:
    return [names.index(column) for column in columns]


def link_integrity(
    link: Relationship,
    child_names: Sequence[str],
    child_rows: Sequence[Sequence[Any]],
    parent_names: Sequence[str],
    parent_rows: Sequence[Sequence[Any]],
    *,
    parent_versioned: bool = False,
) -> LinkIntegrity:
    """Count one relationship's four checks over the child's rows and the parent's.

    Values are compared as the emit spells them (KERNEL-SPEC section 9), which is one spelling
    for one value, so no value is decoded. Raises ``ValueError`` (naming a column, never a value)
    when a column of the relationship is not one of the emit's.
    """
    try:
        at_child = _positions(child_names, link.from_columns)
        at_parent = _positions(parent_names, link.to_columns)
    except ValueError:
        raise ValueError(
            f"relationship {link.name!r} names a column its emit does not carry"
        ) from None
    parent_keys: Dict[Tuple[Hashable, ...], int] = {}
    keyed_parents = 0
    for row in parent_rows:
        key = tuple(row[position] for position in at_parent)
        if None in key:
            continue
        keyed_parents += 1
        parent_keys[key] = parent_keys.get(key, 0) + 1
    found = covered = null_free = 0
    children_of: Dict[Tuple[Hashable, ...], int] = {}
    for row in child_rows:
        key = tuple(row[position] for position in at_child)
        if None in key:
            continue
        null_free += 1
        covered += 1
        if key in parent_keys:
            found += 1
            children_of[key] = children_of.get(key, 0) + 1
    spread: Optional[Mapping[int, int]] = None
    if not parent_versioned:
        per_parent: Dict[int, int] = {}
        for key, copies in parent_keys.items():
            number = children_of.get(key, 0)
            per_parent[number] = per_parent.get(number, 0) + copies
        spread = per_parent
    return LinkIntegrity(
        name=link.name,
        child=link.source,
        parent=link.target,
        from_columns=tuple(link.from_columns),
        to_columns=tuple(link.to_columns),
        cardinality=link.cardinality,
        join_type=link.join_type,
        parents_by_children=spread,
        found=Holds(found, covered),
        unique=None if parent_versioned else Holds(len(parent_keys), keyed_parents),
        nulls=Holds(null_free, len(child_rows)) if link.join_type != "left" else None,
        one_to_one=(
            Holds(sum(1 for count in children_of.values() if count <= 1), len(children_of))
            if link.cardinality == "one_to_one"
            else None
        ),
    )


# ---------------------------------------------------------------------------
# The same four checks, over a run's decoded columns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyColumns:
    """The columns of one key as an emit decoded them, ready to be compared with another emit's.

    Each part is a column as int64 codes: an integer or a decimal as its units at its scale
    (``alphabet`` is ``None``), a string as an index into ``alphabet``, its distinct values.
    ``-1`` marks a NULL, and ``present`` is true on the rows whose every column holds a value.
    Plain arrays and tuples, so a worker can hand it back to the process that joins the emits.
    """

    codes: Tuple["np.ndarray", ...]
    scales: Tuple[int, ...]
    alphabets: Tuple[Optional[Tuple[str, ...]], ...]
    present: "np.ndarray"

    @property
    def rows(self) -> int:
        return int(self.present.shape[0])


def key_columns(
    table: Any, columns: Sequence[str], types: Mapping[str, str]
) -> Optional[KeyColumns]:
    """The key *columns* of *table* as :class:`KeyColumns`, or ``None`` when they cannot be.

    ``None`` sends the caller to the row path, which reads every cell as it always did. Only an
    integer, a decimal and a string column are taken, by their declared type: a string that
    happens to look like a number is never read as one.
    """
    if not isinstance(table, DecodedRows) or any(not table.has(name) for name in columns):
        return None
    codes: List["np.ndarray"] = []
    scales: List[int] = []
    alphabets: List[Optional[Tuple[str, ...]]] = []
    present = np.ones(len(table), dtype=bool)
    for name in columns:
        declared = str(types.get(name, ""))
        if declared == "INT" or declared.startswith("DECIMAL"):
            exact = table.exact(name, ValueDomain.NUMBER)
            if exact is None:
                return None
            codes.append(exact.units)
            scales.append(exact.scale)
            alphabets.append(None)
            if exact.valid is not None:
                present &= exact.valid
        elif declared == "STRING":
            found = table.levels(name)
            if found is None:
                return None
            codes.append(found.codes)
            scales.append(0)
            alphabets.append(found.levels)
            present &= found.codes >= 0
        else:
            return None
    return KeyColumns(tuple(codes), tuple(scales), tuple(alphabets), present)


#: How far past twice their count the values of dense keys may spread and still be counted
#: by their offset from the least one.
_DENSE_SLACK = 1 << 16


def _joint(columns: List["np.ndarray"]) -> "np.ndarray":
    """One small int64 identity per row of the stacked columns: equal rows, equal identities.

    The identities are small whatever the keys are, so a count over them is never over the
    keys' magnitude: a key may be negative or near 1e13. Dense keys of one column, as a key
    laid out from the row index is, are their offset from the least one, without the sort
    :func:`numpy.unique` takes; any other keys are codes from 0 to the count of distinct rows.
    """
    if len(columns) == 1:
        keys = columns[0]
        if keys.size and keys.dtype.kind in "iu":
            low, high = int(keys.min()), int(keys.max())
            if high - low < 2 * keys.size + _DENSE_SLACK:
                return (keys - low).astype(np.int64)
        _, inverse = np.unique(keys, return_inverse=True)
    else:
        _, inverse = np.unique(np.stack(columns, axis=1), axis=0, return_inverse=True)
    return inverse.reshape(-1).astype(np.int64)


def _spread(matched: "np.ndarray", p_valid: "np.ndarray", size: int) -> Mapping[int, int]:
    """How many parents have each number of children, by the identities of the matched child rows."""
    # Each parent row's children, by its identity; the identities of the parents are all < size.
    each = np.bincount(matched, minlength=size)[p_valid] if p_valid.size else p_valid
    if not each.size:
        return {}
    held = np.bincount(each)
    return {int(n): int(held[n]) for n in np.flatnonzero(held)}


def link_integrity_columns(
    link: Relationship,
    child: KeyColumns,
    parent: KeyColumns,
    *,
    parent_versioned: bool = False,
) -> Optional[LinkIntegrity]:
    """:func:`link_integrity` over decoded key columns, or ``None`` when the two sides do not line up.

    The parent's strings are the alphabet the child's are read into, so equal strings get equal
    codes; a child string the parent lacks matches nothing. Decimals must share a scale and an
    integer column pairs only with another, else ``None``: the rows are then read as before.
    """
    if len(child.codes) != len(parent.codes):
        return None
    child_parts: List["np.ndarray"] = []
    parent_parts: List["np.ndarray"] = []
    for index in range(len(child.codes)):
        c_alpha, p_alpha = child.alphabets[index], parent.alphabets[index]
        if (c_alpha is None) != (p_alpha is None) or (
            c_alpha is None and child.scales[index] != parent.scales[index]
        ):
            return None
        mine = child.codes[index]
        if c_alpha is not None and p_alpha is not None:
            place = {label: position for position, label in enumerate(p_alpha)}
            lut = np.array([-1] + [place.get(label, -2) for label in c_alpha], dtype=np.int64)
            mine = lut[mine + 1]
        child_parts.append(mine)
        parent_parts.append(parent.codes[index])
    c_rows = child.rows
    both = _joint(
        [np.concatenate([child_parts[i], parent_parts[i]]) for i in range(len(child_parts))]
    )
    c_id, p_id = both[:c_rows], both[c_rows:]
    # A child code of -2 is a string the parent lacks: it must match nothing, whatever the
    # joint identity says, so such rows are marked unfound apart from the identities.
    unknown = np.zeros(c_rows, dtype=bool)
    for index in range(len(child_parts)):
        if child.alphabets[index] is not None:
            unknown |= child_parts[index] == -2
    p_valid = p_id[parent.present]
    parent_counts = np.bincount(p_valid) if p_valid.size else np.zeros(0, dtype=np.int64)
    distinct = int((parent_counts > 0).sum())
    covered = child.present
    c_valid = c_id[covered & ~unknown]
    known = np.zeros(int(both.max()) + 1 if both.size else 1, dtype=bool)
    known[p_valid] = True
    matched = c_valid[known[c_valid]] if c_valid.size else c_valid
    found = int(matched.size)
    children = np.bincount(matched) if matched.size else np.zeros(0, dtype=np.int64)
    spread = None if parent_versioned else _spread(matched, p_valid, known.size)
    return LinkIntegrity(
        name=link.name,
        parents_by_children=spread,
        child=link.source,
        parent=link.target,
        from_columns=tuple(link.from_columns),
        to_columns=tuple(link.to_columns),
        cardinality=link.cardinality,
        join_type=link.join_type,
        found=Holds(found, int(covered.sum())),
        unique=None if parent_versioned else Holds(distinct, int(parent.present.sum())),
        nulls=Holds(int(covered.sum()), c_rows) if link.join_type != "left" else None,
        one_to_one=(
            Holds(int((children == 1).sum()), int((children > 0).sum()))
            if link.cardinality == "one_to_one"
            else None
        ),
    )
