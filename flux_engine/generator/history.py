# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's layout: entities per parent and versions per entity (KERNEL-SPEC-2 section 8.4).

A Simulation whose namespace carries a ``history`` entry for an emit lays that emit's rows out as
a chain of versions per entity, with the counted layout of KERNEL-SPEC-2 section 8.1 used twice,
one inside the other:

* the **entity layout** ``A`` gives each eligible parent row its entities, when the entry names a
  ``parent``; without one entity ``e`` has no parent;
* the **version layout** ``V`` gives each entity its versions, at least one, so that version row
  ``i`` is version ``i - V.offsets[e]`` of entity ``e = V.parent_of(i)``.

:func:`plan_history` works both out once per process, as
:class:`~flux_engine.generator.relations.RelationReader` works a counted layout out, and keeps them
in a small cache. It also draws each entity's first start, since the room an entity has, ``until``
less its first start, is what the rank coupling of section 8.1 gives the versions by.

**The planner never reads ``spec.timeBounds.end``.** Its window is the start alone
(:func:`window_start`), and the horizon is the entry's ``until``, so a window that grows leaves
every layout, first start and row where it was (ADR 0017 section 10).

**The counts are known without a draw.** As a multiset, the versions per entity follow from the
rows, the entities and the shape alone, so :func:`largest_count`, the largest count ``c`` an
entity takes, is worked out before any first start is drawn and bounds the interval the first
starts are drawn in. Among ranks whose remainders tie, the largest count goes to the entity with
the larger slot, so ``c`` is never below the largest count any assignment of slots to entities
takes.

Every refusal is ``FLX-3403``.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import lru_cache
from itertools import accumulate
from math import lcm
from threading import local
from typing import Any, Callable, Dict, Hashable, List, Mapping, Optional, Sequence, Tuple, Union
from weakref import WeakKeyDictionary

import numpy as np

from flux_engine.core import kernel2
from flux_engine.core.canonical import nfc
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from flux_engine.core.lrm import largest_remainder
from flux_engine.core.permute import permute2
from flux_engine.core.rng import bounded_uint, uniform53
from flux_engine.core.seedtree import SeedTreeRoot, counter_stream, key_label, path_key
from flux_engine.generator.counts import (
    CountedLayout,
    TooFewChildRows,
    check_minimum,
    counted_layout,
    digest_key,
    pass_excess,
)
from flux_engine.generator.paths import (
    CHANGE_PATH_SHAPE,
    ENDED_PATH_SHAPE,
    ENTITIES_PATH_SHAPE,
    HISTORY_PATH_SHAPE,
    REASON_PATH_SHAPE,
    VERSIONS_PATH_SHAPE,
    change_path,
    ended_path,
    entities_path,
    history_path,
    reason_path,
    versions_path,
)
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.counts import ChildShape
from flux_engine.seam.history import (
    END_SHARE_OF_ELIGIBLE,
    Histogram,
    HistoryEntry,
    HistoryTracked,
)

__all__ = [
    "Chain",
    "HistoryBinding",
    "HistoryPick",
    "HistoryPlan",
    "LevelTable",
    "OuterInputs",
    "DeferredBound",
    "ParentBound",
    "ParentCut",
    "ReasonPlan",
    "TrackedSpec",
    "chain_unit_kind",
    "build_chain",
    "chain_columns",
    "check_layout",
    "drawn_length",
    "declared_ended",
    "end_share_note",
    "ended_count",
    "ended_entities",
    "ended_key",
    "entities_key",
    "entity_chain",
    "entity_states",
    "first_start_at",
    "fit_lengths",
    "largest_count",
    "limit_of",
    "limits_of",
    "parent_bound_notes",
    "length_table",
    "level_table",
    "open_end_unit",
    "plan_history",
    "reason_key",
    "tracked_levels",
    "tracked_levels_kept",
    "tracked_spec",
    "unit_of",
    "versions_key",
    "window_start",
]

_DAY_MS = 86_400_000
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_TWO_53 = 1 << 53
#: A key no path owns: the counts of a layout as a multiset never read it.
_NO_KEY = bytes(32)
#: How many histories a process keeps (each holds arrays over its entities and rows).
_PLANS_KEPT = 4
_PLANS: Dict[Tuple[Any, ...], "HistoryPlan"] = {}
_OUTERS: Dict[Tuple[Any, ...], CountedLayout] = {}


def _code() -> Any:
    from flux_engine.generator import relations

    return relations.FLX_3403


def _refuse(message: str) -> FluxError:
    """``FLX-3403`` (imported here: relations imports this module)."""
    return FluxError(_code(), message)


# -- the keys -----------------------------------------------------------------------------------


def entities_key(root: SeedTreeRoot, sim_id: str, expose_id: str, relationship: str) -> bytes:
    """``K_A``: the 32-byte key of ``("sim", sim_id, "gen", exposeId, "entities", relationship)``."""
    return digest_key(root, entities_path(sim_id, expose_id, relationship), ENTITIES_PATH_SHAPE)


def versions_key(root: SeedTreeRoot, sim_id: str, expose_id: str) -> bytes:
    """``K_V``: the 32-byte key of ``("sim", sim_id, "gen", exposeId, "versions")``."""
    return digest_key(root, versions_path(sim_id, expose_id), VERSIONS_PATH_SHAPE)


def ended_key(root: SeedTreeRoot, sim_id: str, expose_id: str) -> bytes:
    """``K_end``: the 32-byte key of ``("sim", sim_id, "gen", exposeId, "ended")``."""
    return digest_key(root, ended_path(sim_id, expose_id), ENDED_PATH_SHAPE)


def reason_key(root: SeedTreeRoot, sim_id: str, expose_id: str) -> bytes:
    """``K_reason``: the 32-byte key of ``("sim", sim_id, "gen", exposeId, "reason")``."""
    return digest_key(root, reason_path(sim_id, expose_id), REASON_PATH_SHAPE)


# -- the chain's unit ---------------------------------------------------------------------------


def chain_unit_kind(kind: LogicalKind) -> LogicalKind:
    """*kind* when it is a chain's column type, a whole day (DATE) or a millisecond (TIMESTAMP)."""
    if kind not in (LogicalKind.DATE, LogicalKind.TIMESTAMP):
        raise _refuse(f"a validity chain is DATE or TIMESTAMP, not {kind.value}")
    return kind


def unit_of(text: str, kind: LogicalKind) -> int:
    """A value of the start column's type as an instant in the chain's unit.

    Days since the epoch for a DATE, milliseconds since the epoch for a TIMESTAMP.
    """
    try:
        if kind is LogicalKind.DATE:
            return date.fromisoformat(text).toordinal() - _EPOCH_ORDINAL
        return parse_rfc3339_ms(text)
    except (ValueError, FluxError):
        raise _refuse(f"{text!r} is not a {chain_unit_kind(kind).value} value") from None


def window_start(time_bounds: Any) -> int:
    """``timeBounds.start`` in milliseconds, the one member of the window a history reads.

    *time_bounds* is the Simulation's ``spec.timeBounds`` (a mapping with an RFC 3339 ``start``)
    or a plan's ``(startMs, endMs)``. The end is never read.
    """
    if isinstance(time_bounds, Mapping):
        text = time_bounds["start"]
        if not isinstance(text, str):
            raise _refuse("timeBounds.start is an RFC 3339 string")
        return parse_rfc3339_ms(text)
    return int(time_bounds[0])


def _in_unit(milliseconds: int, kind: LogicalKind) -> int:
    return milliseconds // _DAY_MS if kind is LogicalKind.DATE else milliseconds


# -- what a plan carries ------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryPick:
    """What an emit's plan carries of its history: the entry, the chain's type and the counts.

    ``rows`` is ``N`` and ``entities`` is ``E``, both after ``--scale``; ``window_start`` is
    ``timeBounds.start`` in milliseconds. The pick is part of the plan's document, so a changed
    declaration moves the plan's digest.
    """

    entry: HistoryEntry
    kind: LogicalKind
    rows: int
    entities: int
    window_start: int
    parent_lifetime: Optional[Tuple[str, Optional[str]]] = None
    #: The rows the contract declares for a history that keeps to its parent's lifetime, after
    #: ``--scale``. The layout is worked out from them and may hold fewer (ADR 0017, amendment of
    #: 2026-11-24), when ``rows`` is what it holds.
    declared_rows: Optional[int] = None

    @property
    def laid_rows(self) -> int:
        """The rows the layout is worked out from: the declared ones for a bound history, else ``rows``."""
        return self.rows if self.declared_rows is None else self.declared_rows

    def as_document(self) -> Dict[str, Any]:
        """The ``history`` member of a driver's plan document: what the entry declares."""
        document: Dict[str, Any] = {
            "history": self.entry.as_member(),
            "kind": self.kind.value,
            "rows": self.rows,
            "entities": self.entities,
            "windowStartMs": self.window_start,
        }
        if self.parent_lifetime is not None:
            # Only a history bound by its parent's lifetime lists the columns it reads.
            document["parentLifetime"] = list(self.parent_lifetime)
        if self.declared_rows is not None and self.declared_rows != self.rows:
            # The layout holds fewer rows than the contract declares.
            document["declaredRows"] = self.declared_rows
        return document


@dataclass(frozen=True)
class OuterInputs:
    """What the entity layout reads of the parent: its rows, the entry's keys and the parent's rows.

    ``shape``, ``minimum`` and ``eligible`` are the ``parent`` entry's, read as a ``counts``
    entry's; ``expected`` is an expected-count hook's value for each eligible parent in rank
    order. ``identity`` stands for ``eligible`` and ``expected`` in the cache (a digest of the
    parent's plan and the hook); without one the cache holds them whole.
    """

    relationship: str
    parents: int
    shape: Optional[ChildShape] = None
    minimum: int = 0
    eligible: Optional[Sequence[int]] = field(default=None, hash=False, compare=False)
    expected: Optional[Sequence[Fraction]] = field(default=None, hash=False, compare=False)
    identity: Optional[Hashable] = field(default=None, hash=False, compare=False)

    def cache_key(self) -> Tuple[Any, ...]:
        """What the plan cache holds for these inputs."""
        held = self.identity
        if held is None:
            held = (
                None if self.eligible is None else tuple(self.eligible),
                None if self.expected is None else tuple(self.expected),
            )
        return (self.relationship, self.parents, self.shape, self.minimum, held)


