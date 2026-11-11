# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §8 and SEAM-FIDELITY-SPEC §3: the stream, its digest, H and G.

The ``outputDigest`` test does not call the implementation twice and check
it agrees with itself. It recomputes §8's formula from the specification
text, with its own ``hashlib`` call and its own framing, and asserts the
implementation matches. A transcription error in either is a failure.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any, Dict, List, Mapping

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.digest import DigestDomain, DigestRef, digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import digest_bytes, seam_bytes
from flux_engine.seam.contract import CompiledExpose
from flux_engine.seam.emit import (
    EMIT_LABEL,
    EmitWriter,
    emit_header_document,
    output_digest,
    row_line,
    seam_digest,
    seam_digests,
    seam_entropy_bytes,
    seam_reusable_digest,
)
from flux_engine.seam.resolve import ExactLoader, ResolvedEmit
from flux_engine.seam.sinks import NullSink

from .conftest import expose_from_schema, minimal_contract, minimal_emit

pytestmark = pytest.mark.unit


@pytest.fixture
def expose() -> CompiledExpose:
    return expose_from_schema(
        [
            {"name": "id", "type": "STRING", "required": True},
            {"name": "amount", "type": "DECIMAL(18,4)"},
            {"name": "seen", "type": "BOOL"},
        ]
    )


@pytest.fixture
def contract_digest() -> Any:
    return digest_of_document({"id": "test.gold.widget"}, subject="contractDigest")


ROWS: List[Dict[str, Any]] = [
    {"id": "a", "amount": Decimal("1.5"), "seen": True},
    {"id": "b", "amount": None, "seen": False},
    {"id": "c", "amount": 0, "seen": None},
]


# ---------------------------------------------------------------------------
# §8's header and framing
# ---------------------------------------------------------------------------


def test_the_header_carries_exactly_the_four_fields_the_spec_names(
    expose: CompiledExpose, contract_digest: Any
) -> None:
    header = emit_header_document(expose, contract_digest)
    assert sorted(header) == ["columns", "contractDigest", "exposeId", "types"]
    assert header["columns"] == ["id", "amount", "seen"]
    assert header["types"] == ["STRING", "DECIMAL(18,4)", "BOOL"]
    assert header["contractDigest"] == contract_digest.value


def test_a_row_line_is_a_json_array_and_a_newline() -> None:
    assert row_line(["a", "1.5000", True]) == b'["a","1.5000",true]\n'
    assert row_line([]) == b"[]\n"


def test_a_row_line_never_contains_a_newline_or_a_nul_inside_it() -> None:
    """The framing has no ambiguous concatenation because JCS emits neither."""
    line = row_line(["a\nb", "c\x00d", "e\td"])
    assert line.count(b"\n") == 1
    assert line.endswith(b"\n")
    assert b"\x00" not in line


# ---------------------------------------------------------------------------
# outputDigest, recomputed from the specification text
# ---------------------------------------------------------------------------


def _output_digest_from_the_spec(header: Mapping[str, Any], rows: List[Any]) -> str:
    """KERNEL-SPEC §8's formula, transcribed independently of the module."""
    lines = b"".join(canonical_bytes(list(row)) + b"\n" for row in rows)
    material = b"flux-emit/1" + b"\x00" + canonical_bytes(header) + b"\x00" + lines
    return "sha256:" + hashlib.sha256(material).hexdigest()


def test_the_output_digest_matches_an_independent_transcription_of_the_formula(
    expose: CompiledExpose, contract_digest: Any
) -> None:
    writer = EmitWriter(expose=expose, contract_digest=contract_digest)
    with writer:
        rendered = [writer.write(row) for row in ROWS]
    assert writer.result().output_digest.value == _output_digest_from_the_spec(
        emit_header_document(expose, contract_digest), rendered
    )


def test_the_output_digest_is_in_the_ndjson_stream_domain(
    expose: CompiledExpose, contract_digest: Any
) -> None:
    writer = EmitWriter(expose=expose, contract_digest=contract_digest)
    with writer:
        writer.write_all(ROWS)
    assert writer.result().output_digest.domain is DigestDomain.JCS_NDJSON_STREAM


def test_the_label_is_part_of_the_pre_image(expose: CompiledExpose, contract_digest: Any) -> None:
    header = emit_header_document(expose, contract_digest)
    with_label = output_digest(header, [], subject="s").value
    without_label = (
        "sha256:" + hashlib.sha256(b"\x00" + canonical_bytes(header) + b"\x00").hexdigest()
    )
    assert EMIT_LABEL == b"flux-emit/1"
    assert with_label != without_label


