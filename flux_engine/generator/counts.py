# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The counted layout: members per owner are counted, not drawn (KERNEL-SPEC-2 section 8.1).

A pick places each child row on a parent by itself, so the number of children a parent gets is a
sum of chances. The counted layout decides how many children each parent has first, then lays the
child rows out parent by parent, so that a parent's children are a run of consecutive rows that
any process names from the parent's row alone.

:func:`counted_layout` is a pure function. It draws no value: its one source of order is
``permute/2`` under a key the caller derives (:func:`count_key`), so the same inputs give the same
layout in every process, in any slice. It is written for any owner and member rows, not for one
relationship: ``parents`` ordered owners take ``children`` ordered members, and a later layout of
another pair (entities and their versions, say) calls it with a key of its own path.

The steps, all on exact integers (the rationals scaled to one denominator):

1. **Eligible parents.** ``eligible`` holds the parent rows that may have children, ascending;
   ``None`` makes every row eligible. A parent's rank is its position among them.
2. **Expected counts.** ``x`` per rank: the slot value of the shape (:func:`slot_values`, ranks
   taking slots through ``sigma``, the keyed permutation of the ranks), the exact rational a hook
   gives (``expected``), a hook ranked into the shape's slots (the rank coupling), or ``1``.
3. **The minimum.** ``S = children - minimum * e`` children are spare; ``S < 0`` is ``FLX-3403``.
   Each rank weighs ``y = max(x - minimum, 0)``, and every rank weighs ``1`` when all weigh ``0``.
4. **The allotment.** Rank ``r`` receives ``floor(S * y / Y)`` and the ``S - sum`` ranks with the
   largest remainders one more, ties to the lower rank. Its count is ``minimum`` plus that. On a
   ``one_to_one`` relationship no parent has more than one child: where the allotment would give a
   parent a second, the spare children go one each to the parents that have none, those with the
   largest ``y`` first, ties to the lower rank (:func:`_capped`).
   With ``given`` (KERNEL-SPEC-2 section 8.1, extended) the eligible parents are split by the class
   of a column of their final rows, keeping their order: class ``l`` has its own weights over the
   shape's classes, ``n_(l,k) = LRM(|E_l|, weights_l)`` parents in count class ``k``, and the
   parents of ``E_l`` in ascending order of ``sigma`` (taken over the whole of ``E``) take its slots
   in order (:func:`_grouped_slots`); a hook's rank coupling applies within each class.
5. **The rows.** Parent ``p``'s children are the rows from the sum of the counts before it, and
   :meth:`CountedLayout.parent_of` finds a child row's parent by bisection.

Every refusal is ``FLX-3403``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, InvalidOperation
from fractions import Fraction
from math import lcm
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

import numpy as np

from flux_engine.core import kernel2
from flux_engine.core.canonical import sha256_hex
from flux_engine.core.errors import FluxError
from flux_engine.core.lrm import WEIGHT_SUM_TOLERANCE, largest_remainder
from flux_engine.core.seedtree import PathShape, SeedPath, SeedTreeRoot, key_label
from flux_engine.generator.paths import COUNT_PATH_SHAPE, count_path
from flux_engine.seam.counts import ChildShape
from flux_stats.fidelity import require_number_length

if TYPE_CHECKING:
    from flux_engine.generator.rows import GeneratorPlan
    from flux_engine.generator.rules import ParentCondition

__all__ = [
    "CountClass",
    "CountedLayout",
    "CountedPick",
    "ExpectedCountHook",
    "TooFewChildRows",
    "TooManyChildRows",
    "check_minimum",
    "classes_of",
    "class_counts",
    "count_key",
    "counted_layout",
    "digest_key",
    "expected_count_hooks",
    "register_expected_count_hook",
    "sigma_of",
    "slot_values",
]

_NUL = b"\x00"
#: Below this a product of two counts is an int64; above it the integers are Python's.
_SAFE = 1 << 62
_LONGEST_TEXT = 64


