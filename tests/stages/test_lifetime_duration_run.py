# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose lifetimes draw their ends as durations (ADR 0024 section 4).

``tests/fixtures/temporal`` is copied with a ``lifetimes`` block: the customers' end is a
duration with an ``open`` weight and the default ``endPastWindow`` (``open``, the end column is
nullable), and the accounts' close is a duration whose ``endPastWindow`` is ``kept``. The tests
run the bundle through the pipeline every command uses, recompute each end from the dates stream
and the cells, and then run ``plan``, ``apply`` and ``verify``:

* every end is not before its start, and every NULL end is one the plan explains: the column's
  own NULL, a draw of the ``open`` slot, or an end after the window;
* an account's end after the window is kept;
* the column path of kernel 0.2.0 holds the rows the per-row path holds;
* ``--jobs 1`` and ``--jobs 4`` give the same digests;
* kernel 0.1.0 refuses the keys, and a bundle without a duration keeps its plan.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.cli import main
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator import columns
from flux_engine.generator.dates import (
    KIND_DATE,
    date_of_days,
    days_of_date,
    duration_table,
    window_in_units,
)
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.rows import clear_memos, generate_rows
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify as run_verify
from tests.fixtures.temporal.bundle import CHAIN, UNCUT_CUSTOMER_END, copy_uncut

pytestmark = pytest.mark.integration

CUSTOMER = (
    "{ emit: customer, start: cu_start, end: cu_end, duration: "
    "{ bins: ['0', '100', '400', '900'], binWeights: ['5', '3', '1'], open: '2' } }"
)
ACCOUNT = (
    "{ emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: "
    "{ bins: ['0', '300', '900'], binWeights: ['2', '1'] } }"
)
KEYS = f"lifetimes:\n  - {CUSTOMER}\n  - {ACCOUNT}\n"
WINDOW_END = date(2025, 12, 31)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return copy_uncut(tmp_path_factory.mktemp("lifetime-duration"), "bundle", KEYS)


@pytest.fixture(scope="module")
def result(bundle: Path) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def _outcome(outcome: pipeline.PipelineResult, expose_id: str) -> pipeline.EmitOutcome:
    return next(e for e in outcome.emits if e.resolved.expose_id == expose_id)


def _rows(outcome: pipeline.PipelineResult, expose_id: str) -> List[Dict[str, Any]]:
    emitted = _outcome(outcome, expose_id)
    return [dict(zip(emitted.expose.column_names, row, strict=True)) for row in emitted.rows]


def _cells(outcome: pipeline.PipelineResult, expose_id: str) -> List[Dict[str, Any]]:
    """The rows as their own cells draw them, before any rule sets a column."""
    planned = _outcome(outcome, expose_id).plan
    assert planned is not None
    rows = generate_rows(replace(planned, rules=None), root=outcome.root)
    return [dict(zip(planned.expose.column_names, row, strict=True)) for row in rows]


# ---------------------------------------------------------------------------
# What the rows hold
# ---------------------------------------------------------------------------


def test_every_end_is_not_before_its_start(result: pipeline.PipelineResult) -> None:
    for expose_id, start, end in (
        ("customer", "cu_start", "cu_end"),
        ("account", "a_open", "a_close"),
    ):
        rows = _rows(result, expose_id)
        ended = [row for row in rows if row[end] is not None]
        assert len(ended) > 0.1 * len(rows)
        assert all(row[end] >= row[start] for row in ended)


def _recomputed(
    result: pipeline.PipelineResult, expose_id: str, column: str
) -> List[Tuple[Dict[str, Any], Any, str]]:
    """Each row's end as the dates stream and the cells give it, and why: ``own``, ``open``, ``past`` or ``kept``."""
    planned = _outcome(result, expose_id).plan
    assert planned is not None and planned.rules is not None
    (rule,) = [rule for rule in planned.rules.rules if rule.duration is not None]
    assert rule.duration is not None and rule.column == column
    table = duration_table(rule.duration.histogram)
    window = window_in_units(KIND_DATE, planned.time_bounds)
    assert window is not None
    streams = CellStreams(result.root, planned.sim_id, expose_id)
    found = []
    for index, cell in enumerate(_cells(result, expose_id)):
        if cell[column] is None:
            found.append((cell, None, "own"))  # NULLs first: the end's own NULL stands
            continue
        u = streams.dates_stream_at(index, column, 1).uniform53()
        slot = bisect_right(table.cuts, u)
        if slot >= len(table.days):
            found.append((cell, None, "open"))
            continue
        end = rule.duration.end_of(days_of_date(cell[rule.duration.start]), u, window)
        found.append((cell, None, "past") if end is None else (cell, date_of_days(end), "kept"))
    return found


