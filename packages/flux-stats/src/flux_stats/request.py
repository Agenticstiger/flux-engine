# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The profile request: what a profiler must measure for a bundle (docs/spec/PROFILE-REQUEST-SPEC.md).

``fluxsim init``, given a contract and no profile, writes a request. A profiler
reads it, measures exactly the statistics, column metrics and fact shares it
lists where the data lives, and writes a profile (docs/spec/PROFILE-SPEC.md).
The request names no record and no value of the data: it lists the shape of the
questions, never the answers.

A request is a YAML text, read exactly (:mod:`flux_stats.exact_yaml`): no float,
no timestamp, at most one document, within bounds that are tighter than the
bundle's own. It is read whole or refused with ``FLX-5038``, naming where the
problem is and never quoting a value. Every mapping carries exactly the keys its
format names, so a key nothing reads is refused rather than ignored.

Borrowed, as a pattern and not a dependency: Deequ's ``AnalysisRunner`` takes a
list of analyzers and runs them in one pass, so the list of what to measure is
data of its own, separate from the data it is measured on.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_stats.column_metrics import CATEGORY, DOMAINS, NUMBER, TIMESTAMP
from flux_stats.errors import FluxError, register
from flux_stats.exact_yaml import ReadBudget, ReadLimits, load_documents
from flux_stats.fixedpoint import MAX_FRACTIONAL_BITS
from flux_stats.rank_correlation import RANK_IDENTIFIERS
from flux_stats.registry import definition, metrics
from flux_stats.spelling import MAX_SAFE_COUNT

__all__ = [
    "FLX_5038",
    "MOST_FACTS",
    "MOST_NAME_CHARACTERS",
    "MOST_RELATIONSHIPS",
    "MOST_STATISTICS_PER_ENTRY",
    "MOST_CLIQUE_CANDIDATES",
    "MOST_COLUMNS_PER_TABLE",
    "MOST_CALENDAR_COLUMNS",
    "MOST_DATE_PAIRS",
    "MOST_EXPOSURES",
    "MOST_QUERIES",
    "MOST_QUERY_BYTES",
    "MOST_RANK_COLUMNS",
    "MOST_TABLES",
    "MOST_WORKLOAD_COLUMNS",
    "MOST_WORKLOAD_SETS",
    "MOST_WORKLOAD_WEIGHT",
    "REQUEST_HEADER",
    "REQUEST_LIMITS",
    "REQUEST_SCHEMA_URL",
    "REQUEST_SCHEMA_URL_1_1_0",
    "MOST_RELATIONSHIP_GROUPS",
    "MOST_RELATIONSHIP_LINKS",
    "REQUEST_SCHEMA_URL_1_2_0",
    "RequestLinked",
    "ProfileRequest",
    "RequestColumn",
    "RequestDatePair",
    "RequestDateLink",
    "RequestEnd",
    "RequestExposure",
    "RequestFact",
    "RequestQuery",
    "RequestRankCorrelation",
    "RequestRelationship",
    "RequestTable",
    "RequestUnit",
    "RequestWorkload",
    "check_request",
    "load_request_text",
    "parse_request",
    "render_request",
]

FLX_5038 = register(
    "FLX-5038",
    "profile request refused",
    "A profile request is read whole or not at all, and this one does not have the "
    "shape docs/spec/PROFILE-REQUEST-SPEC.md gives a request: a key the format does "
    "not define, a name that is not a plain NFC string, a statistic or metric that is "
    "not registered or does not apply to its column's domain, a number written as "
    "anything but a JSON integer, a list past its bound, a consumer query or workload entry "
    "that breaks its rules or sits in a document that names format 1-0-0, or YAML that the exact "
    "reader refuses. The message names where in the document the problem is. `fluxsim init` "
    "applies the same rules before it writes a request, and refuses a contract whose table, "
    "column or fact the reader would refuse, naming it: rename it in the contract. "
    "Regenerate the request with `fluxsim init`, or correct the named entry.",
)

#: Format ``1-0-0`` of the request, named in its ``_schemaURL`` as a profile names its own.
REQUEST_SCHEMA_URL = (
    "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-0-0/FluxProfileRequest.json"
)

#: Format ``1-1-0`` of the request: the one new version of the request in its release. It adds
#: ``datePairs`` and ``calendar`` to a table and ``dates`` and ``exposure`` to a relationship
#: (PROFILE-REQUEST-SPEC.md sections 2 and 3) and the consumer additions (marked below), and a
#: document that uses an addition names this URL. A request that asks for none of them is written
#: as ``1-0-0``, as before. The reader reads ``1-0-0`` and ``1-1-0``.
REQUEST_SCHEMA_URL_1_1_0 = (
    "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-1-0/FluxProfileRequest.json"
)

#: Format ``1-2-0`` of the request. It adds ``rankCorrelations`` and ``cliqueColumns`` to a table
#: (:data:`_ADDITIONS_OF_1_2_0`), and a document that uses one names this URL. A request that asks
#: for none of them keeps the format it named before and its bytes. The reader reads ``1-0-0``,
#: ``1-1-0`` and ``1-2-0``; ``1-2-0`` holds everything ``1-1-0`` does.
REQUEST_SCHEMA_URL_1_2_0 = (
    "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-2-0/FluxProfileRequest.json"
)

#: The bounds a request is held to. A request lists names and identifiers, so
#: every one is far below what a contract of a real data product holds, and far
#: below a bundle's own read bounds: the file may come from anyone.
MOST_TABLES = 1024
MOST_COLUMNS_PER_TABLE = 4096
MOST_RELATIONSHIPS = 4096
MOST_FACTS = 4096
MOST_STATISTICS_PER_ENTRY = 32
#: The most date pairs a table's ``datePairs`` or a relationship's ``dates`` asks for (format 1-1-0).
MOST_DATE_PAIRS = 1024
#: The most columns a table's ``calendar`` and the most pairs a relationship's ``exposure`` asks
#: for (format 1-1-0).
MOST_CALENDAR_COLUMNS = 1024
MOST_EXPOSURES = 1024
#: The most columns one ``rankCorrelations`` entry names, and the most columns a table's
#: ``cliqueColumns`` lists (format 1-2-0).
MOST_RANK_COLUMNS = 64
MOST_CLIQUE_CANDIDATES = 1024
#: The most parent columns a relationship's ``groupBy`` lists, and the most column pairs its ``linked``
#: lists (format 1-2-0).
MOST_RELATIONSHIP_GROUPS = 64
MOST_RELATIONSHIP_LINKS = 1024
MOST_NAME_CHARACTERS = 512

