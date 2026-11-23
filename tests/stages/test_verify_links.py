# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim verify`` checks a conditional link and every declared sentinel (ADR 0022 section 9).

The billing star of ``test_sentinels_run``: an invoice holds its account only where
``i_amount > 250`` and writes ``none`` and ``-1`` for ``region`` and ``a_score`` where its link
reaches no row. Each is a line of the invoice's ``rules``: ``link:account`` covers the rows
whose link reaches a row and holds where the condition is TRUE, and
``sentinel:account.<column>`` covers the rows whose link reaches none and holds where the column
is its sentinel. Both are enforced when the run generated them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.stages._module_checks import module_checks
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify as run_verify

from .test_sentinels_run import LINKS, _bundle_with

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle_with(tmp_path_factory.mktemp("verify-links"), LINKS, "verify-links")


def _run(bundle: Path, state: Path) -> "tuple[RunLayout, Any]":
    planned = plan(bundle, state_dir=state)
    assert planned.success, planned.stderr
    applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]))
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    verified = run_verify(
        bundle, run_id=layout.run_id, state_dir=state, allow_declared_baseline_drift=True
    )
    return layout, verified


def _checks(bundle: Path, layout: RunLayout, *, without: str = "") -> Any:
    receipt = json.loads(layout.receipt_path.read_text("utf-8"))
    document = json.loads(layout.plan_path.read_text("utf-8"))
    if without:
        del receipt["emits"][without]
    return module_checks(bundle, layout, receipt, document, artifacts_ok=True)


def _edit(layout: RunLayout, column: str, change: Any) -> None:
    """Rewrite the invoice rows, *change* taking each row as a mapping and returning True once it edited."""
    path = layout.emit_path("invoice")
    names = json.loads(layout.seam_attestation_path("invoice").read_text("utf-8"))["columns"]
    rows: List[List[Any]] = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    for row in rows:
        if change(dict(zip(names, row, strict=True)), row, names.index(column)):
            break
    else:  # pragma: no cover - the fixture has such a row
        raise AssertionError("no row to edit")
    path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n", "utf-8")


def test_a_run_holds_its_conditions_and_every_sentinel_exists(bundle: Path, tmp_path: Path) -> None:
    _, verified = _run(bundle, tmp_path / ".flux")
    assert verified.success, verified.stderr
    lines = {entry["rule"]: entry for entry in verified.artifacts["rules"]["emits"]["invoice"]}
    for rule_id in ("link:account", "sentinel:account.region", "sentinel:account.a_score"):
        entry = lines[rule_id]
        assert (entry["form"], entry["origin"], entry["enforced"]) == ("check", "bundle", True)
        assert entry["satisfied"] == entry["total"] > 0 and entry["met"] is True, rule_id
    assert "link:account" in verified.stdout and "sentinel:account.region" in verified.stdout


def test_a_row_that_holds_a_link_where_the_condition_fails_breaks_its_line(
    bundle: Path, tmp_path: Path
) -> None:
    layout, verified = _run(bundle, tmp_path / ".flux")
    assert verified.success, verified.stderr
    accounts = [
        json.loads(line)[0] for line in layout.emit_path("account").read_text("utf-8").splitlines()
    ]

    def give_a_link(row: Dict[str, Any], cells: List[Any], position: int) -> bool:
        if row["i_amount"] <= 250 and row["i_account"] is None:
            cells[position] = accounts[0]
            return True
        return False

    _edit(layout, "i_account", give_a_link)
    broken = _checks(bundle, layout)
    (line,) = (s for s in broken.rules if s.rule_id == "link:account")
    assert line.holds.satisfied == line.holds.total - 1 and line.enforced and line.met is False
    assert any(text.startswith("rule invoice.link:account:") for text in broken.failures())


def test_a_row_with_no_link_that_loses_its_sentinel_breaks_its_line(
    bundle: Path, tmp_path: Path
) -> None:
    layout, verified = _run(bundle, tmp_path / ".flux")
    assert verified.success, verified.stderr

    def lose_it(row: Dict[str, Any], cells: List[Any], position: int) -> bool:
        if row["i_account"] is None:
            cells[position] = "somewhere"
            return True
        return False

    _edit(layout, "region", lose_it)
    broken = _checks(bundle, layout)
    (line,) = (s for s in broken.rules if s.rule_id == "sentinel:account.region")
    assert line.holds.satisfied == line.holds.total - 1 and line.enforced and line.met is False
    assert any(
        text.startswith("rule invoice.sentinel:account.region:") for text in broken.failures()
    )
    (other,) = (s for s in broken.rules if s.rule_id == "sentinel:account.a_score")
    assert other.met is True


def test_a_conditional_link_whose_parent_the_run_does_not_emit_is_named_and_fails_nothing(
    bundle: Path, tmp_path: Path
) -> None:
    layout, verified = _run(bundle, tmp_path / ".flux")
    assert verified.success, verified.stderr
    checks = _checks(bundle, layout, without="account")
    assert not checks.failures()
    assert any("invoice" in note and "does not emit" in note for note in checks.notes)
