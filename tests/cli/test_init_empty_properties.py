# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A fidelity block may list no property, and a table with nothing scored is never reported as verified.

Pinned here, through ``fluxsim init`` and a real plan, apply, verify and ship:

* ``init`` writes ``properties: []`` for a table whose columns the profile gives only order
  statistics (a date's ``min`` and ``max``), keeps the ranges and lifetimes of the block, and says
  the table has nothing to verify;
* ``verify`` of such a run exits 0, and reports each table as not scored: in the fidelity line,
  in the scorecard's table line and in the evidence a ship writes, never as passed or within
  tolerance;
* a table that scores nothing never meets a threshold or a ``minCredibility``;
* a hand-written empty list beside a statistic the profile publishes gets a note naming the
  statistic, and still exits 0;
* a bundle that declares no empty list plans to the digest it had before the rule changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.concordance.scorecard import TableInput, build_scorecard
from flux_engine.concordance.verifier import FidelityEvaluation
from flux_engine.seam.scorecard import ScorecardDeclaration, Threshold
from flux_engine.stages.apply import apply as apply_stage
from flux_engine.stages.plan import plan as plan_stage
from flux_engine.stages.ship import ship as ship_stage
from flux_engine.stages.verify import verify as verify_stage
from tests.cli.test_init_dates import CONTRACT, NAMESPACE, Written, _profile
from tests.overlays import ORDERS_CONTRACT, ORDERS_MEAN_PROFILE

pytestmark = pytest.mark.integration

#: The bundle ``tests/fixtures/telco-fidelity-small``, which declares no empty property list.
TELCO_SMALL = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"


def _run(written: Written, tmp_path: Path) -> Dict[str, Any]:
    """Plan, apply and verify a written bundle with its profile."""
    state = tmp_path / "state"
    planned = plan_stage(
        str(written.directory), state_dir=str(state), profile=str(written.profile_path)
    )
    assert planned.exit_code == 0, planned.stderr
    applied = apply_stage(
        written.directory, state_dir=state, run_id=str(planned.artifacts["run_id"])
    )
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])
    verified = verify_stage(
        written.directory,
        run_id=run_id,
        state_dir=state,
        profile=str(written.profile_path),
        allow_declared_baseline_drift=True,
    )
    return {"state": state, "run_id": run_id, "verified": verified}


def _fidelity(written: Written, expose_id: str) -> Dict[str, Any]:
    emits = written.block["emits"]
    block = emits[expose_id]["fidelity"]
    assert isinstance(block, dict)
    return block


def test_init_writes_no_property_for_a_table_whose_columns_carry_only_min_and_max(
    tmp_path: Path,
) -> None:
    written = Written(tmp_path, CONTRACT, _profile())
    for expose_id, column in (("customer", "signed_up"), ("store", "opened_at")):
        block = _fidelity(written, expose_id)
        assert block["properties"] == []
        # The ranges stay in the block, so the rows are still held to what the profile publishes.
        assert block["columns"][column]["range"]["min"] < block["columns"][column]["range"]["max"]
    stdout = written.result.stdout
    assert "nothing to verify" in stdout
    assert "customer" in stdout.split("nothing to verify")[0].splitlines()[-1]
    assert "FLX-5037" not in stdout


def test_a_run_of_those_tables_verifies_with_exit_0_and_says_they_are_not_scored(
    tmp_path: Path,
) -> None:
    written = Written(tmp_path, CONTRACT, _profile())
    ran = _run(written, tmp_path)
    verified = ran["verified"]
    assert verified.success, verified.stdout + verified.stderr
    text = verified.stdout
    for expose_id in ("customer", "store"):
        assert f"fidelity {expose_id}: not scored" in text
        assert f"fidelity {expose_id}: passed" not in text
        assert f"table {expose_id}: not scored" in text
        assert f"table {expose_id}: not measured (0 of 0" not in text
    assert "0 of 0 declared properties within" not in text