def _plural(count: int, one: str, many: str) -> str:
    """*count* with its noun in the right number: ``1 child``, ``2 children``."""
    return f"{count} {one if count == 1 else many}"


def _refuse(message: str) -> FluxError:
    """``FLX-3403`` (imported here: relations imports this module)."""
    from flux_engine.generator import relations

    return FluxError(relations.FLX_3403, message)


class TooFewChildRows(FluxError):
    """``FLX-3403`` for child rows that cannot give every eligible parent its minimum.

    A class of its own so a plan under ``--scale`` can say that the scale left too few child rows
    (:func:`flux_engine.stages.scaling.child_rows_note`). It keeps the three numbers of the shortfall
    (``children``, ``eligible`` and ``minimum``) and, once the layout's owner is known, the exposes
    of the child rows and of the parents (``child`` and ``parent``), so a plan under ``--scale`` can
    ask whether the same shortfall would be there at scale 1 without running it.
    """

    children: int = 0
    eligible: int = 0
    minimum: int = 0
    child: str = ""
    parent: str = ""


class TooManyChildRows(FluxError):
    """``FLX-3403`` for child rows that a ``one_to_one`` relationship cannot give a parent each.

    A class of its own, as :class:`TooFewChildRows` is, so a plan under ``--scale`` can say that
    the scale is what left more child rows than parents
    (:func:`flux_engine.stages.scaling.too_many_child_rows_note`).
    """


def check_minimum(children: int, eligible: int, minimum: int) -> None:
    """``FLX-3403`` (:class:`TooFewChildRows`) when ``children - minimum * eligible`` is negative."""
    if children < minimum * eligible:
        from flux_engine.generator import relations

        short = TooFewChildRows(
            relations.FLX_3403,
            f"the {_plural(children, 'child row', 'child rows')} cannot give each of the "
            f"{_plural(eligible, 'eligible parent', 'eligible parents')} "
            f"{_plural(minimum, 'child', 'children')} (N = {children}, m = {minimum}, e = {eligible}, "
            f"N - m * e < 0)",
        )
        short.children, short.eligible, short.minimum = children, eligible, minimum
        raise short


@dataclass(frozen=True)
class CountClass:
    """One class of the shape: the whole counts ``low`` to ``high`` and its share of the parents."""

    low: int
    high: int
    weight: Decimal


@dataclass(frozen=True)
class CountedPick:
    """What a foreign key on the counted layout carries: the shape and the child's rows.

    ``children`` is the declared shape of the number of children per parent, ``None`` to spread
    them as evenly as whole counts allow; ``child_rows`` is ``N``, the child plan's rows (after
    ``--scale``), which the layout totals.
    """

    children: Optional[ChildShape]
    child_rows: int
    #: ``minChildren`` as declared; ``None`` when the entry declares none (the layout takes 0).
    minimum: Optional[int] = None
    #: ``parentWhen`` as checked: only a parent on whose final row it is TRUE has children.
    parent_when: Optional["ParentCondition"] = None
    #: What an expected-count hook bound for this relationship (:class:`ExpectedCountHook`); the
    #: member is part of the plan's document, so a changed one moves the child's digest.
    expected_count: Optional[Mapping[str, Any]] = field(default=None, hash=False)
    #: The relationship is ``one_to_one``: no parent has more than one child (not in the document,
    #: which the contract's cardinality already binds).
    one_to_one: bool = False
    #: ``given``: the column of the parent whose class on each parent's final row sets the weights
    #: of the shape, and ``children_given``, the weights of every class of it over the shape's
    #: classes (a class the entry gave no list holds the shape's own), sorted by class; the
    #: classes that took the shape's weights are ``given_default``.
    given: Optional[str] = None
    children_given: Optional[Tuple[Tuple[str, Tuple[str, ...]], ...]] = None
    given_default: Tuple[str, ...] = ()
    #: The hook that returned :attr:`expected_count`; the layout asks it for each parent's
    #: expected count. It is no part of the pick's identity or document.
    hook: Optional["ExpectedCountHook"] = field(default=None, compare=False, hash=False, repr=False)

    def as_document(self) -> Dict[str, Any]:
        """The ``counts`` member of the foreign key's plan document: what the entry declares."""
        document: Dict[str, Any] = {}
        if self.minimum is not None:
            document["minChildren"] = self.minimum
        if self.parent_when is not None:
            document["parentWhen"] = self.parent_when.when
        if self.children is not None:
            document["children"] = self.children.as_member()
        document.update(self.as_document_given())
        if self.expected_count is not None:
            document["expectedCount"] = dict(self.expected_count)
        return document

    def as_document_given(self) -> Dict[str, Any]:
        """The ``given`` members of the document: the column, every class's weights, the defaulted classes."""
        if self.given is None or self.children_given is None:
            return {}
        document: Dict[str, Any] = {
            "given": self.given,
            "childrenGiven": {key: list(weights) for key, weights in self.children_given},
        }
        if self.given_default:
            document["childrenGivenDefault"] = list(self.given_default)
        return document

    def shape(self) -> Optional[Mapping[str, Sequence[str]]]:
        return None if self.children is None else self.children.as_member()