def test_an_empty_stream_still_has_a_digest(expose: CompiledExpose, contract_digest: Any) -> None:
    writer = EmitWriter(expose=expose, contract_digest=contract_digest)
    with writer:
        pass
    result = writer.result()
    assert result.rows == 0
    assert result.output_digest.value.startswith("sha256:")


def test_reordering_the_declared_columns_moves_the_digest(contract_digest: Any) -> None:
    """§8's whole reason for arrays: an object would erase the column order."""
    schema = [{"name": "a", "type": "STRING"}, {"name": "b", "type": "STRING"}]
    forward = expose_from_schema(schema)
    backward = expose_from_schema(list(reversed(schema)))
    row = {"a": "1", "b": "2"}

    digests = []
    for compiled in (forward, backward):
        writer = EmitWriter(expose=compiled, contract_digest=contract_digest)
        with writer:
            writer.write(row)
        digests.append(writer.result().output_digest.value)
    assert digests[0] != digests[1]


def test_the_contract_digest_is_inside_the_output_digest(expose: CompiledExpose) -> None:
    """A stream is answerable to a contract, and the digest says which."""
    first = digest_of_document({"id": "one"}, subject="c")
    second = digest_of_document({"id": "two"}, subject="c")
    digests = []
    for contract in (first, second):
        writer = EmitWriter(expose=expose, contract_digest=contract)
        with writer:
            writer.write_all(ROWS)
        digests.append(writer.result().output_digest.value)
    assert digests[0] != digests[1]


@given(rows=st.lists(st.lists(st.integers(min_value=-100, max_value=100), min_size=1, max_size=1)))
def test_the_digest_is_a_function_of_the_rows_alone(rows: List[List[int]]) -> None:
    """Two writers over one contract and one row list agree, always."""
    compiled = expose_from_schema([{"name": "n", "type": "INT"}])
    contract = digest_of_document({"id": "c"}, subject="c")
    values = []
    for _ in range(2):
        writer = EmitWriter(expose=compiled, contract_digest=contract)
        with writer:
            writer.write_all(rows)
        values.append(writer.result().output_digest.value)
    assert values[0] == values[1]


# ---------------------------------------------------------------------------
# The writer's lifecycle
# ---------------------------------------------------------------------------


def test_writing_before_open_is_refused(expose: CompiledExpose, contract_digest: Any) -> None:
    writer = EmitWriter(expose=expose, contract_digest=contract_digest)
    with pytest.raises(FluxError) as error:
        writer.write(ROWS[0])
    assert error.value.code.code == "FLX-3301"


def test_opening_twice_is_refused(expose: CompiledExpose, contract_digest: Any) -> None:
    writer = EmitWriter(expose=expose, contract_digest=contract_digest).open()
    with pytest.raises(FluxError) as error:
        writer.open()
    assert error.value.code.code == "FLX-3301"


def test_writing_after_close_is_refused(expose: CompiledExpose, contract_digest: Any) -> None:
    writer = EmitWriter(expose=expose, contract_digest=contract_digest).open()
    writer.close()
    with pytest.raises(FluxError) as error:
        writer.write(ROWS[0])
    assert error.value.code.code == "FLX-3301"


def test_closing_before_open_is_refused(expose: CompiledExpose, contract_digest: Any) -> None:
    writer = EmitWriter(expose=expose, contract_digest=contract_digest)
    with pytest.raises(FluxError) as error:
        writer.close()
    assert error.value.code.code == "FLX-3301"


def test_closing_twice_returns_the_same_result(
    expose: CompiledExpose, contract_digest: Any
) -> None:
    writer = EmitWriter(expose=expose, contract_digest=contract_digest).open()
    writer.write_all(ROWS)
    assert writer.close() == writer.close()


def test_the_sink_is_closed_even_when_the_body_raises(
    expose: CompiledExpose, contract_digest: Any
) -> None:
    """§8's sink contract: "close() runs even when the run fails"."""
    sink = NullSink()
    writer = EmitWriter(expose=expose, contract_digest=contract_digest, sink=sink)
    with pytest.raises(RuntimeError):
        with writer:
            writer.write(ROWS[0])
            raise RuntimeError("the run failed")
    assert sink.closed is True
    assert sink.rows == 1


def test_a_writer_that_retains_nothing_refuses_to_hand_back_lines(
    expose: CompiledExpose, contract_digest: Any
) -> None:
    writer = EmitWriter(expose=expose, contract_digest=contract_digest, retain_lines=False).open()
    writer.write_all(ROWS)
    digest = writer.close().output_digest
    with pytest.raises(FluxError) as error:
        writer.lines()
    assert error.value.code.code == "FLX-3301"
    assert digest.value.startswith("sha256:")


