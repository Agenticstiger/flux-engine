# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The consumer SQL check at ``validate`` and ``plan`` (ADR 0028 section 2).

A query that cannot be checked is refused when the bundle is read, not found by ``verify`` after a
whole apply. :func:`check_consumer_sql` runs the section 2 check on every view and query a
Simulation declares, against the schema of the exposes the run emits: parse, bounds, allowlist,
table and column resolution, placeholders and the total-order rule. No DuckDB runs, so it needs
sqlglot only.

Without sqlglot (the ``[sql]`` extra) the bundle is not refused: :func:`consumers_unchecked` says
so, and the caller reports that the queries were not checked, with the install command.

The result is kept for the last registry and schema it checked, so that a process that validates,
plans and verifies one bundle checks each text once.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.queries import INSTALL_COMMAND
from flux_engine.consumers.errors import FLX_3624, FLX_3635
from flux_engine.core.errors import FluxError
from flux_engine.seam.consumers import ConsumerRegistry
from flux_engine.seam.resolve import ResolvedEmit, declared_consumers
from flux_engine.sinks._optional import is_available

if TYPE_CHECKING:
    from flux_engine.consumers.check import RegistryCheck
    from flux_engine.consumers.schema import BundleSchema

__all__ = [
    "check_consumer_sql",
    "checked_registry",
    "consumers_unchecked",
    "schema_of_emits",
    "unchecked_line",
]

#: The last check: the registry and schema it read, and what it found.
_last: Optional[Tuple[ConsumerRegistry, "BundleSchema", "RegistryCheck"]] = None


def schema_of_emits(resolved: Sequence[ResolvedEmit]) -> "BundleSchema":
    """The schema of the exposes *resolved* writes, as the checker reads it.

    A hidden table is generated and read through links but never written, so no query can read
    it and it is left out.
    """
    from flux_engine.consumers.schema import bundle_schema, expose_schema

    return bundle_schema(
        expose_schema(emit.compile(), emit.keys) for emit in resolved if not emit.is_hidden_table
    )


def checked_registry(registry: ConsumerRegistry, schema: "BundleSchema") -> "RegistryCheck":
    """Every view and query of *registry* checked against *schema*; the last result is reused."""
    global _last
    if _last is not None and _last[0] == registry and _last[1] == schema:
        return _last[2]
    from flux_engine.consumers.check import check_registry

    found = check_registry(registry, schema)
    _last = (registry, schema, found)
    return found


def consumers_unchecked(simulation: Mapping[str, Any], *, where: str) -> bool:
    """Whether the Simulation declares consumer queries that cannot be checked here (no sqlglot)."""
    if declared_consumers(simulation, where=where) is None:
        return False
    return not is_available("sqlglot")


def unchecked_line() -> str:
    """The sentence that says the consumer queries were not checked, and what installs the check."""
    return (
        "consumer queries were not checked: they need sqlglot "
        f"({INSTALL_COMMAND}); verify reports the queries dimension as not measured"
    )


def check_consumer_sql(
    simulation: Mapping[str, Any], resolved: Sequence[ResolvedEmit], *, where: str
) -> None:
    """Refuse a view or query of the Simulation's ``consumers`` block that the check refuses.

    Raises the checker's own :class:`~flux_engine.core.errors.FluxError`, which names the query or
    view and the construct. Does nothing when the Simulation declares no consumers, or when sqlglot
    is not installed (see :func:`consumers_unchecked`).
    """
    registry = declared_consumers(simulation, where=where)
    if registry is None or not is_available("sqlglot"):
        return
    try:
        checked_registry(registry, schema_of_emits(resolved))
    except FluxError as exc:
        # A table that does not resolve, or a registry entry that maps a name to an expose the
        # schema lacks: either may be a hidden table, which is named when it is.
        if exc.code.code not in (FLX_3624.code, FLX_3635.code):
            raise
        hidden = sorted(emit.expose_id for emit in resolved if emit.is_hidden_table)
        named = [
            name
            for name in hidden
            if re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", exc.detail, re.IGNORECASE)
        ]
        if not named:
            raise
        raise FluxError(
            FLX_3624,
            f"{exc.detail}; {', '.join(named)} is a hidden table (generated and read through "
            "links, never written), so no query or view can read it",
        ) from None