def test_every_null_end_is_one_the_plan_explains(result: pipeline.PipelineResult) -> None:
    written = _rows(result, "customer")
    reasons: Dict[str, int] = {"own": 0, "open": 0, "past": 0, "kept": 0}
    for row, (cell, end, reason) in zip(
        written, _recomputed(result, "customer", "cu_end"), strict=True
    ):
        reasons[reason] += 1
        assert row["cu_start"] == cell["cu_start"]
        assert row["cu_end"] == end
        if row["cu_end"] is None:
            assert reason in ("own", "open", "past")
        else:
            assert row["cu_end"] <= WINDOW_END  # an open end never lies past the window
    # Both draws that make a NULL happen on this fixture, beside the column's own nullFraction.
    assert (
        reasons["own"] > 0 and reasons["open"] > 0 and reasons["past"] > 0 and reasons["kept"] > 0
    )


def test_an_account_end_past_the_window_is_kept(result: pipeline.PipelineResult) -> None:
    written = _rows(result, "account")
    kept_past = 0
    for row, (_cell, end, reason) in zip(
        written, _recomputed(result, "account", "a_close"), strict=True
    ):
        assert reason in ("own", "kept")  # no open slot, and an end past the window is kept
        assert row["a_close"] == end
        if end is not None and end > WINDOW_END:
            kept_past += 1
            assert row["a_close"] > WINDOW_END
    assert kept_past > 0


def test_the_plan_lists_the_step_only_for_an_emit_that_declares_a_duration(
    result: pipeline.PipelineResult, tmp_path: Path
) -> None:
    documents = {e.resolved.expose_id: e.plan for e in result.emits}
    for expose_id in ("customer", "account"):
        plan_ = documents[expose_id]
        assert plan_ is not None
        listed = plan_.as_document()["rules"]["rules"]
        assert [rule["form"] for rule in listed] == ["lifetime"]
        assert listed[0]["lifetime"]["endPastWindow"] == (
            "open" if expose_id == "customer" else "kept"
        )
    for expose_id in ("invoice", "payment"):
        plan_ = documents[expose_id]
        assert plan_ is not None and plan_.rules is None and "rules" not in plan_.as_document()
    # Lifetimes with no duration add no step (the chain's within entries do, and are left out).
    plain = pipeline.execute(
        copy_uncut(tmp_path, "chain", CHAIN.split("within:")[0]), kernel=KERNEL_V2
    )
    assert all(e.plan is not None and e.plan.rules is None for e in plain.emits)
    # The generatorDigest moves for the emits that declare a duration.
    plain_digests = {e.resolved.expose_id: e.plan.digest().value for e in plain.emits if e.plan}
    digests = {e.resolved.expose_id: e.plan.digest().value for e in result.emits if e.plan}
    assert digests["customer"] != plain_digests["customer"]
    assert digests["account"] != plain_digests["account"]


def test_the_coverage_says_the_end_is_a_derived_column(result: pipeline.PipelineResult) -> None:
    rows = {row.column: row for row in _outcome(result, "customer").coverage.columns}
    assert rows["cu_end"].decided_by == "rule"
    assert rows["cu_end"].source == "bundle.lifetimes.cu_end"
    assert rows["cu_start"].decided_by == "fidelity"


def test_a_target_on_a_lifetime_with_a_duration_plans(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end, target: '2', "
        "duration: { bins: ['0', '100'], binWeights: ['1'] } }\n"
    )
    bundle = copy_uncut(tmp_path, "target", keys)
    planned = plan(bundle, state_dir=tmp_path / ".flux", kernel="0.2.0")
    assert planned.success, planned.stderr


def test_a_duration_on_a_column_a_rule_also_sets_is_refused(tmp_path: Path) -> None:
    bundle = copy_uncut(tmp_path, "two", KEYS)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert f"          {UNCUT_CUSTOMER_END}\n" in text
    simulation.write_text(
        text.replace(
            "      customer:\n        rows: 60\n",
            "      customer:\n        rows: 60\n        rules:\n"
            "          - id: end_copy\n            language: flux-expr/3\n"
            "            derive: cu_end\n            expr: ADD_DAYS(cu_start, 0)\n",
            1,
        ),
        "utf-8",
    )
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-6203"


# ---------------------------------------------------------------------------
# The column path, the kernels and a bundle that declares none
# ---------------------------------------------------------------------------


