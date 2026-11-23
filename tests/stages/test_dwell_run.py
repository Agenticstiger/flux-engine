# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose lifetime's duration is an exponential or a dwell family (ADR 0025 section 3).

``tests/fixtures/temporal`` is copied with a hidden STRING column ``seg`` on the customers (and on
the accounts) that a lifetime's ``duration`` is given. The tests run the bundle through the
pipeline every command uses and check, from the rows and the dates stream:

* an end follows the dwell of its row's state: with two states of disjoint bins, every end lies in
  its state's bins, and a state with no dwell, as a NULL state, takes ``otherwise`` or keeps the
  end its own cell drew;
* an exponential's mean offset is near its mean, its open share near ``open``, and each end is the
  start plus the draw of ``uniform53`` at position 1 of the end's dates stream, in integers;
* the state is the row's final value of ``given``, and a ``given`` the end decides is ``FLX-6208``;
* with a period the dwell end is cut at the parent's end, and with a target the violating rows
  come from the candidates;
* the column path holds the rows of the per-row path, ``--jobs 1`` and ``--jobs 4`` give the same
  digests, and ``plan``, ``apply`` and ``verify`` run on such a bundle and pass.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.cli import main
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.expr2.errors import Expr2Error
from flux_engine.generator import columns
from flux_engine.generator.dates import days_of_date
from flux_engine.generator.dwell import ExponentialDwell, draw_dwell
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.rows import clear_memos, generate_rows
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify as run_verify
from tests.fixtures.temporal.bundle import copy_uncut

pytestmark = pytest.mark.integration

_SEG = "{ levels: { a: '0.4', b: '0.4', c: '0.2' }, nullFraction: '0.1' }"
SHORT = "{ bins: ['0', '10'], binWeights: ['1'] }"
LONG = "{ bins: ['500', '510'], binWeights: ['1'] }"
TWO_STATES = f"{{ given: seg, states: {{ a: {SHORT}, b: {LONG} }} }}"


def _lifetime(duration: str, extra: str = "") -> str:
    return (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end, "
        f"{extra}duration: {duration} }}\n"
    )


def _bundle(
    root: Path,
    keys: str,
    *,
    rules: str = "",
    customers: int = 400,
    account_seg: bool = False,
    name: str = "bundle",
) -> Path:
    """The temporal fixture with a hidden ``seg`` on the customers (and optionally on the accounts)."""
    bundle = copy_uncut(root, name, keys)
    path = bundle / "simulation.flux.yml"
    text = path.read_text("utf-8")
    hidden = "        hiddenColumns:\n          - { name: seg, type: STRING }\n"
    stat = f"            seg: {_SEG}\n"
    assert text.count("      customer:\n        rows: 60\n") == 1
    text = text.replace(
        "      customer:\n        rows: 60\n",
        f"      customer:\n        rows: {customers}\n{hidden}",
        1,
    )
    text = text.replace("            cu_score: {", stat + "            cu_score: {", 1)
    if account_seg:
        text = text.replace(
            "      account:\n        rows: 150\n", "      account:\n        rows: 150\n" + hidden, 1
        )
        text = text.replace("            a_limit: {", stat + "            a_limit: {", 1)
    if rules:
        text = text.replace(
            "      account:\n        rows: ", rules + "      account:\n        rows: ", 1
        )
    path.write_text(text, "utf-8")
    return bundle


def _outcome(result: pipeline.PipelineResult, expose_id: str) -> pipeline.EmitOutcome:
    return next(e for e in result.emits if e.resolved.expose_id == expose_id)


def _rows(
    result: pipeline.PipelineResult, expose_id: str, *, rules: bool = True
) -> List[Dict[str, Any]]:
    """The rows with the hidden columns, as the plan generates them; ``rules=False`` gives the cells."""
    planned = _outcome(result, expose_id).plan
    assert planned is not None
    if not rules:
        planned = replace(planned, rules=None)
    rows = generate_rows(planned, root=result.root)
    return [dict(zip(planned.expose.column_names, row, strict=True)) for row in rows]


