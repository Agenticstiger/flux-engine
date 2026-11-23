# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init --consumers FILE``: the consumers' questions become requirements (ADR 0028).

The file holds a ``consumers`` block as a Simulation declares it (the keys of
:mod:`flux_engine.seam.consumers`, at the top level of the file). It is read with the exact YAML
reader and the registry reader, checked with sqlglot against the contract's exposes
(:mod:`flux_engine.consumers.check`), and read for the requirements of its queries
(:mod:`flux_engine.consumers.requirements`). Two things follow:

* the authoring step embeds the block in the Simulation under ``consumers``, with
  ``compiledWith: {sqlglot: <version>}`` beside it, and declares, on each categorical column whose
  levels come from the profile or the contract, the levels the queries filter on
  (:func:`asked_levels_of`), so that ADR 0014's rule gives each its share;
* once the Simulation is written, each requirement is decided against what it declares
  (:func:`assess`), and the report says which are met and which are not. Nothing is refused for an
  unmet one.

This module imports sqlglot (through the consumer package), so it is imported only when
``--consumers`` is given; without the ``[sql]`` extra the command refuses with the install line.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml

from flux_engine.consumers.errors import FLX_3637
from flux_engine.core.errors import FluxError
from flux_engine.seam.consumers import (
    ConsumerRegistry,
    check_consumer_columns,
    read_consumers_block,
)
from flux_engine.seam.contract import LogicalKind, compile_expose
from flux_engine.seam.coverage import printable
from flux_engine.seam.keys import ContractKeys, declared_exposes
from flux_engine.seam.links import LinkEntry
from flux_engine.seam.resolve import read_bundle_file
from flux_engine.sinks._optional import require
from flux_stats.exact_yaml import load_documents

__all__ = [
    "ConsumerPlan",
    "ConsumerReport",
    "assess",
    "asked_levels_of",
    "plan_consumers",
    "read_consumers_file",
    "sentinels_of",
]


def read_consumers_file(path: Path) -> Mapping[str, Any]:
    """The ``consumers`` block *path* holds, read exactly and within bounds, or ``FLX-3637``."""
    shown = printable(str(path))
    try:
        data = read_bundle_file(path)
    except OSError as error:
        raise FluxError(
            FLX_3637, f"cannot read the consumers file {shown}: {error.strerror or error}"
        ) from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FluxError(FLX_3637, f"the consumers file {shown} is not UTF-8 text") from None
    try:
        documents = load_documents(text, name=path.name)
    except FluxError as error:
        cause = error.__cause__
        if not isinstance(cause, yaml.YAMLError):
            raise
        problem = getattr(cause, "problem", None) or type(cause).__name__
        raise FluxError(FLX_3637, f"the consumers file {shown} is not YAML: {problem}") from None
    if len(documents) != 1 or not isinstance(documents[0], dict):
        raise FluxError(
            FLX_3637,
            f"the consumers file {shown} holds {len(documents)} documents; give it one mapping "
            "with the keys of a consumers block (dialect, tables, queries, ...)",
        )
    return documents[0]


@dataclass(frozen=True)
class ConsumerPlan:
    """The consumers' questions, read and checked against a contract, and what they need."""

    #: The block as the Simulation holds it: the file's keys, with ``compiledWith``.
    block: Mapping[str, Any]
    registry: ConsumerRegistry
    requirements: Tuple[Any, ...]
    sqlglot_version: str
    #: The checked views and queries, and the schema they were checked against: what the profile
    #: request is made from (:mod:`flux_engine.consumers.ask`).
    checked: Any = None
    schema: Any = None


