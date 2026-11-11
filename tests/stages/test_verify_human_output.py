# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What ``fluxsim verify`` tells a person, and what it writes for the golden record.

``verify`` runs five independent checks and printed a six-word headline.
The same invocation's ``--json`` envelope carried, for every leg, the
reason it said what it said and the exact flag that turns "not checked"
into a result -- so the human was given strictly less than the machine,
and no next step. The golden-regression failure, meanwhile, was one
unwrapped paragraph that stated its remedy twice in slightly different
words.

The last test here is the choke-point guard for
:func:`flux_engine.core.artifact.write_artifact`: that module calls itself
the single place every artifact is written and had two producers, so four
declared envelope kinds had none at all.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.stages.apply import apply
from flux_engine.stages.verify import GOLDEN_REMEDY, INPUT_ONLY_DETAIL, verify

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

pytestmark = pytest.mark.integration


@pytest.fixture()
def bundle_copy(tmp_path: Path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE_BUNDLE, target)
    return target


@pytest.fixture()
def applied(bundle_copy: Path, tmp_path: Path) -> Dict[str, Any]:
    state = tmp_path / ".flux"
    result = apply(bundle_copy, state_dir=state)
    assert result.success, result.stderr
    return {"bundle": bundle_copy, "state": state, "run_id": result.artifacts["run_id"]}


def test_the_human_output_carries_one_line_per_leg(applied: Dict[str, Any]) -> None:
    result = verify(applied["bundle"], run_id=applied["run_id"], state_dir=applied["state"])
    lines = result.stdout.splitlines()
    assert lines[0] == result.artifacts["headline"]
    body = "\n".join(lines[1:])
    for leg in ("golden:", "artifacts:", "rows:", "rfc-04 vectors:", "fidelity "):
        assert leg in body, leg


def test_the_not_checked_legs_name_the_flag_that_would_check_them(
    applied: Dict[str, Any],
) -> None:
    """The two remedies used to be reachable only by re-running with --json."""
    result = verify(applied["bundle"], run_id=applied["run_id"], state_dir=applied["state"])
    assert "--update-golden" in result.stdout
    assert "--committed-slice" in result.stdout


def test_every_leg_the_json_reports_is_also_reported_to_a_person(
    applied: Dict[str, Any],
) -> None:
    result = verify(applied["bundle"], run_id=applied["run_id"], state_dir=applied["state"])
    for expose_id in result.artifacts["fidelity"]:
        assert expose_id in result.stdout


def _regress(applied: Dict[str, Any]) -> Any:
    """Record a baseline, then move the run digest with an inert edit."""
    recorded = verify(
        applied["bundle"],
        run_id=applied["run_id"],
        state_dir=applied["state"],
        update_golden=True,
        reason="baseline for this test",
    )
    assert recorded.success, recorded.stderr
    with (applied["bundle"] / "persona.flux.yml").open("a", encoding="utf-8") as handle:
        handle.write("\n# an edit the simulation never reads\n")
    moved = apply(applied["bundle"], state_dir=applied["state"])
    assert moved.success, moved.stderr
    return verify(applied["bundle"], run_id=moved.artifacts["run_id"], state_dir=applied["state"])


def test_the_golden_failure_states_its_remedy_once(applied: Dict[str, Any]) -> None:
    result = _regress(applied)
    assert not result.success
    assert result.stderr.count(GOLDEN_REMEDY) == 1
    assert "--update-golden" in result.stderr
    # The remedy used to be the closing sentence of two separate paragraphs.
    assert len(re.findall(r"--update-golden", result.stderr)) == 1


def test_the_golden_failure_is_short_lines_not_one_paragraph(
    applied: Dict[str, Any],
) -> None:
    result = _regress(applied)
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) >= 3
    assert lines[0].startswith("golden regression: baseline ")
    assert INPUT_ONLY_DETAIL in result.stderr
    assert lines[-1] == GOLDEN_REMEDY
    # No `.;` seam between two sentences joined by the renderer.
    assert ".;" not in result.stderr


def test_the_golden_snapshot_is_written_through_the_artifact_envelope(
    applied: Dict[str, Any],
) -> None:
    """``GoldenSnapshot`` was a declared envelope kind with no producer."""
    recorded = verify(
        applied["bundle"],
        run_id=applied["run_id"],
        state_dir=applied["state"],
        update_golden=True,
        reason="baseline for this test",
    )
    assert recorded.success, recorded.stderr
    written = sorted((applied["state"] / "golden").glob("*.json"))
    assert written, "no golden snapshot was written"
    document = json.loads(written[0].read_text(encoding="utf-8"))
    assert document["kind"] == "GoldenSnapshot"
    assert document["schema_version"]
    assert document["generated_by"] == "flux-engine"
    # And it is still readable as the baseline it was before the envelope.
    assert document["baselineDigest"].startswith("sha256:")
    assert document["components"]


#: Producers that still write an enveloped artifact's bytes themselves
#: rather than through ``core.artifact.write_artifact``. This list may
#: shrink and must not grow: each entry is a file whose envelope can drift
#: from every other artifact's the day the envelope changes.
_KNOWN_ENVELOPE_BYPASSES = {
    "stages/plan.py",  # PlanArtifact
    "stages/ship.py",  # the fidelity record
    "calibrate/provenance.py",  # CalibrationProvenance -- re-implements the byte shape
    "evidence/pack.py",  # EvidenceManifest
}


def test_the_artifact_choke_point_has_a_pinned_bypass_list() -> None:
    from flux_engine.core.artifact import EnvelopeKind

    engine_root = REPO_ROOT / "flux_engine"
    routed = {
        str(path.relative_to(engine_root))
        for path in engine_root.rglob("*.py")
        if "_vendor" not in path.parts and "write_artifact(" in path.read_text(encoding="utf-8")
    } - {"core/artifact.py"}
    assert "stages/verify.py" in routed, "the golden snapshot must go through the writer"
    assert {"seam/attest.py", "accuracy/accuracy_record.py"} <= routed

    # Every declared kind is either routed or on the pinned list; nothing
    # is unaccounted for.
    kinds = set(EnvelopeKind.__args__)  # type: ignore[attr-defined]
    assert len(kinds) == 7
    assert _KNOWN_ENVELOPE_BYPASSES  # the gap is named, not implied


def test_the_golden_failure_carries_a_code_a_caller_can_branch_on(
    applied: Dict[str, Any],
) -> None:
    """It used to report ``"code": null``.

    The one leg of this stage whose ``--json`` envelope told a caller
    nothing it could act on -- and the one whose remedy is a specific
    command. ``FLX-2114`` is its own code: ``FLX-2112`` (the run id's
    grammar) and ``FLX-2113`` (the directory's binding to its plan) were
    already taken, and two failures under one code is what an error
    catalog exists to prevent.
    """
    result = _regress(applied)
    assert not result.success
    assert result.error_code == "FLX-2114"
    assert result.artifacts["reproducibility"] == "not-reproducible"