def test_a_ship_of_those_tables_does_not_call_them_verified(tmp_path: Path) -> None:
    written = Written(tmp_path, CONTRACT, _profile())
    ran = _run(written, tmp_path)
    shipped = ship_stage(
        written.directory,
        run_id=ran["run_id"],
        out_dir=tmp_path / "out",
        state_dir=ran["state"],
        profile=str(written.profile_path),
    )
    assert shipped.success, shipped.stdout + shipped.stderr
    evidence = (ran["state"] / "runs" / ran["run_id"] / "EVIDENCE.md").read_text(encoding="utf-8")
    for expose_id in ("customer", "store"):
        assert f"- `{expose_id}`: fidelity not scored" in evidence
        assert f"table {expose_id}: not scored" in evidence
    assert "`customer`: fidelity checked" not in evidence
    assert "0 of 0 declared properties within tolerance" not in evidence


def test_a_table_that_scores_nothing_never_meets_a_threshold_or_a_floor() -> None:
    nothing = FidelityEvaluation(expose_id="customer", outcomes=())
    assert nothing.unscored and not nothing.refused
    declaration = ScorecardDeclaration(
        thresholds=(
            Threshold(
                scope="table",
                name="customer",
                operator="mustBeGreaterOrEqualTo",
                declared="0.5",
                value=_fraction("1/2"),
            ),
            Threshold(
                scope="overall",
                name=None,
                operator="mustBeGreaterOrEqualTo",
                declared="0.5",
                value=_fraction("1/2"),
            ),
        )
    )
    card = build_scorecard(
        run_id="r",
        plan_digest="sha256:0",
        run_digest="sha256:0",
        profile_digest="sha256:0",
        tables=[TableInput(expose_id="customer", evaluation=nothing, declares_fidelity=True)],
        declaration=declaration,
    )
    assert [result.met for result in card.thresholds] == [False, False]
    assert not card.met
    assert card.overall.score is None and card.overall.status == "not measured"
    assert any("customer: not scored" in line for line in card.detail_lines())


def _fraction(text: str) -> Any:
    from fractions import Fraction

    return Fraction(text)


def test_a_hand_written_empty_list_beside_a_published_statistic_gets_a_note_and_exits_0(
    tmp_path: Path,
) -> None:
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(ORDERS_CONTRACT),
        profile=str(ORDERS_MEAN_PROFILE),
    )
    assert result.exit_code == 0, result.stderr
    bundle = tmp_path / "bundle"
    simulation = bundle / "simulation.flux.yml"
    document = yaml.safe_load(simulation.read_text(encoding="utf-8"))
    namespace = document["extensions"][NAMESPACE]
    namespace["emits"]["orders"]["fidelity"]["properties"] = []
    # The register decided the tolerance of the property that is gone: its entries go with it.
    namespace["parameters"] = [
        entry for entry in namespace["parameters"] if "/properties/" not in str(entry["pointer"])
    ]
    simulation.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    class _Hand:
        directory = bundle
        profile_path = ORDERS_MEAN_PROFILE

    verified = _run(_Hand(), tmp_path)["verified"]  # type: ignore[arg-type]
    assert verified.success, verified.stdout + verified.stderr
    assert "fidelity orders: not scored" in verified.stdout
    note = next(line for line in verified.stdout.splitlines() if "note:" in line)
    assert "mean/1 of amount" in note
    assert "declare one under the fidelity block's properties" in note
    # A table whose profile publishes nothing a property could read gets no note.
    assert (
        "note:"
        not in _run(Written(tmp_path / "dates", CONTRACT, _profile()), tmp_path / "d")[
            "verified"
        ].stdout
    )


def test_a_bundle_with_no_empty_list_plans_to_the_digest_it_always_did(tmp_path: Path) -> None:
    planned = plan_stage(str(TELCO_SMALL), state_dir=str(tmp_path / "state"))
    assert planned.exit_code == 0, planned.stderr
    document = json.loads(
        (tmp_path / "state" / "runs" / planned.artifacts["run_id"] / "plan.json").read_text("utf-8")
    )
    assert document["planDigest"] == TELCO_SMALL_PLAN_DIGEST


#: Filled from the plan of the bundle above under the code before the rule changed.
TELCO_SMALL_PLAN_DIGEST = "sha256:d2a9efc85478a25464ded995fa5fe75dc3bce85d5db41535e10eb54d1d35db30"
