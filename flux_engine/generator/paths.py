# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The generator's seed-tree address, and the stream table that serves it.

SEAM-FIDELITY-SPEC §1 fixes one path for every draw the contract-schema
generator makes::

    ("sim", sim_id, "gen", exposeId, "row", rowIndex, column)

Every draw is addressed by *what it is for*, never by the order the
generator happened to reach it, so the value in row 900 column ``amount``
is the same value whether the generator produced rows in order, in
reverse, or one column at a time. That is KERNEL-SPEC §3's rule ("stream
derivation is by **path**, never by spawn order") applied to the one
consumer §1 adds.

**This shape is not yet in the kernel's canonical path table.**
KERNEL-SPEC §3 publishes a closed table of eleven path shapes and says
adding a row is a ``kernelVersion`` bump; §1's generator path is a twelfth
that the table does not carry, so
:meth:`flux_engine.core.seedtree.SeedTree.stream` refuses it (``FLX-2201``).
Until that row lands, :class:`GeneratorStreams` derives these streams
directly from the run's root using the kernel's own primitives --
:func:`~flux_engine.core.seedtree.path_label`,
:func:`~flux_engine.core.seedtree.stream_entropy` and
:func:`~flux_engine.core.seedtree.seed_tree_digest` -- so the entropy of a
generator stream is bit-identical to what the canonical table would
produce once the row is added.

**Two consequences of that detour, stated rather than glossed.** The
generator's paths do **not** fold into the run's ``seedTreeDigest``:
:class:`GeneratorStreams` keeps a private ledger with its own
:meth:`~GeneratorStreams.digest`, and nothing merges it into the run's
:class:`~flux_engine.core.seedtree.SeedPathLedger`. So the generator's
draws are absent from ``seed-paths.ndjson`` (``grep '"gen"'`` on a real
run returns nothing) and outside KERNEL-SPEC §3's plan-binding guard:
``FLX-2201`` cannot detect a generator that drew from a different address
than the plan recorded. Closing both is the same one-line change -- add
§1's shape as the twelfth row of ``CANONICAL_PATH_SHAPES``, which is a
``kernelVersion`` bump plus a ``GOLDEN-CHANGELOG.md`` row, and pass the
run's real :class:`~flux_engine.core.seedtree.SeedTree` in as the
:class:`StreamSource` (:func:`resolve_stream_source` already accepts one).
The engine report carries the exact diff for that row.

:class:`GeneratorStreams` also accepts any object with a ``stream(path)``
method (:class:`StreamSource`), so a caller that has a real
:class:`~flux_engine.core.seedtree.SeedTree` -- once it knows this shape
-- passes it in and the generator uses it unchanged.

A generation pass over a run root does not keep a table at all: it takes
each cell's stream from :class:`CellStreams`, which hands out the stream
:class:`GeneratorStreams` would for the same path and keeps none of them,
since a pass asks for each cell's address once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol, Sequence, Tuple

from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.errors import FluxError
from flux_engine.core.kernel2 import CounterStream, Key
from flux_engine.core.rng import DrawStream
from flux_engine.core.seedtree import (
    FLX_2201,
    FLX_2202,
    PathKind,
    PathShape,
    SeedPath,
    SeedPathPart,
    SeedTreeRoot,
    _require_name,
    _require_ordinal,
    counter_stream,
    key_label,
    path_key,
    path_label,
    seed_tree_digest,
    stream_entropy,
    stream_of_label,
)

_WORD_MASK = (1 << 64) - 1

