# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose date facts carry targets: the rows that violate them are generated (ADR 0024 section 7).

``tests/fixtures/temporal`` is copied with ``lifetimes`` and ``within`` blocks whose entries carry a
``target``. The tests run the bundle through the pipeline every command uses and check, from the rows,
that:

* the share of rows that violate each fact is its target, within four binomial standard deviations;
* a violating row lies outside its fact's interval and inside the other entries';
* a period's violation is on its start, and its end is not cut at the parent's end;
* a column whose own statistics never leave the interval, and the range fallback, are counted as
  ``notViolated``;
* a targeted tie on a middle expose needs a target on the outer entry (``FLX-6204``), and the rows
  whose levels do not meet are ``infeasible``;
* the receipt and the evidence pack carry the counts, a target of 0 or none leaves the rows and the
  receipt as they were, and every TIMESTAMP value a step sets is held to the milliseconds a TIMESTAMP holds.
"""

from __future__ import annotations

import json
import math
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from flux_engine.cli import main
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator import columns
from flux_engine.generator import rows as generator_rows
from flux_engine.generator import rules as generator_rules
from flux_engine.generator.dates import TIMESTAMP_MAX_MS, DatesTally
from flux_engine.generator.rows import clear_memos, generate_rows
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.ship import _injected_violations_text
from flux_engine.stages.verify import verify as run_verify
from tests.fixtures.temporal.bundle import UNCUT_CUSTOMER_END, copy_uncut
from tests.stages.test_period_run import _mixed

pytestmark = pytest.mark.integration

EPOCH = date(1970, 1, 1)

#: A target on a customer's lifetime, on the account's open date and on the invoice's issue date.
TARGET_KEYS = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, duration: { bins: ['0', '100', '400'], binWeights: ['5', '3'] }, target: '30' }
  - { emit: account, start: a_open, end: a_close, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
  - { emit: invoice, start: i_issued }
within:
  - { emit: account, relationship: customer, date: a_open, target: '20' }
  - { emit: invoice, relationship: account, date: i_issued, target: '30' }
"""
#: An account that is a period within its customer, 25 percent of them starting outside it.
PERIOD_KEYS = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, duration: { bins: ['0', '200', '900'], binWeights: ['3', '1'] } }
  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
within:
  - { emit: account, relationship: customer, lifetime: true, target: '25' }
"""
#: A middle tie with a target and an outer entry with one.
MIDDLE_KEYS = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, duration: { bins: ['0', '200', '900'], binWeights: ['3', '1'] } }
  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
  - { emit: invoice, start: i_issued }
within:
  - { emit: account, relationship: customer, lifetime: true, target: '30' }
  - { emit: invoice, relationship: account, date: i_issued }
  - { emit: invoice, relationship: "account.customer", date: i_issued, target: '20' }
"""
ZERO_KEYS = (
    TARGET_KEYS.replace("target: '30'", "target: '0'")
    .replace("target: '20'", "target: '0'")
    .replace("target: '30'", "target: '0'")
)
NONE_KEYS = ZERO_KEYS.replace(", target: '0'", "")


def _run(bundle: Path) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def _outcome(run: pipeline.PipelineResult, expose_id: str) -> pipeline.EmitOutcome:
    return next(e for e in run.emits if e.resolved.expose_id == expose_id)


def _rows(run: pipeline.PipelineResult, expose_id: str) -> List[Dict[str, Any]]:
    emitted = _outcome(run, expose_id)
    return [dict(zip(emitted.expose.column_names, row, strict=True)) for row in emitted.rows]


def _day(value: Optional[date]) -> Optional[int]:
    return None if value is None else (value - EPOCH).days


def _counts(run: pipeline.PipelineResult, expose_id: str) -> Dict[str, Dict[str, int]]:
    tally = _outcome(run, expose_id).dates
    assert tally is not None
    return tally.as_document()


def _covered(found: Dict[str, int]) -> int:
    return found["held"] + found["drawn"] + found["infeasible"] + found["notViolated"]


def _within_four_sd(count: int, total: int, target: Decimal) -> bool:
    share = float(target) / 100
    return abs(count - total * share) <= 4 * math.sqrt(total * share * (1 - share))


