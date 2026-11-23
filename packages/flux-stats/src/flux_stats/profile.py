# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The profile document: aggregate statistics of the data a bundle imitates.

``docs/spec/PROFILE-SPEC.md`` is the normative description of the format and
``docs/spec/profile-1.schema.json`` its JSON Schema. A profiler computes a
profile where the data lives, and the engine only ever reads aggregates out
of one (ADR 0003, amended 2026-11-16). This module reads a profile whole, or
refuses it with ``FLX-5036`` naming where the problem is.

Every statistic in a profile is an instance of a registered definition
(:mod:`flux_stats.registry`) and carries its identifier, a
sample-size band and the disclosure rule that was applied to it. A group or
a cell a rule suppressed carries its SDMX ``CL_CONF_STATUS`` code and no
value. The statistics the fidelity property kinds compare are spelled
exactly as the derived statistic ``S`` spells them
(SEAM-FIDELITY-SPEC §4), under the profile's own declared grid, so a
profile formed over the same records under the same declarations holds the
same numbers ``S`` would.

Borrowed, as patterns rather than dependencies:

* the OpenLineage facet envelope: ``_producer`` and ``_schemaURL``, with
  the version in the schema's ``$id`` (OpenLineage ``BaseFacet``, spec
  2-0-2), and the names of its ``columnMetrics`` (facet
  ``DataQualityMetricsInputDatasetFacet`` 1-0-0), whose values are JSON
  numbers there and integer counts or decimal strings here;
* mostlyai-qa's statistics layout (1.10.11): bin edges plus integer counts,
  without its per-record PCA arrays (``trn_pca.npy``), which are records
  under another name;
* SDMX's ``CL_CONF_STATUS`` codelist (version 1.4) for the status of a
  suppressed cell;
* ACRO's output-checking parameters (1.1.0, ``acro/default.yaml``) for the
  fields of a disclosure rule.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from fractions import Fraction
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from flux_stats.cliques import (
    CLIQUE_DEFINITION,
    CLIQUE_DEFINITION_2,
    MOST_CLIQUES,
    CliqueEntry,
    CliqueSelection,
    check_consistency,
    check_table_limits,
    edge_text,
    read_clique,
    read_clique_2,
    read_clique_selection,
)
from flux_stats.column_metrics import (
    CATEGORY,
    DOMAINS,
    MAX,
    MIN,
    NUMBER,
    QUANTILES,
    TIMESTAMP,
)
from flux_stats.definitions import QUANTILE_BINNED, level_order
from flux_stats.fixedpoint import MAX_FRACTIONAL_BITS
from flux_stats.noise import (
    Accounting,
    Noise,
    Spent,
    check_budget,
    point_estimate,
    read_accounting,
    read_noise,
)
from flux_stats.rank_correlation import (
    RANK_DEFINITIONS,
    RANK_IDENTIFIERS,
    rank_sensitivity,
    rank_value,
)
from flux_stats.registry import (
    GroupStatistic,
    MetricDefinition,
    StatisticDefinition,
    definition,
    metrics,
    read_group,
)
from flux_stats.spelling import (
    normal_text,
    read_count,
    read_counts,
    read_decimal,
    read_texts,
    read_units,
    refuse,
)

__all__ = [
    "BinGrouping",
    "CLIQUE_DEFINITION",
    "CLIQUE_DEFINITION_2",
    "CONF_STATUS",
    "CliqueEntry",
    "CliqueSelection",
    "CALENDAR_LEVELS",
    "CalendarEntry",
    "ColumnEntry",
    "DATE_ORDER_LEVELS",
    "DatePairEntry",
    "DisclosureRule",
    "ExposureEntry",
    "CHAIN_LEVELS",
    "FREE",
    "HistoryColumn",
    "HistoryEntry",
    "InvariantEntry",
    "LAST_VERSION_LEVELS",
    "LinkGrouping",
    "MAX_CALENDAR_ENTRIES",
    "MAX_DATE_PAIRS",
    "MAX_EXPOSURES",
    "MetricEntry",
    "Accounting",
    "Noise",
    "PairEntry",
    "PeriodEntry",
    "Profile",
    "ProfileGroup",
    "QueryEntry",
    "ReferenceTable",
    "RelationshipDateEntry",
    "RelationshipEntry",
    "SCHEMA_URL",
    "SCHEMA_URL_1_1_0",
    "SCHEMA_URL_1_2_0",
    "SCHEMA_URL_1_3_0",
    "SCHEMA_URL_1_4_0",
    "SEVERAL",
    "STRUCTURAL_CLASSES",
    "SampleSize",
    "StatisticEntry",
    "TableEntry",
    "load_profile_text",
    "parse_profile",
]

#: Format ``1-0-0``, which every profile that holds no ``quantile/2``
#: statistic can still name. A profile names its format in ``_schemaURL`` as
#: an OpenLineage facet names its own, and the version is in the path, so a
#: later format is a new URL rather than a new meaning.
SCHEMA_URL = "https://agenticstiger.github.io/flux/engine/spec/profile/1-0-0/FluxProfile.json"

#: Format ``1-1-0``, the ``$id`` of ``docs/spec/profile-1.schema.json``. It
#: adds the ``quantile/2`` statistic (ADR 0012) and changes nothing format
#: ``1-0-0`` says. This reader reads both URLs and refuses any other.
SCHEMA_URL_1_1_0 = "https://agenticstiger.github.io/flux/engine/spec/profile/1-1-0/FluxProfile.json"

#: Format ``1-2-0``, the ``$id`` of ``docs/spec/profile-1-2.schema.json``. It adds the
#: ``invariants`` section, the share of rows on which each declared fact holds, and changes
#: nothing format ``1-1-0`` says. A document that holds an ``invariants`` key names it.
SCHEMA_URL_1_2_0 = "https://agenticstiger.github.io/flux/engine/spec/profile/1-2-0/FluxProfile.json"

#: Format ``1-3-0``, the ``$id`` of ``docs/spec/profile-1-3.schema.json``. It adds the ``references``
#: section (PROFILE-SPEC.md section 5b), a ``groupBy`` on a number column, ``groupByLink`` and
#: ``per``, and changes nothing format ``1-2-0`` says. A document that holds one of them names it.
SCHEMA_URL_1_3_0 = "https://agenticstiger.github.io/flux/engine/spec/profile/1-3-0/FluxProfile.json"

#: Format ``1-4-0``, the ``$id`` of ``docs/spec/profile-1-4.schema.json``. It adds the rank correlations
#: ``kendallTau/1`` and ``spearmanRho/1`` of a ``columnPairs`` entry and the ``rowFloor`` key of
#: PROFILE-SPEC.md section 5, the clique ``cliqueMarginal/2`` and a table's ``cliqueSelection``, and
#: changes nothing format ``1-3-0`` says. A document that holds one of
#: :data:`_FORMS_OF_1_4_0` names it.
SCHEMA_URL_1_4_0 = "https://agenticstiger.github.io/flux/engine/spec/profile/1-4-0/FluxProfile.json"

#: Every form that only format ``1-4-0`` holds, by the key the reader names it with, and what the
#: refusal calls it. A document that names an earlier format and holds one is refused
#: (:meth:`_Reader.require_1_4_0`). A later form of the format is one more entry here and one call at
#: the place the reader reads it.
_FORMS_OF_1_4_0: Mapping[str, str] = MappingProxyType(
    {
        "rankCorrelation": "a rank correlation (kendallTau/1 or spearmanRho/1)",
        "rowFloor": "a rowFloor",
        "cliqueMarginal/2": "a cliqueMarginal/2",
        "cliqueSelection": "a cliqueSelection",
    }
)

#: The classes a relationship may declare structurally empty (format 1-3-0): parents with no child,
#: and child keys, not NULL, whose parent is missing.
STRUCTURAL_CLASSES: Tuple[str, ...] = ("childless", "orphan")

#: How consecutive versions of an entity meet (format 1-3-0, ``history.chain``): the next version
#: starts at the end, a day after it, after a gap, or before it (an overlap). In the order a level
#: histogram counts them, which is code point order.
CHAIN_LEVELS: Tuple[str, ...] = ("dayBefore", "gap", "nextStart", "overlap")

#: How an entity's last version ends (format 1-3-0, ``history.lastVersion``), in code point order.
LAST_VERSION_LEVELS: Tuple[str, ...] = ("closed", "open")

#: Why a ``noise`` block is refused beside ``per`` and in a history: one changed row can move an
#: entity between two cells, and this reader charges a sensitivity of 1 for a noisy statistic.
_MOVES_AN_ENTITY = (
    "is not read: one changed row can move an entity between two cells, and this reader charges "
    "a sensitivity of 1 for a noisy statistic"
)

#: The level of ``history.reasons`` for a change that moves more than one tracked column.
SEVERAL = "several"

#: Every format this reader reads. A later format is a new URL, so a reader that predates it
#: refuses the document rather than skip what it cannot read.
_READ_FORMATS = (
    SCHEMA_URL,
    SCHEMA_URL_1_1_0,
    SCHEMA_URL_1_2_0,
    SCHEMA_URL_1_3_0,
    SCHEMA_URL_1_4_0,
)

#: The formats that hold the sections of format ``1-2-0`` (``invariants``, ``privacy``).
_FROM_1_2_0 = (SCHEMA_URL_1_2_0, SCHEMA_URL_1_3_0, SCHEMA_URL_1_4_0)

#: The formats that hold the forms of format ``1-3-0``.
_FROM_1_3_0 = (SCHEMA_URL_1_3_0, SCHEMA_URL_1_4_0)

#: The most columns a ``per`` names (PROFILE-SPEC.md section 3).
MAX_PER_COLUMNS = 8

#: A number as canonical decimal text: no exponent, no leading zeros, no trailing zeros after the
#: point (EXPR-SPEC-2 section 8); ``0`` is zero, and ``-0`` is no spelling of it.
_CANONICAL_DECIMAL = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]*[1-9])?")
#: A timestamp as its milliseconds since the epoch, in decimal text.
_CANONICAL_INTEGER = re.compile(r"0|-?[1-9][0-9]*")

#: The levels of a date pair's ``order`` (PROFILE-SPEC.md section 5): the end against the start, in
#: the one order a level histogram counts its levels in (code point order).
DATE_ORDER_LEVELS: Tuple[str, ...] = ("after", "before", "same")

#: The most pairs a table's ``datePairs`` or a relationship's ``dates`` lists (format 1-3-0).
MAX_DATE_PAIRS = 1024

#: The most entries a table's ``calendar`` and a relationship's ``exposure`` list (format 1-3-0).
MAX_CALENDAR_ENTRIES = 1024
MAX_EXPOSURES = 1024

#: The levels of the three histograms of a calendar entry (PROFILE-SPEC.md section 5), written
#: with leading zeros so that their code point order is their number's: the month, the ISO 8601
#: day of the week (Monday is ``1``) and the hour of the week from Monday 00:00 UTC (``000``).
CALENDAR_LEVELS: Mapping[str, Tuple[str, ...]] = MappingProxyType(
    {
        "month": tuple(f"{month:02d}" for month in range(1, 13)),
        "dayOfWeek": tuple(str(day) for day in range(1, 8)),
        "hourOfWeek": tuple(f"{hour:03d}" for hour in range(168)),
    }
)

#: The bounds of a reference table (PROFILE-SPEC.md section 5b).
MAX_REFERENCE_TABLES = 1024
MAX_REFERENCE_ROWS = 100_000
MAX_REFERENCE_COLUMNS = 1_000
MAX_REFERENCE_CELLS = 1_000_000
MAX_REFERENCE_CELL_CHARACTERS = 4_096

#: The identifiers of the statistics format ``1-1-0`` added. A document that
#: names format ``1-0-0`` and holds one is refused.
_ADDED_IN_1_1_0 = frozenset({QUANTILE_BINNED.identifier})

#: SDMX ``CL_CONF_STATUS`` 1.4 (maintained by SDMX): the codes, with a short
#: paraphrase of what each one means; the SDMX registry holds the official
#: labels. ``F`` is the one status under which a value is published; every
#: other code is a reason it is not, and the value is then absent.
CONF_STATUS: Mapping[str, str] = MappingProxyType(
    {
        "A": "Primary suppression: small counts",
        "C": "Withheld statistical information",
        "D": "Secondary suppression set by the sender, not for publication",
        "E": "Not for publication until an embargo ends, then free for publication",
        "F": "Free for publication",
        "G": "Primary suppression: dominance by one or two units",
        "M": "Primary suppression: other measures of concentration",
        "N": "Not for publication, restricted to internal use",
        "O": "Primary suppression: dominance by one unit",
        "P": "Withheld under non-statistical secrecy arrangements",
        "S": "Secondary suppression set and managed by the receiver, not for publication",
        "T": "Primary suppression: dominance by two units",
        "X": "Deprecated: withheld for military secrecy",
    }
)
FREE = "F"

#: The one period length version 1 knows: a calendar month, ISO 8601.
MONTH = "P1M"
_MONTH_KEY = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")

#: The levels a query's rate is counted over, in their one order.
QUERY_LEVELS: Tuple[str, ...] = ("false", "true")