__all__ = [
    "ALLOCATE_PATH_SHAPE",
    "CHANGE_PATH_SHAPE",
    "COUNT_PATH_SHAPE",
    "DEPENDENCY_PATH_SHAPE",
    "DEPENDENCY_ROW_PATH_SHAPE",
    "ENDED_PATH_SHAPE",
    "ENTITIES_PATH_SHAPE",
    "GENERATOR_PATH_SHAPE",
    "GENERATOR_RULE_PATH_SHAPE",
    "HISTORY_PATH_SHAPE",
    "KIND_PATH_SHAPE",
    "LAG_PATH_SHAPE",
    "REASON_PATH_SHAPE",
    "VERSIONS_PATH_SHAPE",
    "CellStreams",
    "GeneratorStreams",
    "StreamSource",
    "allocate_path",
    "change_path",
    "count_path",
    "dependency_path",
    "dependency_row_path",
    "dependency_row_stream",
    "dependency_stream",
    "dates_rule_text",
    "ended_path",
    "entities_path",
    "generator_column_path",
    "generator_rule_path",
    "history_path",
    "kind_path",
    "lag_path",
    "matches_generator_shape",
    "reason_path",
    "versions_path",
]

#: SEAM-FIDELITY-SPEC §1's path, written in the same form as KERNEL-SPEC
#: §3's canonical table so the two can be compared row for row -- and so
#: the diff that adds it to that table is a one-line insertion.
GENERATOR_PATH_SHAPE = PathShape(
    name="sim.generator",
    consumer="contract-schema generator draw for one (row, column)",
    pattern=(
        "sim",
        PathKind.NAME,
        "gen",
        PathKind.NAME,
        "row",
        PathKind.ORDINAL,
        PathKind.NAME,
    ),
)


#: EXPR-SPEC-2 §8's path of one contract dependency's draw for one key and one column. It has
#: no ordinal part, so under kernel ``0.2.0`` its key is the whole path (KERNEL-SPEC-2 §2).
#: Like the generator's cell path it is not in KERNEL-SPEC §3's table and stays out of the
#: seed-path ledger.
DEPENDENCY_PATH_SHAPE = PathShape(
    name="sim.dependency",
    consumer="contract dependency draw for one (key, column)",
    pattern=(
        "sim",
        PathKind.NAME,
        "dependency",
        PathKind.NAME,
        "key",
        PathKind.NAME,
        PathKind.NAME,
    ),
)


def dependency_path(sim_id: str, reference: str, key: str, column: str) -> SeedPath:
    """EXPR-SPEC-2 §8's path: ``("sim", sim_id, "dependency", R, "key", K, column)``."""
    return (
        "sim",
        _require_name(sim_id, "sim_id"),
        "dependency",
        _require_name(reference, "rule reference"),
        "key",
        _require_name(key, "key"),
        _require_name(column, "column"),
    )


def dependency_stream(
    root: SeedTreeRoot, sim_id: str, reference: str, key: str, column: str
) -> DrawStream:
    """A new stream for one dependency draw, at its first word or first draw index.

    Derived through the root's kernel, as the generator derives its own: kernel ``0.1.0``
    from the path's label, kernel ``0.2.0`` from the path's key with zero ordinals.
    """
    path = dependency_path(sim_id, reference, key, column)
    if root.counter_based:
        return counter_stream(root, path, DEPENDENCY_PATH_SHAPE)
    return stream_of_label(root, path_label(path))


#: EXPR-SPEC-2 §8's path of one row's NULL draws for a dependency that draws them per row. Its
#: ordinal is the row index, which kernel ``0.2.0`` keeps in the counter, as the cell path's.
DEPENDENCY_ROW_PATH_SHAPE = PathShape(
    name="sim.dependency.row",
    consumer="contract dependency NULL draws for one row",
    pattern=("sim", PathKind.NAME, "dependency", PathKind.NAME, "row", PathKind.ORDINAL),
)


def dependency_row_path(sim_id: str, reference: str, row_index: int) -> SeedPath:
    """EXPR-SPEC-2 §8's row path: ``("sim", sim_id, "dependency", R, "row", rowIndex)``."""
    return (
        "sim",
        _require_name(sim_id, "sim_id"),
        "dependency",
        _require_name(reference, "rule reference"),
        "row",
        _require_ordinal(row_index, "rowIndex"),
    )


