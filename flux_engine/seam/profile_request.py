# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The profile request a contract implies (docs/spec/PROFILE-REQUEST-SPEC.md, ADR 0018).

``fluxsim init`` given a contract and no profile writes this request: every
statistic, column metric and fact share the bundle it would write reads from a
profile. The request is a function of the contract alone, so the same contract
gives the same request on every machine, and it names no record and no value
of the data.

What is asked of each column follows from its logical type: a number, date or
timestamp column is asked for the metrics that order its values and for its mean,
histogram and binned quantile; a column that declares its levels is asked for its
level histogram and support; every other column is asked for its counts only. Each
declared relationship is asked for its children per parent. Each rule of the
contract (``contract.invariants[]``) is one fact, named
``<exposeId>.<ruleId>`` (:func:`fact_name`), whose share of satisfying rows the
profile's ``invariants`` section carries (PROFILE-SPEC.md, format 1-2-0).

Each table is asked for the days between every two of its date or timestamp columns, and each
relationship for the days between every date column of its parent and every one of its child
(ADR 0024 section 9), at most 1 024 pairs for each. The first in the contract's order are taken.
A contract with no such pair asks for none, and its request is written as format 1-0-0.

A request that asks for correlations is written as format 1-2-0 (ADR 0035, ADR 0036, ADR 0037),
and one that asks for none keeps the format and the bytes it had. A table is asked for the
``kendallTau/1`` of its number and timestamp columns that ``init`` would draw from histograms
(:func:`_rank_columns`), for the columns a clique could hold (``cliqueColumns``: those the contract
requires, since a clique column holds no null), and each
relationship ``init`` would lay out on the counted layout is asked to be grouped by the columns
of its parent that ``init`` would draw (``groupBy``), and for the child columns that a link
grouping could draw given a parent column (``linked``). A column that a key, a relationship or a
rule of the contract sets is drawn from none of them, so none of them is asked for it, and a
column's logical type decides the rest. Which of the asked columns a profile answers, and so which
``init`` draws that way, is the profile's: the request holds the candidates.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from itertools import combinations
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, NamedTuple, Sequence, Set, Tuple

from flux_engine.core.errors import FluxError
from flux_engine.seam.contract import LogicalKind, compile_expose
from flux_engine.seam.facts import contract_rule_ids, fact_name
from flux_engine.seam.keys import compile_keys, declared_exposes
from flux_engine.seam.resolve import FLX_3105
from flux_stats import definition_for_kind, metrics
from flux_stats.column_metrics import CATEGORY, NUMBER, TIMESTAMP
from flux_stats.rank_correlation import KENDALL_TAU_ID
from flux_stats.request import (
    MOST_CALENDAR_COLUMNS,
    MOST_CLIQUE_CANDIDATES,
    MOST_DATE_PAIRS,
    MOST_EXPOSURES,
    MOST_RANK_COLUMNS,
    MOST_RELATIONSHIP_GROUPS,
    MOST_RELATIONSHIP_LINKS,
    MOST_WORKLOAD_COLUMNS,
    ProfileRequest,
    RequestColumn,
    RequestDateLink,
    RequestDatePair,
    RequestEnd,
    RequestExposure,
    RequestFact,
    RequestLinked,
    RequestQuery,
    RequestRankCorrelation,
    RequestRelationship,
    RequestTable,
    RequestWorkload,
)

__all__ = [
    "column_domains",
    "fact_name",
    "request_for_contract",
    "contract_rule_ids",
    "workload_of",
]

#: The statistics asked of an ordered column, of a column that declares its levels and of the
#: children per parent. Named by the property kind each serves and read from the registry, so
#: an identifier is spelled once in the code base (the registry's own definition).
_ORDERED_STATISTICS: Tuple[str, ...] = (
    definition_for_kind("mean").identifier,
    definition_for_kind("marginalHistogram").identifier,
    definition_for_kind("quantile", binned=True).identifier,
)
_LEVEL_STATISTICS: Tuple[str, ...] = (
    definition_for_kind("levelHistogram").identifier,
    definition_for_kind("support").identifier,
)
_CHILD_STATISTICS: Tuple[str, ...] = _ORDERED_STATISTICS

_NUMBER_KINDS = frozenset({LogicalKind.INT, LogicalKind.DECIMAL})
_TIME_KINDS = frozenset({LogicalKind.DATE, LogicalKind.TIMESTAMP})


def _domain(kind: LogicalKind) -> str:
    if kind in _NUMBER_KINDS:
        return NUMBER
    if kind in _TIME_KINDS:
        return TIMESTAMP
    return CATEGORY


def _date_columns(compiled: Any) -> Tuple[str, ...]:
    """The names of an expose's DATE and TIMESTAMP columns, in the contract's order."""
    return tuple(
        column.name for column in compiled.columns if column.logical_type.kind in _TIME_KINDS
    )


