# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim verify`` over a counted layout (ADR 0016 section 8): a generated aggregate on every parent.

The bundle is the one ``test_counts_aggregates`` builds: an ``invoice`` that summarises its
``invoice_line`` rows. What is pinned:

* **Held.** A run holds every summary on every invoice, and each contract aggregate is a line of
  the invoice's rules with a share of 100 per cent, enforced.
* **Tampered.** One invoice whose total no longer is the sum of its lines is one violating row of
  its rule and fails the run; the same holds for a count and for a NULL summary.
* **Not generated.** An aggregate over a relationship with no counts entry is not checked, and a
  bundle that declares neither a counts entry nor an aggregate keeps its output.
* **Size.** ``--scale`` below 1, ``--jobs`` and ``--shard`` leave every summary held.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify as run_verify

from .test_counts_aggregates import INVOICES, bundle_with
from .test_counts_run import FIXTURE

pytestmark = pytest.mark.integration

#: The invoice's six summaries, by the id the bundle gives each rule.
SUMMARY_RULES = {
    "invoice_total",
    "invoice_line_count",
    "invoice_max_qty",
    "invoice_big_lines",
    "invoice_big_total",
    "invoice_discount_total",
}


def _applied(bundle: Path, state: Path, **options: Any) -> RunLayout:
    applied = apply(bundle, state_dir=state, **options)
    assert applied.success, applied.stderr
    return RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))


def _verify(bundle: Path, layout: RunLayout, state: Path, **options: Any) -> Any:
    return run_verify(
        bundle,
        run_id=layout.run_id,
        state_dir=state,
        allow_declared_baseline_drift=True,
        **options,
    )


def _rules(result: Any, expose_id: str) -> Dict[str, Dict[str, Any]]:
    return {entry["rule"]: entry for entry in result.artifacts["rules"]["emits"][expose_id]}


def _tampered_checks(
    bundle: Path, layout: RunLayout, edit: Any
) -> Tuple[Any, List[List[Any]], List[str]]:
    """The module checks over *layout* once *edit* has changed the invoice rows (artifacts taken as given).

    Changing an emit moves the run digest, which ``verify`` reports before it reads a row; the
    checks themselves are called with the digest set aside, as the integrity test beside them does.
    """
    from flux_engine.stages._module_checks import module_checks

    path = layout.emit_path("invoice")
    names = json.loads(layout.seam_attestation_path("invoice").read_text("utf-8"))["columns"]
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    edit(names, rows)
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows), "utf-8")
    receipt = json.loads(layout.receipt_path.read_text("utf-8"))
    plan = json.loads(layout.plan_path.read_text("utf-8"))
    return module_checks(bundle, layout, receipt, plan, artifacts_ok=True), rows, names


@pytest.fixture(scope="module")
def counted(tmp_path_factory: pytest.TempPathFactory) -> Tuple[Path, Path, RunLayout]:
    root = tmp_path_factory.mktemp("counts-verify")
    bundle, state = bundle_with(root), root / ".flux"
    return bundle, state, _applied(bundle, state)


def test_every_invoice_holds_every_summary_and_each_is_a_line_of_its_rules(
    counted: Tuple[Path, Path, RunLayout],
) -> None:
    bundle, state, layout = counted
    result = _verify(bundle, layout, state)
    assert result.success, result.stderr
    rules = _rules(result, "invoice")
    assert SUMMARY_RULES <= set(rules)
    for rule_id in SUMMARY_RULES:
        entry = rules[rule_id]
        assert (entry["satisfied"], entry["total"], entry["share"]) == (INVOICES, INVOICES, "1")
        assert entry["form"] == "derive" and entry["enforced"] and entry["met"] is True
    assert "invoice_total 1200 of 1200 (100.00%)" in result.stdout


def test_one_invoice_whose_total_is_not_the_sum_of_its_lines_fails_the_run(
    tmp_path: Path,
) -> None:
    bundle, state = bundle_with(tmp_path), tmp_path / ".flux"
    layout = _applied(bundle, state)
    moved: List[Any] = []

    def edit(names: List[str], rows: List[List[Any]]) -> None:
        position, key = names.index("total"), names.index("invoice_id")
        row = rows[417]
        moved.append(row[key])
        row[position] = str(Decimal(row[position]) + Decimal("0.01"))

    checks, _, _ = _tampered_checks(bundle, layout, edit)
    (share,) = (found for found in checks.rules if found.rule_id == "invoice_total")
    assert (share.holds.satisfied, share.holds.total) == (INVOICES - 1, INVOICES)
    assert share.met is False
    other = [found for found in checks.rules if found.rule_id in SUMMARY_RULES - {"invoice_total"}]
    assert other and all(found.met is True for found in other)
    assert checks.failures() == [
        f"rule invoice.invoice_total: {share.holds.text()} of the rows it covers hold it, "
        "outside what the rule allows"
    ]
    assert moved == [418]