def dependency_row_stream(
    root: SeedTreeRoot, sim_id: str, reference: str, row_index: int
) -> DrawStream:
    """A new stream for one row's NULL draws, derived through the root's kernel."""
    path = dependency_row_path(sim_id, reference, row_index)
    if root.counter_based:
        return counter_stream(root, path, DEPENDENCY_ROW_PATH_SHAPE)
    return stream_of_label(root, path_label(path))


#: KERNEL-SPEC-2 section 8.1's path of one relationship's counted layout. It has no ordinal
#: part, so its key is the whole path and its counter's ordinals are zero. Like the cell path
#: it is not in KERNEL-SPEC section 3's table and stays out of the seed-path ledger.
COUNT_PATH_SHAPE = PathShape(
    name="sim.count",
    consumer="counted layout of the children of one relationship",
    pattern=("sim", PathKind.NAME, "gen", PathKind.NAME, "count", PathKind.NAME),
)


def count_path(sim_id: str, expose_id: str, relationship: str) -> SeedPath:
    """Section 8.1's path: ``("sim", sim_id, "gen", exposeId, "count", relationship)``."""
    return (
        "sim",
        _require_name(sim_id, "sim_id"),
        "gen",
        _require_name(expose_id, "exposeId"),
        "count",
        _require_name(relationship, "relationship"),
    )


#: The path of one column's exact allocation of its classes. Same standing as
#: :data:`COUNT_PATH_SHAPE`: no ordinal part, outside the ledger.
ALLOCATE_PATH_SHAPE = PathShape(
    name="sim.allocate",
    consumer="exact allocation of the classes of one column",
    pattern=("sim", PathKind.NAME, "gen", PathKind.NAME, "allocate", PathKind.NAME),
)


def allocate_path(sim_id: str, expose_id: str, column: str) -> SeedPath:
    """The allocation path: ``("sim", sim_id, "gen", exposeId, "allocate", column)``."""
    return (
        "sim",
        _require_name(sim_id, "sim_id"),
        "gen",
        _require_name(expose_id, "exposeId"),
        "allocate",
        _require_name(column, "column"),
    )


#: KERNEL-SPEC-2 section 8.4's paths of a history's emit. Each has, after ``exposeId``, a literal
#: no other shape has there, so every address is new and a bundle without ``history`` reads none
#: of them. ``entities`` and ``versions`` are layout keys and ``ended`` and ``reason`` are
#: permutation keys: no ordinal part, so each key is the whole path and its counter's ordinals
#: are zero, and none is in the seed-path ledger. ``history`` has one ordinal, the entity index,
#: and ``change`` two, the entity index and the version index (section 2's ordinal parts).
ENTITIES_PATH_SHAPE = PathShape(
    name="sim.history.entities",
    consumer="counted layout of the entities of one relationship's parent rows",
    pattern=("sim", PathKind.NAME, "gen", PathKind.NAME, "entities", PathKind.NAME),
)
VERSIONS_PATH_SHAPE = PathShape(
    name="sim.history.versions",
    consumer="counted layout of the versions of each entity",
    pattern=("sim", PathKind.NAME, "gen", PathKind.NAME, "versions"),
)
HISTORY_PATH_SHAPE = PathShape(
    name="sim.history.first",
    consumer="the first start of one entity",
    pattern=("sim", PathKind.NAME, "gen", PathKind.NAME, "history", PathKind.ORDINAL),
)
CHANGE_PATH_SHAPE = PathShape(
    name="sim.history.change",
    consumer="the length and the tracked values of one version",
    pattern=(
        "sim",
        PathKind.NAME,
        "gen",
        PathKind.NAME,
        "change",
        PathKind.ORDINAL,
        PathKind.ORDINAL,
    ),
)
ENDED_PATH_SHAPE = PathShape(
    name="sim.history.ended",
    consumer="which entities end",
    pattern=("sim", PathKind.NAME, "gen", PathKind.NAME, "ended"),
)
#: ADR 0027 section 6's path of one event's lag: two ordinals, the entity index and the version
#: index, like ``change``. ``lag`` is a literal no other shape has after ``exposeId``, so its
#: addresses are new and a bundle without ``events`` reads none of them.
LAG_PATH_SHAPE = PathShape(
    name="sim.events.lag",
    consumer="the lag of one change event",
    pattern=(
        "sim",
        PathKind.NAME,
        "gen",
        PathKind.NAME,
        "lag",
        PathKind.ORDINAL,
        PathKind.ORDINAL,
    ),
)
#: ADR 0027 section 6's path of one kind's allotment in one period: a permutation key, names
#: only, the kind's and the period's positions written in decimal. Like ``lag``, ``kind`` is a
#: literal no other shape has after ``exposeId``, so its addresses are new.
KIND_PATH_SHAPE = PathShape(
    name="sim.periods.kind",
    consumer="one kind's rows per pair in one period",
    pattern=("sim", PathKind.NAME, "gen", PathKind.NAME, "kind", PathKind.NAME, PathKind.NAME),
)
REASON_PATH_SHAPE = PathShape(
    name="sim.history.reason",
    consumer="which change moves which tracked column",
    pattern=("sim", PathKind.NAME, "gen", PathKind.NAME, "reason"),
)


