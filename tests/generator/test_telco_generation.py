# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The whole path, on the artifact the project ships.

Bundle on disk -> validate -> resolve -> compile -> ``H`` -> seed root ->
generate -> render -> ``outputDigest`` -> attest. Nothing is mocked and
nothing is stubbed; the only thing this file supplies that the shipped
bundle does not is the fidelity block, because SEAM-FIDELITY-SPEC §2's
block is an additive RFC field the published schema does not carry yet.
That block is written here **verbatim from §2's example**, so the test is
also a check that the specification's own example works.

**Every count is computed.** The column count, the type names, the row
count and the identifier lengths are read out of the bundle and the plan.
Nothing about the telco example is typed into an assertion.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest

from flux_engine.core.seedtree import derive_root, effective_seed, seed_tree_digest
from flux_engine.generator import build_plan, generate_rows
from flux_engine.generator.families import ColumnType
from flux_engine.generator.paths import GeneratorStreams
from flux_engine.seam.attest import build_seam_attestation, copy_shipped_contract
from flux_engine.seam.emit import EmitWriter, seam_digests, seam_entropy_bytes
from flux_engine.seam.resolve import load_bundle, resolve_emit
from flux_engine.seam.sinks import NdjsonSink, NullSink

pytestmark = [pytest.mark.integration]

SIMULATION_ID = "sim-payment-recovery"

#: SEAM-FIDELITY-SPEC §2's example block, transcribed. ``subscriber_id``
#: carries the ``unique`` marker §2's prose names ("identifier (STRING/uuid
#: with ``unique``)") but its YAML example does not spell, because the
#: example's ``columns`` map only shows the three columns it needs for its
#: properties.
FIDELITY_BLOCK: Dict[str, Any] = {
    "encoding": {"fractionalBits": 16, "rounding": "floor"},
    "records": {"min": 1000, "max": 100000},
    "columns": {
        "subscriber_id": {"unique": True},
        "amount": {"range": {"min": "0", "max": "10000"}, "null": "exclude"},
        "market": {
            "support": {"values": ["IE", "GB", "FR", "DE"]},
            "maxCardinality": 8,
            "null": "fail",
        },
        "aborted_at": {
            "range": {"min": "2026-07-01T00:00:00Z", "max": "2026-09-30T23:59:59Z"},
            "null": "fail",
        },
    },
    "properties": [
        {
            "property": "mean",
            "column": "amount",
            "tolerance": {"type": "relative", "value": "0.02"},
        },
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
            "tolerance": {"type": "relative", "value": "0.03"},
        },
    ],
    "budget": {"maxChecksPerState": 20, "maxStateAge": "P30D"},
    "failureAction": "withholdBundle",
}

#: The histogram edges §2 declares make the ``amount`` column empirical, so
#: the generator needs the mass per bin. Weights, not counts of records:
#: an aggregate quantity, which is the only kind ADR-0003 permits.
BIN_WEIGHTS = ["12", "28", "34", "20", "6"]

ROWS = 64


@pytest.fixture(scope="module")
def pipeline(telco_dir: Path) -> Dict[str, Any]:
    """Run the whole path once and hand every stage's output to the tests."""
    loaded = load_bundle(telco_dir)
    resolved = resolve_emit(loaded, simulation_id=SIMULATION_ID)
    expose = resolved.compile()

    block = {
        **FIDELITY_BLOCK,
        "columns": {
            **FIDELITY_BLOCK["columns"],
            "amount": {**FIDELITY_BLOCK["columns"]["amount"], "binWeights": BIN_WEIGHTS},
        },
    }
    digests = seam_digests(resolved.contract_document, resolved.emit, expose.column_names)

    simulation = loaded.simulation(SIMULATION_ID)
    world = loaded.flux_documents[simulation["spec"]["worldRef"].split("@", 1)[0]]
    seed = effective_seed(simulation["spec"].get("seed"), world["spec"].get("seed"))
    root = derive_root(seam_entropy_bytes([digests]), seed)

    plan = build_plan(
        expose,
        sim_id=SIMULATION_ID,
        seam_digest=digests.h.value,
        rows=ROWS,
        fidelity=block,
        time_bounds=simulation["spec"]["timeBounds"],
    )
    streams = GeneratorStreams(root=root)
    rows = generate_rows(plan, source=streams)

    writer = EmitWriter(expose=expose, contract_digest=resolved.contract_digest, sink=NullSink())
    with writer:
        writer.write_all(rows)
    result = writer.close()

    return {
        "loaded": loaded,
        "resolved": resolved,
        "expose": expose,
        "block": block,
        "digests": digests,
        "seed": seed,
        "root": root,
        "plan": plan,
        "streams": streams,
        "rows": rows,
        "writer": writer,
        "result": result,
        "directory": telco_dir,
    }


