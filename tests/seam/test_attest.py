# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The seam attestation sidecar, and the two rules it holds.

**Nothing writes into the bundle.** Every path the module takes is resolved
and compared against the bundle root, including the ones a caller reaches
through ``..`` or a symlink. A run that could edit its own input could not
be replayed against what it was given.

**The shipped contract is copied byte-for-byte** (SEAM-FIDELITY-SPEC §7),
and the copy is *checked* rather than asserted: the destination is read
back and its ``raw-bytes`` digest compared with the bundle's.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.digest import digest_of_bytes, digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.seam.attest import (
    ATTESTATION_FILENAME,
    SeamAttestation,
    build_seam_attestation,
    copy_shipped_contract,
    seam_attestation_document,
    shipped_contract_matches,
    write_seam_attestation,
)
from flux_engine.seam.contract import CompiledExpose
from flux_engine.seam.emit import EmitWriter, seam_digests
from flux_engine.seam.resolve import LoadedBundle, ResolvedEmit, load_bundle, resolve_emit
from flux_engine.seam.sinks import NullSink

pytestmark = pytest.mark.unit

KERNEL_VERSION = "0.1.0-test"
GENERATED_AT = "2026-09-06T12:00:00Z"

ROWS: List[Dict[str, Any]] = [
    {"widget_id": "a", "amount": Decimal("1.5")},
    {"widget_id": "b", "amount": None},
]


@pytest.fixture
def bundle_root(write_bundle: Any) -> Path:
    return write_bundle()


@pytest.fixture
def loaded(bundle_root: Path) -> LoadedBundle:
    return load_bundle(bundle_root)


@pytest.fixture
def resolved(loaded: LoadedBundle) -> ResolvedEmit:
    return resolve_emit(loaded, simulation_id="sim-widget")


@pytest.fixture
def expose(resolved: ResolvedEmit) -> CompiledExpose:
    return resolved.compile()


@pytest.fixture
def attestation(resolved: ResolvedEmit, expose: CompiledExpose) -> SeamAttestation:
    writer = EmitWriter(expose=expose, contract_digest=resolved.contract_digest, sink=NullSink())
    with writer:
        writer.write_all(ROWS)
    return build_seam_attestation(resolved, expose, writer.close())


# ---------------------------------------------------------------------------
# What the attestation says
# ---------------------------------------------------------------------------


def test_the_attestation_recomputes_h_and_g_rather_than_quoting_them(
    attestation: SeamAttestation, resolved: ResolvedEmit, expose: CompiledExpose
) -> None:
    expected = seam_digests(resolved.contract_document, resolved.emit, expose.column_names)
    assert attestation.seam.h == expected.h
    assert attestation.seam.g == expected.g


def test_the_attestation_carries_the_declared_and_the_computed_bindings(
    attestation: SeamAttestation, resolved: ResolvedEmit, expose: CompiledExpose
) -> None:
    document = attestation.as_document()
    assert document["reference"] == {
        "productRef": resolved.product_ref,
        "exposeId": expose.expose_id,
        "fluidVersion": resolved.fluid_version,
        "contractDigest": resolved.contract_digest.value,
        "contractFileDigest": resolved.contract_file_digest.value,
    }
    assert document["columns"] == list(expose.column_names)
    assert document["types"] == list(expose.type_names)
    assert document["synthetic"]["rows"] == len(ROWS)
    assert document["synthetic"]["generatorDigest"] is None


def test_the_attestation_records_a_generator_digest_when_one_is_given(
    resolved: ResolvedEmit, expose: CompiledExpose
) -> None:
    writer = EmitWriter(expose=expose, contract_digest=resolved.contract_digest)
    with writer:
        writer.write_all(ROWS)
    generator = digest_of_document({"generatorVersion": "flux-gen/1"}, subject="generatorDigest")
    attestation = build_seam_attestation(
        resolved, expose, writer.close(), generator_digest=generator
    )
    assert attestation.as_document()["synthetic"]["generatorDigest"] == generator.value


def test_an_attestation_for_the_wrong_expose_is_refused(
    resolved: ResolvedEmit, expose: CompiledExpose
) -> None:
    from .conftest import expose_from_schema

    other = expose_from_schema([{"name": "n", "type": "INT"}], expose_id="other")
    writer = EmitWriter(expose=other, contract_digest=resolved.contract_digest)
    with writer:
        writer.write([1])
    with pytest.raises(FluxError) as error:
        build_seam_attestation(resolved, expose, writer.close())
    assert error.value.code.code == "FLX-3106"


