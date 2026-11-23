# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The statistic registry: one definition per statistic.

A *definition* is everything that makes two computations of one statistic
the same computation: the integer quantities it keeps, the pure function
that forms them from values already read onto the grid, the canonical
spelling of those quantities, and the comparison of two of them under a
compiled margin. It is keyed by a stable identifier, ``<name>/<major>``. A
change to any of the four is a new major under a new identifier, never an
edit under the old one, as an FLX code is never given a second meaning.

The pattern is Deequ's (awslabs/deequ 2.1.0, Apache-2.0). One ``Analyzer``
forms a state from the data (``computeStateFrom``) and a metric from the
state (``computeMetricFrom``), and Deequ's ``ColumnProfiler`` and its
``VerificationSuite`` run the same analyzers through one ``AnalysisRunner``,
so a profile and a check of one dataset agree by construction. Great
Expectations 1.25.0 (Apache-2.0) keys its metric providers by a stable name
(``column.mean``) that every expectation resolves in the same way. Here the
states are integer count tables and sums on the fixed-point grid
(SEAM-FIDELITY-SPEC §4), and the comparisons are §5's integer
cross-multiplied inequalities from :mod:`flux_stats.fixedpoint`.

Every side that forms or reads a statistic shares this registry: the
fidelity check's boundary and verifier, and the profiler that comes next.
So it stays small. It imports the standard library and this package's
:mod:`flux_stats.errors`, :mod:`flux_stats.fixedpoint` and
:mod:`flux_stats.spelling`, and nothing else, and
``packages/flux-stats/tests/test_registry.py`` pins that import closure in a
fresh interpreter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, TypeVar

from flux_stats.fixedpoint import SecondMoments
from flux_stats.spelling import (
    normal_text,
    read_count,
    read_counts,
    read_texts,
    read_units,
    refuse,
)

__all__ = [
    "Check",
    "GroupStatistic",
    "MetricDefinition",
    "Parameters",
    "StatisticDefinition",
    "definition",
    "definition_for_kind",
    "definitions",
    "metric",
    "metrics",
    "read_group",
    "register",
    "register_metric",
]

#: ``<name>/<major>``: a lower-camel-case name, then a major version that
#: starts at 1. The major moves whenever the quantities, the measure, the
#: spelling or the comparison change, so an identifier names one
#: computation for as long as it exists.
_IDENTIFIER = re.compile(r"^[a-z][A-Za-z0-9]*/[1-9][0-9]*$")