def _history_head(sim_id: str, expose_id: str) -> Tuple[str, str, str, str]:
    return (
        "sim",
        _require_name(sim_id, "sim_id"),
        "gen",
        _require_name(expose_id, "exposeId"),
    )


def entities_path(sim_id: str, expose_id: str, relationship: str) -> SeedPath:
    """Section 8.4's path: ``("sim", sim_id, "gen", exposeId, "entities", relationship)``."""
    return (
        *_history_head(sim_id, expose_id),
        "entities",
        _require_name(relationship, "relationship"),
    )


def versions_path(sim_id: str, expose_id: str) -> SeedPath:
    """Section 8.4's path: ``("sim", sim_id, "gen", exposeId, "versions")``."""
    return (*_history_head(sim_id, expose_id), "versions")


def history_path(sim_id: str, expose_id: str, entity: int) -> SeedPath:
    """Section 8.4's path: ``("sim", sim_id, "gen", exposeId, "history", entity)``."""
    return (*_history_head(sim_id, expose_id), "history", _require_ordinal(entity, "entity"))


def change_path(sim_id: str, expose_id: str, entity: int, version: int) -> SeedPath:
    """Section 8.4's path: ``("sim", sim_id, "gen", exposeId, "change", entity, version)``."""
    return (
        *_history_head(sim_id, expose_id),
        "change",
        _require_ordinal(entity, "entity"),
        _require_ordinal(version, "version"),
    )


def lag_path(sim_id: str, expose_id: str, entity: int, version: int) -> SeedPath:
    """Section 6's path: ``("sim", sim_id, "gen", exposeId, "lag", entity, version)``."""
    return (
        *_history_head(sim_id, expose_id),
        "lag",
        _require_ordinal(entity, "entity"),
        _require_ordinal(version, "version"),
    )


def kind_path(sim_id: str, expose_id: str, kind: int, period: int) -> SeedPath:
    """Section 6's path: ``("sim", sim_id, "gen", exposeId, "kind", k, p)``, ``k`` and ``p`` in decimal."""
    return (
        *_history_head(sim_id, expose_id),
        "kind",
        _require_name(str(_require_ordinal(kind, "kind")), "kind"),
        _require_name(str(_require_ordinal(period, "period")), "period"),
    )


def ended_path(sim_id: str, expose_id: str) -> SeedPath:
    """Section 8.4's path: ``("sim", sim_id, "gen", exposeId, "ended")``."""
    return (*_history_head(sim_id, expose_id), "ended")


def reason_path(sim_id: str, expose_id: str) -> SeedPath:
    """Section 8.4's path: ``("sim", sim_id, "gen", exposeId, "reason")``."""
    return (*_history_head(sim_id, expose_id), "reason")


