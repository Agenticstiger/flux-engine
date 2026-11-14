# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim validate`` and ``fluxsim plan`` print the refusal ``verify`` prints.

The same malformed property, a ``levelHistogram`` that carries ``bins``, used
to pass both commands and be refused by ``fluxsim verify`` with the line
pinned below, captured from ``verify --committed-slice`` before either
command checked the block. Through the real binary: the line, the pointer
after it, the ``--json`` envelope's code, and that a refused command writes
nothing.
"""

from __future__ import annotations

from pathlib import Path

from flux_engine._links import error_url
from tests.bundles import bundle_with_properties

from .conftest import FIXTURE_BUNDLE, parse_json_stdout, run_flux

#: What ``fluxsim verify --committed-slice`` printed for this bundle before
#: either command checked the block.
VERIFY_SAYS = (
    "FLX-5030: the fidelity block is malformed -- fidelity.properties[1]: a levelHistogram "
    "counts its column's declared support levels and carries no bins"
)
POINTER = f"more: fluxsim explain FLX-5030  {error_url('FLX-5030')}"


def _malformed(tmp_path: Path) -> Path:
    return bundle_with_properties(
        FIXTURE_BUNDLE,
        tmp_path / "bundle",
        (
            '{ property: mean, column: amount, tolerance: { type: absolute, value: "50" } }',
            "{ property: levelHistogram, column: market, bins: ["
            '"0", "1"], tolerance: { type: distance, measure: totalVariation, max: "0.05" } }',
        ),
    )


def test_validate_prints_the_refusal_verify_prints(tmp_path: Path) -> None:
    bundle = _malformed(tmp_path)

    result = run_flux(["validate", str(bundle)], cwd=tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [f"sim-payment-recovery: {VERIFY_SAYS}", POINTER]
    assert not (tmp_path / ".flux").exists()


def test_validate_json_names_the_code(tmp_path: Path) -> None:
    bundle = _malformed(tmp_path)

    envelope = parse_json_stdout(run_flux(["validate", str(bundle), "--json"], cwd=tmp_path))

    assert envelope["ok"] is False
    assert envelope["exitCode"] == 1
    assert envelope["error"] == {
        "code": "FLX-5030",
        "message": f"sim-payment-recovery: {VERIFY_SAYS}",
    }


def test_plan_prints_the_refusal_verify_prints_and_writes_no_run(tmp_path: Path) -> None:
    bundle = _malformed(tmp_path)
    state = tmp_path / ".flux"

    result = run_flux(["plan", str(bundle), "--state-dir", str(state)], cwd=tmp_path)

    assert result.returncode == 1
    assert result.stderr.splitlines() == [VERIFY_SAYS, POINTER]
    assert not state.exists()

    envelope = parse_json_stdout(
        run_flux(["plan", str(bundle), "--state-dir", str(state), "--json"], cwd=tmp_path)
    )
    assert envelope["error"] == {"code": "FLX-5030", "message": VERIFY_SAYS}


def test_a_valid_fidelity_bundle_validates_and_plans_as_before(
    bundle_dir: Path, tmp_path: Path
) -> None:
    validated = run_flux(["validate", str(bundle_dir)], cwd=tmp_path)
    assert validated.returncode == 0, validated.stderr
    assert validated.stdout.strip() == (
        f"{bundle_dir}: valid -- 20 FLUX document(s), 1 FLUID contract(s), 1 Simulation(s)"
    )

    state = tmp_path / ".flux"
    planned = run_flux(["plan", str(bundle_dir), "--state-dir", str(state)], cwd=tmp_path)
    assert planned.returncode == 0, planned.stderr
    assert (state / "runs").is_dir()
