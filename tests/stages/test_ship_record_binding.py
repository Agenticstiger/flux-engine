# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What ``fluxsim ship`` binds the fidelity record to, and what it says about it.

Five defects in one function (``ship._decide_emit``):

* **The H binding could not fail.** One ``compiled`` object, built from
  the run directory's own copy of the contract, was handed both to
  ``build_record`` (which sets ``record.H := compiled.h.value``) and to
  ``run_gate`` (whose ``binding.H`` check is ``record.h ==
  compiled.h.value``) -- a value compared to itself. Editing only
  ``fidelity-input-<expose>.json`` shipped a signed record bound to a
  contract that was in neither the bundle nor ``plan.json``, inside a
  fully verifying evidence pack.
* **``reference.productRef`` was the exposeId**, so a record could not be
  joined back to the FLUID data product it answers for.
* **``reference.fluidVersion`` read a key nothing writes**, so every
  record declared the wildcard ``"*"`` rather than the pin
  ``resolve_emit`` enforced.
* **``reference.contractDigest`` was a second copy of ``H``**, a different
  quantity, leaving the record with no digest of the referenced
  ``.fluid.yml`` at all.
* **``committedState.age`` was measured against the commitment time ship
  itself had stamped moments earlier**, so the age was identically zero
  and ``budget.maxStateAge`` could never refuse anything.
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


@pytest.fixture()
def applied(tmp_path: Path) -> RunLayout:
    result = apply(FIXTURE_BUNDLE, state_dir=tmp_path / ".flux")
    assert result.success, result.stderr
    return RunLayout.for_state_dir(tmp_path / ".flux", str(result.artifacts["run_id"]))


@pytest.fixture()
def slice_uri(applied: RunLayout, tmp_path: Path) -> str:
    """A committed slice made of the run's own rows, so the gate reaches EMIT."""
    attestation = json.loads(applied.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))
    rows = [
        dict(zip(attestation["columns"], json.loads(line), strict=True))
        for line in applied.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    path = tmp_path / "slice.ndjson"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return f"file://{path}"


def _ship(applied: RunLayout, out_dir: Path, slice_uri: str, **kwargs: object) -> object:
    return ship(
        FIXTURE_BUNDLE,
        run_id=applied.run_id,
        out_dir=out_dir,
        state_dir=applied.state_dir,
        committed_slice={EXPOSE_ID: slice_uri},
        **kwargs,  # type: ignore[arg-type]
    )


def _record(applied: RunLayout) -> dict:
    path = applied.fidelity_record_path(EXPOSE_ID)
    assert path.is_file(), "ship never reached the fidelity gate"
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_records_reference_block_names_four_distinct_things(
    applied: RunLayout, slice_uri: str, tmp_path: Path
) -> None:
    result = _ship(applied, tmp_path / "dist", slice_uri)
    assert result.success, result.stderr  # type: ignore[attr-defined]

    record = _record(applied)
    attestation = json.loads(applied.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))
    plan = json.loads(applied.plan_path.read_text(encoding="utf-8"))["emits"][EXPOSE_ID]
    reference = record["reference"]

    assert reference["productRef"] == plan["productRef"] != EXPOSE_ID
    assert reference["exposeId"] == EXPOSE_ID
    assert reference["fluidVersion"] == attestation["reference"]["fluidVersion"] != "*"
    # KERNEL-SPEC §9's digest of the referenced .fluid.yml, which is not H.
    assert reference["contractDigest"] == attestation["reference"]["contractDigest"]
    assert reference["contractDigest"] != record["H"]
    assert record["H"] == plan["H"]
    assert record["G"] == plan["G"]


def test_a_contract_the_plan_never_saw_withholds(
    applied: RunLayout, slice_uri: str, tmp_path: Path
) -> None:
    """Only ``fidelity-input-<expose>.json`` is edited -- the bundle and plan are untouched."""
    path = applied.fidelity_input_path(EXPOSE_ID)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["contract"]["description"] = "a contract nothing planned"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    out_dir = tmp_path / "dist"
    result = _ship(applied, out_dir, slice_uri)
    assert result.exit_code == 1  # type: ignore[attr-defined]
    assert result.error_code == "FLX-8001"  # type: ignore[attr-defined]
    assert not out_dir.exists()
    assert not applied.fidelity_record_path(EXPOSE_ID).exists()

    failed = {
        check["check"]
        for check in result.artifacts["decisions"][EXPOSE_ID]["checks"]  # type: ignore[attr-defined]
        if not check["passed"]
    }
    assert failed == {"contract.plannedH", "contract.plannedG", "contract.digest"}


