# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""End to end against the FLUX spec repo's shipped telco bundle.

The fidelity block is an additive RFC field, so the shipped
``simulation.flux.yml`` does not carry one -- but its emit entry and the
FLUID contract it references are real, and everything this module asserts
about them is read out of those files rather than typed here. Skipped when
the spec repo is not checked out beside this one.

The module also pins one finding about SEAM-FIDELITY-SPEC §2's own example
block: its ``amount`` column declares ``range: { min: "0" }`` and its mean
property declares a *relative* tolerance, and those two cannot be
compiled together. See
:func:`test_the_spec_example_s_relative_mean_over_a_zero_bounded_range_is_refused`.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.concordance.boundary.slice import SliceReference, commit_state, read_slice
from flux_engine.concordance.boundary.store import BoundaryStore
from flux_engine.concordance.compiler import compile_fidelity
from flux_engine.concordance.gate import GateAction, PackedEmit, run_gate
from flux_engine.concordance.predicate import expose_from_contract
from flux_engine.concordance.record import Ed25519Signer, SyntheticReference, build_record
from flux_engine.concordance.verifier import ClearStatistic, emit_stream_digest
from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.errors import FluxError
from tests.concordance.conftest import TEST_PRIVATE_KEY, telco_example_dir, write_slice

EXAMPLE = telco_example_dir()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        EXAMPLE is None, reason="the FLUX spec repo is not checked out beside this one"
    ),
]


class _ExactLoader(yaml.SafeLoader):
    """A YAML loader that builds ``Decimal`` where PyYAML would build ``float``.

    KERNEL-SPEC §2.1 refuses a ``float`` anywhere in a document being
    canonicalised, and the shipped ``simulation.flux.yml`` carries one
    (``minCredibility: 0.7``). A plain ``yaml.safe_load`` therefore
    produces an emit entry that cannot be digested at all -- see
    :func:`test_the_shipped_emit_entry_carries_a_float_pyyaml_must_not_build`,
    which pins that. Reading the scalar as an exact decimal is the only
    reading under which ``H`` exists.
    """


_ExactLoader.add_constructor(
    "tag:yaml.org,2002:float",
    lambda loader, node: Decimal(loader.construct_scalar(node)),
)

#: The markets the shipped example's subjects carry, and the levels the
#: block below declares for them. The shipped contract types ``market`` as
#: ``STRING``, not ``enum(...)``, so ``support: declared`` has nothing to
#: read and §2 requires the levels be written out.
MARKETS = ("IE", "UK")

#: The fidelity block §2's example declares, with one change: the mean's
#: tolerance is absolute rather than relative. The relative form cannot be
#: compiled against a range whose minimum is zero -- see the test at the
#: bottom of this module, which pins that refusal.
BLOCK: Dict[str, Any] = {
    "encoding": {"fractionalBits": 16, "rounding": "floor"},
    "records": {"min": 4, "max": 64},
    "columns": {
        "amount": {"range": {"min": "0", "max": "10000"}, "null": "exclude"},
        "market": {"support": {"values": list(MARKETS)}, "maxCardinality": 8, "null": "fail"},
        "aborted_at": {
            "range": {"min": "2026-07-01T00:00:00Z", "max": "2026-09-30T23:59:59Z"},
            "null": "fail",
        },
    },
    "properties": [
        {"property": "mean", "column": "amount", "tolerance": {"type": "absolute", "value": "50"}},
        {
            "property": "marginalHistogram",
            "column": "amount",
            "bins": ["0", "50", "100", "250", "1000", "10000"],
            "tolerance": {"type": "distance", "measure": "totalVariation", "max": "0.05"},
        },
        {"property": "support", "column": "market", "tolerance": {"type": "count", "max": 0}},
        {
            "property": "mean",
            "column": "amount",
            "groupBy": "market",
            "tolerance": {"type": "absolute", "value": "100"},
        },
    ],
    "budget": {"maxChecksPerState": 20, "maxStateAge": "P30D"},
    "failureAction": "withholdBundle",
}


def _load(name: str) -> Dict[str, Any]:
    assert EXAMPLE is not None
    return yaml.load((EXAMPLE / name).read_text(encoding="utf-8"), Loader=_ExactLoader)


@pytest.fixture(scope="module")
def contract() -> Dict[str, Any]:
    return _load("payment-recovery.fluid.yml")


@pytest.fixture(scope="module")
def simulation() -> Dict[str, Any]:
    return _load("simulation.flux.yml")


@pytest.fixture(scope="module")
def emit(simulation: Dict[str, Any]) -> Dict[str, Any]:
    entries = simulation["spec"]["emits"]
    assert len(entries) == 1, "the shipped simulation declares one emit"
    return copy.deepcopy(entries[0])