def _offset(row: Dict[str, Any], start: str = "cu_start", end: str = "cu_end") -> int:
    return (row[end] - row[start]).days


@pytest.fixture(scope="module")
def two_states(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    bundle = _bundle(tmp_path_factory.mktemp("dwell"), _lifetime(TWO_STATES))
    return pipeline.execute(bundle, kernel=KERNEL_V2)


# ---------------------------------------------------------------------------
# A family of histograms
# ---------------------------------------------------------------------------


def test_an_end_lies_in_the_bins_of_its_rows_state(two_states: pipeline.PipelineResult) -> None:
    written = _rows(two_states, "customer")
    cells = _rows(two_states, "customer", rules=False)
    seen = {"a": 0, "b": 0, "other": 0}
    for row, cell in zip(written, cells, strict=True):
        assert row["seg"] == cell["seg"]
        if cell["cu_end"] is None or row["cu_end"] is None:
            continue  # the end's own NULL stands, and an end past the window is no end
        if row["seg"] == "a":
            assert 0 <= _offset(row) <= 10
            seen["a"] += 1
        elif row["seg"] == "b":
            assert 500 <= _offset(row) <= 510
            seen["b"] += 1
        else:
            # No dwell for this state and no otherwise: the row keeps the end its own cell drew.
            assert row["cu_end"] == cell["cu_end"]
            seen["other"] += 1
    assert min(seen.values()) > 5


def test_a_null_and_an_unlisted_state_take_otherwise(tmp_path: Path) -> None:
    other = "{ bins: ['1000', '1010'], binWeights: ['1'] }"
    keys = _lifetime(f"{{ given: seg, states: {{ a: {SHORT} }}, otherwise: {other} }}")
    result = pipeline.execute(_bundle(tmp_path, keys), kernel=KERNEL_V2)
    states: Dict[Optional[str], int] = {}
    for row, cell in zip(
        _rows(result, "customer"), _rows(result, "customer", rules=False), strict=True
    ):
        if cell["cu_end"] is None or row["cu_end"] is None:
            continue
        states[row["seg"]] = states.get(row["seg"], 0) + 1
        low, high = (0, 10) if row["seg"] == "a" else (1000, 1010)
        assert low <= _offset(row) <= high
    assert set(states) == {"a", "b", "c", None}


def test_a_dwell_for_every_row_is_the_same_dwell(tmp_path: Path) -> None:
    keys = _lifetime(f"{{ given: seg, otherwise: {SHORT} }}")
    result = pipeline.execute(_bundle(tmp_path, keys), kernel=KERNEL_V2)
    ended = [r for r in _rows(result, "customer") if r["cu_end"] is not None]
    assert ended and all(0 <= _offset(r) <= 10 for r in ended)


# ---------------------------------------------------------------------------
# An exponential
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def exponential(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    bundle = _bundle(
        tmp_path_factory.mktemp("exponential"),
        _lifetime("{ mean: '20', open: '0.25' }"),
        customers=1500,
    )
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def test_an_exponentials_mean_offset_and_open_share_are_near_its_own(
    exponential: pipeline.PipelineResult,
) -> None:
    written = _rows(exponential, "customer")
    cells = _rows(exponential, "customer", rules=False)
    drawn = [(row, cell) for row, cell in zip(written, cells, strict=True) if cell["cu_end"]]
    assert len(drawn) > 400
    ended = [row for row, _ in drawn if row["cu_end"] is not None]
    open_share = 1 - len(ended) / len(drawn)
    assert abs(open_share - 0.25) < 0.07
    mean = sum(_offset(row) for row in ended) / len(ended)
    assert 16 <= mean <= 24
    assert all(_offset(row) >= 0 for row in ended)


def test_each_exponential_end_is_the_start_plus_the_draw_of_its_stream(
    exponential: pipeline.PipelineResult,
) -> None:
    planned = _outcome(exponential, "customer").plan
    assert planned is not None
    streams = CellStreams(exponential.root, planned.sim_id, "customer")
    dwell = ExponentialDwell(mean=Fraction(20), open=Fraction(1, 4))
    for index, (row, cell) in enumerate(
        zip(
            _rows(exponential, "customer"), _rows(exponential, "customer", rules=False), strict=True
        )
    ):
        if cell["cu_end"] is None:
            assert row["cu_end"] is None
            continue
        units = draw_dwell(dwell, streams.dates_stream_at(index, "cu_end", 1).uniform53(), 1)
        if units is None:
            assert row["cu_end"] is None
        else:
            assert days_of_date(row["cu_end"]) == days_of_date(row["cu_start"]) + units


# ---------------------------------------------------------------------------
# The state is the row's final value of ``given``
# ---------------------------------------------------------------------------

_SET_SEG = (
    "        rules:\n"
    "          - id: seg_set\n"
    "            language: flux-expr/3\n"
    "            derive: seg\n"
    "            expr: \"'a' || (cu_score - cu_score)\"\n"
)


def test_the_state_read_is_the_final_value_when_a_rule_sets_given(tmp_path: Path) -> None:
    keys = _lifetime(f"{{ given: seg, states: {{ a0: {SHORT}, a: {LONG}, b: {LONG} }} }}")
    result = pipeline.execute(_bundle(tmp_path, keys, rules=_SET_SEG), kernel=KERNEL_V2)
    rows = _rows(result, "customer")
    assert {row["seg"] for row in rows} == {"a0"}  # the rule overrides every cell's draw
    ended = [row for row in rows if row["cu_end"] is not None]
    assert ended and all(0 <= _offset(row) <= 10 for row in ended)


def test_a_given_the_end_decides_is_a_cycle(tmp_path: Path) -> None:
    rules = (
        "        rules:\n"
        "          - id: seg_set\n"
        "            language: flux-expr/3\n"
        "            derive: seg\n"
        "            expr: \"'a' || EXTRACT(DAY FROM cu_end)\"\n"
    )
    with pytest.raises(Expr2Error) as caught:
        pipeline.execute(_bundle(tmp_path, _lifetime(TWO_STATES), rules=rules), kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-6208"


# ---------------------------------------------------------------------------
# A period, a target
# ---------------------------------------------------------------------------


def test_with_a_period_the_dwell_end_is_cut_at_the_parents_end(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n"
        "  - { emit: customer, start: cu_start, end: cu_end, duration: "
        "{ bins: ['0', '60'], binWeights: ['1'] } }\n"
        "  - { emit: account, start: a_open, end: a_close, duration: "
        "{ given: seg, states: { a: { bins: ['400', '800'], binWeights: ['1'] }, "
        "b: { mean: '300' } }, otherwise: { bins: ['0', '30'], binWeights: ['1'] } } }\n"
        "within:\n"
        "  - { emit: account, relationship: customer, lifetime: true }\n"
    )
    result = pipeline.execute(_bundle(tmp_path, keys, account_seg=True), kernel=KERNEL_V2)
    customers = {row["cu_id"]: row for row in _rows(result, "customer")}
    cut = 0
    for row in _rows(result, "account"):
        parent = customers[row["a_customer"]]
        if row["a_close"] is None or parent["cu_end"] is None:
            continue
        assert row["a_close"] <= parent["cu_end"]
        cut += row["a_close"] == parent["cu_end"]
    assert cut > 0


def test_with_a_target_the_violating_rows_come_from_the_candidates(tmp_path: Path) -> None:
    keys = _lifetime(TWO_STATES, "target: '20', ")
    result = pipeline.execute(_bundle(tmp_path, keys, customers=600), kernel=KERNEL_V2)
    cells = {row["cu_id"]: row for row in _rows(result, "customer", rules=False)}
    violating = holding = 0
    for row in _rows(result, "customer"):
        if row["cu_end"] is None or cells[row["cu_id"]]["cu_end"] is None:
            continue
        if row["cu_end"] < row["cu_start"]:
            violating += 1
            # A candidate is a draw of the column's own statistics: a date of its declared range.
            assert date(2000, 1, 1) <= row["cu_end"] <= date(2100, 12, 31)
        elif row["seg"] in ("a", "b"):
            holding += 1
            low, high = (0, 10) if row["seg"] == "a" else (500, 510)
            assert low <= _offset(row) <= high
    assert violating > 0 and holding > violating


# ---------------------------------------------------------------------------
# The column path, jobs, and plan, apply and verify
# ---------------------------------------------------------------------------


def test_the_column_path_gives_the_rows_of_the_per_row_path(
    two_states: pipeline.PipelineResult, monkeypatch: pytest.MonkeyPatch
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
            planned = [e.plan for e in two_states.emits if e.plan is not None]
            outputs[name] = repr([generate_rows(p, root=two_states.root) for p in planned])
    assert any(taken)
    assert outputs["columns"] == outputs["rows"]


def test_a_bundle_with_a_plain_histogram_keeps_its_plan_document(tmp_path: Path) -> None:
    keys = _lifetime("{ bins: ['0', '30'], binWeights: ['1'] }")
    result = pipeline.execute(_bundle(tmp_path, keys), kernel=KERNEL_V2)
    listed = _outcome(result, "customer").plan.as_document()["rules"]["rules"][0]["lifetime"]  # type: ignore[union-attr]
    assert listed["duration"] == {"bins": ["0", "30"], "binWeights": ["1"]}
    family = pipeline.execute(
        _bundle(tmp_path, _lifetime(TWO_STATES), name="family"), kernel=KERNEL_V2
    )
    shaped = _outcome(family, "customer").plan.as_document()["rules"]["rules"][0]["lifetime"]  # type: ignore[union-attr]
    assert shaped["duration"]["given"] == "seg"
    assert [state["state"] for state in shaped["duration"]["states"]] == ["a", "b"]


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
    rows = 2 * parallel.MIN_ROWS_PER_JOB + 301
    keys = _lifetime(
        f"{{ given: seg, states: {{ a: {SHORT}, b: {{ mean: '40', open: '0.1' }} }}, "
        f"otherwise: {LONG} }}"
    )
    bundle = _bundle(tmp_path, keys, customers=rows)
    runs = [_loop(bundle, tmp_path / f"state-{jobs}", jobs) for jobs in (1, 4)]
    assert runs[0]["plan"] == runs[1]["plan"]
    assert runs[0]["digests"] == runs[1]["digests"]
    lines = runs[0]["layout"].emit_path("customer").read_text("utf-8").splitlines()
    assert len(lines) == rows
    assert any(json.loads(line)[2] is not None for line in lines)


@pytest.mark.slow
def test_the_command_line_plans_applies_and_verifies_a_bundle_with_a_dwell_family(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = _lifetime(f"{{ given: seg, states: {{ a: {SHORT}, b: {LONG} }}, otherwise: {SHORT} }}")
    bundle = _bundle(tmp_path, keys)
    state = str(tmp_path / ".flux")
    assert main(["-j", "plan", str(bundle), "--state-dir", state, "--kernel", "0.2.0"]) == 0
    run_id = json.loads(capsys.readouterr().out)["artifacts"]["run_id"]
    assert main(["-j", "apply", str(bundle), "--state-dir", state, "--run-id", run_id]) == 0
    capsys.readouterr()
    layout = RunLayout.for_state_dir(state, run_id)
    for line in layout.emit_path("customer").read_text("utf-8").splitlines():
        _, start, end, _ = json.loads(line)
        assert end is None or end >= start
    verify_args = ["-j", "verify", str(bundle), "--state-dir", state, "--run-id", run_id]
    assert main([*verify_args, "--allow-declared-baseline-drift"]) == 0
    assert json.loads(capsys.readouterr().out)["stage"] == "verify"
