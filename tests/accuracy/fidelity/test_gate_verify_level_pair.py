# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 3, end to end: ``verify`` checks a categorical link through the real command.

The library-level case (``test_level_pair_draws.py``) holds the fidelity check
to its rule for a link between two categorical columns. This holds the
*command* to it: ``python -m flux_engine.cli verify --committed-slice`` against
a copy of ``tests/fixtures/telco-fidelity-small`` whose contract gains a
``channel`` column. The copy declares ``market`` drawn given ``channel`` with
``levelWeightsGiven``, and two properties: a ``levelHistogram`` of market
grouped by channel, which checks the link, and a ``levelHistogram`` of
channel.

The committed slice is written from the same (channel, market) counts the
declaration states (synthetic, stated parameters), in two versions:

* matched: the counts as declared, so the fidelity check must pass (exit 0);
* mismatched: each channel takes the other's market shares and keeps its own
  size. The channel counts do not move and every market stays a declared
  level, so neither the channel property nor the support property can fail;
  the (channel, market) link is reversed. The check must fail (exit 1) on a
  margin, with no refusal, which leaves the grouped property as the only one
  that can be outside.

Pass criterion: both exits, with the fidelity leg ``passed`` and ``failed`` as
the declared rule decides, and no refusal in either case.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest
import yaml

from flux_engine.core.digest import digest_of_document
from flux_engine.stages.apply import apply
from tests.accuracy.harness.record import ResultLog

FAMILY = "gate-roc"
REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"
EXPOSE_ID = "payment_recovery_moment"
EXTENSION = "com.agentics.flux/1"

#: The declared (channel, market) counts. Market depends on channel.
TABLE: Dict[str, Dict[str, int]] = {
    "store": {"DE": 40, "FR": 80, "GB": 120, "IE": 160},
    "web": {"DE": 300, "FR": 180, "GB": 90, "IE": 30},
}
TV = {"type": "distance", "measure": "totalVariation", "max": "0.1"}
assert {c: sum(row.values()) for c, row in TABLE.items()} == {"store": 400, "web": 600}


def _bundle(directory: Path) -> Path:
    """Copy the fixture, add ``channel`` to its contract, and declare the link."""
    bundle = directory / "bundle"
    shutil.copytree(FIXTURE, bundle)
    contract_path = bundle / "payment-recovery.fluid.yml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    schema = contract["exposes"][0]["contract"]["schema"]
    schema.append({"name": "channel", "type": "STRING", "required": True})
    contract_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")

    simulation_path = bundle / "simulation.flux.yml"
    simulation = yaml.safe_load(simulation_path.read_text(encoding="utf-8"))
    entry = simulation["extensions"][EXTENSION]["emits"][EXPOSE_ID]
    entry["rows"] = 2000
    fidelity = entry["fidelity"]
    fidelity["columns"]["channel"] = {
        "support": {"values": sorted(TABLE)},
        "maxCardinality": 4,
        "null": "fail",
        # A root's levels are shares; the given table's weights may be counts.
        "levels": {"store": "0.4", "web": "0.6"},
    }
    fidelity["columns"]["market"]["given"] = "channel"
    fidelity["columns"]["market"]["levelWeightsGiven"] = {
        channel: {market: str(count) for market, count in row.items()}
        for channel, row in TABLE.items()
    }
    fidelity["properties"] = [
        {"property": "support", "column": "market", "tolerance": {"type": "count", "max": 0}},
        {"property": "levelHistogram", "column": "market", "groupBy": "channel", "tolerance": TV},
        {"property": "levelHistogram", "column": "channel", "tolerance": TV},
    ]
    emit = simulation["spec"]["emits"][0]
    emit["provenance"]["contractDigest"] = digest_of_document(
        contract, subject="contractDigest"
    ).value
    simulation_path.write_text(yaml.safe_dump(simulation, sort_keys=False), encoding="utf-8")
    return bundle


def _slice(table: Mapping[str, Mapping[str, int]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for channel, row in table.items():
        for market, count in row.items():
            for _ in range(count):
                n = len(rows) + 1
                rows.append(
                    {
                        "subscriber_id": f"s-{n:05d}",
                        "aborted_at": f"2026-08-{1 + n % 28:02d}T12:00:00Z",
                        "amount": "100.0000",
                        "market": market,
                        "channel": channel,
                    }
                )
    return rows


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    root = tmp_path_factory.mktemp("verify-level-pair")
    bundle = _bundle(root)
    state_dir = root / ".flux"
    result = apply(bundle, state_dir=state_dir)
    assert result.success, result.stderr
    return {"bundle": bundle, "state_dir": state_dir, "run_id": str(result.artifacts["run_id"])}


def _verify(run: Mapping[str, Any], rows: List[Dict[str, Any]], tmp_path: Path) -> Any:
    slice_path = tmp_path / "slice.ndjson"
    slice_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "flux_engine.cli",
            "verify",
            str(run["bundle"]),
            "--run-id",
            run["run_id"],
            "--state-dir",
            str(run["state_dir"]),
            "--committed-slice",
            f"{EXPOSE_ID}={slice_path.as_uri()}",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=REPO_ROOT,
    )


@pytest.mark.parametrize("pairing", ["matched", "mismatched"])
def test_verify_decides_the_link_as_the_declared_rule(
    pairing: str, run: Dict[str, Any], tmp_path: Path, accuracy_log: ResultLog
) -> None:
    table: Mapping[str, Mapping[str, int]] = TABLE
    if pairing == "mismatched":
        # Each channel takes the other's market shares and keeps its own
        # size: the channel counts stay, and the (channel, market) link reverses.
        table = {
            "store": {m: TABLE["web"][m] * 400 // 600 for m in TABLE["web"]},
            "web": {m: TABLE["store"][m] * 600 // 400 for m in TABLE["store"]},
        }
        assert {c: sum(row.values()) for c, row in table.items()} == {
            c: sum(row.values()) for c, row in TABLE.items()
        }
    completed = _verify(run, _slice(table), tmp_path)
    report = json.loads(completed.stdout)
    fidelity = report["artifacts"]["fidelity"][EXPOSE_ID]
    status = fidelity["status"]
    should_pass = pairing == "matched"
    passed = fidelity["refusals"] == [] and (
        completed.returncode == 0 and status == "passed"
        if should_pass
        else completed.returncode == 1 and status == "failed"
    )
    accuracy_log.record(
        family=FAMILY,
        case=f"verify-level-pair-{pairing}",
        criterion=(
            "verify passes a link drawn with levelWeightsGiven and fails a slice "
            "whose link is reversed"
        ),
        passed=passed,
        n=2000,
        metrics={
            "exitCode": completed.returncode,
            "fidelity": status,
            "refusals": len(fidelity["refusals"]),
        },
    )
    assert passed, (completed.returncode, completed.stdout[-3000:], completed.stderr[-2000:])
