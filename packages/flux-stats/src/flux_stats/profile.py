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
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

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
    "CONF_STATUS",
    "ColumnEntry",
    "DisclosureRule",
    "FREE",
    "MetricEntry",
    "PairEntry",
    "PeriodEntry",
    "Profile",
    "ProfileGroup",
    "QueryEntry",
    "RelationshipEntry",
    "SCHEMA_URL",
    "SCHEMA_URL_1_1_0",
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

    @property
    def published(self) -> bool:
        """Whether the group's quantities were published at all."""
        return self.status == FREE

    @property
    def suppressed_cells(self) -> Tuple[int, ...]:
        """The positions of the cells whose counts were not published."""
        return tuple(index for index, status in enumerate(self.cell_status) if status != FREE)


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


@dataclass(frozen=True)
class PairEntry:
    """Statistics over two columns of one table, in the order they are named."""

    columns: Tuple[str, str]
    statistics: Tuple[StatisticEntry, ...]


@dataclass(frozen=True)
class PeriodEntry:
    """Counts and sums per period: one group per month of a timestamp column."""

    column: str
    period: str
    statistics: Tuple[StatisticEntry, ...]


@dataclass(frozen=True)
class TableEntry:
    """What a profile says about one table, keyed by its FLUID ``exposeId``."""

    expose_id: str
    row_count: Optional[int]
    columns: Mapping[str, ColumnEntry]
    pairs: Tuple[PairEntry, ...]
    periods: Tuple[PeriodEntry, ...]


@dataclass(frozen=True)
class RelationshipEntry:
    """Children per parent: statistics over the number of child rows each parent has."""

    parent_table: str
    parent_columns: Tuple[str, ...]
    child_table: str
    child_columns: Tuple[str, ...]
    statistics: Tuple[StatisticEntry, ...]


@dataclass(frozen=True)
class QueryEntry:
    """A query's rate: how many records it matches, over a two-level count."""

    name: str
    statistics: Tuple[StatisticEntry, ...]


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