# ---------------------------------------------------------------------------
# The shape is the contract's shape
# ---------------------------------------------------------------------------


def test_the_plan_covers_exactly_the_declared_columns(pipeline: Dict[str, Any]) -> None:
    declared = pipeline["resolved"].expose["contract"]["schema"]
    plan = pipeline["plan"]
    assert [item.column.name for item in plan.columns] == [column["name"] for column in declared]
    assert len(plan.columns) == len(declared)


def test_every_column_of_the_shipped_contract_gets_a_family(
    pipeline: Dict[str, Any],
) -> None:
    """No column of the real contract falls through the six-row table."""
    for item in pipeline["plan"].columns:
        if item.column_type is ColumnType.IDENTIFIER:
            assert item.family is None
        else:
            assert item.family is not None


def test_the_seed_is_the_one_the_bundle_declares(pipeline: Dict[str, Any]) -> None:
    simulation = pipeline["loaded"].simulation(SIMULATION_ID)
    assert pipeline["seed"] == simulation["spec"]["seed"]


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------


def test_the_declared_number_of_rows_is_produced(pipeline: Dict[str, Any]) -> None:
    assert len(pipeline["rows"]) == pipeline["plan"].rows == pipeline["result"].rows


def test_every_row_satisfies_the_contracts_own_declarations(
    pipeline: Dict[str, Any],
) -> None:
    """Required columns are present; declared bounds hold; nothing is a float."""
    expose = pipeline["expose"]
    index = expose.index_of
    block = pipeline["block"]["columns"]
    support = set(block["market"]["support"]["values"])
    low = Decimal(block["amount"]["range"]["min"])
    high = Decimal(block["amount"]["range"]["max"])

    for row in pipeline["rows"]:
        for column in expose.columns:
            value = row[index[column.name]]
            if column.required:
                assert value is not None, column.name
            assert not isinstance(value, float)
        assert row[index["market"]] in support
        assert low <= row[index["amount"]] <= high


def test_the_amount_column_lands_in_the_declared_histogram_bins(
    pipeline: Dict[str, Any],
) -> None:
    edges = [Decimal(edge) for edge in pipeline["block"]["properties"][1]["bins"]]
    values = [row[pipeline["expose"].index_of["amount"]] for row in pipeline["rows"]]
    assert all(edges[0] <= value <= edges[-1] for value in values)
    occupied = {max(index for index, edge in enumerate(edges) if edge <= value) for value in values}
    assert len(occupied) > 1, "the declared weights spread mass over more than one bin"


def test_the_identifier_column_is_unique_and_seed_independent(
    pipeline: Dict[str, Any],
) -> None:
    index = pipeline["expose"].index_of["subscriber_id"]
    values: List[str] = [row[index] for row in pipeline["rows"]]
    assert len(set(values)) == len(values)

    other_root = derive_root(seam_entropy_bytes([pipeline["digests"]]), pipeline["seed"] + 1)
    moved = generate_rows(pipeline["plan"], root=other_root)
    assert [row[index] for row in moved] == values


def test_a_different_seed_moves_every_drawn_column(pipeline: Dict[str, Any]) -> None:
    """The negative control: the run is seeded, and the seed matters."""
    expose = pipeline["expose"]
    other_root = derive_root(seam_entropy_bytes([pipeline["digests"]]), pipeline["seed"] + 1)
    moved = generate_rows(pipeline["plan"], root=other_root)
    for item in pipeline["plan"].columns:
        if item.family is None:
            continue
        position = expose.index_of[item.column.name]
        assert [row[position] for row in moved] != [
            row[position] for row in pipeline["rows"]
        ], item.column.name