@dataclass(frozen=True)
class GroupStatistic:
    """The aggregate quantities for one statistic inside one group.

    ``k`` is the number of records that contributed after the null policy
    and the grouping; every other field is a sum, a count, a sorted member
    list or one order statistic over exactly those ``k`` records. Which
    fields a statistic fills is its definition's business: a mean keeps
    ``sum_x``, a histogram its bin counts, and so on.
    """

    group: str
    k: int
    sum_x: int = 0
    sum_y: int = 0
    sum_xx: int = 0
    sum_yy: int = 0
    sum_xy: int = 0
    bin_counts: Tuple[int, ...] = ()
    #: Records per declared level of a level histogram's column, in
    #: :func:`flux_stats.definitions.level_order` order.
    level_counts: Tuple[int, ...] = ()
    support: Tuple[str, ...] = ()
    #: Records carried by each member of :attr:`support`, in the same order.
    #: Held in process only -- §4 publishes "the sorted committed support"
    #: and nothing per-value about the real data, so :meth:`to_document`
    #: (the ``C2`` commitment) does not carry it. It exists so the SUPPORT
    #: comparison can bound how *much* of the synthetic dataset is
    #: off-support, not merely how many different strange values it holds.
    support_counts: Tuple[int, ...] = ()
    quantile_witness: Optional[int] = None
    quantile_below: int = 0
    quantile_at_or_below: int = 0
    #: ``quantile/2`` only (ADR 0012): the index of the declared bin that
    #: holds the order statistic, and the records below that bin's lower
    #: edge and below its upper edge (all ``k`` for the last bin). The
    #: witness stays in the group on the synthetic side, where the engine
    #: forms it from its own data, and :meth:`to_document` never spells it
    #: for a binned quantile.
    quantile_bin: Optional[int] = None
    bin_below: int = 0
    bin_through: int = 0

    def cell_counts(self, kind: str) -> Tuple[int, ...]:
        """The counts a total-variation comparison reads for a *kind* statistic.

        A histogram's bin counts, or a level histogram's level counts: one
        count per declared cell, in the cell order both sides share.
        """
        return self.level_counts if kind == _LEVEL_HISTOGRAM else self.bin_counts

    @property
    def moments(self) -> SecondMoments:
        """The second-moment view a correlation comparison consumes."""
        return SecondMoments(
            count=self.k,
            sum_x=self.sum_x,
            sum_y=self.sum_y,
            sum_xx=self.sum_xx,
            sum_yy=self.sum_yy,
            sum_xy=self.sum_xy,
        )

    def to_document(self, statistic: Any) -> Dict[str, Any]:
        """The canonical form of this group, for the ``C2`` commitment.

        ``group`` and ``k``, then the quantities *statistic* spells. It is
        the :class:`StatisticDefinition` that formed the group, or a
        property kind, which names the one definition that serves it
        unbinned (a quantile with bins is ``quantile/2`` and is named by its
        definition, :func:`flux_stats.fidelity.definition_for_property`).
        Counts are JSON integers because ``records.max`` keeps them
        inside the safe-integer domain RFC 8785 serialises through. Encoded
        sums and the quantile witness are decimal *strings*: a sum of a
        hundred thousand encoded values has no safe-integer spelling, and
        KERNEL-SPEC §2.1 refuses an integer above 2^53-1 rather than
        letting it round.
        """
        if isinstance(statistic, StatisticDefinition):
            entry = statistic
        elif self.quantile_bin is not None:
            # A kind names its unbinned definition, which would spell this
            # group's witness: a binned group is spelled by quantile/2 only.
            raise ValueError(
                "a group formed by quantile/2 is spelled by its definition, not by "
                "its kind (flux_stats.fidelity.definition_for_property)"
            )
        else:
            entry = definition_for_kind(statistic)
        document: Dict[str, Any] = {"group": self.group, "k": self.k}
        document.update(entry.spell(self))
        return document


#: The one kind whose cells are levels rather than bins. Spelled here, not
#: imported from :class:`flux_stats.fidelity.PropertyKind`, because that module
#: imports this one (it measures a group with the registered definitions), so
#: importing it back would make a cycle. The engine's
#: ``tests/stats/test_engine_agreement.py`` ties every kind this package
#: names to that grammar.
_LEVEL_HISTOGRAM = "levelHistogram"


@dataclass(frozen=True)
class Parameters:
    """The declared shape one statistic is measured on.

    Read from the declaration by whoever measures, never from the data, so
    two sides measuring under one declaration measure on one shape.

    ``edges``
        A histogram's bin edges, or the bin edges of a quantile that
        declares them, encoded onto the grid.
    ``level_count``
        How many declared levels a level histogram counts.
    ``level``
        A quantile's level, in ``[0, 1]``.
    """

    edges: Tuple[int, ...] = ()
    level_count: int = 0
    level: Optional[Fraction] = None