# -- the expected-count hook --------------------------------------------------------------------


class ExpectedCountHook(Protocol):
    """A module's per-parent expected count, given to the counted layout (ADR 0016 section 3).

    A parent's expected count may come from somewhere other than a declared shape: the days a
    parent is active inside the window, for example. The module that owns such a count declares
    it in a key of its own and registers a hook (:func:`register_expected_count_hook`). Hooks are
    registered by the engine's own modules and never loaded from a bundle, and a hook draws
    nothing and moves no position.
    """

    def member(
        self, namespace: Mapping[str, Any], child: "GeneratorPlan", relationship: str
    ) -> Optional[Mapping[str, Any]]:
        """At plan time: None when the bundle declares no expected count for this relationship; otherwise what the hook binds into the child plan's document, so that a changed declaration moves its digest. Refusals are FLX-3403."""
        ...

    def expected(
        self, parent: "GeneratorPlan", row: int, final_row: Tuple[Any, ...], relationship: str
    ) -> Fraction:
        """The expected number of children of parent row `row`, exact and not negative, from that row as the parent's own pass writes it (ADR 0022 section 3, in `parent.columns` order) and from the plan, its `spec.timeBounds` included."""
        ...


_HOOKS: List[ExpectedCountHook] = []


def register_expected_count_hook(hook: ExpectedCountHook) -> Callable[[], None]:
    """Register *hook*; the returned function unregisters it.

    An engine module registers its hook when it is imported. Registering the same object twice
    registers it once, and unregistering a hook that is gone does nothing.
    """
    if not any(known is hook for known in _HOOKS):
        _HOOKS.append(hook)

    def unregister() -> None:
        _HOOKS[:] = [known for known in _HOOKS if known is not hook]

    return unregister


def expected_count_hooks() -> Tuple[ExpectedCountHook, ...]:
    """The registered hooks, in the order they registered."""
    return tuple(_HOOKS)


# -- the key ------------------------------------------------------------------------------------


def digest_key(root: SeedTreeRoot, path: SeedPath, shape: PathShape) -> bytes:
    """The 32-byte key of *path*: the whole digest whose first 16 bytes are the path's key.

    ``sha256(domain NUL root NUL key_label(path))`` (KERNEL-SPEC-2 section 8.1), the ``key``
    ``permute/2`` takes. Kernel ``0.2.0``'s roots only.
    """
    if not root.counter_based:
        raise _refuse(f"a counted layout needs kernel 0.2.0; the root is {root.seed_tree_version}")
    label, _ordinals = key_label(path, shape)
    return bytes.fromhex(sha256_hex(root.domain + _NUL + root.digest + _NUL + label))


