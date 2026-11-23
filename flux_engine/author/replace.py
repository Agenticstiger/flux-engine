# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a profile of the contract would publish in place of each number ``init`` assumes (ADR 0030 section 8).

``fluxsim init`` writes a number it has no statistic for as an assumption, with its reason. Most of
those numbers are the answer to a question a profiler can measure: how many rows, which levels,
which histogram, which range, how many nulls. :func:`replacements_for` names, for each assumed
number, the statistic of a profile of the contract that would replace it, as the register's
``replacedBy`` (format 1-3-0), and :func:`replacement_request` writes a profile request (the
format main reads today, docs/spec/PROFILE-REQUEST-SPEC.md) for exactly those statistics. Running
``init`` again with a profile that answers the request turns each of those assumptions into a
profile citation, because the rules that read a statistic cite it as the profile's.

A number that is a setting and no statistic has none: a tolerance, the bounds of a committed
state, a check's budget, the time window and the template defaults. They are decisions of the
bundle's author, so nothing a profiler measures would replace them.

**Where the statistic sits.** A metric is at ``/tables/<table>/columns/<column>/metrics/<name>`` and
a table's row count at ``/tables/<table>/rowCount``. A statistic is in its column's ``statistics``
list, and its index is its place in the request's list for that column, which is in identifier
order: a profiler that writes the profile from the request lists the statistics in the order the
request does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.seam.parameters import Citation, parse_pointer, render_pointer
from flux_stats import definition_for_kind, metrics
from flux_stats.column_metrics import CATEGORY, NUMBER, TIMESTAMP
from flux_stats.request import (
    ProfileRequest,
    RequestColumn,
    RequestTable,
    check_request,
)

__all__ = [
    "ASSUMPTIONS_REQUEST_SUFFIX",
    "Replacement",
    "replacement_request",
    "replacements_for",
]

#: What the request is named after the bundle directory: ``demo`` gives
#: ``demo.assumptions.profile-request.yml``, beside it and never inside it.
ASSUMPTIONS_REQUEST_SUFFIX = ".assumptions.profile-request.yml"

_ORDERED = frozenset({NUMBER, TIMESTAMP})
_EVERY = frozenset({CATEGORY, NUMBER, TIMESTAMP})

_LEVELS = definition_for_kind("levelHistogram").identifier
_HISTOGRAM = definition_for_kind("marginalHistogram").identifier
_MEAN = definition_for_kind("mean").identifier

#: A statistic asked for: the key under a column of the fidelity block that holds the assumed number,
#: and the registered statistic or metric a profile would publish for it, with the domains it reads.
_STATISTIC_FOR_KEY: Mapping[str, Tuple[str, frozenset]] = {
    "levels": (_LEVELS, frozenset({CATEGORY})),
    "levelWeightsGiven": (_LEVELS, frozenset({CATEGORY})),
    "rate": (_LEVELS, frozenset({CATEGORY})),
    "bins": (_HISTOGRAM, _ORDERED),
    "binWeights": (_HISTOGRAM, _ORDERED),
    "binWeightsGiven": (_HISTOGRAM, _ORDERED),
    "mean": (_MEAN, _ORDERED),
}
#: The metric for a key, by the metric's name.
_METRIC_FOR_KEY: Mapping[str, Tuple[str, frozenset]] = {
    "nullFraction": ("nullCount", _EVERY),
    "maxCardinality": ("distinctCount", _EVERY),
    "range": ("min", _ORDERED),
}


@dataclass(frozen=True)
class Replacement:
    """A statistic of a profile of the contract: a table's row count, or a column's metric or statistic."""

    table: str
    column: Optional[str]
    #: ``rowCount`` for a table's row count, otherwise the identifier of a metric or a statistic.
    identifier: Optional[str]
    #: Whether *identifier* names a column metric (``True``) or a statistic (``False``).
    metric: bool = False

    @property
    def sort_key(self) -> Tuple[str, str, int, str]:
        return (self.table, self.column or "", int(not self.metric), self.identifier or "")


def _metric_identifier(name: str) -> str:
    return next(entry.identifier for entry in metrics() if entry.name == name)


def _for_column(
    table: str, column: str, rest: Tuple[str, ...], domain: str
) -> Optional[Replacement]:
    """The statistic that would replace the number at *rest*, under a column of the fidelity block."""
    key = rest[0]
    if key in _STATISTIC_FOR_KEY:
        identifier, read = _STATISTIC_FOR_KEY[key]
        return Replacement(table, column, identifier) if domain in read else None
    if key == "range" and rest[1:2] == ("max",):
        name, read = "max", _ORDERED
    elif key in _METRIC_FOR_KEY:
        name, read = _METRIC_FOR_KEY[key]
    else:
        return None
    return Replacement(table, column, _metric_identifier(name), True) if domain in read else None