# ---------------------------------------------------------------------------
# The stream, its digest, and the sidecar
# ---------------------------------------------------------------------------


def test_the_whole_pipeline_is_reproducible(pipeline: Dict[str, Any]) -> None:
    """Re-running every stage from the bundle on disk gives the same digest."""
    loaded = load_bundle(pipeline["directory"])
    resolved = resolve_emit(loaded, simulation_id=SIMULATION_ID)
    expose = resolved.compile()
    digests = seam_digests(resolved.contract_document, resolved.emit, expose.column_names)
    assert digests.h == pipeline["digests"].h

    simulation = loaded.simulation(SIMULATION_ID)
    world = loaded.flux_documents[simulation["spec"]["worldRef"].split("@", 1)[0]]
    root = derive_root(
        seam_entropy_bytes([digests]),
        effective_seed(simulation["spec"].get("seed"), world["spec"].get("seed")),
    )
    plan = build_plan(
        expose,
        sim_id=SIMULATION_ID,
        seam_digest=digests.h.value,
        rows=ROWS,
        fidelity=pipeline["block"],
        time_bounds=simulation["spec"]["timeBounds"],
    )
    assert plan.digest() == pipeline["plan"].digest()

    writer = EmitWriter(expose=expose, contract_digest=resolved.contract_digest)
    with writer:
        writer.write_all(generate_rows(plan, root=root))
    assert writer.result().output_digest == pipeline["result"].output_digest


def test_writing_the_rows_to_a_file_does_not_move_the_digest(
    tmp_path: Path, pipeline: Dict[str, Any]
) -> None:
    """§8's rule, on the real stream."""
    path = tmp_path / "payment_recovery_moment.ndjson"
    writer = EmitWriter(
        expose=pipeline["expose"],
        contract_digest=pipeline["resolved"].contract_digest,
        sink=NdjsonSink(path=path),
    )
    with writer:
        writer.write_all(pipeline["rows"])
    assert writer.result().output_digest == pipeline["result"].output_digest
    assert path.read_bytes() == pipeline["writer"].stream_bytes()
    assert path.read_text(encoding="utf-8").count("\n") == len(pipeline["rows"])


def test_the_seed_tree_digest_covers_one_stream_per_drawn_cell(
    pipeline: Dict[str, Any],
) -> None:
    drawn = sum(1 for item in pipeline["plan"].columns if item.family is not None)
    paths = pipeline["streams"].materialised_paths
    assert len(paths) == drawn * len(pipeline["rows"])
    assert pipeline["streams"].digest() == seed_tree_digest(paths)


def test_the_attestation_binds_the_stream_to_the_contract(
    tmp_path: Path, pipeline: Dict[str, Any]
) -> None:
    attestation = build_seam_attestation(
        pipeline["resolved"],
        pipeline["expose"],
        pipeline["result"],
        generator_digest=pipeline["plan"].digest(),
    )
    document = attestation.as_document()
    assert document["reference"]["contractDigest"] == (
        pipeline["resolved"].declared_contract_digest
    )
    assert document["H"] == pipeline["digests"].h.value
    assert document["synthetic"]["outputDigest"] == pipeline["result"].output_digest.value
    assert document["synthetic"]["generatorDigest"] == pipeline["plan"].digest().value
    assert document["columns"] == list(pipeline["expose"].column_names)

    staged, digest = copy_shipped_contract(
        pipeline["resolved"], tmp_path / "pack", bundle_directory=pipeline["directory"]
    )
    assert staged.read_bytes() == pipeline["resolved"].contract_path.read_bytes()
    assert digest == pipeline["resolved"].contract_file_digest


def test_the_bundle_is_untouched_by_the_whole_run(pipeline: Dict[str, Any]) -> None:
    """The shipped example is an input, and nothing here writes to it."""
    directory: Path = pipeline["directory"]
    on_disk: Set[str] = {path.name for path in directory.iterdir() if path.is_file()}
    assert set(pipeline["loaded"].file_bytes) == on_disk
    for name, recorded in pipeline["loaded"].file_bytes.items():
        assert (directory / name).read_bytes() == recorded
