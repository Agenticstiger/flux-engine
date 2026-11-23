# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The whole six-step loop (``validate -> plan -> apply -> run -> verify ->
ship``), plus ``replay``, driven entirely through the real binary --
``python -m flux_engine.cli`` as a subprocess, exit codes asserted at every
step. No in-process shortcuts: this is what a shell user actually sees.

Two bundles, for two different guarantees:

- ``tests/fixtures/telco-fidelity-small`` (population 500) exercises the
  loop's *withholding* path -- a declared fidelity block with no
  ``--committed-slice`` withholds ``ship`` (``FLX-8001``), which is a
  documented, correct outcome, not a build failure.
- The FLUX spec repo's public example (``FLUX/examples/telco-payment-
  recovery``, no declared fidelity block) exercises the loop's *success*
  path end to end, including a real ``ship`` and a real ``replay``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL_FIXTURE_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"
FLUX_SPEC_REPO = REPO_ROOT.parent / "FLUX"
PUBLIC_EXAMPLE = FLUX_SPEC_REPO / "examples" / "telco-payment-recovery"

pytestmark = pytest.mark.integration


def _flux(args: List[str], *, cwd: Path) -> subprocess.CompletedProcess:
    argv = [sys.executable, "-m", "flux_engine.cli", "--json", *args]
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=600
    )  # run --verify --ship executes the 25,000-subject world twice (~142 s quiet)


def _artifacts(result: subprocess.CompletedProcess) -> dict:
    return json.loads(result.stdout)["artifacts"]


def test_validate_step_exits_zero_on_a_conforming_bundle(tmp_path: Path) -> None:
    result = _flux(["validate", str(SMALL_FIXTURE_BUNDLE)], cwd=tmp_path)
    assert result.returncode == 0, result.stderr


def test_validate_step_exits_one_on_a_bundle_directory_that_does_not_exist(tmp_path: Path) -> None:
    result = _flux(["validate", str(tmp_path / "no-such-bundle")], cwd=tmp_path)
    assert result.returncode == 1


def test_six_step_loop_withholds_ship_without_a_committed_slice(tmp_path: Path) -> None:
    """validate -> plan -> apply -> verify -> ship, all six real
    invocations, on a bundle whose one emit declares a fidelity block:
    every step through ``verify`` exits 0, and ``ship`` -- correctly --
    exits 1 with ``FLX-8001``, having withheld rather than shipped an
    unproven bundle.
    """
    state_dir = tmp_path / ".flux"

    validated = _flux(["validate", str(SMALL_FIXTURE_BUNDLE)], cwd=tmp_path)
    assert validated.returncode == 0, validated.stderr

    planned = _flux(
        ["plan", str(SMALL_FIXTURE_BUNDLE), "--state-dir", str(state_dir)], cwd=tmp_path
    )
    assert planned.returncode == 0, planned.stderr
    run_id = _artifacts(planned)["run_id"]

    applied = _flux(
        ["apply", str(SMALL_FIXTURE_BUNDLE), "--run-id", run_id, "--state-dir", str(state_dir)],
        cwd=tmp_path,
    )
    assert applied.returncode == 0, applied.stderr

    verified = _flux(
        [
            "verify",
            str(SMALL_FIXTURE_BUNDLE),
            "--run-id",
            run_id,
            "--state-dir",
            str(state_dir),
        ],
        cwd=tmp_path,
    )
    assert verified.returncode == 0, verified.stderr

    shipped = _flux(
        [
            "ship",
            str(SMALL_FIXTURE_BUNDLE),
            "--run-id",
            run_id,
            "--state-dir",
            str(state_dir),
            "-o",
            str(tmp_path / "dist"),
        ],
        cwd=tmp_path,
    )
    assert shipped.returncode == 1
    assert "FLX-8001" in shipped.stderr
    assert _artifacts(shipped)["events"][0]["type"] == "com.agentics.flux.ship.withheld"


