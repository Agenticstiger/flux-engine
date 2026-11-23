# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Exact allocation: each class of a column holds exactly its share of the rows (KERNEL-SPEC-2 section 8.3).

A column that declares ``allocation: exact`` does not draw a class for each row. Its classes are
its levels in NFC order or its bins in edge order, then NULL when it declares a ``nullFraction``
above 0. Their
weights are the declared ones as exact rationals, normalised to sum to 1 and, with a NULL class,
scaled by ``1 - nullFraction``, NULL taking ``nullFraction``. The class counts are
``LRM(N, weights)``, a tie going to the lower class, and the slots ``0 .. N - 1`` are laid out
class by class. Row ``i`` takes the class of the slot ``permute/2(K, N, i)``, ``K`` the 32-byte
key of the path ``("sim", sim_id, "gen", exposeId, "allocate", column)``.

Everything here is a pure function of the declaration, the rows ``N`` and the run's root. It draws
no value, so a cell still makes the draws it always made, at the indexes it always took, and
leaves them unread.
"""

from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal
from fractions import Fraction
from functools import lru_cache
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Union, cast

import numpy as np

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.lrm import largest_remainder
from flux_engine.core.rng import nfc_sorted_items
from flux_engine.core.seedtree import SeedTreeRoot
from flux_engine.generator import counts as _counts
from flux_engine.generator.paths import ALLOCATE_PATH_SHAPE, allocate_path
from flux_engine.generator.statistics import FLX_3402, ColumnStatistics

__all__ = [
    "Label",
    "Layout",
    "allocate_key",
    "allocated_levels",
    "class_names",
    "class_weights",
    "exact_counts",
    "layout_of",
]

_KEPT = 64

#: A class: a level's name, a bin's index, or ``None`` for NULL.
Label = Union[str, int, None]

#: A layout: the class labels (NULL, when present, last and ``None``), their counts and first slots.
Layout = Tuple[Tuple[Label, ...], Tuple[int, ...], Tuple[int, ...]]


def allocate_key(root: SeedTreeRoot, sim_id: str, expose_id: str, column: str) -> bytes:
    """``K`` of the path ``("sim", sim_id, "gen", exposeId, "allocate", column)``."""
    return _counts.digest_key(root, allocate_path(sim_id, expose_id, column), ALLOCATE_PATH_SHAPE)


def class_names(statistics: ColumnStatistics, given_level: Optional[str] = None) -> List[Any]:
    """The classes of a column apart from NULL: its levels in NFC order, or its bins' indexes.

    A column drawn given another takes its classes from the table row of *given_level*.
    """
    if statistics.bins is not None:
        return list(range(len(_declared_weights(statistics, given_level))))
    if given_level is not None and statistics.level_weights_given is not None:
        return [name for name, _ in nfc_sorted_items(statistics.level_weights_given[given_level])]
    if statistics.levels is not None:
        return [name for name, _ in nfc_sorted_items(statistics.levels)]
    return [name for name, _ in nfc_sorted_items(dict.fromkeys(statistics.support or ()))]


def _declared_weights(statistics: ColumnStatistics, given_level: Optional[str]) -> List[Fraction]:
    """The declared weights of the classes of :func:`class_names`, before any normalising."""
    if statistics.bins is not None:
        if given_level is not None and statistics.bin_weights_given is not None:
            weights = statistics.bin_weights_given[given_level]
        else:
            weights = statistics.bin_weights or ()
        return [Fraction(weight) for weight in weights]
    names = class_names(statistics, given_level)
    if given_level is not None and statistics.level_weights_given is not None:
        table = statistics.level_weights_given[given_level]
        return [Fraction(table[name]) for name in names]
    if statistics.levels is not None:
        return [Fraction(statistics.levels[name]) for name in names]
    return [Fraction(1)] * len(names)


def class_weights(
    statistics: ColumnStatistics, given_level: Optional[str] = None
) -> List[Fraction]:
    """The weights of :func:`class_names`' classes, then NULL's when the column declares one.

    The declared weights are exact rationals normalised by their sum, or equal when a
    categorical column declares only a support. A ``nullFraction`` above 0 adds NULL as the last
    class with that weight and scales the others by ``1 - nullFraction``.
    """
    declared = _declared_weights(statistics, given_level)
    total = sum(declared)
    if not declared or total <= 0:
        raise FluxError(FLX_3402, f"{statistics.column}: the classes carry no weight to allocate")
    shares = [weight / total for weight in declared]
    null = statistics.null_fraction
    if null is None or null == 0:
        return shares
    return [share * (1 - Fraction(null)) for share in shares] + [Fraction(null)]


def exact_counts(rows: int, weights: Sequence[Fraction]) -> List[int]:
    """``LRM(rows, weights)`` of KERNEL-SPEC section 5, each class keyed by its position.

    A class is keyed by its position in decimal, zero-padded to the width of the largest
    position, so the keys sort in class order and a tie goes to the lower class, as
    :func:`flux_engine.generator.counts.class_counts` keys the classes of a shape. The weights
    stay exact rationals: a share such as one third is not rounded to a decimal first.
    """
    if rows == 0:
        return [0] * len(weights)
    width = len(str(len(weights) - 1))
    keyed = {str(position).zfill(width): weight for position, weight in enumerate(weights)}
    counts = largest_remainder(rows, cast("Mapping[str, Decimal]", keyed))
    return [counts[name] for name in keyed]


def _starts(counts: Sequence[int]) -> List[int]:
    found, total = [], 0
    for count in counts:
        found.append(total)
        total += count
    return found


@lru_cache(maxsize=_KEPT)
def _layout(rows: int, names: Tuple[Label, ...], weights: Tuple[Fraction, ...]) -> Layout:
    counts = exact_counts(rows, weights)
    return names, tuple(counts), tuple(_starts(counts))


#: The classes and weights of a declaration, by the declaration itself (the statistics hold
#: dicts, so they do not hash). The entry keeps the statistics alive, so the identity it is keyed
#: by is never reused, and the oldest entry goes first.
_CLASSES: "OrderedDict[Tuple[int, Optional[str]], Tuple[ColumnStatistics, Tuple[Label, ...], Tuple[Fraction, ...]]]" = (OrderedDict())


def _classes_of_declaration(
    statistics: ColumnStatistics, given_level: Optional[str]
) -> Tuple[Tuple[Label, ...], Tuple[Fraction, ...]]:
    """The class labels (NULL last) and weights of *statistics*, formed once per declaration."""
    key = (id(statistics), given_level)
    held = _CLASSES.get(key)
    if held is not None and held[0] is statistics:
        _CLASSES.move_to_end(key)
        return held[1], held[2]
    names: Tuple[Label, ...] = tuple(class_names(statistics, given_level))
    weights = tuple(class_weights(statistics, given_level))
    if len(weights) > len(names):
        names = (*names, None)
    _CLASSES[key] = (statistics, names, weights)
    while len(_CLASSES) > _KEPT:
        _CLASSES.popitem(last=False)
    return names, weights


def layout_of(statistics: ColumnStatistics, rows: int, given_level: Optional[str] = None) -> Layout:
    """The classes of *statistics*' column over *rows* rows, their counts and first slots."""
    names, weights = _classes_of_declaration(statistics, given_level)
    return _layout(rows, names, weights)