@pytest.fixture(scope="module")
def target_run(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    return _run(copy_uncut(tmp_path_factory.mktemp("target"), "bundle", TARGET_KEYS))


@pytest.fixture(scope="module")
def period_run(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    return _run(copy_uncut(tmp_path_factory.mktemp("period"), "bundle", PERIOD_KEYS))


@pytest.fixture(scope="module")
def middle_run(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    return _run(copy_uncut(tmp_path_factory.mktemp("middle"), "bundle", MIDDLE_KEYS))


# ---------------------------------------------------------------------------
# The shares and the rows that violate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expose_id", "fact", "target"),
    [
        ("customer", "lifetime:cu_start.cu_end", "30"),
        ("account", "within:customer.a_open", "20"),
        ("invoice", "within:account.i_issued", "30"),
    ],
)
def test_the_share_of_rows_that_draw_a_violation_is_the_target_within_four_deviations(
    target_run: pipeline.PipelineResult, expose_id: str, fact: str, target: str
) -> None:
    found = _counts(target_run, expose_id)[fact]
    asked = found["drawn"] + found["notViolated"] + found["infeasible"]
    assert _within_four_sd(asked, _covered(found), Decimal(target))


def test_a_lifetime_that_violates_ends_before_it_starts(
    target_run: pipeline.PipelineResult,
) -> None:
    rows = _rows(target_run, "customer")
    crossed = [
        r for r in rows if r["cu_end"] is not None and _day(r["cu_end"]) < _day(r["cu_start"])
    ]
    found = _counts(target_run, "customer")["lifetime:cu_start.cu_end"]
    assert len(crossed) == found["drawn"] > 0
    assert found["held"] + found["drawn"] + found["notViolated"] == sum(
        r["cu_end"] is not None for r in rows
    )


def _lifetimes(
    rows: List[Dict[str, Any]], key: str, start: str, end: str
) -> Dict[Any, Tuple[int, Optional[int]]]:
    found: Dict[Any, Tuple[int, Optional[int]]] = {}
    for row in rows:
        low, high = _day(row[start]), _day(row[end])
        if low is None:
            continue
        found[row[key]] = (low, None if high is None or high < low else high)
    return found


def test_a_date_that_violates_lies_outside_its_parents_lifetime(
    target_run: pipeline.PipelineResult,
) -> None:
    parents = _lifetimes(_rows(target_run, "customer"), "cu_id", "cu_start", "cu_end")
    outside = 0
    for row in _rows(target_run, "account"):
        low, high = parents[row["a_customer"]]
        opened = _day(row["a_open"])
        assert opened is not None
        if opened < low or (high is not None and opened > high):
            outside += 1
    assert outside == _counts(target_run, "account")["within:customer.a_open"]["drawn"] > 0


def test_a_chain_with_an_enforced_inner_entry_keeps_violating_dates_inside_it(
    tmp_path: Path,
) -> None:
    keys = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end }
  - { emit: account, start: a_open, end: a_close, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
  - { emit: invoice, start: i_issued }
within:
  - { emit: account, relationship: customer, lifetime: true }
  - { emit: invoice, relationship: account, date: i_issued }
  - { emit: invoice, relationship: "account.customer", date: i_issued, target: '40' }
"""
    run = _run(copy_uncut(tmp_path, "inner", keys))
    found = _counts(run, "invoice")
    # the account lies inside its customer, so no date is outside the customer and inside the account
    outer = found["within:account.customer.i_issued"]
    assert outer["drawn"] == 0 and outer["infeasible"] == 0 and outer["notViolated"] > 0
    accounts = _lifetimes(_rows(run, "account"), "a_id", "a_open", "a_close")
    for row in _rows(run, "invoice"):
        issued = row["i_issued"]
        low, high = accounts[row["i_account"]]
        assert issued is not None
        millis = int(issued)
        assert millis >= low * 86_400_000
        if high is not None:
            assert millis < (high + 1) * 86_400_000


def test_a_period_that_violates_starts_outside_its_parent_and_its_end_is_not_cut(
    period_run: pipeline.PipelineResult,
) -> None:
    parents = _lifetimes(_rows(period_run, "customer"), "cu_id", "cu_start", "cu_end")
    leaving = uncut = 0
    for row in _rows(period_run, "account"):
        low, high = parents[row["a_customer"]]
        opened = _day(row["a_open"])
        assert opened is not None
        if opened < low or (high is not None and opened > high):
            leaving += 1
            closed = _day(row["a_close"])
            if high is not None and closed is not None and closed > high:
                uncut += 1
        elif high is not None:
            closed = _day(row["a_close"])
            assert closed is not None and closed <= high  # the rows that hold are cut as before
    found = _counts(period_run, "account")["within:customer.lifetime"]
    assert leaving == found["drawn"] > 0
    assert uncut > 0


def test_a_target_of_zero_draws_nothing_and_every_row_holds(tmp_path: Path) -> None:
    zero = _run(copy_uncut(tmp_path, "zero", ZERO_KEYS))
    for expose_id in ("customer", "account", "invoice"):
        assert _outcome(zero, expose_id).dates is None
    for row in _rows(zero, "customer"):
        assert row["cu_end"] is None or _day(row["cu_end"]) >= _day(row["cu_start"])
    parents = _lifetimes(_rows(zero, "customer"), "cu_id", "cu_start", "cu_end")
    for row in _rows(zero, "account"):
        low, high = parents[row["a_customer"]]
        assert _day(row["a_open"]) >= low and (high is None or _day(row["a_open"]) <= high)


# ---------------------------------------------------------------------------
# Candidates and the range fallback
# ---------------------------------------------------------------------------


def test_a_column_whose_statistics_never_leave_the_interval_is_not_violated(
    tmp_path: Path,
) -> None:
    # The durations reach the end range of 2030 and 2031 from the starts, so that the end is cut
    # inside the range (ADR 0024 section 4) and no candidate of the range lies before a start.
    keys = TARGET_KEYS.replace(
        "end: cu_end, duration: { bins: ['0', '100', '400'], binWeights: ['5', '3'] }",
        "end: cu_end, endPastWindow: kept, duration: { bins: ['1800', '3000'], binWeights: ['1'] }",
    )
    assert keys != TARGET_KEYS
    bundle = copy_uncut(tmp_path, "never", keys)
    path = bundle / "simulation.flux.yml"
    text = path.read_text("utf-8")
    old = UNCUT_CUSTOMER_END
    assert old in text
    path.write_text(
        text.replace(
            old, "cu_end: { nullFraction: '0.6', range: { min: '2030-01-01', max: '2031-12-31' } }"
        ),
        "utf-8",
    )
    run = _run(bundle)
    found = _counts(run, "customer")["lifetime:cu_start.cu_end"]
    assert found["drawn"] == 0 and found["notViolated"] > 0
    for row in _rows(run, "customer"):
        assert row["cu_end"] is None or _day(row["cu_end"]) >= _day(row["cu_start"])


def test_the_candidates_then_the_ends_of_the_declared_range_are_tried_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drawn: List[int] = []
    positions: List[int] = []

    class Streams:
        def dates_stream_at(self, row: int, column: str, position: int) -> int:
            positions.append(position)
            return position

    context = SimpleNamespace(
        plan=SimpleNamespace(sim_id="s", expose=SimpleNamespace(expose_id="e")),
        reader=lambda: SimpleNamespace(streams=lambda *_: Streams()),
    )
    statistics = SimpleNamespace(range_min=Decimal(5), range_max=Decimal(900))
    step = SimpleNamespace(
        column="c", plan=SimpleNamespace(family=generator_rows.Family.RATE, statistics=statistics)
    )

    def candidate(plan: Any, stream: int, *, skip_null: bool) -> date:
        assert skip_null
        drawn.append(stream)
        return date(1970, 1, 11)  # day 10

    monkeypatch.setattr(generator_rows, "_cell_on_stream", candidate)
    kind = "DATE"
    assert generator_rules._violating_value(step, context, 0, kind, lambda c: c == 900) == 900
    assert drawn == [257 + j for j in range(16)] and positions == drawn  # m is 1 for a rate
    assert generator_rules._violating_value(step, context, 0, kind, lambda c: c == 5) == 5
    assert generator_rules._violating_value(step, context, 0, kind, lambda c: c == 10) == 10
    assert generator_rules._violating_value(step, context, 0, kind, lambda c: False) is None


def test_a_candidate_takes_as_many_positions_as_the_columns_draw_makes() -> None:
    from flux_engine.core.seedtree import derive_root  # noqa: F401  (the streams below need a root)

    calls = {"uniform": 1, "rate": 1, "histogram": 2}
    for name, expected in calls.items():
        plan = SimpleNamespace(
            family=(
                generator_rows.Family.RATE
                if name == "rate"
                else generator_rows.Family.UNIFORM_EMPIRICAL
            ),
            statistics=SimpleNamespace(bins=(1, 2) if name == "histogram" else None),
        )
        assert generator_rows.candidate_calls(plan) == expected  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Chains
# ---------------------------------------------------------------------------


def test_a_middle_target_with_an_enforced_outer_entry_is_refused(tmp_path: Path) -> None:
    keys = MIDDLE_KEYS.replace(", target: '20'", "")
    planned = plan(
        copy_uncut(tmp_path, "enforced", keys), state_dir=tmp_path / ".s", kernel="0.2.0"
    )
    assert not planned.success and planned.error_code == "FLX-6204"
    assert "must carry a target" in planned.stderr


def test_a_middle_target_with_a_targeted_outer_entry_gives_infeasible_rows(
    middle_run: pipeline.PipelineResult,
) -> None:
    found = _counts(middle_run, "invoice")
    outer = found["within:account.customer.i_issued"]
    assert outer["infeasible"] > 0
    assert found["within:account.i_issued"]["infeasible"] == 0


def test_two_targets_on_one_column_are_refused(tmp_path: Path) -> None:
    keys = MIDDLE_KEYS.replace("date: i_issued }", "date: i_issued, target: '5' }", 1)
    planned = plan(copy_uncut(tmp_path, "two", keys), state_dir=tmp_path / ".s", kernel="0.2.0")
    assert not planned.success and planned.error_code == "FLX-3105"
    assert "more than one target" in planned.stderr


# ---------------------------------------------------------------------------
# The receipt, the evidence and the digests
# ---------------------------------------------------------------------------


def test_the_receipt_counts_each_fact_of_an_emit_with_a_target(tmp_path: Path) -> None:
    bundle = copy_uncut(tmp_path, "receipt", TARGET_KEYS)
    state = tmp_path / ".flux"
    planned = plan(bundle, state_dir=state, kernel="0.2.0")
    assert planned.success, planned.stderr
    applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]))
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(planned.artifacts["run_id"]))
    receipt = json.loads(layout.receipt_path.read_text("utf-8"))
    dates = receipt["emits"]["invoice"]["dates"]
    assert set(dates) == {"within:account.i_issued"}
    assert set(dates["within:account.i_issued"]) == {
        "held",
        "drawn",
        "infeasible",
        "notViolated",
        "offsetFallbacks",
    }
    assert "dates" not in receipt["emits"]["payment"]
    text = _injected_violations_text(receipt)
    assert "`invoice.within:account.i_issued`" in text and "drawn" in text
    assert _injected_violations_text({"emits": {"payment": {}}}) == ""


def test_a_bundle_without_targets_keeps_its_receipt_free_of_dates(tmp_path: Path) -> None:
    for name, keys in (("zero", ZERO_KEYS), ("none", NONE_KEYS)):
        bundle = copy_uncut(tmp_path, name, keys)
        state = tmp_path / f"state-{name}"
        planned = plan(bundle, state_dir=state, kernel="0.2.0")
        applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]))
        assert applied.success, applied.stderr
        layout = RunLayout.for_state_dir(state, str(planned.artifacts["run_id"]))
        receipt = json.loads(layout.receipt_path.read_text("utf-8"))
        assert all("dates" not in entry for entry in receipt["emits"].values())


def test_the_column_path_gives_the_rows_of_the_per_row_path(
    target_run: pipeline.PipelineResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: List[bool] = []
    column_rows = columns.column_rows

    def spy(*args: Any) -> Any:
        computed = column_rows(*args)
        taken.append(computed is not None)
        return computed

    outputs: Dict[str, Any] = {}
    for name, patched in (("columns", spy), ("rows", lambda *args: None)):
        clear_memos()
        with monkeypatch.context() as patch:
            patch.setattr(columns, "column_rows", patched)
            outputs[name] = []
            for expose_id in ("customer", "account", "invoice"):
                tally = DatesTally(_outcome(target_run, expose_id).plan.rules.date_facts())  # type: ignore[union-attr]
                rows = generate_rows(
                    _outcome(target_run, expose_id).plan, root=target_run.root, tally=tally  # type: ignore[arg-type]
                )
                outputs[name].append((rows, tally.as_document()))
    assert any(taken)
    assert outputs["columns"] == outputs["rows"]


# ---------------------------------------------------------------------------
# --jobs and the command line
# ---------------------------------------------------------------------------


def _big(tmp_path: Path) -> Path:
    big = copy_uncut(tmp_path, "big", TARGET_KEYS)
    path = big / "simulation.flux.yml"
    text = path.read_text("utf-8")
    rows = 2 * parallel.MIN_ROWS_PER_JOB + 301
    for old in ("rows: 150\n", "rows: 900\n"):
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
        bundle, run_id=layout.run_id, state_dir=state, allow_declared_baseline_drift=True, jobs=jobs
    )
    assert verified.success, verified.stderr
    return {
        "plan": planned.artifacts.get("plan_digest"),
        "digests": {
            k: v for k, v in applied.artifacts.items() if "digest" in k.lower() and k != "run_id"
        },
        "receipt": json.loads(layout.receipt_path.read_text("utf-8")),
    }


@pytest.mark.slow
def test_jobs_one_and_four_give_the_same_digests_and_counts(tmp_path: Path) -> None:
    bundle = _big(tmp_path)
    runs = [_loop(bundle, tmp_path / f"state-{jobs}", jobs) for jobs in (1, 4)]
    assert runs[0]["plan"] == runs[1]["plan"]
    assert runs[0]["digests"] == runs[1]["digests"]
    for expose_id in ("account", "invoice"):
        first = runs[0]["receipt"]["emits"][expose_id]
        assert first["dates"] == runs[1]["receipt"]["emits"][expose_id]["dates"]
        assert first["outputDigest"] == runs[1]["receipt"]["emits"][expose_id]["outputDigest"]


@pytest.mark.slow
def test_the_command_line_plans_applies_and_verifies_a_run_with_targets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = copy_uncut(tmp_path, "cli", TARGET_KEYS)
    state = str(tmp_path / ".flux")
    assert main(["-j", "plan", str(bundle), "--state-dir", state, "--kernel", "0.2.0"]) == 0
    run_id = json.loads(capsys.readouterr().out)["artifacts"]["run_id"]
    assert main(["-j", "apply", str(bundle), "--state-dir", state, "--run-id", run_id]) == 0
    capsys.readouterr()
    verify_args = ["-j", "verify", str(bundle), "--state-dir", state, "--run-id", run_id]
    assert main([*verify_args, "--allow-declared-baseline-drift"]) == 0
    assert json.loads(capsys.readouterr().out)["stage"] == "verify"


# ---------------------------------------------------------------------------
# A TIMESTAMP value is held to the milliseconds a TIMESTAMP holds (FLX-6112)
# ---------------------------------------------------------------------------

#: The most whole days a duration bin may name: 7 digits, which a TIMESTAMP cannot hold from any start.
HUGE = "9999999"


def _refused(bundle: Path) -> FluxError:
    with pytest.raises(FluxError) as caught:
        _run(bundle)
    assert caught.value.code.code == "FLX-6112"
    assert HUGE not in str(caught.value) and "TIMESTAMP" in str(caught.value)
    return caught.value


def test_a_duration_that_ends_past_the_timestamps_a_column_holds_is_refused(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: invoice, start: i_issued, end: i_closed, endPastWindow: kept, "
        f"duration: {{ bins: ['0', '{HUGE}'], binWeights: ['1'] }} }}\n"
    )
    error = _refused(_mixed(tmp_path, "duration", keys))
    assert "i_closed" in str(error) and "row" in str(error)


def test_a_period_that_ends_past_the_timestamps_a_column_holds_is_refused(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: account, start: a_open }\n"
        "  - { emit: invoice, start: i_issued, end: i_closed, endPastWindow: kept, "
        f"duration: {{ bins: ['0', '{HUGE}'], binWeights: ['1'] }} }}\n"
        "within:\n  - { emit: invoice, relationship: account, lifetime: true }\n"
    )
    _refused(_mixed(tmp_path, "period", keys))


def test_a_within_date_and_a_violation_past_the_timestamps_a_column_holds_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = (
        "lifetimes:\n  - { emit: account, start: a_open }\n"
        "within:\n  - { emit: invoice, relationship: account, date: i_issued, target: '50' }\n"
    )
    bundle = copy_uncut(tmp_path, "within", keys)
    monkeypatch.setattr(generator_rules, "draw_within", lambda *args: (TIMESTAMP_MAX_MS + 1, False))
    monkeypatch.setattr(generator_rules, "_holds", lambda *args: True)
    _refused(bundle)
    monkeypatch.setattr(generator_rules, "_holds", lambda *args: False)
    monkeypatch.setattr(generator_rules, "_violating_value", lambda *args: -(10**30))
    _refused(bundle)
