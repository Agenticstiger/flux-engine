# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim ship``'s signature: attributable, verifiable, and written after the gate.

Three defects, one per section below.

1. The key was generated fresh with ``os.urandom(32)`` and the gate was
   then handed *that signer's own* public key, so the ``record.signature``
   check could never fail and proved nothing about who produced the
   record.
2. The public key was never written anywhere -- not to the record, not to
   the run directory, not to the evidence pack -- so the signature was
   unverifiable by anyone, forever, while the record advertised
   ``"alg": "ed25519"`` and a keyId a reader would take for an
   attributable signature.
3. The record was written to disk *before* the gate ran, so a withheld
   ship left a signed record behind.

A fourth, found later: the key that named the signer sat in the
``signature`` object, which :func:`~flux_engine.concordance.record.signing_payload`
excludes -- so it was not covered by the signature it accompanied, and a
rewritten record re-signed with an attacker's key verified against the
only key travelling with it. The identity now lives in a top-level
``signer`` block inside the signed body, and the gate checks against a
trust anchor its caller supplies (``trust_key``) rather than against a
key derived from the one it just signed with.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

from flux_engine.concordance.record import verify_record
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.ship import ship

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"
EXPOSE_ID = "payment_recovery_moment"


def _applied(state: Path) -> str:
    result = apply(SMALL_BUNDLE, state_dir=state)
    assert result.success, result.stderr
    return str(result.artifacts["run_id"])