def test_the_document_orders_emits_by_their_declared_index(
    attestation: SeamAttestation,
) -> None:
    second = SeamAttestation(**{**attestation.__dict__, "emit_index": 1})
    document = seam_attestation_document([second, attestation], kernel_version=KERNEL_VERSION)
    assert [entry["emitIndex"] for entry in document["emits"]] == [0, 1]


def test_the_document_records_the_run_bindings_when_they_are_given(
    attestation: SeamAttestation,
) -> None:
    input_digest = digest_of_document({"a": 1}, subject="inputDigest")
    plan_digest = digest_of_document({"b": 2}, subject="planDigest")
    document = seam_attestation_document(
        [attestation],
        kernel_version=KERNEL_VERSION,
        input_digest=input_digest,
        plan_digest=plan_digest,
    )
    assert document["inputDigest"] == input_digest.value
    assert document["planDigest"] == plan_digest.value
    assert document["kernelVersion"] == KERNEL_VERSION

    absent = seam_attestation_document([attestation], kernel_version=KERNEL_VERSION)
    assert absent["inputDigest"] is None and absent["planDigest"] is None


# ---------------------------------------------------------------------------
# Writing it
# ---------------------------------------------------------------------------


def test_writing_the_sidecar_produces_the_named_file(
    tmp_path: Path, bundle_root: Path, attestation: SeamAttestation
) -> None:
    run = tmp_path / "run"
    path, digest = write_seam_attestation(
        run,
        [attestation],
        bundle_directory=bundle_root,
        kernel_version=KERNEL_VERSION,
        generated_at=GENERATED_AT,
    )
    assert path == run / ATTESTATION_FILENAME
    payload = json.loads(path.read_text())
    assert payload["kind"] == "RunAttestation"
    assert payload["generated_at"] == GENERATED_AT
    assert payload["emits"][0]["reference"]["exposeId"] == attestation.expose_id
    assert digest.value.startswith("sha256:")


def test_the_returned_digest_is_over_the_payload_not_the_envelope(
    tmp_path: Path, bundle_root: Path, attestation: SeamAttestation
) -> None:
    """Two runs over identical inputs agree, though their timestamps do not."""
    _, first = write_seam_attestation(
        tmp_path / "run-a",
        [attestation],
        bundle_directory=bundle_root,
        kernel_version=KERNEL_VERSION,
        generated_at="2026-09-06T12:00:00Z",
    )
    _, second = write_seam_attestation(
        tmp_path / "run-b",
        [attestation],
        bundle_directory=bundle_root,
        kernel_version=KERNEL_VERSION,
        generated_at="2027-01-01T00:00:00Z",
    )
    assert first == second


@pytest.mark.parametrize("suffix", ["", "run", "nested/run", "../bundle/run"])
def test_writing_inside_the_bundle_is_refused(
    bundle_root: Path, attestation: SeamAttestation, suffix: str
) -> None:
    target = bundle_root / suffix if suffix else bundle_root
    with pytest.raises(FluxError) as error:
        write_seam_attestation(
            target,
            [attestation],
            bundle_directory=bundle_root,
            kernel_version=KERNEL_VERSION,
            generated_at=GENERATED_AT,
        )
    assert error.value.code.code == "FLX-3106"
    assert not (bundle_root / ATTESTATION_FILENAME).exists()


def test_a_path_that_walks_back_into_the_bundle_is_refused(
    tmp_path: Path, bundle_root: Path, attestation: SeamAttestation
) -> None:
    sneaky = tmp_path / "run" / ".." / bundle_root.name / "inside"
    with pytest.raises(FluxError) as error:
        write_seam_attestation(
            sneaky,
            [attestation],
            bundle_directory=bundle_root,
            kernel_version=KERNEL_VERSION,
            generated_at=GENERATED_AT,
        )
    assert error.value.code.code == "FLX-3106"


