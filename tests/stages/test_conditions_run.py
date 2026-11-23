# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose Simulation declares conditions (ADR 0028 section 10).

``tests/fixtures/conditions`` gets its ``conditions`` from the tests. The runs go through the
pipeline every command uses and through the CLI (``plan``, ``apply``, ``verify``), and check what a
run hands on: the groups' rows hold their values, jobs and shards give the bytes one process gives,
and ``--scale`` scales the groups' counts by the largest remainder.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from flux_engine.cli import main
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import build_plan_document
from tests.seam.test_conditions_block import TWO_GROUPS, bundle_with

pytestmark = pytest.mark.integration

# plan, region, channel, tier, spend, note, then the identifier first: sub_id.
_ID, _PLAN, _REGION, _CHANNEL, _TIER = 0, 1, 2, 3, 4


def _rows(result: pipeline.PipelineResult) -> Tuple[Tuple[Any, ...], ...]:
    (emit,) = result.emits
    return emit.rows


def _counts(result: pipeline.PipelineResult) -> List[int]:
    (emit,) = result.emits
    assert emit.plan is not None and emit.plan.conditions is not None
    return [group.count or 0 for group in emit.plan.conditions.groups]


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    return pipeline.execute(bundle_with(tmp_path_factory.mktemp("conditions-run"), TWO_GROUPS))


def test_the_groups_rows_hold_their_values_in_the_order_the_groups_are_listed(
    run: pipeline.PipelineResult,
) -> None:
    rows = _rows(run)
    assert len(rows) == 1000
    assert {(row[_PLAN], row[_REGION]) for row in rows[:400]} == {("premium", "EU")}
    assert {row[_PLAN] for row in rows[400:]} == {"basic"}
    # What is not fixed is drawn given what is: a premium subscription in the EU has a channel and
    # a tier the declared weights allow, and the basic ones reach all three regions.
    assert {row[_CHANNEL] for row in rows[:400]} == {"online", "store"}
    assert {row[_REGION] for row in rows[400:]} == {"APAC", "EU", "US"}
    assert not any(
        row[_PLAN] == "basic" and row[_CHANNEL] == "store" and row[_TIER] == "gold" for row in rows
    )


def test_the_plan_records_the_groups(run: pipeline.PipelineResult) -> None:
    (emit,) = run.emits
    assert emit.plan is not None
    assert emit.plan.as_document()["conditions"] == {
        "groups": [
            {"values": {"plan": "premium", "region": "EU"}, "count": 400},
            {"values": {"plan": "basic"}, "count": 600},
        ]
    }
    assert _counts(run) == [400, 600]
    assert build_plan_document(run)


def test_a_second_run_of_the_bundle_gives_the_same_rows(
    run: pipeline.PipelineResult, tmp_path: Path
) -> None:
    again = pipeline.execute(bundle_with(tmp_path, TWO_GROUPS))
    assert _rows(again) == _rows(run)
    assert again.emits[0].result == run.emits[0].result


def test_one_job_and_four_make_the_same_run(tmp_path: Path) -> None:
    rows = 3 * parallel.MIN_ROWS_PER_JOB + 11
    text = (
        "conditions:\n  subscriptions:\n"
        f"    - {{ values: {{ plan: premium, region: EU }}, count: {rows // 3} }}\n"
        f"    - {{ values: {{ tier: gold }}, count: {rows - rows // 3} }}\n"
    )
    bundle = bundle_with(tmp_path, text, rows=rows)
    one = pipeline.execute(bundle, jobs=1)
    four = pipeline.execute(bundle, jobs=4)
    assert build_plan_document(four) == build_plan_document(one)
    assert four.emits[0].result == one.emits[0].result
    assert four.emits[0].writer.lines() == one.emits[0].writer.lines()
    assert four.components.emits == one.components.emits
    if parallel.workers_for(rows, 4) > 1:
        assert four.emits[0].processes > 1


@pytest.mark.parametrize(
    ("scale", "counts"),
    [("0.5", [200, 300]), ("2", [800, 1200]), ("0.25", [100, 150]), ("1.5", [600, 900])],
)
def test_scale_scales_each_groups_count(tmp_path: Path, scale: str, counts: List[int]) -> None:
    result = pipeline.execute(bundle_with(tmp_path, TWO_GROUPS), scale=Decimal(scale))
    assert _counts(result) == counts
    rows = _rows(result)
    assert len(rows) == sum(counts)
    assert {(row[_PLAN], row[_REGION]) for row in rows[: counts[0]]} == {("premium", "EU")}
    assert {row[_PLAN] for row in rows[counts[0] :]} == {"basic"}