def _replacement(
    tokens: Tuple[str, ...], domains: Mapping[Tuple[str, str], str]
) -> Optional[Replacement]:
    """The statistic that would replace the number at *tokens*, under the namespace, or ``None``."""
    if len(tokens) < 3 or tokens[0] != "emits":
        return None
    table, rest = tokens[1], tokens[2:]
    if rest == ("rows",):
        return Replacement(table, None, None)
    if len(rest) < 4 or rest[:2] != ("fidelity", "columns"):
        return None
    domain = domains.get((table, rest[2]))
    return _for_column(table, rest[2], rest[3:], domain) if domain is not None else None


def _pointer(replacement: Replacement, request: ProfileRequest) -> Tuple[str, Optional[str]]:
    """The pointer a profile answering *request* publishes *replacement* at, and its definition."""
    base: Tuple[str, ...] = ("tables", replacement.table)
    if replacement.column is None:
        return render_pointer(base + ("rowCount",)), None
    column = ("columns", replacement.column)
    if replacement.metric:
        name = next(entry.name for entry in metrics() if entry.identifier == replacement.identifier)
        return render_pointer(base + column + ("metrics", name)), replacement.identifier
    entry = next(
        item
        for table in request.tables
        if table.expose_id == replacement.table
        for item in table.columns
        if item.name == replacement.column
    )
    index = entry.statistics.index(str(replacement.identifier))
    return render_pointer(base + column + ("statistics", str(index))), replacement.identifier


def replacement_request(
    replacements: Sequence[Replacement],
    domains: Mapping[Tuple[str, str], str],
    *,
    contract_digest: str,
    fractional_bits: int = 16,
) -> ProfileRequest:
    """The profile request for exactly *replacements*: the format main reads today.

    Tables and columns in name order, a column's metrics in name order and its statistics in
    identifier order, as the request writer lists them. *domains* are the contract's columns.
    """
    tables: Dict[str, Dict[str, Tuple[Set[str], Set[str]]]] = {}
    rows: Set[str] = set()
    for item in replacements:
        in_table = tables.setdefault(item.table, {})
        if item.column is None:
            rows.add(item.table)
            continue
        found = in_table.setdefault(item.column, (set(), set()))
        if item.metric:
            found[0].add(next(e.name for e in metrics() if e.identifier == item.identifier))
        else:
            found[1].add(str(item.identifier))
    request = ProfileRequest(
        contract_digest=contract_digest,
        fractional_bits=fractional_bits,
        tables=tuple(
            RequestTable(
                expose_id=table,
                row_count=table in rows,
                columns=tuple(
                    RequestColumn(
                        name=column,
                        domain=domains[(table, column)],
                        metrics=tuple(sorted(tables[table][column][0])),
                        statistics=tuple(sorted(tables[table][column][1])),
                    )
                    for column in sorted(tables[table])
                ),
            )
            for table in sorted(tables)
        ),
    )
    check_request(request)
    return request


def replacements_for(
    entries: Sequence[Mapping[str, Any]],
    domains: Mapping[Tuple[str, str], str],
    namespace: str,
    simulation_id: str,
    *,
    contract_digest: str,
    fractional_bits: int = 16,
) -> Tuple[List[Dict[str, Any]], Optional[ProfileRequest], List[Tuple[str, Optional[str], int]]]:
    """Give each assumption of *entries* its ``replacedBy``, and the request that asks for them.

    Returns the entries (each ``assumption`` of the Simulation whose number a profile statistic would
    replace gaining ``replacedBy``, a mapping of ``pointer`` and ``definition``), the profile request
    for exactly those statistics (``None`` when there are none), and each statistic, as its pointer and
    definition, with the number of entries it would replace. An entry that is not an assumption, or sits in another document,
    or is a setting, is returned as it was.
    """
    found: List[Optional[Replacement]] = []
    for entry in entries:
        replacement: Optional[Replacement] = None
        if entry.get("source") == "assumption" and entry.get("document") == simulation_id:
            tokens = parse_pointer(str(entry["pointer"]))
            if tokens[:2] == ("extensions", namespace):
                replacement = _replacement(tokens[2:], domains)
        found.append(replacement)
    wanted = sorted({item for item in found if item is not None}, key=lambda item: item.sort_key)
    if not wanted:
        return [dict(entry) for entry in entries], None, []
    request = replacement_request(
        wanted, domains, contract_digest=contract_digest, fractional_bits=fractional_bits
    )
    pointers = {item: _pointer(item, request) for item in wanted}
    result: List[Dict[str, Any]] = []
    counts: Dict[Replacement, int] = {}
    for entry, replacement in zip(entries, found, strict=True):
        document = dict(entry)
        if replacement is not None:
            pointer, definition = pointers[replacement]
            citation = Citation(
                pointer=pointer, tokens=parse_pointer(pointer), definition=definition
            )
            document["replacedBy"] = citation.as_document()
            counts[replacement] = counts.get(replacement, 0) + 1
        result.append(document)
    return (
        result,
        request,
        [(*pointers[item], counts[item]) for item in wanted],
    )