def _classes_of(starts: Sequence[int], slots: "np.ndarray") -> List[int]:
    """The class of each slot: the last class that starts at or before it, so never an empty one."""
    return (np.searchsorted(np.array(starts, dtype=np.int64), slots, side="right") - 1).tolist()


def _slots(key: bytes, domain: int, at: Any) -> "np.ndarray":
    return np.array(kernel2.permute2_array(key, domain, at), dtype=np.int64)


def allocated_levels(
    statistics: ColumnStatistics,
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    rows: int,
    at: Any,
) -> List[Label]:
    """The class of each row in *at* (an int64 array of row indexes), for a column given nothing.

    A level's name, a bin's index, or ``None`` for NULL.
    """
    names, _counts_of, starts = layout_of(statistics, rows)
    key = allocate_key(root, sim_id, expose_id, statistics.column)
    return [names[k] for k in _classes_of(starts, _slots(key, rows, at))]


def allocated_given(
    statistics: ColumnStatistics,
    parent: ColumnStatistics,
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    rows: int,
    at: Any,
) -> List[Label]:
    """The class of each row in *at* for a column given the exact column *parent* (section 8.3).

    A row's parent class is the class of its slot in the parent's layout; the rows of that
    class are its slots, the row's index among them is its slot less the class's first slot, and
    the column's own counts and permutation over a domain of the class's rows give the class.
    """
    names, counts, starts = layout_of(parent, rows)
    parent_slots = _slots(allocate_key(root, sim_id, expose_id, parent.column), rows, at)
    parent_classes = np.array(_classes_of(starts, parent_slots), dtype=np.int64)
    key = allocate_key(root, sim_id, expose_id, statistics.column)
    found: List[Label] = [None] * parent_slots.shape[0]
    for level in np.unique(parent_classes).tolist():
        take = np.nonzero(parent_classes == level)[0]
        size = counts[level]
        if names[level] is None:
            # A given column has no level to condition on in a NULL (plan time refuses this first).
            raise FluxError(
                FLX_3402,
                f"{statistics.column}: given {parent.column!r}, whose NULL class has no level "
                "to condition on",
            )
        group = layout_of(statistics, size, str(names[level]))
        within = _slots(key, size, parent_slots[take] - starts[level])
        for position, k in zip(take.tolist(), _classes_of(group[2], within), strict=True):
            found[position] = group[0][k]
    return found
