# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for the sink tests: a compiled expose, its header, its rendered rows."""

from __future__ import annotations

from types import ModuleType
from typing import Any, Dict, Iterator, List, Mapping, Sequence, Tuple

import numpy
import pytest

from flux_engine.api.emit import EmitHeader
from flux_engine.core.digest import DigestRef, digest_of_document
from flux_engine.seam.conform import render_row
from flux_engine.seam.contract import CompiledExpose, compile_expose
from flux_engine.seam.emit import emit_header_document, output_digest, row_line

#: One explicit credential for each cloud, so a test that builds an object store target (always
#: with the store constructor replaced) is not refused for having none. The tests of that refusal
#: (tests/sinks/test_no_metadata_probe.py) clear these.
TEST_CREDENTIALS: Mapping[str, str] = {
    "AWS_ACCESS_KEY_ID": "test-access-key-id",
    "AWS_SECRET_ACCESS_KEY": "test-secret-access-key",
    "GOOGLE_BEARER_TOKEN": "test-bearer-token",
    "AZURE_STORAGE_ACCOUNT_KEY": "dGVzdC1hY2NvdW50LWtleQ==",
}


@pytest.fixture(autouse=True)
def _explicit_test_credentials(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in TEST_CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    yield


#: What a name resolves to in the tests: ``localhost`` is this machine, anything else does not
#: resolve. A test never reads the machine's hosts file or asks a resolver.
RESOLVED: Dict[str, List[str]] = {"localhost": ["127.0.0.1", "::1"]}


@pytest.fixture(autouse=True)
def _fixed_resolver(monkeypatch: pytest.MonkeyPatch) -> Iterator[Dict[str, List[str]]]:
    """Replace the resolver the Kafka sink's loopback check uses with a table the test may edit."""
    from flux_engine.sinks import _kafka_broker

    table = dict(RESOLVED)

    def resolve(name: str) -> List[str]:
        if name not in table:
            raise OSError(f"{name} does not resolve")
        return list(table[name])

    monkeypatch.setattr(_kafka_broker, "_resolve", resolve)
    yield table


CONTRACT_DIGEST: DigestRef = digest_of_document({"id": "test.sim.widget"}, subject="contractDigest")


def pyarrow_or_skip() -> ModuleType:
    """pyarrow, or a skip of the calling module or test where this NumPy is too old for it.

    pyarrow 26 raises ``ImportError`` at import on NumPy 1.x, and its wheel does
    not declare the requirement, so pip installs the pair without a word. The
    nightly's determinism cells pin NumPy 1.26.0 over an install that carries
    pyarrow and collect the whole tree; pytest 9.1 skips only on
    ``ModuleNotFoundError``, so these modules errored at collection and the
    step that holds the pinned digest set exited non-zero.

    Below NumPy 2 any ``ImportError`` is a skip. From NumPy 2 on, only a missing
    pyarrow is: one that is installed and will not import there is a broken
    environment, and a skip would read as a pass. Both calls name ``exc_type``,
    because before pytest 9.1 the default skipped any ``ImportError`` too, with
    a warning.
    """
    too_old = int(numpy.__version__.partition(".")[0]) < 2
    return pytest.importorskip("pyarrow", exc_type=ImportError if too_old else ModuleNotFoundError)


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