def _date_pairs(names: Tuple[str, ...]) -> Tuple[RequestDatePair, ...]:
    """Every unordered pair of *names* once, the earlier in the contract's order as the start.

    At most :data:`~flux_stats.request.MOST_DATE_PAIRS` are asked: the first ones in the
    contract's order, which is the order of the pairs listed here.
    """
    pairs = (RequestDatePair(start=start, end=end) for start, end in combinations(names, 2))
    return tuple(pair for _, pair in zip(range(MOST_DATE_PAIRS), pairs, strict=False))


def _date_links(parent: Tuple[str, ...], child: Tuple[str, ...]) -> Tuple[RequestDateLink, ...]:
    """Every pair of a parent's date column and a child's, the first 1 024 in the contract's order."""
    links = (RequestDateLink(parent=up, child=down) for up in parent for down in child)
    return tuple(link for _, link in zip(range(MOST_DATE_PAIRS), links, strict=False))


def _calendar(names: Tuple[str, ...]) -> Tuple[str, ...]:
    """The date columns whose calendar is asked: each one, the first 1 024 in the contract's order."""
    return names[:MOST_CALENDAR_COLUMNS]


def _exposures(
    parent: Tuple[str, ...], pairs: Tuple[RequestDatePair, ...]
) -> Tuple[RequestExposure, ...]:
    """The lifetimes of a relationship's parent whose exposure is asked.

    Each pair of the parent's date columns that the request asks the days apart of is a lifetime
    with an end, and each date column of the parent alone is a lifetime with a start only. At most
    :data:`~flux_stats.request.MOST_EXPOSURES` are asked, the pairs first, in the contract's order.
    """
    found = [RequestExposure(start=pair.start, end=pair.end) for pair in pairs]
    found.extend(RequestExposure(start=name) for name in parent)
    return tuple(found[:MOST_EXPOSURES])


def column_domains(contract: Mapping[str, Any]) -> Dict[Tuple[str, str], str]:
    """The profile domain (``category``, ``number`` or ``timestamp``) of each column of the contract.

    Keyed by ``(exposeId, column)``: the domain a profile of the contract records for the column.
    """
    found: Dict[Tuple[str, str], str] = {}
    exposes = declared_exposes(contract)
    for expose_id in sorted(exposes):
        for column in compile_expose(exposes[expose_id]).columns:
            found[(expose_id, column.name)] = _domain(column.logical_type.kind)
    return found


def _metric_names(domain: str) -> Tuple[str, ...]:
    return tuple(sorted(entry.name for entry in metrics() if domain in entry.domains))


def workload_of(
    reads: Iterable[Tuple[str, FrozenSet[str]]], categorical: Mapping[str, FrozenSet[str]]
) -> Dict[str, Tuple[RequestWorkload, ...]]:
    """The workload of each table: the sets of categorical columns queries read together.

    *reads* holds one ``(exposeId, columns)`` per calibration query and table it reads, and
    *categorical* each table's columns that the request lists with the domain ``category``. A
    query's columns that are not categorical are left out of its set, a set left empty or past 8
    columns is no entry, and each set's weight is the number of queries that read exactly it. The
    entries of a table are in sorted order, as the request's reader requires.
    """
    counted: Dict[str, Counter] = {}
    for expose_id, columns in reads:
        wanted = tuple(sorted(columns & categorical.get(expose_id, frozenset())))
        if 1 <= len(wanted) <= MOST_WORKLOAD_COLUMNS:
            counted.setdefault(expose_id, Counter())[wanted] += 1
    return {
        expose_id: tuple(
            RequestWorkload(columns=names, weight=count) for names, count in sorted(sets.items())
        )
        for expose_id, sets in counted.items()
    }


#: Tags and a semantic type that make a column an identifier the engine derives, which is drawn
#: from no distribution (SEAM-FIDELITY-SPEC.md section 1).
_IDENTIFIER_MARKERS = frozenset({"identifier", "primary-key", "primary_key"})
_IDENTIFIER_TYPES = frozenset({"uuid", "uniqueidentifier", "guid"})


class _Drawn(NamedTuple):
    """The columns of one expose a profile's distributions can draw, by domain, in contract order."""

    #: Every column that nothing but its own distribution sets, with its domain.
    columns: Tuple[Tuple[str, str], ...]
    #: Every column that a key, a relationship or a rule sets.
    set_elsewhere: FrozenSet[str]
    #: The columns that a rule alone sets.
    by_rule: FrozenSet[str] = frozenset()
    #: The columns the contract does not require, which may hold NULL.
    nullable: FrozenSet[str] = frozenset()


def _is_identifier(column: Any) -> bool:
    declared = str(column.declared_type).strip().lower()
    semantic = column.semantic_type
    return (
        declared in _IDENTIFIER_TYPES
        or (semantic is not None and str(semantic).lower() in _IDENTIFIER_MARKERS)
        or any(str(tag).lower() in _IDENTIFIER_MARKERS for tag in column.tags)
    )


