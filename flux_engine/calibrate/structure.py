# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Suggest which column each column is drawn "given", from aggregate tables alone.

The suggestion is a Chow-Liu tree: the maximum spanning tree of pairwise
mutual information (C. K. Chow and C. N. Liu, "Approximating discrete
probability distributions with dependence trees", IEEE Transactions on
Information Theory 14(3), 1968). It is the selection step of the MST
synthesizer (R. McKenna, G. Miklau and D. Sheldon, "Winning the NIST
Contest: A scalable and general approach to differentially private
synthetic data", 2021, arXiv 2108.04978), but without privacy noise: this
module is offline and reads aggregate pairwise count tables only
(:class:`PairTable`), never records. A committed S holds those tables:
a grouped ``levelHistogram``'s (group, level) counts for two categorical
columns (:func:`pair_table_from_level_groups`), and a grouped
``marginalHistogram``'s (group, bin) counts for a categorical and a
numeric one (:func:`pair_table_from_groups`).

Everything numeric is computed with :mod:`decimal` under one explicit
local context, never the ambient one and never a float, so a result is
the same on every platform (``Decimal.ln`` is correctly rounded).
Every refusal is ``FluxError(FLX_3402, ...)``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError
from flux_engine.generator.statistics import FLX_3402

__all__ = [
    "PairTable",
    "TreeEdge",
    "declarations",
    "mutual_information",
    "pair_table_from_groups",
    "pair_table_from_level_groups",
    "suggest_tree",
]

_PRECISION = 40
_SHARE_PLACES = 20


def _context() -> Context:
    return Context(prec=_PRECISION, rounding=ROUND_HALF_EVEN)


def _refuse(message: str) -> FluxError:
    return FluxError(FLX_3402, message)


@dataclass(frozen=True)
class PairTable:
    """A two-way table of counts for one pair of columns.

    ``counts`` is keyed by ``(level of columns[0], level of columns[1])``.
    A numeric (histogram) column's levels are its bin indices written as
    decimal strings. Names and levels are normalised to NFC on
    construction; two keys that normalise onto one cell are refused, as the
    generator refuses two spellings of one level.
    """

    columns: Tuple[str, str]
    counts: Mapping[Tuple[str, str], int]

    def __post_init__(self) -> None:
        cols = tuple(self.columns)
        if len(cols) != 2 or not all(isinstance(c, str) for c in cols):
            raise _refuse("a pair table names exactly two columns, as strings")
        first, second = nfc(cols[0]), nfc(cols[1])
        if first == second:
            raise _refuse(f"a pair table needs two distinct columns, got {first!r} twice")
        merged: Dict[Tuple[str, str], int] = {}
        for key, count in dict(self.counts).items():
            if not (isinstance(key, tuple) and len(key) == 2):
                raise _refuse(f"table {first!r}/{second!r}: a key must be a pair of levels")
            if not all(isinstance(level, str) for level in key):
                raise _refuse(f"table {first!r}/{second!r}: levels must be strings")
            if isinstance(count, bool) or not isinstance(count, int):
                raise _refuse(f"table {first!r}/{second!r}: count {count!r} is not an integer")
            if count < 0:
                raise _refuse(f"table {first!r}/{second!r}: count {count!r} is negative")
            norm = (nfc(key[0]), nfc(key[1]))
            if norm in merged:
                raise _refuse(f"table {first!r}/{second!r}: two keys normalise onto {norm!r}")
            merged[norm] = count
        if sum(merged.values()) <= 0:
            raise _refuse(f"table {first!r}/{second!r}: the total count must be positive")
        object.__setattr__(self, "columns", (first, second))
        object.__setattr__(self, "counts", merged)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def margin(self, column: str) -> Dict[str, int]:
        """The one-way margin of *column* (level -> count), zero levels kept."""
        index = self.columns.index(column)
        out: Dict[str, int] = {}
        for key, count in self.counts.items():
            out[key[index]] = out.get(key[index], 0) + count
        return out


def pair_table_from_groups(
    group_column: str, numeric_column: str, groups: Iterable[Any]
) -> PairTable:
    """Build a table from per-group histograms (duck-typed).

    Each element has ``.group`` (a str level of *group_column*) and
    ``.bin_counts`` (a sequence of ints). Counts are keyed
    ``(group, str(bin_index))``; every group must have the same number of bins.
    """
    counts: Dict[Tuple[str, str], int] = {}
    bins: Optional[int] = None
    for item in groups:
        level = getattr(item, "group", None)
        bin_counts = getattr(item, "bin_counts", None)
        if not isinstance(level, str):
            raise _refuse("a group needs a string .group level")
        if bin_counts is None:
            raise _refuse(f"group {level!r} has no .bin_counts")
        row = list(bin_counts)
        if not row:
            raise _refuse(f"group {level!r} has no bins")
        if bins is None:
            bins = len(row)
        elif len(row) != bins:
            raise _refuse(f"group {level!r} has {len(row)} bins, expected {bins}")
        level = nfc(level)
        if any(key[0] == level for key in counts):
            raise _refuse(f"group {level!r} appears twice")
        for index, count in enumerate(row):
            counts[(level, str(index))] = count
    if not counts:
        raise _refuse("no groups given")
    return PairTable((group_column, numeric_column), counts)


def pair_table_from_level_groups(
    group_column: str, column: str, levels: Iterable[str], groups: Iterable[Any]
) -> PairTable:
    """Build a two-categorical table from a grouped level histogram's groups (duck-typed).

    The categorical twin of :func:`pair_table_from_groups`, for the groups of
    a ``levelHistogram`` over *column* grouped by *group_column*. Each element
    of *groups* has ``.group`` (a str level of *group_column*) and
    ``.level_counts`` (one int per level of *column*). *levels* are the
    column's declared support levels, in any order: S counts them in one order,
    NFC-normalised and sorted, and they are sorted the same way here to name
    the counts. Counts are keyed ``(group, level)``, and a level no group holds
    is kept as a cell of zero.
    """
    names: List[str] = []
    for declared in levels:
        if not isinstance(declared, str):
            raise _refuse(f"column {column!r}: level {declared!r} is not a string")
        names.append(nfc(declared))
    if not names:
        raise _refuse(f"column {column!r} declares no level")
    if len(set(names)) != len(names):
        raise _refuse(f"column {column!r}: two declared levels normalise onto one")
    ordered = sorted(names)
    counts: Dict[Tuple[str, str], int] = {}
    seen: Set[str] = set()
    for item in groups:
        group = getattr(item, "group", None)
        level_counts = getattr(item, "level_counts", None)
        if not isinstance(group, str):
            raise _refuse("a group needs a string .group level")
        if level_counts is None:
            raise _refuse(f"group {group!r} has no .level_counts")
        row = list(level_counts)
        if len(row) != len(ordered):
            raise _refuse(
                f"group {group!r} has {len(row)} level counts, and {column!r} declares "
                f"{len(ordered)} levels"
            )
        group = nfc(group)
        if group in seen:
            raise _refuse(f"group {group!r} appears twice")
        seen.add(group)
        for name, count in zip(ordered, row, strict=True):
            counts[(group, name)] = count
    if not counts:
        raise _refuse("no groups given")
    return PairTable((group_column, column), counts)


def mutual_information(table: PairTable) -> Decimal:
    """The mutual information of *table*, in nats, as an exact-context Decimal.

    ``I = sum over cells with n_ab > 0 of (n_ab / n) * ln(n_ab * n / (n_a * n_b))``,
    computed under ``Context(prec=40, rounding=ROUND_HALF_EVEN)`` whatever the
    ambient context is. An exactly independent table returns ``Decimal(0)``.
    """
    with localcontext(_context()):
        n = table.total
        rows = table.margin(table.columns[0])
        cols = table.margin(table.columns[1])
        # Cells with the same ratio share one ln, so rounding enters once per
        # distinct ratio rather than once per cell.
        weight: Dict[Decimal, int] = {}
        for (a, b), count in table.counts.items():
            if count == 0:
                continue
            ratio = Decimal(count * n) / Decimal(rows[a] * cols[b])
            if ratio != 1:
                weight[ratio] = weight.get(ratio, 0) + count
        total = Decimal(0)
        for ratio in sorted(weight):
            total += Decimal(weight[ratio]) * ratio.ln()
        if total <= 0:
            return Decimal(0)
        return total / Decimal(n)


@dataclass(frozen=True)
class TreeEdge:
    """One edge of the suggested tree: *child* is drawn given *parent*."""

    parent: str
    child: str
    mutual_information: Decimal


def _normalise_sets(
    categorical: Iterable[str], numeric: Iterable[str]
) -> Tuple[Set[str], Set[str]]:
    cat = {nfc(c) for c in categorical}
    num = {nfc(c) for c in numeric}
    both = sorted(cat & num)
    if both:
        raise _refuse(f"column {both[0]!r} is declared both categorical and numeric")
    return cat, num


def _check_tables(tables: Sequence[PairTable], known: Set[str]) -> None:
    """Refuse tables that do not come from one committed state."""
    totals = {t.total for t in tables}
    if len(totals) > 1:
        raise _refuse(f"the tables have different totals: {sorted(totals)}")
    margins: Dict[str, Dict[str, int]] = {}
    pairs: Set[Tuple[str, str]] = set()
    for table in tables:
        pair = (min(table.columns), max(table.columns))
        if pair in pairs:
            raise _refuse(f"two tables pair {pair[0]!r} with {pair[1]!r}")
        pairs.add(pair)
        for column in table.columns:
            if column not in known:
                raise _refuse(f"column {column!r} is neither categorical nor numeric")
            margin = {k: v for k, v in table.margin(column).items() if v}
            if column in margins and margins[column] != margin:
                raise _refuse(f"the one-way margin of {column!r} differs between tables")
            margins[column] = margin


def _find(parent: Dict[str, str], node: str) -> str:
    while parent[node] != node:
        parent[node] = parent[parent[node]]
        node = parent[node]
    return node


def suggest_tree(
    tables: Iterable[PairTable],
    *,
    categorical: Iterable[str],
    numeric: Iterable[str] = (),
    root: Optional[str] = None,
) -> Tuple[TreeEdge, ...]:
    """Suggest the Chow-Liu tree under the constraint that numeric columns are leaves.

    Step 1 runs Kruskal's maximum spanning forest over the categorical
    columns, using only the categorical-categorical tables given. Step 2
    attaches each numeric column to the categorical column with the highest
    mutual information among the tables that pair them (numeric-numeric
    tables are ignored; a numeric column with no such table gets no edge).

    This is exactly the optimal tree under the leaf constraint. A leaf has
    degree one, so a tree in which every numeric column is a leaf is a
    spanning tree of the categorical columns plus one attaching edge per
    numeric column. Its total weight is the sum of the categorical tree's
    weight and the attaching edges' weights, and the two parts share no
    edge and no choice, so each is maximised on its own: Kruskal for the
    first, a per-column argmax for the second.

    Ties are deterministic: candidates sort by ``(-mutual_information,
    nfc-sorted pair of names)``, so the output depends on neither the order
    of *tables* nor the order within *categorical* or *numeric*. In each
    connected component of categorical columns the root is *root* if it lies
    there, else the NFC-smallest name; edges point away from it, and numeric
    edges point from their categorical column to the numeric one. Edges come
    back ordered by depth from the root, then by child name.

    Any root gives the same tree distribution (a tree factorises the same
    way whichever node is the root), so the root only decides which
    conditionals get declared.
    """
    cat, num = _normalise_sets(categorical, numeric)
    tbls = list(tables)
    _check_tables(tbls, cat | num)
    if root is not None:
        root = nfc(root)
        if root not in cat:
            raise _refuse(f"root {root!r} is not a categorical column")

    cat_candidates: List[Tuple[Decimal, Tuple[str, str]]] = []
    best_attach: Dict[str, Tuple[Decimal, str]] = {}
    for table in tbls:
        first, second = table.columns
        mi = mutual_information(table)
        if first in cat and second in cat:
            cat_candidates.append((mi, (min(first, second), max(first, second))))
        elif (first in cat) != (second in cat):
            attach, leaf = (first, second) if first in cat else (second, first)
            current = best_attach.get(leaf)
            if current is None or (-mi, attach) < (-current[0], current[1]):
                best_attach[leaf] = (mi, attach)

    cat_candidates.sort(key=lambda item: (-item[0], item[1]))
    uf = {name: name for name in cat}
    chosen: Dict[Tuple[str, str], Decimal] = {}
    for mi, (a, b) in cat_candidates:
        ra, rb = _find(uf, a), _find(uf, b)
        if ra != rb:
            uf[ra] = rb
            chosen[(a, b)] = mi

    adjacency: Dict[str, List[Tuple[str, Decimal]]] = {name: [] for name in cat}
    for (a, b), mi in chosen.items():
        adjacency[a].append((b, mi))
        adjacency[b].append((a, mi))

    depth: Dict[str, int] = {}
    ordered: List[Tuple[int, str, TreeEdge]] = []
    members: Dict[str, List[str]] = {}
    for name in sorted(cat):
        members.setdefault(_find(uf, name), []).append(name)
    for component in sorted(members.values()):
        start = root if root is not None and root in component else component[0]
        depth[start] = 0
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for other, mi in sorted(adjacency[node]):
                if other in depth:
                    continue
                depth[other] = depth[node] + 1
                ordered.append((depth[other], other, TreeEdge(node, other, mi)))
                queue.append(other)
    for leaf in sorted(best_attach):
        mi, attach = best_attach[leaf]
        ordered.append((depth[attach] + 1, leaf, TreeEdge(attach, leaf, mi)))

    ordered.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in ordered)