def test_a_changed_count_and_a_summary_made_null_each_fail_their_own_rule(
    tmp_path: Path,
) -> None:
    bundle, state = bundle_with(tmp_path), tmp_path / ".flux"
    layout = _applied(bundle, state)

    def edit(names: List[str], rows: List[List[Any]]) -> None:
        rows[3][names.index("line_count")] += 1
        rows[9][names.index("discount_total")] = "0.01"
        rows[10][names.index("max_qty")] = None

    checks, _, _ = _tampered_checks(bundle, layout, edit)
    broken = {found.rule_id: found for found in checks.rules if found.met is False}
    assert set(broken) <= {"invoice_line_count", "invoice_discount_total", "invoice_max_qty"}
    assert broken["invoice_line_count"].holds.violating == 1
    assert len(checks.failures()) == len(broken) >= 2


def test_a_run_whose_invoice_rows_were_edited_is_refused_by_the_digest_before_the_rules(
    tmp_path: Path,
) -> None:
    bundle, state = bundle_with(tmp_path), tmp_path / ".flux"
    layout = _applied(bundle, state)
    path = layout.emit_path("invoice")
    lines = path.read_text("utf-8").splitlines()
    row = json.loads(lines[5])
    row[0] += 1
    lines[5] = json.dumps(row, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", "utf-8")
    result = _verify(bundle, layout, state)
    assert result.exit_code == 1
    assert "rules and integrity: not checked" in result.stdout


def test_an_aggregate_with_no_counts_entry_is_not_checked(tmp_path: Path) -> None:
    from flux_engine.concordance.rule_shares import RuleSource, rule_shares
    from flux_engine.generator.rules import model_from_contract

    bundle = bundle_with(tmp_path, counted=False)
    contract = _contract_document(bundle)
    model = model_from_contract(contract)
    assert model.exposes["invoice"].columns["total"]
    tables = {"invoice": [], "invoice_line": []}
    shown = rule_shares(contract, "invoice", tables, RuleSource())
    assert not {share.rule_id for share in shown} & SUMMARY_RULES


def _contract_document(bundle: Path) -> Any:
    import yaml

    return yaml.safe_load((bundle / "sales-chain.fluid.yml").read_text("utf-8"))


@pytest.mark.parametrize("scale", ["0.5", "0.25"])
def test_a_scale_below_one_keeps_every_summary_on_every_invoice(tmp_path: Path, scale: str) -> None:
    bundle, state = bundle_with(tmp_path), tmp_path / ".flux"
    layout = _applied(bundle, state, scale=scale)
    result = _verify(bundle, layout, state)
    assert result.success, result.stderr
    invoices = int(Decimal(INVOICES) * Decimal(scale))
    for rule_id in SUMMARY_RULES:
        entry = _rules(result, "invoice")[rule_id]
        assert entry["satisfied"] == entry["total"] == invoices and entry["met"] is True


def test_the_checks_are_the_same_for_any_number_of_jobs(tmp_path: Path) -> None:
    bundle, state = bundle_with(tmp_path), tmp_path / ".flux"
    layout = _applied(bundle, state)
    results = [_verify(bundle, layout, state, jobs=jobs) for jobs in (1, 2)]
    assert results[0].artifacts["rules"] == results[1].artifacts["rules"]
    assert results[0].stdout == results[1].stdout


def test_the_shards_of_a_run_joined_are_the_run_the_summaries_hold_on(tmp_path: Path) -> None:
    bundle, state = bundle_with(tmp_path), tmp_path / ".flux"
    layout = _applied(bundle, state)
    assert _verify(bundle, layout, state).success
    parts = tmp_path / "shards"
    shard_layout = _applied(bundle, parts, shard="1/3")
    for index in (2, 3):
        _applied_shard = apply(
            bundle, state_dir=parts, run_id=shard_layout.run_id, shard=f"{index}/3"
        )
        assert _applied_shard.success, _applied_shard.stderr
    for expose_id in ("invoice", "invoice_line"):
        joined = b"".join(
            shard_layout.shard_emit_path(expose_id, index, 3).read_bytes() for index in (1, 2, 3)
        )
        assert joined == layout.emit_path(expose_id).read_bytes(), expose_id


def test_a_bundle_with_no_counts_entry_and_no_aggregate_verifies_as_it_did(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".flux"
    layout = _applied(FIXTURE, state)
    result = _verify(FIXTURE, layout, state)
    assert result.success, result.stderr
    assert "rules" not in result.artifacts
    assert all("rules" not in line for line in result.stdout.splitlines())
    assert "childrenPerParent" not in result.stdout


# -- the minimum and the condition (ADR 0016 section 8) ---------------------------------------

MINIMUM = "counts:account.minChildren"
CONDITION = "counts:account.parentWhen"


def _conditioned_run(tmp_path: Path) -> Tuple[Path, Path, RunLayout]:
    from .test_counts_run import _conditioned

    bundle, state = _conditioned(tmp_path, account_minimum=2), tmp_path / ".flux"
    return bundle, state, _applied(bundle, state)


def _accounts(layout: RunLayout) -> Dict[Any, str]:
    names = json.loads(layout.seam_attestation_path("account").read_text("utf-8"))["columns"]
    rows = [json.loads(x) for x in layout.emit_path("account").read_text("utf-8").splitlines()]
    return {row[names.index("account_id")]: row[names.index("status")] for row in rows}


def _moved(layout: RunLayout, bundle: Path, moves: Dict[int, Any]) -> Any:
    """The module checks after the invoices at the given row positions take the given account."""

    def edit(names: List[str], rows: List[List[Any]]) -> None:
        for position, account in moves.items():
            rows[position][names.index("account_id")] = account

    return _tampered_checks(bundle, layout, edit)[0]


def _share(checks: Any, rule_id: str) -> Any:
    (found,) = (share for share in checks.rules if share.rule_id == rule_id)
    return found


def test_a_run_holds_its_minimum_and_its_condition_on_every_parent(tmp_path: Path) -> None:
    bundle, state, layout = _conditioned_run(tmp_path)
    result = _verify(bundle, layout, state)
    assert result.success, result.stderr
    rules = _rules(result, "invoice")
    closed = [key for key, status in _accounts(layout).items() if status == "closed"]
    assert len(closed) > 0
    assert rules[CONDITION]["total"] == len(closed) == rules[CONDITION]["satisfied"]
    assert rules[MINIMUM]["total"] == 300 - len(closed) == rules[MINIMUM]["satisfied"]
    for rule_id in (MINIMUM, CONDITION):
        assert rules[rule_id]["enforced"] and rules[rule_id]["met"] is True
    assert f"{MINIMUM} {300 - len(closed)} of {300 - len(closed)} (100.00%)" in result.stdout


def test_an_invoice_given_to_a_closed_account_fails_the_condition(tmp_path: Path) -> None:
    bundle, _, layout = _conditioned_run(tmp_path)
    status = _accounts(layout)
    closed = next(key for key, value in status.items() if value == "closed")
    checks = _moved(layout, bundle, {0: closed})
    condition = _share(checks, CONDITION)
    assert (condition.holds.violating, condition.met) == (1, False)
    assert any(line.startswith(f"rule invoice.{CONDITION}:") for line in checks.failures())


def test_an_open_account_left_below_the_minimum_fails_the_minimum(tmp_path: Path) -> None:
    bundle, _, layout = _conditioned_run(tmp_path)
    names = json.loads(layout.seam_attestation_path("invoice").read_text("utf-8"))["columns"]
    rows = [json.loads(x) for x in layout.emit_path("invoice").read_text("utf-8").splitlines()]
    column = names.index("account_id")
    held: Dict[Any, List[int]] = {}
    for position, row in enumerate(rows):
        held.setdefault(row[column], []).append(position)
    open_keys = [key for key, value in _accounts(layout).items() if value != "closed"]
    # An open account with exactly two invoices loses one to another open account.
    poor = next(key for key in open_keys if len(held[key]) == 2)
    other = next(key for key in open_keys if key != poor)
    checks = _moved(layout, bundle, {held[poor][0]: other})
    minimum = _share(checks, MINIMUM)
    assert (minimum.holds.violating, minimum.met) == (1, False)
    assert _share(checks, CONDITION).met is True
    assert any(line.startswith(f"rule invoice.{MINIMUM}:") for line in checks.failures())


def test_a_minimum_with_no_condition_covers_every_parent_and_adds_no_condition_line(
    tmp_path: Path,
) -> None:
    from .test_counts_run import bundle_with as counted_with

    bundle, state = counted_with(tmp_path, "floor", minimum=1), tmp_path / ".flux"
    layout = _applied(bundle, state)
    result = _verify(bundle, layout, state)
    assert result.success, result.stderr
    rules = _rules(result, "invoice_line")
    assert set(rules) == {"counts:invoice.minChildren"}
    assert rules["counts:invoice.minChildren"]["total"] == INVOICES
