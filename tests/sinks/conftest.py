# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for the sink tests: a compiled expose, its header, its rendered rows."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from flux_engine.api.emit import EmitHeader
from flux_engine.core.digest import DigestRef, digest_of_document
from flux_engine.seam.conform import render_row
from flux_engine.seam.contract import CompiledExpose, compile_expose
from flux_engine.seam.emit import emit_header_document, output_digest, row_line

CONTRACT_DIGEST: DigestRef = digest_of_document({"id": "test.sim.widget"}, subject="contractDigest")


def compiled(schema: Sequence[Mapping[str, Any]], *, expose_id: str = "widget") -> CompiledExpose:
    """Compile an ad-hoc expose from a column list."""
    return compile_expose(
        {
            "exposeId": expose_id,
            "kind": "stream",
            "contract": {"schema": list(schema)},
            "binding": {"platform": "kafka", "format": "kafka_topic", "location": {"topic": "t"}},
        }
    )


def header_of(expose: CompiledExpose, digest: DigestRef = CONTRACT_DIGEST) -> EmitHeader:
    """The EmitHeader an EmitWriter would open a sink with."""
    return EmitHeader(
        expose_id=expose.expose_id,
        columns=expose.column_names,
        types=expose.type_names,
        contract_digest=digest,
    )


def rendered(expose: CompiledExpose, rows: Sequence[Mapping[str, Any]]) -> List[List[Any]]:
    """Render *rows* exactly the way the engine's emit path does."""
    return [list(render_row(dict(row), expose, row_index=index)) for index, row in enumerate(rows)]


def expected_ref(expose: CompiledExpose, rows: Sequence[Sequence[Any]]) -> DigestRef:
    """§8's outputDigest over already-rendered *rows*, the writer's own way."""
    document = emit_header_document(expose, CONTRACT_DIGEST)
    lines = [row_line(list(row)) for row in rows]
    return output_digest(document, lines, subject=f"outputDigest:{expose.expose_id}")


def expected_digest(expose: CompiledExpose, rows: Sequence[Sequence[Any]]) -> str:
    """:func:`expected_ref`'s ``sha256:`` value."""
    return expected_ref(expose, rows).value


#: One column of every logical type, for the type-map tests.
EVERY_TYPE: Tuple[Dict[str, Any], ...] = (
    {"name": "s", "type": "STRING"},
    {"name": "i", "type": "INT"},
    {"name": "d", "type": "DECIMAL(18,4)"},
    {"name": "w", "type": "DECIMAL(50,10)"},
    {"name": "b", "type": "BOOL"},
    {"name": "day", "type": "DATE"},
    {"name": "at", "type": "TIMESTAMP"},
    {"name": "j", "type": "JSON"},
    {"name": "raw", "type": "BYTES"},
)