def _slice_from_the_run(state: Path, run_id: str, path: Path) -> str:
    """A committed slice built from this run's own emitted rows.

    Mirrors ``tests/golden/test_telco_golden.py``: a slice that is not the
    run's own rows would fail the tolerance check for reasons that have
    nothing to do with the signature this module is about.
    """
    layout = RunLayout.for_state_dir(state, run_id)
    attestation = json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))
    columns = attestation["columns"]
    rows = [
        dict(zip(columns, json.loads(line), strict=True))
        for line in layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return f"file://{path}"


def _record(state: Path, run_id: str) -> dict:
    path = RunLayout.for_state_dir(state, run_id).fidelity_record_path(EXPOSE_ID)
    assert path.is_file(), "ship never reached the fidelity gate"
    return json.loads(path.read_text(encoding="utf-8"))


def test_an_unattributed_signature_says_so_and_carries_its_public_key(tmp_path: Path) -> None:
    state = tmp_path / ".flux"
    run_id = _applied(state)
    slice_uri = _slice_from_the_run(state, run_id, tmp_path / "slice.ndjson")

    result = ship(
        SMALL_BUNDLE,
        run_id=run_id,
        out_dir=tmp_path / "out",
        state_dir=state,
        committed_slice={EXPOSE_ID: slice_uri},
    )
    assert result.success, result.stderr

    document = _record(state, run_id)
    signer = document["signer"]
    assert signer["ephemeral"] is True
    assert signer["keyId"].startswith("ephemeral:")
    assert document["signature"]["keyId"] == signer["keyId"]
    assert len(bytes.fromhex(signer["publicKey"])) == 32


def test_the_recorded_public_key_actually_verifies_the_signature(tmp_path: Path) -> None:
    """Without the key in the record the signature was uncheckable by anyone."""
    state = tmp_path / ".flux"
    run_id = _applied(state)
    slice_uri = _slice_from_the_run(state, run_id, tmp_path / "slice.ndjson")
    assert ship(
        SMALL_BUNDLE,
        run_id=run_id,
        out_dir=tmp_path / "out",
        state_dir=state,
        committed_slice={EXPOSE_ID: slice_uri},
    ).success

    document = _record(state, run_id)
    public_key = bytes.fromhex(document["signer"]["publicKey"])
    assert verify_record(document, public_key) == []
    # The negative control: a different key must NOT verify it.
    assert verify_record(document, secrets.token_bytes(32)) != []


def test_an_operator_key_produces_an_attributable_key_id(tmp_path: Path) -> None:
    state = tmp_path / ".flux"
    run_id = _applied(state)
    slice_uri = _slice_from_the_run(state, run_id, tmp_path / "slice.ndjson")

    assert ship(
        SMALL_BUNDLE,
        run_id=run_id,
        out_dir=tmp_path / "out",
        state_dir=state,
        committed_slice={EXPOSE_ID: slice_uri},
        signing_key=secrets.token_bytes(32),
    ).success

    signer = _record(state, run_id)["signer"]
    assert signer["ephemeral"] is False
    assert not signer["keyId"].startswith("ephemeral:")


def test_two_ships_of_one_slice_under_one_key_write_one_byte_identical_record(
    tmp_path: Path,
) -> None:
    """The defect: the record moved between two ships of an unchanged run.

    ``C2``'s blinding value was drawn with ``generate_blinding()`` on every
    ``ship`` call, and the commitment time was stamped from the host clock
    on every call too -- so one applied run, one committed slice and one
    ``--signing-key`` produced two different ``committedState`` blocks,
    two different signatures over them, and two evidence archives that
    disagreed about a state nobody had touched. Both belong to the
    committed state: drawn once when the records are first committed, kept
    in the boundary store, read back for every later check.
    """
    state = tmp_path / ".flux"
    run_id = _applied(state)
    slice_uri = _slice_from_the_run(state, run_id, tmp_path / "slice.ndjson")
    key = secrets.token_bytes(32)

    def ship_once(out: str) -> bytes:
        result = ship(
            SMALL_BUNDLE,
            run_id=run_id,
            out_dir=tmp_path / out,
            state_dir=state,
            committed_slice={EXPOSE_ID: slice_uri},
            signing_key=key,
        )
        assert result.success, result.stderr
        return RunLayout.for_state_dir(state, run_id).fidelity_record_path(EXPOSE_ID).read_bytes()

    first = ship_once("out1")
    second = ship_once("out2")
    assert first == second

    document = json.loads(first)
    assert document["committedState"]["C2"] == json.loads(second)["committedState"]["C2"]
    # The control that the record is not identical for a trivial reason:
    # it does carry the quantities a moving blinding value used to move.
    assert document["committedState"]["C2"].startswith("sha256:")
    assert document["signature"]["sig"]


def test_without_a_signing_key_two_ships_differ_only_in_the_signer_blocks(
    tmp_path: Path,
) -> None:
    """No ``--signing-key`` means a per-invocation key, and only that moves.

    Stated explicitly because it is the one difference that is *supposed*
    to remain: a key generated in memory for one invocation is a different
    key on the next, so ``signer`` and ``signature`` differ. Everything the
    record says about the committed state -- its ordinal, its commitment
    time, ``C2``, every property outcome -- is the same document, because
    the state is the same state.
    """
    state = tmp_path / ".flux"
    run_id = _applied(state)
    slice_uri = _slice_from_the_run(state, run_id, tmp_path / "slice.ndjson")

    def ship_once(out: str) -> dict:
        result = ship(
            SMALL_BUNDLE,
            run_id=run_id,
            out_dir=tmp_path / out,
            state_dir=state,
            committed_slice={EXPOSE_ID: slice_uri},
        )
        assert result.success, result.stderr
        return _record(state, run_id)

    first = ship_once("out1")
    second = ship_once("out2")

    moved = {key for key in set(first) | set(second) if first.get(key) != second.get(key)}
    assert moved == {"signer", "signature"}, moved
    assert first["signer"]["ephemeral"] is second["signer"]["ephemeral"] is True
    assert first["signer"]["publicKey"] != second["signer"]["publicKey"]
    assert first["committedState"] == second["committedState"]


def test_a_withheld_ship_leaves_no_signed_record_on_disk(tmp_path: Path) -> None:
    """No ``--committed-slice`` for a declared block is a withholding."""
    state = tmp_path / ".flux"
    run_id = _applied(state)

    result = ship(SMALL_BUNDLE, run_id=run_id, out_dir=tmp_path / "out", state_dir=state)

    assert result.exit_code == 1
    assert result.error_code == "FLX-8001"
    record_path = RunLayout.for_state_dir(state, run_id).fidelity_record_path(EXPOSE_ID)
    assert not record_path.exists(), "a withheld ship wrote a signed record anyway"


def test_the_evidence_report_discloses_an_unattributed_signature(tmp_path: Path) -> None:
    state = tmp_path / ".flux"
    run_id = _applied(state)
    slice_uri = _slice_from_the_run(state, run_id, tmp_path / "slice.ndjson")
    assert ship(
        SMALL_BUNDLE,
        run_id=run_id,
        out_dir=tmp_path / "out",
        state_dir=state,
        committed_slice={EXPOSE_ID: slice_uri},
    ).success

    report = (RunLayout.for_state_dir(state, run_id).run_dir / "EVIDENCE.md").read_text(
        encoding="utf-8"
    )
    assert "attributes the records to no operator" in report