def test_the_column_path_gives_the_rows_of_the_per_row_path(
    result: pipeline.PipelineResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: List[bool] = []
    column_rows = columns.column_rows

    def spy(*args: Any) -> Any:
        computed = column_rows(*args)
        taken.append(computed is not None)
        return computed

    outputs: Dict[str, str] = {}
    for name, patched in (("columns", spy), ("rows", lambda *args: None)):
        clear_memos()
        with monkeypatch.context() as patch:
            patch.setattr(columns, "column_rows", patched)
            planned = [_outcome(result, e).plan for e in ("customer", "account")]
            outputs[name] = repr(
                [generate_rows(p, root=result.root) for p in planned if p is not None]
            )
    assert any(taken)
    assert outputs["columns"] == outputs["rows"]


def test_kernel_0_1_0_refuses_the_keys_naming_the_kernel(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105"
    assert "kernel 0.1.0" in str(caught.value) and "lifetimes" in str(caught.value)


def test_a_bundle_without_the_keys_is_the_bundle_it_was(tmp_path: Path) -> None:
    for name, kernel in (("v1", KERNEL_V1), ("v2", KERNEL_V2)):
        plain = pipeline.execute(copy_uncut(tmp_path, f"plain-{name}"), kernel=kernel)
        assert all(e.plan is not None and e.plan.rules is None for e in plain.emits)
        assert all("rules" not in e.plan.as_document() for e in plain.emits if e.plan)


# ---------------------------------------------------------------------------
# plan, apply and verify
# ---------------------------------------------------------------------------


def _big(tmp_path: Path) -> Path:
    """The bundle with enough customers and accounts for ``--jobs 4`` to cut them into slices."""
    big = copy_uncut(tmp_path, "big", KEYS)
    path = big / "simulation.flux.yml"
    text = path.read_text("utf-8")
    rows = 2 * parallel.MIN_ROWS_PER_JOB + 301
    for old in ("rows: 60\n", "rows: 150\n"):
        assert text.count(old) == 1
        text = text.replace(old, f"rows: {rows}\n")
    path.write_text(text, "utf-8")
    return big


def _loop(bundle: Path, state: Path, jobs: int) -> Dict[str, Any]:
    planned = plan(bundle, state_dir=state, jobs=jobs, kernel="0.2.0")
    assert planned.success, planned.stderr
    applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    verified = run_verify(
        bundle,
        run_id=layout.run_id,
        state_dir=state,
        allow_declared_baseline_drift=True,
        jobs=jobs,
    )
    assert verified.success, verified.stderr
    return {
        "layout": layout,
        "plan": planned.artifacts.get("plan_digest"),
        "digests": {
            k: v for k, v in applied.artifacts.items() if "digest" in k.lower() and k != "run_id"
        },
    }


@pytest.mark.slow
def test_jobs_one_and_four_give_the_same_digests(tmp_path: Path) -> None:
    bundle = _big(tmp_path)
    runs = [_loop(bundle, tmp_path / f"state-{jobs}", jobs) for jobs in (1, 4)]
    assert runs[0]["plan"] == runs[1]["plan"]
    assert runs[0]["digests"] == runs[1]["digests"]
    for run in runs:
        lines = run["layout"].emit_path("customer").read_text("utf-8").splitlines()
        assert len(lines) == 2 * parallel.MIN_ROWS_PER_JOB + 301
        rows = [json.loads(line) for line in lines]
        assert all(end is None or end >= start for _, start, end, _ in rows)
        assert any(end is None for _, _, end, _ in rows)
    first = runs[0]["layout"].emit_path("account").read_text("utf-8")
    assert first == runs[1]["layout"].emit_path("account").read_text("utf-8")


@pytest.mark.slow
def test_the_command_line_plans_applies_and_verifies_a_bundle_with_durations(
    bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = str(tmp_path / ".flux")
    assert main(["-j", "plan", str(bundle), "--state-dir", state, "--kernel", "0.2.0"]) == 0
    planned = json.loads(capsys.readouterr().out)
    run_id = planned["artifacts"]["run_id"]
    assert main(["-j", "apply", str(bundle), "--state-dir", state, "--run-id", run_id]) == 0
    capsys.readouterr()
    layout = RunLayout.for_state_dir(state, run_id)
    for line in layout.emit_path("customer").read_text("utf-8").splitlines():
        _, start, end, _ = json.loads(line)
        assert end is None or end >= start
    verify_args = ["-j", "verify", str(bundle), "--state-dir", state, "--run-id", run_id]
    assert main([*verify_args, "--allow-declared-baseline-drift"]) == 0
    assert json.loads(capsys.readouterr().out)["stage"] == "verify"
    assert main(["-j", "plan", str(bundle), "--state-dir", state, "--kernel", "0.1.0"]) == 1
    assert "FLX-3105" in capsys.readouterr().err
