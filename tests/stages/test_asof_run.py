# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An as-of link generates and verifies: a run of ``tests/fixtures/history-asof`` through the CLI.

The bundle is ``tests/fixtures/history-chain`` with an ``asOf`` entry on ``invoice.subscription`` and a
contract rule that derives an invoice's ``amount`` from the plan its subscription had. The invoices are
dated in 2026, after the history's ``until``, so every invoice finds a version, which the required
``amount`` needs. What is pinned:

* **The read.** Every invoice's amount follows the plan of the version of its subscription valid on its
  date, which the rows of ``subscription_version`` give by a join the test makes itself.
* **Verify.** ``fluxsim verify`` passes, checks the rule with the same as-of join, and reports an
  ``asOf:subscription.invoice_date`` line that counts the rows that read a version, measured and not scored.
  A written row that breaks the rule fails it.
* **Size.** ``--jobs 1`` and ``--jobs 4`` make one run, and ``--scale`` below 1 scales entities and rows.
* **The window.** An ``asOf`` entry on an emit with no history refuses the plan.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import _parallel as parallel
from flux_engine.stages.layout import RunLayout

from ._same_run import assert_same_run_at_one_and_four_jobs, assert_the_run_repeats
from .test_history_run import _rows, _subscriptions

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "history-asof"
NAMESPACE = "com.agentics.flux/1"
AMOUNTS = {"basic": "19.00", "pro": "49.00", "team": "99.00"}
LINE = "asOf:subscription.invoice_date"


def _bundle(tmp_path: Path, *, name: str = "bundle", invoices: Optional[int] = None) -> Path:
    """A copy of the fixture; with *invoices* its invoice count is changed."""
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    if invoices is not None:
        path = target / "simulation.flux.yml"
        document = yaml.safe_load(path.read_text("utf-8"))
        emit = document["extensions"][NAMESPACE]["emits"]["invoice"]
        emit["rows"] = invoices
        emit["fidelity"]["columns"]["invoice_id"]["range"]["max"] = str(invoices)
        path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def flux(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """The real ``python -m flux_engine.cli`` binary."""
    argv: List[str] = [sys.executable, "-m", "flux_engine.cli", *args]
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=300)


def run_cli(bundle: Path, state: Path, *options: str) -> RunLayout:
    """``plan`` then ``apply`` under kernel 0.2.0, through the CLI."""
    planned = flux(
        "plan",
        str(bundle),
        "--kernel",
        "0.2.0",
        "--state-dir",
        str(state),
        "-j",
        *options,
        cwd=bundle,
    )
    assert planned.returncode == 0, planned.stderr
    run_id = json.loads(planned.stdout)["artifacts"]["run_id"]
    applied = flux("apply", str(bundle), "--run-id", run_id, "--state-dir", str(state), cwd=bundle)
    assert applied.returncode == 0, applied.stderr
    return RunLayout.for_state_dir(state, run_id)


def verify_cli(bundle: Path, state: Path, layout: RunLayout) -> subprocess.CompletedProcess[str]:
    return flux(
        "verify",
        str(bundle),
        "--run-id",
        layout.run_id,
        "--state-dir",
        str(state),
        "-j",
        cwd=bundle,
    )


def moment(text: str) -> date:
    return date.fromisoformat(text)


def valid_version(versions: Sequence[Dict[str, Any]], when: date) -> Optional[Dict[str, Any]]:
    """The version whose start is not after *when* and whose end is open or after it (nextStart)."""
    for row in versions:
        stop = row["valid_to"]
        if moment(row["valid_from"]) <= when and (stop is None or when < moment(stop)):
            return row
    return None


def check_amounts(layout: RunLayout, *, entities: int, invoices: int) -> None:
    versions = _subscriptions(_rows(layout, "subscription_version"))
    assert len(versions) == entities
    held = _rows(layout, "invoice")
    assert len(held) == invoices
    seen = set()
    for invoice in held:
        version = valid_version(
            versions[invoice["subscription_id"]], moment(invoice["invoice_date"])
        )
        assert version is not None
        assert invoice["amount"] == AMOUNTS[version["plan"]]
        seen.add(version["plan"])
        # Dated after until, the version is the entity's open last one.
        assert version["valid_to"] is None
    assert seen == set(AMOUNTS)


