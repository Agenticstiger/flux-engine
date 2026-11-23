# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim verify --trust-key``: the anchor a reader outside the ship supplies.

``ship`` prints ``fluxsim verify`` as the command a reader runs to reach the
same conclusion from the pack alone, and signs the fidelity record it
leaves in the run directory. ``fluxsim verify`` had no ``--trust-key`` and no
``--signing-key`` at all, so the record could be re-checked only by the
process that wrote it -- the one signature state that is a statement about
*who* signed was reachable from ``ship`` and from nowhere else.

Four states, all of them exercised here against a real signed record:
``trusted`` (a key that came from outside), ``self-attested`` (the public
half of the seed this invocation was handed -- internal consistency and
nothing more), ``not trusted`` (a key that is not the signer's), and
``not checked`` (no anchor supplied at all, which must never read as
green).
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.cli import main
from flux_engine.concordance.record import Ed25519Signer
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration

EXPOSE_ID = "payment_recovery_moment"

#: A fixed seed, so the record and its public key are the same on every
#: run of this module. Test material, never a key any artifact ships with.
_SEED = bytes(range(32))
_OTHER_SEED = bytes(range(32, 64))


def _public(seed: bytes) -> bytes:
    return Ed25519Signer(private_key=seed, key_id="_").public_key


@pytest.fixture(scope="module")
def shipped(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    """An applied, shipped run whose record was signed with :data:`_SEED`."""
    root = tmp_path_factory.mktemp("verify-trust-key")
    state_dir = root / ".flux"
    applied = apply(FIXTURE_BUNDLE, state_dir=state_dir)
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])

    layout = RunLayout.for_state_dir(state_dir, run_id)
    # The slice is the run's own emitted rows: every declared property
    # then evaluates `within` whatever the kernel's numerics do, so the
    # gate reaches its signing path deterministically. It claims nothing
    # about real-data fidelity; what is under test is the signature.
    columns = json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text())["columns"]
    rows = [
        dict(zip(columns, json.loads(line), strict=True))
        for line in layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert rows, "the fixture declares a fidelity block, so its rows are generated"
    slice_path = root / "slice.ndjson"
    slice_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    key_path = root / "signing.hex"
    key_path.write_text(_SEED.hex(), encoding="utf-8")

    exit_code = main(
        [
            "ship",
            str(FIXTURE_BUNDLE),
            "--run-id",
            run_id,
            "--state-dir",
            str(state_dir),
            "-o",
            str(root / "dist"),
            "--committed-slice",
            f"{EXPOSE_ID}={slice_path.as_uri()}",
            "--signing-key",
            str(key_path),
        ]
    )
    assert exit_code == 0
    assert layout.fidelity_record_path(EXPOSE_ID).is_file()
    return {"root": root, "state_dir": state_dir, "run_id": run_id}


def _verify(shipped: Dict[str, Any], *extra: str) -> Dict[str, Any]:
    """Run ``fluxsim verify --json`` and return its envelope, plus the exit code."""
    argv: List[str] = [
        "verify",
        str(FIXTURE_BUNDLE),
        "--run-id",
        shipped["run_id"],
        "--state-dir",
        str(shipped["state_dir"]),
        "--allow-declared-baseline-drift",
        "--json",
        *extra,
    ]
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = main(argv)
    envelope = json.loads(buffer.getvalue())
    envelope["exit_code"] = exit_code
    return envelope


def _key_file(shipped: Dict[str, Any], name: str, material: bytes) -> str:
    path = Path(shipped["root"]) / name
    path.write_text(material.hex(), encoding="utf-8")
    return str(path)


def test_the_signing_key_is_the_anchor_and_the_check_says_it_is_self_attested(
    shipped: Dict[str, Any],
) -> None:
    envelope = _verify(shipped, "--signing-key", _key_file(shipped, "seed.hex", _SEED))
    record = envelope["artifacts"]["fidelity"][EXPOSE_ID]["record"]
    assert record["status"] == "self-attested"
    assert "nothing about who signed" in record["detail"]


def test_the_signers_public_key_supplied_from_outside_is_trusted(
    shipped: Dict[str, Any],
) -> None:
    envelope = _verify(shipped, "--trust-key", _key_file(shipped, "trust.hex", _public(_SEED)))
    record = envelope["artifacts"]["fidelity"][EXPOSE_ID]["record"]
    assert record["status"] == "trusted"
    assert record["detail"] is None
    assert envelope["exit_code"] == 0


def test_a_key_that_did_not_sign_the_record_fails_the_leg(shipped: Dict[str, Any]) -> None:
    """The negative control: an anchor that refuses nothing is not an anchor."""
    envelope = _verify(
        shipped, "--trust-key", _key_file(shipped, "wrong.hex", _public(_OTHER_SEED))
    )
    outcome = envelope["artifacts"]["fidelity"][EXPOSE_ID]
    assert outcome["record"]["status"] == "not trusted"
    assert "does not verify" in outcome["record"]["detail"]
    assert outcome["status"] == "failed", "an untrusted record is a failed leg, not a footnote"
    # The detail moves with the status. Leaving the "declared, not
    # checked" sentence beside the word `failed` would name a check that
    # never ran as the reason this leg failed.
    assert outcome["detail"].startswith(f"fidelity-record-{EXPOSE_ID}.json: ")
    assert "--committed-slice" not in outcome["detail"]
    assert envelope["exit_code"] == 1
    assert envelope["ok"] is False


def test_no_key_at_all_reports_the_check_unchecked_rather_than_green(
    shipped: Dict[str, Any],
) -> None:
    """A record's own key is never promoted to a trust anchor."""
    envelope = _verify(shipped)
    record = envelope["artifacts"]["fidelity"][EXPOSE_ID]["record"]
    assert record["status"] == "not checked"
    assert "--trust-key" in record["detail"]


def test_a_run_with_no_record_says_nothing_about_signatures(
    tmp_path: Path,
) -> None:
    """No record on disk, no key in the outcome -- not a green check over nothing."""
    state_dir = tmp_path / ".flux"
    applied = apply(FIXTURE_BUNDLE, state_dir=state_dir)
    assert applied.success, applied.stderr
    envelope = _verify(
        {"root": tmp_path, "state_dir": state_dir, "run_id": str(applied.artifacts["run_id"])}
    )
    assert "record" not in envelope["artifacts"]["fidelity"][EXPOSE_ID]
