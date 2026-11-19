# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 3, end to end: ``ship`` ships within tolerance and withholds outside it.

The library-level measurements in ``test_gate_roc.py`` hold the fidelity
check to its declared rules. This holds the *command* to them: the real
``python -m flux_engine.cli ship`` against the shipped
``tests/fixtures/telco-fidelity-small`` bundle, whose fidelity block declares
``mean(amount)`` with an absolute tolerance of 50 and ``support(market)``.

The committed slice is built from the run's own emitted rows (synthetic, the
engine's own output), so the deviation is known exactly:

* the rows as emitted -- mean deviation 0, so the bundle must ship (exit 0);
* every committed ``amount`` raised by 200 -- the mean deviates by exactly
  200 > 50, so ``ship`` must exit 1 with ``FLX-8001`` and write no bundle.

Pass criterion: both exits, and in each case the decision equals the
declared rule ``|mean_synthetic - mean_committed| <= 50`` evaluated on the
two record sets in rational arithmetic.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from decimal import Decimal  # noqa: E402
from fractions import Fraction  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, List  # noqa: E402

import pytest  # noqa: E402

from flux_engine.stages.apply import apply  # noqa: E402
from flux_engine.stages.layout import RunLayout  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402

FAMILY = "gate-roc"
REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"
EXPOSE_ID = "payment_recovery_moment"
TOLERANCE = Fraction(50)


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    state_dir = tmp_path_factory.mktemp("ship-e2e") / ".flux"
    result = apply(BUNDLE, state_dir=state_dir)
    assert result.success, result.stderr
    run_id = str(result.artifacts["run_id"])
    layout = RunLayout.for_state_dir(state_dir, run_id)
    columns = json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))[
        "columns"
    ]
    rows = [
        dict(zip(columns, json.loads(line), strict=True))
        for line in layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert rows
    return {"state_dir": state_dir, "run_id": run_id, "rows": rows}


def _mean(rows: List[Dict[str, Any]]) -> Fraction:
    values = [Fraction(Decimal(str(row["amount"]))) for row in rows if row["amount"] is not None]
    return sum(values, Fraction(0)) / len(values)


def _ship(run: Dict[str, Any], slice_rows: List[Dict[str, Any]], tmp_path: Path) -> Any:
    slice_path = tmp_path / "slice.ndjson"
    slice_path.write_text("".join(json.dumps(row) + "\n" for row in slice_rows), encoding="utf-8")
    out_dir = tmp_path / "dist"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "flux_engine.cli",
            "ship",
            str(BUNDLE),
            "--run-id",
            run["run_id"],
            "-o",
            str(out_dir),
            "--state-dir",
            str(run["state_dir"]),
            "--committed-slice",
            f"{EXPOSE_ID}={slice_path.as_uri()}",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=REPO_ROOT,
    )
    return completed, out_dir


@pytest.mark.parametrize("offset", ["0", "200"])
def test_ship_decides_as_the_declared_rule(
    offset: str, run: Dict[str, Any], tmp_path: Path, accuracy_log: ResultLog
) -> None:
    slice_rows = [
        {
            **row,
            "amount": (
                None
                if row["amount"] is None
                else str(Decimal(str(row["amount"])) + Decimal(offset))
            ),
        }
        for row in run["rows"]
    ]
    deviation = abs(_mean(run["rows"]) - _mean(slice_rows))
    should_ship = deviation <= TOLERANCE
    completed, out_dir = _ship(run, slice_rows, tmp_path)
    output = completed.stdout + completed.stderr
    shipped = completed.returncode == 0
    withheld_as_declared = completed.returncode == 1 and "FLX-8001" in output
    passed = shipped if should_ship else withheld_as_declared
    accuracy_log.record(
        family=FAMILY,
        case=f"ship-end-to-end-offset-{offset}",
        criterion="ship exits 0 within tolerance and 1 with FLX-8001 outside it, as the rule decides",
        passed=passed,
        n=len(run["rows"]),
        metrics={
            "meanDeviation": float(deviation),
            "tolerance": float(TOLERANCE),
            "exitCode": completed.returncode,
            "bundleWritten": out_dir.exists() and any(out_dir.iterdir()),
        },
    )
    if should_ship:
        assert shipped, output[-2000:]
    else:
        assert withheld_as_declared, output[-2000:]
        assert not (out_dir.exists() and any(out_dir.iterdir())), "a withheld ship wrote a bundle"