def test_a_symlink_into_the_bundle_is_refused(
    tmp_path: Path, bundle_root: Path, attestation: SeamAttestation
) -> None:
    link = tmp_path / "link-to-bundle"
    link.symlink_to(bundle_root, target_is_directory=True)
    with pytest.raises(FluxError) as error:
        write_seam_attestation(
            link / "run",
            [attestation],
            bundle_directory=bundle_root,
            kernel_version=KERNEL_VERSION,
            generated_at=GENERATED_AT,
        )
    assert error.value.code.code == "FLX-3106"


def test_the_bundle_is_unchanged_by_a_successful_write(
    tmp_path: Path, bundle_root: Path, attestation: SeamAttestation
) -> None:
    before = {path.name: path.read_bytes() for path in sorted(bundle_root.iterdir())}
    write_seam_attestation(
        tmp_path / "run",
        [attestation],
        bundle_directory=bundle_root,
        kernel_version=KERNEL_VERSION,
        generated_at=GENERATED_AT,
    )
    after = {path.name: path.read_bytes() for path in sorted(bundle_root.iterdir())}
    assert before == after


# ---------------------------------------------------------------------------
# The shipped contract copy (SEAM-FIDELITY-SPEC §7)
# ---------------------------------------------------------------------------


def test_the_staged_contract_is_byte_identical(
    tmp_path: Path, bundle_root: Path, resolved: ResolvedEmit
) -> None:
    target, digest = copy_shipped_contract(
        resolved, tmp_path / "pack", bundle_directory=bundle_root
    )
    assert target.read_bytes() == resolved.contract_path.read_bytes()
    assert digest == resolved.contract_file_digest
    assert digest == digest_of_bytes(target.read_bytes(), subject=target.name)
    assert shipped_contract_matches(resolved, target)


def test_the_staged_copy_can_be_renamed_and_stays_byte_identical(
    tmp_path: Path, bundle_root: Path, resolved: ResolvedEmit
) -> None:
    target, _ = copy_shipped_contract(
        resolved, tmp_path / "pack", bundle_directory=bundle_root, filename="shipped.fluid.yml"
    )
    assert target.name == "shipped.fluid.yml"
    assert target.read_bytes() == resolved.contract_bytes


def test_a_staged_copy_that_was_edited_no_longer_matches(
    tmp_path: Path, bundle_root: Path, resolved: ResolvedEmit
) -> None:
    target, _ = copy_shipped_contract(resolved, tmp_path / "pack", bundle_directory=bundle_root)
    target.write_bytes(target.read_bytes() + b"# tampered\n")
    assert shipped_contract_matches(resolved, target) is False


def test_an_absent_staged_copy_does_not_match(tmp_path: Path, resolved: ResolvedEmit) -> None:
    assert shipped_contract_matches(resolved, tmp_path / "never-written.yml") is False


def test_staging_into_the_bundle_is_refused(bundle_root: Path, resolved: ResolvedEmit) -> None:
    with pytest.raises(FluxError) as error:
        copy_shipped_contract(resolved, bundle_root / "pack", bundle_directory=bundle_root)
    assert error.value.code.code == "FLX-3106"
    assert not (bundle_root / "pack").exists()


def test_a_copy_whose_readback_disagrees_is_reported(
    tmp_path: Path, bundle_root: Path, resolved: ResolvedEmit
) -> None:
    """The check is on the readback, so a resolution carrying wrong bytes fails.

    A negative control for the byte-identity claim: if the copy compared
    the source with itself instead of with what landed on disk, this would
    pass.
    """
    tampered = ResolvedEmit(
        **{
            **resolved.__dict__,
            "contract_file_digest": digest_of_bytes(b"other bytes", subject="other"),
        }
    )
    with pytest.raises(FluxError) as error:
        copy_shipped_contract(tampered, tmp_path / "pack", bundle_directory=bundle_root)
    assert error.value.code.code == "FLX-3106"


# ---------------------------------------------------------------------------
# The shipped example
# ---------------------------------------------------------------------------


def test_the_telco_contract_stages_byte_for_byte(
    tmp_path: Path, telco_dir: Path, telco_emit: ResolvedEmit
) -> None:
    target, digest = copy_shipped_contract(
        telco_emit, tmp_path / "pack", bundle_directory=telco_dir
    )
    assert target.read_bytes() == (telco_dir / telco_emit.contract_path.name).read_bytes()
    assert digest == telco_emit.contract_file_digest
