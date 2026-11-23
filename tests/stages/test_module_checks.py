# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``verify`` checks every rule on the run's rows and reports the share that holds (ADR 0018)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify as run_verify

from .test_rules_run import FIXTURE, _bundle

pytestmark = pytest.mark.integration

#: A rule no row of the run can satisfy: every date_dim year is 2026.
_BEFORE_2000 = '        - id: before_2000\n          check: "d_year < 2000"\n'
_BANDED = (
    '        - id: before_2000\n          check: "d_year < 2000"\n' "          mustBeLessThan: 1\n"
)


def _run(bundle: Path, state: Path) -> Any:
    applied = apply(bundle, state_dir=state)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    return run_verify(
        bundle, run_id=layout.run_id, state_dir=state, allow_declared_baseline_drift=True
    )


def _rules(result: Any) -> Dict[str, Dict[str, Any]]:
    document = result.artifacts["rules"]["emits"]["date_dim"]
    return {entry["rule"]: entry for entry in document}


def test_every_rule_of_the_contract_is_reported_with_its_share(tmp_path: Path) -> None:
    result = _run(_bundle(tmp_path), tmp_path / ".flux")
    assert result.success, result.stderr
    rules = _rules(result)
    assert set(rules) == {"date_of_key", "year_of_date"}
    for entry in rules.values():
        assert (entry["satisfied"], entry["total"], entry["share"]) == (365, 365, "1")
        assert entry["enforced"] and entry["met"] is True
    assert result.artifacts["rules"]["ok"] is True
    assert "  rules date_dim: date_of_key 365 of 365 (100.00%); year_of_date 365 of 365" in (
        result.stdout
    )


def test_a_rule_the_engine_does_not_enforce_is_a_share_and_not_a_failure(tmp_path: Path) -> None:
    result = _run(_bundle(tmp_path, invariants=_BEFORE_2000), tmp_path / ".flux")
    assert result.success, result.stderr
    entry = _rules(result)["before_2000"]
    assert (entry["satisfied"], entry["total"], entry["share"]) == (0, 365, "0")
    assert entry["enforced"] is False and entry["met"] is None
    assert "before_2000 0 of 365 (0.00%) (not enforced)" in result.stdout


def test_a_rule_off_its_declared_band_fails_the_run(tmp_path: Path) -> None:
    result = _run(_bundle(tmp_path, invariants=_BANDED), tmp_path / ".flux")
    assert result.exit_code == 1
    assert _rules(result)["before_2000"]["met"] is False
    assert result.artifacts["rules"]["ok"] is False
    assert "rule date_dim.before_2000: 0 of 365 (0.00%)" in result.stderr
    assert "(outside its band)" in result.stdout


def test_a_bundle_that_declares_no_rule_adds_nothing_to_verify(tmp_path: Path) -> None:
    result = _run(FIXTURE, tmp_path / ".flux")
    assert result.success, result.stderr
    assert "rules" not in result.artifacts
    assert "rules" not in result.stdout.replace("rules:", "")
    assert all("  rules" not in line for line in result.stdout.splitlines())


def test_a_run_whose_artifacts_moved_is_not_checked_against_its_rows(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    state = tmp_path / ".flux"
    applied = apply(bundle, state_dir=state)
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    path = layout.emit_path("date_dim")
    path.write_text(path.read_text("utf-8").replace("2026", "2027", 1), "utf-8")
    result = run_verify(
        bundle, run_id=layout.run_id, state_dir=state, allow_declared_baseline_drift=True
    )
    assert result.exit_code == 1
    assert "rules and integrity: not checked" in result.stdout
    assert result.artifacts["rules"]["skipped"]


def test_every_declared_relationship_is_scored_and_whole_on_a_run(tmp_path: Path) -> None:
    result = _run(FIXTURE, tmp_path / ".flux")
    assert result.success, result.stderr
    links = {
        (entry["child"], entry["relationship"]): entry
        for entry in result.artifacts["integrity"]["relationships"]
    }
    assert ("store_sales", "item") in links and ("store_returns", "sale") in links
    assert result.artifacts["integrity"]["ok"] is True
    for entry in links.values():
        for check in entry["checks"].values():
            assert check["satisfied"] == check["total"] and check["total"] > 0
    sale = links[("store_returns", "sale")]
    assert set(sale["checks"]) == {"found", "unique", "oneToOne"}
    assert any(
        line.startswith("  integrity store_sales.item -> item: found 4000 of 4000 (100.00%)")
        for line in result.stdout.splitlines()
    )


def test_a_broken_key_in_the_rows_is_a_share_under_one_hundred_per_cent(tmp_path: Path) -> None:
    """The check reads the rows back: a parent that lost a row leaves a child key unfound."""
    from flux_engine.stages._module_checks import module_checks

    bundle, state = FIXTURE, tmp_path / ".flux"
    applied = apply(bundle, state_dir=state)
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    receipt = json.loads(layout.receipt_path.read_text("utf-8"))
    plan = json.loads(layout.plan_path.read_text("utf-8"))
    whole = module_checks(bundle, layout, receipt, plan, artifacts_ok=True)
    assert all(link.whole for link in whole.links) and not whole.failures()
    path = layout.emit_path("item")
    lines = path.read_text("utf-8").splitlines()
    path.write_text("\n".join(lines[:150]) + "\n", "utf-8")  # 200 items become 150
    broken = module_checks(bundle, layout, receipt, plan, artifacts_ok=True)
    (item,) = (link for link in broken.links if (link.child, link.name) == ("store_sales", "item"))
    assert item.found.total == 4000 and 0 < item.found.satisfied < 4000
    assert not item.whole
    assert any(
        line.startswith("integrity store_sales.item -> item: found") for line in broken.failures()
    )


def test_the_checks_are_the_same_for_any_number_of_jobs(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    state = tmp_path / ".flux"
    applied = apply(bundle, state_dir=state)
    run_id = str(applied.artifacts["run_id"])
    results = [
        run_verify(
            bundle,
            run_id=run_id,
            state_dir=state,
            allow_declared_baseline_drift=True,
            jobs=jobs,
        )
        for jobs in (1, 2)
    ]
    assert results[0].artifacts["rules"] == results[1].artifacts["rules"]
    assert results[0].artifacts["integrity"] == results[1].artifacts["integrity"]
    assert results[0].stdout == results[1].stdout