def generator_column_path(sim_id: str, expose_id: str, row_index: int, column: str) -> SeedPath:
    """Build §1's path for one (row, column) draw."""
    return (
        "sim",
        _require_name(sim_id, "sim_id"),
        "gen",
        _require_name(expose_id, "exposeId"),
        "row",
        _require_ordinal(row_index, "rowIndex"),
        _require_name(column, "column"),
    )


#: EXPR-SPEC-2 §7's rule stream of one generator row: ``<row path> + ("rule", R)``. Its ordinal is
#: the row index, which kernel ``0.2.0`` keeps in the counter, as the cell path's. Not in KERNEL-SPEC
#: §3's table, like the cell path.
GENERATOR_RULE_PATH_SHAPE = PathShape(
    name="sim.generator.rule",
    consumer="rule stream of one generator row",
    pattern=(
        "sim",
        PathKind.NAME,
        "gen",
        PathKind.NAME,
        "row",
        PathKind.ORDINAL,
        "rule",
        PathKind.NAME,
    ),
)


def dates_rule_text(column: str) -> str:
    """``R`` of a date column that a lifetime's duration or a ``within`` entry sets (ADR 0024 section 6).

    The canonical JSON (KERNEL-SPEC §2) of ``["dates", <column>]``. Its first element is no
    element a contract or a bundle rule's ``R`` holds.
    """
    return canonical_bytes(["dates", _require_name(column, "column")]).decode("utf-8")


def generator_rule_path(sim_id: str, expose_id: str, row_index: int, rule_text: str) -> SeedPath:
    """The rule stream's path: ``("sim", sim_id, "gen", exposeId, "row", rowIndex, "rule", R)``."""
    return (
        "sim",
        _require_name(sim_id, "sim_id"),
        "gen",
        _require_name(expose_id, "exposeId"),
        "row",
        _require_ordinal(row_index, "rowIndex"),
        "rule",
        _require_name(rule_text, "rule text"),
    )


def matches_generator_shape(path: Sequence[SeedPathPart]) -> bool:
    """Whether *path* is a generator path, checked against the shape above."""
    if len(path) != len(GENERATOR_PATH_SHAPE.pattern):
        return False
    for expected, part in zip(GENERATOR_PATH_SHAPE.pattern, path, strict=True):
        if isinstance(expected, str):
            if not isinstance(part, str) or part != expected:
                return False
        elif expected is PathKind.ORDINAL:
            if isinstance(part, bool) or not isinstance(part, int) or part < 0:
                return False
        elif not isinstance(part, str) or not part:
            return False
    return True


class StreamSource(Protocol):
    """Anything that can hand out the stream addressed by a seed-tree path."""

    def stream(self, path: Sequence[SeedPathPart]) -> DrawStream:
        """Return the stream at *path*, deriving it on first request."""


