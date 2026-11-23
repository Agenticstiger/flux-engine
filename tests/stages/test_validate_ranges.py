# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``validate`` refuses bins that leave their range, and warns about dates outside the run."""

from __future__ import annotations

import importlib
import shutil
from pathlib import Path

import pytest

from flux_engine.stages.validate import validate

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "related-star"
QUANTITY = 'ss_quantity: { range: { min: "1", max: "10" } }'


def _bundle(tmp_path: Path, declaration: str) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE, target)
    path = target / "simulation.flux.yml"
    text = path.read_text(encoding="utf-8")
    assert QUANTITY in text
    path.write_text(text.replace(QUANTITY, declaration), encoding="utf-8")
    return target


def test_a_last_bin_past_the_range_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        'ss_quantity: { range: { min: "1", max: "10" }, bins: ["1","5","11"], '
        'binWeights: ["0.5","0.5"] }',
    )
    result = validate(bundle)
    assert result.exit_code == 1
    assert result.error_code == "FLX-3402"
    assert "ss_quantity" in result.stderr and "leaves the declared range" in result.stderr


def test_bins_inside_the_range_validate(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        'ss_quantity: { range: { min: "1", max: "10" }, bins: ["1","5","10"], '
        'binWeights: ["0.5","0.5"] }',
    )
    assert validate(bundle).exit_code == 0


def test_a_finding_about_dates_is_a_warning_not_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = importlib.import_module("flux_engine.stages.validate")

    monkeypatch.setattr(stage, "declared_range_findings", lambda *_: ("e.d: outside the window",))
    result = validate(_bundle(tmp_path, QUANTITY))
    assert result.exit_code == 0
    assert result.artifacts["warnings"] and "warning: e.d: outside the window" in result.stdout
