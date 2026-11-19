# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from flux_engine.stages.validate import validate


def test_validate_the_fixture_bundle_succeeds(bundle_dir: Path) -> None:
    result = validate(bundle_dir)
    assert result.success, result.stderr
    assert result.artifacts["fluxDocuments"] > 0
    simulations = result.artifacts["simulations"]
    assert "sim-payment-recovery" in simulations
    assert simulations["sim-payment-recovery"]["fidelity"] == "deterministic"
    assert simulations["sim-payment-recovery"]["emitsWithFidelity"] == ["payment_recovery_moment"]


def test_validate_a_missing_directory_fails(tmp_path: Path) -> None:
    result = validate(tmp_path / "does-not-exist")
    assert not result.success


def test_validate_never_creates_a_run_directory(bundle_dir: Path, tmp_path: Path) -> None:
    before = set(tmp_path.iterdir())
    validate(bundle_dir)
    assert set(tmp_path.iterdir()) == before