@dataclass(frozen=True, eq=False)
class ParentBound:
    """The lifetime of each parent row in the chain's unit (ADR 0017, amendment of 2026-11-24).

    ``starts[p]`` is parent row ``p``'s lifetime start, meaningful where ``has_start[p]``; a parent
    whose start is NULL bounds nothing. ``ends[p]`` is its last day (a DATE end bounds a TIMESTAMP
    chain up to the last millisecond of its day, ADR 0024 section 5), meaningful where
    ``has_end[p]``; a parent with no end, a NULL end, or an end before its start has none, and so
    cuts nothing. ``identity`` stands for the arrays in the plan cache.
    """

    starts: "np.ndarray"
    has_start: "np.ndarray"
    ends: "np.ndarray"
    has_end: "np.ndarray"
    identity: Hashable

    def get(self) -> "ParentBound":
        """The bound itself (a :class:`DeferredBound` reads it when it is first needed)."""
        return self

    @classmethod
    def of(
        cls, lifetimes: Sequence[Optional[Tuple[int, Optional[int]]]], identity: Hashable
    ) -> "ParentBound":
        """The bound of parent rows whose lifetimes, ``None`` for a NULL start, are *lifetimes*."""
        size = len(lifetimes)
        starts = np.zeros(size, dtype=np.int64)
        ends = np.zeros(size, dtype=np.int64)
        has_start = np.zeros(size, dtype=bool)
        has_end = np.zeros(size, dtype=bool)
        for row, found in enumerate(lifetimes):
            if found is None:
                continue
            starts[row], has_start[row] = found[0], True
            if found[1] is not None:
                ends[row], has_end[row] = found[1], True
        return cls(starts, has_start, ends, has_end, identity)


@dataclass(frozen=True, eq=False)
class DeferredBound:
    """A parent bound that is read only when a layout has to be worked out.

    The layout of a history that keeps to its parent's lifetime is kept by the bound's
    ``identity``, so a layout this process holds is found without reading a parent row; the
    ``load`` that reads them runs only for a layout it does not hold.
    """

    identity: Hashable
    load: Callable[[], ParentBound]

    def get(self) -> ParentBound:
        """The bound, read now."""
        return self.load()


@dataclass(frozen=True, eq=False)
class ParentCut:
    """What a parent's lifetime does to the entities of a history (ADR 0017, amendment of 2026-11-24).

    ``limits[e]`` is where entity ``e``'s chain must end, in the chain's unit. ``forced`` flags the
    entities whose parent ended inside the horizon (they end). ``capped`` is the number of entities
    whose version count was cut to the room their parent leaves them, ``moved`` the versions they
    lost that went to entities with room to spare, and ``shortfall`` the versions no entity had
    room for, which the layout does not hold (its rows are the declared ones less the shortfall).
    """

    limits: "np.ndarray"
    forced: "np.ndarray"
    capped: int = 0
    moved: int = 0
    shortfall: int = 0


@dataclass(frozen=True)
class ReasonPlan:
    """Which tracked column each ordinary change moves (ADR 0017 section 6).

    ``total`` is ``T``, the number of ordinary changes, and ``cumulative`` the running total of
    the changes per class, so that the slot ``permute/2(K_reason, T, t)`` of change ``t`` falls in
    the class whose run holds it. ``columns`` gives each class's place in ``tracked``.
    """

    key: bytes
    total: int
    cumulative: Tuple[int, ...]
    columns: Tuple[int, ...]

    def column_of(self, change: int) -> int:
        """The tracked column change *change* moves."""
        slot = permute2(self.key, self.total, change)
        return self.columns[bisect_right(self.cumulative, slot)]

    def columns_of(self, changes: "np.ndarray") -> "np.ndarray":
        """:meth:`column_of` for an array of changes."""
        slots = np.array(kernel2.permute2_array(self.key, self.total, changes), dtype=np.int64)
        classes = np.searchsorted(np.array(self.cumulative, dtype=np.int64), slots, side="right")
        return np.array(self.columns, dtype=np.int64)[classes]


@dataclass(frozen=True, eq=False)
class HistoryPlan:
    """The layout of one history's emit: who the entities are and how many versions each has.

    ``versions`` is ``V``: ``counts[e]`` versions for entity ``e``, version row ``i`` belonging to
    ``V.parent_of(i)``. ``per_parent`` is ``A`` (``None`` without a ``parent``). ``first_starts``
    holds each entity's first start in the chain's unit, ``c`` the largest count an entity takes,
    and ``until`` the horizon in the same unit.

    ``ended`` flags the entities whose history ends (``None`` without an ``end``); ``closes_last``
    says an ended entity's last version is closed and ``forces_ending`` that it takes the end
    value. ``changes`` holds the running count of ordinary changes before each entity, ``E + 1``
    entries, and ``reasons`` the class of each change, for a history that tracks columns.
    """

    entities: int
    rows: int
    c: int
    until: int
    first_starts: "np.ndarray"
    versions: CountedLayout
    per_parent: Optional[CountedLayout]
    ended: Optional["np.ndarray"] = None
    closes_last: bool = False
    forces_ending: bool = False
    changes: Optional["np.ndarray"] = None
    reasons: Optional[ReasonPlan] = None
    cut: Optional[ParentCut] = None

    @property
    def rooms(self) -> "np.ndarray":
        """Each entity's room: where its chain must end, less its first start, in the chain's unit."""
        return limits_of(self, np.arange(self.entities, dtype=np.int64)) - self.first_starts

    def entity_of(self, row: int) -> int:
        """The entity version row *row* belongs to."""
        return self.versions.parent_of(row)

    def entities_of(self, rows: "np.ndarray") -> "np.ndarray":
        """:meth:`entity_of` for an array of rows."""
        return self.versions.parents_of(rows)

    def version_of(self, row: int) -> int:
        """The version ordinal of *row* within its entity's chain."""
        return row - int(self.versions.offsets[self.entity_of(row)])

    def versions_of(self, rows: "np.ndarray") -> "np.ndarray":
        """:meth:`version_of` for an array of rows."""
        return rows - self.versions.offsets[self.entities_of(rows)]

    def first_row_of(self, row: int) -> int:
        """The first version row of the entity of *row*: the row a carried column is drawn on."""
        return int(self.versions.offsets[self.entity_of(row)])

    def first_rows_of(self, rows: "np.ndarray") -> "np.ndarray":
        """:meth:`first_row_of` for an array of rows."""
        return self.versions.offsets[self.entities_of(rows)]

    def parent_of_entity(self, entity: int) -> Optional[int]:
        """The parent row *entity* is a child of (``None`` without a ``parent``)."""
        return None if self.per_parent is None else self.per_parent.parent_of(entity)

    def parent_row_of(self, row: int) -> Optional[int]:
        """The parent row whose entity version row *row* is, copied whole on every version."""
        return self.parent_of_entity(self.entity_of(row))

    def parent_rows_of(self, rows: "np.ndarray") -> "np.ndarray":
        """:meth:`parent_row_of` for an array of rows (needs a ``parent``)."""
        assert self.per_parent is not None
        return self.per_parent.parents_of(self.entities_of(rows))

    def has_ended(self, entity: int) -> bool:
        """Whether *entity*'s history ends."""
        return self.ended is not None and bool(self.ended[entity])

    def ending_rows(self, rows: "np.ndarray") -> "np.ndarray":
        """Whether each of *rows* is its entity's ending version, which takes the end value."""
        if not self.forces_ending or self.ended is None or rows.shape[0] == 0:
            return np.zeros(rows.shape[0], dtype=bool)
        entities = self.entities_of(rows)
        last = self.versions.offsets[entities] + self.versions.counts[entities] - 1
        return self.ended[entities] & (rows == last)

    def closes_last_of(self, entity: int) -> bool:
        """Whether *entity*'s last version is closed (``closeLast`` and an ended entity)."""
        return self.closes_last and self.has_ended(entity)


def limit_of(plan: HistoryPlan, entity: int) -> int:
    """Where *entity*'s chain must end: ``until``, or its parent's last day when that is earlier."""
    return plan.until if plan.cut is None else int(plan.cut.limits[entity])


def limits_of(plan: HistoryPlan, entities: "np.ndarray") -> "np.ndarray":
    """:func:`limit_of` for an array of entities."""
    if plan.cut is None:
        return np.full(entities.shape[0], plan.until, dtype=np.int64)
    return plan.cut.limits[entities]


# -- the counts as a multiset -------------------------------------------------------------------


def _count_multiset(entities: int, rows: int, versions: Optional[ChildShape]) -> "np.ndarray":
    """The versions per entity as a multiset, from declared values alone (an unordered array)."""
    if versions is None:
        base, spare = divmod(rows, entities)
        counts = np.full(entities, base, dtype=np.int64)
        counts[:spare] += 1
        return counts
    # Rank r is expected to hold E - 1 - r, so the ranks take the slots in reverse order and a
    # tie between remainders, which goes to the lower rank, goes to the larger slot.
    layout = counted_layout(
        _NO_KEY,
        entities,
        rows,
        shape=versions.as_member(),
        minimum=1,
        expected=[Fraction(entities - 1 - rank) for rank in range(entities)],
    )
    return layout.counts