def plan_consumers(
    path: Path, contract: Mapping[str, Any], keys: Optional[ContractKeys]
) -> ConsumerPlan:
    """Read *path*, check every query against *contract*'s exposes, and derive the requirements.

    Needs sqlglot: without the ``[sql]`` extra it refuses with ``FLX-3501`` and the install line.
    A refusal of the block or of a query is the registry reader's or the checker's own
    ``FLX-36nn`` error, naming the key or the query and the construct.
    """
    require("sqlglot", extra="sql", feature="fluxsim init --consumers")
    from flux_engine.consumers.check import SQLGLOT_VERSION, check_registry
    from flux_engine.consumers.requirements import derive_requirements
    from flux_engine.consumers.schema import bundle_schema, expose_schema

    block = dict(read_consumers_file(path))
    exposes = declared_exposes(contract)
    registry = read_consumers_block(
        block, emits=frozenset(exposes), where=f"consumers file {printable(path.name)}"
    )
    schemas = []
    for expose_id in sorted(exposes):
        check_consumer_columns(
            registry,
            expose_id=expose_id,
            expose=exposes[expose_id],
            where=f"consumers file {printable(path.name)}",
        )
        compiled = compile_expose(exposes[expose_id])
        schemas.append(
            expose_schema(compiled, keys.expose(expose_id) if keys is not None else None)
        )
    schema = bundle_schema(schemas)
    checked = check_registry(registry, schema)
    block["compiledWith"] = {"sqlglot": SQLGLOT_VERSION}
    return ConsumerPlan(
        block=block,
        registry=registry,
        requirements=derive_requirements(checked.queries, schema),
        sqlglot_version=SQLGLOT_VERSION,
        checked=checked,
        schema=schema,
    )


def asked_levels_of(
    plan: ConsumerPlan, expose_id: str, *, skip: Iterable[str] = ()
) -> Dict[str, Tuple[Tuple[str, Tuple[str, ...]], ...]]:
    """Per column of *expose_id*, the levels its queries filter on, each with the query ids.

    A column in *skip* (one a clique of the profile draws, whose counts hold no other level) is
    left out: its level stays unmet. The levels of a column are sorted, and so are the ids.
    """
    left = frozenset(skip)
    found: Dict[str, Dict[str, set]] = {}
    for requirement in plan.requirements:
        if (
            requirement.kind != "level"
            or requirement.expose != expose_id
            or requirement.column in left
        ):
            continue
        found.setdefault(requirement.column, {}).setdefault(requirement.values[0], set()).add(
            requirement.query
        )
    return {
        column: tuple((level, tuple(sorted(queries))) for level, queries in sorted(levels.items()))
        for column, levels in found.items()
    }


def sentinels_of(links: Iterable[LinkEntry]) -> Dict[Tuple[str, str], Tuple[str, ...]]:
    """The texts links write in place of a missing parent, by ``(exposeId, column)``."""
    found: Dict[Tuple[str, str], List[str]] = {}
    for entry in links:
        for column, text in entry.sentinels:
            found.setdefault((entry.emit, column), []).append(text)
    return {key: tuple(texts) for key, texts in found.items()}


@dataclass(frozen=True)
class ConsumerReport:
    """What ``init`` made of the requirements, for its report."""

    queries: int
    sqlglot_version: str
    assessments: Tuple[Any, ...]

    def counts(self) -> Dict[str, int]:
        found = {"met": 0, "unmet": 0, "verify": 0}
        for each in self.assessments:
            found[each.status] += 1
        return found

    def lines(self) -> List[str]:
        """The report: a summary line, then each requirement as met, unmet or left to verify."""
        counts = self.counts()
        total = len(self.assessments)
        noun = "requirement" if total == 1 else "requirements"
        queries = "query" if self.queries == 1 else "queries"
        lines = [
            f"consumers: {self.queries} {queries} read with sqlglot {self.sqlglot_version}, "
            f"{total} {noun} -- {counts['met']} met, {counts['unmet']} unmet, "
            f"{counts['verify']} left to verify"
        ]
        labels = {"met": "met", "unmet": "unmet", "verify": "to verify"}
        for each in self.assessments:
            requirement = each.requirement
            lines.append(
                f"  {labels[each.status]:<9} {printable(requirement.query)}: "
                f"{printable(requirement.expose)}.{printable(requirement.column)} needs "
                f"{printable(requirement.describe())} -- {each.note}"
            )
        return lines

    def as_document(self) -> Dict[str, Any]:
        return {
            "queries": self.queries,
            "sqlglot": self.sqlglot_version,
            "requirements": [
                {
                    "query": each.requirement.query,
                    "exposeId": each.requirement.expose,
                    "column": each.requirement.column,
                    "kind": each.requirement.kind,
                    "values": list(each.requirement.values),
                    "status": each.status,
                    "note": each.note,
                }
                for each in self.assessments
            ],
            "counts": self.counts(),
        }