def count_key(root: SeedTreeRoot, sim_id: str, expose_id: str, relationship: str) -> bytes:
    """``K`` of the path ``("sim", sim_id, "gen", exposeId, "count", relationship)``."""
    return digest_key(root, count_path(sim_id, expose_id, relationship), COUNT_PATH_SHAPE)


def sigma_of(key: bytes, ranks: int) -> List[int]:
    """``sigma(r) = permute/2(key, ranks, r)`` for every rank ``r``."""
    if ranks == 0:
        return []
    return kernel2.permute2_array(key, ranks, np.arange(ranks, dtype=np.int64))


# -- the shape ----------------------------------------------------------------------------------


_SHOWN = 40


def _shown(value: object) -> str:
    """*value* as a refusal message quotes it: its repr, cut to a short prefix and an ellipsis."""
    spelled = repr(value)
    return spelled if len(spelled) <= _SHOWN else f"{spelled[:_SHOWN]}..."


def _decimal(text: str, where: str) -> Decimal:
    """*text* as a finite decimal, read as the fidelity block reads its numbers, or ``FLX-3403``.

    The text is at most 64 characters, and the digits it needs written out without an exponent
    are bounded as ``require_number_length`` bounds a fidelity number, before any value is formed:
    ``1E-999999999`` is twelve characters and would be a billion digits.
    """
    try:
        value = Decimal(text) if len(text) <= _LONGEST_TEXT else None
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite():
        raise _refuse(
            f"{where} is {_shown(text)}, which is no decimal of at most {_LONGEST_TEXT} characters"
        )
    try:
        require_number_length(value, where)
    except FluxError as long:
        raise _refuse(
            f"{where} is {_shown(text)}, which needs too many digits written out"
        ) from long
    return value


def _whole(text: str, where: str) -> int:
    """A whole number of at least 0, from decimal text."""
    value = _decimal(text, where)
    try:
        whole = value >= 0 and value == value.to_integral_value() and value.adjusted() <= 18
    except DecimalException:
        whole = False
    if not whole:
        raise _refuse(f"{where} is {_shown(text)}, which is no whole number of 0 to 10**18")
    return int(value)


def _ascending(values: Sequence[str], where: str) -> List[int]:
    """The whole numbers *values* spell, which must be strictly ascending."""
    found = [_whole(text, f"{where}[{position}]") for position, text in enumerate(values)]
    if any(low >= high for low, high in zip(found, found[1:], strict=False)):
        raise _refuse(f"{where} is not strictly ascending: {_shown(list(values))}")
    return found


def _weights(values: Sequence[str], where: str) -> List[Decimal]:
    found: List[Decimal] = []
    for position, text in enumerate(values):
        weight = _decimal(text, f"{where}[{position}]")
        if weight < 0:
            raise _refuse(f"{where}[{position}] is {_shown(text)}, which is no weight of 0 or more")
        found.append(weight)
    try:
        total = sum(found, Decimal(0))
        off = abs(total - 1) > WEIGHT_SUM_TOLERANCE
    except DecimalException as overflow:
        raise _refuse(f"{where} do not sum to 1: {overflow.__class__.__name__}") from overflow
    if off:
        raise _refuse(f"{where} sum to {total}, and the shares of a shape sum to 1")
    return found


