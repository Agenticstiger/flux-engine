# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Ship accepts a bundle that declares a temporal key, one test for each key.

``lifetimes``, ``within``, ``rates``, ``hidden`` and ``hiddenColumns`` are extension declarations that
the plan leaves out of the reusable digest ``G``. Ship recompiles ``G`` from the bundle and compares it
with the plan's, so the two must leave out the same keys. Each test copies ``tests/fixtures/temporal``
into a temporary directory with one key, plans it on kernel 0.2.0, applies it and ships it with a
committed slice of every written emit, and pins that ship passes every check (``contract.plannedG``
included), writes ``EVIDENCE.md`` and leaves no withheld record. A bundle without a temporal key is the
control.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify
from tests.fixtures.temporal.bundle import copy_bundle

pytestmark = pytest.mark.integration

LIFETIMES = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, duration: { bins: ['0', '200', '900'], binWeights: ['3', '1'] } }
  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
"""
WITHIN = """\
within:
  - { emit: account, relationship: customer, date: a_open, target: '5' }
"""
RATES = WITHIN + """\
rates:
  - { emit: account, column: a_open, month: ['20', '1', '1', '1', '1', '1', '1', '1', '1', '1', '1', '1'] }
"""
#: A lifetime that reads the hidden column ``churn`` of the customer, and a date within it.
HIDDEN_COLUMN_KEYS = """\
lifetimes:
  - { emit: customer, start: cu_start, end: churn }
  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
within:
  - { emit: account, relationship: customer, date: a_open, target: '5' }
"""
#: A rule of the account that reads the hidden table through the link ``customer``.
HIDDEN_TABLE_RULES = (
    "        rules:\n"
    "          - id: open_copy\n"
    "            language: flux-expr/3\n"
    "            derive: a_open\n"
    '            expr: "ADD_DAYS(customer.cu_start, 0)"\n'
)


def _edit(bundle: Path, edit: Callable[[str], str]) -> None:
    path = bundle / "simulation.flux.yml"
    path.write_text(edit(path.read_text("utf-8")), "utf-8")


def _hide_column(text: str) -> str:
    text = text.replace(
        "      customer:\n        rows: 60\n",
        "      customer:\n        rows: 60\n        hiddenColumns:\n          - { name: churn, type: DATE }\n",
        1,
    )
    return text.replace(
        "            cu_score: { range",
        "            churn: { range: { min: '2024-01-01', max: '2025-12-31' } }\n"
        "            cu_score: { range",
        1,
    )


def _hide_table(text: str) -> str:
    text = text.replace(
        "      customer:\n        rows: 60\n",
        "      customer:\n        hidden: true\n        rows: 60\n",
        1,
    )
    anchor = "      account:\n        rows: 150\n"
    start = text.index(anchor)
    end = text.index("          failureAction: withholdBundle\n", start) + len(
        "          failureAction: withholdBundle\n"
    )
    return text[:end] + HIDDEN_TABLE_RULES + text[end:]


def _shipped(tmp_path: Path, keys: str, edit: Callable[[str], str] = lambda t: t) -> Dict[str, Any]:
    """Plan (kernel 0.2.0), apply and ship the fixture with *keys*, and what the run left behind."""
    bundle = copy_bundle(tmp_path, "bundle", keys)
    _edit(bundle, edit)
    state = tmp_path / "state"
    planned = plan(bundle, state_dir=state, kernel="0.2.0")
    assert planned.success, planned.stderr
    applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]))
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    slices: Dict[str, str] = {}
    for expose_id in json.loads(layout.plan_path.read_text("utf-8"))["emits"]:
        emitted = layout.emit_path(expose_id)
        if not emitted.exists():  # a hidden table writes no rows
            continue
        columns = json.loads(layout.seam_attestation_path(expose_id).read_text("utf-8"))["columns"]
        path = tmp_path / f"{expose_id}.ndjson"
        path.write_text(
            "".join(
                json.dumps(dict(zip(columns, json.loads(line), strict=True))) + "\n"
                for line in emitted.read_text("utf-8").splitlines()
                if line
            ),
            "utf-8",
        )
        slices[expose_id] = path.as_uri()
    result = ship(
        bundle,
        run_id=layout.run_id,
        out_dir=tmp_path / "out",
        state_dir=state,
        committed_slice=slices,
    )
    return {"result": result, "layout": layout, "emits": list(slices)}


def _failed_checks(result: Any) -> List[str]:
    """The names of every gate check that failed, wherever the stage artifact keeps them."""
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("passed") is False and "name" in node:
                found.append(str(node["name"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(result.artifacts)
    return found


def _assert_ships(shipped: Dict[str, Any]) -> None:
    result, layout = shipped["result"], shipped["layout"]
    assert "contract.plannedG" not in _failed_checks(result), result.stderr
    assert "FLX-8001" not in result.stderr, result.stderr
    assert result.success, result.stderr
    assert (layout.run_dir / "EVIDENCE.md").is_file()


def _assert_g_matches_plan(shipped: Dict[str, Any]) -> None:
    """Every written emit's attestation carries the ``G`` the plan recorded for it."""
    layout = shipped["layout"]
    planned = json.loads(layout.plan_path.read_text("utf-8"))["emits"]
    for expose_id in shipped["emits"]:
        attested = json.loads(layout.seam_attestation_path(expose_id).read_text("utf-8"))
        assert attested["G"] == planned[expose_id]["G"], expose_id