@dataclass(frozen=True)
class Check:
    """What one comparison needs from a compiled property, and nothing more.

    The verifier builds it from a compiled property
    (:class:`flux_engine.concordance.compiler.CompiledProperty`), a module
    this one may not import. It carries the compiled margins and the names
    a refusal quotes, never a declaration.

    ``subject``
        The property id a refusal names.
    ``tolerance``
        The tolerance ``type`` the property declares (§2): ``absolute``,
        ``relative``, ``distance`` or ``count``.
    ``margin``, ``margin_units``, ``max_non_members``
        The compiled margins (``τ′``) of :class:`CompiledProperty`.
    ``min_variance``, ``level``
        A correlation's variance floor and a quantile's level.
    ``edges``
        The encoded bin edges of a quantile that declares bins
        (``quantile/2``); empty for every other comparison.
    ``reference_side``
        What a refusal calls the side the synthetic data is compared with.
    """

    subject: str
    tolerance: str
    margin: Optional[Fraction] = None
    margin_units: Optional[int] = None
    max_non_members: Optional[int] = None
    min_variance: Optional[Fraction] = None
    level: Optional[Fraction] = None
    edges: Tuple[int, ...] = ()
    reference_side: str = "committed"


#: Forms one group's quantities from the values a reader produced, in row
#: order: encoded integers, level slots, NFC category strings or encoded
#: pairs, as the definition says.
Measure = Callable[[str, Sequence[Any], Parameters], GroupStatistic]

#: The quantities' canonical spelling, without ``group`` and ``k``.
Spell = Callable[[GroupStatistic], Dict[str, Any]]

#: ``True`` when the synthetic group (first) is within the compiled margin
#: of the reference group (second). A fail-closed condition raises.
Compare = Callable[[GroupStatistic, GroupStatistic, Check], bool]


@dataclass(frozen=True)
class StatisticDefinition:
    """One statistic: its identifier, the kind it serves, and its functions.

    ``kind`` is the fidelity property kind whose comparison this statistic
    feeds (``mean``, ``marginalHistogram``, ...). A kind has one statistic
    for each value of ``binned``: ``binned`` is ``True`` for the statistic of
    a property of that kind that declares ``bins`` where the kind also has
    one without them, which today is ``quantile/2`` beside ``quantile/1``
    (ADR 0012). ``summary`` is one line
    saying what the quantities are. :attr:`measure`, :attr:`spell` and
    :attr:`compare` are pure: integers and strings in, integers, strings
    and booleans out, no state and no I/O. :func:`read_group` reads the
    spelling back.

    ``on_grid`` says whether the quantities mean something only under the
    grid they were formed on: encoded values (a sum, a moment, an order
    statistic), and counts per bin, since a value is floored onto the grid
    before it is binned, so on a grid that misses an edge a value within one
    grid step below that edge falls in the bin above it. Counts per declared
    level and member lists do not depend on the grid. ``cell_table`` says whether the quantities are one
    count per declared cell, the shape whose cells a disclosure rule can
    suppress one at a time.
    """

    identifier: str
    kind: str
    summary: str
    measure: Measure
    spell: Spell
    compare: Compare
    on_grid: bool
    cell_table: bool
    binned: bool = False


@dataclass(frozen=True)
class MetricDefinition:
    """One column metric, named as OpenLineage's ``columnMetrics`` names it.

    A metric describes one column's values in their own domain, not on the
    grid: a count, or an exact decimal. It feeds no fidelity property yet.
    ``name`` is its OpenLineage name and the identifier's name part;
    ``domains`` are the column domains it applies to. :attr:`measure` takes
    the column's values as read (``None`` for a null) and the declared
    quantile levels; :attr:`spell` and :attr:`read` write and read its value.
    """

    identifier: str
    name: str
    summary: str
    domains: FrozenSet[str]
    measure: Callable[[Sequence[Any], Tuple[Fraction, ...]], Any]
    spell: Callable[[Any], Dict[str, Any]]
    read: Callable[[Mapping[str, Any], str], Any]


_DEFINITIONS: Dict[str, StatisticDefinition] = {}
#: Keyed by the kind and by whether the property declares bins. A kind has
#: at most one definition of each, so a property's declaration picks its
#: definition (:func:`definition_for_kind`).
_BY_KIND: Dict[Tuple[str, bool], StatisticDefinition] = {}
_METRICS: Dict[str, MetricDefinition] = {}


_Entry = TypeVar("_Entry", StatisticDefinition, MetricDefinition)


