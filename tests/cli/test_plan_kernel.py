# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim plan --kernel``: a new plan takes the kernel it is asked for (ADR 0010, step 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.cli import main
from flux_engine.cli.commands.plan import KERNEL_CHOICES
from flux_engine.core import kernels
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

pytestmark = pytest.mark.integration


def test_the_flag_offers_exactly_the_kernels_the_engine_implements() -> None:
    assert KERNEL_CHOICES == tuple(sorted(kernels.KERNELS))


def test_the_help_names_the_default_the_engine_takes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["plan", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert f"(default {kernels.DEFAULT_KERNEL.version})" in help_text


@pytest.mark.parametrize(
    ("asked", "seed_tree"),
    [(None, "flux-seed/2"), ("0.1.0", "flux-seed/1"), ("0.2.0", "flux-seed/2")],
)
def test_the_plan_records_the_kernel_it_was_asked_for(
    asked: str, seed_tree: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["-j", "plan", str(BUNDLE), "--state-dir", str(tmp_path / ".flux")]
    if asked is not None:
        argv += ["--kernel", asked]
    assert main(argv) == 0
    run_id = json.loads(capsys.readouterr().out)["artifacts"]["run_id"]
    document = json.loads(
        RunLayout.for_state_dir(tmp_path / ".flux", run_id).plan_path.read_text(encoding="utf-8")
    )
    assert document["kernelVersion"] == (asked or kernels.DEFAULT_KERNEL.version)
    assert document["seedTreeVersion"] == seed_tree


def test_an_unknown_kernel_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["plan", str(BUNDLE), "--state-dir", str(tmp_path / ".flux"), "--kernel", "9.9.9"])
    assert exited.value.code == 2
    assert "9.9.9" in capsys.readouterr().err


def test_the_library_refuses_an_unknown_kernel_with_flx_2119(tmp_path: Path) -> None:
    result = plan(BUNDLE, state_dir=tmp_path / ".flux", kernel="9.9.9")
    assert result.exit_code == 1
    assert result.error_code == "FLX-2119"
    assert "9.9.9" in result.stderr and "0.2.0" in result.stderr
    assert not (tmp_path / ".flux" / "runs").exists()