def test_a_bundle_without_a_temporal_key_ships(tmp_path: Path) -> None:
    shipped = _shipped(tmp_path, "")
    _assert_ships(shipped)
    _assert_g_matches_plan(shipped)
    assert len(shipped["emits"]) == 4


def test_a_bundle_with_lifetimes_and_a_duration_ships(tmp_path: Path) -> None:
    shipped = _shipped(tmp_path, LIFETIMES)
    _assert_ships(shipped)
    _assert_g_matches_plan(shipped)


def test_a_bundle_with_a_within_date_ships(tmp_path: Path) -> None:
    shipped = _shipped(tmp_path, LIFETIMES + WITHIN)
    _assert_ships(shipped)
    _assert_g_matches_plan(shipped)


def test_a_bundle_with_rates_on_a_within_date_ships(tmp_path: Path) -> None:
    shipped = _shipped(tmp_path, LIFETIMES + RATES)
    _assert_ships(shipped)
    _assert_g_matches_plan(shipped)


def test_a_bundle_with_a_hidden_table_read_through_a_link_ships(tmp_path: Path) -> None:
    shipped = _shipped(tmp_path, "", _hide_table)
    _assert_ships(shipped)
    _assert_g_matches_plan(shipped)
    assert "customer" not in shipped["emits"] and "account" in shipped["emits"]


def test_a_bundle_with_a_hidden_column_a_lifetime_reads_ships(tmp_path: Path) -> None:
    shipped = _shipped(tmp_path, HIDDEN_COLUMN_KEYS, _hide_column)
    _assert_ships(shipped)
    _assert_g_matches_plan(shipped)


def test_a_bundle_whose_rule_verify_fails_is_withheld_naming_the_rule(tmp_path: Path) -> None:
    """The lifetime rule of the account draws its end apart from its start, so a share of rows breaks it."""
    keys = HIDDEN_COLUMN_KEYS.replace(
        ", duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] }", ""
    )
    shipped = _shipped(tmp_path, keys, _hide_column)
    result = shipped["result"]
    bundle = tmp_path / "bundle"
    verified = verify(
        bundle,
        run_id=shipped["layout"].run_id,
        state_dir=tmp_path / "state",
        allow_declared_baseline_drift=True,
    )
    assert not verified.success
    assert "rule account.lifetime:a_open.a_close" in verified.stderr
    assert not result.success
    assert result.error_code == "FLX-8001"
    assert "run.rules: rule account.lifetime:a_open.a_close" in result.stdout
    assert not (shipped["layout"].run_dir / "EVIDENCE.md").exists()