class _Reader:
    """One profile's reading state: the rules a statistic may name."""

    def __init__(self, rules: Mapping[str, DisclosureRule], *, first_format: bool) -> None:
        self.rules = rules
        #: Whether the document names format ``1-0-0``, which holds none of
        #: the statistics :data:`_ADDED_IN_1_1_0` names.
        self.first_format = first_format

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
    ) -> StatisticEntry:
        """Read one statistic of a section that allows *kinds*."""
        shape_keys = ("bins", "levels", "level")
        optional: Tuple[str, ...] = shape_keys
        if grouped_by is not None:
            optional += ("groupBy",)
        if of is not None:
            optional += ("of",)
        raw = _mapping(
            value,
            where,
            required=("definition", "sampleSize", "disclosure", "groups"),
            optional=optional,
        )
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
        bins = self._bins(raw, where, entry)
        levels = self._levels(raw, where, entry)
        if entry.kind != "quantile" and "level" in raw:
            raise refuse(f"{where}.level", f"{entry.identifier} declares no level")
        if entry.kind == "quantile" and "level" not in raw:
            raise refuse(where, "a quantile declares its level")
        level = _unit_interval(raw["level"], f"{where}.level") if entry.kind == "quantile" else None
        group_by = None
        if "groupBy" in raw:
            assert grouped_by is not None
            group_by = _name(raw["groupBy"], f"{where}.groupBy")
            if group_by not in grouped_by:
                raise refuse(f"{where}.groupBy", "names no categorical column of this table")
        measured = None
        if of is not None:
            if "of" not in raw:
                raise refuse(where, "a period statistic names the column it sums with 'of'")
            measured = _name(raw["of"], f"{where}.of")
            if measured not in of:
                raise refuse(f"{where}.of", "names no number column of this table")
        disclosure = raw["disclosure"]
        if not isinstance(disclosure, str) or disclosure not in self.rules:
            raise refuse(f"{where}.disclosure", "names no rule in disclosureRules")
        cells = (
            (len(bins) - 1) if entry.kind == "marginalHistogram" or entry.binned else len(levels)
        )
        groups = self._groups(
            raw["groups"],
            f"{where}.groups",
            entry,
            cells=cells,
            grouped=group_by is not None,
            period=period,
        )
        return StatisticEntry(
            definition=entry,
            sample_size=_sample_size(raw["sampleSize"], f"{where}.sampleSize"),
            disclosure=disclosure,
            groups=groups,
            group_by=group_by,
            of=measured,
            bins=bins,
            levels=levels,
            level=level,
        )

    def statistics(self, value: Any, where: str, **section: Any) -> Tuple[StatisticEntry, ...]:
        """Read a section's list of statistics; no two may answer one question."""
        read = tuple(
            self.statistic(item, f"{where}[{index}]", **section)
            for index, item in enumerate(_list(value, where))
        )
        seen: Set[Tuple[Any, ...]] = set()
        for index, statistic in enumerate(read):
            key = (
                statistic.identifier,
                statistic.group_by,
                statistic.of,
                statistic.bins,
                statistic.levels,
                statistic.level,
            )
            if key in seen:
                raise refuse(f"{where}[{index}]", "repeats a statistic already in this list")
            seen.add(key)
        return read

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
    ) -> Tuple[ProfileGroup, ...]:
        items = _list(value, where)
        if not items:
            raise refuse(where, "a statistic has at least one group")
        groups = tuple(
            self._group(item, f"{where}[{index}]", entry, cells=cells)
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

    def column(self, name: str, value: Any, where: str, categorical: Set[str]) -> ColumnEntry:
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
            value, where, required=("columns",), optional=("rowCount", "columnPairs", "periods")
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
        columns = {
            name: self.column(name, declared[name], f"{where}.columns.{name}", categorical)
            for name in sorted(names)
        }
        pairs = tuple(
            self._pair(item, f"{where}.columnPairs[{index}]", ordered)
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
        return TableEntry(
            expose_id=expose_id,
            row_count=(
                read_count(raw["rowCount"], f"{where}.rowCount") if "rowCount" in raw else None
            ),
            columns=MappingProxyType(columns),
            pairs=pairs,
            periods=periods,
        )

    def _pair(self, value: Any, where: str, ordered: Set[str]) -> PairEntry:
        raw = _mapping(value, where, required=("columns", "statistics"))
        columns = tuple(read_texts(raw["columns"], f"{where}.columns"))
        if len(columns) != 2 or columns[0] == columns[1] or not set(columns) <= ordered:
            raise refuse(f"{where}.columns", "a pair is two different number or timestamp columns")
        return PairEntry(
            columns=(columns[0], columns[1]),
            statistics=self.statistics(raw["statistics"], f"{where}.statistics", kinds=_PAIR_KINDS),
        )

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
        raw = _mapping(value, where, required=("parent", "child", "statistics"))
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
        return RelationshipEntry(
            parent_table=ends[0][0],
            parent_columns=ends[0][1],
            child_table=ends[1][0],
            child_columns=ends[1][1],
            statistics=self.statistics(
                raw["statistics"], f"{where}.statistics", kinds=_RELATIONSHIP_KINDS
            ),
        )

    def query(self, value: Any, where: str) -> QueryEntry:
        raw = _mapping(value, where, required=("name", "statistics"))
        statistics = self.statistics(raw["statistics"], f"{where}.statistics", kinds=_QUERY_KINDS)
        if any(statistic.levels != QUERY_LEVELS for statistic in statistics):
            raise refuse(
                f"{where}.statistics", f"a query's rate counts the levels {list(QUERY_LEVELS)}"
            )
        return QueryEntry(name=_name(raw["name"], f"{where}.name"), statistics=statistics)


def parse_profile(document: Any) -> Profile:
    """Read a parsed profile document whole, or refuse it (``FLX-5036``)."""
    _require_nfc(document, "profile")
    raw = _mapping(
        document,
        "profile",
        required=("_producer", "_schemaURL", "encoding", "disclosureRules", "tables"),
        optional=("relationships", "queries"),
    )
    producer = raw["_producer"]
    if not isinstance(producer, str) or ":" not in producer:
        raise refuse("profile._producer", "the producer is a URI")
    if raw["_schemaURL"] not in (SCHEMA_URL, SCHEMA_URL_1_1_0):
        raise refuse("profile._schemaURL", f"this engine reads {SCHEMA_URL} and {SCHEMA_URL_1_1_0}")
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
    reader = _Reader(MappingProxyType(rules), first_format=raw["_schemaURL"] == SCHEMA_URL)
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
    return Profile(
        producer=producer,
        fractional_bits=bits,
        rounding="floor",
        disclosure_rules=MappingProxyType(rules),
        tables=MappingProxyType(tables),
        relationships=relationships,
        queries=queries,
    )
