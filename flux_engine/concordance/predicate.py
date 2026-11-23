# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The fidelity-block grammar, SEAM-FIDELITY-SPEC §2.

The grammar is :mod:`flux_stats.fidelity`'s (ADR 0009), so a profiler reads a
Simulation's fidelity block with the parser the engine uses. Every name here is
an import of the same object, so no engine module or test that imports it
changes. The key tuples a test reads (``_BLOCK_KEYS`` and the like) stay
importable for the same reason.
"""

from __future__ import annotations

from flux_stats.fidelity import (
    _BLOCK_KEYS,  # noqa: F401
    _PROPERTY_KEYS,  # noqa: F401
    _RANGE_KEYS,  # noqa: F401
    _RECORDS_KEYS,  # noqa: F401
    _TOLERANCE_KEYS,  # noqa: F401
    COLUMN_KEYS,
    SUPPORT_DECLARED,
    SUPPORT_VALUES_KEY,
    Budget,
    ColumnDeclaration,
    ColumnKind,
    DeclaredExpose,
    Encoding,
    ExposeSchema,
    FailureAction,
    FidelityBlock,
    NullPolicy,
    PropertyDeclaration,
    PropertyKind,
    RecordsRange,
    Tolerance,
    ToleranceKind,
    ValueDomain,
    expose_from_contract,
    parse_duration_ms,
    parse_fidelity_block,
    read_support_levels,
    require_number_length,
)

__all__ = [
    "COLUMN_KEYS",
    "SUPPORT_DECLARED",
    "SUPPORT_VALUES_KEY",
    "Budget",
    "ColumnDeclaration",
    "ColumnKind",
    "DeclaredExpose",
    "Encoding",
    "ExposeSchema",
    "FidelityBlock",
    "FailureAction",
    "NullPolicy",
    "PropertyDeclaration",
    "PropertyKind",
    "RecordsRange",
    "Tolerance",
    "ToleranceKind",
    "ValueDomain",
    "expose_from_contract",
    "parse_duration_ms",
    "parse_fidelity_block",
    "read_support_levels",
    "require_number_length",
]