def largest_count(entities: int, rows: int, versions: Optional[ChildShape]) -> int:
    """``c``: the largest number of versions an entity takes, from declared values alone.

    Without a shape every entity counts alike, so it is ``ceil(rows / entities)``. With one it is
    the layout of section 8.1 over ranks that take the slots in order, ties going to the larger
    slot. ``0`` when there are no entities.
    """
    if entities == 0:
        return 0
    if versions is None:
        return -(-rows // entities)
    return int(_count_multiset(entities, rows, versions).max())


def parent_bound_notes(expose_id: str, chain: "HistoryPlan") -> List[str]:
    """What ``plan`` says of a history that keeps to its parent's lifetime, one sentence each.

    The entities whose parent's lifetime leaves room for fewer versions than they were given, and
    the versions that moved to entities with room to spare (a shortfall too, the declared rows the
    parents leave no room for), and the count of entities forced to end with their parent in one
    version, whose one version is the ending version itself and carries the end value from its
    start (ADR 0017, amendment of 2026-11-24). Advice only, except the shortfall, which the plan
    document records too (``declaredRows`` and ``shortfall``).
    """
    notes: List[str] = []
    cut = chain.cut
    if cut is not None and cut.capped:
        said = (
            f"note: history ({expose_id}): the parents' lifetimes leave {cut.capped} of "
            f"{chain.entities} entities room for fewer versions than they were given; "
            f"{cut.moved} versions moved to entities with room to spare"
        )
        if cut.shortfall:
            said += (
                f", and {cut.shortfall} found none, so the history has {chain.rows} rows "
                f"of the {chain.rows + cut.shortfall} declared"
            )
        notes.append(said)
    if chain.forces_ending and cut is not None:
        single = int((cut.forced & (chain.versions.counts == 1)).sum())
        if single:
            notes.append(
                f"note: history ({expose_id}): {single} entities end with their parent and have one "
                "version, which is their ending version: it takes the end value from its start"
            )
    return notes


def _bound_by_parent(entry: HistoryEntry) -> bool:
    """Whether the entry keeps to its parent's lifetime (``parent.lifetime``)."""
    return entry.parent is not None and entry.parent.lifetime


def _closes_last(entry: HistoryEntry) -> bool:
    """Whether an ended entity's last version is closed: ``closeLast``, or no ``end`` under a parent bound.

    A history bound by its parent's lifetime with no ``end`` ends only the entities whose parent
    ended, and closes their last version at the parent's last day (ADR 0017, amendment of
    2026-11-24).
    """
    if entry.end is None:
        return _bound_by_parent(entry)
    return entry.end.mode == "closeLast"


def _closing_words(entry: HistoryEntry) -> str:
    """What the last version needs room for, in the words of the refusal that finds none."""
    if not _closes_last(entry):
        return ""
    if entry.end is None:
        return (
            " and an entity whose parent ended has its last version closed on the parent's last day"
        )
    return " and closeLast closes the last one"


def _first_start_interval(
    entry: HistoryEntry, kind: LogicalKind, window: int, c: int
) -> Tuple[int, int, int]:
    """``J`` as its first and last value, and ``until``, in the chain's unit; empty is ``FLX-3403``."""
    until = unit_of(entry.until, kind)
    first = _in_unit(window, kind)
    reserved = c + (1 if _closes_last(entry) else 0)
    last = until - reserved - 1
    if last < first:
        raise _refuse(
            f"history ({entry.emit}): the first starts have no room. They are drawn in "
            f"[timeBounds.start, until - {reserved}), because an entity takes up to {c} "
            f"versions{_closing_words(entry)}, "
            f"and that interval is empty; move until later, start the window earlier, or "
            f"declare fewer versions per entity"
        )
    return first, last, until


def _check_sizes(entry: HistoryEntry, rows: int, entities: int, parent_rows: Optional[int]) -> None:
    """The refusals that read the sizes alone: no entity, too few rows, a parent with no rows."""
    if entities == 0 and rows > 0:
        raise TooFewChildRows(
            _code(),
            f"history ({entry.emit}) has no entity (E = 0) for its {rows} version rows; declare "
            "entities of at least one",
        )
    check_minimum(rows, entities, 1)
    if parent_rows == 0 and entities > 0:
        raise TooFewChildRows(
            _code(),
            f"history ({entry.emit}) has {entities} entities and its parent has no rows, so no "
            "parent is eligible for them",
        )


def check_layout(
    entry: HistoryEntry,
    kind: LogicalKind,
    rows: int,
    entities: int,
    window: int,
    parent_rows: Optional[int] = None,
) -> int:
    """The refusals a history's layout makes from declared values alone, and ``c``.

    ``E = 0`` with rows, more entities than rows, a ``parent`` with no rows and entities, and an
    empty first-start interval are ``FLX-3403``; the first three are
    :class:`~flux_engine.generator.counts.TooFewChildRows`, so a plan under ``--scale`` says that
    the scale is what left too few.
    """
    chain_unit_kind(kind)
    _check_sizes(entry, rows, entities, parent_rows)
    if not entities:
        return 0
    counts = _count_multiset(entities, rows, entry.versions)
    c = int(counts.max())
    _first_start_interval(entry, kind, window, c)
    return c


# -- how a history ends (ADR 0017 section 5) ---------------------------------------------------


def _eligible(entry: HistoryEntry, counts: "np.ndarray") -> "np.ndarray":
    """Which entities may end: all under ``closeLast`` without a column, else those with two versions."""
    end = entry.end
    assert end is not None
    if end.every_entity_eligible:
        return np.ones(counts.shape[0], dtype=bool)
    return counts >= 2


def declared_ended(entities: int, share: str) -> int:
    """``n = LRM(E, [share, 1 - share])[0]``: the entities the declared share asks to end, of *all* of them.

    The classes are keyed ``"0"`` and ``"1"``. Whether that many are eligible is :func:`ended_count`'s.
    """
    if entities == 0:
        return 0
    weight = _weight(share, "history.end.share")
    if not 0 <= weight <= 1:
        raise _refuse(f"history.end.share is a decimal from 0 to 1, not {share!r}")
    return largest_remainder(entities, {"0": weight, "1": 1 - weight})["0"]  # type: ignore[dict-item]


def ended_count(entities: int, eligible: int, share: str, *, of_eligible: bool = False) -> int:
    """The entities that end: ``n`` of all *entities*, or every eligible one when fewer are eligible.

    With *of_eligible*, the share of a plan recorded before the end share moved to every entity
    (:data:`~flux_engine.seam.history.END_SHARE_OF_ELIGIBLE`): ``n = LRM(E', [share, 1 - share])[0]``
    of the *eligible* entities alone, which is what every engine to 0.11.1 ended. Such a plan
    replays under it, so its run id keeps the digests it had.
    """
    if of_eligible:
        return declared_ended(eligible, share)
    return min(declared_ended(entities, share), eligible)


def end_share_note(expose_id: str, entry: HistoryEntry, plan: "HistoryPlan") -> Optional[str]:
    """One sentence when fewer entities are eligible to end than the share of every entity asks for.

    Every eligible entity then ends and the share yielded is lower than the one declared (ADR 0017,
    the amendment of 2026-11-24). It names the emit, the declared share and the yielded share as a
    decimal. It is advice only: nothing here changes a value, and no plan document carries it.
    """
    end = entry.end
    if end is None or plan.ended is None or plan.entities == 0:
        return None
    if end.share_of == END_SHARE_OF_ELIGIBLE:
        return None  # a plan recorded before the share moved to every entity declares no such share
    if int(plan.ended.sum()) >= declared_ended(plan.entities, end.share):
        return None
    yielded = Fraction(int(plan.ended.sum()), plan.entities)
    return (
        f"note: {expose_id} declares a history end share of {end.share}, a share of every "
        f"entity, and only {int(plan.ended.sum())} of {plan.entities} entities can end, so every "
        f"eligible entity ends and the share yielded is {float(yielded):.4f}"
    )


def ended_entities(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    entry: HistoryEntry,
    counts: "np.ndarray",
    forced: Optional["np.ndarray"] = None,
) -> "np.ndarray":
    """Which entities end, as a flag per entity: the eligible entity of rank ``r`` ends when ``permute/2(K_end, E', r) < n``.

    ``n`` is the share of every entity (:func:`ended_count`), chosen among the entities that can
    end; for a plan recorded before the share moved, the share of the eligible entities alone
    (``entry.end.share_of``). *forced* flags the entities whose parent ended inside the horizon (ADR 0017, amendment of
    2026-11-24). They can end whatever their versions, they end, and they count towards ``n``: the
    others end by the same permutation over the eligible entities that are not forced, ``n`` less
    the forced ones, and when the forced ones are more than ``n`` they all end and no other does.
    """
    end = entry.end
    assert end is not None
    ended = np.zeros(counts.shape[0], dtype=bool)
    eligible_flags = _eligible(entry, counts)
    held = 0
    if forced is not None:
        ended |= forced
        held = int(forced.sum())
        eligible_flags = eligible_flags & ~forced
    eligible = np.nonzero(eligible_flags)[0]
    ranks = int(eligible.shape[0])
    count = max(
        ended_count(
            int(counts.shape[0]),
            ranks + held,
            end.share,
            of_eligible=end.share_of == END_SHARE_OF_ELIGIBLE,
        )
        - held,
        0,
    )
    if count == 0:
        return ended
    images = np.array(
        kernel2.permute2_array(
            ended_key(root, sim_id, expose_id), ranks, np.arange(ranks, dtype=np.int64)
        ),
        dtype=np.int64,
    )
    ended[eligible[images < count]] = True
    return ended


# -- the first start ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _StartTable:
    """``start`` over the part of it inside ``J``: its bins' thresholds and their values inside."""

    thresholds: Tuple[int, ...]
    firsts: Tuple[int, ...]
    counts: Tuple[int, ...]

    def value(self, u: int) -> int:
        """The value one ``uniform53`` *u* picks (EXPR-SPEC-2 section 6's integer arithmetic)."""
        index = bisect_right(self.thresholds, u)
        before = self.thresholds[index - 1] if index else 0
        width = self.thresholds[index] - before
        return self.firsts[index] + ((u - before) * self.counts[index]) // width

    def values(self, u: "np.ndarray") -> "np.ndarray":
        """:meth:`value` for each of the ``uint64`` array *u*, as int64."""
        thresholds = np.array(self.thresholds, dtype=np.int64)
        draws = u.astype(np.int64)
        index = np.searchsorted(thresholds, draws, side="right")
        before = np.concatenate((np.zeros(1, dtype=np.int64), thresholds))[index]
        widths = np.array(self.thresholds, dtype=object)[index] - before.astype(object)
        counts = np.array(self.counts, dtype=object)[index]
        offsets = ((draws - before).astype(object) * counts) // widths
        firsts = np.array(self.firsts, dtype=object)[index]
        return np.array((firsts + offsets).tolist(), dtype=np.int64)


def _weight(text: str, where: str) -> Fraction:
    try:
        return Fraction(Decimal(text))
    except (InvalidOperation, ValueError):
        raise _refuse(f"{where} is a decimal weight, not {text!r}") from None


def _bin_values(edges: Sequence[int], position: int, closed: str) -> Tuple[int, int]:
    """The first and last value of bin *position*: closed below by default, the last also at the top."""
    last_bin = len(edges) - 2
    if closed == "upper":
        low = edges[position] + (0 if position == 0 else 1)
        return low, edges[position + 1]
    top = edges[position + 1] + (0 if position == last_bin else -1)
    return edges[position], top


def _start_table(
    histogram: Optional[Histogram], kind: LogicalKind, first: int, last: int, emit: str
) -> "Optional[Union[_StartTable, _CutTable]]":
    """The table of ``start`` inside ``[first, last]``, or ``None`` when no bin meets it.

    Each bin keeps its weight times its values inside over all its values, exactly; a bin with
    none inside, or with weight zero, drops out. Which of the table and a uniform draw an entity
    takes depends on ``J`` and the bins, never on a draw.
    """
    if histogram is None:
        return None
    bins = _start_bins(histogram, kind, emit)
    if bins is None:  # bins that do not run in order: the table is cut bin by bin
        edges = [unit_of(text, kind) for text in histogram.edges]
        return _table_over(histogram, edges, first, last, f"history ({emit}).start")
    return _cut_table(bins, first, last)


@dataclass(frozen=True)
class _StartBins:
    """The bins of ``start`` as integers, so the table inside any interval is cut without a pass over them.

    ``lows`` and ``highs`` are each non-empty bin's first and last value, in order and apart;
    ``weights`` are the bins' weights over one denominator (a weight that is not above zero is
    0), and ``prefix[j]`` is the sum of the first ``j`` of them.
    """

    lows: Tuple[int, ...]
    highs: Tuple[int, ...]
    weights: Tuple[int, ...]
    prefix: Tuple[int, ...]


@lru_cache(maxsize=8)
def _start_bins(histogram: Histogram, kind: LogicalKind, emit: str) -> Optional[_StartBins]:
    """The bins of *histogram* in *kind*'s unit, or ``None`` when they do not run in order and apart."""
    edges = [unit_of(text, kind) for text in histogram.edges]
    closed = histogram.closed or "lower"
    fractions = [_weight(text, f"history ({emit}).start.binWeights") for text in histogram.weights]
    denominator = lcm(*(weight.denominator for weight in fractions)) if fractions else 1
    lows: List[int] = []
    highs: List[int] = []
    weights: List[int] = []
    for position, weight in enumerate(fractions):
        low, high = _bin_values(edges, position, closed)
        if high < low:
            continue  # a bin with no value drops out
        if lows and low <= highs[-1]:
            return None
        lows.append(low)
        highs.append(high)
        weights.append(int(weight * denominator) if weight > 0 else 0)
    return _StartBins(tuple(lows), tuple(highs), tuple(weights), (0, *accumulate(weights)))


class _CutTable:
    """``start`` over the part of it inside ``[first, last]``, with each threshold worked out on demand.

    It answers exactly as :class:`_StartTable` over :func:`_table_over` does for the same
    interval: the bins that meet the interval are the contiguous run ``f`` to ``g``; each keeps
    its weight times its values inside over all its values, and the thresholds are the running
    shares over their total, truncated at ``2**53``. A bin between the first and the last of the
    run lies wholly inside, so a threshold is the first bin's share, the weights of the bins
    after it, over the total, all in whole numbers over ``size(f) * size(l)`` times the weights'
    denominator, and one ``value`` reads ``log2`` of the run of them. A bin of weight zero adds
    nothing, so its threshold is its predecessor's and no draw picks it.
    """

    __slots__ = ("_bins", "_first", "_f", "_l", "_scale", "_head", "_inside", "_total")

    def __init__(self, bins: _StartBins, first: int, last: int, f: int, g: int) -> None:
        self._bins = bins
        self._first = first
        self._f = f
        self._l = g
        lows, highs, weights = bins.lows, bins.highs, bins.weights
        inside_f = min(highs[f], last) - max(lows[f], first) + 1
        inside_l = min(highs[g], last) - max(lows[g], first) + 1
        self._inside = (inside_f, inside_l)
        if f == g:
            # One bin: its share is the whole, so its one threshold is ``2**53``.
            self._scale = 1
            self._head = self._total = weights[f] * inside_f
            return
        # Every share is over ``size(f) * size(g)`` times the weights' denominator.
        size_f, size_l = highs[f] - lows[f] + 1, highs[g] - lows[g] + 1
        self._scale = size_f * size_l
        self._head = weights[f] * inside_f * size_l
        tail = weights[g] * inside_l * size_f
        middle = (bins.prefix[g] - bins.prefix[f + 1]) * self._scale
        self._total = self._head + middle + tail

    @property
    def empty(self) -> bool:
        """Whether no bin of the run has a weight above zero."""
        return self._total == 0

    def _threshold(self, k: int) -> int:
        if k == self._l:
            return _TWO_53
        shares = (
            self._head + (self._bins.prefix[k + 1] - self._bins.prefix[self._f + 1]) * self._scale
        )
        return (_TWO_53 * shares) // self._total

    def value(self, u: int) -> int:
        """The value one ``uniform53`` *u* picks (EXPR-SPEC-2 section 6's integer arithmetic)."""
        low, high = self._f, self._l
        while low < high:
            middle = (low + high) // 2
            if self._threshold(middle) > u:
                high = middle
            else:
                low = middle + 1
        index = low
        before = self._threshold(index - 1) if index > self._f else 0
        width = self._threshold(index) - before
        lows, highs = self._bins.lows, self._bins.highs
        if index == self._f:
            start, count = max(lows[index], self._first), self._inside[0]
        elif index == self._l:
            start, count = lows[index], self._inside[1]
        else:
            start, count = lows[index], highs[index] - lows[index] + 1
        return start + ((u - before) * count) // width

    def values(self, u: "np.ndarray") -> "np.ndarray":
        """:meth:`value` for each of the ``uint64`` array *u*, as int64."""
        return np.array([self.value(int(draw)) for draw in u.tolist()], dtype=np.int64)


def _cut_table(bins: _StartBins, first: int, last: int) -> Optional[_CutTable]:
    """The table of *bins* inside ``[first, last]``, or ``None`` when no bin of weight above zero meets it."""
    f = bisect_left(bins.highs, first)
    g = bisect_right(bins.lows, last) - 1
    if f > g:
        return None
    table = _CutTable(bins, first, last, f, g)
    return None if table.empty else table


def _table_over(
    histogram: Histogram, edges: Sequence[int], first: int, last: int, where: str
) -> Optional[_StartTable]:
    """*histogram*'s bins, whose edges are *edges* in the chain's unit, as a table inside ``[first, last]``."""
    closed = histogram.closed or "lower"
    shares: List[Fraction] = []
    firsts: List[int] = []
    counts: List[int] = []
    for position, text in enumerate(histogram.weights):
        weight = _weight(text, f"{where}.binWeights")
        low, high = _bin_values(edges, position, closed)
        inside = min(high, last) - max(low, first) + 1
        if weight > 0 and inside > 0 and high >= low:
            shares.append(weight * Fraction(inside, high - low + 1))
            firsts.append(max(low, first))
            counts.append(inside)
    if not shares:
        return None
    total = sum(shares, Fraction(0))
    thresholds: List[int] = []
    running = Fraction(0)
    for share in shares:
        running += share
        thresholds.append(int(_TWO_53 * running / total))
    return _StartTable(tuple(thresholds), tuple(firsts), tuple(counts))


def _first_start_key(root: SeedTreeRoot, sim_id: str, expose_id: str) -> kernel2.Key:
    label, _ordinals = key_label(history_path(sim_id, expose_id, 0), HISTORY_PATH_SHAPE)
    return path_key(root, label)


def _first_starts(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    entities: int,
    interval: Tuple[int, int],
    table: "Optional[Union[_StartTable, _CutTable]]",
) -> "np.ndarray":
    """Every entity's first start: position 0 of its ``history`` path, in the chain's unit."""
    if entities == 0:
        return np.zeros(0, dtype=np.int64)
    key = _first_start_key(root, sim_id, expose_id)
    rows = np.arange(entities, dtype=np.int64)
    if table is not None:
        return table.values(kernel2.column_uniform53(key, rows, 0))
    first, last = interval
    drawn = kernel2.column_bounded(key, rows, 0, last - first + 1)
    return first + drawn.astype(np.int64)


def _bounded_first_starts(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    entry: HistoryEntry,
    kind: LogicalKind,
    low: "np.ndarray",
    high: "np.ndarray",
) -> "np.ndarray":
    """Every entity's first start inside its own interval ``[low, high]``: position 0 of its ``history`` path.

    Entities that share an interval share one draw's arithmetic, the table of ``start`` inside it
    or a ``bounded`` over it, so an entity whose interval is ``J`` draws what :func:`_first_starts`
    draws.
    """
    entities = int(low.shape[0])
    found = np.zeros(entities, dtype=np.int64)
    if entities == 0:
        return found
    key = _first_start_key(root, sim_id, expose_id)
    order = np.lexsort((high, low))
    edges = np.flatnonzero(
        np.concatenate(
            ([True], (low[order][1:] != low[order][:-1]) | (high[order][1:] != high[order][:-1]))
        )
    )
    bounds = np.concatenate((edges, [entities]))
    draws: Optional[List[int]] = None
    words: Optional[List[int]] = None
    for begin, stop in zip(bounds[:-1].tolist(), bounds[1:].tolist(), strict=True):
        rows = order[begin:stop].astype(np.int64)
        first, last = int(low[rows[0]]), int(high[rows[0]])
        table = _start_table(entry.start, kind, first, last, entry.emit)
        if table is not None:
            if draws is None:
                # A row's draw is its own, wherever it is asked for: every row's, once.
                draws = kernel2.column_uniform53(
                    key, np.arange(entities, dtype=np.int64), 0
                ).tolist()
            found[rows] = np.array(
                [table.value(draws[row]) for row in rows.tolist()], dtype=np.int64
            )
        elif stop - begin > _SMALL_GROUP:
            found[rows] = first + kernel2.column_bounded(key, rows, 0, last - first + 1).astype(
                np.int64
            )
        else:
            if words is None:
                words = kernel2.column_words(key, np.arange(entities, dtype=np.int64), 0).tolist()
            found[rows] = first + _bounded_each(key, rows, words, last - first + 1)
    return found


#: The most entities that share an interval for which each one is drawn on its own (a ``bounded``
#: over a few entities costs more in array calls than in arithmetic).
_SMALL_GROUP = 8
_MASK_64 = (1 << 64) - 1


def _bounded_each(
    key: kernel2.Key, rows: "np.ndarray", words: Sequence[int], n: int
) -> "np.ndarray":
    """:func:`~flux_engine.core.kernel2.column_bounded` at draw 0 over *rows*, from their attempt-0 *words*.

    The same arithmetic one row at a time: a row whose first word clears ``2**64 mod n`` takes the
    high word of ``word * n``; the rare row it does not is drawn by ``column_bounded`` itself.
    """
    threshold = (1 << 64) % n
    found = np.zeros(rows.shape[0], dtype=np.int64)
    pending: List[int] = []
    for place, row in enumerate(rows.tolist()):
        product = words[row] * n
        if (product & _MASK_64) >= threshold:
            found[place] = product >> 64
        else:
            pending.append(place)
    if pending:
        found[pending] = kernel2.column_bounded(key, rows[pending], 0, n).astype(np.int64)
    return found


def _parent_interval(
    bound: ParentBound,
    parents: "np.ndarray",
    span: Tuple[int, int, int, int],
    closing: int,
) -> Tuple["np.ndarray", "np.ndarray", "np.ndarray", "np.ndarray"]:
    """Each entity's first-start interval, where its chain must end, and which entities have no room.

    *span* is the window's first unit, ``J``'s last, the horizon and the largest count ``c`` of
    versions an entity takes; *closing* is ``1`` when an ended entity's last version is closed. The
    chain must end at ``limit = min(until, pe)``. The first start lies in ``[max(first, ps),
    limit - c' - 1]`` when that is not empty, so the entity has the room for ``c`` versions
    (``c' = c + closing``), and so draws what it would draw without the bound where its parent cuts
    nothing; otherwise in ``[max(first, ps), limit - closing - 2]``, the room for one version.
    An entity with no such start is the third array's ``True``.
    """
    first, _last, until, c = span
    low = np.where(
        bound.has_start[parents], np.maximum(first, bound.starts[parents]), first
    ).astype(np.int64)
    limit = np.where(bound.has_end[parents], np.minimum(until, bound.ends[parents]), until).astype(
        np.int64
    )
    full = limit - (c + closing) - 1
    one = limit - (1 + closing) - 1
    high = np.where(low <= full, full, one)
    return low, high, limit, low > one


def first_start_at(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    entry: HistoryEntry,
    kind: LogicalKind,
    entity: int,
    *,
    c: int,
    time_bounds: Any,
    parent: Optional[Tuple[Optional[int], Optional[int]]] = None,
) -> int:
    """Entity *entity*'s first start drawn on its own stream: the per-row counterpart of the planner's.

    The one ``uniform53`` over the part of ``start`` inside ``J`` when a bin meets it, else one
    ``bounded`` over ``J``, at draw index 0 of ``("sim", sim_id, "gen", exposeId, "history",
    entity)``. *parent* is the entity's parent lifetime in the chain's unit, its start and its last
    day (``None`` for no bound): ``J`` is cut to it as the planner cuts it, to the room for ``c``
    versions, else for one (:func:`_parent_interval`).
    """
    first, last, until = _first_start_interval(entry, kind, window_start(time_bounds), c)
    if parent is not None:
        closing = 1 if _closes_last(entry) else 0
        low, high = first, last
        if parent[0] is not None:
            low = max(low, parent[0])
        limit = until if parent[1] is None else min(until, parent[1])
        high = limit - (c + closing) - 1
        if low > high:
            high = limit - (1 + closing) - 1
        if low > high:
            raise _refuse(f"history ({entry.emit}).parent.lifetime: the parent leaves no room")
        first, last = low, high
    table = _start_table(entry.start, kind, first, last, entry.emit)
    stream = counter_stream(root, history_path(sim_id, expose_id, entity), HISTORY_PATH_SHAPE)
    if table is not None:
        return table.value(uniform53(stream))
    return first + bounded_uint(stream, last - first + 1)


# -- the plan -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryBinding:
    """What :func:`~flux_engine.generator.relations.relate_plans` reads of one history's emit.

    ``entry`` is the declared history, ``entities`` its ``entities`` after ``--scale`` and
    ``start`` the contract's validity-start column, whose type is the chain's unit, and ``end``
    its validity-end column (``None`` leaves the end column to draw as any other).
    """

    entry: HistoryEntry
    entities: int
    start: str
    end: Optional[str] = None
    #: The start and the end column of the parent emit's ``lifetimes`` entry, read by an entry that
    #: declares ``parent.lifetime`` (ADR 0017, amendment of 2026-11-24).
    parent_lifetime: Optional[Tuple[str, Optional[str]]] = None
    #: The emit's rows as declared (after ``--scale``), for an entry that declares
    #: ``parent.lifetime``: the layout is worked out from them and may hold fewer.
    declared_rows: Optional[int] = None


def plan_history(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    entry: HistoryEntry,
    *,
    kind: LogicalKind,
    rows: int,
    entities: int,
    time_bounds: Any,
    outer: Optional[OuterInputs] = None,
    bound: Optional[Union[ParentBound, DeferredBound]] = None,
) -> HistoryPlan:
    """The layout of *entry*'s emit, worked out once per process (KERNEL-SPEC-2 section 8.4).

    *rows* is ``N`` and *entities* is ``E``, both after ``--scale``; *time_bounds* is the
    Simulation's ``spec.timeBounds`` or a plan's ``(startMs, endMs)``, of which only the start is
    read; *outer* is what the entity layout reads of the parent, given exactly when the entry
    declares a ``parent``. Every refusal is ``FLX-3403`` (:func:`check_layout`).

    *bound* is the lifetime of each parent row, given exactly when the entry declares
    ``parent.lifetime``: the entity layout is then worked out first, since each entity's first
    start and room read its parent's lifetime (ADR 0017, amendment of 2026-11-24).
    """
    window = window_start(time_bounds)
    chain_unit_kind(kind)
    _check_sizes(entry, rows, entities, None if outer is None else outer.parents)
    if _bound_by_parent(entry) != (bound is not None) or (bound is not None and outer is None):
        raise _refuse(
            f"history ({entry.emit}): the parents' lifetimes are read with the entity layout, "
            "and exactly when the entry declares parent.lifetime"
        )
    per_parent = None
    if bound is not None:
        assert outer is not None
        per_parent = _outer_layout(root, sim_id, expose_id, entities, outer)
    held: Tuple[Any, ...] = (
        root.digest,
        root.seed_tree_version,
        sim_id,
        expose_id,
        entry,
        kind,
        rows,
        entities,
        _in_unit(window, kind),
    )
    if bound is not None:
        assert outer is not None
        held = held + (outer.cache_key(), bound.identity)
    chain = _PLANS.get(held)
    if chain is None:
        chain = _chain(
            root,
            sim_id,
            expose_id,
            entry,
            kind,
            rows,
            entities,
            window,
            None if bound is None or per_parent is None else (per_parent, bound),
        )
        if len(_PLANS) >= _PLANS_KEPT:
            _PLANS.clear()
        _PLANS[held] = chain
    if outer is None:
        return chain
    if per_parent is None:
        per_parent = _outer_layout(root, sim_id, expose_id, entities, outer)
    return replace(chain, per_parent=per_parent)


def _outer_layout(
    root: SeedTreeRoot, sim_id: str, expose_id: str, entities: int, outer: OuterInputs
) -> CountedLayout:
    """The entities per parent, ``A``, worked out once per process."""
    outer_held = (
        root.digest,
        root.seed_tree_version,
        sim_id,
        expose_id,
        entities,
        outer.cache_key(),
    )
    per_parent = _OUTERS.get(outer_held)
    if per_parent is None:
        per_parent = counted_layout(
            entities_key(root, sim_id, expose_id, outer.relationship),
            outer.parents,
            entities,
            shape=None if outer.shape is None else outer.shape.as_member(),
            minimum=outer.minimum,
            eligible=outer.eligible,
            expected=outer.expected,
        )
        if len(_OUTERS) >= _PLANS_KEPT:
            _OUTERS.clear()
        _OUTERS[outer_held] = per_parent
    return per_parent


def _chain(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    entry: HistoryEntry,
    kind: LogicalKind,
    rows: int,
    entities: int,
    window: int,
    bound_by: Optional[Tuple[CountedLayout, Union[ParentBound, DeferredBound]]] = None,
) -> HistoryPlan:
    """The version layout and the first starts: everything but the entities' parents.

    With *bound_by*, the entities' layout per parent and the parents' lifetimes, each entity's
    first start and room read its parent's lifetime (:func:`_bounded_chain`).
    """
    c = largest_count(entities, rows, entry.versions)
    until = unit_of(entry.until, kind)
    starts = np.zeros(0, dtype=np.int64)
    cut = None
    if entities:
        first, last, until = _first_start_interval(entry, kind, window, c)
        if bound_by is None:
            table = _start_table(entry.start, kind, first, last, entry.emit)
            starts = _first_starts(root, sim_id, expose_id, entities, (first, last), table)
        else:
            span = (first, last, until, c)
            starts, cut = _bounded_chain(root, sim_id, expose_id, (entry, kind), span, bound_by)
    versions = counted_layout(
        versions_key(root, sim_id, expose_id),
        entities,
        rows,
        shape=None if entry.versions is None else entry.versions.as_member(),
        minimum=1,
        expected=_rooms_of(entry, until, starts, cut),
    )
    if entities and int(versions.counts.max()) > c:
        raise _refuse(
            f"history ({entry.emit}): an entity takes {int(versions.counts.max())} versions, "
            f"more than the {c} the first starts leave room for"
        )
    if cut is not None:
        versions, cut = _fitted_counts(versions, starts, cut, 1 if _closes_last(entry) else 0)
    plan = HistoryPlan(
        entities=entities,
        rows=versions.children,
        c=c,
        until=until,
        first_starts=starts,
        versions=versions,
        per_parent=None,
        closes_last=_closes_last(entry),
        cut=cut,
    )
    return _with_endings(root, sim_id, expose_id, entry, plan)


def _bounded_chain(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    declared: Tuple[HistoryEntry, LogicalKind],
    span: Tuple[int, int, int, int],
    bound_by: Tuple[CountedLayout, Union[ParentBound, DeferredBound]],
) -> Tuple["np.ndarray", ParentCut]:
    """Every entity's first start inside its parent's lifetime, and what the lifetimes do to it.

    *span* is the first starts' interval, the horizon and the largest count of versions an entity
    takes. A parent that leaves an entity no room for even one version is ``FLX-3403``.
    """
    entry, kind = declared
    until = span[2]
    per_parent, bound = bound_by
    lifetimes = bound.get()
    parents = per_parent.parents_of(np.arange(per_parent.children, dtype=np.int64))
    low, high, limits, short = _parent_interval(
        lifetimes, parents, span, 1 if _closes_last(entry) else 0
    )
    if bool(short.any()):
        raise _no_room(entry, kind, lifetimes, parents[short], int(parents.shape[0]), until)
    starts = _bounded_first_starts(root, sim_id, expose_id, entry, kind, low, high)
    closing = lifetimes.has_end[parents]
    forced = closing & (lifetimes.ends[parents] <= until)
    return starts, ParentCut(limits, forced)


def _no_room(
    entry: HistoryEntry,
    kind: LogicalKind,
    bound: ParentBound,
    parents: "np.ndarray",
    entities: int,
    until: int,
) -> FluxError:
    """``FLX-3403`` for the entities whose parent leaves no room for one version: how many, and their parents' lifetimes."""
    opened = bound.starts[parents][bound.has_start[parents]]
    closed = bound.ends[parents][bound.has_end[parents]]

    def spread(found: "np.ndarray") -> str:
        return (
            "none"
            if found.shape[0] == 0
            else f"{_unit_text(int(found.min()), kind)} to {_unit_text(int(found.max()), kind)}"
        )

    return _refuse(
        f"history ({entry.emit}).parent.lifetime: {int(parents.shape[0])} of {entities} entities "
        "have a parent whose lifetime leaves no room for one version before "
        f"until ({_unit_text(until, kind)}){_closing_words(entry)}; the parents of those entities "
        f"open {spread(opened)} and close {spread(closed)}; give the parents longer lifetimes, "
        "move until later, or drop parent.lifetime"
    )


def _unit_text(value: int, kind: LogicalKind) -> str:
    """An instant in the chain's unit as the date or timestamp a contract writes."""
    if kind is LogicalKind.DATE:
        return date.fromordinal(value + _EPOCH_ORDINAL).isoformat()
    moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fitted_counts(
    versions: CountedLayout, starts: "np.ndarray", cut: ParentCut, closing: int
) -> Tuple[CountedLayout, ParentCut]:
    """The version counts held to each entity's room, and the versions that lost their place passed on.

    An entity's chain holds ``room - closing - 1`` versions, the first start and a closed last
    version each taking a unit (ADR 0017 section 4, point 4), and at least one by the first start's
    interval. Each count is capped at that; the versions above it go one at a time, in rounds, to
    the entities still under their own cap, the most room first and a tie to the lower entity: the
    cap-and-pass of :func:`~flux_engine.generator.counts.pass_excess` (``counts._capped``'s
    allotment, with a cap for each entity and the room as the weight, which is the order the rank
    coupling gives the counts by). Versions no entity has room for are the ``shortfall``: the
    layout holds fewer rows than it was asked for.
    """
    rooms = cut.limits - starts
    caps = rooms - closing - 1
    counts = versions.counts
    kept = np.minimum(counts, caps)
    lost = int((counts - kept).sum())
    if lost == 0:
        return versions, cut
    fitted, left = pass_excess(kept, caps, rooms, lost)
    layout = CountedLayout(
        counts=fitted, offsets=np.cumsum(fitted) - fitted, children=int(fitted.sum())
    )
    held = replace(cut, capped=int((counts > caps).sum()), moved=lost - left, shortfall=left)
    return layout, held


def _rooms_of(
    entry: HistoryEntry, until: int, starts: "np.ndarray", cut: Optional[ParentCut]
) -> Optional[List[Fraction]]:
    """Each entity's room, for a shape: the entity with the most room takes the most versions.

    Without a shape every entity counts alike and the rooms are not read.
    """
    if entry.versions is None:
        return None
    return [
        Fraction(int(room)) for room in ((until if cut is None else cut.limits) - starts).tolist()
    ]


def _with_endings(
    root: SeedTreeRoot, sim_id: str, expose_id: str, entry: HistoryEntry, plan: HistoryPlan
) -> HistoryPlan:
    """*plan* with the entities that end and, for a history that tracks columns, its ordinary changes."""
    versions, forced = plan.versions, None if plan.cut is None else plan.cut.forced
    if entry.end is None:
        ended = forced if _bound_by_parent(entry) else None
    else:
        ended = _ended(root, sim_id, expose_id, entry, versions, forced)
    forces = entry.end is not None and entry.end.column is not None
    changes = reasons = None
    if entry.tracked:
        changes = _change_offsets(versions.counts, ended if forces else None)
        reasons = _reason_plan(root, sim_id, expose_id, entry, int(changes[-1]))
    return replace(plan, ended=ended, forces_ending=forces, changes=changes, reasons=reasons)


def _ended(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    entry: HistoryEntry,
    versions: CountedLayout,
    forced: Optional["np.ndarray"] = None,
) -> "np.ndarray":
    return ended_entities(root, sim_id, expose_id, entry, versions.counts, forced)


def _change_offsets(counts: "np.ndarray", forced: Optional["np.ndarray"]) -> "np.ndarray":
    """The ordinary changes before each entity, ``E + 1`` running totals.

    An entity has one change for each version after its first, less its ending version, which is
    forced and no change reason.
    """
    changes = counts.astype(np.int64) - 1
    if forced is not None:
        # An entity forced to end by its parent may have one version, which is its ending version.
        changes = np.maximum(changes - forced.astype(np.int64), 0)
    return np.concatenate((np.zeros(1, dtype=np.int64), np.cumsum(changes)))


def _reason_plan(
    root: SeedTreeRoot, sim_id: str, expose_id: str, entry: HistoryEntry, total: int
) -> Optional[ReasonPlan]:
    """The classes of the ordinary changes: ``LRM(T, reasons weights)`` keyed as ADR 0016 keys classes.

    ``None`` for one tracked column, which is the reason of every change.
    """
    if len(entry.tracked) < 2:
        return None
    reasons = entry.reasons
    if reasons is None:
        raise _refuse(f"history ({entry.emit}): reasons name the tracked columns' shares")
    places = {tracked.column: place for place, tracked in enumerate(entry.tracked)}
    try:
        columns = tuple(places[name] for name in reasons.values)
    except KeyError as unknown:
        raise _refuse(
            f"history ({entry.emit}).reasons names {unknown}, which is not tracked"
        ) from None
    weights = _exact_weights(reasons.weights, f"history ({entry.emit}).reasons.levelWeights")
    width = len(str(len(weights) - 1))
    keyed = {str(place).zfill(width): weight for place, weight in enumerate(weights)}
    held = list(largest_remainder(total, keyed).values()) if total else [0] * len(weights)  # type: ignore[arg-type]
    return ReasonPlan(
        key=reason_key(root, sim_id, expose_id),
        total=total,
        cumulative=tuple(accumulate(held)),
        columns=columns,
    )


# -- the validity chain (ADR 0017 section 4) -----------------------------------------------------


@dataclass(frozen=True)
class Chain:
    """One entity's versions: each start and each end in the chain's unit.

    ``ends[j]`` is ``None`` for the open last version, whose end is NULL or ``openEnd``.
    """

    starts: Tuple[int, ...]
    ends: Tuple[Optional[int], ...]


@lru_cache(maxsize=64)
def length_table(entry: HistoryEntry, kind: LogicalKind) -> _StartTable:
    """``length`` as a table over whole units: day bins, or the milliseconds inside them.

    Read as EXPR-SPEC-2 section 6 reads a histogram, so a length is one ``uniform53`` and exact
    integer arithmetic, of at least one unit.
    """
    scale = _DAY_MS if kind is LogicalKind.TIMESTAMP else 1
    where = f"history ({entry.emit}).length"
    try:
        edges = [int(Decimal(text)) * scale for text in entry.length.edges]
    except (InvalidOperation, ValueError):
        raise _refuse(f"{where}: the bin edges are whole days") from None
    table = _table_over(entry.length, edges, 1, 1 << 62, where)
    if table is None:
        raise _refuse(f"{where} has no bin with a weight above zero")
    return table


def open_end_unit(entry: HistoryEntry, kind: LogicalKind) -> Optional[int]:
    """The open end as an instant in the chain's unit: ``openEnd`` read as the type, else ``None`` (NULL)."""
    return None if entry.open_end is None else unit_of(entry.open_end, kind)


def _change_key(root: SeedTreeRoot, sim_id: str, expose_id: str) -> kernel2.Key:
    label, _ordinals = key_label(change_path(sim_id, expose_id, 0, 0), CHANGE_PATH_SHAPE)
    return path_key(root, label)


def drawn_length(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    entry: HistoryEntry,
    kind: LogicalKind,
    entity: int,
    version: int,
) -> int:
    """Version *version* of entity *entity*'s length as drawn, before the fit.

    One ``uniform53`` at position 0 of ``("sim", sim_id, "gen", exposeId, "change", entity,
    version)``, read through ``length`` at the chain's unit.
    """
    stream = counter_stream(
        root, change_path(sim_id, expose_id, entity, version), CHANGE_PATH_SHAPE
    )
    return length_table(entry, kind).value(uniform53(stream))


def fit_lengths(lengths: Sequence[int], room: int) -> List[int]:
    """The closed *lengths* of one chain fitted into *room* units (ADR 0017 section 4, point 4).

    Lengths whose sum is at most ``room - 1`` stay. Otherwise each keeps one unit and the other
    ``room - 1 - k`` units are allotted in proportion to ``d_j - 1`` by the Largest Remainder
    Method on exact integers, ties to the lower version. It draws nothing and never fails for a
    chain that has the room the first start leaves it.
    """
    closed = len(lengths)
    if sum(lengths) <= room - 1:
        return list(lengths)
    spare = room - 1 - closed
    if spare < 0:
        raise _refuse(f"a chain of {closed} closed versions does not fit in {room} units")
    weights = [length - 1 for length in lengths]
    total = sum(weights)
    products = [spare * weight for weight in weights]
    allotted = [product // total for product in products]
    remainders = [product % total for product in products]
    leftover = spare - sum(allotted)
    for version in sorted(range(closed), key=lambda j: (-remainders[j], j))[:leftover]:
        allotted[version] += 1
    return [1 + share for share in allotted]


def instant_ms(value: Any) -> int:
    """A DATE cell (a date) or a TIMESTAMP cell (milliseconds) as milliseconds since the epoch.

    A DATE is the instant of its midnight UTC (ADR 0017 section 8).
    """
    if isinstance(value, date):
        return (value.toordinal() - _EPOCH_ORDINAL) * _DAY_MS
    return int(value)


def version_valid_on(
    chain: Chain, entry: HistoryEntry, kind: LogicalKind, moment: int
) -> Optional[int]:
    """The version of *chain* valid at *moment* (milliseconds since the epoch), or ``None`` (ADR 0017 section 8).

    The version is the one whose start is not after *moment* and whose end is open or, under
    ``nextStart``, after it, or, under ``dayBefore``, not before it, a DATE end covering its whole
    day. It is found by bisection over the starts. *kind* is the type of the chain's columns.
    """
    unit = _DAY_MS if kind is LogicalKind.DATE else 1
    found = bisect_right(chain.starts, moment // unit) - 1
    if found < 0:
        return None
    end = chain.ends[found]
    if end is None:
        return found
    step = 1 if _day_before(entry, kind) else 0
    return found if moment < (end + step) * unit else None


def build_chain(
    first: int,
    drawn: Sequence[int],
    until: int,
    *,
    day_before: bool = False,
    closed_last: bool = False,
) -> Chain:
    """The chain of an entity with first start *first* and the *drawn* lengths of its closed versions.

    The last version is open, unless *closed_last* closes it too, so that every version has a
    drawn length. Under ``nextStart`` a closed version ends where the next one starts, or where its
    own length ends; under ``dayBefore`` the day before.
    """
    fitted = fit_lengths(drawn, until - first)
    reached = [first]
    for length in fitted:
        reached.append(reached[-1] + length)
    step = 1 if day_before else 0
    ends: List[Optional[int]] = [start - step for start in reached[1:]]
    if closed_last:
        return Chain(tuple(reached[:-1]), tuple(ends))
    ends.append(None)
    return Chain(tuple(reached), tuple(ends))


def _day_before(entry: HistoryEntry, kind: LogicalKind) -> bool:
    if entry.chain != "dayBefore":
        return False
    if kind is not LogicalKind.DATE:
        raise _refuse(f"history ({entry.emit}): dayBefore is for DATE columns, not {kind.value}")
    return True


def entity_chain(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    entry: HistoryEntry,
    kind: LogicalKind,
    plan: HistoryPlan,
    entity: int,
) -> Chain:
    """Entity *entity*'s chain on the per-row path: one length drawn per closed version."""
    closes = plan.closes_last_of(entity)
    versions = int(plan.versions.counts[entity])
    drawn = [
        drawn_length(root, sim_id, expose_id, entry, kind, entity, version)
        for version in range(versions - (0 if closes else 1))
    ]
    return build_chain(
        int(plan.first_starts[entity]),
        drawn,
        limit_of(plan, entity),
        day_before=_day_before(entry, kind),
        closed_last=closes,
    )


def chain_columns(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    entry: HistoryEntry,
    kind: LogicalKind,
    plan: HistoryPlan,
    rows: "np.ndarray",
) -> Tuple["np.ndarray", "np.ndarray", "np.ndarray"]:
    """The start, the end and whether it is open, for each version row of *rows*, as arrays.

    Kernel 0.2.0's column path: the lengths of every closed version of the entities the rows
    belong to are one Philox call under the ``change`` key, and the chains are cumulative sums.
    An entity whose lengths overrun its room takes :func:`fit_lengths`, which draws nothing.
    The values are :func:`entity_chain`'s, entity by entity.
    """
    if rows.shape[0] == 0:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty, np.zeros(0, dtype=bool)
    day_before = _day_before(entry, kind)
    row_entities = plan.entities_of(rows)
    ents = np.unique(row_entities)
    counts = plan.versions.counts[ents].astype(np.int64)
    # The closed versions of an entity: all but the last, or all of them when its last is closed.
    closed_counts = counts - 1
    if plan.closes_last and plan.ended is not None:
        closed_counts = closed_counts + plan.ended[ents].astype(np.int64)
    local = np.concatenate((np.zeros(1, dtype=np.int64), np.cumsum(counts)))[:-1]
    total = int(counts.sum())
    owner = np.repeat(np.arange(ents.shape[0]), counts)
    version = np.arange(total, dtype=np.int64) - local[owner]
    closed = version < closed_counts[owner]
    lengths = np.zeros(total, dtype=np.int64)
    if closed.any():
        zero = np.zeros(int(closed.sum()), dtype=np.uint64)
        words = kernel2.philox_arrays(
            ents[owner[closed]].astype(np.uint64),
            version[closed].astype(np.uint64),
            zero,
            zero,
            _change_key(root, sim_id, expose_id),
        )[0]
        lengths[closed] = length_table(entry, kind).values(words >> np.uint64(11))
    firsts = plan.first_starts[ents].astype(np.int64)
    room = limits_of(plan, ents) - firsts
    sums = np.zeros(ents.shape[0], dtype=np.int64)
    np.add.at(sums, owner, lengths)
    for index in np.nonzero(sums > room - 1)[0].tolist():
        begin, count = int(local[index]), int(closed_counts[index])
        lengths[begin : begin + count] = fit_lengths(
            lengths[begin : begin + count].tolist(), int(room[index])
        )
    running = np.cumsum(lengths) - lengths
    starts = firsts[owner] + running - running[local][owner]
    ends = starts + lengths - (1 if day_before else 0)
    position = local[np.searchsorted(ents, row_entities)] + (
        rows - plan.versions.offsets[row_entities]
    )
    return starts[position], ends[position], ~closed[position]


# -- tracked columns (ADR 0017 section 6) ---------------------------------------------------------

#: The first position of a version's ``change`` path that holds a tracked column's value: tracked
#: column ``c`` draws at ``_STATE_DRAW + c``. Positions 0 to 255 are the length and what stays reserved.
_STATE_DRAW = 256


def _exact_weights(texts: Sequence[str], where: str) -> List[Fraction]:
    """The shares *texts* spell, exactly, rescaled to total one; a total of zero is ``FLX-3403``."""
    weights = [_weight(text, where) for text in texts]
    total = sum(weights, Fraction(0))
    if total <= 0:
        raise _refuse(f"{where} total zero")
    return [weight / total for weight in weights]


@dataclass(frozen=True)
class LevelTable:
    """One ``categorical`` as exact integers: its levels in NFC order and their cumulative thresholds.

    A uniform53 below the first threshold is the first level, and so on; the last level takes the
    rest, as :func:`flux_engine.core.rng.categorical` does.
    """

    thresholds: Tuple[int, ...]
    names: Tuple[str, ...]

    def pick(self, u: int) -> str:
        """The level the uniform53 *u* picks."""
        return self.names[bisect_right(self.thresholds, u)]

    def picks(self, u: "np.ndarray") -> "np.ndarray":
        """:meth:`pick` for each of the ``uint64`` array *u*, as an array of level texts."""
        bounds = np.array(self.thresholds, dtype=np.uint64)
        names = np.empty(len(self.names), dtype=object)
        names[:] = self.names
        return names[np.searchsorted(bounds, u, side="right")]


def level_table(weights: Sequence[Tuple[str, Fraction]], where: str = "weights") -> LevelTable:
    """The table of levels with exact *weights*, rescaled to total one; ordered as ``categorical`` orders levels."""
    if not weights:
        raise _refuse(f"{where} name no level")
    ordered = sorted(weights, key=lambda item: (nfc(item[0]), item[0]))
    total = sum((weight for _name, weight in ordered), Fraction(0))
    if total <= 0:
        raise _refuse(f"{where} total zero")
    thresholds: List[int] = []
    running = Fraction(0)
    for _name, weight in ordered[:-1]:
        running += weight / total
        thresholds.append(int(running * _TWO_53))
    return LevelTable(tuple(thresholds), tuple(name for name, _weight_ in ordered))


@dataclass(frozen=True)
class TrackedSpec:
    """A tracked column's draws: its initial levels and the levels each level moves to."""

    column: str
    initial: LevelTable
    moves: Tuple[Tuple[str, LevelTable], ...]
    _by_level: Dict[str, LevelTable] = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_level", dict(self.moves))

    def move(self, level: str, u: int) -> str:
        """The level a chain at *level* moves to, for the uniform53 *u*."""
        return self.table_of(level).pick(u)

    def table_of(self, level: str) -> LevelTable:
        """The row of ``transitions`` for *level*."""
        found = self._by_level.get(level)
        if found is None:
            raise _refuse(f"tracked column {self.column!r} has no transitions row for {level!r}")
        return found


def tracked_spec(tracked: HistoryTracked, own: Mapping[str, Decimal]) -> TrackedSpec:
    """*tracked* read against its column's own levels and weights, which ``initial`` defaults to.

    Weights that do not total one are rescaled exactly, as every share of the entry is.
    """
    where = f"history tracked column {tracked.column!r}"
    if tracked.initial is not None:
        pairs = [
            (level, _weight(text, f"{where}.initial"))
            for level, text in zip(tracked.initial.values, tracked.initial.weights, strict=True)
        ]
    else:
        pairs = [(level, Fraction(weight)) for level, weight in own.items()]
    moves = tuple(
        (
            level,
            level_table(
                [(target, _weight(text, f"{where}.transitions")) for target, text in row],
                f"{where}.transitions[{level!r}]",
            ),
        )
        for level, row in tracked.transitions
    )
    return TrackedSpec(tracked.column, level_table(pairs, f"{where}.initial"), moves)


def _draw_at(stream: Any, draw: int) -> int:
    """The uniform53 at draw index *draw* of a counter stream: top 53 bits of word 0, at attempt 0."""
    return int(stream.block(draw)[0]) >> 11


def _change_stream(
    root: SeedTreeRoot, sim_id: str, expose_id: str, entity: int, version: int
) -> Any:
    return counter_stream(root, change_path(sim_id, expose_id, entity, version), CHANGE_PATH_SHAPE)


def entity_states(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    specs: Sequence[TrackedSpec],
    plan: HistoryPlan,
    entity: int,
) -> List[Tuple[str, ...]]:
    """Every version's tracked levels for entity *entity*, in tracked order: the per-row path's.

    Version 0 draws each initial level at ``256 + c`` of its ``change`` path. Each later version
    but an ending version is one ordinary change: its reason's column moves from its previous level
    by one ``categorical`` over that level's row, at ``256 + c``, and the others carry. An ending
    version carries every level (its end value is set after).
    """
    count = int(plan.versions.counts[entity])
    first = _change_stream(root, sim_id, expose_id, entity, 0)
    state = [
        spec.initial.pick(_draw_at(first, _STATE_DRAW + place)) for place, spec in enumerate(specs)
    ]
    states = [tuple(state)]
    forced = plan.forces_ending and plan.has_ended(entity)
    offset = 0 if plan.changes is None else int(plan.changes[entity])
    for version in range(1, count):
        if not (forced and version == count - 1):
            change = offset + version - 1
            place = 0 if plan.reasons is None else plan.reasons.column_of(change)
            stream = _change_stream(root, sim_id, expose_id, entity, version)
            state[place] = specs[place].move(state[place], _draw_at(stream, _STATE_DRAW + place))
        states.append(tuple(state))
    return states


def _uniforms_at(key: kernel2.Key, entities: "np.ndarray", version: int, draw: int) -> "np.ndarray":
    """The uniform53 at draw *draw* of ``(entity, version)``'s ``change`` path, for each entity."""
    size = entities.shape[0]
    zero = np.zeros(size, dtype=np.uint64)
    words = kernel2.philox_arrays(
        entities.astype(np.uint64),
        np.full(size, version, dtype=np.uint64),
        np.full(size, draw, dtype=np.uint64),
        zero,
        key,
    )[0]
    return words >> np.uint64(11)


def tracked_levels(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    specs: Sequence[TrackedSpec],
    plan: HistoryPlan,
    rows: "np.ndarray",
) -> List["np.ndarray"]:
    """Each tracked column's level text for each version row of *rows*: the column path's.

    The versions of the entities the rows belong to are taken step by step across entities: at
    version ``j`` one Philox call per tracked column draws for the entities whose change moves it,
    and a ``categorical`` per level of its previous value. The values are :func:`entity_states`'s,
    entity by entity.
    """
    row_entities = plan.entities_of(rows)
    ents = np.unique(row_entities)
    counts = plan.versions.counts[ents].astype(np.int64)
    local = np.concatenate((np.zeros(1, dtype=np.int64), np.cumsum(counts)))[:-1]
    key = _change_key(root, sim_id, expose_id)
    found = [np.empty(int(counts.sum()), dtype=object) for _ in specs]
    current = []
    for place, spec in enumerate(specs):
        levels = spec.initial.picks(_uniforms_at(key, ents, 0, _STATE_DRAW + place))
        found[place][local] = levels
        current.append(levels)
    forced = (
        plan.ended[ents]
        if plan.forces_ending and plan.ended is not None
        else np.zeros_like(ents, dtype=bool)
    )
    offsets = (
        np.zeros(ents.shape[0], dtype=np.int64) if plan.changes is None else plan.changes[ents]
    )
    for version in range(1, int(counts.max())):
        active = np.nonzero(counts > version)[0]
        moving = active[~(forced[active] & (counts[active] - 1 == version))]
        if moving.shape[0]:
            _move(key, specs, plan, ents, current, moving, offsets[moving] + version - 1, version)
        for place in range(len(specs)):
            found[place][local[active] + version] = current[place][active]
    position = local[np.searchsorted(ents, row_entities)] + (
        rows - plan.versions.offsets[row_entities]
    )
    return [column[position] for column in found]


#: Rows above which :func:`tracked_levels_kept` reads a slice directly: a slice that large is the
#: column path's own pass, which replays each chain once whatever is kept.
_KEPT_SLICE_ROWS = 256
#: How many entities one run's kept levels hold before it starts over.
_KEPT_ENTITIES = 1 << 14


@dataclass(eq=False)
class _KeptLevels:
    """The tracked levels of the entities one emit has read, every version of each, kept whole."""

    plan: HistoryPlan
    specs: Tuple[TrackedSpec, ...]
    entities: Dict[int, List["np.ndarray"]] = field(default_factory=dict)


# One run's kept levels by emit, held by the run's root and gone with it: nothing here is read by
# another run, another bundle or another process. Each thread keeps its own, so no lock is needed.
_KEPT_HERE = local()


def _kept_store() -> "WeakKeyDictionary[SeedTreeRoot, Dict[Tuple[str, str], _KeptLevels]]":
    found = getattr(_KEPT_HERE, "store", None)
    if found is None:
        found = WeakKeyDictionary()
        _KEPT_HERE.store = found
    return found


def tracked_levels_kept(
    root: SeedTreeRoot,
    emit: Tuple[str, str],
    specs: Sequence[TrackedSpec],
    plan: HistoryPlan,
    rows: "np.ndarray",
) -> List["np.ndarray"]:
    """:func:`tracked_levels`, replaying each entity's chain once for the run rather than per call.

    *emit* is the simulation id and the exposeId, which :func:`tracked_levels` takes apart.

    A rule that reads a previous version asks for one row at a time, and :func:`tracked_levels`
    replays the whole chain of the row's entity to answer it. A small slice is answered from
    the levels of every version of each entity it reaches, worked out once, in one pass over
    the entities not yet kept. The values are :func:`tracked_levels`'s: an entity's levels depend
    on that entity alone. A slice of more than ``_KEPT_SLICE_ROWS`` rows is read directly.
    """
    if rows.shape[0] > _KEPT_SLICE_ROWS:
        return tracked_levels(root, *emit, specs, plan, rows)
    store = _kept_store()
    by_emit = store.get(root)
    if by_emit is None:
        by_emit = {}
        store[root] = by_emit
    kept = by_emit.get(emit)
    if kept is None or kept.plan is not plan or kept.specs != tuple(specs):
        kept = _KeptLevels(plan, tuple(specs))
        by_emit[emit] = kept
    row_entities = plan.entities_of(rows)
    held = kept.entities
    # The slice answers from what it takes out of the store here, so a clear below changes nothing.
    wanted = set(row_entities.tolist())
    taken = {e: held[e] for e in wanted if e in held}
    missing = np.array(sorted(wanted - taken.keys()), dtype=np.int64)
    if missing.shape[0]:
        offsets = plan.versions.offsets[missing].astype(np.int64)
        counts = plan.versions.counts[missing].astype(np.int64)
        whole = np.concatenate(
            [
                np.arange(first, first + count, dtype=np.int64)
                for first, count in zip(offsets, counts, strict=True)
            ]
        )
        levels = tracked_levels(root, *emit, specs, plan, whole)
        edges = np.concatenate((np.zeros(1, dtype=np.int64), np.cumsum(counts)))
        fresh = {
            entity: [column[edges[at] : edges[at + 1]] for column in levels]
            for at, entity in enumerate(missing.tolist())
        }
        taken.update(fresh)
        if len(held) + len(fresh) > _KEPT_ENTITIES:
            held.clear()
        held.update(fresh)
    versions = (rows - plan.versions.offsets[row_entities]).tolist()
    found = [np.empty(rows.shape[0], dtype=object) for _ in specs]
    for at, (entity, version) in enumerate(zip(row_entities.tolist(), versions, strict=True)):
        levels_of = taken[entity]
        for place in range(len(specs)):
            found[place][at] = levels_of[place][version]
    return found


def _move(
    key: kernel2.Key,
    specs: Sequence[TrackedSpec],
    plan: HistoryPlan,
    ents: "np.ndarray",
    current: List["np.ndarray"],
    moving: "np.ndarray",
    changes: "np.ndarray",
    version: int,
) -> None:
    """Move, in *current*, the column each of the *moving* entities' change at *version* is the reason of."""
    reasons = (
        np.zeros(moving.shape[0], dtype=np.int64)
        if plan.reasons is None
        else plan.reasons.columns_of(changes)
    )
    for place, spec in enumerate(specs):
        chosen = moving[reasons == place]
        if chosen.shape[0] == 0:
            continue
        draws = _uniforms_at(key, ents[chosen], version, _STATE_DRAW + place)
        before = current[place][chosen]
        after = np.empty(chosen.shape[0], dtype=object)
        for level in sorted(set(before.tolist())):
            held = before == level
            after[held] = spec.table_of(level).picks(draws[held])
        current[place][chosen] = after