def classes_of(
    shape: Mapping[str, Sequence[str]], where: str = "children"
) -> Tuple[CountClass, ...]:
    """The classes of a ``children`` shape, in declared order, or ``FLX-3403``.

    ``levels`` and ``levelWeights`` give one class ``[v, v]`` per level. ``bins`` and
    ``binWeights`` give bin ``k`` the counts ``[edge_k, edge_{k+1} - 1]``, except the last bin,
    which holds its upper edge too. Levels and edges are strictly ascending whole numbers of at
    least 0, with one weight per class, and the weights sum to 1.
    """
    if set(shape) == {"levels", "levelWeights"}:
        values = _ascending(shape["levels"], f"{where}.levels")
        weights = _weights(shape["levelWeights"], f"{where}.levelWeights")
        ranges = [(value, value) for value in values]
    elif set(shape) == {"bins", "binWeights"}:
        edges = _ascending(shape["bins"], f"{where}.bins")
        if len(edges) < 2:
            raise _refuse(f"{where}.bins needs at least two edges")
        weights = _weights(shape["binWeights"], f"{where}.binWeights")
        ranges = [(edges[k], edges[k + 1] - 1) for k in range(len(edges) - 2)]
        ranges.append((edges[-2], edges[-1]))
    else:
        raise _refuse(
            f"{where} carries levels and levelWeights, or bins and binWeights, and nothing else"
        )
    if len(weights) != len(ranges):
        raise _refuse(
            f"{where} has {len(ranges)} classes and {len(weights)} weights; give one weight each"
        )
    return tuple(
        CountClass(low, high, weight) for (low, high), weight in zip(ranges, weights, strict=True)
    )


def class_counts(parents: int, classes: Sequence[CountClass]) -> List[int]:
    """The parents of each class: the Largest Remainder Method (KERNEL-SPEC section 5).

    Each class is keyed by its position in decimal, zero-padded to the width of the largest
    position, so the keys sort in class order and a tie goes to the lower class.
    """
    width = len(str(len(classes) - 1))
    keyed = {str(position).zfill(width): cls.weight for position, cls in enumerate(classes)}
    counts = largest_remainder(parents, keyed)
    return [counts[name] for name in keyed]