def _set_elsewhere(
    contract: Mapping[str, Any],
    expose_id: str,
    expose_index: int,
    expose: Mapping[str, Any],
    held: Any,
) -> Tuple[FrozenSet[str], FrozenSet[str]]:
    """The columns of *expose* that a key, a relationship or a rule sets, and those a rule sets.

    The same columns ``fluxsim init`` never draws from a distribution: a column of a key or of a
    foreign key, one a ``derive``, a ``dependency``, an aggregate or a ``sharedColumns`` or
    ``sharedAttribute`` copy sets.
    """
    from flux_engine.author.derived import (
        contract_aggregates,
        contract_copies,
        contract_dependents,
        contract_derives,
        contract_guarded,
    )

    related: Set[str] = set()
    if held is not None:
        for key in (*held.keys, *held.instant_keys):
            related.update(key)
        for link in held.relationships:
            related.update(link.from_columns)
    taken = (
        set(contract_derives(expose))
        | contract_dependents(expose)
        | contract_aggregates(expose)
        | contract_guarded(expose)
    )
    copies = contract_copies(contract, expose_id, expose_index, set(related), set(taken))
    by_rule = taken | set(copies)
    return frozenset(related | by_rule), frozenset(by_rule)


def _drawn(
    contract: Mapping[str, Any],
    expose_id: str,
    expose_index: int,
    expose: Mapping[str, Any],
    compiled: Any,
    held: Any,
) -> _Drawn:
    set_by, by_rule = _set_elsewhere(contract, expose_id, expose_index, expose, held)
    found = tuple(
        (column.name, _domain(column.logical_type.kind))
        for column in compiled.columns
        if column.name not in set_by and not column.hidden and not _is_identifier(column)
    )
    nullable = frozenset(column.name for column in compiled.columns if not column.required)
    return _Drawn(columns=found, set_elsewhere=set_by, by_rule=by_rule, nullable=nullable)


def _rank_columns(drawn: _Drawn) -> Tuple[str, ...]:
    """The number and timestamp columns asked for rank correlations, at most 64 in contract order.

    A table with fewer than two of them asks for none, since a correlation needs a pair.
    """
    names = tuple(name for name, domain in drawn.columns if domain in (NUMBER, TIMESTAMP))
    return names[:MOST_RANK_COLUMNS] if len(names) >= 2 else ()


def _clique_columns(drawn: _Drawn) -> Tuple[str, ...]:
    """The columns a clique could hold: every column drawn on its own that the contract requires.

    A clique column holds no null, so a column the contract leaves optional is none, and a profiler
    releases one over its known values outside the cliques. At most 1,024; a clique joins two or
    more columns, so a table with one asks for none.
    """
    names = tuple(name for name, _ in drawn.columns if name not in drawn.nullable)
    return names[:MOST_CLIQUE_CANDIDATES] if len(names) >= 2 else ()


def _counted(held: Any, link: Any, by_rule: FrozenSet[str]) -> bool:
    """Whether ``init`` could lay *link* out on the counted layout, from the contract alone.

    A relationship of one column on each side, to another expose, whose foreign key no key, other
    relationship or rule of the contract sets. What the profile then publishes decides the rest.
    """
    if len(link.from_columns) != 1 or len(link.to_columns) != 1 or link.target == link.source:
        return False
    (column,) = link.from_columns
    if column in by_rule:
        return False
    keyed = any(column in key for key in (*held.keys, *held.instant_keys))
    # A key of the expose keeps the column out, but for the column a one_to_one relationship
    # makes unique itself.
    if keyed and not _only_this_key(held, link, column):
        return False
    return not any(
        other is not link and column in other.from_columns for other in held.relationships
    )


def _only_this_key(held: Any, link: Any, column: str) -> bool:
    """Whether *column* is a key only because *link* is one_to_one and nothing else makes it one."""
    if link.cardinality != "one_to_one":
        return False
    others = [
        key
        for key in (*held.keys, *held.instant_keys)
        if column in key and tuple(key) != tuple(link.from_columns)
    ]
    return not others


def _group_by(parent: _Drawn) -> Tuple[str, ...]:
    """The parent's columns a relationship's statistics are grouped by: every column it draws."""
    return tuple(name for name, _ in parent.columns)[:MOST_RELATIONSHIP_GROUPS]


def _linked(child: _Drawn, parent: _Drawn) -> Tuple[RequestLinked, ...]:
    """The child columns a parent column could group, through the relationship.

    A link grouping reads the levels of the parent's column, so the parent's column is a
    category one; the child's is any column the child draws on its own. At most 1,024 pairs, the
    child's column varying slowest.
    """
    found = (
        RequestLinked(column=name, parent_column=up)
        for name, _ in child.columns
        for up, domain in parent.columns
        if domain == CATEGORY
    )
    return tuple(link for _, link in zip(range(MOST_RELATIONSHIP_LINKS), found, strict=False))