def _admit(entry: Any, table: Mapping[str, Any]) -> bool:
    """Whether *entry* is new to *table*, or refuse it.

    ``False`` when the same object is already registered there, so a module
    imported twice registers nothing twice. Refuses a malformed identifier,
    an empty summary, and an identifier either table already holds:
    statistics and metrics share one identifier space.
    """
    if table.get(entry.identifier) is entry:
        return False
    if not _IDENTIFIER.match(entry.identifier):
        raise ValueError(
            f"statistic identifier {entry.identifier!r} is not '<name>/<major>' "
            "(a lower-camel-case name and a major version from 1)"
        )
    if not entry.summary.strip():
        raise ValueError(f"{entry.identifier}: summary must not be empty")
    if entry.identifier in _DEFINITIONS or entry.identifier in _METRICS:
        raise ValueError(
            f"{entry.identifier} is already registered; a changed statistic takes a new major"
        )
    return True


def _kind_key(kind: Any) -> str:
    """A kind as its declared spelling: a ``PropertyKind`` or the string itself."""
    return str(getattr(kind, "value", kind))


def register(entry: StatisticDefinition) -> StatisticDefinition:
    """Add *entry* to the registry and return it.

    Called at import time by the module that defines the statistic, as
    :func:`flux_stats.errors.register` is for an FLX code. Refuses a
    malformed identifier, an empty summary, a second definition under an
    identifier already registered and a second definition for a kind and a
    ``binned`` that already have one: two definitions of one statistic are
    two computations wearing one name. Registering the same object again returns it, so
    importing a module twice is harmless.
    """
    if not _admit(entry, _DEFINITIONS):
        return entry
    kind = _kind_key(entry.kind)
    claimed = _BY_KIND.get((kind, entry.binned))
    if claimed is not None:
        raise ValueError(
            f"kind {kind!r}{' with bins' if entry.binned else ''} is already measured by "
            f"{claimed.identifier}; one kind has one statistic for each of its two forms"
        )
    _DEFINITIONS[entry.identifier] = entry
    _BY_KIND[(kind, entry.binned)] = entry
    return entry


def register_metric(entry: MetricDefinition) -> MetricDefinition:
    """Add a column metric to the registry and return it.

    Metrics and statistics share one identifier space, so an identifier
    names one computation whichever kind it is. The identifier's name part
    is the metric's name: ``count/1`` is the metric called ``count``.
    """
    if not _admit(entry, _METRICS):
        return entry
    if entry.identifier.split("/")[0] != entry.name:
        raise ValueError(f"{entry.identifier}: the identifier's name part is {entry.name!r}")
    _METRICS[entry.identifier] = entry
    return entry


def _one(table: Mapping[str, _Entry], key: str, missing: str) -> _Entry:
    found = table.get(key)
    if found is None:
        raise KeyError(f"no {missing} {key!r}; registered: {sorted(table)}")
    return found


def _every(table: Mapping[str, _Entry]) -> Tuple[_Entry, ...]:
    return tuple(table[key] for key in sorted(table))


def definition(identifier: str) -> StatisticDefinition:
    """Return the definition registered under *identifier*.

    Raises ``KeyError`` naming the registered identifiers when there is
    none; a reader of a document that names a statistic turns that into
    its own refusal.
    """
    return _one(_DEFINITIONS, identifier, "statistic is registered as")


def definition_for_kind(kind: Any, *, binned: bool = False) -> StatisticDefinition:
    """Return the one definition that serves the property *kind*.

    *kind* is a ``PropertyKind`` or its declared spelling (``"mean"``).
    *binned* picks the definition of a property that declares bins, for the
    one kind that has two (``quantile``); a property's own declaration says
    which it is (:func:`flux_stats.fidelity.definition_for_property`).
    """
    key = (_kind_key(kind), binned)
    found = _BY_KIND.get(key)
    if found is None:
        served = sorted(f"{name} (bins)" if flag else name for name, flag in _BY_KIND)
        form = " with bins" if binned else ""
        raise KeyError(f"no statistic serves kind {key[0]!r}{form}; registered: {served}")
    return found