#: Which statistics each section may carry, by property kind.
_COLUMN_KINDS = frozenset({"mean", "marginalHistogram", "levelHistogram", "support", "quantile"})
_PAIR_KINDS = frozenset({"correlation"})
_PERIOD_KINDS = frozenset({"mean"})
_RELATIONSHIP_KINDS = frozenset({"mean", "marginalHistogram", "quantile"})
_QUERY_KINDS = frozenset({"levelHistogram"})
#: The statistics of the days between two dates, and of their order (format 1-3-0).
_DATE_GAP_KINDS = frozenset({"marginalHistogram", "quantile"})
#: The statistics that count cells, the only ones a profiler may release noisily (format 1-2-0).
_NOISY_KINDS = frozenset({"levelHistogram", "marginalHistogram"})
#: A count table's sensitivity: one row lands in one cell, so L1 and the squared L2 are both one.
_COUNT_SENSITIVITY = Fraction(1)
#: The kinds that count a categorical column; the others read ordered values.
_CATEGORICAL_KINDS = frozenset({"levelHistogram", "support"})
_ORDERED_DOMAINS = frozenset({NUMBER, TIMESTAMP})

#: The metrics whose values are values of the column, so on a timestamp
#: column integer milliseconds; the counts are counts on every column.
_TIMESTAMP_VALUED = (MIN, MAX, QUANTILES)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SampleSize:
    """The band the number of records behind a statistic lies in."""

    minimum: int
    maximum: Optional[int] = None


@dataclass(frozen=True)
class DisclosureRule:
    """One disclosure rule, in ACRO's terms; an absent field was not applied.

    ``minimum_cell`` is ACRO's ``safe_threshold``, ``zeros_disclosive`` its
    ``zeros_are_disclosive``, ``rounding_base`` its ``safe_round_base``,
    ``dominance`` its ``(safe_nk_n, safe_nk_k)`` and ``p_ratio`` its
    ``safe_pratio_p``. They describe output-checking conventions, and a
    profile that names them makes no privacy or anonymity claim.
    """

    minimum_cell: Optional[int] = None
    zeros_disclosive: bool = False
    rounding_base: Optional[int] = None
    dominance: Optional[Tuple[int, Fraction]] = None
    p_ratio: Optional[Fraction] = None


@dataclass(frozen=True)
class ProfileGroup:
    """One group of a statistic: published (status ``F``) or suppressed.

    A published group holds its quantities as :class:`GroupStatistic`. A
    count-table group can still have suppressed cells: :attr:`cell_status`
    holds one status per cell, and a suppressed cell's count reads as 0 in
    :attr:`statistic` and means *unknown*, never zero.
    """

    key: str
    status: str
    statistic: Optional[GroupStatistic] = None
    cell_status: Tuple[str, ...] = ()
    #: The noisy cell values of a noisy statistic's group, as released (format 1-2-0); empty for
    #: an exact one. :attr:`statistic` then holds the point estimate they make
    #: (:func:`flux_stats.noise.point_estimate`).
    noisy: Tuple[Fraction, ...] = ()
    #: The value of a rank correlation's group (format 1-4-0), exact: ``S / C(N, 2)`` or
    #: ``3 A4 / (N (N^2 - 1))`` for an exact group, and for a noisy one the released number clamped
    #: to ``[-1, 1]``. ``None`` for every other statistic and for a suppressed group.
    value: Optional[Fraction] = None

    @property
    def published(self) -> bool:
        """Whether the group's quantities were published at all."""
        return self.status == FREE

    @property
    def suppressed_cells(self) -> Tuple[int, ...]:
        """The positions of the cells whose counts were not published."""
        return tuple(index for index, status in enumerate(self.cell_status) if status != FREE)


@dataclass(frozen=True)
class LinkGrouping:
    """A column statistic grouped by a column of the row a relationship names (format 1-3-0).

    ``child_columns`` are the key columns of the statistic's own table and ``parent_columns``
    those of ``parent_table``, a table of the profile or a ``references`` entry; ``column`` is the
    parent's column whose value keys each group (PROFILE-SPEC.md section 3).
    """

    parent_table: str
    parent_columns: Tuple[str, ...]
    child_columns: Tuple[str, ...]
    column: str


@dataclass(frozen=True)
class BinGrouping:
    """A column statistic grouped by the bin of another column of its table (format 1-4-0).

    ``column`` is a number or timestamp column and ``bins`` its edges, strictly increasing. The
    groups are keyed by the canonical decimal text of each bin's lower edge
    (:func:`flux_stats.cliques.edge_text`); the records whose value of ``column`` is NULL are one
    more group, never written.
    """

    column: str
    bins: Tuple[Fraction, ...]

    @property
    def keys(self) -> Tuple[str, ...]:
        """The group keys, one per bin, in order."""
        return tuple(edge_text(edge) for edge in self.bins[:-1])


@dataclass(frozen=True)
class StatisticEntry:
    """One statistic of a profile and the declared shape it was formed on."""

    definition: StatisticDefinition
    sample_size: SampleSize
    disclosure: str
    groups: Tuple[ProfileGroup, ...]
    group_by: Optional[str] = None
    of: Optional[str] = None
    bins: Tuple[Fraction, ...] = ()
    levels: Tuple[str, ...] = ()
    level: Optional[Fraction] = None
    #: The mechanism, scale and budget share the profiler released a noisy statistic under
    #: (format 1-2-0); ``None`` for an exact one.
    noise: Optional[Noise] = None
    #: The variance of each cell's count: the mechanism's, or for a statistic formed from a
    #: clique (:attr:`derived`) the sum over the cells it adds. ``None`` for an exact statistic.
    cell_variance: Optional[Fraction] = None
    #: Formed by the reader from a clique's noisy counts, not written in the document.
    derived: bool = False
    #: The grouping by a linked row (format 1-3-0); ``None`` for a statistic with none.
    group_by_link: Optional[LinkGrouping] = None
    #: The columns the statistic is counted once per distinct value of (format 1-3-0): its counts
    #: are entities, not rows. Empty for a statistic counted per row.
    per: Tuple[str, ...] = ()
    #: The grouping by the bins of another column (format 1-4-0); ``None`` for a statistic with
    #: none. A statistic that holds one is grouped even though :attr:`group_by` is ``None``.
    group_by_bins: Optional[BinGrouping] = None
    #: The ``rowFloor`` of a rank correlation (format 1-4-0), a count of at least 2; ``None`` when
    #: the statistic states none and ``N`` is the number of its records.
    row_floor: Optional[int] = None

    @property
    def identifier(self) -> str:
        """The registered identifier of the statistic's definition."""
        return self.definition.identifier


@dataclass(frozen=True)
class MetricEntry:
    """One OpenLineage-named column metric; ``value`` is ``None`` when suppressed."""

    definition: MetricDefinition
    status: str
    sample_size: SampleSize
    disclosure: str
    value: Any = None


@dataclass(frozen=True)
class ColumnEntry:
    """What a profile says about one column."""

    name: str
    domain: str
    metrics: Mapping[str, MetricEntry]
    statistics: Tuple[StatisticEntry, ...]
    #: The level histograms its cliques answer that a statistic of the document already
    #: answers too (:func:`_derived`): the draw of a column in a clique rests on these, so a
    #: comparison against the document's own statistic carries both noises (SCORECARD-SPEC.md 9).
    clique_statistics: Tuple[StatisticEntry, ...] = ()


@dataclass(frozen=True)
class PairEntry:
    """Statistics over two columns of one table, in the order they are named."""

    columns: Tuple[str, str]
    statistics: Tuple[StatisticEntry, ...]


@dataclass(frozen=True)
class DatePairEntry:
    """Statistics of the days from ``start`` to ``end``, two timestamp columns of one table.

    ``statistics`` are of ``DAYS_BETWEEN(start, end)`` in whole days, over the rows where both
    are known; ``order`` is a ``levelHistogram/1`` over :data:`DATE_ORDER_LEVELS`, the end against
    the start (format 1-3-0, PROFILE-SPEC.md section 5).
    """

    start: str
    end: str
    statistics: Tuple[StatisticEntry, ...]
    order: StatisticEntry


@dataclass(frozen=True)
class RelationshipDateEntry:
    """Statistics of the days from a parent's date to a child's, across a relationship.

    ``parent`` is a timestamp column of the relationship's parent table and ``child`` one of its
    child table; the statistics and ``order`` are over the joined rows where both are known, the
    child's date against the parent's (format 1-3-0).
    """

    parent: str
    child: str
    statistics: Tuple[StatisticEntry, ...]
    order: StatisticEntry


@dataclass(frozen=True)
class CalendarEntry:
    """How the known values of one timestamp column fall by month, day of week and hour of week.

    Each is an ungrouped ``levelHistogram/1`` over the levels of :data:`CALENDAR_LEVELS`, read in
    UTC, or ``None`` when the entry does not hold it; at least one is held (format 1-3-0).
    """

    column: str
    month: Optional[StatisticEntry] = None
    day_of_week: Optional[StatisticEntry] = None
    hour_of_week: Optional[StatisticEntry] = None


@dataclass(frozen=True)
class ExposureEntry:
    """What a parent's lifetime columns say of the children it can have (format 1-3-0).

    ``start`` and ``end`` are timestamp columns of the relationship's parent table, ``end`` being
    ``None`` when none is named. ``children`` is the number of child rows whose parent's start is
    known and ``active_days`` the sum, over every parent row whose start is known, of the whole
    days it is active inside the window ``window_start`` to ``window_end`` (milliseconds since the
    epoch, both inclusive of their day). Each is a ``count/1`` metric whose ``value`` is ``None``
    when the profile withheld it.
    """

    start: str
    end: Optional[str]
    children: MetricEntry
    active_days: MetricEntry
    window_start: int
    window_end: int


@dataclass(frozen=True)
class PeriodEntry:
    """Counts and sums per period: one group per month of a timestamp column."""

    column: str
    period: str
    statistics: Tuple[StatisticEntry, ...]


@dataclass(frozen=True)
class HistoryColumn:
    """One candidate tracked column of a history (format 1-3-0).

    ``initial`` counts the column's value on first versions. ``transitions`` is one
    ``levelHistogram/1`` grouped by the column itself: each group is a current value, keyed by its
    text, and counts the next value over the changes where the column moves.
    """

    initial: StatisticEntry
    transitions: StatisticEntry


@dataclass(frozen=True)
class HistoryEntry:
    """What a profile says about the versions of one table's entities (format 1-3-0).

    ``entity`` names the columns that identify an entity, and ``start`` and ``end`` the timestamp
    columns of a version's validity window. Every statistic is read as a statistic of the document,
    with its disclosure rule and, where the profile states a privacy block, its noisy cells.
    """

    entity: Tuple[str, ...]
    start: str
    end: str
    versions_per_entity: StatisticEntry
    first_start: StatisticEntry
    version_length: StatisticEntry
    chain: StatisticEntry
    last_version: StatisticEntry
    columns: Mapping[str, HistoryColumn] = field(default_factory=lambda: MappingProxyType({}))
    reasons: Optional[StatisticEntry] = None


@dataclass(frozen=True)
class TableEntry:
    """What a profile says about one table, keyed by its FLUID ``exposeId``."""

    expose_id: str
    row_count: Optional[int]
    columns: Mapping[str, ColumnEntry]
    pairs: Tuple[PairEntry, ...]
    periods: Tuple[PeriodEntry, ...]
    #: The noisy clique marginals of format 1-2-0 (``cliqueMarginal/1``), in document order.
    cliques: Tuple[CliqueEntry, ...] = ()
    #: The ``cliqueMarginal/2`` cliques (format 1-4-0), in document order. They are kept apart from
    #: :attr:`cliques` so that a consumer that draws categorical cliques only does not read a
    #: binned one as one; the agreement of the two lists is checked together.
    cliques2: Tuple[CliqueEntry, ...] = ()
    #: How the table's cliques were chosen and released (format 1-4-0), or ``None``.
    clique_selection: Optional[CliqueSelection] = None
    #: The statistics of the days between two timestamp columns of the table (format 1-3-0).
    date_pairs: Tuple[DatePairEntry, ...] = ()
    #: The counts of a timestamp column by month, day of week and hour of week (format 1-3-0).
    calendar: Tuple[CalendarEntry, ...] = ()
    #: The history of a table with a validity window (format 1-3-0), or ``None`` when absent.
    history: Optional[HistoryEntry] = None


@dataclass(frozen=True)
class RelationshipEntry:
    """Children per parent: statistics over the number of child rows each parent has."""

    parent_table: str
    parent_columns: Tuple[str, ...]
    child_table: str
    child_columns: Tuple[str, ...]
    statistics: Tuple[StatisticEntry, ...]
    #: The classes of parents the source's own constraints leave empty (format 1-3-0): ``childless``
    #: (every parent has a child) and ``orphan`` (every non-NULL child key has its parent). A
    #: structural class is empty by a declared constraint, not by a count of records, so a
    #: profiler states it even where zeros are disclosive. Empty when the entry states none.
    structural: FrozenSet[str] = frozenset()
    #: The statistics of the days between a parent's date and a child's (format 1-3-0).
    dates: Tuple[RelationshipDateEntry, ...] = ()
    #: The exposure of the parent's lifetime columns to children (format 1-3-0).
    exposure: Tuple[ExposureEntry, ...] = ()


