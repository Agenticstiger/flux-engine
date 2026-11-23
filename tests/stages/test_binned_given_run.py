# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column drawn given a column drawn from bins, end to end (ADR 0036 decision 3).

The bundle is ``tests/fixtures/copula`` with its columns given one another: ``balance`` (a
histogram) is given ``amount``'s bin, ``region`` (a categorical) is given ``balance``'s bin, and
``limit_raised`` is given the bins of a timestamp and a date column together. Every run is on
kernel 0.2.0. Each test holds one promise the declaration makes: the class of a row is the bin of
the parent's drawn value, the column path equals the per-row path, the rows of a slice are the
whole run's, ``--jobs`` and ``--shard`` change no byte, and ``--scale`` changes only the count.
"""

from __future__ import annotations

import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator import columns
from flux_engine.generator.rows import bin_class, class_names, is_binned
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.ship import ship
from tests.generator.test_rules_over_links import _by_both_paths
from tests.seam.test_copula_block import FIXTURE, _plan

pytestmark = pytest.mark.integration

NAMES = ["acct_id", "amount", "balance", "opened_at", "closed_on", "region", "limit_raised"]
_KEY = bytes(range(64, 96))

#: Each replacement edits one column of the fixture's fidelity block.
BALANCE = (
    "            balance:\n"
    "              range: { min: '0', max: '500' }\n"
    "              bins: ['0', '50', '200', '500']\n"
    "              binWeights: ['0.2', '0.5', '0.3']\n",
    "            balance:\n"
    "              range: { min: '0', max: '500' }\n"
    "              bins: ['0', '50', '200', '500']\n"
    "              given: amount\n"
    "              binWeightsGiven:\n"
    "                '0': ['0.8', '0.1', '0.1']\n"
    "                '100': ['0.1', '0.8', '0.1']\n"
    "                '400': ['0.1', '0.1', '0.8']\n",
)
REGION = (
    "            region: { levels: { north: '0.5', south: '0.5' } }\n",
    "            region:\n"
    "              given: balance\n"
    "              levelWeightsGiven:\n"
    "                '0': { north: '0.95', south: '0.05' }\n"
    "                '50': { north: '0.5', south: '0.5' }\n"
    "                '200': { north: '0.05', south: '0.95' }\n",
)
LIMIT = (
    "            limit_raised:\n"
    "              range: { min: '0', max: '9' }\n"
    "              bins: ['0', '3', '9']\n"
    "              binWeights: ['0.7', '0.3']\n",
    "            limit_raised:\n"
    "              range: { min: '0', max: '9' }\n"
    "              bins: ['0', '3', '9']\n"
    "              given: [opened_at, closed_on]\n"
    "              binWeightsGiven:\n"
    "                '2026-07-01T00:00:00Z':\n"
    "                  '2026-10-01': ['0.9', '0.1']\n"
    "                  '2026-11-01': ['0.6', '0.4']\n"
    "                '2026-08-01T00:00:00Z':\n"
    "                  '2026-10-01': ['0.3', '0.7']\n"
    "                  '2026-11-01': ['0.1', '0.9']\n",
)
EDITS = (BALANCE, REGION, LIMIT)


def binned_bundle(
    tmp_path: Path,
    name: str = "bundle",
    *,
    rows: int = 400,
    edits: Sequence[Tuple[str, str]] = EDITS,
) -> Path:
    """A copy of the copula fixture whose columns are given one another by their bins."""
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    simulation = target / "simulation.flux.yml"
    text = simulation.read_text("utf-8").replace("        rows: 400\n", f"        rows: {rows}\n")
    for old, new in edits:
        assert text.count(old) == 1, old
        text = text.replace(old, new, 1)
    simulation.write_text(text, "utf-8")
    return target


def _run(bundle: Path, **options: Any) -> Any:
    return pipeline.execute(bundle, kernel=KERNEL_V2, **options)


def _rows(outcome: Any) -> List[Sequence[Any]]:
    return [tuple(row) for row in outcome.emit("accounts").rows]


def _by(row: Sequence[Any]) -> Dict[str, Any]:
    return dict(zip(NAMES, row, strict=True))


@pytest.mark.parametrize("chunk", [4096, 7])
def test_the_column_path_equals_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    bundle = binned_bundle(tmp_path, "paths", rows=300)
    by_columns, by_rows, taken = _by_both_paths(monkeypatch, lambda: _run(bundle))
    assert _rows(by_columns) == _rows(by_rows)
    assert taken and all(taken)


def test_the_class_of_a_row_is_the_bin_of_its_parents_value(tmp_path: Path) -> None:
    rows = [_by(row) for row in _rows(_run(binned_bundle(tmp_path, rows=6000)))]
    # balance is given amount's bin: the bin of its own value follows the declared weights.
    first_bin = [row["balance"] for row in rows if row["amount"] < 100]
    last_bin = [row["balance"] for row in rows if row["amount"] >= 400]
    assert sum(value < 50 for value in first_bin) / len(first_bin) == pytest.approx(0.8, abs=0.04)
    assert sum(value >= 200 for value in last_bin) / len(last_bin) == pytest.approx(0.8, abs=0.04)
    # region is given balance's bin, which is itself given another column's: a chain.
    low = [row["region"] for row in rows if row["balance"] < 50]
    high = [row["region"] for row in rows if row["balance"] >= 200]
    assert low.count("north") / len(low) == pytest.approx(0.95, abs=0.03)
    assert high.count("south") / len(high) == pytest.approx(0.95, abs=0.03)
    # limit_raised is given the bins of a timestamp and a date together.
    early_october = [
        row["limit_raised"]
        for row in rows
        if row["opened_at"] < 1785542400000 and row["closed_on"].month == 10
    ]
    assert sum(value < 3 for value in early_october) / len(early_october) == pytest.approx(
        0.9, abs=0.05
    )


def test_a_value_is_placed_in_its_bin_the_last_closed_above(tmp_path: Path) -> None:
    column_plan = _plan(binned_bundle(tmp_path)).column_plan("amount")
    assert is_binned(column_plan) and class_names(column_plan) == ("0", "100", "400")
    assert bin_class(column_plan, Decimal("0")) == "0"
    assert bin_class(column_plan, Decimal("99.99")) == "0"
    assert bin_class(column_plan, Decimal("100")) == "100"
    assert bin_class(column_plan, Decimal("1000")) == "400"
    dates = _plan(binned_bundle(tmp_path, "dates")).column_plan("closed_on")
    assert class_names(dates) == ("2026-10-01", "2026-11-01")
    assert bin_class(dates, date(2026, 10, 31)) == "2026-10-01"
    assert bin_class(dates, date(2026, 12, 31)) == "2026-11-01"


@pytest.mark.parametrize("count", [3, 5])
def test_the_shards_joined_are_the_unsharded_run(tmp_path: Path, count: int) -> None:
    bundle = binned_bundle(tmp_path, "shards", rows=300)
    whole = _rows(_run(bundle))
    parts = [_rows(_run(bundle, shard=(i, count))) for i in range(1, count + 1)]
    assert [row for part in parts for row in part] == whole


@pytest.mark.parametrize("scale", ["0.25", "0.5", "2"])
def test_a_scaled_run_has_the_scaled_rows_on_either_path_and_replays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scale: str
) -> None:
    bundle = binned_bundle(tmp_path, "scales", rows=400)
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: _run(bundle, scale=Decimal(scale))
    )
    assert len(_rows(by_columns)) == int(400 * Decimal(scale))
    assert _rows(by_columns) == _rows(by_rows) and taken and all(taken)
    assert _rows(_run(bundle, scale=Decimal(scale))) == _rows(by_columns)
    # A shard of a scaled run is that run's slice.
    first = _rows(_run(bundle, scale=Decimal(scale), shard=(1, 2)))
    assert first == _rows(by_columns)[: len(first)]


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_jobs_do_not_move_the_rows(tmp_path: Path) -> None:
    bundle = binned_bundle(tmp_path, "jobs", rows=12000)
    runs = [_run(bundle, jobs=jobs) for jobs in (1, 4)]
    assert runs[1].emit("accounts").processes > 1
    assert (
        runs[0].emit("accounts").writer.stream_bytes()
        == runs[1].emit("accounts").writer.stream_bytes()
    )
    assert (
        runs[0].emit("accounts").writer.output_digest()
        == runs[1].emit("accounts").writer.output_digest()
    )


def _slice_of(layout: RunLayout, expose: str, target: Path) -> str:
    columns_ = json.loads(layout.seam_attestation_path(expose).read_text("utf-8"))["columns"]
    rows = [
        dict(zip(columns_, json.loads(line), strict=True))
        for line in layout.emit_path(expose).read_text("utf-8").splitlines()
        if line
    ]
    target.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return target.as_uri()


def _shipped(bundle: Path, tmp_path: Path) -> Tuple[RunLayout, Any]:
    state = tmp_path / ".flux"
    applied = apply(bundle, state_dir=state)
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])
    layout = RunLayout.for_state_dir(state, run_id)
    result = ship(
        bundle,
        run_id=run_id,
        out_dir=tmp_path / "dist",
        state_dir=state,
        committed_slice={"accounts": _slice_of(layout, "accounts", tmp_path / "slice.ndjson")},
        signing_key=_KEY,
    )
    assert result.success, result.stderr
    return layout, result


def test_a_bundle_with_binned_parents_ships_and_writes_the_evidence(tmp_path: Path) -> None:
    layout, _ = _shipped(binned_bundle(tmp_path), tmp_path)
    assert (layout.run_dir / "EVIDENCE.md").is_file()


def test_the_plans_g_is_the_g_that_ship_recompiles(tmp_path: Path) -> None:
    state = tmp_path / ".flux"
    planned = plan(binned_bundle(tmp_path), state_dir=state)
    assert planned.success, planned.stderr
    layout, _ = _shipped(binned_bundle(tmp_path, "again"), tmp_path / "second")
    recorded = json.loads(layout.plan_path.read_text("utf-8"))["emits"]["accounts"]
    assert recorded["G"] == recorded["fidelity"]["G"]
