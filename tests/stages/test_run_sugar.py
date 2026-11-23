# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import flux_engine.stages.run  # noqa: F401 - imported so sys.modules holds the MODULE
from flux_engine.stages.run import run


def test_run_without_flags_only_plans_and_applies(bundle_dir: Path, state_dir: Path) -> None:
    result = run(bundle_dir, state_dir=state_dir)
    assert result.success, result.stderr
    assert "plan" in result.artifacts
    assert "apply" in result.artifacts
    assert "verify" not in result.artifacts
    assert "ship" not in result.artifacts


def test_run_ship_without_out_dir_is_a_usage_error(bundle_dir: Path, state_dir: Path) -> None:
    result = run(bundle_dir, state_dir=state_dir, ship=True)
    assert result.exit_code == 2


def test_run_ship_true_withholds_when_no_committed_slice_is_given(
    bundle_dir: Path, state_dir: Path, tmp_path: Path
) -> None:
    """The fixture's one emit declares a fidelity block; ship needs a slice for it."""
    result = run(bundle_dir, state_dir=state_dir, ship=True, out_dir=tmp_path / "dist")
    assert not result.success
    assert result.artifacts["ship"]["code"] == "FLX-8001"
    assert "plan" in result.artifacts and "apply" in result.artifacts


def test_run_verify_true_succeeds_on_a_first_run_with_no_golden_baseline_yet(
    bundle_dir: Path, state_dir: Path
) -> None:
    """No <state_dir>/golden/ file yet: NOT_CHECKED is honest, not a failure."""
    result = run(bundle_dir, state_dir=state_dir, verify=True)
    assert result.success, result.stderr
    assert result.artifacts["verify"]["reproducibility"] == "not-checked"
    assert "ship" not in result.artifacts


def test_run_verify_true_fails_once_the_bundle_moves_past_a_pinned_baseline(
    bundle_dir: Path, state_dir: Path
) -> None:
    from flux_engine.stages.verify import verify as run_verify

    first = run(bundle_dir, state_dir=state_dir, verify=True)
    assert first.success, first.stderr
    pinned = run_verify(
        bundle_dir,
        run_id=first.artifacts["plan"]["run_id"],
        state_dir=state_dir,
        update_golden=True,
        reason="pin a baseline for this test",
    )
    assert pinned.success, pinned.stderr

    second = run(bundle_dir, state_dir=state_dir, verify=True)
    assert second.success, second.stderr
    assert second.artifacts["verify"]["reproducibility"] == "reproducible"


def test_a_requested_stage_the_run_stopped_before_says_it_did_not_run(
    bundle_dir: Path, state_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    """The absence of a key is not a way to say "this did not happen".

    ``fluxsim run --verify --ship`` that stopped at ``verify`` returned an
    artifacts map with no ``ship`` key at all, which reads exactly like a
    ship that ran and produced nothing -- and a caller reaching for
    ``artifacts["ship"]`` got a ``KeyError`` rather than an answer.
    """
    from flux_engine.api.stages import StageResult

    run_module = sys.modules["flux_engine.stages.run"]

    monkeypatch.setattr(
        run_module,
        "run_verify",
        lambda *_args, **_kwargs: StageResult(stage="verify", exit_code=1, stderr="a leg failed"),
    )
    result = run(bundle_dir, state_dir=state_dir, verify=True, ship=True, out_dir=tmp_path / "dist")
    assert result.exit_code == 1
    assert result.artifacts["ship"] == {"ran": False, "reason": "verify exited 1"}
    assert "ship: not run (verify exited 1)" in result.stdout
    assert not (tmp_path / "dist").exists()


def test_an_apply_that_failed_accounts_for_both_stages_it_skipped(
    bundle_dir: Path, state_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    from flux_engine.api.stages import StageResult

    run_module = sys.modules["flux_engine.stages.run"]

    monkeypatch.setattr(
        run_module,
        "run_apply",
        lambda *_args, **_kwargs: StageResult(stage="apply", exit_code=1, stderr="apply failed"),
    )
    result = run(bundle_dir, state_dir=state_dir, verify=True, ship=True, out_dir=tmp_path / "dist")
    assert result.exit_code == 1
    for stage in ("verify", "ship"):
        assert result.artifacts[stage] == {"ran": False, "reason": "apply exited 1"}
        assert f"{stage}: not run (apply exited 1)" in result.stdout


def test_a_stage_nobody_asked_for_is_not_reported_at_all(
    bundle_dir: Path, state_dir: Path, monkeypatch
) -> None:
    """The negative control: ``not run`` is about stages that were requested."""
    from flux_engine.api.stages import StageResult

    run_module = sys.modules["flux_engine.stages.run"]

    monkeypatch.setattr(
        run_module,
        "run_apply",
        lambda *_args, **_kwargs: StageResult(stage="apply", exit_code=1, stderr="apply failed"),
    )
    result = run(bundle_dir, state_dir=state_dir)
    assert "verify" not in result.artifacts
    assert "ship" not in result.artifacts


def test_run_passes_its_jobs_on_to_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib import import_module

    from flux_engine.api.stages import StageResult

    run_module = import_module("flux_engine.stages.run")
    seen = {}

    def fake(*args: object, **kwargs: object) -> StageResult:
        seen.update(kwargs)
        return StageResult(stage="verify", exit_code=0)

    monkeypatch.setattr(run_module, "run_verify", fake)
    from flux_engine.stages.run import run

    result = run(
        Path(__file__).resolve().parents[1] / "fixtures" / "related-star",
        state_dir=tmp_path / ".flux",
        verify=True,
        jobs=3,
    )
    assert result.success, result.stderr
    assert seen["jobs"] == 3


@pytest.mark.integration
def test_the_parameters_line_is_printed_once_by_run_and_once_by_each_command_alone(
    tmp_path: Path,
) -> None:
    from flux_engine.cli._init import run_init
    from flux_engine.stages.plan import plan
    from flux_engine.stages.ship import ship
    from flux_engine.stages.verify import verify

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "private-telco"
    profile = fixture / "telco.profile.json"
    made = run_init(
        str(tmp_path / "bundle"),
        contract=str(fixture / "telco.fluid.yaml"),
        profile=str(profile),
    )
    assert made.exit_code == 0, made.stderr
    bundle = tmp_path / "bundle"

    def count(text: str) -> int:
        return sum(1 for line in text.splitlines() if line.strip().startswith("parameters: "))

    whole = run(
        bundle,
        state_dir=tmp_path / "a",
        profile=profile,
        verify=True,
        ship=True,
        out_dir=tmp_path / "out",
    )
    assert whole.success, whole.stderr
    assert count(whole.stdout) == 1

    planned = plan(bundle, state_dir=tmp_path / "b", profile=profile)
    assert count(planned.stdout) == 1
    run_id = str(planned.artifacts["run_id"])
    from flux_engine.stages.apply import apply

    assert apply(bundle, state_dir=tmp_path / "b", run_id=run_id, profile=None).success
    verified = verify(bundle, state_dir=tmp_path / "b", run_id=run_id, profile=profile)
    assert count(verified.stdout) == 1
    shipped = ship(
        bundle, state_dir=tmp_path / "b", run_id=run_id, profile=profile, out_dir=tmp_path / "o2"
    )
    assert count(shipped.stdout) == 1
