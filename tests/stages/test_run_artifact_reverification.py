# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The run directory has to reproduce the digest it records (``FLX-2110``).

Neither ``fluxsim verify`` nor ``fluxsim ship`` used to re-derive anything from
the emit streams on disk. ``verify``'s golden leg hashed the *document*
``run-digest.json``; row conformance counted lines and compared row widths
against each other. So a row rewritten in
``<state>/runs/<id>/emits/<expose>.ndjson`` after ``apply`` wrote it --
same line count, same width -- left ``verify`` reporting ``reproducible``
and exit 0, and ``ship`` packing the altered rows into the signed evidence
archive whose in-toto runtime Statement still named the pre-tamper
``runDigest``. Ship's own ``synthetic.outputDigest`` check caught it only
for an emit that declares a fidelity block; an emit with no block shipped
unconditionally, and the public FLUX telco example declares none.

Every test here is a negative control on one of KERNEL-SPEC §10's
components: change the artifact, and the component that names it must be
the one reported.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import (
    component_mismatches,
    recompute_components,
    recompute_input_digest,
)
from flux_engine.stages.verify import verify as run_verify

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration

EXPOSE_ID = "payment_recovery_moment"


@pytest.fixture()
def applied(tmp_path: Path) -> RunLayout:
    state = tmp_path / ".flux"
    result = apply(FIXTURE_BUNDLE, state_dir=state)
    assert result.success, result.stderr
    return RunLayout.for_state_dir(state, str(result.artifacts["run_id"]))


def _recorded_components(layout: RunLayout) -> dict:
    document = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    return dict(document["components"])


def _receipt(layout: RunLayout) -> dict:
    return json.loads(layout.receipt_path.read_text(encoding="utf-8"))


def test_a_clean_run_directory_reproduces_every_recorded_component(applied: RunLayout) -> None:
    """The positive control. Without it the negatives below prove nothing."""
    recorded = _recorded_components(applied)
    recomputed = recompute_components(applied, _receipt(applied))
    assert recomputed == recorded
    assert component_mismatches(applied, _receipt(applied), recorded) == ()


def test_every_component_of_the_run_digest_is_recomputed(applied: RunLayout) -> None:
    """No component is quietly skipped -- the map has to cover them all.

    A recomputation that omitted, say, ``delivery`` would compare five of
    seven entries and report the run reproducible over the two it never
    looked at.
    """
    recorded = _recorded_components(applied)
    recomputed = recompute_components(applied, _receipt(applied))
    assert set(recomputed) == set(recorded)
    assert set(recomputed["emits"]) == set(recorded["emits"])
    assert all(value is not None for key, value in recomputed.items() if key != "emits")
    assert all(value is not None for value in recomputed["emits"].values())