def test_the_retained_lines_are_the_digest_pre_image(
    expose: CompiledExpose, contract_digest: Any
) -> None:
    writer = EmitWriter(expose=expose, contract_digest=contract_digest)
    with writer:
        writer.write_all(ROWS)
    header = canonical_bytes(emit_header_document(expose, contract_digest))
    material = EMIT_LABEL + b"\x00" + header + b"\x00" + writer.stream_bytes()
    assert writer.result().output_digest.value == "sha256:" + hashlib.sha256(material).hexdigest()


# ---------------------------------------------------------------------------
# H and G (SEAM-FIDELITY-SPEC §3)
# ---------------------------------------------------------------------------

FIDELITY_BLOCK: Dict[str, Any] = {
    "encoding": {"fractionalBits": 16, "rounding": "floor"},
    "records": {"min": 1000, "max": 100000},
    "columns": {"amount": {"range": {"min": "0", "max": "10000"}, "null": "exclude"}},
    "properties": [
        {
            "property": "mean",
            "column": "amount",
            "tolerance": {"type": "relative", "value": "0.02"},
        },
        {
            "property": "correlation",
            "columns": ["amount", "seen"],
            "minVariance": "0.01",
            "tolerance": {"type": "absolute", "value": "0.05"},
        },
    ],
    "budget": {"maxChecksPerState": 20, "maxStateAge": "P30D"},
    "failureAction": "withholdBundle",
}


def _extension_with_fidelity(**changes: Any) -> Dict[str, Any]:
    """The emit's entry in the document-level extension map, block included.

    SEAM-FIDELITY-SPEC §2 puts the block there, not in the ``emits[]``
    entry, so this is what ``H`` and ``G`` hash under ``"extension"``.
    """
    block = yaml.safe_load(yaml.safe_dump(FIDELITY_BLOCK))
    for path, value in changes.items():
        cursor: Any = block
        parts = path.split(".")
        for part in parts[:-1]:
            cursor = cursor[int(part)] if part.isdigit() else cursor[part]
        cursor[parts[-1]] = value
    return {"fidelity": block}


def test_h_covers_the_contract_the_emit_and_the_columns() -> None:
    contract = minimal_contract()
    emit = minimal_emit()
    columns = ("widget_id", "amount")
    expected = digest_of_document(
        {"contract": contract, "emit": emit, "columns": list(columns)}, subject="H"
    )
    assert seam_digest(contract, emit, columns).value == expected.value


def test_h_is_stable_under_yaml_key_reordering() -> None:
    """A re-ordered YAML file is the same contract, so it is the same H."""
    contract = minimal_contract()
    reordered = yaml.load(
        yaml.safe_dump({key: contract[key] for key in reversed(list(contract))}),
        Loader=ExactLoader,
    )
    assert reordered != contract or list(reordered) != list(contract)
    emit = minimal_emit()
    columns = ("widget_id", "amount")
    assert seam_digest(contract, emit, columns) == seam_digest(reordered, emit, columns)


def test_h_is_stable_under_nfd_and_nfc_spellings() -> None:
    """Two spellings of one word are one word (KERNEL-SPEC §2.1)."""
    composed = "café"
    decomposed = "café"
    assert composed != decomposed
    columns = ("widget_id",)
    composed_contract = minimal_contract(product_id=f"test.{composed}")
    decomposed_contract = minimal_contract(product_id=f"test.{decomposed}")
    assert seam_digest(composed_contract, minimal_emit(exposeId=composed), columns) == seam_digest(
        decomposed_contract, minimal_emit(exposeId=decomposed), columns
    )


def test_h_moves_when_the_column_set_moves() -> None:
    contract = minimal_contract()
    emit = minimal_emit()
    assert seam_digest(contract, emit, ("a", "b")) != seam_digest(contract, emit, ("b", "a"))
    assert seam_digest(contract, emit, ("a",)) != seam_digest(contract, emit, ("a", "b"))


def test_h_moves_when_the_contract_moves() -> None:
    """KERNEL-SPEC §3: "Changing the contract changes every stream"."""
    emit = minimal_emit()
    columns = ("widget_id", "amount")
    original = minimal_contract()
    changed = minimal_contract()
    changed["exposes"][0]["contract"]["schema"].append({"name": "extra", "type": "INT"})
    assert seam_digest(original, emit, columns) != seam_digest(changed, emit, columns)


@pytest.mark.parametrize(
    "change",
    [
        {"properties.0.tolerance": {"type": "relative", "value": "0.99"}},
        {"properties.1.minVariance": "0.5"},
        {"budget.maxStateAge": "P90D"},
    ],
)
def test_the_excluded_set_moves_h_and_leaves_g_alone(change: Dict[str, Any]) -> None:
    """SEAM-FIDELITY-SPEC §8, row 1: the committed statistic stays reusable."""
    contract = minimal_contract()
    columns = ("widget_id", "amount")
    emit = minimal_emit()
    before = seam_digests(contract, emit, columns, _extension_with_fidelity())
    after = seam_digests(contract, emit, columns, _extension_with_fidelity(**change))
    assert before.h != after.h
    assert before.g == after.g


