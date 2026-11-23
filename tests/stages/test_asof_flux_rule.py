# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A FLUX-side rule reads through an ``asOf`` link into a history, through the ``fluxsim`` command line.

The bundle is ``tests/fixtures/history-asof`` with the contract's ``amount_of_plan`` invariant moved into a
``flux-expr/2`` rule (with a second path through the customer, which the contract cannot carry) under ``emits.invoice.rules`` (ADR 0017 section 8). What is pinned:

* **The read.** The rule plans, runs and verifies, and every invoice's amount follows the plan of the version
  of its subscription valid on its date, which a join over the emitted rows gives.
* **The refusal.** The same rule over a relationship no ``asOf`` entry names is refused with ``FLX-6106``.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Dict

import pytest
import yaml

from tests.bundles import repin_contract

from .conftest import REPO_ROOT
from .test_asof_run import flux, run_cli, valid_version, verify_cli
from .test_history_run import _rows, _subscriptions

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "history-asof"
NAMESPACE = "com.agentics.flux/1"
# The second path crosses two links, which is what keeps the rule on the FLUX side.
EXPRESSION = (
    "CASE WHEN subscription.plan = 'team' THEN 99 WHEN subscription.plan = 'pro' THEN 49 ELSE 19 END"
    " + CASE WHEN subscription.customer.region = 'eu' THEN 1 ELSE 0 END"
)
BASE = {"basic": 19, "pro": 49, "team": 99}
INVARIANT = re.compile(r"      invariants:\n        - id: amount_of_plan\n(?:          .*\n)+")


def _bundle(tmp_path: Path, *, named: bool = True, history: bool = True) -> Path:
    """The fixture with its amount rule on the FLUX side; without *named* its ``asOf`` entry is gone."""
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE, target)
    contract = target / "subscriptions.fluid.yml"
    text = contract.read_text("utf-8")
    assert len(INVARIANT.findall(text)) == 1
    contract.write_text(INVARIANT.sub("", text), "utf-8")
    path = target / "simulation.flux.yml"
    repin_contract(contract, path, "billing.subscriptions")
    document = yaml.safe_load(path.read_text("utf-8"))
    namespace = document["extensions"][NAMESPACE]
    namespace["emits"]["invoice"]["rules"] = [
        {"id": "amount_of_plan", "language": "flux-expr/2", "derive": "amount", "expr": EXPRESSION}
    ]
    if not named and history:
        del namespace["asOf"]
    if not history:
        del namespace["asOf"], namespace["history"]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def test_a_flux_rule_through_an_asof_link_reads_the_version_valid_on_the_date(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    validated = flux("validate", str(bundle), cwd=bundle)
    assert validated.returncode == 0, validated.stderr
    layout = run_cli(bundle, tmp_path / "state")
    chains = _subscriptions(_rows(layout, "subscription_version"))
    regions = {row["customer_id"]: row["region"] for row in _rows(layout, "customer")}
    invoices = _rows(layout, "invoice")
    assert len(invoices) == 600
    seen = set()
    for invoice in invoices:
        version = valid_version(
            chains[invoice["subscription_id"]], date.fromisoformat(invoice["invoice_date"])
        )
        assert version is not None
        region = regions[chains[invoice["subscription_id"]][0]["customer_id"]]
        assert invoice["amount"] == f"{BASE[version['plan']] + (region == 'eu')}.00"
        seen.add(version["plan"])
    # The plan changes across versions, so a read of the first version would not give these amounts.
    assert seen == set(BASE)
    verified = verify_cli(bundle, tmp_path / "state", layout)
    assert verified.returncode == 0, verified.stderr
    rules = {
        item["rule"]: item
        for item in json.loads(verified.stdout)["artifacts"]["rules"]["emits"]["invoice"]
    }
    assert rules["amount_of_plan"]["satisfied"] == rules["amount_of_plan"]["total"] == 600


@pytest.mark.parametrize(
    "options",
    [{"named": False}, {"history": False}],
    ids=["no asOf entry names the link", "no history declared"],
)
def test_a_flux_rule_through_a_link_that_opens_no_history_is_refused(
    tmp_path: Path, options: Dict[str, bool]
) -> None:
    """A path into an expose with a validity window opens nothing without a named asOf link."""
    bundle = _bundle(tmp_path, **options)
    planned = flux(
        "plan", str(bundle), "--kernel", "0.2.0", "--state-dir", str(tmp_path / "s"), cwd=bundle
    )
    assert planned.returncode == 1
    assert "FLX-6106" in planned.stderr