@dataclass(frozen=True)
class QueryEntry:
    """A query's rate: how many records it matches, over a two-level count."""

    name: str
    statistics: Tuple[StatisticEntry, ...]


@dataclass(frozen=True)
class InvariantEntry:
    """A declared fact's share: how many rows satisfy it, over a two-level count (format 1-2-0).

    ``name`` is the fact's name, ``<exposeId>.<ruleId>`` for a contract rule. The
    statistic is a ``levelHistogram/1`` over ``["false", "true"]``, so a published
    group's ``levelCounts`` are the rows that do not and do satisfy the fact.
    """

    name: str
    statistics: Tuple[StatisticEntry, ...]

    def counts(self) -> Optional[Tuple[int, int]]:
        """``(satisfied, total)`` over the published counts, or ``None`` when none are published.

        The share of rows that satisfy a fact is its ``true`` count over the sum of its
        published counts (PROFILE-SPEC.md section 8). A group the disclosure rules withheld,
        a cell of it that is withheld, or a total of zero leaves nothing to state, so there
        is no share: ``None``, never a default.
        """
        for statistic in self.statistics:
            for group in statistic.groups:
                if not group.published or group.suppressed_cells or group.statistic is None:
                    continue
                counts = group.statistic.level_counts
                total = sum(counts)
                if len(counts) == len(QUERY_LEVELS) and total > 0:
                    return counts[1], total
        return None


@dataclass(frozen=True)
class ReferenceTable:
    """The rows of a table the owner releases whole (format 1-3-0, PROFILE-SPEC.md section 5b).

    ``disclosure`` names the rule of ``disclosureRules`` that was applied. Every row holds one cell
    per column, in published order; a cell is a string, written as EXPR-SPEC-2 section 8 writes a
    key's value, or ``None`` for NULL.
    """

    disclosure: str
    columns: Tuple[str, ...]
    rows: Tuple[Tuple[Optional[str], ...], ...]


@dataclass(frozen=True)
class Profile:
    """A whole profile, read and checked."""

    producer: str
    fractional_bits: int
    rounding: str
    disclosure_rules: Mapping[str, DisclosureRule]
    tables: Mapping[str, TableEntry]
    relationships: Tuple[RelationshipEntry, ...] = ()
    queries: Tuple[QueryEntry, ...] = ()
    #: The facts' shares of format 1-2-0; empty for a profile of an earlier format.
    invariants: Tuple[InvariantEntry, ...] = ()
    #: The release-level account of a profile released under differential privacy (format
    #: 1-2-0); ``None`` for a profile that carries no noise.
    accounting: Optional[Accounting] = None
    #: The tables released whole, by ``exposeId`` (format 1-3-0); empty for an earlier format.
    references: Mapping[str, ReferenceTable] = field(default_factory=lambda: MappingProxyType({}))
    #: Every noisy part of the release, once each: the place in the document and its mechanism,
    #: scale and share. This is the one enumeration of the budget's parts. The reader checks the
    #: total against it, and a grade or a report counts from it and never from the document.
    parts: Tuple[Tuple[str, Spent], ...] = ()

    def invariant(self, name: str) -> Optional[InvariantEntry]:
        """One fact's entry, or ``None``."""
        return next((entry for entry in self.invariants if entry.name == name), None)

    def column_statistics(self, expose_id: str, column: str) -> Tuple[StatisticEntry, ...]:
        """Every statistic the profile holds over one column of one table."""
        table = self.tables.get(expose_id)
        entry = table.columns.get(column) if table is not None else None
        return entry.statistics if entry is not None else ()

    def pair_statistics(self, expose_id: str, columns: Sequence[str]) -> Tuple[StatisticEntry, ...]:
        """Every statistic the profile holds over one ordered pair of columns."""
        table = self.tables.get(expose_id)
        if table is None:
            return ()
        wanted = tuple(columns)
        return tuple(
            statistic
            for pair in table.pairs
            if pair.columns == wanted
            for statistic in pair.statistics
        )

    def column(self, expose_id: str, column: str) -> Optional[ColumnEntry]:
        """One column's entry, or ``None``."""
        table = self.tables.get(expose_id)
        return table.columns.get(column) if table is not None else None


# ---------------------------------------------------------------------------
# Reading the JSON text
# ---------------------------------------------------------------------------


def _refuse_float(text: str) -> Any:
    raise refuse("profile", "a number is written as a JSON float; write it as a decimal string")


def _refuse_constant(text: str) -> Any:
    raise refuse("profile", f"{text} is not a JSON value a profile may carry")


def _no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    document: Dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise refuse("profile", "an object names one key twice")
        document[key] = value
    return document


#: The most digits a JSON integer in a profile may have: every integer there
#: is a count, at most 2^53 - 1, which has 16. The bound is checked on the
#: text, before CPython forms the integer, whose own limit (4300 digits)
#: would raise a plain ``ValueError`` rather than a refusal.
_MOST_INTEGER_DIGITS = 20


#: The deepest nesting a profile document may have. A profile nests a handful of levels, so this is
#: generous; it is stated here because the reader used to rely on the interpreter instead: CPython
#: 3.11 to 3.13 raise ``RecursionError`` from ``json.loads`` on a deeply nested array, and 3.14 reads
#: one to any depth, so the same text was refused with one reason on one Python and read on another.
_MOST_DEPTH = 128


