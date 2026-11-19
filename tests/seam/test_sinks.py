# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §8: "the sink cannot influence" the ``outputDigest``.

The headline test is the one the specification's sentence implies: an
NDJSON sink writing a file and a null sink writing nothing produce the
same ``outputDigest`` for the same rows. If they ever differed, the digest
would be a property of where the data went rather than of what the run
produced, and a golden would move when somebody changed a destination.
"""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.api.emit import EmitSink
from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.seam.contract import CompiledExpose
from flux_engine.seam.emit import EmitWriter, row_line
from flux_engine.seam.sinks import NdjsonSink, NullSink

from .conftest import expose_from_schema

pytestmark = pytest.mark.unit


@pytest.fixture
def expose() -> CompiledExpose:
    return expose_from_schema(
        [
            {"name": "id", "type": "STRING", "required": True},
            {"name": "amount", "type": "DECIMAL(18,4)"},
        ]
    )


@pytest.fixture
def contract_digest() -> Any:
    return digest_of_document({"id": "test.gold.widget"}, subject="contractDigest")


ROWS: List[Dict[str, Any]] = [
    {"id": "a", "amount": Decimal("1.5")},
    {"id": "b", "amount": None},
    {"id": "café", "amount": Decimal("-2")},
]


def _digest_through(sink: Any, expose: CompiledExpose, contract_digest: Any, rows: Any) -> str:
    writer = EmitWriter(expose=expose, contract_digest=contract_digest, sink=sink)
    with writer:
        writer.write_all(rows)
    return writer.result().output_digest.value


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_both_sinks_yield_the_same_output_digest_for_the_same_rows(
    tmp_path: Path, expose: CompiledExpose, contract_digest: Any
) -> None:
    """§8: the digest is computed above the sink."""
    through_null = _digest_through(NullSink(), expose, contract_digest, ROWS)
    through_file = _digest_through(
        NdjsonSink(path=tmp_path / "out" / "rows.ndjson"), expose, contract_digest, ROWS
    )
    through_stream = _digest_through(NdjsonSink(stream=io.BytesIO()), expose, contract_digest, ROWS)
    through_none = _digest_through(None, expose, contract_digest, ROWS)
    assert {through_null, through_file, through_stream, through_none} == {through_null}


@given(
    rows=st.lists(
        st.fixed_dictionaries({"id": st.text(min_size=1, max_size=6)}),
        min_size=0,
        max_size=8,
    )
)
def test_the_sinks_agree_over_generated_rows(rows: List[Dict[str, str]]) -> None:
    compiled = expose_from_schema([{"name": "id", "type": "STRING", "required": True}])
    contract = digest_of_document({"id": "c"}, subject="c")
    assert _digest_through(NullSink(), compiled, contract, rows) == _digest_through(
        NdjsonSink(stream=io.BytesIO()), compiled, contract, rows
    )


# ---------------------------------------------------------------------------
# What the NDJSON sink actually writes
# ---------------------------------------------------------------------------


def test_the_file_holds_the_framed_lines_and_nothing_else(
    tmp_path: Path, expose: CompiledExpose, contract_digest: Any
) -> None:
    path = tmp_path / "nested" / "rows.ndjson"
    writer = EmitWriter(expose=expose, contract_digest=contract_digest, sink=NdjsonSink(path=path))
    with writer:
        rendered = [writer.write(row) for row in ROWS]
    assert path.read_bytes() == b"".join(row_line(row) for row in rendered)
    assert path.read_bytes() == writer.stream_bytes()


def test_the_header_is_not_written_into_the_data_file(
    tmp_path: Path, expose: CompiledExpose, contract_digest: Any
) -> None:
    path = tmp_path / "rows.ndjson"
    writer = EmitWriter(expose=expose, contract_digest=contract_digest, sink=NdjsonSink(path=path))
    with writer:
        writer.write_all(ROWS)
    text = path.read_text(encoding="utf-8")
    assert "exposeId" not in text
    assert text.count("\n") == len(ROWS)


def test_the_sink_owns_only_the_handle_it_opened(
    tmp_path: Path, expose: CompiledExpose, contract_digest: Any
) -> None:
    """A caller's stream is still open after close; the sink's own is not."""
    stream = io.BytesIO()
    sink = NdjsonSink(stream=stream)
    writer = EmitWriter(expose=expose, contract_digest=contract_digest, sink=sink)
    with writer:
        writer.write_all(ROWS)
    assert stream.closed is False
    assert stream.getvalue() == sink.written_bytes()


def test_a_sink_takes_exactly_one_destination() -> None:
    with pytest.raises(ValueError):
        NdjsonSink()
    with pytest.raises(ValueError):
        NdjsonSink(path=Path("x"), stream=io.BytesIO())


def test_the_sink_accepts_a_string_path(tmp_path: Path) -> None:
    sink = NdjsonSink(path=str(tmp_path / "rows.ndjson"))
    assert isinstance(sink.path, Path)


# ---------------------------------------------------------------------------
# The lifecycle both sinks hold
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", [lambda: NullSink(), lambda: NdjsonSink(stream=io.BytesIO())])
def test_a_sink_satisfies_the_published_protocol(build: Any) -> None:
    assert isinstance(build(), EmitSink)


@pytest.mark.parametrize("build", [lambda: NullSink(), lambda: NdjsonSink(stream=io.BytesIO())])
def test_writing_before_open_is_refused(build: Any) -> None:
    with pytest.raises(FluxError) as error:
        build().write(["a"])
    assert error.value.code.code == "FLX-3301"


@pytest.mark.parametrize("build", [lambda: NullSink(), lambda: NdjsonSink(stream=io.BytesIO())])
def test_opening_twice_is_refused(build: Any, expose: CompiledExpose, contract_digest: Any) -> None:
    from flux_engine.api.emit import EmitHeader

    header = EmitHeader(
        expose_id=expose.expose_id,
        columns=expose.column_names,
        types=expose.type_names,
        contract_digest=contract_digest,
    )
    sink = build()
    sink.open(header)
    with pytest.raises(FluxError) as error:
        sink.open(header)
    assert error.value.code.code == "FLX-3301"


@pytest.mark.parametrize("build", [lambda: NullSink(), lambda: NdjsonSink(stream=io.BytesIO())])
def test_writing_after_close_is_refused(
    build: Any, expose: CompiledExpose, contract_digest: Any
) -> None:
    from flux_engine.api.emit import EmitHeader

    sink = build()
    sink.open(
        EmitHeader(
            expose_id=expose.expose_id,
            columns=expose.column_names,
            types=expose.type_names,
            contract_digest=contract_digest,
        )
    )
    sink.close()
    with pytest.raises(FluxError) as error:
        sink.write(["a", None])
    assert error.value.code.code == "FLX-3301"


def test_closing_twice_is_harmless(tmp_path: Path) -> None:
    sink = NdjsonSink(path=tmp_path / "rows.ndjson")
    null = NullSink()
    for target in (sink, null):
        target.close()
        target.close()
    assert sink.closed and null.closed


def test_the_null_sink_counts_what_it_discards(
    expose: CompiledExpose, contract_digest: Any
) -> None:
    sink = NullSink()
    writer = EmitWriter(expose=expose, contract_digest=contract_digest, sink=sink)
    with writer:
        writer.write_all(ROWS)
    assert sink.rows == len(ROWS)
    assert sink.header is not None
    assert sink.header.columns == expose.column_names