def request_for_contract(
    contract: Mapping[str, Any],
    *,
    contract_digest: str,
    fractional_bits: int = 16,
    queries: Sequence[RequestQuery] = (),
    reads: Iterable[Tuple[str, FrozenSet[str]]] = (),
) -> ProfileRequest:
    """The request *contract* implies, for the contract whose ``jcs-document`` digest is given.

    *queries* are the consumers' calibration queries that have a unit, and *reads* the column
    sets those queries read together (:func:`workload_of`); either makes the request format 1-1-0.

    Refuses (``FLX-3105``) a contract with no expose, and lets the contract's own
    refusals (an unreadable type, an unresolved key) through, since a request for
    a contract the engine cannot read would only move the failure to the second
    step of ``init``.
    """
    exposes = declared_exposes(contract)
    if not exposes:
        raise FluxError(FLX_3105, "the contract declares no expose, so there is nothing to profile")
    tables: List[RequestTable] = []
    dates: Dict[str, Tuple[str, ...]] = {}
    facts: List[RequestFact] = []
    asked_columns: Dict[str, FrozenSet[str]] = {}
    keys = compile_keys(contract)
    drawn: Dict[str, _Drawn] = {}
    positions = {
        str(entry.get("exposeId")): index
        for index, entry in enumerate(contract.get("exposes") or ())
        if isinstance(entry, Mapping)
    }
    for expose_id in sorted(exposes):
        compiled = compile_expose(exposes[expose_id])
        held = keys.expose(expose_id) if keys is not None else None
        drawn[expose_id] = _drawn(
            contract, expose_id, positions.get(expose_id, 0), exposes[expose_id], compiled, held
        )
        dates[expose_id] = _date_columns(compiled)
        columns = []
        # In name order, the order a request is read back into (flux_stats.request).
        for column in sorted(compiled.columns, key=lambda entry: entry.name):
            domain = _domain(column.logical_type.kind)
            levels = domain == CATEGORY and bool(column.enum_values)
            columns.append(
                RequestColumn(
                    name=column.name,
                    domain=domain,
                    metrics=_metric_names(domain),
                    statistics=(
                        _LEVEL_STATISTICS
                        if levels
                        else () if domain == CATEGORY else _ORDERED_STATISTICS
                    ),
                )
            )
        ranked = _rank_columns(drawn[expose_id])
        tables.append(
            RequestTable(
                expose_id=expose_id,
                row_count=True,
                columns=tuple(columns),
                date_pairs=_date_pairs(dates[expose_id]),
                calendar=_calendar(dates[expose_id]),
                rank_correlations=(
                    (RequestRankCorrelation(definition=KENDALL_TAU_ID, columns=ranked),)
                    if ranked
                    else ()
                ),
                clique_columns=_clique_columns(drawn[expose_id]),
            )
        )
        asked_columns[expose_id] = frozenset(
            column.name for column in columns if column.domain == CATEGORY
        )
        facts.extend(
            RequestFact(name=fact_name(expose_id, rule), table=expose_id, rule=rule)
            for rule in contract_rule_ids(exposes[expose_id])
        )
    relationships: List[RequestRelationship] = []
    if keys is not None:
        for expose_id in sorted(exposes):
            held = keys.expose(expose_id)
            for link in held.relationships:
                parent_drawn, child_drawn = drawn.get(link.target), drawn.get(link.source)
                counted = (
                    parent_drawn is not None
                    and child_drawn is not None
                    and _counted(held, link, child_drawn.by_rule)
                )
                relationships.append(
                    RequestRelationship(
                        parent=RequestEnd(link.target, tuple(link.to_columns)),
                        child=RequestEnd(link.source, tuple(link.from_columns)),
                        statistics=_CHILD_STATISTICS,
                        dates=_date_links(dates.get(link.target, ()), dates.get(link.source, ())),
                        exposure=_exposures(
                            dates.get(link.target, ()),
                            _date_pairs(dates.get(link.target, ())),
                        ),
                        group_by=_group_by(parent_drawn) if counted and parent_drawn else (),
                        linked=(
                            _linked(child_drawn, parent_drawn)
                            if counted and parent_drawn and child_drawn
                            else ()
                        ),
                    )
                )
    workload = workload_of(reads, asked_columns)
    return ProfileRequest(
        contract_digest=contract_digest,
        fractional_bits=fractional_bits,
        tables=tuple(
            replace(table, workload=workload.get(table.expose_id, ())) for table in tables
        ),
        relationships=tuple(relationships),
        facts=tuple(facts),
        queries=tuple(queries),
    )
