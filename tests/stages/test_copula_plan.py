# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The plan of an emit's copula (ADR 0035 decision 5): ``plan.json``'s member and ``EVIDENCE.md``.

An emit that declares a copula has ``copula: {alpha, factor}`` in its plan entry, so the shrinkage
and the factor's digest enter ``planDigest`` there; an emit that declares none has no such key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.core import copula as core
from flux_engine.evidence.report import render_copula_note
from flux_engine.stages.plan import plan
from tests.seam.test_copula_block import COPULA, bundle_with
from tests.stages.test_ship_copula import _shipped

pytestmark = pytest.mark.integration

#: Three columns whose latent matrix is not a correlation matrix, so the plan shrinks it.
SHRUNK = (
    "copula:\n  columns: [amount, balance, opened_at]\n  rankCorrelations:\n    measure: kendall\n"
    "    pairs:\n"
    "      - { columns: [amount, balance], value: '0.9' }\n"
    "      - { columns: [balance, opened_at], value: '0.9' }\n"
    "      - { columns: [amount, opened_at], value: '-0.9' }\n"
)


def _plan_document(bundle: Path, tmp_path: Path) -> Dict[str, Any]:
    result = plan(bundle, state_dir=tmp_path / ".flux")
    assert result.success, result.stderr
    run_id = str(result.artifacts["run_id"])
    path = tmp_path / ".flux" / "runs" / run_id / "plan.json"
    return dict(json.loads(path.read_text("utf-8")))


def test_an_emit_without_a_copula_has_no_member(tmp_path: Path) -> None:
    document = _plan_document(bundle_with(tmp_path, None), tmp_path)
    assert "copula" not in document["emits"]["accounts"]


def test_a_valid_matrix_has_alpha_zero_and_the_factors_digest(tmp_path: Path) -> None:
    document = _plan_document(bundle_with(tmp_path, COPULA), tmp_path)
    member = document["emits"]["accounts"]["copula"]
    expected = core.plan_copula(
        ["amount", "balance", "opened_at"],
        "kendall",
        {("amount", "balance"): "0.5", ("amount", "opened_at"): "-0.25"},
    )
    assert member == {"alpha": "0", "factor": expected.digest}


def test_a_matrix_that_fails_is_shrunk_and_the_plan_says_how_much(tmp_path: Path) -> None:
    document = _plan_document(bundle_with(tmp_path, SHRUNK), tmp_path)
    member = document["emits"]["accounts"]["copula"]
    assert float(member["alpha"]) > 0
    assert len(member["factor"]) == 64


def test_the_copula_moves_the_plan_digest(tmp_path: Path) -> None:
    plain = _plan_document(bundle_with(tmp_path, None, "a"), tmp_path / "a")
    moved = _plan_document(bundle_with(tmp_path, COPULA, "b"), tmp_path / "b")
    assert plain["planDigest"] != moved["planDigest"]
    # The same declaration plans to the same bytes.
    again = _plan_document(bundle_with(tmp_path, COPULA, "c"), tmp_path / "c")
    assert again["emits"]["accounts"]["copula"] == moved["emits"]["accounts"]["copula"]


def test_evidence_states_the_copula_and_that_a_shrunk_one_was_shrunk(tmp_path: Path) -> None:
    layout, _ = _shipped(bundle_with(tmp_path, SHRUNK), tmp_path)
    text = (layout.run_dir / "EVIDENCE.md").read_text("utf-8")
    fidelity = text[text.index("## Fidelity") : text.index("## Population")]
    assert "Copula of emit `accounts`" in fidelity
    assert "`amount`, `balance`, `opened_at`" in fidelity and "Kendall" in fidelity
    assert "was shrunk (alpha" in fidelity and "one minus alpha" in fidelity


def test_evidence_of_a_valid_matrix_says_alpha_zero(tmp_path: Path) -> None:
    layout, _ = _shipped(bundle_with(tmp_path, COPULA), tmp_path)
    text = (layout.run_dir / "EVIDENCE.md").read_text("utf-8")
    assert "valid as written (alpha 0)" in text and "shrunk" not in text


def test_a_run_without_a_copula_writes_no_copula_note(tmp_path: Path) -> None:
    layout, _ = _shipped(bundle_with(tmp_path, None), tmp_path)
    assert "Copula" not in (layout.run_dir / "EVIDENCE.md").read_text("utf-8")


def test_the_note_is_none_without_a_copula() -> None:
    assert render_copula_note(()) is None
    note = render_copula_note([("t", ["a", "b"], "spearman", "0.25")])
    assert note is not None and "Spearman" in note and "alpha 0.25" in note
