# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Association tables: two counted relationships paired stub by stub (ADR 0039, KERNEL-SPEC-2 section 8.7).

An association table holds two foreign keys, an enrolment's student and course say, and its rows
are the edges of a bipartite graph between the two parents. Each side's number of rows per parent
is that relationship's ``counts`` shape, and the rows pair the two sides by the bipartite
configuration model, the textbook one: each parent holds as many stubs as it has rows, and a row
joins a stub of each side. Every step is a pure function of the run root and the declaration, so
every process that needs a row's parents finds the same ones.

1. **Degrees.** Both sides are laid out by :func:`flux_engine.generator.counts.counted_layout`
   over the emit's ``E`` rows, so both sum to exactly ``E``.
2. **Caps** (``distinct`` only). A parent of one side has at most as many rows as the other side
   has parents. The rows above a cap go to the parents still under theirs, in proportion to their
   rows, by the largest remainders (:func:`flux_engine.generator.counts.allot`), and the plan
   records how many moved. More rows than pairs (``E > |A| * |B|``) is ``FLX-3403``.
3. **Gale-Ryser** (``distinct`` only). Two degree sequences are those of a simple bipartite graph
   if and only if, with the second sorted in decreasing order, the sum of its first ``k`` is at
   most ``sum_a min(d_a, k)`` for every ``k`` (Gale 1957, Ryser 1957). Where it fails, one row at a
   time moves from the second side's parent with the most rows to the one with the fewest (ties to
   the lower row) until it holds; an even spread always holds, so this ends, and the plan records
   the moves.
4. **Pairs.** Row ``r`` is the first side's stub ``r`` (its parents' runs in row order, as a
   counted layout lays them) and the second side's stub ``permute/2(K, E, r)``, whose parent is
   found by bisection over that side's offsets. ``K`` is the key of the path
   ``("sim", sim_id, "gen", exposeId, "associate", a, b, "stubs")``. With repeats allowed this is
   the whole layout, and a row's parents are read from its index alone, in any slice.