def test_an_edited_emit_row_moves_the_emit_component(applied: RunLayout) -> None:
    path = applied.emit_path(EXPOSE_ID)
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row[0] = "EVIL0000000000000000000000000000"
    lines[0] = json.dumps(row, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert component_mismatches(applied, _receipt(applied), _recorded_components(applied)) == (
        f"emits.{EXPOSE_ID}",
    )


@pytest.mark.parametrize(
    ("component", "mutate"),
    [
        ("population", "population.ndjson"),
        ("decisions", "decisions.ndjson"),
    ],
)
def test_an_edited_ledger_moves_its_own_component(
    applied: RunLayout, component: str, mutate: str
) -> None:
    path = applied.run_dir / mutate
    path.write_text((path.read_text(encoding="utf-8") + '{"op": "extra"}\n'), encoding="utf-8")
    moved = component_mismatches(applied, _receipt(applied), _recorded_components(applied))
    assert component in moved


def test_a_missing_artifact_is_a_mismatch_not_a_pass(applied: RunLayout) -> None:
    """An artifact that is gone must never read as one that agrees."""
    applied.emit_path(EXPOSE_ID).unlink()
    moved = component_mismatches(applied, _receipt(applied), _recorded_components(applied))
    assert f"emits.{EXPOSE_ID}" in moved


def test_verify_refuses_a_tampered_emit_stream(applied: RunLayout) -> None:
    """The end-to-end trigger: same line count, same width, different bytes."""
    before = run_verify(
        FIXTURE_BUNDLE,
        run_id=applied.run_id,
        state_dir=applied.state_dir,
        allow_declared_baseline_drift=True,
    )
    assert before.success, before.stderr

    path = applied.emit_path(EXPOSE_ID)
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row[2] = "999999.9999"
    lines[0] = json.dumps(row, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    after = run_verify(
        FIXTURE_BUNDLE,
        run_id=applied.run_id,
        state_dir=applied.state_dir,
        allow_declared_baseline_drift=True,
    )
    assert after.exit_code == 1
    assert after.error_code == "FLX-2110"
    assert after.artifacts["artifactComponents"]["movedComponents"] == [f"emits.{EXPOSE_ID}"]
    assert f"emits.{EXPOSE_ID}" in after.stderr


def test_ship_packs_nothing_when_the_artifacts_do_not_reproduce(
    applied: RunLayout, tmp_path: Path
) -> None:
    """The emit here declares a fidelity block, but the refusal comes before the gate.

    The same refusal covers an emit with no block, which is the case that
    had no check at all: nothing between an edited stream and a signed
    archive naming a runDigest those rows do not produce.
    """
    path = applied.emit_path(EXPOSE_ID)
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row[0] = "sub-EVIL"
    lines[0] = json.dumps(row, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    out_dir = tmp_path / "dist"
    result = ship(
        FIXTURE_BUNDLE, run_id=applied.run_id, out_dir=out_dir, state_dir=applied.state_dir
    )
    assert result.exit_code == 1
    assert result.error_code == "FLX-8001"
    assert "FLX-2110" in result.stderr
    assert not out_dir.exists(), "ship wrote an archive over artifacts that do not reproduce"


def test_the_recomputed_input_digest_is_the_pipelines_own(applied: RunLayout) -> None:
    """``verify`` recomputes ``inputDigest`` itself; the two must not drift apart.

    ``recompute_input_digest`` reads the bundle without executing it, so
    it is a second implementation of KERNEL-SPEC §11's document. This pins
    it to the value the pipeline records in the receipt, which is the one
    ``apply`` compared the plan against.
    """
    assert recompute_input_digest(FIXTURE_BUNDLE) == _receipt(applied)["inputDigest"]


def test_lineage_is_rewritten_whole_on_every_apply(applied: RunLayout) -> None:
    """``lineage.ndjson`` was the one artifact ``apply`` appended to.

    Two more RunEvents per re-apply and per replay: a run directory that
    grows without bound over byte-identical inputs, a lineage consumer
    reading repeated executions of one logical run, and an evidence
    archive whose Merkle root moves each time -- against both
    ``layout.py``'s "never appended to" and ``replay.py``'s "overwrites it
    with byte-identical artifacts".
    """

    def events() -> list:
        # `eventTime` is a wall-clock reading and legitimately differs
        # between two applies; everything else about the pair must not.
        return [
            {key: value for key, value in json.loads(line).items() if key != "eventTime"}
            for line in applied.lineage_path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    first = events()
    assert len(first) == 2

    for _ in range(2):
        again = apply(FIXTURE_BUNDLE, state_dir=applied.state_dir)
        assert again.success, again.stderr
        assert again.artifacts["run_id"] == applied.run_id, "a replan must reuse the directory"
        assert events() == first


def test_the_lineage_job_namespace_names_the_simulation(applied: RunLayout) -> None:
    event = json.loads(applied.lineage_path.read_text(encoding="utf-8").splitlines()[0])
    receipt = _receipt(applied)
    assert event["job"]["namespace"] == f"flux://{receipt['simulationId']}"
    assert FIXTURE_BUNDLE.name not in event["job"]["namespace"]