@pytest.mark.parametrize(
    "change",
    [
        {"records.min": 2000},
        {"encoding.fractionalBits": 24},
        {"budget.maxChecksPerState": 5},
        {"failureAction": "warn"},
        {"properties.0.column": "widget_id"},
    ],
)
def test_any_other_change_moves_g_too(change: Dict[str, Any]) -> None:
    """SEAM-FIDELITY-SPEC §8, row 2: a new committed state is required."""
    contract = minimal_contract()
    columns = ("widget_id", "amount")
    emit = minimal_emit()
    before = seam_digests(contract, emit, columns, _extension_with_fidelity())
    after = seam_digests(contract, emit, columns, _extension_with_fidelity(**change))
    assert before.h != after.h
    assert before.g != after.g


def test_g_is_h_when_the_emit_declares_none_of_the_excluded_set() -> None:
    contract = minimal_contract()
    emit = minimal_emit()
    digests = seam_digests(contract, emit, ("widget_id",))
    assert digests.h == digests.g


def test_an_emit_with_no_extension_entry_keeps_the_digest_it_always_had() -> None:
    """§3: the ``extension`` key is present only when the emit has an entry.

    A bundle that declares no block keeps its ``H``, its seed root and the
    whole stream it had before the block existed -- which is what lets the
    shipped example go on producing the rows it produced.
    """
    contract = minimal_contract()
    emit = minimal_emit()
    columns = ("widget_id", "amount")
    without = seam_digest(contract, emit, columns)
    expected = digest_of_document(
        {"contract": contract, "emit": emit, "columns": list(columns)}, subject="H"
    )
    assert without.value == expected.value
    assert seam_digest(contract, emit, columns, extension={}).value != without.value


def test_a_max_state_age_outside_a_budget_block_is_left_alone() -> None:
    """§3's excluded set names ``budget.maxStateAge``, not every key so named."""
    contract = minimal_contract()
    columns = ("widget_id",)
    plain = minimal_emit(fidelity={"records": {"min": 1}})
    elsewhere = minimal_emit(fidelity={"records": {"min": 1, "maxStateAge": "P1D"}})
    assert (
        seam_reusable_digest(contract, plain, columns).value
        != seam_reusable_digest(contract, elsewhere, columns).value
    )


# ---------------------------------------------------------------------------
# H_bytes: the entropy the seed tree takes
# ---------------------------------------------------------------------------


def test_a_single_emit_contributes_its_own_digest_bytes() -> None:
    digests = seam_digests(minimal_contract(), minimal_emit(), ("widget_id",))
    assert digests.entropy_bytes == digest_bytes(digests.h.value)
    assert seam_entropy_bytes([digests]) == seam_bytes([digests.h.value])


def test_several_emits_fold_in_declared_order() -> None:
    """§3: "the sha256 of the concatenation of the per-emit digests in order"."""
    contract = minimal_contract()
    first = seam_digests(contract, minimal_emit(exposeId="one"), ("a",))
    second = seam_digests(contract, minimal_emit(exposeId="two"), ("b",))
    assert seam_entropy_bytes([first, second]) == seam_bytes([first.h.value, second.h.value])
    assert seam_entropy_bytes([first, second]) != seam_entropy_bytes([second, first])


# ---------------------------------------------------------------------------
# The shipped example
# ---------------------------------------------------------------------------


def test_the_telco_seam_digests_are_stable_and_h_equals_g(
    telco_emit: ResolvedEmit, telco_expose: CompiledExpose
) -> None:
    """The shipped emit declares none of §3's excluded set, so H is G."""
    digests = seam_digests(telco_emit.contract_document, telco_emit.emit, telco_expose.column_names)
    assert digests.h.domain is DigestDomain.JCS_DOCUMENT
    assert digests.h == digests.g
    again = seam_digests(telco_emit.contract_document, telco_emit.emit, telco_expose.column_names)
    assert again.h == digests.h


def test_the_writer_reports_what_it_has_written(
    expose: CompiledExpose, contract_digest: DigestRef
) -> None:
    """``rows`` and ``closed`` are the writer's own account of the stream."""
    writer = EmitWriter(expose=expose, contract_digest=contract_digest)
    assert writer.rows == 0 and writer.closed is False
    writer.open()
    for index, row in enumerate(ROWS, start=1):
        writer.write(row)
        assert writer.rows == index
    assert writer.closed is False
    result = writer.close()
    assert writer.closed is True
    assert writer.rows == len(ROWS) == result.rows