def test_scale_takes_the_largest_remainder_over_the_groups(tmp_path: Path) -> None:
    text = (
        "conditions:\n  subscriptions:\n"
        "    - { values: { plan: premium }, count: 3 }\n"
        "    - { values: { plan: basic }, count: 3 }\n"
        "    - { values: { region: EU }, count: 4 }\n"
    )
    bundle = bundle_with(tmp_path, text, rows=10)
    assert _counts(pipeline.execute(bundle, scale=Decimal("0.5"))) == [2, 1, 2]
    assert _counts(pipeline.execute(bundle, scale=Decimal("0.1"))) == [0, 0, 1]
    assert _counts(pipeline.execute(bundle, scale=Decimal("3"))) == [9, 9, 12]


def test_scale_one_changes_nothing(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, TWO_GROUPS)
    assert _rows(pipeline.execute(bundle, scale=Decimal(1))) == _rows(pipeline.execute(bundle))


def test_the_two_halves_of_a_shard_make_the_whole_run(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, TWO_GROUPS)
    whole = _rows(pipeline.execute(bundle))
    first = _rows(pipeline.execute(bundle, shard=(1, 2)))
    second = _rows(pipeline.execute(bundle, shard=(2, 2)))
    assert first + second == whole
    # The cut at 500 falls inside the second group: the rows keep their group.
    assert {row[_PLAN] for row in second} == {"basic"}
    thirds = [_rows(pipeline.execute(bundle, shard=(i, 3))) for i in (1, 2, 3)]
    assert thirds[0] + thirds[1] + thirds[2] == whole
    assert {(row[_PLAN], row[_REGION]) for row in thirds[0]} == {("premium", "EU")}


def test_the_cli_plans_applies_and_verifies_a_conditioned_bundle(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, TWO_GROUPS)
    state = tmp_path / ".flux"

    def cli(*arguments: str) -> Any:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*arguments, "--json"])
        assert code == 0, err.getvalue() + out.getvalue()
        return json.loads(out.getvalue())

    assert cli("validate", str(bundle))["ok"]
    planned = cli("plan", str(bundle), "--state-dir", str(state))
    run_id = planned["artifacts"]["run_id"]
    applied = cli("apply", str(bundle), "--run-id", run_id, "--state-dir", str(state))
    assert applied["artifacts"]["run_digest"].startswith("sha256:")
    verified = cli("verify", str(bundle), "--run-id", run_id, "--state-dir", str(state))
    assert verified["artifacts"]["rowConformance"]["ok"]
    lines = RunLayout.for_state_dir(state, run_id).emit_path("subscriptions")
    emitted = [json.loads(line) for line in lines.read_text("utf-8").splitlines() if line]
    assert len(emitted) == 1000
    assert {(row[_PLAN], row[_REGION]) for row in emitted[:400]} == {("premium", "EU")}
    replayed = cli(
        "replay",
        str(bundle),
        "--run-id",
        run_id,
        "--assert-digest",
        applied["artifacts"]["run_digest"],
        "--state-dir",
        str(state),
    )
    assert replayed["ok"]


def test_a_zero_probability_group_is_refused_at_plan_before_a_row_is_drawn(
    tmp_path: Path,
) -> None:
    from flux_engine.core.errors import FluxError
    from flux_engine.generator.conditions import FLX_3646

    # Basic subscriptions bought in a store are never gold: the declared weight is zero.
    text = (
        "conditions:\n  subscriptions:\n"
        "    - { values: { plan: premium }, count: 10 }\n"
        "    - { values: { plan: basic, channel: store, tier: gold }, count: 10 }\n"
    )
    bundle = bundle_with(tmp_path, text, rows=20)
    with pytest.raises(FluxError) as refused:
        pipeline.execute(bundle)
    assert refused.value.code == FLX_3646
    assert "group 1" in str(refused.value)
    state = tmp_path / ".flux"
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["plan", str(bundle), "--state-dir", str(state)])
    assert code != 0
    assert "FLX-3646" in out.getvalue() + err.getvalue()
    assert not (state / "runs").exists() or not any((state / "runs").iterdir())