@dataclass
class GeneratorStreams:
    """The stream table for one expose's generation pass.

    Memoised by path label, so asking twice for one address continues one
    stream rather than restarting it -- the same contract
    :class:`~flux_engine.core.seedtree.SeedTree` holds. Every path is
    checked against :data:`GENERATOR_PATH_SHAPE` first, so a typo in a
    column name is ``FLX-2201`` rather than a stream nobody planned.

    :meth:`digest` is :func:`~flux_engine.core.seedtree.seed_tree_digest`
    over the paths this pass materialised, which is the contribution the
    generator makes to ``plan.json``'s ``seedTreeDigest``.
    """

    root: SeedTreeRoot
    _streams: Dict[bytes, DrawStream] = field(default_factory=dict, repr=False)
    _materialised: Dict[bytes, SeedPath] = field(default_factory=dict, repr=False)

    def stream(self, path: Sequence[SeedPathPart]) -> DrawStream:
        """Return the stream at *path*, deriving it on first request."""
        normalised: SeedPath = tuple(path)
        if not matches_generator_shape(normalised):
            raise FluxError(
                FLX_2201,
                f"path {normalised!r} is not a generator path "
                f"({GENERATOR_PATH_SHAPE.name}, SEAM-FIDELITY-SPEC §1)",
            )
        label = path_label(normalised)
        existing = self._streams.get(label)
        if existing is not None:
            return existing
        created: DrawStream
        if self.root.counter_based:
            created = counter_stream(self.root, normalised, GENERATOR_PATH_SHAPE)
        else:
            created = stream_of_label(self.root, label)
        self._streams[label] = created
        self._materialised[label] = normalised
        return created

    def entropy_of(self, path: Sequence[SeedPathPart]) -> int:
        """The ``SeedSequence`` entropy for *path*, without materialising it.

        The same value :func:`~flux_engine.core.seedtree.stream_entropy`
        gives, so a test can show the local derivation is the kernel's own
        rather than a second implementation of it. A kernel ``0.2.0`` path has a
        key and a counter instead, so under a ``flux-seed/2`` root this is
        ``FLX-2202``.
        """
        normalised: SeedPath = tuple(path)
        if not matches_generator_shape(normalised):
            raise FluxError(
                FLX_2201,
                f"path {normalised!r} is not a generator path "
                f"({GENERATOR_PATH_SHAPE.name}, SEAM-FIDELITY-SPEC §1)",
            )
        return stream_entropy(self.root, normalised)

    @property
    def materialised_paths(self) -> Tuple[SeedPath, ...]:
        """Every path this pass has derived a stream for, sorted by label."""
        return tuple(self._materialised[label] for label in sorted(self._materialised))

    def digest(self) -> str:
        """``seedTreeDigest`` over the paths this pass materialised."""
        return seed_tree_digest(self.materialised_paths)


#: The byte between two parts of a seed-tree label (KERNEL-SPEC §3).
_PART_SEPARATOR = b"\x1f"


