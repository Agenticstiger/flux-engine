# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A contract's range for a date column is drawn within, and ``verify`` checks it (ADR 0024, amendment of 2026-11-25).

``tests/fixtures/history-fleet`` is copied with a range on ``service_visit.visited_on``:
``validationRules: [{type: range, constraint: "between '2001-01-01' and '2005-12-31'"}]``. The
column's bins run from 2000 to 2009, so before the cut a run wrote 246 of its 600 dates outside the
range and ``verify`` said ``rows: ok``. Pinned here:

* on kernel 0.2.0 the column is drawn inside the range, ``plan`` notes the cut once, and ``verify``
  passes with the output it printed before the finding existed;
* on kernel 0.1.0 the bins are drawn as they were and ``verify`` fails with the new finding, which
  names the expose, the column, the rows and the range;
* a range holds numbers too: ``verify`` counts the rows of a number column outside it;
* a bundle whose bins already lie inside its range keeps its rows and its ``verify`` output, and a
  bundle that declares no range keeps its ``verify`` output, byte for byte;
* a range that holds none of the column's dates is ``FLX-3403`` at ``plan``;
* one job or four, and a run applied in shares, write the same rows.
"""

from __future__ import annotations

import hashlib
import shutil
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.stages.layout import RunLayout
from tests.bundles import add_contract_columns, repin_contract

from ._fleet import (
    CONTRACT,
    NAMESPACE,
    VISIT,
    bundle,
    expose,
    plan_module,
    plan_refused,
    repin,
    rows,
    run_bundle,
)
from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run
from .conftest import FIXTURE_BUNDLE
from .test_scorecard_stages import EXPOSE as PLAIN_EXPOSE

pytestmark = pytest.mark.integration

apply_module = import_module("flux_engine.stages.apply")
verify_module = import_module("flux_engine.stages.verify")

LOW, HIGH = "2001-01-01", "2005-12-31"
RANGE = f"between '{LOW}' and '{HIGH}'"
#: Bins that run past both ends of the range, and bins that lie inside it.
PAST = ["2000-01-01", "2003-01-01", "2006-01-01", "2009-01-01"]
INSIDE = ["2001-01-01", "2003-01-01", "2005-12-31"]
WEIGHTS = ["0.3", "0.4", "0.3"]
INSIDE_WEIGHTS = ["0.6", "0.4"]

#: What ``verify`` printed for the bundles below before it read a contract's ranges. ``{periods}`` is
#: the rows of ``vehicle_month``, which each bundle's contract digest sets.
FLEET_VERIFY = """\
reproducible: not checked | validity: not asserted
  golden: not checked (no golden baseline recorded yet -- record one with `--update-golden --reason "<why>"`)
  artifacts: every run-digest component reproduces
  rows: ok
  rfc-04 vectors: 0 failure(s)
  fidelity depot: declared, not checked -- declares a fidelity block; pass --committed-slice depot=file://<path> to run the check
  fidelity service_visit: declared, not checked -- declares a fidelity block; pass --committed-slice service_visit=file://<path> to run the check
  fidelity vehicle_change: declared, not checked -- declares a fidelity block; pass --committed-slice vehicle_change=file://<path> to run the check
  fidelity vehicle_current: declared, not checked -- declares a fidelity block; pass --committed-slice vehicle_current=file://<path> to run the check
  fidelity vehicle_month: declared, not checked -- declares a fidelity block; pass --committed-slice vehicle_month=file://<path> to run the check
  fidelity vehicle_version: declared, not checked -- declares a fidelity block; pass --committed-slice vehicle_version=file://<path> to run the check
  rules vehicle_change: event:matches_transition 60 of 60 (100.00%)
  rules vehicle_current: snapshot:equals_last_version 60 of 60 (100.00%)
  rules vehicle_month: period:within_validity {periods} of {periods} (100.00%); period:matches_valid_version {periods} of {periods} (100.00%)
  rules vehicle_version: history:no_gaps_no_overlap 60 of 60 (100.00%); history:one_current 60 of 60 (100.00%)
  integrity vehicle_version.depot -> depot: found 200 of 200 (100.00%); unique 50 of 50 (100.00%); non-null 200 of 200 (100.00%)
  integrity service_visit.vehicle -> vehicle_version: found 600 of 600 (100.00%); non-null 600 of 600 (100.00%)
  integrity vehicle_change.vehicle -> vehicle_version: found 200 of 200 (100.00%); non-null 200 of 200 (100.00%)
  integrity vehicle_month.vehicle -> vehicle_version: found {periods} of {periods} (100.00%); non-null {periods} of {periods} (100.00%)"""


def _visits(
    tmp_path: Path,
    *,
    rule: Optional[str] = RANGE,
    bins: Optional[List[str]] = PAST,
    weights: List[str] = WEIGHTS,
    name: str = "bundle",
    cost_rule: Optional[str] = None,
    declared: Optional[Dict[str, Any]] = None,
) -> Path:
    """The fleet bundle with ``visited_on`` drawn from *bins* and a range rule on it, or on ``cost``."""
    target = bundle(tmp_path, name=name)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    columns = document["extensions"][NAMESPACE]["emits"][VISIT]["fidelity"]["columns"]
    if bins is not None:
        columns["visited_on"] = {"bins": bins, "binWeights": weights}
    columns["visited_on"].update(declared or {})
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    schema = expose(contract, VISIT)["contract"]["schema"]
    for column, text in (("visited_on", rule), ("cost", cost_rule)):
        if text is not None:
            held = next(item for item in schema if item["name"] == column)
            held["validationRules"] = [{"type": "range", "constraint": text}]
    repin(target, contract)
    return target


def _plain(tmp_path: Path, *, rule: str = RANGE) -> Path:
    """The small starter bundle (no history, so kernel 0.1.0 plans it) with a ``due_on`` DATE column.

    The column is drawn from ``PAST`` bins and its contract range is *rule*.
    """
    target = tmp_path / "plain"
    shutil.copytree(FIXTURE_BUNDLE, target)
    (target / "flux-manifest.json").unlink(missing_ok=True)
    add_contract_columns(target, [("due_on", "DATE", False)])
    contract = target / "payment-recovery.fluid.yml"
    text = contract.read_text("utf-8")
    anchor = "        - name: due_on\n          type: DATE\n          required: false\n"
    assert text.count(anchor) == 1
    declared = f'          validationRules:\n            - type: range\n              constraint: "{rule}"\n'
    contract.write_text(text.replace(anchor, anchor + declared), "utf-8")
    simulation = target / "simulation.flux.yml"
    repin_contract(contract, simulation, "telco.gold.payment_recovery_moment")
    text = simulation.read_text("utf-8")
    column_anchor = "            aborted_at:\n"
    assert text.count(column_anchor) == 1
    edges = ", ".join(f'"{edge}"' for edge in PAST)
    weights = ", ".join(f'"{weight}"' for weight in WEIGHTS)
    due = f"            due_on:\n              bins: [{edges}]\n              binWeights: [{weights}]\n              null: exclude\n"
    simulation.write_text(text.replace(column_anchor, due + column_anchor), "utf-8")
    return target


def _verified(target: Path, state: Path, layout: RunLayout) -> Any:
    return verify_module.verify(target, run_id=layout.run_id, state_dir=state)


def _dates(layout: RunLayout) -> List[str]:
    return [row["visited_on"] for row in rows(layout, VISIT)]


def _outside(dates: List[str]) -> int:
    return sum(1 for value in dates if not LOW <= value <= HIGH)


def test_a_date_column_whose_bins_pass_its_range_is_drawn_inside_it(tmp_path: Path) -> None:
    target = _visits(tmp_path)
    state = tmp_path / "state"
    layout = run_bundle(target, state)
    dates = _dates(layout)
    assert len(dates) == 600
    assert _outside(dates) == 0
    # Both ends of the range are reached: the cut bins still cover it.
    assert min(dates) < "2001-03-01" and max(dates) > "2005-10-01"
    verified = _verified(target, state, layout)
    assert verified.success, verified.stderr
    assert verified.stdout == FLEET_VERIFY.format(periods=1522)
    assert "rangeConformance" not in verified.artifacts


def test_plan_notes_the_cut_of_each_column_once(tmp_path: Path) -> None:
    planned = plan_module.plan(
        _visits(tmp_path), state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert planned.success, planned.stderr
    notes = [line for line in planned.stderr.splitlines() if line.startswith("note: ")]
    assert notes == [
        "note: service_visit.visited_on is drawn inside the contract's range "
        f"{LOW} to {HIGH}: its declared bins reach beyond it, so each bin is cut at the range "
        "and keeps weight in proportion to the width it keeps"
    ]


def test_a_run_on_kernel_0_1_0_keeps_its_dates_and_verify_names_the_rows_outside(
    tmp_path: Path,
) -> None:
    target = _plain(tmp_path)
    state = tmp_path / "state"
    # `run_bundle` plans under kernel 0.2.0; this run is the one on 0.1.0.
    planned = plan_module.plan(target, state_dir=state, kernel=KERNEL_V1.version, stage=False)
    assert planned.success, planned.stderr
    applied = apply_module.apply(target, run_id=planned.artifacts["run_id"], state_dir=state)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, planned.artifacts["run_id"])
    dated = [row["due_on"] for row in rows(layout, PLAIN_EXPOSE) if row["due_on"] is not None]
    outside = _outside(dated)
    assert outside > 0
    verified = _verified(target, state, layout)
    assert not verified.success
    finding = (
        f"{PLAIN_EXPOSE}.due_on: {outside} of {len(rows(layout, PLAIN_EXPOSE))} rows are outside "
        f"the declared range (>= {LOW} and <= {HIGH})"
    )
    assert f"  ranges: {finding}" in verified.stdout.splitlines()
    assert "  rows: ok" in verified.stdout.splitlines()
    assert finding in verified.stderr
    assert verified.artifacts["rangeConformance"] == {"ok": False, "problems": [finding]}


def test_the_same_bundle_on_kernel_0_2_0_is_drawn_inside_its_range_and_verifies(
    tmp_path: Path,
) -> None:
    target = _plain(tmp_path)
    state = tmp_path / "state"
    layout = run_bundle(target, state)
    dated = [row["due_on"] for row in rows(layout, PLAIN_EXPOSE) if row["due_on"] is not None]
    assert dated and _outside(dated) == 0
    verified = _verified(target, state, layout)
    assert verified.success, verified.stderr
    assert "  ranges:" not in verified.stdout


def test_verify_counts_the_rows_of_a_number_column_outside_its_range(tmp_path: Path) -> None:
    # cost is declared 5 to 500 and the contract allows up to 100.
    target = _visits(tmp_path, rule=None, cost_rule="between 5 and 100")
    state = tmp_path / "state"
    layout = run_bundle(target, state)
    outside = sum(1 for row in rows(layout, VISIT) if float(row["cost"]) > 100)
    assert outside > 0
    verified = _verified(target, state, layout)
    assert not verified.success
    finding = (
        f"service_visit.cost: {outside} of 600 rows are outside the declared range "
        "(>= 5 and <= 100)"
    )
    assert f"  ranges: {finding}" in verified.stdout.splitlines()
    assert verified.artifacts["rangeConformance"]["problems"] == [finding]


def test_a_bundle_with_bins_inside_its_range_keeps_its_rows_and_its_verify_output(
    tmp_path: Path,
) -> None:
    target = _visits(tmp_path, bins=INSIDE, weights=INSIDE_WEIGHTS)
    state = tmp_path / "state"
    layout = run_bundle(target, state)
    assert _outside(_dates(layout)) == 0
    # Pinned on the commit before the cut: nothing inside the range is moved by it.
    digest = hashlib.sha256(layout.emit_path(VISIT).read_bytes()).hexdigest()
    assert digest == "2bc7566a5c5e83285e86b380349a6323cf1b586476914e1a2493a9d690bbdb3d"
    plan_text = plan_module.plan(
        target, state_dir=tmp_path / "again", kernel=KERNEL_V2.version, stage=False
    )
    assert not [line for line in plan_text.stderr.splitlines() if line.startswith("note: ")]
    verified = _verified(target, state, layout)
    assert verified.success, verified.stderr
    assert verified.stdout == FLEET_VERIFY.format(periods=1489)


def test_a_bundle_with_no_declared_range_keeps_its_verify_output(tmp_path: Path) -> None:
    target = bundle(tmp_path)
    state = tmp_path / "state"
    layout = run_bundle(target, state)
    verified = _verified(target, state, layout)
    assert verified.success, verified.stderr
    assert verified.stdout == FLEET_VERIFY.format(periods=1659)
    assert "rangeConformance" not in verified.artifacts


def test_a_range_that_holds_none_of_the_column_s_dates_is_refused_at_plan(tmp_path: Path) -> None:
    target = _visits(tmp_path, rule="between '2012-01-01' and '2013-12-31'")
    planned = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert not planned.success
    assert "FLX-3403" in planned.stderr
    assert "service_visit.visited_on" in planned.stderr
    assert "2012-01-01 to 2013-12-31" in planned.stderr


@pytest.mark.parametrize(
    ("rule", "spelled"),
    [
        ("between '2005-12-31' and '2001-01-01'", ">= 2005-12-31 and <= 2001-01-01"),
        (">= '2003-01-01' and < '2003-01-01'", ">= 2003-01-01 and < 2003-01-01"),
    ],
)
def test_an_empty_range_is_refused_at_plan_naming_the_column_and_both_ends(
    tmp_path: Path, rule: str, spelled: str
) -> None:
    refused = plan_refused(_visits(tmp_path, rule=rule), tmp_path / "state")
    assert f"service_visit.visited_on: the contract's range ({spelled}) holds no value" in (
        refused.stderr
    )
    assert refused.error_code == "FLX-3403"


def test_a_declared_range_wider_than_the_contract_s_is_cut_to_it(tmp_path: Path) -> None:
    target = _visits(
        tmp_path,
        bins=None,
        declared={"range": {"min": "2000-01-01", "max": "2009-12-31"}},
    )
    layout = run_bundle(target, tmp_path / "state")
    assert _outside(_dates(layout)) == 0


def test_the_cut_column_is_the_same_at_one_job_and_four_and_in_shares(tmp_path: Path) -> None:
    target = _visits(tmp_path)
    assert_same_run_at_one_and_four_jobs(target)

    def run(bundle_dir: Path, state: Path) -> RunLayout:
        return run_bundle(bundle_dir, state)

    assert_shares_join_to_the_run(target, tmp_path / "shares", run, 3, [VISIT])


@pytest.mark.parametrize(
    "rule",
    [
        ">= '2024-01-01'",
        ">= '2024-01-01' and <= '9999-12-31'",
        "between '2024-01-01' and '9999-12-31'",
    ],
)
def test_a_range_with_one_end_is_drawn_within_and_verified(tmp_path: Path, rule: str) -> None:
    bins = ["2022-01-01", "2024-01-01", "2027-01-01"]
    target = _visits(tmp_path, rule=rule, bins=bins, weights=["0.5", "0.5"])
    state = tmp_path / "state"
    layout = run_bundle(target, state)
    dates = _dates(layout)
    assert dates and min(dates) >= "2024-01-01" and max(dates) > "2025-01-01"
    verified = _verified(target, state, layout)
    assert verified.success, verified.stderr
    assert "  ranges:" not in verified.stdout


def test_a_run_that_draws_before_a_one_sided_range_fails_verify_with_the_end_it_names(
    tmp_path: Path,
) -> None:
    target = _plain(tmp_path, rule=">= '2001-01-01'")
    state = tmp_path / "state"
    planned = plan_module.plan(target, state_dir=state, kernel=KERNEL_V1.version, stage=False)
    assert planned.success, planned.stderr
    run_id = planned.artifacts["run_id"]
    assert apply_module.apply(target, run_id=run_id, state_dir=state).success
    layout = RunLayout.for_state_dir(state, run_id)
    dated = [row["due_on"] for row in rows(layout, PLAIN_EXPOSE) if row["due_on"] is not None]
    before = sum(1 for value in dated if value < "2001-01-01")
    assert before > 0
    verified = _verified(target, state, layout)
    assert not verified.success
    finding = (
        f"{PLAIN_EXPOSE}.due_on: {before} of {len(rows(layout, PLAIN_EXPOSE))} rows are outside "
        "the declared range (>= 2001-01-01)"
    )
    assert f"  ranges: {finding}" in verified.stdout.splitlines()


def test_a_range_whose_ends_meet_draws_and_verifies_that_one_day(tmp_path: Path) -> None:
    target = _visits(tmp_path, rule="between '2003-05-05' and '2003-05-05'")
    state = tmp_path / "state"
    layout = run_bundle(target, state)
    assert set(_dates(layout)) == {"2003-05-05"}
    verified = _verified(target, state, layout)
    assert verified.success, verified.stderr
    assert "  ranges:" not in verified.stdout