def test_replay_of_the_just_applied_run_succeeds_and_a_wrong_digest_fails(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / ".flux"
    planned = _flux(
        ["plan", str(SMALL_FIXTURE_BUNDLE), "--state-dir", str(state_dir)], cwd=tmp_path
    )
    run_id = _artifacts(planned)["run_id"]
    applied = _flux(
        ["apply", str(SMALL_FIXTURE_BUNDLE), "--run-id", run_id, "--state-dir", str(state_dir)],
        cwd=tmp_path,
    )
    assert applied.returncode == 0, applied.stderr
    run_digest = _artifacts(applied)["run_digest"]

    good_replay = _flux(
        [
            "replay",
            str(SMALL_FIXTURE_BUNDLE),
            "--run-id",
            run_id,
            "--assert-digest",
            run_digest,
            "--state-dir",
            str(state_dir),
        ],
        cwd=tmp_path,
    )
    assert good_replay.returncode == 0, good_replay.stderr

    bad_replay = _flux(
        [
            "replay",
            str(SMALL_FIXTURE_BUNDLE),
            "--run-id",
            run_id,
            "--assert-digest",
            "sha256:" + "0" * 64,
            "--state-dir",
            str(state_dir),
        ],
        cwd=tmp_path,
    )
    assert bad_replay.returncode == 1
    assert "FLX-2106" in bad_replay.stderr


def test_run_sugar_plan_apply_verify_in_one_invocation(tmp_path: Path) -> None:
    """``fluxsim run --verify`` -- the loop's own sugar for step-by-step
    plan/apply/verify, in one process.
    """
    state_dir = tmp_path / ".flux"
    result = _flux(
        ["run", str(SMALL_FIXTURE_BUNDLE), "--verify", "--state-dir", str(state_dir)],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    artifacts = _artifacts(result)
    assert artifacts["plan"]["run_id"] == artifacts["apply"]["run_id"]
    assert "verify" in artifacts


_requires_flux_spec_checkout = pytest.mark.skipif(
    not PUBLIC_EXAMPLE.is_dir(), reason=f"sibling FLUX spec checkout not found at {FLUX_SPEC_REPO}"
)


@_requires_flux_spec_checkout
def test_full_six_step_loop_ships_and_replays_successfully(tmp_path: Path) -> None:
    """The success path, start to finish, on the public example (no
    declared fidelity block -> nothing for ``ship`` to withhold on):
    validate, plan, apply, run --verify --ship (the loop's own sugar for
    the last three steps), then replay against the shipped run's own
    digest. Every step exits 0.
    """
    state_dir = tmp_path / ".flux"
    out_dir = tmp_path / "dist"

    validated = _flux(["validate", str(PUBLIC_EXAMPLE)], cwd=tmp_path)
    assert validated.returncode == 0, validated.stderr

    planned = _flux(["plan", str(PUBLIC_EXAMPLE), "--state-dir", str(state_dir)], cwd=tmp_path)
    assert planned.returncode == 0, planned.stderr
    run_id = _artifacts(planned)["run_id"]

    ran = _flux(
        [
            "run",
            str(PUBLIC_EXAMPLE),
            "--verify",
            # The public example declares
            # `spec.golden.baselineDigest: sha256:9f2c1a0b1e4d...` -- a
            # hand-typed hex ramp, not a digest of anything. No literal
            # could replace it: the bundle's own bytes feed `inputDigest`,
            # which feeds `runDigest`, so writing a run's digest into that
            # YAML moves the digest (see
            # tests/stages/test_verify_declared_baseline.py). `fluxsim verify`
            # is fail-closed on a declared-vs-actual mismatch, so this walk
            # says out loud that it accepts the example's stale
            # declaration rather than the gate quietly exiting 0 over it.
            # Remove this flag once the example drops the block.
            "--allow-declared-baseline-drift",
            "--ship",
            "-o",
            str(out_dir),
            "--state-dir",
            str(state_dir),
        ],
        cwd=tmp_path,
    )
    assert ran.returncode == 0, ran.stderr
    ran_artifacts = _artifacts(ran)
    assert ran_artifacts["apply"]["run_id"] == run_id
    archive = Path(ran_artifacts["ship"]["archive"])
    assert archive.is_file(), archive

    replayed = _flux(
        [
            "replay",
            str(PUBLIC_EXAMPLE),
            "--run-id",
            run_id,
            "--assert-digest",
            ran_artifacts["apply"]["run_digest"],
            "--state-dir",
            str(state_dir),
        ],
        cwd=tmp_path,
    )
    assert replayed.returncode == 0, replayed.stderr


# ---------------------------------------------------------------------------
# Build item 6: register_lint.py over --help of every subcommand, and over
# the generated EVIDENCE.md.
# ---------------------------------------------------------------------------


def test_register_lint_script_passes_over_every_subcommand_help() -> None:
    """``scripts/register_lint.py`` itself already recurses every subparser's
    ``--help`` (``_iter_help_texts`` in that script) -- run the real script
    as a subprocess and report its verbatim exit code/output, rather than
    re-implementing its traversal here.
    """
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "register_lint.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"register_lint.py failed over README/docs/--help:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "passed" in result.stdout


@_requires_flux_spec_checkout
def test_register_lint_vocabulary_also_passes_over_a_generated_evidence_md(
    tmp_path: Path,
) -> None:
    """``scripts/register_lint.py`` does not itself scan a run's generated
    ``EVIDENCE.md`` (only README.md, docs/, and every subcommand's
    ``--help``) -- ships the public example to produce a real one, then
    reuses that script's own ``_scan_text``/``BANNED_WORDS`` (imported, not
    re-typed) against it, so this is the same rule applied to the one
    generated-document surface the script's own traversal does not reach.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import register_lint  # noqa: E402 - deliberately imported after sys.path edit

    from flux_engine.stages.apply import apply
    from flux_engine.stages.layout import RunLayout
    from flux_engine.stages.ship import ship

    state_dir = tmp_path / ".flux"
    applied = apply(PUBLIC_EXAMPLE, state_dir=state_dir)
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])
    shipped = ship(PUBLIC_EXAMPLE, run_id=run_id, out_dir=tmp_path / "dist", state_dir=state_dir)
    assert shipped.success, shipped.stderr

    evidence_path = RunLayout.for_state_dir(state_dir, run_id).run_dir / "EVIDENCE.md"
    assert evidence_path.is_file(), evidence_path
    evidence_text = evidence_path.read_text(encoding="utf-8")

    hits = register_lint._scan_text("EVIDENCE.md", evidence_text)  # noqa: SLF001
    assert hits == [], f"banned vocabulary found in a generated EVIDENCE.md: {hits}"