class CellStreams:
    """The generator's own streams for one pass over a run root: a new stream per cell.

    :class:`GeneratorStreams` keeps every stream it hands out, so that asking twice
    for one address continues one stream, and records every path for its
    :meth:`~GeneratorStreams.digest`. A generation pass over a run root needs
    neither. It asks for each cell's address exactly once, since a compiled
    expose's column names are unique (``FLX-3004``), and nothing reads the pass's
    table afterwards. Keeping one numpy generator per cell alive until the pass
    ended was what made a run's memory grow with its row count.

    Each stream here is the one :class:`GeneratorStreams` hands out for the same
    path: the same label, so the same entropy, so the same words. The label is
    put together from parts checked once per pass and once per column, rather than
    rebuilt and re-checked part by part for every cell, which is where much of a
    cell's time went.
    """

    __slots__ = ("root", "_sim_id", "_expose_id", "_prefix", "_columns", "_keys", "_dates_keys")

    def __init__(self, root: SeedTreeRoot, sim_id: str, expose_id: str) -> None:
        self.root = root
        self._sim_id = sim_id
        self._expose_id = expose_id
        self._prefix: Optional[bytes] = None
        self._columns: Dict[str, bytes] = {}
        # Kernel 0.2.0: one Philox key per column, the row in the counter.
        self._keys: Dict[str, Key] = {}
        self._dates_keys: Dict[str, Tuple[bytes, Key]] = {}

    def label(self, row_index: int, column: str) -> bytes:
        """§3's label of ``generator_column_path(sim_id, expose_id, row_index, column)``.

        Byte for byte what :func:`~flux_engine.core.seedtree.path_label` returns for
        that path: a decimal ordinal is its own NFC form and holds no separator, so
        its part is its ASCII digits.
        """
        prefix = self._prefix
        try:
            suffix = self._columns.get(column)
        except TypeError:  # not a name at all: the whole route below refuses it
            suffix = None
        if prefix is None or suffix is None:
            # A column's first cell is labelled the whole way, so every part is
            # checked, and refused, exactly as for any other path. No part holds
            # a separator, so the label splits back into its seven parts.
            whole = path_label(
                generator_column_path(self._sim_id, self._expose_id, row_index, column)
            )
            head, _ordinal, tail = whole.rsplit(_PART_SEPARATOR, 2)
            self._prefix = head + _PART_SEPARATOR
            self._columns[column] = _PART_SEPARATOR + tail
            return whole
        # Every fixed part and this column have passed; only the ordinal is new.
        ordinal = _require_ordinal(row_index, "rowIndex")
        return prefix + str(ordinal).encode("ascii") + suffix

    def key_label(self, column: str) -> bytes:
        """KERNEL-SPEC-2 section 2's key label of *column*'s cells: the label, its row empty.

        :meth:`label` has checked every part of the column's path by the time a caller
        asks, as :meth:`stream_at` and an identifier cell do.
        """
        if self._prefix is None or column not in self._columns:
            self.label(0, column)
        assert self._prefix is not None
        return self._prefix + self._columns[column]

    def column_key(self, column: str) -> Key:
        """Kernel ``0.2.0``'s Philox key of *column*'s cells, derived once per column."""
        key = self._keys.get(column)
        if key is None:
            key = path_key(self.root, self.key_label(column))
            self._keys[column] = key
        return key

    def dates_key(self, column: str) -> Tuple[bytes, Key]:
        """The key label and Philox key of the rule stream of the date column *column* (kernel ``0.2.0``).

        The stream is ``<row path> + ("rule", R)`` with ``R`` the canonical JSON of
        ``["dates", column]`` (ADR 0024 section 6): its key leaves the row out, and its counter
        carries it. Derived once per column.
        """
        found = self._dates_keys.get(column)
        if found is None:
            if not self.root.counter_based:
                raise FluxError(
                    FLX_2202, "a date set by a lifetime or a within entry is kernel 0.2.0's"
                )
            path = generator_rule_path(self._sim_id, self._expose_id, 0, dates_rule_text(column))
            label, _ordinals = key_label(path, GENERATOR_RULE_PATH_SHAPE)
            found = (label, path_key(self.root, label))
            self._dates_keys[column] = found
        return found

    def dates_stream_at(self, row_index: int, column: str, first_draw: int = 0) -> CounterStream:
        """A new stream of *column*'s dates rule for one row, whose next draw is *first_draw*.

        A position of ADR 0024 section 6 is a draw index, so the value of a direct draw is the
        first sampler call of ``dates_stream_at(row, column, 1)``.
        """
        label, key = self.dates_key(column)
        if not 0 <= row_index <= _WORD_MASK:
            raise FluxError(FLX_2202, f"row {row_index} does not fit in one counter word")
        return CounterStream(
            key, (row_index, 0), label=label.decode("utf-8", "replace"), position=first_draw
        )

    def stream_at(self, row_index: int, column: str) -> DrawStream:
        """A new stream for one cell, positioned at its first word.

        Under a ``flux-seed/2`` root it is kernel ``0.2.0``'s: the column's key, and the
        row as the counter's first word.
        """
        label = self.label(row_index, column)
        if not self.root.counter_based:
            return stream_of_label(self.root, label)
        key = self.column_key(column)
        if row_index > _WORD_MASK:
            raise FluxError(FLX_2202, f"row {row_index} does not fit in one counter word")
        return CounterStream(key, (row_index, 0), label=label.decode("utf-8", "replace"))


def resolve_stream_source(
    root: Optional[SeedTreeRoot] = None, source: Optional[StreamSource] = None
) -> StreamSource:
    """Return the stream source to draw from: a caller's, or one over *root*.

    Exactly one of the two is given. A caller that owns a
    :class:`~flux_engine.core.seedtree.SeedTree` passes it as *source* and
    the generator's paths join that tree's ``seedTreeDigest``; a caller
    that has only the run root gets a :class:`GeneratorStreams` over it.
    """
    if (root is None) == (source is None):
        raise FluxError(FLX_2202, "pass exactly one of root= or source=")
    if source is not None:
        return source
    assert root is not None  # guarded above
    return GeneratorStreams(root=root)
