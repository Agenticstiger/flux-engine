# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim verify`` checks every copy against its source through the link (ADR 0022 section 9).

* a run that made the copies reads ``satisfied == total`` for each, as a rule line of origin ``shared``;
* a run whose output is edited to break a copy row fails with that line;
* a copy the run did not make (kernel 0.1.0, a key or foreign key member) is reported and fails nothing;
* a copy whose parent the run does not emit is named as one that cannot be checked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.stages._module_checks import module_checks
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify as run_verify
from tests.bundles import repin_contract

from .test_shared_columns_run import _CONTRACT_TEXT, _bundle

pytestmark = pytest.mark.integration

ROWS = 400


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("verify-copies"), rows=ROWS)


def _run(bundle: Path, state: Path, *, kernel: Optional[str] = None) -> "tuple[RunLayout, Any]":
    planned = plan(bundle, state_dir=state, kernel=kernel)
    assert planned.success, planned.stderr
    applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]))
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    verified = run_verify(
        bundle, run_id=layout.run_id, state_dir=state, allow_declared_baseline_drift=True
    )
    return layout, verified


def _lines(verified: Any, expose_id: str) -> Dict[str, Dict[str, Any]]:
    return {entry["rule"]: entry for entry in verified.artifacts["rules"]["emits"][expose_id]}


def _checks(bundle: Path, layout: RunLayout) -> Any:
    receipt = json.loads(layout.receipt_path.read_text("utf-8"))
    document = json.loads(layout.plan_path.read_text("utf-8"))
    return module_checks(bundle, layout, receipt, document, artifacts_ok=True)


def test_every_copy_reads_satisfied_equals_total_and_shows_in_the_output(
    bundle: Path, tmp_path: Path
) -> None:
    _, verified = _run(bundle, tmp_path / ".flux")
    assert verified.success, verified.stderr
    lines = _lines(verified, "invoice")
    for rule_id in ("account.region", "account.a_score"):
        entry = lines[rule_id]
        assert (entry["form"], entry["origin"], entry["enforced"]) == ("derive", "shared", True)
        assert entry["satisfied"] == entry["total"] > 0 and entry["met"] is True
        assert (
            entry["total"] < ROWS
        )  # the rows with a NULL key reach no row: not the copy's to decide
    # The copy of the foreign key is not made and is not a column of the parent: no line for it.
    assert "account.i_account" not in lines
    assert "account.region " in verified.stdout


def test_a_run_edited_to_break_one_copy_row_fails_with_that_line(
    bundle: Path, tmp_path: Path
) -> None:
    layout, verified = _run(bundle, tmp_path / ".flux")
    assert verified.success, verified.stderr
    assert not _checks(bundle, layout).failures()
    path = layout.emit_path("invoice")
    names = json.loads(layout.seam_attestation_path("invoice").read_text("utf-8"))["columns"]
    column = names.index("a_score")
    rows: List[List[Any]] = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    row = next(r for r in rows if r[column] is not None)
    row[column] = int(row[column]) + 1
    path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n", "utf-8")
    broken = _checks(bundle, layout)
    (line,) = (s for s in broken.rules if s.rule_id == "account.a_score")
    assert line.holds.satisfied == line.holds.total - 1 and line.enforced
    assert line.met is False
    assert any(text.startswith("rule invoice.account.a_score:") for text in broken.failures())
    assert all(s.met is not False for s in broken.rules if s.rule_id != "account.a_score")


def test_a_bundle_without_copies_has_no_copy_line_under_either_kernel(
    tmp_path: Path,
) -> None:
    plain = _bundle(tmp_path / "plain", rows=ROWS, copies=False)
    for kernel in ("0.1.0", "0.2.0"):
        _, verified = _run(plain, tmp_path / f".flux-{kernel}", kernel=kernel)
        assert verified.success, verified.stderr
        assert "rules" not in verified.artifacts, kernel
        assert "shared" not in verified.stdout and "(not enforced)" not in verified.stdout


_KEY_CONTRACT = _CONTRACT_TEXT.replace(
    "        - { name: cu_age, type: INT, required: false }\n",
    "        - { name: cu_age, type: INT, required: false }\n"
    "        - { name: a_customer, type: STRING, required: false }\n",
    1,
).replace("sharedColumns: [region]", "sharedColumns: [region, a_customer]", 1)


def test_a_foreign_key_member_is_reported_and_fails_nothing(tmp_path: Path) -> None:
    extension_bundle = _bundle(tmp_path, rows=ROWS, contract=_KEY_CONTRACT)
    text = (extension_bundle / "simulation.flux.yml").read_text("utf-8")
    simulation = text.replace(
        "            cu_age: { range: { min: '18', max: '90' } }\n",
        "            cu_age: { range: { min: '18', max: '90' } }\n"
        "            a_customer: { levels: { c1: '0.5', c2: '0.5' } }\n",
        1,
    )
    (extension_bundle / "simulation.flux.yml").write_text(simulation, "utf-8")
    repin_contract(
        extension_bundle / "store-star.fluid.yml",
        extension_bundle / "simulation.flux.yml",
        "retail.store_star",
    )
    _, verified = _run(extension_bundle, tmp_path / ".flux")
    assert verified.success, verified.stderr
    account = _lines(verified, "account")
    assert account["customer.region"]["enforced"] is True
    key = account["customer.a_customer"]
    assert key["origin"] == "shared" and key["enforced"] is False and key["met"] is None
    assert key["total"] > 0


def test_a_copy_whose_parent_the_run_does_not_emit_is_named_and_fails_nothing(
    bundle: Path, tmp_path: Path
) -> None:
    layout, verified = _run(bundle, tmp_path / ".flux")
    assert verified.success, verified.stderr
    receipt = json.loads(layout.receipt_path.read_text("utf-8"))
    document = json.loads(layout.plan_path.read_text("utf-8"))
    del receipt["emits"]["account"]
    checks = module_checks(bundle, layout, receipt, document, artifacts_ok=True)
    assert not checks.failures()
    assert any("invoice" in note and "does not emit" in note for note in checks.notes)
    assert not [s for s in checks.rules if s.expose_id == "invoice"]