# --- plan, apply, verify ----------------------------------------------------------------------


def test_an_invoices_amount_follows_the_plan_valid_on_its_date(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    layout = run_cli(bundle, tmp_path / "state")
    check_amounts(layout, entities=60, invoices=600)
    # The plan changes across an entity's versions, so the version read is not always the first.
    chains = _subscriptions(_rows(layout, "subscription_version"))
    assert any(len({row["plan"] for row in versions}) > 1 for versions in chains.values())
    firsts = {key: versions[0]["plan"] for key, versions in chains.items()}
    lasts = {key: versions[-1]["plan"] for key, versions in chains.items()}
    assert firsts != lasts


def test_verify_passes_checks_the_rule_and_measures_the_link(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    layout = run_cli(bundle, tmp_path / "state")
    verified = verify_cli(bundle, tmp_path / "state", layout)
    assert verified.returncode == 0, verified.stderr
    rules = {
        item["rule"]: item
        for item in json.loads(verified.stdout)["artifacts"]["rules"]["emits"]["invoice"]
    }
    assert rules["amount_of_plan"]["satisfied"] == rules["amount_of_plan"]["total"] == 600
    assert rules["amount_of_plan"]["enforced"] is True
    measured = rules[LINE]
    # Every invoice read a version, and the line is measured and not scored.
    assert measured["satisfied"] == measured["total"] == 600
    assert measured["enforced"] is False and measured["met"] is None
    text = flux(
        "verify",
        str(bundle),
        "--run-id",
        layout.run_id,
        "--state-dir",
        str(tmp_path / "state"),
        cwd=bundle,
    )
    assert LINE in text.stdout


def test_a_written_row_that_breaks_the_rule_fails_verify(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    layout = run_cli(bundle, tmp_path / "state")
    rows = _rows(layout, "invoice")
    victim = rows[0]
    victim["amount"] = "1.00"
    names = list(rows[0])
    layout.emit_path("invoice").write_text(
        "".join(json.dumps([row[name] for name in names]) + "\n" for row in rows), "utf-8"
    )
    again = verify_cli(bundle, tmp_path / "state", layout)
    assert again.returncode != 0


def test_a_run_is_the_same_run_again(tmp_path: Path) -> None:
    assert_the_run_repeats(_bundle(tmp_path), tmp_path, run_cli, "invoice")


# --- size -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("scale", "entities", "invoices"), [("0.5", 30, 300), ("0.25", 15, 150)])
def test_a_scale_below_one_scales_the_entities_and_the_invoices(
    tmp_path: Path, scale: str, entities: int, invoices: int
) -> None:
    bundle = _bundle(tmp_path)
    layout = run_cli(bundle, tmp_path / "state", "--scale", scale)
    check_amounts(layout, entities=entities, invoices=invoices)
    assert verify_cli(bundle, tmp_path / "state", layout).returncode == 0


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_four_jobs_make_the_run_one_job_makes(tmp_path: Path) -> None:
    invoices = 3 * parallel.MIN_ROWS_PER_JOB + 11
    bundle = _bundle(tmp_path, invoices=invoices)
    _, four = assert_same_run_at_one_and_four_jobs(bundle, result=True, kernel=KERNEL_V2)
    generated = [e for e in four.emits if e.plan is not None and e.plan.rows == invoices]
    assert generated and all(e.processes > 1 for e in generated)


# --- a refusal --------------------------------------------------------------------------------


def test_an_entry_on_a_relationship_into_an_emit_with_no_history_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    del document["extensions"][NAMESPACE]["history"]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    planned = flux(
        "plan", str(bundle), "--kernel", "0.2.0", "--state-dir", str(tmp_path / "s"), cwd=bundle
    )
    assert planned.returncode == 1
    assert "FLX-3403" in planned.stderr and "history" in planned.stderr