@pytest.fixture(scope="module")
def extension() -> Dict[str, Any]:
    """The emit's entry in the document-level extension map (§2).

    The shipped simulation declares no such entry -- the block is this
    engine's own declaration -- so the fixture builds the entry the way a
    bundle would carry it, leaving the shipped ``spec.emits[]`` entry
    exactly as published.
    """
    return {"fidelity": copy.deepcopy(BLOCK)}


@pytest.fixture(scope="module")
def expose(contract: Dict[str, Any], emit: Dict[str, Any]):
    return expose_from_contract(contract, emit["exposeId"])


@pytest.fixture(scope="module")
def compiled(contract: Dict[str, Any], emit: Dict[str, Any], extension: Dict[str, Any], expose):
    return compile_fidelity(contract=contract, emit=emit, extension=extension, expose=expose)


def _rows(count: int, *, shift: Decimal = Decimal(0)) -> List[Dict[str, Any]]:
    return [
        {
            "subscriber_id": f"sub-{index:04d}",
            "aborted_at": f"2026-07-{(index % 28) + 1:02d}T09:00:00.000Z",
            "amount": str(Decimal(25 * index) + shift),
            "market": MARKETS[index % len(MARKETS)],
        }
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# The block really is checked against the shipped contract
# ---------------------------------------------------------------------------


def test_the_block_s_columns_are_members_of_the_shipped_expose(expose, contract) -> None:
    declared = contract["exposes"][0]["contract"]["schema"]
    assert list(expose.column_names) == [column["name"] for column in declared]
    assert set(BLOCK["columns"]) <= set(expose.column_names)


def test_the_shipped_emit_entry_carries_a_float_pyyaml_must_not_build() -> None:
    """``minCredibility: 0.7`` is a YAML float, and no float may be hashed.

    KERNEL-SPEC §2.1 refuses a ``float`` anywhere in a document being
    canonicalised. The shipped ``simulation.flux.yml`` emit entry carries
    one, so ``H`` cannot be taken over the entry as ``yaml.safe_load``
    builds it: the loader that feeds the seam has to read a YAML float as
    an exact decimal. This test pins the refusal so the requirement on the
    loader cannot be lost.
    """
    assert EXAMPLE is not None
    plainly = yaml.safe_load((EXAMPLE / "simulation.flux.yml").read_text(encoding="utf-8"))
    entry = plainly["spec"]["emits"][0]
    assert any(isinstance(value, float) for value in entry.values())
    with pytest.raises(FluxError) as caught:
        canonical_bytes(entry)
    assert caught.value.code.code == "FLX-1001"

    exact = _load("simulation.flux.yml")["spec"]["emits"][0]
    assert not any(isinstance(value, float) for value in exact.values())
    assert canonical_bytes(exact)


def test_the_emit_entry_is_the_shipped_one(emit, simulation) -> None:
    shipped = simulation["spec"]["emits"][0]
    for key, value in shipped.items():
        assert emit[key] == value


def test_a_column_the_shipped_expose_does_not_declare_is_refused(
    contract, emit, extension, expose
) -> None:
    broken = copy.deepcopy(extension)
    broken["fidelity"]["columns"]["churn_risk"] = {
        "range": {"min": "0", "max": "1"},
        "null": "exclude",
    }
    with pytest.raises(FluxError) as caught:
        compile_fidelity(contract=contract, emit=emit, extension=broken, expose=expose)
    assert caught.value.code.code == "FLX-5031"


def test_h_covers_the_shipped_contract(contract, emit, extension, expose, compiled) -> None:
    changed = copy.deepcopy(contract)
    changed["exposes"][0]["contract"]["schema"].append({"name": "extra", "type": "STRING"})
    moved = compile_fidelity(
        contract=changed,
        emit=emit,
        extension=extension,
        expose=expose_from_contract(changed, emit["exposeId"]),
    )
    assert moved.h.value != compiled.h.value
    assert moved.g.value != compiled.g.value


def test_every_property_compiles_a_margin_inside_the_field_capacity(compiled) -> None:
    assert len(compiled.properties) == len(BLOCK["properties"])
    for entry in compiled.properties:
        if entry.margin is not None:
            assert entry.margin > 0
        for width in entry.magnitudes.values():
            assert width <= compiled.capacity_bits


# ---------------------------------------------------------------------------
# The whole loop, over the shipped contract
# ---------------------------------------------------------------------------


def test_the_whole_loop_emits_for_a_synthetic_dataset_that_agrees(
    tmp_path, compiled, expose
) -> None:
    committed_rows = _rows(32)
    uri = write_slice(tmp_path / "committed.ndjson", committed_rows)
    rows, reference = read_slice(uri, maximum=compiled.block.records.maximum)
    assert len(rows) == len(committed_rows)

    blinding = bytes(range(32))
    state = commit_state(
        rows=rows,
        compiled=compiled,
        ordinal=1,
        committed_at="2026-09-01T00:00:00.000Z",
        blinding=blinding,
        slice_reference=reference,
    )
    assert {entry.column for entry in state.c1} == set(BLOCK["columns"])
    assert state.record_count == len(committed_rows)

    store = BoundaryStore(max_checks_per_state=compiled.block.budget.max_checks_per_state)
    store.put(state, blinding)
    check_ordinal = store.record_check(state.ordinal, g=compiled.g.value)
    assert check_ordinal == 1

    synthetic = _rows(32, shift=Decimal("1"))
    evaluation = ClearStatistic(
        compiled=compiled, committed=state, synthetic_rows=synthetic
    ).evaluate()
    assert evaluation.within, evaluation.refusals

    columns = list(expose.column_names)
    header = {
        "exposeId": expose.expose_id,
        "columns": columns,
        "contractDigest": compiled.h.value,
    }
    packed = PackedEmit(
        header=header,
        rows=tuple(tuple(row[name] for name in columns) for row in synthetic),
    )
    digest = emit_stream_digest(header=header, rows=packed.rows, subject="d")
    signer = Ed25519Signer(private_key=TEST_PRIVATE_KEY, key_id="telco-verifier")
    record = build_record(
        compiled=compiled,
        committed=state,
        evaluation=evaluation,
        synthetic=SyntheticReference(
            output_digest=digest.value,
            rows=len(synthetic),
            generator_digest="sha256:" + "b" * 64,
        ),
        product_ref=compiled.block.expose_id,
        fluid_version="^0.7.3",
        contract_digest=compiled.h.value,
        check_ordinal=check_ordinal,
        signer=signer,
    )
    assert record.signed and record.within

    decision = run_gate(
        compiled=compiled,
        record_document=record.document,
        committed=state,
        packed=packed,
        now_ms=state.committed_at_ms + 24 * 60 * 60 * 1000,
        trusted_keys=[signer.public_key],
        self_attested=True,
    )
    assert decision.action is GateAction.EMIT, decision.failures


def test_the_check_budget_the_block_declares_is_the_one_the_store_enforces(
    tmp_path, compiled
) -> None:
    # Two markets, and the grouped property needs records.min in each.
    committed_rows = _rows(compiled.block.records.minimum * len(MARKETS))
    blinding = bytes(range(32))
    state = commit_state(
        rows=committed_rows,
        compiled=compiled,
        ordinal=1,
        committed_at="2026-09-01T00:00:00.000Z",
        blinding=blinding,
        slice_reference=SliceReference(uri="file:///s", first_line=1, last_line=1),
    )
    budget = compiled.block.budget.max_checks_per_state
    store = BoundaryStore(max_checks_per_state=budget)
    store.put(state, blinding)
    for expected in range(1, budget + 1):
        assert store.record_check(state.ordinal, g=compiled.g.value) == expected
    with pytest.raises(FluxError) as caught:
        store.record_check(state.ordinal, g=compiled.g.value)
    assert caught.value.code.code == "FLX-5010"


# ---------------------------------------------------------------------------
# The finding about §2's own example
# ---------------------------------------------------------------------------


def test_the_spec_example_s_relative_mean_over_a_zero_bounded_range_is_refused(
    contract, emit, extension, expose
) -> None:
    """SEAM-FIDELITY-SPEC §2's example block cannot be compiled as written.

    Its ``amount`` column declares ``range: { min: "0", max: "10000" }``
    and its first property declares ``tolerance: { type: relative, value:
    "0.02" }``. A relative tolerance needs a reference magnitude, and a
    mean of values in ``[0, 10000]`` can be zero -- so KERNEL-SPEC §12's
    epsilon propagation has no bound to offer and the compile fails closed
    with ``FLX-5002``. Either the column's minimum must be above zero or
    the tolerance must be absolute. This test pins the refusal so the
    finding cannot be lost.
    """
    as_written = copy.deepcopy(extension)
    as_written["fidelity"]["properties"][0]["tolerance"] = {
        "type": "relative",
        "value": "0.02",
    }
    with pytest.raises(FluxError) as caught:
        compile_fidelity(contract=contract, emit=emit, extension=as_written, expose=expose)
    assert caught.value.code.code == "FLX-5002"
    assert "spans zero" in caught.value.detail


def test_the_same_declaration_compiles_once_the_minimum_is_above_zero(
    contract, emit, extension, expose
) -> None:
    """The paired positive case: the refusal is about the range, not the form."""
    fixed = copy.deepcopy(extension)
    fixed["fidelity"]["properties"][0]["tolerance"] = {"type": "relative", "value": "0.02"}
    fixed["fidelity"]["columns"]["amount"]["range"]["min"] = "1"
    fixed["fidelity"]["properties"][1]["bins"] = ["1", "50", "100", "250", "1000", "10000"]
    compiled = compile_fidelity(contract=contract, emit=emit, extension=fixed, expose=expose)
    assert compiled.properties[0].margin is not None