def test_the_binding_checks_are_recorded_on_a_clean_ship(
    applied: RunLayout, slice_uri: str, tmp_path: Path
) -> None:
    result = _ship(applied, tmp_path / "dist", slice_uri)
    assert result.success, result.stderr  # type: ignore[attr-defined]
    checks = {
        check["check"]: check
        for check in result.artifacts["decisions"][EXPOSE_ID]["checks"]  # type: ignore[attr-defined]
    }
    assert checks["contract.plannedH"]["passed"] is True
    assert checks["binding.H"]["passed"] is True


def test_the_state_age_reports_itself_unchecked_when_ship_stamped_the_commitment(
    applied: RunLayout, slice_uri: str, tmp_path: Path
) -> None:
    """A control that cannot fire must say so, not report green.

    The fixture declares ``budget.maxStateAge: P30D``. With ship stamping
    ``committedAt`` itself, the age is the distance from one instant to
    the same instant, and the recorded check used to read "the committed
    state is 0 ms old against a declared maximum of 2592000000 ms".
    """
    result = _ship(applied, tmp_path / "dist", slice_uri)
    assert result.success, result.stderr  # type: ignore[attr-defined]
    age = next(
        check
        for check in result.artifacts["decisions"][EXPOSE_ID]["checks"]  # type: ignore[attr-defined]
        if check["check"] == "committedState.age"
    )
    assert age["checked"] is False
    assert "not checked" in age["detail"]


def test_a_declared_commitment_time_makes_the_age_budget_able_to_refuse(
    applied: RunLayout, slice_uri: str, tmp_path: Path
) -> None:
    """Given the owner's own commitment time, the declared maximum bites."""
    out_dir = tmp_path / "dist"
    result = _ship(applied, out_dir, slice_uri, committed_at="2019-01-01T00:00:00.000Z")
    assert result.exit_code == 1  # type: ignore[attr-defined]
    assert result.error_code == "FLX-8001"  # type: ignore[attr-defined]
    assert not out_dir.exists()
    age = next(
        check
        for check in result.artifacts["decisions"][EXPOSE_ID]["checks"]  # type: ignore[attr-defined]
        if check["check"] == "committedState.age"
    )
    assert age["checked"] is True
    assert age["passed"] is False

    # The positive control on the same path: a fresh commitment time passes.
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    fresh = _ship(applied, tmp_path / "dist2", slice_uri, committed_at=now)
    assert fresh.success, fresh.stderr  # type: ignore[attr-defined]


def test_a_committed_slice_for_an_unknown_expose_is_refused_by_name(
    applied: RunLayout, slice_uri: str, tmp_path: Path
) -> None:
    out_dir = tmp_path / "dist"
    result = ship(
        FIXTURE_BUNDLE,
        run_id=applied.run_id,
        out_dir=out_dir,
        state_dir=applied.state_dir,
        committed_slice={"payment_recovery_momnt": slice_uri},
    )
    assert result.exit_code == 2
    assert result.error_code == "FLX-2111"
    assert "payment_recovery_momnt" in result.stderr
    assert EXPOSE_ID in result.stderr
    assert not out_dir.exists()


def test_a_short_row_is_a_catalogued_refusal_not_a_value_error(
    applied: RunLayout, slice_uri: str, tmp_path: Path
) -> None:
    """The pack path used to raise ``zip() argument 2 is shorter`` with ``error.code: null``.

    The artifact re-derivation catches a narrowed stream first, which is
    the point -- but the width guard behind it is reached through
    ``PackedEmit.as_mappings`` now, so nothing on this path can produce an
    uncatalogued ``ValueError``.
    """
    path = applied.emit_path(EXPOSE_ID)
    narrowed = [
        json.dumps(json.loads(line)[:3], separators=(",", ":"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    path.write_text("\n".join(narrowed) + "\n", encoding="utf-8")

    out_dir = tmp_path / "dist"
    result = _ship(applied, out_dir, slice_uri)
    assert result.exit_code == 1  # type: ignore[attr-defined]
    assert result.error_code in {"FLX-8001", "FLX-2110"}  # type: ignore[attr-defined]
    assert not out_dir.exists()


def test_a_malformed_committed_at_is_a_result_not_an_exception(
    applied: RunLayout, slice_uri: str, tmp_path: Path
) -> None:
    """``ship`` returns a ``StageResult``; a bad input must not escape as an exception."""
    out_dir = tmp_path / "dist"
    result = _ship(applied, out_dir, slice_uri, committed_at="not-a-timestamp")
    assert result.exit_code == 1  # type: ignore[attr-defined]
    assert result.error_code == "FLX-5022"  # type: ignore[attr-defined]
    assert not out_dir.exists()