def _shares(margin: Mapping[str, int], n: int) -> Dict[str, str]:
    """Shares of the levels that occur, at 20 places; the residue goes to the NFC-last one."""
    quantum = Decimal(1).scaleb(-_SHARE_PLACES)
    live = sorted(level for level, count in margin.items() if count > 0)
    with localcontext(_context()):
        shares = {level: (Decimal(margin[level]) / Decimal(n)).quantize(quantum) for level in live}
        last = live[-1]
        others = sum((v for k, v in shares.items() if k != last), Decimal(0))
        shares[last] = (Decimal(1) - others).quantize(quantum)
    return {level: format(shares[level], "f") for level in live}


def declarations(
    edges: Sequence[TreeEdge],
    tables: Iterable[PairTable],
    *,
    categorical: Iterable[str],
    numeric: Iterable[str] = (),
    bins: Optional[Mapping[str, int]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Fidelity ``columns.<name>`` fragments for the suggested tree.

    *bins* names a numeric column's declared bin count. With it, a table may
    leave empty bins out, and every ``binWeightsGiven`` row still carries one
    weight per declared bin. Without it, a numeric table must name every bin
    index ``0..k-1``, as :func:`pair_table_from_groups` always does.

    Every number is a decimal string. A categorical root (no edge's child)
    gets ``{"levels": {level: share}}``; a categorical child gets
    ``{"given": parent, "levelWeightsGiven": {parent_level: {child_level:
    count}}}``; a numeric child gets ``{"given": parent, "binWeightsGiven":
    {parent_level: [count per bin]}}``. A numeric column with no edge gets
    nothing.

    Levels that never occur (a zero one-way margin) are left out: a root's
    levels, a categorical child's levels and every child table's parent rows
    name only the levels the committed state holds. Each row the generator
    can reach therefore carries mass, which it requires; a declared
    ``support`` may still list more levels than are drawn. A histogram child
    keeps every bin, empty ones at weight ``"0"``.
    """
    cat, num = _normalise_sets(categorical, numeric)
    tbls = list(tables)
    _check_tables(tbls, cat | num)
    by_pair = {frozenset(t.columns): t for t in tbls}
    out: Dict[str, Dict[str, Any]] = {}
    children: Set[str] = set()

    for edge in edges:
        parent, child = nfc(edge.parent), nfc(edge.child)
        if parent in num:
            raise _refuse(f"parent {parent!r} is numeric; numeric columns are leaves")
        if parent not in cat:
            raise _refuse(f"parent {parent!r} is not a known categorical column")
        if child not in cat and child not in num:
            raise _refuse(f"child {child!r} is neither categorical nor numeric")
        if child in children:
            raise _refuse(f"column {child!r} has two parents")
        table = by_pair.get(frozenset((parent, child)))
        if table is None:
            raise _refuse(f"no table pairs {parent!r} with {child!r}")
        children.add(child)
        flip = table.columns[0] != parent
        cells = {(b, a) if flip else (a, b): c for (a, b), c in table.counts.items()}
        parent_margin, child_margin = table.margin(parent), table.margin(child)
        parent_levels = sorted(level for level, count in parent_margin.items() if count > 0)
        if child in cat:
            child_levels = sorted(level for level, count in child_margin.items() if count > 0)
            out[child] = {
                "given": parent,
                "levelWeightsGiven": {
                    p: {c: str(cells.get((p, c), 0)) for c in child_levels} for p in parent_levels
                },
            }
            continue
        declared = None if bins is None else bins.get(child)
        count = len(child_margin) if declared is None else declared
        indices = {str(i) for i in range(count)}
        present = set(child_margin)
        if (present != indices) if declared is None else not (present <= indices):
            raise _refuse(f"numeric column {child!r}: levels are not bin indices 0..{count - 1}")
        out[child] = {
            "given": parent,
            "binWeightsGiven": {
                p: [str(cells.get((p, str(i)), 0)) for i in range(count)] for p in parent_levels
            },
        }

    for name in sorted(cat - children):
        source = next((t for t in tbls if name in t.columns), None)
        if source is None:
            raise _refuse(f"no table gives the margin of root {name!r}")
        out[name] = {"levels": _shares(source.margin(name), source.total)}
    return out