5. **Repair** (``distinct`` only). A row whose pair an earlier row already holds is repaired by a
   degree-preserving switch: with a partner row ``s`` holding ``(a', b')``, ``(a, b), (a', b')``
   becomes ``(a, b'), (a', b)`` when neither new pair is held. Partners are
   ``permute/2(K', E, r * 64 + t)`` for the attempts ``t < 64``, ``K'`` the key of the path ending
   ``"switches"``. The rows left go through a Havel-Hakimi pass over their own stubs (each first-side
   parent, most rows first, takes the second-side stubs with the most left that it does not already
   pair with); should that pass find no stub for a parent, the whole table is laid out again by
   Havel-Hakimi, which the Gale-Ryser condition guarantees. So the repair always ends, with no
   pair repeated. It reads every row: it is a global pass, made once per process and not per
   slice, so a slice's rows are the same in any process, but the pass costs ``O(E)`` memory.

The realised total variation distance between each side's parents counted into its shape's
classes and the shape's weights is recorded in the plan (:func:`degree_distance`), and ``verify``
checks it on the rows.

Prior art: the bipartite configuration model, Havel-Hakimi and the degree-preserving switch as
networkx 3.7 writes them (``networkx/algorithms/bipartite/generators.py``, ``swap.py``, BSD-3; read
as a reference, not a dependency): networkx draws a whole shuffled stub list from one random state
and so cannot give one row alone, and has no bounded, keyed repair. Chung-Lu and the maximum-entropy
bipartite configuration model meet the degrees only in expectation.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import cached_property
from math import lcm
from typing import AbstractSet, Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import numpy as np

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.permute import permute2
from flux_engine.core.seedtree import SeedTreeRoot
from flux_engine.generator.counts import (
    CountedLayout,
    allot,
    classes_of,
    count_key,
    counted_layout,
    digest_key,
)
from flux_engine.generator.paths import ASSOCIATE_PATH_SHAPE, associate_path
from flux_engine.seam.associations import AssociationEntry
from flux_engine.seam.counts import ChildShape, CountsEntry
from flux_engine.seam.keys import Relationship

__all__ = [
    "SWITCH_ATTEMPTS",
    "PairKeys",
    "PairedLayout",
    "PairedSide",
    "Pairing",
    "DEGREE_TOLERANCE",
    "capped",
    "degree_distance",
    "degree_overlap",
    "gale_ryser_gap",
    "pair_keys",
    "pair_layout",
    "paired_sides",
    "pairing_for",
    "realisable",
]

#: The largest total variation distance ``verify`` allows a side's rows per parent from its shape
#: when its ``counts`` entry declares no tolerance: the children-per-parent check's own default.
DEGREE_TOLERANCE = Fraction(1, 10)
#: The partner rows a repeated pair's switch tries before the Havel-Hakimi pass takes the row.
SWITCH_ATTEMPTS = 64
#: Pair codes ``a * |B| + b`` are int64: a table whose pairs do not fit is refused.
_MOST_PAIRS = 1 << 62


def _refuse(message: str) -> FluxError:
    """``FLX-3403`` (imported here: relations imports this module)."""
    from flux_engine.generator import relations

    return FluxError(relations.FLX_3403, message)


@dataclass(frozen=True)
class PairedSide:
    """What a counted pick of one side of an association carries, the other side's declaration too.

    ``side`` is ``0`` for the relationship the rows follow in order and ``1`` for the one paired
    through the stubs. Both sides' shapes, minimums and parent rows are held, so a change to either
    moves both sides' plan documents.
    """

    relationships: Tuple[str, str]
    side: int
    shapes: Tuple[Optional[ChildShape], Optional[ChildShape]]
    minima: Tuple[int, int]
    parents: Tuple[int, int]
    distinct: bool

    def as_document(self) -> Dict[str, Any]:
        """The ``association`` member of the side's ``counts`` document."""
        return {
            "relationships": list(self.relationships),
            "side": self.side,
            "distinct": self.distinct,
            "parentRows": list(self.parents),
            "minChildren": list(self.minima),
            "children": [None if shape is None else shape.as_member() for shape in self.shapes],
        }


@dataclass(frozen=True)
class PairKeys:
    """The keys an association reads: each side's counted layout's, the stubs' and the switches'."""

    counts: Tuple[bytes, bytes]
    stubs: bytes
    switches: bytes


@dataclass(frozen=True)
class Pairing:
    """An association's laid-out rows: each side's rows per parent and the second side's parents.

    ``second`` is the second side's parent of every row when the repair had to hold them all
    (``distinct``), else ``None``: the parent is then read from the stub of the row alone.
    ``moved`` is the rows the caps moved on each side and the rows the Gale-Ryser step moved;
    ``switches``, ``placed`` and ``rebuilt`` say what the repair did.
    """

    edges: int
    degrees: Tuple["np.ndarray", "np.ndarray"]
    stubs: bytes
    second: Optional["np.ndarray"] = None
    moved: Tuple[int, int, int] = (0, 0, 0)
    switches: int = 0
    placed: int = 0
    rebuilt: bool = False
    distances: Tuple[Optional[Fraction], Optional[Fraction]] = (None, None)

    @cached_property
    def offsets(self) -> Tuple["np.ndarray", "np.ndarray"]:
        """Each side's offsets: the rows before each parent's."""
        first, second = (np.cumsum(held) - held for held in self.degrees)
        return first, second

    def second_of(self, rows: "np.ndarray") -> "np.ndarray":
        """The second side's parent row of each of *rows*."""
        if self.second is not None:
            return self.second[rows]
        stubs = np.asarray(kernel2.permute2_array(self.stubs, self.edges, rows), dtype=np.int64)
        found: "np.ndarray" = np.searchsorted(self.offsets[1], stubs, side="right") - 1
        return found

    def side(self, index: int) -> CountedLayout:
        """The layout one side's foreign key reads: a counted layout, or the paired second side."""
        if index == 0:
            return CountedLayout(self.degrees[0], self.offsets[0], self.edges)
        return PairedLayout(self.degrees[1], self.offsets[1], self.edges, pairing=self)

    def report(self) -> Dict[str, Any]:
        """What the plan records of the pairing: the moves, the repair and each side's distance."""
        return {
            "rows": self.edges,
            "moved": {"caps": list(self.moved[:2]), "galeRyser": self.moved[2]},
            "repair": {"switches": self.switches, "placed": self.placed, "rebuilt": self.rebuilt},
            "distance": [None if gap is None else _decimal(gap) for gap in self.distances],
        }


@dataclass(frozen=True)
class PairedLayout(CountedLayout):
    """The second side of an association: its rows per parent, and each row's parent through the stubs.

    Its rows are not runs of consecutive rows, so it has no :meth:`children_of`.
    """

    pairing: Optional[Pairing] = None

    def parent_of(self, child_row: int) -> int:
        if not 0 <= child_row < self.children:
            raise IndexError(f"child row {child_row} is outside the {self.children} child rows")
        return int(self.parents_of(np.array([child_row], dtype=np.int64))[0])

    def parents_of(self, child_rows: "np.ndarray") -> "np.ndarray":
        assert self.pairing is not None
        if child_rows.shape[0] == 0:
            return np.empty(0, dtype=np.int64)
        if not (int(child_rows.min()) >= 0 and int(child_rows.max()) < self.children):
            raise IndexError(f"a child row is outside the {self.children} child rows")
        return self.pairing.second_of(np.asarray(child_rows, dtype=np.int64))

    def children_of(self, parent_row: int) -> range:
        raise _refuse(
            "the second side of an association is paired through its stubs, so its children are "
            "no run of rows; read the children through the first side"
        )


def _decimal(value: Fraction) -> str:
    """*value* as a decimal of at most six places, rounded half up (a report, not a check)."""
    scaled = (value * 10**6 * 2 + 1) // 2
    whole, part = divmod(int(scaled), 10**6)
    return f"{whole}.{part:06d}".rstrip("0").rstrip(".") if part else str(whole)


# -- steps 2 and 3: the caps and the Gale-Ryser condition -----------------------------------------


def capped(degrees: "np.ndarray", cap: int) -> Tuple["np.ndarray", int]:
    """*degrees* with none above *cap*, the excess shared out over the others; and the excess.

    The rows above the cap go to the parents still under it in proportion to their rows (every
    open parent alike when none has any), by the largest remainders, round after round until none
    is left. The caller has checked that the caps hold every row.
    """
    excess = int(np.maximum(degrees - cap, 0).sum())
    found = np.minimum(degrees, cap).astype(np.int64)
    left = excess
    while left > 0:
        room = cap - found
        open_ = room > 0
        weights = np.where(open_, found, 0)
        if int(weights.sum()) == 0:
            weights = open_.astype(np.int64)
        share = np.minimum(allot(left, weights), room)
        found += share
        left -= int(share.sum())
    return found, excess


def gale_ryser_gap(first: "np.ndarray", second: "np.ndarray") -> int:
    """How far two degree sequences fall short of a simple bipartite graph; ``0`` when they hold.

    The largest ``sum_(i<=k) b_(i) - sum_a min(d_a, k)`` over ``k``, with the second side sorted in
    decreasing order (Gale-Ryser). The sums are equal and no first-side degree is above the second
    side's parents: the caller's caps.
    """
    count = int(second.shape[0])
    if count == 0 or first.shape[0] == 0:
        return 0
    held = np.bincount(np.minimum(first, count), minlength=count + 1)
    at_least = np.cumsum(held[::-1])[::-1]
    room = np.cumsum(at_least[1 : count + 1])
    wanted = np.cumsum(np.sort(second)[::-1])
    return max(0, int((wanted - room).max()))


def realisable(first: "np.ndarray", second: "np.ndarray") -> Tuple["np.ndarray", int]:
    """*second* moved, one row at a time, until the two sides meet Gale-Ryser; and the rows moved.

    Each move takes a row from the parent with the most (the lower row on a tie) to the one with
    the fewest (the lower row on a tie). The sum of squares falls with every move and an even
    spread meets the condition (a first-side parent's stubs laid on consecutive second-side
    parents, modulo their number, are distinct), so the moves end.
    """
    found = second.astype(np.int64).copy()
    moved = 0
    gap = gale_ryser_gap(first, found)
    while gap > 0:
        for _ in range(gap):
            most, fewest = int(np.argmax(found)), int(np.argmin(found))
            if int(found[most]) - int(found[fewest]) <= 1:
                raise _refuse("an even spread of an association's rows failed the Gale-Ryser check")
            found[most] -= 1
            found[fewest] += 1
            moved += 1
        gap = gale_ryser_gap(first, found)
    return found, moved


# -- steps 1 to 5 ---------------------------------------------------------------------------------


def _degrees(
    keys: PairKeys, paired: PairedSide, edges: int, where: str
) -> Tuple["np.ndarray", "np.ndarray"]:
    """Each side's rows per parent, laid out over the *edges* rows (step 1)."""
    found = []
    for index in (0, 1):
        shape = paired.shapes[index]
        laid = counted_layout(
            keys.counts[index],
            paired.parents[index],
            edges,
            shape=None if shape is None else shape.as_member(),
            minimum=paired.minima[index],
            where=f"{where} side {paired.relationships[index]!r}",
        )
        found.append(np.asarray(laid.counts, dtype=np.int64))
    return found[0], found[1]


def _check_distinct(paired: PairedSide, edges: int, where: str) -> None:
    """Refuse an association whose rows no simple layout holds (``FLX-3403``)."""
    first, second = paired.parents
    if edges > first * second:
        raise _refuse(
            f"{where} holds {edges} rows and no pair repeats, but {first} x {second} parents make "
            f"only {first * second} pairs; lower the rows or set distinct: false"
        )
    if first * second >= _MOST_PAIRS:
        raise _refuse(f"{where}: {first} x {second} pairs are too many to check for repeats")


def pair_layout(keys: PairKeys, paired: PairedSide, edges: int, where: str = "") -> Pairing:
    """The pairing of an association of *edges* rows (steps 1 to 5); every refusal is ``FLX-3403``."""
    first, second = _degrees(keys, paired, edges, where)
    moved = (0, 0, 0)
    if paired.distinct:
        _check_distinct(paired, edges, where)
        first, over_first = capped(first, paired.parents[1])
        second, over_second = capped(second, paired.parents[0])
        second, shifted = realisable(first, second)
        moved = (over_first, over_second, shifted)
    distances = (
        degree_distance(first, paired.shapes[0]),
        degree_distance(second, paired.shapes[1]),
    )
    plain = Pairing(edges, (first, second), keys.stubs, moved=moved, distances=distances)
    if not paired.distinct or edges == 0:
        return plain
    rows = np.arange(edges, dtype=np.int64)
    owners = np.repeat(np.arange(first.shape[0], dtype=np.int64), first)
    repair = _Repair(owners, plain.second_of(rows), int(second.shape[0]))
    repair.switch(keys.switches)
    rebuilt = not _place(repair)
    held = _rebuilt(first, second, keys.stubs) if rebuilt else repair.second
    return Pairing(
        edges,
        (first, second),
        keys.stubs,
        second=held,
        moved=moved,
        switches=repair.switched,
        placed=repair.placed,
        rebuilt=rebuilt,
        distances=distances,
    )


# -- step 5: the repair ---------------------------------------------------------------------------


class _Held:
    """The pairs a distinct association holds, as codes ``a * width + b``.

    A sorted array of the codes at the start, with two small sets of the codes added and taken off
    since, so a membership test is one bisection and no set holds every row.
    """

    def __init__(self, codes: "np.ndarray") -> None:
        self.start = np.unique(codes)
        self.added: Set[int] = set()
        self.removed: Set[int] = set()

    def __contains__(self, code: int) -> bool:
        if code in self.added:
            return True
        if code in self.removed:
            return False
        at = int(np.searchsorted(self.start, code))
        return at < self.start.shape[0] and int(self.start[at]) == code

    def take(self, code: int) -> None:
        if code in self.added:
            self.added.discard(code)
        else:
            self.removed.add(code)

    def give(self, code: int) -> None:
        if code in self.removed:
            self.removed.discard(code)
        else:
            self.added.add(code)


class _Repair:
    """The rows of a distinct association whose pair repeats, and the keyed switches that clear them.

    ``first`` holds each row's first-side parent (never changed: the rows follow it in order) and
    ``second`` its second-side parent, changed in place. ``pending`` is the rows still repeated, in
    row order; a row whose pair repeated at the start is never a partner.
    """

    def __init__(self, first: "np.ndarray", second: "np.ndarray", width: int) -> None:
        self.first, self.second, self.width = first, second.copy(), width
        codes = first * width + self.second
        order = np.argsort(codes, kind="stable")
        repeated = np.zeros(codes.shape[0], dtype=np.bool_)
        repeated[order[1:]] = codes[order][1:] == codes[order][:-1]
        self.pending: List[int] = np.flatnonzero(repeated).tolist()
        self.repeated: Set[int] = set(codes[repeated].tolist())
        self.held = _Held(codes)
        self.switched = 0
        self.placed = 0

    def code(self, row: int) -> int:
        return int(self.first[row]) * self.width + int(self.second[row])

    def _partner(self, row: int, partner: int, waiting: Set[int]) -> bool:
        """Switch *row* with *partner* when the switch is degree-preserving and repeats no pair."""
        mine, theirs = int(self.first[row]), int(self.first[partner])
        b_mine, b_theirs = int(self.second[row]), int(self.second[partner])
        if partner in waiting or mine == theirs or b_mine == b_theirs:
            return False
        made = (mine * self.width + b_theirs, theirs * self.width + b_mine)
        if self.code(partner) in self.repeated or made[0] in self.held or made[1] in self.held:
            return False
        self.held.take(self.code(partner))
        self.second[row], self.second[partner] = b_theirs, b_mine
        self.held.give(made[0])
        self.held.give(made[1])
        return True

    def switch(self, key: bytes) -> None:
        """Try :data:`SWITCH_ATTEMPTS` partners keyed by *key* for each repeated row, in row order."""
        edges = int(self.first.shape[0])
        waiting = set(self.pending)
        left: List[int] = []
        for row in self.pending:
            for attempt in range(SWITCH_ATTEMPTS):
                ordinal = (row * SWITCH_ATTEMPTS + attempt) % edges
                if self._partner(row, permute2(key, edges, ordinal), waiting):
                    waiting.discard(row)
                    self.switched += 1
                    break
            else:
                left.append(row)
        self.pending = left


def _place(repair: _Repair) -> bool:
    """Havel-Hakimi over the rows the switches left; ``False`` when a parent finds too few stubs.

    The rows' second-side parents are a multiset of stubs. Their first-side parents, most rows
    first (the lower row on a tie), each take for their rows the parents of the stubs left that they
    do not pair with, most stubs first (the lower row on a tie).
    """
    stubs: Dict[int, int] = {}
    rows_of: Dict[int, List[int]] = {}
    for row in repair.pending:
        stubs[int(repair.second[row])] = stubs.get(int(repair.second[row]), 0) + 1
        rows_of.setdefault(int(repair.first[row]), []).append(row)
    width = repair.width
    for parent in sorted(rows_of, key=lambda held: (-len(rows_of[held]), held)):
        rows = rows_of[parent]
        free = [b for b in stubs if stubs[b] > 0 and parent * width + b not in repair.held]
        if len(free) < len(rows):
            return False
        for row, b in zip(rows, sorted(free, key=lambda b: (-stubs[b], b)), strict=False):
            stubs[b] -= 1
            repair.second[row] = b
            repair.held.give(parent * width + b)
            repair.placed += 1
    repair.pending = []
    return True


def _rebuilt(first: "np.ndarray", second: "np.ndarray", key: bytes) -> "np.ndarray":
    """Every row's second-side parent by Havel-Hakimi over the whole table (Gale-Ryser holds).

    First-side parents in decreasing order of rows (the lower row on a tie) each take the
    second-side parents with the most stubs left (the lower row on a tie); a parent's rows hold its
    picks in the order of the keyed permutation of the second side.
    """
    width = int(second.shape[0])
    left = second.astype(np.int64).copy()
    offsets = np.cumsum(first) - first
    held = np.empty(int(first.sum()), dtype=np.int64)
    order = sorted(range(first.shape[0]), key=lambda parent: (-int(first[parent]), parent))
    rank = np.asarray(
        kernel2.permute2_array(key, width, np.arange(width, dtype=np.int64)), dtype=np.int64
    )
    for parent in order:
        need = int(first[parent])
        if need == 0:
            break
        picked = np.lexsort((np.arange(width), -left))[:need]
        if int(left[picked].min()) <= 0:
            raise _refuse("an association that met the Gale-Ryser check found no layout")
        left[picked] -= 1
        start = int(offsets[parent])
        held[start : start + need] = picked[np.argsort(rank[picked], kind="stable")]
    return held


# -- the distance ---------------------------------------------------------------------------------


def degree_overlap(
    degrees: "Sequence[int] | np.ndarray", shape: Optional[ChildShape]
) -> Optional[Tuple[int, int]]:
    """How much the parents counted into *shape*'s classes share with its weights, as two whole numbers.

    ``(satisfied, total)`` whose ratio is ``1 - d``, ``d`` the total variation distance between the
    share of parents in each class and the class's weight: with the weights as whole numbers
    ``w_k`` summing to ``W`` and ``n_k`` of the ``n`` parents in class ``k``, ``total = n * W``
    and ``satisfied = sum_k min(n_k * W, w_k * n)``. A parent's count goes to the class whose low
    edge is the greatest at or below it (the first class when none is), as ``verify`` bins children
    per parent. ``None`` with no shape or no parent.
    """
    held = np.asarray(degrees, dtype=np.int64)
    if shape is None or held.shape[0] == 0:
        return None
    classes = classes_of(shape.as_member())
    lows = np.array([cls.low for cls in classes], dtype=np.int64)
    index = np.clip(np.searchsorted(lows, held, side="right") - 1, 0, len(classes) - 1)
    run = np.bincount(index, minlength=len(classes)).tolist()
    shares = [Fraction(cls.weight) for cls in classes]
    scale = lcm(*(share.denominator for share in shares))
    weights = [int(share * scale) for share in shares]
    whole, parents = sum(weights), int(held.shape[0])
    if whole == 0:
        return None
    satisfied = sum(
        min(count * whole, weight * parents) for count, weight in zip(run, weights, strict=True)
    )
    return satisfied, parents * whole


def degree_distance(
    degrees: "Sequence[int] | np.ndarray", shape: Optional[ChildShape]
) -> Optional[Fraction]:
    """The total variation distance between the parents counted into *shape*'s classes and its weights.

    Exact; ``None`` with no shape or no parent (:func:`degree_overlap`).
    """
    overlap = degree_overlap(degrees, shape)
    return None if overlap is None else 1 - Fraction(*overlap)


# -- the sides, and the pairing a process reads -----------------------------------------------------


def paired_sides(
    entry: AssociationEntry,
    relationships: Mapping[str, Relationship],
    counts: Mapping[str, CountsEntry],
    parents: Mapping[str, int],
    hooked: AbstractSet[str] = frozenset(),
) -> Dict[str, PairedSide]:
    """Each side of *entry* by relationship, or ``FLX-3403`` for a side an association cannot hold.

    *relationships* are the emit's, *counts* its entries (one for each side), *parents* each
    relationship's parent rows and *hooked* the relationships an expected-count hook binds.
    """
    where = f"expose {entry.emit!r} association {list(entry.relationships)}"
    for name in entry.relationships:
        relationship = relationships.get(name)
        if relationship is None or name not in parents:
            raise _refuse(f"{where} names {name!r}, which is no relationship to a generated parent")
        if relationship.cardinality == "one_to_one":
            raise _refuse(f"{where} names {name!r}, which is one_to_one; a side is many_to_one")
        held = counts[name]
        if held.parent_when is not None or held.given is not None or name in hooked:
            raise _refuse(
                f"{where}: the counts of {name!r} declare a parentWhen, a given or an expected "
                "count, which pair with no stub of the other side; drop it"
            )
    shapes = (counts[entry.relationships[0]].children, counts[entry.relationships[1]].children)
    minima = tuple(counts[name].min_children or 0 for name in entry.relationships)
    sizes = tuple(parents[name] for name in entry.relationships)
    return {
        name: PairedSide(
            relationships=entry.relationships,
            side=index,
            shapes=shapes,
            minima=(minima[0], minima[1]),
            parents=(sizes[0], sizes[1]),
            distinct=entry.distinct,
        )
        for index, name in enumerate(entry.relationships)
    }


#: The pairings this process has worked out, and how many it keeps (each may hold an array of rows).
_PAIRINGS: Dict[Tuple[Any, ...], Pairing] = {}
_PAIRINGS_KEPT = 4


def pair_keys(root: SeedTreeRoot, sim_id: str, expose_id: str, pair: Tuple[str, str]) -> PairKeys:
    """The keys of an association's pairing under *root* (kernel 0.2.0 roots only)."""
    first, second = pair
    return PairKeys(
        counts=(
            count_key(root, sim_id, expose_id, first),
            count_key(root, sim_id, expose_id, second),
        ),
        stubs=digest_key(
            root, associate_path(sim_id, expose_id, pair, "stubs"), ASSOCIATE_PATH_SHAPE
        ),
        switches=digest_key(
            root, associate_path(sim_id, expose_id, pair, "switches"), ASSOCIATE_PATH_SHAPE
        ),
    )


def pairing_for(
    root: SeedTreeRoot, address: Tuple[str, str, str], edges: int, paired: PairedSide
) -> Pairing:
    """The pairing of an association of *edges* rows, worked out once per process.

    *address* is the emit's Simulation, exposeId and seam digest.
    """
    sim_id, expose_id, seam_digest = address
    held = (
        root.digest,
        root.seed_tree_version,
        sim_id,
        expose_id,
        seam_digest,
        edges,
        paired.relationships,
        paired.shapes,
        paired.minima,
        paired.parents,
        paired.distinct,
    )
    found = _PAIRINGS.get(held)
    if found is None:
        found = pair_layout(
            pair_keys(root, sim_id, expose_id, paired.relationships),
            paired,
            edges,
            where=f"expose {expose_id!r} association {list(paired.relationships)}",
        )
        if len(_PAIRINGS) >= _PAIRINGS_KEPT:
            _PAIRINGS.clear()
        _PAIRINGS[held] = found
    return found