def slot_values(parents: int, classes: Sequence[CountClass]) -> "np.ndarray":
    """The value of each of the *parents* slots, class by class; never decreasing with the slot.

    The ``j``-th slot of a class that holds ``n`` parents takes
    ``low + floor(j * (high - low + 1) / n)``, which spreads a bin's parents evenly over its
    counts. An int64 array, or an array of Python integers when a count is too large for one.
    """
    pieces: List["np.ndarray"] = []
    for cls, held in zip(classes, class_counts(parents, classes), strict=True):
        if held == 0:
            continue
        width = cls.high - cls.low + 1
        if held * width < _SAFE and cls.high < _SAFE:
            pieces.append(cls.low + (np.arange(held, dtype=np.int64) * width) // held)
        else:
            pieces.append(
                np.array([cls.low + (j * width) // held for j in range(held)], dtype=object)
            )
    return np.concatenate(pieces) if pieces else np.empty(0, dtype=np.int64)


# -- the layout ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class CountedLayout:
    """The children of each parent row: how many, and where their run starts.

    ``counts[p]`` is parent row ``p``'s number of children and ``offsets[p]`` the child row its
    run starts at (the sum of the counts before it), both int64 arrays over every parent row.
    ``children`` is ``N``, the sum of the counts.
    """

    counts: "np.ndarray"
    offsets: "np.ndarray"
    children: int

    def parent_of(self, child_row: int) -> int:
        """The parent row whose run holds *child_row*, by bisection over the offsets."""
        if not 0 <= child_row < self.children:
            raise IndexError(f"child row {child_row} is outside the {self.children} child rows")
        return int(np.searchsorted(self.offsets, child_row, side="right")) - 1

    def parents_of(self, child_rows: "np.ndarray") -> "np.ndarray":
        """:meth:`parent_of` for an array of child rows (all inside ``[0, children)``)."""
        if child_rows.shape[0] and not (
            int(child_rows.min()) >= 0 and int(child_rows.max()) < self.children
        ):
            raise IndexError(f"a child row is outside the {self.children} child rows")
        return np.searchsorted(self.offsets, child_rows, side="right") - 1

    def children_of(self, parent_row: int) -> range:
        """The child rows of *parent_row*: a run of consecutive rows, empty for a parent with none."""
        start = int(self.offsets[parent_row])
        return range(start, start + int(self.counts[parent_row]))


def _eligible_rows(parents: int, eligible: Optional[Sequence[int]]) -> "np.ndarray":
    if eligible is None:
        return np.arange(parents, dtype=np.int64)
    rows = np.asarray(eligible, dtype=np.int64)
    if rows.shape[0] and (
        int(rows.min()) < 0 or int(rows.max()) >= parents or bool((np.diff(rows) <= 0).any())
    ):
        raise _refuse(f"the eligible parents are ascending rows of the {parents} parent rows")
    return rows


def _scaled(expected: Sequence[Fraction]) -> Tuple[List[int], int]:
    """The hook's rationals as integers over one denominator, and that denominator."""
    values = [Fraction(value) for value in expected]
    if any(value < 0 for value in values):
        raise _refuse("an expected count is exact and not negative")
    scale = lcm(*(value.denominator for value in values)) if values else 1
    return [value.numerator * (scale // value.denominator) for value in values], scale


def _expected_counts(
    key: bytes,
    ranks: int,
    classes: Optional[Sequence[CountClass]],
    expected: Optional[Sequence[Fraction]],
    grouped: Optional[Tuple[Sequence[Sequence[CountClass]], "np.ndarray"]] = None,
) -> Tuple["np.ndarray", int]:
    """Each rank's expected count as integers over a common denominator, and the denominator.

    A shape alone: the slot value of the rank's ``sigma``. A hook alone: its value. Both, the
    rank coupling: ranks ordered by ``(hook, sigma)`` ascending take the slots in order. Neither:
    ``1``. With *grouped* (the classes of each given class and every rank's given class), the
    slots are each given class's own, taken within it (:func:`_grouped_slots`).
    """
    scaled, scale = _scaled(expected) if expected is not None else ([], 1)
    if grouped is not None:
        return _grouped_slots(key, ranks, grouped, scaled if expected is not None else None), 1
    if classes is None:
        if expected is None:
            return np.ones(ranks, dtype=np.int64), 1
        fits = max(scaled, default=0) < _SAFE
        return np.array(scaled, dtype=np.int64 if fits else object), scale
    slots = slot_values(ranks, classes)
    sigma = sigma_of(key, ranks)
    if expected is None:
        return slots[np.array(sigma, dtype=np.int64)], 1
    order = sorted(range(ranks), key=lambda rank: (scaled[rank], sigma[rank]))
    coupled = np.empty_like(slots)
    coupled[np.array(order, dtype=np.int64)] = slots
    return coupled, 1


def _grouped_slots(
    key: bytes,
    ranks: int,
    grouped: Tuple[Sequence[Sequence[CountClass]], "np.ndarray"],
    scaled: Optional[Sequence[int]],
) -> "np.ndarray":
    """Each rank's slot value when the eligible parents are split by a given class.

    *grouped* holds the count classes of each given class ``l`` (its own weights) and the given
    class of every rank. The ranks of class ``l`` keep their order; ``sigma`` is taken over every
    rank (:func:`sigma_of`), and the ranks of the class in ascending order of ``sigma`` take its
    slots ``v_(l,0), v_(l,1), ...`` in order. With a hook (*scaled*, over a common denominator) the
    ranks of the class are ordered by ``(hook, sigma)`` instead, as the rank coupling orders them.
    A class with no rank has no slot.
    """
    per_class, groups = grouped
    sigma = np.array(sigma_of(key, ranks), dtype=np.int64)
    pieces: List[Tuple["np.ndarray", "np.ndarray"]] = []
    for level, classes in enumerate(per_class):
        members = np.flatnonzero(groups == level)
        if members.shape[0] == 0:
            continue
        slots = slot_values(int(members.shape[0]), classes)
        if scaled is None:
            order = np.argsort(sigma[members], kind="stable")
        else:
            held = sorted(members.tolist(), key=lambda rank: (scaled[rank], int(sigma[rank])))
            order = np.searchsorted(members, np.array(held, dtype=np.int64))
        pieces.append((members[order], slots))
    wide = any(slots.dtype == object for _, slots in pieces)
    found = np.zeros(ranks, dtype=object if wide else np.int64)
    for rows, slots in pieces:
        found[rows] = slots
    return found


def _weights_of(expected: "np.ndarray", floor: int, spare: int) -> "np.ndarray":
    """``y = max(x - minimum, 0)`` per rank, every rank ``1`` when all are ``0`` and some are spare."""
    if expected.dtype == object or floor >= _SAFE:
        weights = np.array([max(int(value) - floor, 0) for value in expected], dtype=object)
    else:
        weights = np.maximum(expected - floor, 0)
    if spare > 0 and weights.shape[0] and int(weights.max()) == 0:
        return np.ones(weights.shape[0], dtype=np.int64)
    return weights


def _allot(spare: int, weights: "np.ndarray") -> "np.ndarray":
    """*spare* children over the ranks by the weights, the largest remainders taking the rest.

    ``floor(spare * w / W)`` each, and the ``spare - sum`` ranks with the largest remainders
    ``(spare * w) mod W`` one more, a tie going to the lower rank. Exact: int64 when the products
    fit it, Python integers when they do not.
    """
    ranks = weights.shape[0]
    if spare == 0 or ranks == 0:
        return np.zeros(ranks, dtype=np.int64)
    largest = int(weights.max())
    if largest * max(spare, ranks) < _SAFE:
        weights = weights.astype(np.int64)
    else:
        weights = weights.astype(object)
    total = int(weights.sum())
    product = weights * spare
    floors = product // total
    left = spare - int(floors.sum())
    if left:
        # A stable sort keeps the lower rank first among equal remainders.
        order = np.argsort(-(product - floors * total), kind="stable")[:left]
        floors[order] += 1
    return floors.astype(np.int64)


def _capped(per_rank: "np.ndarray", weights: "np.ndarray") -> "np.ndarray":
    """The allotment of a ``one_to_one`` relationship: no rank has more than one child.

    Equal to *per_rank* when no rank has more than one. Otherwise each rank keeps at most one
    and the children above one go one each to the ranks with none, the largest weight ``y``
    first and a tie to the lower rank; more such children than ranks is ``FLX-3403``
    (:class:`TooManyChildRows`).
    """
    spare = int(np.maximum(per_rank - 1, 0).sum())
    if spare == 0:
        return per_rank
    capped = np.minimum(per_rank, 1)
    empty = [int(rank) for rank in np.flatnonzero(capped == 0)]
    if spare > len(empty):
        raise TooManyChildRows(
            _code(),
            f"a one_to_one relationship gives no parent more than one child, and the "
            f"{_plural(int(per_rank.sum()), 'child row', 'child rows')} are more than its "
            f"{_plural(per_rank.shape[0], 'eligible parent', 'eligible parents')}",
        )
    empty.sort(key=lambda rank: (-int(weights[rank]), rank))
    capped[np.array(empty[:spare], dtype=np.int64)] = 1
    return capped


def _code() -> Any:
    from flux_engine.generator import relations

    return relations.FLX_3403


def counted_layout(
    key: bytes,
    parents: int,
    children: int,
    *,
    shape: Optional[Mapping[str, Sequence[str]]] = None,
    minimum: int = 0,
    eligible: Optional[Sequence[int]] = None,
    expected: Optional[Sequence[Fraction]] = None,
    one_to_one: bool = False,
    where: str = "",
    class_shapes: Optional[Sequence[Mapping[str, Sequence[str]]]] = None,
    groups: Optional[Sequence[int]] = None,
) -> CountedLayout:
    """The counted layout of *children* child rows over *parents* parent rows (KERNEL-SPEC-2 section 8.1).

    *key* is the 32-byte key of the layout's path (:func:`count_key`). *shape* is a ``children``
    mapping (:func:`classes_of`); *minimum* the least count of every eligible parent; *eligible*
    the parent rows that may have children, ascending (``None``: every row); *expected* the hook's
    exact expected count of each eligible parent in rank order (``None``: no hook);
    *where* names the child expose, relationship and ``parentWhen`` in a refusal; *one_to_one* caps every parent at one child (:func:`_capped`): a layout whose allotment would
    give a parent more is the same layout with the spare children moved to parents with none, and
    more child rows than eligible parents, or a *minimum* above one, is refused. With *class_shapes*,
    one ``children`` mapping per class of a given column (the same classes, each its own weights),
    and *groups*, the class index of each eligible parent in rank order, the layout is the one
    that splits the eligible parents by class (:func:`_grouped_slots`); *shape* is then not
    read. A pure function:
    it draws no value, and the same inputs give the same layout. Every refusal is ``FLX-3403``.
    """
    classes = classes_of(shape) if shape is not None else None
    rows = _eligible_rows(parents, eligible)
    ranks = int(rows.shape[0])
    _check_layout(children, ranks, minimum, expected, one_to_one, where)
    grouped: Optional[Tuple[Sequence[Sequence[CountClass]], "np.ndarray"]] = None
    if class_shapes is not None:
        if groups is None or len(groups) != ranks:
            raise _refuse("a layout given a column has the class of each eligible parent")
        grouped = (
            [
                classes_of(each, f"{where} children class {index}")
                for index, each in enumerate(class_shapes)
            ],
            np.asarray(groups, dtype=np.int64),
        )
        if ranks and (int(grouped[1].min()) < 0 or int(grouped[1].max()) >= len(class_shapes)):
            raise _refuse("a parent has a class the given column does not have")
    per_rank = np.zeros(0, dtype=np.int64)
    if ranks:
        per_rank = _per_rank(key, children, ranks, classes, minimum, expected, one_to_one, grouped)
    counts = np.zeros(parents, dtype=np.int64)
    counts[rows] = per_rank
    return CountedLayout(counts=counts, offsets=np.cumsum(counts) - counts, children=children)


def _check_layout(
    children: int,
    ranks: int,
    minimum: int,
    expected: Optional[Sequence[Fraction]],
    one_to_one: bool,
    where: str = "",
) -> None:
    """Refuse a layout's inputs that no layout can satisfy (``FLX-3403``)."""
    if expected is not None and len(expected) != ranks:
        raise _refuse(
            f"a hook gives one expected count per eligible parent ({ranks}), not {len(expected)}"
        )
    check_minimum(children, ranks, minimum)
    if ranks == 0 and children > 0:
        raise _refuse(
            f"{where + ': ' if where else ''}no parent is eligible (e = 0) for the "
            f"{_plural(children, 'child row', 'child rows')}; relax the parentWhen condition or "
            "the parent's rows so some parent meets it"
        )
    if one_to_one and minimum > 1:
        raise _refuse(
            "a one_to_one relationship gives no parent more than one child, so minimum is 0 or 1"
        )


def _per_rank(
    key: bytes,
    children: int,
    ranks: int,
    classes: Any,
    minimum: int,
    expected: Optional[Sequence[Fraction]],
    one_to_one: bool,
    grouped: Optional[Tuple[Sequence[Sequence[CountClass]], "np.ndarray"]] = None,
) -> np.ndarray:
    """The child count of each of *ranks* eligible parents, in rank order."""
    spare = children - minimum * ranks
    x, scale = _expected_counts(key, ranks, classes, expected, grouped)
    weights = _weights_of(x, minimum * scale, spare)
    per_rank = minimum + _allot(spare, weights)
    return _capped(per_rank, weights) if one_to_one else per_rank
