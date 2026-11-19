# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``ship`` is fail-closed across the whole run, in the run directory too.

SEAM-FIDELITY-SPEC section 7's "write no bundle" was honoured -- a
withheld ship packs no archive. The run directory was not: records were
written inside the per-emit decision loop, so a bundle whose *second*
emit is withheld still left the first emit's signed, gate-accepted
``fidelity-record-<exposeId>.json`` on disk, from a ship that produced no
evidence at all. A reader finding that file has no way to tell it from
one a successful ship wrote.

Also here: a seam attestation with no ``generatorDigest`` used to be
signed with ``sha256:`` + 64 zeros in its place -- a value of exactly the
right shape, in the one document this engine signs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.ship import ship

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration

EXPOSE_ID = "payment_recovery_moment"
#: Sorts after the real expose, so the real one is decided (and earns a
#: record) before this one is reached.
PHANTOM_ID = "zz_second_emit"


@pytest.fixture()
def applied(tmp_path: Path) -> RunLayout:
    result = apply(FIXTURE_BUNDLE, state_dir=tmp_path / ".flux")
    assert result.success, result.stderr
    return RunLayout.for_state_dir(tmp_path / ".flux", str(result.artifacts["run_id"]))


@pytest.fixture()
def slice_uri(applied: RunLayout, tmp_path: Path) -> str:
    attestation = json.loads(applied.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))
    rows = [
        dict(zip(attestation["columns"], json.loads(line), strict=True))
        for line in applied.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    path = tmp_path / "slice.ndjson"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return f"file://{path}"


def _add_a_second_emit(applied: RunLayout) -> None:
    """Give the run a second declared emit that the gate will withhold.

    Its artifacts are the first emit's, so every re-derivation ``ship``
    does before the gate still reproduces ``run-digest.json``; what makes
    it withhold is that no ``--committed-slice`` names it, which is the
    refusal SEAM-FIDELITY-SPEC asks for and the one that needs no
    tampering to reach.
    """
    from flux_engine.stages.verify import recompute_components

    applied.emit_path(PHANTOM_ID).write_bytes(applied.emit_path(EXPOSE_ID).read_bytes())
    applied.fidelity_input_path(PHANTOM_ID).write_bytes(
        applied.fidelity_input_path(EXPOSE_ID).read_bytes()
    )
    attestation = json.loads(applied.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))
    attestation["exposeId"] = PHANTOM_ID
    applied.seam_attestation_path(PHANTOM_ID).write_text(
        json.dumps(attestation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    receipt = json.loads(applied.receipt_path.read_text(encoding="utf-8"))
    receipt["emits"][PHANTOM_ID] = dict(receipt["emits"][EXPOSE_ID])
    applied.receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    # The recorded digests are recomputed from the directory as it now
    # stands rather than copied: an emit's digest covers its exposeId, so
    # the second stream is not the first stream's bytes under another
    # name, and `ship`'s FLX-2110 re-derivation would (rightly) refuse a
    # run-digest.json that said it was.
    run_digest = json.loads(applied.run_digest_path.read_text(encoding="utf-8"))
    recomputed = recompute_components(applied, receipt)
    run_digest["components"]["emits"] = dict(recomputed["emits"])
    applied.run_digest_path.write_text(json.dumps(run_digest, indent=2, sort_keys=True) + "\n")


def _ship(applied: RunLayout, out_dir: Path, slices: dict) -> object:
    return ship(
        FIXTURE_BUNDLE,
        run_id=applied.run_id,
        out_dir=out_dir,
        state_dir=applied.state_dir,
        committed_slice=slices,
    )


def test_a_withheld_second_emit_leaves_no_record_for_the_first(
    applied: RunLayout, slice_uri: str, tmp_path: Path
) -> None:
    _add_a_second_emit(applied)
    out_dir = tmp_path / "dist"

    result = _ship(applied, out_dir, {EXPOSE_ID: slice_uri})

    assert not result.success  # type: ignore[attr-defined]
    assert result.error_code == "FLX-8001"  # type: ignore[attr-defined]
    assert result.artifacts["withheld"] == [PHANTOM_ID], (  # type: ignore[attr-defined]
        "the first emit was withheld too, so this proves nothing about the write phase"
    )
    assert not out_dir.exists(), "a withheld ship wrote a bundle"
    assert not applied.fidelity_record_path(
        EXPOSE_ID
    ).is_file(), "the first emit's signed record survived a ship that produced no bundle"
    assert not applied.fidelity_record_path(PHANTOM_ID).is_file()


def test_a_ship_that_withholds_nothing_still_writes_the_record(
    applied: RunLayout, slice_uri: str, tmp_path: Path
) -> None:
    """The positive control: the deferred write phase still writes.

    A "rollback" that simply never wrote a record would pass the test
    above and prove nothing.
    """
    result = _ship(applied, tmp_path / "dist", {EXPOSE_ID: slice_uri})

    assert result.success, result.stderr  # type: ignore[attr-defined]
    assert applied.fidelity_record_path(EXPOSE_ID).is_file()


def test_an_attestation_with_no_generator_digest_is_refused_not_zero_filled(
    applied: RunLayout, slice_uri: str, tmp_path: Path
) -> None:
    """An unknown digest is an absent statement, not a placeholder value."""
    path = applied.seam_attestation_path(EXPOSE_ID)
    attestation = json.loads(path.read_text(encoding="utf-8"))
    assert attestation["synthetic"].get("generatorDigest"), "fixture no longer exercises this"
    attestation["synthetic"]["generatorDigest"] = None
    path.write_text(json.dumps(attestation, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    result = _ship(applied, tmp_path / "dist", {EXPOSE_ID: slice_uri})

    assert not result.success  # type: ignore[attr-defined]
    assert result.error_code == "FLX-8001"  # type: ignore[attr-defined]
    assert "generatorDigest" in result.stderr  # type: ignore[attr-defined]
    assert not applied.fidelity_record_path(EXPOSE_ID).is_file()
    assert not (tmp_path / "dist").exists()