#: What the exact reader may write out of a request: 200 000 values, nested 12 deep,
#: 8 MiB of characters, no merge keys to speak of. An alias is a way to make a short
#: text stand for a long one, and a request has no use for one.
REQUEST_LIMITS = ReadLimits(
    values=200_000, depth=12, merge_pairs=0, merged=0, characters=8 * 1024 * 1024
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_FRACTION_BITS_DEFAULT = 16


def _refuse(where: str, problem: str) -> FluxError:
    return FluxError(FLX_5038, f"{where}: {problem}")


@dataclass(frozen=True)
class RequestColumn:
    """One column's requested metrics and statistics, by name and registered identifier."""

    name: str
    domain: str
    metrics: Tuple[str, ...] = ()
    statistics: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestDatePair:
    """Two timestamp columns of one table whose days apart are asked (format 1-1-0)."""

    start: str
    end: str


@dataclass(frozen=True)
class RequestDateLink:
    """A timestamp column of a relationship's parent and one of its child (format 1-1-0)."""

    parent: str
    child: str


@dataclass(frozen=True)
class RequestExposure:
    """A parent's start column, and its end column when it has one, whose exposure is asked.

    Both are timestamp columns of the relationship's parent table (format 1-1-0).
    """

    start: str
    end: Optional[str] = None


@dataclass(frozen=True)
class RequestRankCorrelation:
    """A rank correlation asked over every pair of the listed columns of one table (format 1-2-0).

    ``definition`` is ``kendallTau/1`` or ``spearmanRho/1``; ``columns`` are two to
    :data:`MOST_RANK_COLUMNS` distinct number or timestamp columns of the table.
    """

    definition: str
    columns: Tuple[str, ...]


@dataclass(frozen=True)
class RequestLinked:
    """A column of a relationship's child table grouped by a column of its parent table (format 1-2-0)."""

    column: str
    parent_column: str


@dataclass(frozen=True)
class RequestTable:
    """One table (a FLUID ``exposeId``): whether its row count is asked, and its columns.

    ``date_pairs`` are the pairs of its timestamp columns whose days apart the profile states, and
    ``calendar`` the timestamp columns it counts by month, day of week and hour of week, in the
    order asked (format 1-1-0). ``rank_correlations`` are the rank correlations asked over the
    pairs of its number and timestamp columns, and ``clique_columns`` the columns a profiler may
    join into cliques (format 1-2-0).
    """

    expose_id: str
    row_count: bool
    columns: Tuple[RequestColumn, ...]
    date_pairs: Tuple[RequestDatePair, ...] = ()
    calendar: Tuple[str, ...] = ()
    #: Format 1-1-0 (consumer additions): the column sets consumers' queries read together.
    workload: Tuple["RequestWorkload", ...] = ()
    #: Format 1-2-0: the rank correlations asked, one entry per definition.
    rank_correlations: Tuple[RequestRankCorrelation, ...] = ()
    #: Format 1-2-0: the columns a profiler may join into cliques, each once.
    clique_columns: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RequestEnd:
    """One side of a relationship: a table and its key columns."""

    table: str
    columns: Tuple[str, ...]


@dataclass(frozen=True)
class RequestRelationship:
    """Children per parent: the statistics asked over the number of children each parent has."""

    parent: RequestEnd
    child: RequestEnd
    statistics: Tuple[str, ...]
    #: The pairs of a parent's and a child's timestamp columns whose days apart are asked
    #: (format 1-1-0).
    dates: Tuple[RequestDateLink, ...] = ()
    #: The parent's lifetime columns whose exposure is asked (format 1-1-0).
    exposure: Tuple[RequestExposure, ...] = ()
    #: The columns of the parent table the relationship's statistics are grouped by, each once
    #: (format 1-2-0).
    group_by: Tuple[str, ...] = ()
    #: The child columns whose statistics are grouped by a parent column through this relationship,
    #: each pair once (format 1-2-0).
    linked: Tuple[RequestLinked, ...] = ()


@dataclass(frozen=True)
class RequestFact:
    """A fact share: the share of one table's rows that satisfy one rule.

    ``name`` is the entry a profile's ``invariants`` section carries it under.
    """

    name: str
    table: str
    rule: str


@dataclass(frozen=True)
class ProfileRequest:
    """A whole request, read and checked."""

    contract_digest: Optional[str]
    fractional_bits: int
    tables: Tuple[RequestTable, ...]
    relationships: Tuple[RequestRelationship, ...] = ()
    facts: Tuple[RequestFact, ...] = ()
    #: Format 1-1-0 (consumer additions): the consumers' queries a profiler answers.
    queries: Tuple["RequestQuery", ...] = ()

    def table(self, expose_id: str) -> Optional[RequestTable]:
        """One table's entry, or ``None``."""
        return next((entry for entry in self.tables if entry.expose_id == expose_id), None)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _mapping(
    value: Any, where: str, *, required: Sequence[str] = (), optional: Sequence[str] = ()
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _refuse(where, f"expected a mapping, got {type(value).__name__}")
    keys = {key for key in value}
    if not all(isinstance(key, str) for key in keys):
        raise _refuse(where, "a key is not a string")
    unknown = sorted(keys - set(required) - set(optional))
    if unknown:
        raise _refuse(where, f"unknown key {unknown[0]!r}")
    missing = sorted(set(required) - keys)
    if missing:
        raise _refuse(where, f"missing key {missing[0]!r}")
    return value


def _list(value: Any, where: str, *, most: int) -> List[Any]:
    if not isinstance(value, list):
        raise _refuse(where, f"expected a list, got {type(value).__name__}")
    if len(value) > most:
        raise _refuse(where, f"holds more than {most} entries")
    return value


def _name(value: Any, where: str) -> str:
    """A name: a non-empty NFC string with no control character, at most 512 characters."""
    if not isinstance(value, str):
        raise _refuse(where, f"expected a string, got {type(value).__name__}")
    if not value or len(value) > MOST_NAME_CHARACTERS:
        raise _refuse(where, f"a name is 1 to {MOST_NAME_CHARACTERS} characters")
    if not unicodedata.is_normalized("NFC", value):
        raise _refuse(where, "a name is written in Unicode Normalization Form C")
    if any(unicodedata.category(character)[0] in "CZ" and character != " " for character in value):
        raise _refuse(where, "a name holds no control, format or separator character")
    return value


def _names(value: Any, where: str, *, most: int) -> Tuple[str, ...]:
    return tuple(
        _name(item, f"{where}[{index}]")
        for index, item in enumerate(_list(value, where, most=most))
    )


def _distinct(names: Sequence[str], where: str) -> None:
    if len(set(names)) != len(names):
        raise _refuse(where, "names one entry twice")


def _statistics(value: Any, where: str, domain: Optional[str]) -> Tuple[str, ...]:
    """Registered statistic identifiers; for a column, those that read its domain."""
    found = _names(value, where, most=MOST_STATISTICS_PER_ENTRY)
    _distinct(found, where)
    for index, identifier in enumerate(found):
        try:
            entry = definition(identifier)
        except KeyError:
            raise _refuse(f"{where}[{index}]", "names no registered statistic") from None
        if domain is not None:
            categorical = entry.kind in ("levelHistogram", "support")
            if entry.kind == "correlation" or categorical != (domain == CATEGORY):
                raise _refuse(f"{where}[{index}]", f"does not read a {domain} column")
    return found


def _metric_names(value: Any, where: str, domain: str) -> Tuple[str, ...]:
    found = _names(value, where, most=MOST_STATISTICS_PER_ENTRY)
    _distinct(found, where)
    known = {entry.name: entry for entry in metrics()}
    for index, name in enumerate(found):
        entry = known.get(name)
        if entry is None:
            raise _refuse(f"{where}[{index}]", "names no registered column metric")
        if domain not in entry.domains:
            raise _refuse(f"{where}[{index}]", f"does not apply to a {domain} column")
    return found


def _column(name: str, value: Any, where: str) -> RequestColumn:
    raw = _mapping(value, where, required=("domain",), optional=("metrics", "statistics"))
    domain = raw["domain"]
    if domain not in DOMAINS:
        raise _refuse(f"{where}.domain", f"is one of {list(DOMAINS)}")
    return RequestColumn(
        name=name,
        domain=domain,
        metrics=_metric_names(raw.get("metrics", []), f"{where}.metrics", domain),
        statistics=_statistics(raw.get("statistics", []), f"{where}.statistics", domain),
    )


def _table(
    expose_id: str,
    value: Any,
    where: str,
    *,
    additions: bool = True,
    from_1_2_0: bool = True,
) -> RequestTable:
    raw = _mapping(
        value,
        where,
        required=("columns",),
        optional=(
            ("rowCount", "workload", "datePairs", "calendar")
            if additions
            else ("rowCount", "workload")
        )
        + _keys_of_1_2_0("table"),
    )
    _needs_1_2_0(from_1_2_0, raw, where, "table")
    row_count = raw.get("rowCount", False)
    if not isinstance(row_count, bool):
        raise _refuse(f"{where}.rowCount", "is true or false")
    declared = _mapping_of_names(raw["columns"], f"{where}.columns", MOST_COLUMNS_PER_TABLE)
    columns = tuple(
        _column(name, declared[name], f"{where}.columns.{name}") for name in sorted(declared)
    )
    pairs = tuple(
        _date_pair(item, f"{where}.datePairs[{index}]", columns)
        for index, item in enumerate(
            _list(raw.get("datePairs", []), f"{where}.datePairs", most=MOST_DATE_PAIRS)
        )
    )
    _distinct([f"{pair.start}\n{pair.end}" for pair in pairs], f"{where}.datePairs")
    calendar = _names(raw.get("calendar", []), f"{where}.calendar", most=MOST_CALENDAR_COLUMNS)
    for index, name in enumerate(calendar):
        _timestamp(name, columns, f"{where}.calendar[{index}]")
    _distinct(calendar, f"{where}.calendar")
    workload: Tuple[RequestWorkload, ...] = ()
    if "workload" in raw:
        _needs_1_1_0(additions, f"{where}.workload")
        workload = _workload(raw["workload"], f"{where}.workload", columns)
    return RequestTable(
        expose_id=expose_id,
        row_count=row_count,
        columns=columns,
        date_pairs=pairs,
        calendar=calendar,
        workload=workload,
        rank_correlations=_rank_correlations(
            raw.get("rankCorrelations", []), f"{where}.rankCorrelations", columns
        ),
        clique_columns=_clique_columns(
            raw.get("cliqueColumns", []), f"{where}.cliqueColumns", columns
        ),
    )


def _timestamp(name: str, columns: Sequence[RequestColumn], where: str) -> None:
    held = next((column for column in columns if column.name == name), None)
    if held is None or held.domain != TIMESTAMP:
        raise _refuse(where, "names no timestamp column of its table")


def _date_pair(value: Any, where: str, columns: Sequence[RequestColumn]) -> RequestDatePair:
    raw = _mapping(value, where, required=("start", "end"))
    start, end = _name(raw["start"], f"{where}.start"), _name(raw["end"], f"{where}.end")
    if start == end:
        raise _refuse(where, "a date pair is two different columns")
    _timestamp(start, columns, f"{where}.start")
    _timestamp(end, columns, f"{where}.end")
    return RequestDatePair(start=start, end=end)


def _date_link(
    value: Any, where: str, parent: RequestTable, child: RequestTable
) -> RequestDateLink:
    raw = _mapping(value, where, required=("parent", "child"))
    link = RequestDateLink(
        parent=_name(raw["parent"], f"{where}.parent"), child=_name(raw["child"], f"{where}.child")
    )
    _timestamp(link.parent, parent.columns, f"{where}.parent")
    _timestamp(link.child, child.columns, f"{where}.child")
    return link


def _exposure(value: Any, where: str, parent: RequestTable) -> RequestExposure:
    raw = _mapping(value, where, required=("start",), optional=("end",))
    start = _name(raw["start"], f"{where}.start")
    _timestamp(start, parent.columns, f"{where}.start")
    end: Optional[str] = None
    if "end" in raw:
        end = _name(raw["end"], f"{where}.end")
        if end == start:
            raise _refuse(where, "an exposure is two different columns")
        _timestamp(end, parent.columns, f"{where}.end")
    return RequestExposure(start=start, end=end)


def _mapping_of_names(value: Any, where: str, most: int) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _refuse(where, f"expected a mapping, got {type(value).__name__}")
    if len(value) > most:
        raise _refuse(where, f"holds more than {most} entries")
    for key in value:
        _name(key, f"{where} key")
    return value


def _end(value: Any, where: str, tables: Mapping[str, RequestTable]) -> RequestEnd:
    raw = _mapping(value, where, required=("table", "columns"))
    table = _name(raw["table"], f"{where}.table")
    if table not in tables:
        raise _refuse(f"{where}.table", "names no table of this request")
    columns = _names(raw["columns"], f"{where}.columns", most=MOST_COLUMNS_PER_TABLE)
    held = {column.name for column in tables[table].columns}
    if not columns or not set(columns) <= held:
        raise _refuse(f"{where}.columns", "names columns of its table, at least one")
    return RequestEnd(table=table, columns=columns)


def _relationship(
    value: Any,
    where: str,
    tables: Mapping[str, RequestTable],
    *,
    dated: bool,
    from_1_2_0: bool = True,
) -> RequestRelationship:
    raw = _mapping(
        value,
        where,
        required=("parent", "child", "statistics"),
        optional=(("dates", "exposure") if dated else ()) + _keys_of_1_2_0("relationship"),
    )
    _needs_1_2_0(from_1_2_0, raw, where, "relationship")
    parent = _end(raw["parent"], f"{where}.parent", tables)
    child = _end(raw["child"], f"{where}.child", tables)
    if len(parent.columns) != len(child.columns):
        raise _refuse(where, "a parent and a child key have the same number of columns")
    statistics = _statistics(raw["statistics"], f"{where}.statistics", NUMBER)
    links = tuple(
        _date_link(item, f"{where}.dates[{index}]", tables[parent.table], tables[child.table])
        for index, item in enumerate(
            _list(raw.get("dates", []), f"{where}.dates", most=MOST_DATE_PAIRS)
        )
    )
    _distinct([f"{link.parent}\n{link.child}" for link in links], f"{where}.dates")
    exposures = tuple(
        _exposure(item, f"{where}.exposure[{index}]", tables[parent.table])
        for index, item in enumerate(
            _list(raw.get("exposure", []), f"{where}.exposure", most=MOST_EXPOSURES)
        )
    )
    _distinct([f"{item.start}\n{item.end}" for item in exposures], f"{where}.exposure")
    return RequestRelationship(
        parent=parent,
        child=child,
        statistics=statistics,
        dates=links,
        exposure=exposures,
        group_by=_group_by(raw.get("groupBy", []), f"{where}.groupBy", tables[parent.table]),
        linked=_linked(
            raw.get("linked", []), f"{where}.linked", tables[parent.table], tables[child.table]
        ),
    )


def _fact(value: Any, where: str, tables: Mapping[str, RequestTable]) -> RequestFact:
    raw = _mapping(value, where, required=("name", "table", "rule"))
    table = _name(raw["table"], f"{where}.table")
    if table not in tables:
        raise _refuse(f"{where}.table", "names no table of this request")
    return RequestFact(
        name=_name(raw["name"], f"{where}.name"),
        table=table,
        rule=_name(raw["rule"], f"{where}.rule"),
    )


def parse_request(document: Any) -> ProfileRequest:
    """Read a parsed request document whole, or refuse it (``FLX-5038``)."""
    raw = _mapping(
        document,
        "request",
        required=("_schemaURL", "tables"),
        optional=("contract", "encoding", "relationships", "facts", "queries"),
    )
    if raw["_schemaURL"] not in _READ_FORMATS:
        raise _refuse(
            "request._schemaURL",
            "this reader reads " + ", ".join(_READ_FORMATS),
        )
    from_1_2_0 = raw["_schemaURL"] == REQUEST_SCHEMA_URL_1_2_0
    dated = raw["_schemaURL"] in (REQUEST_SCHEMA_URL_1_1_0, REQUEST_SCHEMA_URL_1_2_0)
    additions = dated
    digest: Optional[str] = None
    if "contract" in raw:
        block = _mapping(raw["contract"], "request.contract", required=("digest",))
        digest = block["digest"]
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise _refuse("request.contract.digest", "is sha256: and 64 lowercase hex digits")
    bits = _FRACTION_BITS_DEFAULT
    if "encoding" in raw:
        encoding = _mapping(
            raw["encoding"], "request.encoding", required=("fractionalBits", "rounding")
        )
        bits = encoding["fractionalBits"]
        if isinstance(bits, bool) or not isinstance(bits, int) or not 0 <= bits <= MAX_SAFE_COUNT:
            raise _refuse("request.encoding.fractionalBits", "is a whole number")
        if bits > MAX_FRACTIONAL_BITS:
            raise _refuse("request.encoding.fractionalBits", f"is at most {MAX_FRACTIONAL_BITS}")
        if encoding["rounding"] != "floor":
            raise _refuse(
                "request.encoding.rounding", "the grid rounds toward minus infinity, 'floor'"
            )
    declared = _mapping_of_names(raw["tables"], "request.tables", MOST_TABLES)
    tables = {
        name: _table(
            name,
            declared[name],
            f"request.tables.{name}",
            additions=additions,
            from_1_2_0=from_1_2_0,
        )
        for name in sorted(declared)
    }
    relationships = tuple(
        _relationship(
            item, f"request.relationships[{index}]", tables, dated=dated, from_1_2_0=from_1_2_0
        )
        for index, item in enumerate(
            _list(raw.get("relationships", []), "request.relationships", most=MOST_RELATIONSHIPS)
        )
    )
    facts = tuple(
        _fact(item, f"request.facts[{index}]", tables)
        for index, item in enumerate(_list(raw.get("facts", []), "request.facts", most=MOST_FACTS))
    )
    _distinct([fact.name for fact in facts], "request.facts")
    queries: Tuple[RequestQuery, ...] = ()
    if "queries" in raw:
        _needs_1_1_0(additions, "request.queries")
        queries = _queries(raw["queries"], "request.queries", tables)
    return ProfileRequest(
        contract_digest=digest,
        fractional_bits=bits,
        tables=tuple(tables[name] for name in sorted(tables)),
        relationships=relationships,
        facts=facts,
        queries=queries,
    )


def load_request_text(text: str, *, name: str = "profile request") -> ProfileRequest:
    """Read a request from its YAML text: exactly, within :data:`REQUEST_LIMITS`, one document.

    Needs PyYAML (the ``yaml`` extra). The text is read as a bundle's documents
    are (:func:`flux_stats.exact_yaml.load_documents`), so a float, a timestamp,
    an alias bomb and a document nested too deeply are refused before the request
    is read; this reader then refuses any key, name or value the format does not
    define. A file is opened by the caller, which holds it to a size and never
    follows a link.
    """
    # The exact reader's own refusal, FLX-3105, names the file and the line.
    documents = load_documents(text, name=name, budget=ReadBudget(REQUEST_LIMITS))
    if len(documents) != 1:
        raise _refuse(name, f"holds {len(documents)} documents, and a request is exactly one")
    return parse_request(documents[0])


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _twice(names: Sequence[str], where: str, what: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise _refuse(f"{where} {what} {name!r}", "is named twice")
        seen.add(name)


def check_request(request: ProfileRequest) -> None:
    """Refuse (``FLX-5038``) a request this module's own reader would refuse, naming the entry.

    The reader (:func:`parse_request`) holds every name, bound and uniqueness rule that makes a
    request one; a request built in code has not been through it. The rules of names, bounds
    and duplicates are applied to the request as it stands, so the refusal names the table,
    column or fact; then the document :func:`render_request` writes is read by
    :func:`parse_request` itself, so no rule of the reader is missed (the ends of a
    relationship, for one). A request that passes is read back by :func:`load_request_text`
    as itself with its tables and columns in name order, the order the reader gives them.
    """
    _bounded(len(request.tables), MOST_TABLES, "request.tables")
    _bounded(len(request.relationships), MOST_RELATIONSHIPS, "request.relationships")
    _bounded(len(request.facts), MOST_FACTS, "request.facts")
    _bounded(len(request.queries), MOST_QUERIES, "request.queries")
    _twice([table.expose_id for table in request.tables], "request", "table")
    for table in request.tables:
        where = f"request table {table.expose_id!r}"
        _name(table.expose_id, where)
        _bounded(len(table.columns), MOST_COLUMNS_PER_TABLE, f"{where} columns")
        _twice([column.name for column in table.columns], where, "column")
        _bounded(len(table.date_pairs), MOST_DATE_PAIRS, f"{where} date pairs")
        _bounded(len(table.calendar), MOST_CALENDAR_COLUMNS, f"{where} calendar")
        _bounded(len(table.clique_columns), MOST_CLIQUE_CANDIDATES, f"{where} clique columns")
        _bounded(len(table.rank_correlations), len(RANK_IDENTIFIERS), f"{where} rank correlations")
        for entry in table.rank_correlations:
            _bounded(len(entry.columns), MOST_RANK_COLUMNS, f"{where} rank correlation columns")
        for column in table.columns:
            _name(column.name, f"{where} column {column.name!r}")
    for index, relationship in enumerate(request.relationships):
        _bounded(len(relationship.dates), MOST_DATE_PAIRS, f"request.relationships[{index}].dates")
        _bounded(
            len(relationship.exposure), MOST_EXPOSURES, f"request.relationships[{index}].exposure"
        )
        _bounded(
            len(relationship.group_by),
            MOST_RELATIONSHIP_GROUPS,
            f"request.relationships[{index}].groupBy",
        )
        _bounded(
            len(relationship.linked),
            MOST_RELATIONSHIP_LINKS,
            f"request.relationships[{index}].linked",
        )
    for fact in request.facts:
        where = f"request fact {fact.name!r}"
        _name(fact.name, where)
        _name(fact.table, f"{where} table")
        _name(fact.rule, f"{where} rule")
    _twice([fact.name for fact in request.facts], "request", "fact")
    # What the rules above do not mirror, the reader holds: the document the request is
    # written as is read here by the reader itself.
    parse_request(request_document(request))


def _bounded(count: int, most: int, where: str) -> None:
    if count > most:
        raise _refuse(where, f"holds more than {most} entries")


#: Characters JSON leaves as they are and YAML reads as a line break.
_YAML_BREAKS = {"\u0085": "\\u0085", " ": "\\u2028", " ": "\\u2029"}


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = json.dumps(value, ensure_ascii=False)
    for character, escaped in _YAML_BREAKS.items():
        text = text.replace(character, escaped)
    return text


def _emit(value: Any, indent: int, lines: List[str]) -> None:
    pad = " " * indent
    if isinstance(value, Mapping):
        if not value:
            lines.append(f"{pad}{{}}")
            return
        for key in value:
            item = value[key]
            head = f"{pad}{_scalar(key)}:"
            if isinstance(item, Mapping) and item:
                lines.append(head)
                _emit(item, indent + 2, lines)
            elif isinstance(item, list) and item:
                lines.append(head)
                _emit(item, indent + 2, lines)
            elif isinstance(item, (Mapping, list)):
                lines.append(f"{head} {'{}' if isinstance(item, Mapping) else '[]'}")
            else:
                lines.append(f"{head} {_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping) and item:
                inner: List[str] = []
                _emit(item, indent + 2, inner)
                lines.append(f"{pad}- {inner[0].lstrip()}")
                lines.extend(inner[1:])
            elif isinstance(item, list) and item:
                lines.append(f"{pad}-")
                _emit(item, indent + 2, lines)
            else:
                lines.append(
                    f"{pad}- {_scalar(item) if not isinstance(item, (Mapping, list)) else '[]'}"
                )
    else:  # pragma: no cover - the documents built here hold nothing else
        lines.append(f"{pad}{_scalar(value)}")


def _relationship_document(entry: RequestRelationship) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "parent": {"table": entry.parent.table, "columns": list(entry.parent.columns)},
        "child": {"table": entry.child.table, "columns": list(entry.child.columns)},
        "statistics": list(entry.statistics),
    }
    if entry.dates:
        item["dates"] = [{"parent": link.parent, "child": link.child} for link in entry.dates]
    if entry.exposure:
        item["exposure"] = [
            {"start": each.start} if each.end is None else {"start": each.start, "end": each.end}
            for each in entry.exposure
        ]
    if entry.group_by:
        item["groupBy"] = list(entry.group_by)
    if entry.linked:
        item["linked"] = [
            {"column": each.column, "parentColumn": each.parent_column} for each in entry.linked
        ]
    return item


def _table_document(table: RequestTable) -> Dict[str, Any]:
    """One table of the request as its document: its columns in name order, then the optional members."""
    columns: Dict[str, Any] = {}
    for column in sorted(table.columns, key=lambda entry: entry.name):
        entry: Dict[str, Any] = {"domain": column.domain}
        if column.metrics:
            entry["metrics"] = list(column.metrics)
        if column.statistics:
            entry["statistics"] = list(column.statistics)
        columns[column.name] = entry
    item: Dict[str, Any] = {"columns": columns}
    if table.row_count:
        item["rowCount"] = True
    if table.date_pairs:
        item["datePairs"] = [{"start": pair.start, "end": pair.end} for pair in table.date_pairs]
    if table.calendar:
        item["calendar"] = list(table.calendar)
    if table.workload:
        item["workload"] = _workload_document(table.workload)
    if table.rank_correlations:
        item["rankCorrelations"] = [
            {"definition": entry.definition, "columns": list(entry.columns)}
            for entry in table.rank_correlations
        ]
    if table.clique_columns:
        item["cliqueColumns"] = list(table.clique_columns)
    return item


def request_document(request: ProfileRequest) -> Dict[str, Any]:
    """The request as the document :func:`parse_request` reads back."""
    document: Dict[str, Any] = {"_schemaURL": _schema_url_of(request)}
    if request.contract_digest is not None:
        document["contract"] = {"digest": request.contract_digest}
    document["encoding"] = {"fractionalBits": request.fractional_bits, "rounding": "floor"}
    # Tables and columns are written in the order the reader sorts them into, so what is read back
    # is the request written.
    document["tables"] = {
        table.expose_id: _table_document(table)
        for table in sorted(request.tables, key=lambda entry: entry.expose_id)
    }
    if request.relationships:
        document["relationships"] = [
            _relationship_document(entry) for entry in request.relationships
        ]
    if request.facts:
        document["facts"] = [
            {"name": fact.name, "table": fact.table, "rule": fact.rule} for fact in request.facts
        ]
    if request.queries:
        document["queries"] = _queries_document(request.queries)
    return document


#: The two comment lines every request starts with: what the file is, and where its format is
#: written. The reader takes comments as YAML does, so they change no request.
REQUEST_HEADER: Tuple[str, str] = (
    "# A profile request: the statistics a profiler measures for the tables of a FLUID contract.",
    "# Its format is docs/spec/PROFILE-REQUEST-SPEC.md; a profiler writes the profile it asks for.",
)


def render_request(request: ProfileRequest) -> str:
    """The request as YAML text: block style, keys in a fixed order, every string quoted.

    The text starts with two comment lines (:data:`REQUEST_HEADER`) naming what the file is and its
    format.

    The same request gives the same bytes on every machine, and
    :func:`load_request_text` reads the text back to an equal request when the request's tables
    and columns are in name order, which is the order the reader gives them and the order this
    writes them in. :func:`check_request` says whether the reader would take it at all.
    """
    lines: List[str] = list(REQUEST_HEADER)
    _emit(request_document(request), 0, lines)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Format 1-1-0: the consumer additions (ADR 0028 section 6)
#
# Kept together, apart from the other additions of the same format, so that the two merge by
# keeping both. Everything below is read and written through the hooks marked above:
# ``queries`` on the request and ``workload`` on a table.
# ---------------------------------------------------------------------------

#: At most this many queries, and this many UTF-8 bytes in one query's text.
MOST_QUERIES = 1024
MOST_QUERY_BYTES = 65_536
#: A workload entry names 1 to 8 columns, has a weight of 1 to 1 000 000, and a table lists at
#: most this many entries.
MOST_WORKLOAD_COLUMNS = 8
MOST_WORKLOAD_WEIGHT = 1_000_000
MOST_WORKLOAD_SETS = 4096


@dataclass(frozen=True)
class RequestUnit:
    """The rows whose count a query's rate divides by: a table of the request, or a view's SQL."""

    table: Optional[str] = None
    sql: Optional[str] = None


@dataclass(frozen=True)
class RequestQuery:
    """One consumer query a profiler answers with a ``levelHistogram/1`` over ``false``, ``true``.

    ``sql`` is the consumers' question and its literals are theirs, never the data's: a profiler
    treats it as untrusted text (PROFILE-REQUEST-SPEC.md section 6).
    """

    name: str
    sql: str
    unit: RequestUnit
    group_by: Optional[str] = None


@dataclass(frozen=True)
class RequestWorkload:
    """A set of categorical columns consumers' queries read together, and how many queries do."""

    columns: Tuple[str, ...]
    weight: int


def _schema_url_of(request: ProfileRequest) -> str:
    """The earliest format that holds every addition the request uses, so one without keeps its text.

    Format 1-2-0 when the request holds an addition of :data:`_ADDITIONS_OF_1_2_0`, 1-1-0 when it
    holds one of that format, and 1-0-0 otherwise.
    """
    if any(
        addition.held(entry)
        for addition in _ADDITIONS_OF_1_2_0
        for entry in _entries_of(request, addition.section)
    ):
        return REQUEST_SCHEMA_URL_1_2_0
    if (
        request.queries
        or any(table.workload or table.date_pairs or table.calendar for table in request.tables)
        or any(entry.dates or entry.exposure for entry in request.relationships)
    ):
        return REQUEST_SCHEMA_URL_1_1_0
    return REQUEST_SCHEMA_URL


@dataclass(frozen=True)
class _Addition:
    """One key that only format 1-2-0 holds: the section of the request it sits in, its name, and
    how to tell that a parsed entry of that section uses it."""

    section: str
    key: str
    held: Callable[[Any], bool]


#: Every key that only format ``1-2-0`` holds. A document that names an earlier format and holds
#: one is refused (:func:`_needs_1_2_0`), the keys a section may carry (:func:`_keys_of_1_2_0`) and
#: the format a request is written in (:func:`_schema_url_of`) come from this list, and a later
#: addition of the format is one more entry here and one place that reads it.
_ADDITIONS_OF_1_2_0: Tuple[_Addition, ...] = (
    _Addition("table", "rankCorrelations", lambda table: bool(table.rank_correlations)),
    _Addition("table", "cliqueColumns", lambda table: bool(table.clique_columns)),
    _Addition("relationship", "groupBy", lambda relationship: bool(relationship.group_by)),
    _Addition("relationship", "linked", lambda relationship: bool(relationship.linked)),
)

#: Every format this reader reads, oldest first.
_READ_FORMATS: Tuple[str, ...] = (
    REQUEST_SCHEMA_URL,
    REQUEST_SCHEMA_URL_1_1_0,
    REQUEST_SCHEMA_URL_1_2_0,
)


def _keys_of_1_2_0(section: str) -> Tuple[str, ...]:
    """The keys a *section* may carry in format 1-2-0 and not before."""
    return tuple(item.key for item in _ADDITIONS_OF_1_2_0 if item.section == section)


def _entries_of(request: ProfileRequest, section: str) -> Sequence[Any]:
    """The parsed entries of a *section* of the request, in document order."""
    return request.tables if section == "table" else request.relationships


def _needs_1_2_0(from_1_2_0: bool, raw: Mapping[str, Any], where: str, section: str) -> None:
    """Refuse (``FLX-5038``) an addition of format 1-2-0 in a document that names an earlier format."""
    if from_1_2_0:
        return
    for key in _keys_of_1_2_0(section):
        if key in raw:
            raise _refuse(
                f"{where}.{key}",
                "is an addition of format 1-2-0, and this document names an earlier format: name "
                f"{REQUEST_SCHEMA_URL_1_2_0} in _schemaURL",
            )


def _rank_correlations(
    value: Any, where: str, columns: Sequence[RequestColumn]
) -> Tuple[RequestRankCorrelation, ...]:
    """A table's ``rankCorrelations``: each definition once, over 2 to 64 distinct ordered columns."""
    domains = {column.name: column.domain for column in columns}
    found: List[RequestRankCorrelation] = []
    for index, item in enumerate(_list(value, where, most=len(RANK_IDENTIFIERS))):
        here = f"{where}[{index}]"
        raw = _mapping(item, here, required=("definition", "columns"))
        identifier = _name(raw["definition"], f"{here}.definition")
        if identifier not in RANK_IDENTIFIERS:
            raise _refuse(f"{here}.definition", f"is one of {sorted(RANK_IDENTIFIERS)}")
        names = _names(raw["columns"], f"{here}.columns", most=MOST_RANK_COLUMNS)
        if len(names) < 2:
            raise _refuse(f"{here}.columns", f"names 2 to {MOST_RANK_COLUMNS} columns")
        _distinct(names, f"{here}.columns")
        for position, name in enumerate(names):
            if domains.get(name) not in (NUMBER, TIMESTAMP):
                raise _refuse(
                    f"{here}.columns[{position}]",
                    "names no column of this table that the request lists with the domain "
                    "number or timestamp",
                )
        found.append(RequestRankCorrelation(definition=identifier, columns=names))
    _distinct([entry.definition for entry in found], where)
    return tuple(found)


def _clique_columns(value: Any, where: str, columns: Sequence[RequestColumn]) -> Tuple[str, ...]:
    """A table's ``cliqueColumns``: columns of the table, each once, at most 1,024."""
    names = _names(value, where, most=MOST_CLIQUE_CANDIDATES)
    _distinct(names, where)
    known = {column.name for column in columns}
    for position, name in enumerate(names):
        if name not in known:
            raise _refuse(f"{where}[{position}]", "names no column of this table")
    return names


def _group_by(value: Any, where: str, parent: RequestTable) -> Tuple[str, ...]:
    """A relationship's ``groupBy``: columns of the parent table, each once, at most 64."""
    names = _names(value, where, most=MOST_RELATIONSHIP_GROUPS)
    _distinct(names, where)
    known = {column.name for column in parent.columns}
    for position, name in enumerate(names):
        if name not in known:
            raise _refuse(
                f"{where}[{position}]", "names no column of the relationship's parent table"
            )
    return names


def _linked(
    value: Any, where: str, parent: RequestTable, child: RequestTable
) -> Tuple[RequestLinked, ...]:
    """A relationship's ``linked``: a child column and a parent column, each pair once, at most 1,024."""
    found: List[RequestLinked] = []
    parent_columns = {column.name for column in parent.columns}
    child_columns = {column.name for column in child.columns}
    for index, item in enumerate(_list(value, where, most=MOST_RELATIONSHIP_LINKS)):
        here = f"{where}[{index}]"
        raw = _mapping(item, here, required=("column", "parentColumn"))
        column = _name(raw["column"], f"{here}.column")
        parent_column = _name(raw["parentColumn"], f"{here}.parentColumn")
        if column not in child_columns:
            raise _refuse(f"{here}.column", "names no column of the relationship's child table")
        if parent_column not in parent_columns:
            raise _refuse(
                f"{here}.parentColumn", "names no column of the relationship's parent table"
            )
        found.append(RequestLinked(column=column, parent_column=parent_column))
    _distinct([f"{entry.column}\n{entry.parent_column}" for entry in found], where)
    return tuple(found)


def _needs_1_1_0(additions: bool, where: str) -> None:
    if not additions:
        raise _refuse(
            where,
            "is an addition of format 1-1-0, and this document names format 1-0-0: name "
            f"{REQUEST_SCHEMA_URL_1_1_0} in _schemaURL",
        )


def _text(value: Any, where: str) -> str:
    """A query's text: NFC, no control character but newline and tab, at most 64 KiB of UTF-8."""
    if not isinstance(value, str):
        raise _refuse(where, f"expected a string, got {type(value).__name__}")
    if not value.strip():
        raise _refuse(where, "a query text is not empty")
    if len(value.encode("utf-8", errors="surrogatepass")) > MOST_QUERY_BYTES:
        raise _refuse(where, f"a query text is at most {MOST_QUERY_BYTES} UTF-8 bytes")
    if not unicodedata.is_normalized("NFC", value):
        raise _refuse(where, "a query text is written in Unicode Normalization Form C")
    if any(
        unicodedata.category(character) == "Cc" and character not in "\n\t" for character in value
    ):
        raise _refuse(where, "a query text holds no control character but newline and tab")
    return value


def _unit(value: Any, where: str, tables: Mapping[str, RequestTable]) -> RequestUnit:
    raw = _mapping(value, where, optional=("table", "sql"))
    if len(raw) != 1:
        raise _refuse(where, "holds exactly one of table and sql")
    if "table" in raw:
        table = _name(raw["table"], f"{where}.table")
        if table not in tables:
            raise _refuse(f"{where}.table", "names no table of this request")
        return RequestUnit(table=table)
    return RequestUnit(sql=_text(raw["sql"], f"{where}.sql"))


def _queries(
    value: Any, where: str, tables: Mapping[str, RequestTable]
) -> Tuple[RequestQuery, ...]:
    found: List[RequestQuery] = []
    for index, item in enumerate(_list(value, where, most=MOST_QUERIES)):
        here = f"{where}[{index}]"
        raw = _mapping(item, here, required=("name", "sql", "unit"), optional=("groupBy",))
        found.append(
            RequestQuery(
                name=_name(raw["name"], f"{here}.name"),
                sql=_text(raw["sql"], f"{here}.sql"),
                unit=_unit(raw["unit"], f"{here}.unit", tables),
                group_by=_name(raw["groupBy"], f"{here}.groupBy") if "groupBy" in raw else None,
            )
        )
    _distinct([query.name for query in found], where)
    return tuple(found)


def _workload(
    value: Any, where: str, columns: Sequence[RequestColumn]
) -> Tuple[RequestWorkload, ...]:
    domains = {column.name: column.domain for column in columns}
    found: List[RequestWorkload] = []
    for index, item in enumerate(_list(value, where, most=MOST_WORKLOAD_SETS)):
        here = f"{where}[{index}]"
        raw = _mapping(item, here, required=("columns", "weight"))
        names = _names(raw["columns"], f"{here}.columns", most=MOST_WORKLOAD_COLUMNS)
        if not names:
            raise _refuse(f"{here}.columns", f"names 1 to {MOST_WORKLOAD_COLUMNS} columns")
        for position, name in enumerate(names):
            if domains.get(name) != CATEGORY:
                raise _refuse(
                    f"{here}.columns[{position}]",
                    "names no column of this table that the request lists with the domain category",
                )
        if list(names) != sorted(set(names)):
            raise _refuse(f"{here}.columns", "names each column once, in sorted order")
        weight = raw["weight"]
        if (
            isinstance(weight, bool)
            or not isinstance(weight, int)
            or not 1 <= weight <= MOST_WORKLOAD_WEIGHT
        ):
            raise _refuse(f"{here}.weight", f"is a whole number from 1 to {MOST_WORKLOAD_WEIGHT}")
        found.append(RequestWorkload(columns=names, weight=weight))
    keys = [entry.columns for entry in found]
    if keys != sorted(set(keys)):
        raise _refuse(where, "lists each set of columns once, in sorted order")
    return tuple(found)


def _workload_document(workload: Sequence[RequestWorkload]) -> List[Dict[str, Any]]:
    return [
        {"columns": list(entry.columns), "weight": entry.weight}
        for entry in sorted(workload, key=lambda item: item.columns)
    ]


def _queries_document(queries: Sequence[RequestQuery]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for query in queries:
        unit: Dict[str, Any] = (
            {"table": query.unit.table} if query.unit.table is not None else {"sql": query.unit.sql}
        )
        entry: Dict[str, Any] = {"name": query.name, "sql": query.sql, "unit": unit}
        if query.group_by is not None:
            entry["groupBy"] = query.group_by
        out.append(entry)
    return out