def _nests_deeper_than(document: Any, limit: int) -> bool:
    """Whether *document* nests more than *limit* levels of lists and objects, counted without recursion."""
    stack: List[Tuple[Any, int]] = [(document, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, dict):
            children: Iterable[Any] = value.values()
        elif isinstance(value, list):
            children = value
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def _read_int(text: str) -> int:
    if len(text.lstrip("-")) > _MOST_INTEGER_DIGITS:
        raise refuse("profile", "an integer has more digits than any count")
    return int(text)


def load_profile_text(text: str) -> Tuple[Profile, Dict[str, Any]]:
    """Read a profile from its JSON text: the profile, and the document it parsed from.

    A JSON float, ``NaN``, ``Infinity`` and a key named twice in one object
    are refused before the document is read, because each has no single
    exact meaning, and so are an integer longer than any count and nesting
    deeper than the reader can follow. The document is returned beside the
    profile because its canonical digest (``jcs-document``) is what a
    Simulation pins.
    """
    try:
        document = json.loads(
            text,
            parse_float=_refuse_float,
            parse_int=_read_int,
            parse_constant=_refuse_constant,
            object_pairs_hook=_no_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise refuse("profile", f"not JSON: {exc.msg} at line {exc.lineno}") from None
    except RecursionError:
        raise refuse("profile", "the document nests deeper than a profile does") from None
    if _nests_deeper_than(document, _MOST_DEPTH):
        raise refuse("profile", "the document nests deeper than a profile does")
    return parse_profile(document), document


# ---------------------------------------------------------------------------
# Reading the document
# ---------------------------------------------------------------------------


def _mapping(
    value: Any, where: str, *, required: Sequence[str] = (), optional: Sequence[str] = ()
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise refuse(where, f"expected an object, got {type(value).__name__}")
    keys = {str(key) for key in value}
    unknown = sorted(keys - set(required) - set(optional))
    if unknown:
        raise refuse(where, f"unknown key {unknown[0]!r}")
    missing = sorted(set(required) - keys)
    if missing:
        raise refuse(where, f"missing key {missing[0]!r}")
    return value


def _object(value: Any, where: str) -> Mapping[str, Any]:
    """A mapping keyed by names (tables, columns, rules, metrics): any key."""
    if not isinstance(value, Mapping):
        raise refuse(where, f"expected an object, got {type(value).__name__}")
    return value


def _name(value: Any, where: str) -> str:
    """A name as the profile writes it: a non-empty string already in NFC.

    Names are keys a lookup goes by, so a profile spells each one the one
    way the engine's canonical form does (KERNEL-SPEC §2.1) rather than
    leaving two spellings of a column to compare unequal.
    """
    return normal_text(value, where)


def _list(value: Any, where: str) -> List[Any]:
    if not isinstance(value, list):
        raise refuse(where, f"expected a list, got {type(value).__name__}")
    return value


def _flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise refuse(where, f"expected true or false, got {type(value).__name__}")
    return value


def _whole_milliseconds(raw: Mapping[str, Any], where: str) -> None:
    """A timestamp metric's values are integer milliseconds, written without a fraction.

    The fidelity check reads a timestamp as integer milliseconds since the
    epoch, so a profile's ``min``, ``max`` and quantile values are spelled
    as integers: the same rule the schema states with ``Units``.
    """
    if "values" in raw:
        for key in sorted(raw["values"]):
            read_units(raw["values"][key], f"{where}.values[{key!r}]")
    else:
        read_units(raw["value"], f"{where}.value")


def _require_nfc(document: Any, where: str) -> None:
    """Refuse any string, key or value, not in Unicode Normalization Form C.

    The digest's canonical form puts every string in NFC (KERNEL-SPEC §2.1),
    so two spellings of one text would share a digest. With every string
    already in NFC, the text the reader reads is the text the digest covers.
    Iterative, so a deep document is never a recursion.
    """
    pending: List[Tuple[Any, str]] = [(document, where)]
    while pending:
        value, at = pending.pop()
        if isinstance(value, str):
            if not unicodedata.is_normalized("NFC", value):
                raise refuse(at, "a string is written in Unicode Normalization Form C")
        elif isinstance(value, Mapping):
            for key, item in value.items():
                if isinstance(key, str) and not unicodedata.is_normalized("NFC", key):
                    raise refuse(at, "a key is written in Unicode Normalization Form C")
                pending.append((item, f"{at}.{key}"))
        elif isinstance(value, list):
            pending.extend((item, f"{at}[{index}]") for index, item in enumerate(value))


def _is_canonical_decimal(text: str) -> bool:
    return text != "-0" and _CANONICAL_DECIMAL.fullmatch(text) is not None


def _is_canonical_integer(text: str) -> bool:
    return _CANONICAL_INTEGER.fullmatch(text) is not None


def _date_pair_bound(count: int, where: str) -> None:
    if count > MAX_DATE_PAIRS:
        raise refuse(where, f"holds at most {MAX_DATE_PAIRS} pairs")


def _status(value: Any, where: str) -> str:
    if not isinstance(value, str) or value not in CONF_STATUS:
        raise refuse(where, "a status is an SDMX CL_CONF_STATUS code")
    return value


def _sample_size(value: Any, where: str) -> SampleSize:
    raw = _mapping(value, where, required=("min",), optional=("max",))
    minimum = read_count(raw["min"], f"{where}.min")
    maximum = read_count(raw["max"], f"{where}.max") if "max" in raw else None
    if maximum is not None and maximum < minimum:
        raise refuse(where, "a sample-size band's max is not below its min")
    return SampleSize(minimum=minimum, maximum=maximum)


def _unit_interval(value: Any, where: str) -> Fraction:
    number = read_decimal(value, where)
    if value.startswith("-") or not 0 <= number <= 1:
        raise refuse(where, "a level, share or ratio lies in [0, 1], written with no sign")
    return number


def _disclosure_rule(value: Any, where: str) -> DisclosureRule:
    raw = _mapping(
        value,
        where,
        optional=("minimumCell", "zerosDisclosive", "roundingBase", "dominance", "pRatio"),
    )

    def positive(key: str) -> Optional[int]:
        if key not in raw:
            return None
        count = read_count(raw[key], f"{where}.{key}")
        if count < 1:
            raise refuse(f"{where}.{key}", "is at least 1")
        return count

    dominance: Optional[Tuple[int, Fraction]] = None
    if "dominance" in raw:
        rule = _mapping(raw["dominance"], f"{where}.dominance", required=("n", "k"))
        n = read_count(rule["n"], f"{where}.dominance.n")
        if n < 1:
            raise refuse(f"{where}.dominance.n", "is at least 1")
        dominance = (n, _unit_interval(rule["k"], f"{where}.dominance.k"))
    return DisclosureRule(
        minimum_cell=positive("minimumCell"),
        zeros_disclosive=(
            _flag(raw["zerosDisclosive"], f"{where}.zerosDisclosive")
            if "zerosDisclosive" in raw
            else False
        ),
        rounding_base=positive("roundingBase"),
        dominance=dominance,
        p_ratio=_unit_interval(raw["pRatio"], f"{where}.pRatio") if "pRatio" in raw else None,
    )


def _statistic_optional(
    noisy_in: Optional[str],
    grouped_by: Optional[Set[str]],
    of: Optional[Set[str]],
    plain_group: bool,
    query_group: bool = False,
) -> Tuple[str, ...]:
    """The keys a statistic may carry beyond the required four, as its section allows them."""
    optional: Tuple[str, ...] = ("bins", "levels", "level")
    if noisy_in is not None:
        optional += ("noise",)
    if grouped_by is not None:
        optional += ("groupBy",) if plain_group else ("groupBy", "groupByLink", "per")
    if query_group:
        optional += ("groupBy",)
    if of is not None:
        optional += ("of",)
    return optional


def _quantile_level(
    raw: Mapping[str, Any], where: str, entry: StatisticDefinition
) -> Optional[Fraction]:
    """A quantile's ``level`` as an exact fraction in the unit interval; any other statistic has none."""
    if entry.kind != "quantile":
        if "level" in raw:
            raise refuse(f"{where}.level", f"{entry.identifier} declares no level")
        return None
    if "level" not in raw:
        raise refuse(where, "a quantile declares its level")
    return _unit_interval(raw["level"], f"{where}.level")


def _exposure_window(value: Any, where: str) -> Tuple[int, int]:
    """An exposure's ``window`` as epoch milliseconds: two integer texts, the start not after the end."""
    window = _mapping(value, where, required=("start", "end"))
    edges = []
    for side in ("start", "end"):
        text = window[side]
        if not isinstance(text, str) or not _is_canonical_integer(text):
            raise refuse(f"{where}.{side}", "is integer milliseconds since the epoch")
        edges.append(int(text))
    if edges[0] > edges[1]:
        raise refuse(where, "a window does not start after it ends")
    return edges[0], edges[1]


class _Reader:
    """One profile's reading state: the rules a statistic may name."""

    def __init__(
        self,
        rules: Mapping[str, DisclosureRule],
        *,
        first_format: bool,
        accounting: Optional[Accounting] = None,
        from_1_3: bool = False,
        from_1_4: bool = False,
    ) -> None:
        self.rules = rules
        #: Whether the document names format ``1-3-0``, the only one that holds a ``groupBy`` on
        #: a number column, a ``groupByLink`` or a ``per``.
        self.from_1_3 = from_1_3
        #: Whether the document names format ``1-4-0``, the only one that holds the forms of
        #: :data:`_FORMS_OF_1_4_0`.
        self.from_1_4 = from_1_4
        #: Every statistic grouped by a link, with the place it was read at and its own table,
        #: checked against the tables once all of them are read (:meth:`check_links`).
        self.links: List[Tuple[str, str, StatisticEntry]] = []
        #: The release-level account, when the document states one (format 1-2-0).
        self.accounting = accounting
        #: Every noisy statistic and clique read, by place, for the budget check.
        self.parts: List[Tuple[str, Spent]] = []
        #: Whether the document names format ``1-0-0``, which holds none of
        #: the statistics :data:`_ADDED_IN_1_1_0` names.
        self.first_format = first_format

    def require_1_4_0(self, form: str, where: str) -> None:
        """Refuse (``FLX-5036``) a form of format 1-4-0 in a document that names an earlier one."""
        if form not in _FORMS_OF_1_4_0:
            raise KeyError(f"{form!r} is no form of format 1-4-0")
        if not self.from_1_4:
            raise refuse(
                where,
                f"{_FORMS_OF_1_4_0[form]} is a form of format 1-4-0, and this document names an "
                "earlier format",
            )

    # -- statistics ----------------------------------------------------------

    def statistic(
        self,
        value: Any,
        where: str,
        *,
        kinds: FrozenSet[str],
        grouped_by: Optional[Set[str]] = None,
        of: Optional[Set[str]] = None,
        period: bool = False,
        domain: Optional[str] = None,
        noisy_in: Optional[str] = None,
        numeric_by: Optional[Set[str]] = None,
        column_of: Optional[Tuple[str, Set[str]]] = None,
        plain_group: bool = False,
        query_group: bool = False,
    ) -> StatisticEntry:
        """Read one statistic of a section that allows *kinds*.

        *noisy_in* is the expose a column statistic belongs to: a statistic may carry a ``noise``
        block (format 1-2-0) only there. *numeric_by* are the number columns of that table, which
        a ``groupBy`` may name in format ``1-3-0``, and *column_of* is the statistic's own column
        and every column of the table, for ``per``. A *plain_group* section takes a ``groupBy`` and
        no ``groupByLink`` and no ``per``. *query_group* lets a query's statistic carry a ``groupBy``
        (format ``1-3-0``).
        """
        raw = _mapping(
            value,
            where,
            required=("definition", "sampleSize", "disclosure", "groups"),
            optional=_statistic_optional(noisy_in, grouped_by, of, plain_group, query_group),
        )
        entry = self._read_definition(raw, where, kinds, domain)
        bins = self._bins(raw, where, entry)
        levels = self._levels(raw, where, entry)
        level = _quantile_level(raw, where, entry)
        if query_group:
            group_by, key_form = self._query_group_by(raw, where)
        else:
            group_by, key_form = self._group_by(raw, where, grouped_by, numeric_by)
        link = self._group_by_link(raw, where, group_by)
        per = self._read_per(raw, where, group_by, link, column_of)
        if per and "noise" in raw:
            raise refuse(f"{where}.noise", _MOVES_AN_ENTITY + " in a statistic with per")
        if link is not None and "noise" in raw:
            raise refuse(f"{where}.noise", _MOVES_AN_ENTITY + " in a statistic with groupByLink")
        measured = self._measured(raw, where, of)
        disclosure = raw["disclosure"]
        if not isinstance(disclosure, str) or disclosure not in self.rules:
            raise refuse(f"{where}.disclosure", "names no rule in disclosureRules")
        cells = (
            (len(bins) - 1) if entry.kind == "marginalHistogram" or entry.binned else len(levels)
        )
        noise = self._noise(raw, where, entry, noisy_in)
        groups = self._groups(
            raw["groups"],
            f"{where}.groups",
            entry,
            cells=cells,
            grouped=group_by is not None or link is not None,
            period=period,
            noisy=noise is not None,
            key_form=key_form,
        )
        found = StatisticEntry(
            definition=entry,
            sample_size=_sample_size(raw["sampleSize"], f"{where}.sampleSize"),
            disclosure=disclosure,
            groups=groups,
            group_by=group_by,
            of=measured,
            bins=bins,
            levels=levels,
            level=level,
            noise=noise,
            cell_variance=noise.variance if noise is not None else None,
            group_by_link=link,
            per=per,
        )
        if link is not None:
            assert noisy_in is not None  # a link is read only in a column statistic
            self.links.append((where, noisy_in, found))
        return found

    def _read_definition(
        self, raw: Mapping[str, Any], where: str, kinds: FrozenSet[str], domain: Optional[str]
    ) -> StatisticDefinition:
        """The registered definition a statistic names, once its section and column allow it."""
        identifier = raw["definition"]
        if not isinstance(identifier, str):
            raise refuse(f"{where}.definition", "a definition is a registered identifier")
        try:
            entry = definition(identifier)
        except KeyError:
            raise refuse(f"{where}.definition", "no statistic is registered under it") from None
        if entry.kind not in kinds:
            raise refuse(f"{where}.definition", f"this section carries {sorted(kinds)} only")
        if self.first_format and entry.identifier in _ADDED_IN_1_1_0:
            raise refuse(
                f"{where}.definition",
                f"{entry.identifier} is a statistic of format 1-1-0, and this document names 1-0-0",
            )
        if domain is not None:
            wanted = {CATEGORY} if entry.kind in _CATEGORICAL_KINDS else _ORDERED_DOMAINS
            if domain not in wanted:
                raise refuse(where, f"{entry.kind} does not apply to a {domain} column")
        return entry

    def _group_by(
        self,
        raw: Mapping[str, Any],
        where: str,
        grouped_by: Optional[Set[str]],
        numeric_by: Optional[Set[str]],
    ) -> Tuple[Optional[str], Optional[Callable[[str], bool]]]:
        """The column a statistic is grouped by and how its group keys are written (format 1-3-0
        for a number column)."""
        if "groupBy" not in raw:
            return None, None
        assert grouped_by is not None
        group_by = _name(raw["groupBy"], f"{where}.groupBy")
        if group_by in grouped_by:
            return group_by, None
        if numeric_by is not None and group_by in numeric_by:
            self._require_1_3(f"{where}.groupBy", "a groupBy on a number column")
            return group_by, _is_canonical_decimal
        raise refuse(
            f"{where}.groupBy",
            (
                "names no categorical or number column of this table"
                if self.from_1_3
                else "names no categorical column of this table"
            ),
        )

    def _query_group_by(
        self, raw: Mapping[str, Any], where: str
    ) -> Tuple[Optional[str], Optional[Callable[[str], bool]]]:
        """The column of a query's rows its rate is grouped by (format 1-3-0), or ``None``."""
        if "groupBy" not in raw:
            return None, None
        if not self.from_1_3:
            raise refuse(
                f"{where}.groupBy",
                "a groupBy on a query's statistic is a form of format 1-3-0, "
                "and this document names an earlier format",
            )
        return _name(raw["groupBy"], f"{where}.groupBy"), None

    def _group_by_link(
        self, raw: Mapping[str, Any], where: str, group_by: Optional[str]
    ) -> Optional[LinkGrouping]:
        """The link a statistic is grouped by (format 1-3-0), or ``None``."""
        if "groupByLink" not in raw:
            return None
        self._require_1_3(f"{where}.groupByLink", "a groupByLink")
        if group_by is not None:
            raise refuse(where, "a statistic holds at most one of groupBy and groupByLink")
        return self._link(raw["groupByLink"], f"{where}.groupByLink")

    def _read_per(
        self,
        raw: Mapping[str, Any],
        where: str,
        group_by: Optional[str],
        link: Optional[LinkGrouping],
        column_of: Optional[Tuple[str, Set[str]]],
    ) -> Tuple[str, ...]:
        """The columns a statistic is counted per (format 1-3-0), or none."""
        if "per" not in raw:
            return ()
        self._require_1_3(f"{where}.per", "a per")
        if group_by is not None or link is not None:
            raise refuse(where, "a statistic with per holds no groupBy and no groupByLink")
        assert column_of is not None
        return self._per(raw["per"], f"{where}.per", *column_of)

    @staticmethod
    def _measured(raw: Mapping[str, Any], where: str, of: Optional[Set[str]]) -> Optional[str]:
        """The number column a period statistic sums, or ``None`` when the section has none."""
        if of is None:
            return None
        if "of" not in raw:
            raise refuse(where, "a period statistic names the column it sums with 'of'")
        measured = _name(raw["of"], f"{where}.of")
        if measured not in of:
            raise refuse(f"{where}.of", "names no number column of this table")
        return measured

    def _require_1_3(self, where: str, what: str) -> None:
        if not self.from_1_3:
            raise refuse(
                where,
                f"{what} is a form of format 1-3-0, and this document names an earlier format",
            )

    @staticmethod
    def _link(value: Any, where: str) -> LinkGrouping:
        raw = _mapping(value, where, required=("parent", "columns", "column"))
        parent = _mapping(raw["parent"], f"{where}.parent", required=("table", "columns"))
        table = _name(parent["table"], f"{where}.parent.table")
        parent_columns = tuple(read_texts(parent["columns"], f"{where}.parent.columns"))
        child_columns = tuple(read_texts(raw["columns"], f"{where}.columns"))
        if not parent_columns or not child_columns:
            raise refuse(where, "a link names at least one key column on each side")
        if len(parent_columns) != len(child_columns):
            raise refuse(where, "a parent and a child key have the same number of columns")
        return LinkGrouping(
            parent_table=table,
            parent_columns=parent_columns,
            child_columns=child_columns,
            column=_name(raw["column"], f"{where}.column"),
        )

    @staticmethod
    def _per(value: Any, where: str, own: str, names: Set[str]) -> Tuple[str, ...]:
        columns = tuple(read_texts(value, where))
        if not 1 <= len(columns) <= MAX_PER_COLUMNS:
            raise refuse(where, f"names 1 to {MAX_PER_COLUMNS} columns")
        if len(set(columns)) != len(columns):
            raise refuse(where, "names a column twice")
        if own in columns:
            raise refuse(where, "does not name the statistic's own column")
        if not set(columns) <= names:
            raise refuse(where, "names columns of its table")
        return columns

    def check_links(
        self, tables: Mapping[str, TableEntry], references: Mapping[str, ReferenceTable]
    ) -> None:
        """Check each ``groupByLink`` against the tables, once every table is read.

        The parent is a table of the profile or a ``references`` entry; its key columns and the
        column that keys the groups are columns of it, and the child's key columns are columns of
        the statistic's own table. A group key is written as a cell is (PROFILE-SPEC.md section
        5b), so where the parent column is a number or a timestamp of a table of the profile its
        keys are the canonical decimal text, or whole milliseconds.
        """
        for where, child, statistic in self.links:
            link = statistic.group_by_link
            assert link is not None
            if not set(link.child_columns) <= set(tables[child].columns):
                raise refuse(f"{where}.groupByLink.columns", "names columns of its table")
            domain: Optional[str] = None
            if link.parent_table in tables:
                parent = tables[link.parent_table]
                names = set(parent.columns)
                if link.column in parent.columns:
                    domain = parent.columns[link.column].domain
            elif link.parent_table in references:
                names = set(references[link.parent_table].columns)
            else:
                raise refuse(
                    f"{where}.groupByLink.parent.table",
                    "names no table of this profile and no reference table",
                )
            if not set(link.parent_columns) <= names:
                raise refuse(f"{where}.groupByLink.parent.columns", "names columns of its table")
            if link.column not in names:
                raise refuse(f"{where}.groupByLink.column", "names no column of the parent table")
            form = {NUMBER: _is_canonical_decimal, TIMESTAMP: _is_canonical_integer}.get(
                domain or ""
            )
            if form is not None and not all(form(group.key) for group in statistic.groups):
                raise refuse(
                    f"{where}.groups",
                    "a group is keyed by its value as a cell writes it: a number as canonical "
                    "decimal text, a timestamp as whole milliseconds",
                )

    def _noise(
        self,
        raw: Mapping[str, Any],
        where: str,
        entry: StatisticDefinition,
        noisy_in: Optional[str],
    ) -> Optional[Noise]:
        """The statistic's ``noise`` block, read and entered in the budget, or ``None``."""
        if "noise" not in raw:
            return None
        if entry.kind not in _NOISY_KINDS:
            raise refuse(
                f"{where}.noise", f"is carried by {sorted(_NOISY_KINDS)} statistics, as counts"
            )
        accounting = self._require_account(f"{where}.noise")
        assert noisy_in is not None  # the key is optional only where a statistic may carry it
        self._in_unit(noisy_in, f"{where}.noise", accounting)
        noise = read_noise(
            raw["noise"],
            f"{where}.noise",
            accounting,
            l1=_COUNT_SENSITIVITY,
            l2_squared=_COUNT_SENSITIVITY,
        )
        self.parts.append((where, noise))
        return noise

    def _require_account(self, where: str) -> Accounting:
        if self.accounting is None:
            raise refuse(where, "a noisy statistic needs the profile's privacy block")
        return self.accounting

    @staticmethod
    def _in_unit(expose_id: str, where: str, accounting: Accounting) -> None:
        """A noisy count lies in the table of the unit of privacy, and only there (ADR 0019)."""
        if expose_id != accounting.unit:
            raise refuse(
                where,
                "the profile's unit of privacy is one row of another expose, and the profile "
                "does not state how many rows of this table one unit contributes",
            )

    def statistics(self, value: Any, where: str, **section: Any) -> Tuple[StatisticEntry, ...]:
        """Read a section's list of statistics; no two may answer one question."""
        read = tuple(
            self.statistic(item, f"{where}[{index}]", **section)
            for index, item in enumerate(_list(value, where))
        )
        self._no_repeats(read, where)
        return read

    @staticmethod
    def _no_repeats(read: Tuple[StatisticEntry, ...], where: str) -> None:
        """Refuse a list in which two statistics answer one question."""
        seen: Set[Tuple[Any, ...]] = set()
        for index, statistic in enumerate(read):
            key = (
                statistic.identifier,
                statistic.group_by,
                statistic.group_by_link,
                statistic.group_by_bins,
                statistic.per,
                statistic.of,
                statistic.bins,
                statistic.levels,
                statistic.level,
            )
            if key in seen:
                raise refuse(f"{where}[{index}]", "repeats a statistic already in this list")
            seen.add(key)

    @staticmethod
    def _bins(
        raw: Mapping[str, Any], where: str, entry: StatisticDefinition
    ) -> Tuple[Fraction, ...]:
        if entry.kind != "marginalHistogram" and not entry.binned:
            if "bins" in raw:
                raise refuse(f"{where}.bins", f"{entry.identifier} declares no bins")
            return ()
        if "bins" not in raw:
            raise refuse(where, f"{entry.identifier} declares its bin edges")
        edges = tuple(
            read_decimal(edge, f"{where}.bins[{index}]")
            for index, edge in enumerate(_list(raw["bins"], f"{where}.bins"))
        )
        if len(edges) < 2 or any(b <= a for a, b in zip(edges, edges[1:], strict=False)):
            raise refuse(f"{where}.bins", "bin edges are at least two, strictly increasing")
        return edges

    @staticmethod
    def _levels(raw: Mapping[str, Any], where: str, entry: StatisticDefinition) -> Tuple[str, ...]:
        if entry.kind != "levelHistogram":
            if "levels" in raw:
                raise refuse(f"{where}.levels", f"{entry.identifier} declares no levels")
            return ()
        if "levels" not in raw:
            raise refuse(where, "a level histogram declares its levels")
        levels = tuple(read_texts(raw["levels"], f"{where}.levels"))
        if not levels or levels != level_order(set(levels)):
            raise refuse(f"{where}.levels", "levels are distinct and sorted, and at least one")
        return levels

    def _groups(
        self,
        value: Any,
        where: str,
        entry: StatisticDefinition,
        *,
        cells: int,
        grouped: bool,
        period: bool,
        noisy: bool = False,
        key_form: Optional[Callable[[str], bool]] = None,
    ) -> Tuple[ProfileGroup, ...]:
        items = _list(value, where)
        if not items:
            raise refuse(where, "a statistic has at least one group")
        read_group_at = self._noisy_group if noisy else self._group
        groups = tuple(
            read_group_at(item, f"{where}[{index}]", entry, cells=cells)
            for index, item in enumerate(items)
        )
        keys = [group.key for group in groups]
        if keys != sorted(set(keys)):
            raise refuse(where, "groups are distinct and sorted by key")
        if not grouped and not period and keys != [""]:
            raise refuse(where, "an ungrouped statistic has one group, keyed by the empty string")
        if (grouped or period) and "" in keys:
            raise refuse(where, "a grouped statistic keys every group by a name")
        if period and not all(_MONTH_KEY.fullmatch(key) for key in keys):
            raise refuse(where, "a period group is keyed by its month, YYYY-MM")
        if key_form is not None and not all(key_form(key) for key in keys):
            raise refuse(where, "a group is keyed by the canonical decimal text of its value")
        return groups

    def _group(
        self, value: Any, where: str, entry: StatisticDefinition, *, cells: int
    ) -> ProfileGroup:
        if not isinstance(value, Mapping):
            raise refuse(where, f"a group is an object, got {type(value).__name__}")
        status = _status(value.get("status"), f"{where}.status")
        if status != FREE:
            raw = _mapping(value, where, required=("group", "status"))
            key = raw["group"]
            return ProfileGroup(
                key="" if key == "" else _name(key, f"{where}.group"), status=status
            )
        document = {
            str(key): item for key, item in value.items() if key not in ("status", "cellStatus")
        }
        cell_status: Tuple[str, ...] = ()
        if entry.cell_table:
            field = "binCounts" if entry.kind == "marginalHistogram" else "levelCounts"
            cell_status, document[field] = self._cells(value, where, field, cells)
        elif "cellStatus" in value:
            raise refuse(f"{where}.cellStatus", f"{entry.identifier} has no cells")
        statistic = read_group(entry, document, where)
        if entry.binned:
            self._bin(statistic, where, bins=cells)
        return ProfileGroup(
            key=statistic.group, status=status, statistic=statistic, cell_status=cell_status
        )

    @staticmethod
    def _noisy_group(
        value: Any, where: str, entry: StatisticDefinition, *, cells: int
    ) -> ProfileGroup:
        """A published group of a noisy statistic: its group key and one noisy value per cell.

        The values are decimal strings (they can be negative or fractional). The group's
        :class:`GroupStatistic` is their point estimate (:func:`flux_stats.noise.point_estimate`):
        whole counts that add up to the estimated record count, so a comparison reads a noisy
        group as it reads an exact one.
        """
        raw = _mapping(value, where, required=("group", "status", "noisyCounts"))
        if raw["status"] != FREE:
            raise refuse(f"{where}.status", "a noisy statistic publishes every group, status F")
        key = raw["group"]
        key = "" if key == "" else _name(key, f"{where}.group")
        values = _list(raw["noisyCounts"], f"{where}.noisyCounts")
        if len(values) != cells:
            raise refuse(f"{where}.noisyCounts", f"{len(values)} values for {cells} cells")
        noisy = tuple(
            read_decimal(item, f"{where}.noisyCounts[{index}]") for index, item in enumerate(values)
        )
        estimate = point_estimate(noisy)
        fields = {
            "level_counts" if entry.kind == "levelHistogram" else "bin_counts": estimate.counts
        }
        state = GroupStatistic(group=key, k=estimate.records, **fields)  # type: ignore[arg-type]
        return ProfileGroup(key=key, status=FREE, statistic=state, noisy=noisy)

    @staticmethod
    def _bin(statistic: GroupStatistic, where: str, *, bins: int) -> None:
        """A published ``quantile/2`` group names a declared bin and counts that fit it.

        ``quantileBin`` is one of the statistic's ``bins``, and ``binBelow``
        and ``binThrough`` are counts of its ``k`` records in that order, so
        the bin lies where the counts say it does. Whether the counts show
        the order statistic in that bin is the verifier's to check, as
        ``quantile/1``'s ranks are.
        """
        if statistic.k == 0 and statistic.quantile_bin is None:
            # An empty group holds no order statistic, as quantile/1's holds no
            # witness: read here, and refused by the property that compares it.
            if statistic.bin_below or statistic.bin_through:
                raise refuse(f"{where}.binThrough", "an empty group counts no records")
            return
        if statistic.quantile_bin is None or not 0 <= statistic.quantile_bin < bins:
            raise refuse(f"{where}.quantileBin", "names one of the statistic's bins")
        if not statistic.bin_below <= statistic.bin_through <= statistic.k:
            raise refuse(
                f"{where}.binThrough", "binBelow is at most binThrough, which is at most k"
            )
        # The first bin starts at the declared minimum and the last ends at the
        # declared maximum, so no record lies below the first or above the last.
        # Rounding keeps both: it maps 0 to 0 and k and binThrough alike.
        if statistic.quantile_bin == 0 and statistic.bin_below != 0:
            raise refuse(f"{where}.binBelow", "no record lies below the first bin")
        if statistic.quantile_bin == bins - 1 and statistic.bin_through != statistic.k:
            raise refuse(f"{where}.binThrough", "every record lies through the last bin")

    @staticmethod
    def _cells(
        value: Mapping[str, Any], where: str, field: str, cells: int
    ) -> Tuple[Tuple[str, ...], List[Optional[int]]]:
        counts = read_counts(value.get(field), f"{where}.{field}", length=cells)
        if "cellStatus" in value:
            raw = _list(value["cellStatus"], f"{where}.cellStatus")
            if len(raw) != cells:
                raise refuse(f"{where}.cellStatus", f"{len(raw)} statuses for {cells} cells")
            statuses = tuple(
                _status(item, f"{where}.cellStatus[{index}]") for index, item in enumerate(raw)
            )
        else:
            statuses = (FREE,) * cells
        for index, (count, status) in enumerate(zip(counts, statuses, strict=True)):
            if (count is None) != (status != FREE):
                raise refuse(
                    f"{where}.{field}[{index}]",
                    "a cell is null exactly when its status is not F",
                )
        return statuses, [0 if count is None else count for count in counts]

    # -- metrics -------------------------------------------------------------

    def metric(self, name: str, value: Any, where: str, *, domain: str) -> MetricEntry:
        known = {entry.name: entry for entry in metrics()}
        if name not in known:
            raise refuse(where, f"metrics are named {sorted(known)}")
        entry = known[name]
        if domain not in entry.domains:
            raise refuse(where, f"{name} does not apply to a {domain} column")
        value_keys = ("values",) if name == "quantiles" else ("value",)
        status = _status(
            value.get("status") if isinstance(value, Mapping) else None, f"{where}.status"
        )
        raw = _mapping(
            value,
            where,
            required=("definition", "status", "sampleSize", "disclosure")
            + (value_keys if status == FREE else ()),
        )
        if raw["definition"] != entry.identifier:
            raise refuse(f"{where}.definition", f"the {name} metric is {entry.identifier}")
        disclosure = raw["disclosure"]
        if not isinstance(disclosure, str) or disclosure not in self.rules:
            raise refuse(f"{where}.disclosure", "names no rule in disclosureRules")
        value_read = entry.read(raw, where) if status == FREE else None
        if value_read is not None and domain == TIMESTAMP and entry in _TIMESTAMP_VALUED:
            _whole_milliseconds(raw, where)
        return MetricEntry(
            definition=entry,
            status=status,
            sample_size=_sample_size(raw["sampleSize"], f"{where}.sampleSize"),
            disclosure=disclosure,
            value=value_read,
        )

    # -- sections ------------------------------------------------------------

    def column(
        self,
        name: str,
        value: Any,
        where: str,
        categorical: Set[str],
        expose_id: Optional[str] = None,
        numeric: Optional[Set[str]] = None,
        names: Optional[Set[str]] = None,
    ) -> ColumnEntry:
        raw = _mapping(value, where, required=("domain",), optional=("metrics", "statistics"))
        domain = raw["domain"]
        if domain not in DOMAINS:
            raise refuse(f"{where}.domain", f"a domain is one of {list(DOMAINS)}")
        found_metrics: Dict[str, MetricEntry] = {}
        for metric_name in sorted(_object(raw.get("metrics", {}), f"{where}.metrics")):
            found_metrics[metric_name] = self.metric(
                metric_name,
                raw["metrics"][metric_name],
                f"{where}.metrics.{metric_name}",
                domain=domain,
            )
        statistics = self.statistics(
            raw.get("statistics", []),
            f"{where}.statistics",
            kinds=_COLUMN_KINDS,
            grouped_by=categorical - {name},
            domain=domain,
            noisy_in=expose_id,
            numeric_by=(numeric or set()) - {name},
            column_of=(name, names if names is not None else {name}),
        )
        low, high = found_metrics.get("min"), found_metrics.get("max")
        if low is not None and high is not None and low.value is not None:
            if high.value is not None and low.value > high.value:
                raise refuse(f"{where}.metrics", "the published min is above the published max")
        return ColumnEntry(
            name=name,
            domain=domain,
            metrics=MappingProxyType(found_metrics),
            statistics=statistics,
        )

    def table(self, expose_id: str, value: Any, where: str) -> TableEntry:
        raw = _mapping(
            value,
            where,
            required=("columns",),
            optional=(
                "rowCount",
                "columnPairs",
                "periods",
                "cliques",
                "datePairs",
                "calendar",
                "history",
                "cliqueSelection",
            ),
        )
        declared = _object(raw["columns"], f"{where}.columns")
        names = {_name(name, f"{where}.columns") for name in declared}
        domains = {
            name: _object(declared[name], f"{where}.columns.{name}").get("domain") for name in names
        }
        categorical = {name for name, domain in domains.items() if domain == CATEGORY}
        ordered = {
            name
            for name, domain in domains.items()
            if isinstance(domain, str) and domain in _ORDERED_DOMAINS
        }
        numeric = {name for name, domain in domains.items() if domain == NUMBER}
        columns = {
            name: self.column(
                name,
                declared[name],
                f"{where}.columns.{name}",
                categorical,
                expose_id,
                numeric=numeric,
                names=names,
            )
            for name in sorted(names)
        }
        cliques, cliques2 = self._cliques(
            raw.get("cliques", []),
            where,
            expose_id,
            categorical,
            {name: domain for name, domain in domains.items() if isinstance(domain, str)},
        )
        selection = self._clique_selection(raw, where, expose_id)
        if cliques or cliques2:
            formed = {
                name: _derived(name, cliques + cliques2, entry) for name, entry in columns.items()
            }
            columns = {
                name: replace(
                    entry,
                    statistics=entry.statistics + formed[name][0],
                    clique_statistics=formed[name][1],
                )
                for name, entry in columns.items()
            }
        pairs = tuple(
            self._pair(item, f"{where}.columnPairs[{index}]", ordered, expose_id)
            for index, item in enumerate(_list(raw.get("columnPairs", []), f"{where}.columnPairs"))
        )
        if len({pair.columns for pair in pairs}) != len(pairs):
            raise refuse(f"{where}.columnPairs", "names one pair twice")
        periods = tuple(
            self._period(item, f"{where}.periods[{index}]", domains)
            for index, item in enumerate(_list(raw.get("periods", []), f"{where}.periods"))
        )
        if len({(period.column, period.period) for period in periods}) != len(periods):
            raise refuse(f"{where}.periods", "names one period twice")
        date_pairs = self._date_pairs(raw, where, expose_id, domains, categorical, numeric)
        return TableEntry(
            expose_id=expose_id,
            row_count=(
                read_count(raw["rowCount"], f"{where}.rowCount") if "rowCount" in raw else None
            ),
            columns=MappingProxyType(columns),
            pairs=pairs,
            periods=periods,
            cliques=cliques,
            cliques2=cliques2,
            clique_selection=selection,
            date_pairs=date_pairs,
            calendar=self._calendar(raw, where, expose_id, domains),
            history=self._history(raw, where, expose_id, domains),
        )

    def _calendar(
        self, raw: Mapping[str, Any], where: str, expose_id: str, domains: Mapping[str, Any]
    ) -> Tuple[CalendarEntry, ...]:
        """A table's ``calendar`` (format 1-3-0): one entry per timestamp column."""
        if "calendar" not in raw:
            return ()
        self._require_1_3(f"{where}.calendar", "calendar")
        items = _list(raw["calendar"], f"{where}.calendar")
        if len(items) > MAX_CALENDAR_ENTRIES:
            raise refuse(f"{where}.calendar", f"holds at most {MAX_CALENDAR_ENTRIES} entries")
        found = []
        for index, item in enumerate(items):
            at = f"{where}.calendar[{index}]"
            entry = _mapping(item, at, required=("column",), optional=tuple(CALENDAR_LEVELS))
            column = _name(entry["column"], f"{at}.column")
            if domains.get(column) != TIMESTAMP:
                raise refuse(f"{at}.column", "names no timestamp column of this table")
            held = [key for key in CALENDAR_LEVELS if key in entry]
            if not held:
                raise refuse(at, "a calendar entry holds at least one of its three histograms")
            read = {
                key: self._calendar_histogram(entry[key], f"{at}.{key}", key, expose_id)
                for key in held
            }
            found.append(
                CalendarEntry(
                    column=column,
                    month=read.get("month"),
                    day_of_week=read.get("dayOfWeek"),
                    hour_of_week=read.get("hourOfWeek"),
                )
            )
        if len({entry.column for entry in found}) != len(found):
            raise refuse(f"{where}.calendar", "names one column twice")
        return tuple(found)

    def _calendar_histogram(
        self, value: Any, where: str, key: str, noisy_in: Optional[str]
    ) -> StatisticEntry:
        """One histogram of a calendar entry: ungrouped, over exactly the levels of its key."""
        expected = list(CALENDAR_LEVELS[key])
        if isinstance(value, Mapping) and value.get("levels") != expected:
            raise refuse(f"{where}.levels", f"a {key} histogram counts the levels of the format")
        histogram = self.statistic(
            value, where, kinds=_QUERY_KINDS, domain=CATEGORY, noisy_in=noisy_in
        )
        if histogram.levels != CALENDAR_LEVELS[key]:
            raise refuse(f"{where}.levels", f"a {key} histogram counts the levels of the format")
        return histogram

    def _date_pairs(
        self,
        raw: Mapping[str, Any],
        where: str,
        expose_id: str,
        domains: Mapping[str, Any],
        categorical: Set[str],
        numeric: Set[str],
    ) -> Tuple[DatePairEntry, ...]:
        """A table's ``datePairs`` (format 1-3-0): each a pair of its timestamp columns."""
        if "datePairs" not in raw:
            return ()
        self._require_1_3(f"{where}.datePairs", "datePairs")
        items = _list(raw["datePairs"], f"{where}.datePairs")
        _date_pair_bound(len(items), f"{where}.datePairs")
        found = []
        for index, item in enumerate(items):
            at = f"{where}.datePairs[{index}]"
            entry = _mapping(item, at, required=("start", "end", "statistics", "order"))
            start, end = _name(entry["start"], f"{at}.start"), _name(entry["end"], f"{at}.end")
            if start == end:
                raise refuse(at, "a date pair is two different columns")
            for side, column in (("start", start), ("end", end)):
                if domains.get(column) != TIMESTAMP:
                    raise refuse(f"{at}.{side}", "names no timestamp column of this table")
            found.append(
                DatePairEntry(
                    start=start,
                    end=end,
                    statistics=self.statistics(
                        entry["statistics"],
                        f"{at}.statistics",
                        kinds=_DATE_GAP_KINDS,
                        grouped_by=categorical,
                        numeric_by=numeric,
                        domain=NUMBER,
                        noisy_in=expose_id,
                        plain_group=True,
                    ),
                    order=self._date_order(entry["order"], f"{at}.order", expose_id),
                )
            )
        if len({(pair.start, pair.end) for pair in found}) != len(found):
            raise refuse(f"{where}.datePairs", "names one pair twice")
        return tuple(found)

    def _date_order(self, value: Any, where: str, noisy_in: Optional[str]) -> StatisticEntry:
        """The ``order`` of a date pair: one ungrouped ``levelHistogram/1`` over the three levels."""
        if isinstance(value, Mapping) and value.get("levels") != list(DATE_ORDER_LEVELS):
            raise refuse(f"{where}.levels", f"an order counts the levels {list(DATE_ORDER_LEVELS)}")
        order = self.statistic(value, where, kinds=_QUERY_KINDS, domain=CATEGORY, noisy_in=noisy_in)
        if order.levels != DATE_ORDER_LEVELS:
            raise refuse(f"{where}.levels", f"an order counts the levels {list(DATE_ORDER_LEVELS)}")
        return order

    def _history(
        self, raw: Mapping[str, Any], where: str, expose_id: str, domains: Mapping[str, Any]
    ) -> Optional[HistoryEntry]:
        """The ``history`` of a table (format 1-3-0), or ``None`` when it states none."""
        if "history" not in raw:
            return None
        here = f"{where}.history"
        self._require_1_3(here, "'history'")
        found = _mapping(
            raw["history"],
            here,
            required=(
                "entity",
                "start",
                "end",
                "versionsPerEntity",
                "firstStart",
                "versionLength",
                "chain",
                "lastVersion",
            ),
            optional=("columns", "reasons"),
        )
        entity = tuple(read_texts(found["entity"], f"{here}.entity"))
        start, end = (_name(found[key], f"{here}.{key}") for key in ("start", "end"))
        if not entity or len(set(entity)) != len(entity) or not set(entity) <= set(domains):
            raise refuse(f"{here}.entity", "names distinct columns of its table, at least one")
        for key, column in (("start", start), ("end", end)):
            if domains.get(column) != TIMESTAMP:
                raise refuse(f"{here}.{key}", "names a timestamp column of its table")
        if start == end or {start, end} & set(entity):
            raise refuse(here, "an entity, a start and an end name different columns")
        shapes = {
            key: self._history_statistic(found[key], f"{here}.{key}", expose_id, kind, levels)
            for key, kind, levels in (
                ("versionsPerEntity", "marginalHistogram", None),
                ("firstStart", "marginalHistogram", None),
                ("versionLength", "marginalHistogram", None),
                ("chain", "levelHistogram", CHAIN_LEVELS),
                ("lastVersion", "levelHistogram", LAST_VERSION_LEVELS),
            )
        }
        taken = set(entity) | {start, end}
        columns = self._history_columns(found, here, expose_id, domains, taken)
        reasons = self._history_reasons(found, here, expose_id, set(columns))
        return HistoryEntry(
            entity=entity,
            start=start,
            end=end,
            versions_per_entity=shapes["versionsPerEntity"],
            first_start=shapes["firstStart"],
            version_length=shapes["versionLength"],
            chain=shapes["chain"],
            last_version=shapes["lastVersion"],
            columns=MappingProxyType(columns),
            reasons=reasons,
        )

    def _history_statistic(
        self,
        value: Any,
        where: str,
        expose_id: str,
        kind: str,
        levels: Optional[Tuple[str, ...]],
        *,
        grouped_by: Optional[str] = None,
    ) -> StatisticEntry:
        """One statistic of a history: of *kind*, over exactly *levels* when they are fixed.

        It is ungrouped, unless *grouped_by* names the column it is grouped by, and a history
        statistic holds no ``groupByLink`` and no ``per``.
        """
        if isinstance(value, Mapping) and "noise" in value:
            raise refuse(f"{where}.noise", _MOVES_AN_ENTITY + " in a history statistic")
        statistic = self.statistic(
            value,
            where,
            kinds=frozenset({kind}),
            grouped_by=None if grouped_by is None else {grouped_by},
            noisy_in=None,
        )
        if levels is not None and statistic.levels != levels:
            raise refuse(f"{where}.levels", f"are exactly {list(levels)}")
        if statistic.group_by != grouped_by:
            raise refuse(where, "is " + ("grouped by " + grouped_by if grouped_by else "ungrouped"))
        if statistic.group_by_link is not None or statistic.per:
            raise refuse(where, "a history statistic holds no groupByLink and no per")
        return statistic

    def _history_columns(
        self,
        found: Mapping[str, Any],
        where: str,
        expose_id: str,
        domains: Mapping[str, Any],
        taken: Set[str],
    ) -> Dict[str, HistoryColumn]:
        """The candidate tracked columns of a history: categorical, and no key or window column."""
        declared = _object(found.get("columns", {}), f"{where}.columns")
        columns: Dict[str, HistoryColumn] = {}
        for name in sorted(declared):
            here = f"{where}.columns.{_name(name, f'{where}.columns')}"
            if domains.get(name) != CATEGORY or name in taken:
                raise refuse(
                    here, "a tracked column is a categorical column that is no key or window column"
                )
            raw = _mapping(declared[name], here, required=("initial", "transitions"))
            columns[name] = HistoryColumn(
                initial=self._history_statistic(
                    raw["initial"], f"{here}.initial", expose_id, "levelHistogram", None
                ),
                transitions=self._history_statistic(
                    raw["transitions"],
                    f"{here}.transitions",
                    expose_id,
                    "levelHistogram",
                    None,
                    grouped_by=name,
                ),
            )
        return columns

    def _history_reasons(
        self, found: Mapping[str, Any], where: str, expose_id: str, tracked: Set[str]
    ) -> Optional[StatisticEntry]:
        """The ``reasons`` of a history: one level per tracked column, and ``several``."""
        if "reasons" not in found:
            return None
        here = f"{where}.reasons"
        if not tracked:
            raise refuse(here, "a history with no tracked column states no reasons")
        reasons = self._history_statistic(found["reasons"], here, expose_id, "levelHistogram", None)
        if not set(reasons.levels) <= tracked | {SEVERAL}:
            raise refuse(f"{here}.levels", f"are tracked columns of this history, and '{SEVERAL}'")
        return reasons

    def _cliques(
        self,
        value: Any,
        where: str,
        expose_id: str,
        categorical: Set[str],
        domains: Mapping[str, str],
    ) -> Tuple[Tuple[CliqueEntry, ...], Tuple[CliqueEntry, ...]]:
        """A table's noisy cliques, each read, entered in the budget and checked for agreement.

        The ``cliqueMarginal/1`` cliques and the ``cliqueMarginal/2`` ones (format 1-4-0) come back
        apart, each in document order, and every clique of both is checked against the others. A
        document of format 1-4-0 holds at most :data:`flux_stats.cliques.MOST_CLIQUES` cliques in a
        table and :data:`flux_stats.cliques.MOST_TABLE_CELLS` cells over them.
        """
        items = _list(value, f"{where}.cliques")
        if not items:
            return (), ()
        accounting = self._require_account(f"{where}.cliques")
        self._in_unit(expose_id, f"{where}.cliques", accounting)
        if self.from_1_4 and len(items) > MOST_CLIQUES:
            check_table_limits(where, len(items), 0)
        first: List[CliqueEntry] = []
        second: List[CliqueEntry] = []
        cells = 0
        for index, item in enumerate(items):
            at = f"{where}.cliques[{index}]"
            if isinstance(item, Mapping) and item.get("definition") == CLIQUE_DEFINITION_2:
                self.require_1_4_0("cliqueMarginal/2", f"{at}.definition")
                clique = read_clique_2(
                    item,
                    at,
                    expose_id=expose_id,
                    index=index,
                    domains=domains,
                    accounting=accounting,
                )
                second.append(clique)
            else:
                clique = read_clique(
                    item,
                    at,
                    expose_id=expose_id,
                    index=index,
                    categorical=categorical,
                    accounting=accounting,
                )
                first.append(clique)
            cells += clique.cell_count
            if self.from_1_4:
                check_table_limits(where, len(items), cells)
        read = sorted(first + second, key=lambda clique: clique.index)
        for clique in read:
            self.parts.append((f"{where}.cliques[{clique.index}]", clique.noise))
        check_consistency(read)
        return tuple(first), tuple(second)

    def _clique_selection(
        self, raw: Mapping[str, Any], where: str, expose_id: str
    ) -> Optional[CliqueSelection]:
        """A table's ``cliqueSelection`` (format 1-4-0), its shares entered in the budget, or ``None``.

        A share is a part of the account in the account's measure, once each: ``selection`` and
        ``domain`` are :class:`flux_stats.noise.PrivacyShare` parts, with no mechanism and no scale.
        """
        if "cliqueSelection" not in raw:
            return None
        at = f"{where}.cliqueSelection"
        self.require_1_4_0("cliqueSelection", at)
        accounting = self._require_account(at)
        self._in_unit(expose_id, at, accounting)
        selection = read_clique_selection(raw["cliqueSelection"], at, accounting=accounting)
        for key, share in (("selection", selection.selection), ("domain", selection.domain)):
            if share is not None:
                self.parts.append((f"{at}.{key}", share))
        return selection

    def _pair(self, value: Any, where: str, ordered: Set[str], expose_id: str) -> PairEntry:
        raw = _mapping(value, where, required=("columns", "statistics"))
        columns = tuple(read_texts(raw["columns"], f"{where}.columns"))
        if len(columns) != 2 or columns[0] == columns[1] or not set(columns) <= ordered:
            raise refuse(f"{where}.columns", "a pair is two different number or timestamp columns")
        statistics = tuple(
            self._pair_statistic(item, f"{where}.statistics[{index}]", expose_id)
            for index, item in enumerate(_list(raw["statistics"], f"{where}.statistics"))
        )
        self._no_repeats(statistics, f"{where}.statistics")
        return PairEntry(columns=(columns[0], columns[1]), statistics=statistics)

    def _pair_statistic(self, value: Any, where: str, expose_id: str) -> StatisticEntry:
        """One statistic of a column pair: a ``correlation/1``, or a rank correlation (format 1-4-0)."""
        named = value.get("definition") if isinstance(value, Mapping) else None
        if isinstance(named, str) and named in RANK_IDENTIFIERS:
            return self._rank_statistic(value, where, expose_id)
        return self.statistic(value, where, kinds=_PAIR_KINDS)

    def _rank_statistic(self, value: Any, where: str, expose_id: str) -> StatisticEntry:
        """A ``kendallTau/1`` or ``spearmanRho/1`` of a column pair (PROFILE-SPEC.md section 5).

        Exact, its one group holds ``k`` and ``S`` or ``A4``. Noisy, which needs a ``rowFloor``, it
        holds the released value, and the share is charged the sensitivity of the definition at that
        floor (:func:`flux_stats.rank_correlation.rank_sensitivity`) and enters the budget once.
        """
        self.require_1_4_0("rankCorrelation", f"{where}.definition")
        raw = _mapping(
            value,
            where,
            required=("definition", "sampleSize", "disclosure", "groups"),
            optional=("rowFloor", "noise"),
        )
        entry = RANK_DEFINITIONS[raw["definition"]]
        floor: Optional[int] = None
        if "rowFloor" in raw:
            self.require_1_4_0("rowFloor", f"{where}.rowFloor")
            floor = read_count(raw["rowFloor"], f"{where}.rowFloor")
            if floor < 2:
                raise refuse(f"{where}.rowFloor", "a row floor is a count of at least 2")
        disclosure = raw["disclosure"]
        if not isinstance(disclosure, str) or disclosure not in self.rules:
            raise refuse(f"{where}.disclosure", "names no rule in disclosureRules")
        noise: Optional[Noise] = None
        if "noise" in raw:
            if floor is None:
                raise refuse(
                    f"{where}.noise",
                    "a noisy rank correlation states a rowFloor, so that its sensitivity does not "
                    "depend on the number of records",
                )
            accounting = self._require_account(f"{where}.noise")
            self._in_unit(expose_id, f"{where}.noise", accounting)
            sensitivity = rank_sensitivity(entry.identifier, floor)
            noise = read_noise(
                raw["noise"],
                f"{where}.noise",
                accounting,
                l1=sensitivity,
                l2_squared=sensitivity * sensitivity,
            )
            if noise.mechanism == "discreteGaussian":
                raise refuse(
                    f"{where}.noise.mechanism",
                    "a rank correlation is not an integer count, so a discrete Gaussian does not apply",
                )
            self.parts.append((where, noise))
        group = self._rank_group(
            raw["groups"], f"{where}.groups", entry, floor, noisy=noise is not None
        )
        return StatisticEntry(
            definition=entry,
            sample_size=_sample_size(raw["sampleSize"], f"{where}.sampleSize"),
            disclosure=disclosure,
            groups=(group,),
            noise=noise,
            cell_variance=noise.variance if noise is not None else None,
            row_floor=floor,
        )

    def _rank_group(
        self,
        value: Any,
        where: str,
        entry: StatisticDefinition,
        floor: Optional[int],
        *,
        noisy: bool,
    ) -> ProfileGroup:
        """The one group of a rank correlation, keyed by the empty string, with its value."""
        items = _list(value, where)
        if len(items) != 1:
            raise refuse(where, "a rank correlation has one group, keyed by the empty string")
        at = f"{where}[0]"
        if noisy:
            raw = _mapping(items[0], at, required=("group", "status", "noisyValue"))
            if raw["status"] != FREE:
                raise refuse(f"{at}.status", "a noisy statistic publishes every group, status F")
            if raw["group"] != "":
                raise refuse(f"{at}.group", "a rank correlation is keyed by the empty string")
            released = read_decimal(raw["noisyValue"], f"{at}.noisyValue")
            clamped = max(Fraction(-1), min(Fraction(1), released))
            return ProfileGroup(key="", status=FREE, noisy=(released,), value=clamped)
        group = self._group(items[0], at, entry, cells=0)
        if group.key != "":
            raise refuse(f"{at}.group", "a rank correlation is keyed by the empty string")
        state = group.statistic
        if state is None:
            return group
        size = state.k
        if size < 2:
            raise refuse(f"{at}.k", "a published rank correlation holds at least 2 records")
        if abs(state.rank_s) > size * (size - 1) // 2:
            raise refuse(f"{at}.S", "lies within plus or minus C(k, 2)")
        if 3 * abs(state.rank_a4) > size * (size * size - 1):
            raise refuse(f"{at}.A4", "lies within plus or minus k (k^2 - 1) / 3")
        return replace(group, value=rank_value(entry.identifier, state, floor))

    def _period(self, value: Any, where: str, domains: Mapping[str, Any]) -> PeriodEntry:
        raw = _mapping(value, where, required=("column", "period", "statistics"))
        column = _name(raw["column"], f"{where}.column")
        if domains.get(column) != TIMESTAMP:
            raise refuse(f"{where}.column", "a period is taken over a timestamp column")
        if raw["period"] != MONTH:
            raise refuse(f"{where}.period", f"version 1 knows one period, {MONTH}")
        numbers = {name for name, domain in domains.items() if domain == NUMBER}
        return PeriodEntry(
            column=column,
            period=MONTH,
            statistics=self.statistics(
                raw["statistics"],
                f"{where}.statistics",
                kinds=_PERIOD_KINDS,
                of=numbers,
                period=True,
            ),
        )

    def relationship(
        self, value: Any, where: str, tables: Mapping[str, TableEntry]
    ) -> RelationshipEntry:
        raw = _mapping(
            value,
            where,
            required=("parent", "child", "statistics"),
            optional=("structural", "dates", "exposure"),
        )
        ends = []
        for side in ("parent", "child"):
            end = _mapping(raw[side], f"{where}.{side}", required=("table", "columns"))
            table = _name(end["table"], f"{where}.{side}.table")
            if table not in tables:
                raise refuse(f"{where}.{side}.table", "names no table of this profile")
            columns = tuple(read_texts(end["columns"], f"{where}.{side}.columns"))
            if not columns or not set(columns) <= set(tables[table].columns):
                raise refuse(f"{where}.{side}.columns", "names columns of its table, at least one")
            ends.append((table, columns))
        if len(ends[0][1]) != len(ends[1][1]):
            raise refuse(where, "a parent and a child key have the same number of columns")
        structural = self._structural(raw, where)
        return RelationshipEntry(
            parent_table=ends[0][0],
            parent_columns=ends[0][1],
            child_table=ends[1][0],
            child_columns=ends[1][1],
            statistics=self.statistics(
                raw["statistics"], f"{where}.statistics", kinds=_RELATIONSHIP_KINDS
            ),
            structural=structural,
            dates=self._relationship_dates(raw, where, tables, ends[0][0], ends[1][0]),
            exposure=self._exposure(raw, where, tables, ends[0][0]),
        )

    def _structural(self, raw: Mapping[str, Any], where: str) -> FrozenSet[str]:
        """The ``structural`` classes of a relationship (format 1-3-0): distinct, at least one."""
        if "structural" not in raw:
            return frozenset()
        here = f"{where}.structural"
        self._require_1_3(here, "'structural'")
        found = _list(raw["structural"], here)
        if not found:
            raise refuse(here, "names at least one class")
        for index, item in enumerate(found):
            if item not in STRUCTURAL_CLASSES:
                raise refuse(f"{here}[{index}]", f"is one of {list(STRUCTURAL_CLASSES)}")
        if len(set(found)) != len(found):
            raise refuse(here, "names each class once")
        return frozenset(found)

    def _exposure(
        self, raw: Mapping[str, Any], where: str, tables: Mapping[str, TableEntry], parent: str
    ) -> Tuple[ExposureEntry, ...]:
        """A relationship's ``exposure`` (format 1-3-0): a parent's start and end columns."""
        if "exposure" not in raw:
            return ()
        self._require_1_3(f"{where}.exposure", "exposure")
        items = _list(raw["exposure"], f"{where}.exposure")
        if len(items) > MAX_EXPOSURES:
            raise refuse(f"{where}.exposure", f"holds at most {MAX_EXPOSURES} entries")
        found = [
            self._exposure_entry(item, f"{where}.exposure[{index}]", tables, parent)
            for index, item in enumerate(items)
        ]
        if len({(each.start, each.end) for each in found}) != len(found):
            raise refuse(f"{where}.exposure", "names one pair twice")
        return tuple(found)

    def _exposure_entry(
        self, item: Any, at: str, tables: Mapping[str, TableEntry], parent: str
    ) -> ExposureEntry:
        """One ``exposure`` entry at *at*: two different timestamp columns of the parent, two counts and a window."""
        entry = _mapping(
            item,
            at,
            required=("start", "children", "activeDays", "window"),
            optional=("end",),
        )
        names = [_name(entry["start"], f"{at}.start")]
        if "end" in entry:
            names.append(_name(entry["end"], f"{at}.end"))
            if names[0] == names[1]:
                raise refuse(at, "an exposure is two different columns")
        for side, name in zip(("start", "end"), names, strict=False):
            held = tables[parent].columns.get(name)
            if held is None or held.domain != TIMESTAMP:
                raise refuse(f"{at}.{side}", "names no timestamp column of its parent table")
        window_start, window_end = _exposure_window(entry["window"], f"{at}.window")
        counts = [
            self.metric("count", entry[key], f"{at}.{key}", domain=TIMESTAMP)
            for key in ("children", "activeDays")
        ]
        return ExposureEntry(
            start=names[0],
            end=names[1] if len(names) > 1 else None,
            children=counts[0],
            active_days=counts[1],
            window_start=window_start,
            window_end=window_end,
        )

    def _relationship_dates(
        self,
        raw: Mapping[str, Any],
        where: str,
        tables: Mapping[str, TableEntry],
        parent: str,
        child: str,
    ) -> Tuple[RelationshipDateEntry, ...]:
        """A relationship's ``dates`` (format 1-3-0): a parent's timestamp column and a child's."""
        if "dates" not in raw:
            return ()
        self._require_1_3(f"{where}.dates", "dates")
        items = _list(raw["dates"], f"{where}.dates")
        _date_pair_bound(len(items), f"{where}.dates")
        found = []
        for index, item in enumerate(items):
            at = f"{where}.dates[{index}]"
            entry = _mapping(item, at, required=("parent", "child", "statistics", "order"))
            names = []
            for side, table in (("parent", parent), ("child", child)):
                name = _name(entry[side], f"{at}.{side}")
                held = tables[table].columns.get(name)
                if held is None or held.domain != TIMESTAMP:
                    raise refuse(f"{at}.{side}", "names no timestamp column of its table")
                names.append(name)
            found.append(
                RelationshipDateEntry(
                    parent=names[0],
                    child=names[1],
                    statistics=self.statistics(
                        entry["statistics"],
                        f"{at}.statistics",
                        kinds=_DATE_GAP_KINDS,
                        domain=NUMBER,
                    ),
                    order=self._date_order(entry["order"], f"{at}.order", None),
                )
            )
        if len({(pair.parent, pair.child) for pair in found}) != len(found):
            raise refuse(f"{where}.dates", "names one pair twice")
        return tuple(found)

    def query(self, value: Any, where: str) -> QueryEntry:
        raw = _mapping(value, where, required=("name", "statistics"))
        statistics = self.statistics(
            raw["statistics"], f"{where}.statistics", kinds=_QUERY_KINDS, query_group=True
        )
        if any(statistic.levels != QUERY_LEVELS for statistic in statistics):
            raise refuse(
                f"{where}.statistics", f"a query's rate counts the levels {list(QUERY_LEVELS)}"
            )
        return QueryEntry(name=_name(raw["name"], f"{where}.name"), statistics=statistics)

    def invariant(self, value: Any, where: str) -> InvariantEntry:
        """A declared fact's share: exactly one ungrouped ``levelHistogram/1`` over false and true."""
        raw = _mapping(value, where, required=("name", "statistics"))
        statistics = self.statistics(raw["statistics"], f"{where}.statistics", kinds=_QUERY_KINDS)
        if len(statistics) != 1 or statistics[0].levels != QUERY_LEVELS:
            raise refuse(
                f"{where}.statistics",
                f"a fact's share is one statistic counting the levels {list(QUERY_LEVELS)}",
            )
        return InvariantEntry(name=_name(raw["name"], f"{where}.name"), statistics=statistics)


def _derived(
    name: str, cliques: Tuple[CliqueEntry, ...], entry: ColumnEntry
) -> Tuple[Tuple[StatisticEntry, ...], Tuple[StatisticEntry, ...]]:
    """The histograms of column *name* that its cliques answer, as the reader forms them.

    Summing a clique onto one column gives that column's ungrouped histogram, and onto two gives a
    histogram grouped by the other (a two-way count table). A category column gives a
    ``levelHistogram/1``. A number or timestamp column of a ``cliqueMarginal/2`` gives a
    ``marginalHistogram/1`` over the clique's bins, grouped by a category column of the clique, or,
    for two binned columns, the first in the clique's order grouped by the bins of the later one
    (``groupByBins``). Each is a post-processing of the noisy counts: the point estimate of the
    summed values (one group of the whole, or one per level or bin of the other column), with the
    variance of a sum of independent cells. A statistic the column already carries in the document
    is not added to its statistics: the one formed is kept apart
    (:attr:`ColumnEntry.clique_statistics`), because the draw of a column in a clique rests on it.
    One pair is formed once, from the first clique that holds it. The result is the statistics to
    add and the ones kept apart.
    """
    from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM

    have = {
        (item.identifier, item.group_by, item.group_by_bins, item.levels, item.bins)
        for item in entry.statistics
        if item.group_by_link is None and not item.per
    }
    formed: List[StatisticEntry] = []
    shadowed: List[StatisticEntry] = []
    seen: Set[Optional[str]] = set()
    for clique in cliques:
        if name not in clique.columns:
            continue
        levels = clique.levels_of(name)
        edges = clique.edges_of(name)
        position = clique.columns.index(name)
        others: List[Optional[str]] = [None]
        for neighbour in clique.columns:
            if neighbour == name:
                continue
            if not clique.binned(neighbour):
                others.append(neighbour)
            elif edges and clique.columns.index(neighbour) > position:
                others.append(neighbour)
        for other in others:
            if other in seen:
                continue
            seen.add(other)
            keep = [name] if other is None else [other, name]
            table = clique.marginal(keep)
            summed = len(clique.noisy) // len(table)
            keys = [""] if other is None else list(clique.levels_of(other))
            groups = []
            for key in keys:
                cells = [table[(level,) if other is None else (key, level)] for level in levels]
                estimate = point_estimate(cells)
                held = {"bin_counts" if edges else "level_counts": estimate.counts}
                groups.append(
                    ProfileGroup(
                        key=key,
                        status=FREE,
                        statistic=GroupStatistic(
                            group=key, k=estimate.records, **held  # type: ignore[arg-type]
                        ),
                        noisy=tuple(cells),
                    )
                )
            by_bins = (
                BinGrouping(column=other, bins=clique.edges_of(other))
                if other is not None and clique.binned(other)
                else None
            )
            group_by = None if by_bins is not None else other
            identifier = (MARGINAL_HISTOGRAM if edges else LEVEL_HISTOGRAM).identifier
            (
                shadowed
                if (identifier, group_by, by_bins, levels if not edges else (), edges) in have
                else formed
            ).append(
                StatisticEntry(
                    definition=MARGINAL_HISTOGRAM if edges else LEVEL_HISTOGRAM,
                    sample_size=SampleSize(
                        minimum=min(g.statistic.k for g in groups if g.statistic)
                    ),
                    disclosure="",
                    groups=tuple(groups),
                    group_by=group_by,
                    levels=() if edges else levels,
                    bins=edges,
                    noise=clique.noise,
                    cell_variance=clique.noise.variance * summed,
                    derived=True,
                    group_by_bins=by_bins,
                )
            )
    return tuple(formed), tuple(shadowed)


def _reference_table(value: Any, where: str, rules: Mapping[str, DisclosureRule]) -> ReferenceTable:
    raw = _mapping(value, where, required=("disclosure", "columns", "rows"))
    disclosure = raw["disclosure"]
    if not isinstance(disclosure, str) or disclosure not in rules:
        raise refuse(f"{where}.disclosure", "names no rule in disclosureRules")
    names = _list(raw["columns"], f"{where}.columns")
    if not 1 <= len(names) <= MAX_REFERENCE_COLUMNS:
        raise refuse(f"{where}.columns", f"holds 1 to {MAX_REFERENCE_COLUMNS} columns")
    columns = tuple(_name(name, f"{where}.columns[{index}]") for index, name in enumerate(names))
    if len(set(columns)) != len(columns):
        raise refuse(f"{where}.columns", "names a column twice")
    items = _list(raw["rows"], f"{where}.rows")
    if len(items) > MAX_REFERENCE_ROWS:
        raise refuse(f"{where}.rows", f"holds at most {MAX_REFERENCE_ROWS} rows")
    if len(items) * len(columns) > MAX_REFERENCE_CELLS:
        raise refuse(where, f"holds at most {MAX_REFERENCE_CELLS} cells")
    rows: List[Tuple[Optional[str], ...]] = []
    for index, item in enumerate(items):
        at = f"{where}.rows[{index}]"
        cells = _list(item, at)
        if len(cells) != len(columns):
            raise refuse(at, "a row holds exactly one cell per column")
        for position, cell in enumerate(cells):
            if cell is not None and not isinstance(cell, str):
                raise refuse(f"{at}[{position}]", "a cell is a string or null")
            if cell is not None and len(cell) > MAX_REFERENCE_CELL_CHARACTERS:
                raise refuse(
                    f"{at}[{position}]",
                    f"a cell holds at most {MAX_REFERENCE_CELL_CHARACTERS} characters",
                )
        rows.append(tuple(cells))
    return ReferenceTable(disclosure=disclosure, columns=columns, rows=tuple(rows))


def _require_format_of_sections(raw: Mapping[str, Any]) -> None:
    """Refuse a section the document's format does not have (``FLX-5036``)."""
    for section in ("invariants", "privacy"):
        if section in raw and raw["_schemaURL"] not in _FROM_1_2_0:
            raise refuse(
                f"profile.{section}",
                "is a section of format 1-2-0, and this document names an earlier format",
            )
    if "references" in raw and raw["_schemaURL"] not in _FROM_1_3_0:
        raise refuse(
            "profile.references",
            "is a section of format 1-3-0, and this document names an earlier format",
        )


def _read_references(raw: Mapping[str, Any], rules: Mapping[str, Any]) -> Dict[str, ReferenceTable]:
    """The reference tables a profile releases whole, by name, at most ``MAX_REFERENCE_TABLES``."""
    declared = _object(raw.get("references", {}), "profile.references")
    if len(declared) > MAX_REFERENCE_TABLES:
        raise refuse("profile.references", f"holds at most {MAX_REFERENCE_TABLES} tables")
    return {
        _name(name, "profile.references"): _reference_table(
            declared[name], f"profile.references.{name}", rules
        )
        for name in sorted(declared)
    }


def parse_profile(document: Any) -> Profile:
    """Read a parsed profile document whole, or refuse it (``FLX-5036``)."""
    _require_nfc(document, "profile")
    raw = _mapping(
        document,
        "profile",
        required=("_producer", "_schemaURL", "encoding", "disclosureRules", "tables"),
        optional=("relationships", "queries", "invariants", "privacy", "references"),
    )
    producer = raw["_producer"]
    if not isinstance(producer, str) or ":" not in producer:
        raise refuse("profile._producer", "the producer is a URI")
    if raw["_schemaURL"] not in _READ_FORMATS:
        raise refuse("profile._schemaURL", "this reader reads " + ", ".join(_READ_FORMATS))
    _require_format_of_sections(raw)
    accounting = read_accounting(raw["privacy"], "profile.privacy") if "privacy" in raw else None
    encoding = _mapping(
        raw["encoding"], "profile.encoding", required=("fractionalBits", "rounding")
    )
    bits = read_count(encoding["fractionalBits"], "profile.encoding.fractionalBits")
    if bits > MAX_FRACTIONAL_BITS:
        raise refuse("profile.encoding.fractionalBits", f"is at most {MAX_FRACTIONAL_BITS}")
    if encoding["rounding"] != "floor":
        raise refuse("profile.encoding.rounding", "the grid rounds toward minus infinity, 'floor'")
    rules = {
        _name(name, "profile.disclosureRules"): _disclosure_rule(
            rule, f"profile.disclosureRules.{name}"
        )
        for name, rule in _object(raw["disclosureRules"], "profile.disclosureRules").items()
    }
    reader = _Reader(
        MappingProxyType(rules),
        first_format=raw["_schemaURL"] == SCHEMA_URL,
        accounting=accounting,
        from_1_3=raw["_schemaURL"] in _FROM_1_3_0,
        from_1_4=raw["_schemaURL"] == SCHEMA_URL_1_4_0,
    )
    declared = _object(raw["tables"], "profile.tables")
    tables = {
        _name(name, "profile.tables"): reader.table(name, declared[name], f"profile.tables.{name}")
        for name in sorted(declared)
    }
    relationships = tuple(
        reader.relationship(item, f"profile.relationships[{index}]", tables)
        for index, item in enumerate(_list(raw.get("relationships", []), "profile.relationships"))
    )
    queries = tuple(
        reader.query(item, f"profile.queries[{index}]")
        for index, item in enumerate(_list(raw.get("queries", []), "profile.queries"))
    )
    if len({query.name for query in queries}) != len(queries):
        raise refuse("profile.queries", "names one query twice")
    invariants = tuple(
        reader.invariant(item, f"profile.invariants[{index}]")
        for index, item in enumerate(_list(raw.get("invariants", []), "profile.invariants"))
    )
    if len({entry.name for entry in invariants}) != len(invariants):
        raise refuse("profile.invariants", "names one fact twice")
    references = _read_references(raw, rules)
    reader.check_links(tables, references)
    if accounting is not None:
        check_budget(accounting, reader.parts)
    return Profile(
        producer=producer,
        fractional_bits=bits,
        rounding="floor",
        disclosure_rules=MappingProxyType(rules),
        tables=MappingProxyType(tables),
        relationships=relationships,
        queries=queries,
        invariants=invariants,
        accounting=accounting,
        references=MappingProxyType(references),
        parts=tuple(reader.parts),
    )
