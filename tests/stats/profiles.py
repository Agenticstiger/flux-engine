# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Profiles for the tests, formed the way a profiler forms one.

:func:`profile_from_rows` forms ``S`` over a record set with the engine's
own statistic and writes it as a profile, so a profile and a committed
state over the same records hold the same numbers by construction, and a
test can compare the profile path with the slice path directly. The records
are the synthetic ones the tests already use; nothing here reads real data.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping, Optional, Sequence

from flux_engine.concordance.boundary.statistic import compute_statistic, level_cells
from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.predicate import ColumnKind, PropertyKind, ValueDomain
from flux_stats import definition_for_property
from flux_stats.profile import SCHEMA_URL, SCHEMA_URL_1_1_0
from flux_stats.spelling import render_exact

PRODUCER = "urn:example:flux-test-profiler"


def domain_of(kind: ColumnKind, domain: ValueDomain) -> str:
    if kind is ColumnKind.SUPPORT:
        return "category"
    return "timestamp" if domain is ValueDomain.TIMESTAMP else "number"


def profile_from_rows(
    rows: Sequence[Mapping[str, Any]],
    compiled: CompiledFidelity,
    *,
    fractional_bits: Optional[int] = None,
) -> Dict[str, Any]:
    """A profile of *rows* under *compiled*'s declarations, every cell published.

    It names format ``1-1-0`` when a property is a quantile with bins
    (``quantile/2``, ADR 0012) and ``1-0-0`` otherwise.
    """
    statistic = compute_statistic(rows, compiled)
    block = compiled.block
    columns: Dict[str, Dict[str, Any]] = {
        name: {"domain": domain_of(column.kind, column.domain), "statistics": []}
        for name, column in block.columns.items()
    }
    pairs: Dict[tuple, List[Dict[str, Any]]] = {}
    for entry in compiled.properties:
        declared = entry.declaration
        formed = statistic.properties[entry.property_id]
        statistic_definition = definition_for_property(declared)
        document: Dict[str, Any] = {
            "definition": statistic_definition.identifier,
            "sampleSize": {"min": len(rows), "max": len(rows)},
            "disclosure": "none",
            "groups": [
                dict(group.to_document(statistic_definition), status="F") for group in formed.groups
            ],
        }
        if declared.group_by is not None:
            document["groupBy"] = declared.group_by
        if declared.bins:
            document["bins"] = [render_exact(edge) for edge in declared.bins]
        if declared.kind is PropertyKind.LEVEL_HISTOGRAM:
            document["levels"] = list(level_cells(block.columns[declared.columns[0]]))
        if declared.level is not None:
            document["level"] = render_exact(declared.level)
        if declared.kind is PropertyKind.CORRELATION:
            pairs.setdefault(tuple(declared.columns), []).append(document)
        else:
            columns[declared.columns[0]]["statistics"].append(document)
    binned = any(definition_for_property(entry.declaration).binned for entry in compiled.properties)
    return {
        "_producer": PRODUCER,
        "_schemaURL": SCHEMA_URL_1_1_0 if binned else SCHEMA_URL,
        "encoding": {
            "fractionalBits": (
                block.encoding.fractional_bits if fractional_bits is None else fractional_bits
            ),
            "rounding": "floor",
        },
        "disclosureRules": {
            "none": {},
            "acro-default": {
                "minimumCell": 10,
                "zerosDisclosive": True,
                "roundingBase": 5,
                "dominance": {"n": 2, "k": "0.90"},
                "pRatio": "0.10",
            },
        },
        "tables": {
            block.expose_id: {
                "rowCount": len(rows),
                "columns": columns,
                "columnPairs": [
                    {"columns": list(pair), "statistics": found} for pair, found in pairs.items()
                ],
            }
        },
    }


def statistic_of(
    profile: Dict[str, Any], expose_id: str, column: str, identifier: str
) -> Dict[str, Any]:
    """The first statistic of *column* with *identifier*, for a test to edit in place."""
    for found in profile["tables"][expose_id]["columns"][column]["statistics"]:
        if found["definition"] == identifier:
            return found
    raise KeyError(identifier)


def edited(profile: Dict[str, Any]) -> Dict[str, Any]:
    """A deep copy, so a test can break one thing without touching the fixture."""
    return copy.deepcopy(profile)