def definitions() -> Tuple[StatisticDefinition, ...]:
    """Every registered statistic definition, in identifier order."""
    return _every(_DEFINITIONS)


def metric(identifier: str) -> MetricDefinition:
    """Return the column metric registered under *identifier*."""
    return _one(_METRICS, identifier, "metric is registered as")


def metrics() -> Tuple[MetricDefinition, ...]:
    """Every registered column metric, in identifier order."""
    return _every(_METRICS)


# ---------------------------------------------------------------------------
# Reading a spelled group back
# ---------------------------------------------------------------------------


def _read_key(value: Any, where: str) -> str:
    """A group key: the empty string for an ungrouped statistic, else a name."""
    if value == "":
        return ""
    return normal_text(value, where)


def _read_cells(value: Any, where: str) -> Tuple[int, ...]:
    counts = read_counts(value, where)
    if any(count is None for count in counts):
        raise refuse(where, "a published cell count is a JSON integer, not null")
    return tuple(count for count in counts if count is not None)


def _read_members(value: Any, where: str) -> Tuple[str, ...]:
    members = tuple(read_texts(value, where))
    if list(members) != sorted(set(members)):
        raise refuse(where, "support members are distinct and sorted")
    return members


def _read_witness(value: Any, where: str) -> Optional[int]:
    return None if value is None else read_units(value, where)


def _read_bin(value: Any, where: str) -> Optional[int]:
    return None if value is None else read_count(value, where)


#: Each quantity a definition spells, the field of :class:`GroupStatistic`
#: it fills and how it is read. Which of these a definition uses is what its
#: own ``spell`` writes, so the spelling stays the one definition of the
#: document form and this table only knows how to read each kind of value.
_QUANTITIES: Mapping[str, Tuple[str, Callable[[Any, str], Any]]] = {
    "Sx": ("sum_x", read_units),
    "Sy": ("sum_y", read_units),
    "Sxx": ("sum_xx", read_units),
    "Sxy": ("sum_xy", read_units),
    "Syy": ("sum_yy", read_units),
    "binCounts": ("bin_counts", _read_cells),
    "levelCounts": ("level_counts", _read_cells),
    "support": ("support", _read_members),
    "quantileWitness": ("quantile_witness", _read_witness),
    "quantileBelow": ("quantile_below", read_count),
    "quantileAtOrBelow": ("quantile_at_or_below", read_count),
    "quantileBin": ("quantile_bin", _read_bin),
    "binBelow": ("bin_below", read_count),
    "binThrough": ("bin_through", read_count),
}


def read_group(entry: StatisticDefinition, document: Any, where: str) -> GroupStatistic:
    """Read one group of *entry* as :meth:`GroupStatistic.to_document` spells it.

    The exact inverse: the keys are exactly ``group``, ``k`` and what the
    definition spells, each value has its one spelling, and the group read
    back re-spells to the same document. Anything else is ``FLX-5036``.
    """
    if not isinstance(document, Mapping):
        raise refuse(where, f"a group is a mapping, got {type(document).__name__}")
    quantities = list(entry.spell(GroupStatistic(group="", k=0)))
    expected = {"group", "k", *quantities}
    found = {str(key) for key in document}
    unknown: List[str] = sorted(found - expected)
    missing: List[str] = sorted(expected - found)
    if unknown or missing:
        raise refuse(where, f"{entry.identifier} groups carry {sorted(expected)}")
    fields: Dict[str, Any] = {
        "group": _read_key(document["group"], f"{where}.group"),
        "k": read_count(document["k"], f"{where}.k"),
    }
    for key in quantities:
        field, reader = _QUANTITIES[key]
        fields[field] = reader(document[key], f"{where}.{key}")
    state = GroupStatistic(**fields)
    if state.to_document(entry) != dict(document):
        raise refuse(where, "a group is written in its one canonical spelling")
    return state