def _shown_kind(kind: LogicalKind) -> str:
    """The kind a requirement is judged against: ``bool``, ``number``, ``date``, ``timestamp`` or ``string``."""
    if kind is LogicalKind.BOOL:
        return "bool"
    if kind in (LogicalKind.INT, LogicalKind.DECIMAL):
        return "number"
    if kind is LogicalKind.DATE:
        return "date"
    if kind is LogicalKind.TIMESTAMP:
        return "timestamp"
    return "string"


def _declared_span(declared: Any) -> Optional[Tuple[str, str]]:
    """The ``(low, high)`` of a declared range, or ``None`` when it does not give both."""
    if isinstance(declared, Mapping) and "min" in declared and "max" in declared:
        return (str(declared["min"]), str(declared["max"]))
    return None


def _facts_of(plan: Any, kind: LogicalKind, context: Any) -> Any:
    from flux_engine.consumers.requirements import ColumnFacts

    declaration = plan.declaration
    column = context.column
    numeric = kind in (LogicalKind.INT, LogicalKind.DECIMAL)
    temporal = kind in (LogicalKind.DATE, LogicalKind.TIMESTAMP)
    levels = declaration.get("levels")
    values = tuple(str(level) for level in levels) if (numeric or temporal) and levels else ()
    names = tuple(plan.level_names) if plan.level_names is not None else None
    return ColumnFacts(
        kind=_shown_kind(kind),
        levels=names if not (numeric or temporal) else None,
        enum=tuple(column.enum_values) if column.enum_values else None,
        values=values + ((names or ()) if numeric else ()),
        draws_nulls=_draws_nulls(plan, context),
        range=_declared_span(declaration.get("range")),
    )


def _draws_nulls(plan: Any, context: Any) -> bool:
    declared = plan.declaration.get("nullFraction")
    if declared is not None:
        try:
            return Decimal(str(declared)) > 0
        except InvalidOperation:
            return False
    metrics = context.entry.get("metrics") if isinstance(context.entry, Mapping) else None
    nulls = metrics.get("nullCount") if isinstance(metrics, Mapping) else None
    count = nulls.get("value") if isinstance(nulls, Mapping) else None
    published = isinstance(nulls, Mapping) and nulls.get("status") == "F"
    return (
        published
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count > 0
        and not context.column.required
    )


def assess(
    plan: ConsumerPlan,
    authored: Sequence[Any],
    *,
    window: Optional[Tuple[str, str]] = None,
    links: Iterable[LinkEntry] = (),
) -> ConsumerReport:
    """Decide each requirement of *plan* against the exposes ``init`` has just declared.

    *authored* is the list of :class:`~flux_engine.author.bundle.AuthoredExpose` the Simulation
    was written from; *window* is the run's time window; *links* are the link entries the Simulation
    declares (none, for a freshly written one).
    """
    from flux_engine.consumers.requirements import assess_requirements

    facts: Dict[Tuple[str, str], Any] = {}
    for expose in authored:
        if expose.skipped is not None or expose.reference:
            continue
        for column in expose.columns:
            context = expose.contexts.get(column.name)
            kind = expose.kinds.get(column.name)
            if context is not None and kind is not None:
                facts[(expose.expose_id, column.name)] = _facts_of(column, kind, context)
    return ConsumerReport(
        queries=len(plan.registry.queries),
        sqlglot_version=plan.sqlglot_version,
        assessments=assess_requirements(
            plan.requirements, facts, sentinels=sentinels_of(links), window=window
        ),
    )
