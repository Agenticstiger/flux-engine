# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A conditioned run whose columns are given the bin of another (ADR 0036 decision 5), end to end.

``tests/fixtures/conditions`` with ``plan`` drawn given the bin of ``spend``, a histogram. The
groups fix ``plan`` and ``region``; the bins of ``spend`` are summed out exactly, so the rows
that follow draw ``spend`` from its posterior. Jobs, shards and ``--scale`` change no byte they
changed before, and the run ships and writes ``EVIDENCE.md``.
"""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.ship import ship
from tests.seam.test_conditions_block import FIXTURE, NAMESPACE, TWO_GROUPS
from tests.stages.test_ship_conditions_evidence import _KEY, _slice_of

pytestmark = pytest.mark.integration

PLAN = (
    "            plan: { levels: { basic: '0.6', premium: '0.4' } }\n",
    "            plan:\n"
    "              given: spend\n"
    "              levelWeightsGiven:\n"
    "                '0': { basic: '9', premium: '1' }\n"
    "                '100': { basic: '1', premium: '9' }\n",
)
SPEND = (
    "              given: plan\n              binWeightsGiven:\n"
    "                basic: ['3', '1']\n                premium: ['1', '3']\n",
    "              binWeights: ['3', '1']\n",
)


def _bundle(tmp_path: Path, name: str = "bundle", conditions: str = TWO_GROUPS) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    simulation = target / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    for old, new in (PLAN, SPEND):
        assert text.count(old) == 1, old
        text = text.replace(old, new, 1)
    block = "".join(f"    {line}\n" for line in conditions.splitlines())
    simulation.write_text(text.replace(NAMESPACE, NAMESPACE + block, 1), "utf-8")
    return target


def _rows(result: Any) -> List[Tuple[Any, ...]]:
    (emit,) = result.emits
    return list(emit.rows)


def test_the_groups_hold_their_values_and_spend_follows_its_posterior(tmp_path: Path) -> None:
    rows = _rows(pipeline.execute(_bundle(tmp_path)))
    spend = 5
    assert {(row[1], row[2]) for row in rows[:400]} == {("premium", "EU")}
    # The upper bin of spend (100 to 300) has a quarter of the prior, and premium nine times
    # the weight there: its posterior given premium is three quarters. Given basic the lower
    # bin's is 27 in 28.
    premium = [row[spend] for row in rows[:400]]
    assert sum(value >= 100 for value in premium) / len(premium) > 0.65
    basic = [row[spend] for row in rows[400:]]
    assert sum(value < 100 for value in basic) / len(basic) > 0.9


def test_the_two_halves_of_a_shard_make_the_whole_run(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    whole = _rows(pipeline.execute(bundle))
    parts = [_rows(pipeline.execute(bundle, shard=(i, 3))) for i in (1, 2, 3)]
    assert [row for part in parts for row in part] == whole


@pytest.mark.parametrize("scale", ["0.5", "2"])
def test_a_scaled_run_scales_the_groups(tmp_path: Path, scale: str) -> None:
    bundle = _bundle(tmp_path)
    result = pipeline.execute(bundle, scale=Decimal(scale))
    assert len(_rows(result)) == int(1000 * Decimal(scale))
    assert _rows(result) == _rows(pipeline.execute(bundle, scale=Decimal(scale)))


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_one_job_and_four_make_the_same_run(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    one, four = pipeline.execute(bundle, jobs=1), pipeline.execute(bundle, jobs=4)
    assert _rows(one) == _rows(four)


def test_the_run_ships_and_writes_the_evidence(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
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
        committed_slice={
            "subscriptions": _slice_of(layout, "subscriptions", tmp_path / "slice.ndjson")
        },
        signing_key=_KEY,
    )
    assert result.success, result.stderr
    assert (layout.run_dir / "EVIDENCE.md").is_file()
