# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose dates follow seasonal rates (ADR 0025 section 2).

``tests/fixtures/temporal`` is copied with ``lifetimes``, ``within`` and ``rates`` blocks. The tests run
the bundle through the pipeline every command uses and then through ``plan``, ``apply`` and ``verify``:

* the dates follow the factors: counts by weekday and by hour of week of the invoices' issue timestamps,
  by month of the accounts' opening dates, and by weekday of the accounts' end dates, which a duration draws;
* thinning keeps ``within``: every date still lies in its parent's lifetime, a period's end is thinned
  before it is cut at its parent's end, and a NULL proposal (an open end) is accepted at once;
* a column without rates, and a column whose rates give every value the same weight, keep the rows they
  had, and the plan lists the rates only for the emit that declares them;
* the receipt counts the rows that fell back, exactly;
* the column path of kernel 0.2.0 holds the rows the per-row path holds, ``--jobs 1`` and ``--jobs 4``
  give the same digests, and the command line plans, applies and verifies a bundle with rates.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.cli import main
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator import columns
from flux_engine.generator.rows import clear_memos, generate_rows
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify as run_verify
from tests.fixtures.temporal.bundle import copy_uncut

pytestmark = pytest.mark.integration

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
#: Wednesday weighs ten times any other weekday; the hours 09:00 to 16:59 ten times any other hour.
WEDNESDAY = ["1", "1", "10", "1", "1", "1", "1"]
BUSINESS = [("10" if 9 <= hour < 17 else "1") for _ in range(7) for hour in range(24)]
JANUARY = ["8"] + ["1"] * 11
FRIDAY = ["1", "1", "1", "1", "10", "1", "1"]
SUNDAY_ONLY = ["0", "0", "0", "0", "0", "0", "1"]
MONDAY = ["10", "1", "1", "1", "1", "1", "1"]

BASE = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, duration: { bins: ['0', '100', '400', '900'], binWeights: ['5', '3', '1'], open: '2' } }
  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
  - { emit: invoice, start: i_issued }
within:
  - { emit: account, relationship: customer, date: a_open }
  - { emit: invoice, relationship: account, date: i_issued }
"""
RATES = f"""\
rates:
  - {{ emit: customer, column: cu_end, dayOfWeek: {FRIDAY} }}
  - {{ emit: account, column: a_open, month: {JANUARY} }}
  - {{ emit: account, column: a_close, dayOfWeek: {FRIDAY} }}
  - {{ emit: invoice, column: i_issued, dayOfWeek: {WEDNESDAY}, hourOfWeek: {BUSINESS} }}
"""
KEYS = BASE + RATES
#: The accounts are periods within their customers, and both their start and their end follow rates.
PERIOD_BASE = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, duration: { bins: ['0', '200', '900'], binWeights: ['3', '1'] } }
  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
within:
  - { emit: account, relationship: customer, lifetime: true }
"""
PERIOD_KEYS = PERIOD_BASE + f"""\
rates:
  - {{ emit: account, column: a_open, dayOfWeek: {MONDAY} }}
  - {{ emit: account, column: a_close, dayOfWeek: {FRIDAY} }}
"""
#: An account's end is its start or the day after, and only a Sunday is weighed: a row that starts on a
#: Saturday or a Sunday has a proposal on a Sunday, and any other row rejects every proposal and falls
#: back to proposal 0.
FALLBACK_KEYS = f"""\
lifetimes:
  - {{ emit: customer, start: cu_start, end: cu_end }}
  - {{ emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: {{ bins: ['0', '1'], binWeights: ['1'] }} }}
within:
  - {{ emit: account, relationship: customer, date: a_open }}
rates:
  - {{ emit: account, column: a_close, dayOfWeek: {SUNDAY_ONLY} }}
"""
#: Every value weighs the same, so proposal 0 is accepted on every row.
UNIFORM_RATES = f"""\
rates:
  - {{ emit: customer, column: cu_end, dayOfWeek: {["1"] * 7} }}
  - {{ emit: account, column: a_open, month: {["3"] * 12} }}
  - {{ emit: account, column: a_close, dayOfWeek: {["2"] * 7} }}
  - {{ emit: invoice, column: i_issued, hourOfWeek: {["5"] * 168} }}
"""


def _scaled(
    root: Path,
    name: str,
    keys: str,
    *,
    customers: int = 400,
    accounts: int = 1500,
    invoices: int = 6000,
) -> Path:
    """The fixture with *keys* and enough rows for counts by weekday and hour to follow their factors."""
    bundle = copy_uncut(root, name, keys)
    path = bundle / "simulation.flux.yml"
    text = path.read_text("utf-8")
    for old, new in (
        ("rows: 60\n", customers),
        ("rows: 150\n", accounts),
        ("rows: 900\n", invoices),
    ):
        assert text.count(old) == 1
        text = text.replace(old, f"rows: {new}\n")
    path.write_text(text, "utf-8")
    return bundle


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _scaled(tmp_path_factory.mktemp("rates"), "bundle", KEYS)


@pytest.fixture(scope="module")
def result(bundle: Path) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    """The same bundle without its ``rates``: what proposal 0 of every row is."""
    return pipeline.execute(
        _scaled(tmp_path_factory.mktemp("plain"), "plain", BASE), kernel=KERNEL_V2
    )


def _outcome(outcome: pipeline.PipelineResult, expose_id: str) -> pipeline.EmitOutcome:
    return next(e for e in outcome.emits if e.resolved.expose_id == expose_id)


def _rows(outcome: pipeline.PipelineResult, expose_id: str) -> List[Dict[str, Any]]:
    emitted = _outcome(outcome, expose_id)
    return [dict(zip(emitted.expose.column_names, row, strict=True)) for row in emitted.rows]


def _moment(milliseconds: int) -> datetime:
    return EPOCH + timedelta(milliseconds=milliseconds)


def _share(hits: int, total: int) -> float:
    return hits / total


# ---------------------------------------------------------------------------
# The dates follow the factors
# ---------------------------------------------------------------------------


def test_invoices_follow_the_weekday_and_hour_of_week_factors(
    result: pipeline.PipelineResult, plain: pipeline.PipelineResult
) -> None:
    def shares(outcome: pipeline.PipelineResult) -> tuple[float, float]:
        moments = [_moment(row["i_issued"]) for row in _rows(outcome, "invoice")]
        wednesday = sum(moment.weekday() == 2 for moment in moments)
        business = sum(9 <= moment.hour < 17 for moment in moments)
        return _share(wednesday, len(moments)), _share(business, len(moments))

    wednesday, business = shares(result)
    base_wednesday, base_business = shares(plain)
    # ten to one over six others is 10/16 = 0.625 over a long interval; eight of 24 hours at ten to one is 10/12
    assert 0.45 < wednesday < 0.8 and base_wednesday < 0.25
    assert 0.7 < business < 0.95 and base_business < 0.5


def test_account_openings_follow_the_month_factor(
    result: pipeline.PipelineResult, plain: pipeline.PipelineResult
) -> None:
    def january(outcome: pipeline.PipelineResult) -> float:
        opened: List[date] = [row["a_open"] for row in _rows(outcome, "account")]
        return _share(sum(day.month == 1 for day in opened), len(opened))

    assert january(plain) < 0.15  # a twelfth when the draw is uniform over a year or two
    assert january(result) > 0.25


def test_a_duration_end_follows_the_weekday_factor(
    result: pipeline.PipelineResult, plain: pipeline.PipelineResult
) -> None:
    def friday(outcome: pipeline.PipelineResult) -> float:
        ended = [row["a_close"] for row in _rows(outcome, "account") if row["a_close"] is not None]
        return _share(sum(day.weekday() == 4 for day in ended), len(ended))

    assert friday(plain) < 0.25
    assert 0.45 < friday(result) < 0.8


# ---------------------------------------------------------------------------
# Thinning keeps within, and a NULL proposal stands
# ---------------------------------------------------------------------------


def test_every_date_still_lies_in_its_parents_lifetime(result: pipeline.PipelineResult) -> None:
    customers = {row["cu_id"]: row for row in _rows(result, "customer")}
    accounts = {row["a_id"]: row for row in _rows(result, "account")}
    for account in accounts.values():
        parent = customers[account["a_customer"]]
        assert parent["cu_start"] <= account["a_open"]
        if parent["cu_end"] is not None and parent["cu_end"] >= parent["cu_start"]:
            assert account["a_open"] <= parent["cu_end"]
        assert account["a_close"] is None or account["a_close"] >= account["a_open"]
    for invoice in _rows(result, "invoice"):
        parent = accounts[invoice["i_account"]]
        issued = _moment(invoice["i_issued"])
        assert issued.date() >= parent["a_open"]
        assert parent["a_close"] is None or issued.date() <= parent["a_close"]


def _without_rates(outcome: pipeline.PipelineResult, expose_id: str) -> List[Dict[str, Any]]:
    """The rows of *expose_id* with its rates left out of its plan: proposal 0 of every row, on the same streams."""
    planned = _outcome(outcome, expose_id).plan
    assert planned is not None and planned.rules is not None
    stripped = replace(planned, rules=replace(planned.rules, rates=()))
    rows = generate_rows(stripped, root=outcome.root)
    return [dict(zip(planned.expose.column_names, row, strict=True)) for row in rows]


def test_a_row_whose_end_is_open_stays_open_and_a_thinned_row_takes_a_later_proposal(
    result: pipeline.PipelineResult,
) -> None:
    """A NULL proposal is accepted at once, so every row proposal 0 left open stays open."""
    thinned = {row["cu_id"]: row["cu_end"] for row in _rows(result, "customer")}
    proposal_zero = {row["cu_id"]: row["cu_end"] for row in _without_rates(result, "customer")}
    open_rows = [key for key, end in proposal_zero.items() if end is None]
    assert open_rows and all(thinned[key] is None for key in open_rows)
    moved = [key for key, end in proposal_zero.items() if end is not None and thinned[key] != end]
    assert moved  # a rejected proposal 0 gives way to a later one
    assert all(thinned[key] is None or thinned[key] >= date(2023, 1, 1) for key in moved)


def test_a_period_start_and_end_follow_rates_and_the_end_is_cut_at_the_parents(
    tmp_path: Path,
) -> None:
    thinned = pipeline.execute(_scaled(tmp_path, "period", PERIOD_KEYS), kernel=KERNEL_V2)
    untouched = pipeline.execute(_scaled(tmp_path, "period-plain", PERIOD_BASE), kernel=KERNEL_V2)
    customers = {row["cu_id"]: row for row in _rows(thinned, "customer")}
    accounts = _rows(thinned, "account")
    monday = _share(sum(row["a_open"].weekday() == 0 for row in accounts), len(accounts))
    base_monday = _share(
        sum(row["a_open"].weekday() == 0 for row in _rows(untouched, "account")), len(accounts)
    )
    assert monday > 0.35 and base_monday < 0.25
    for row in accounts:
        parent = customers[row["a_customer"]]
        assert parent["cu_start"] <= row["a_open"]
        assert row["a_close"] is None or row["a_open"] <= row["a_close"]
        if parent["cu_end"] is not None:
            assert row["a_close"] <= parent["cu_end"]  # thinned, then cut at the parent's end
    cut = sum(
        row["a_close"] == customers[row["a_customer"]]["cu_end"]
        for row in accounts
        if customers[row["a_customer"]]["cu_end"] is not None
    )
    assert cut > 0
    ended = [row["a_close"] for row in accounts if row["a_close"] is not None]
    friday = _share(sum(end.weekday() == 4 for end in ended), len(ended))
    assert friday > 0.3  # the cut at a parent's end moves some ends off a Friday


# ---------------------------------------------------------------------------
# Rows without rates, the plan and the receipt
# ---------------------------------------------------------------------------


def test_rates_that_weigh_every_value_alike_keep_every_row(tmp_path: Path) -> None:
    uniform = pipeline.execute(_scaled(tmp_path, "uniform", BASE + UNIFORM_RATES), kernel=KERNEL_V2)
    # every proposal 0 is accepted: the rows are the ones the plan gives with its rates left out
    for expose_id in ("customer",):
        assert _rows(uniform, expose_id) == _without_rates(uniform, expose_id)
    plans = {
        e.resolved.expose_id: e.plan.as_document() for e in uniform.emits if e.plan is not None
    }
    assert plans["invoice"]["rules"]["rates"][0]["column"] == "i_issued"


def test_an_emit_without_rates_keeps_its_rules_and_its_receipt(
    tmp_path: Path, plain: pipeline.PipelineResult
) -> None:
    """Rates on the invoice list in the invoice's plan alone, and only its receipt counts dates."""
    only = f"rates:\n  - {{ emit: invoice, column: i_issued, dayOfWeek: {WEDNESDAY} }}\n"
    declared = pipeline.execute(_scaled(tmp_path, "invoice-only", BASE + only), kernel=KERNEL_V2)
    documents = {e.resolved.expose_id: e.plan.as_document() for e in declared.emits if e.plan}
    base = {e.resolved.expose_id: e.plan.as_document() for e in plain.emits if e.plan}
    for expose_id in ("customer", "account", "payment"):
        assert documents[expose_id].get("rules") == base[expose_id].get("rules")
    assert [r["column"] for r in documents["invoice"]["rules"]["rates"]] == ["i_issued"]
    assert "rates" not in base["invoice"]["rules"]
    assert [e.dates is None for e in declared.emits] == [True, True, False, True]


def test_a_bundle_without_rates_declares_none_and_counts_none(
    plain: pipeline.PipelineResult,
) -> None:
    assert all(e.dates is None for e in plain.emits)
    assert all("rates" not in e.plan.as_document().get("rules", {}) for e in plain.emits if e.plan)


def _run_once(bundle: Path, state: Path, *, jobs: int = 1) -> RunLayout:
    planned = plan(bundle, state_dir=state, jobs=jobs, kernel="0.2.0")
    assert planned.success, planned.stderr
    applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
    assert applied.success, applied.stderr
    return RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))


def test_the_receipt_counts_the_rows_that_fell_back_exactly(tmp_path: Path) -> None:
    bundle = _scaled(tmp_path, "fallback", FALLBACK_KEYS, customers=60, accounts=400, invoices=900)
    layout = _run_once(bundle, tmp_path / ".flux")
    receipt = json.loads(layout.receipt_path.read_text("utf-8"))
    counts = receipt["emits"]["account"]["dates"]["lifetime:a_open.a_close"]
    outcome = pipeline.execute(bundle, kernel=KERNEL_V2)
    # a row whose own end is NULL (nullFraction) stands and counts nothing
    accounts = [row for row in _rows(outcome, "account") if row["a_close"] is not None]
    # the end is the start or the day after, and only a Sunday is weighed: a row that starts on a
    # Saturday or a Sunday has a proposal on a Sunday and is accepted, any other row falls back
    stands = [row for row in accounts if row["a_open"].weekday() in (5, 6)]
    fell_back = len(accounts) - len(stands)
    assert 0 < len(stands) < len(accounts)
    assert counts["ratesFallbacks"] == fell_back and counts["held"] == len(accounts)
    assert all(row["a_close"].weekday() == 6 for row in stands)
    assert set(counts) == {
        "held",
        "drawn",
        "infeasible",
        "notViolated",
        "offsetFallbacks",
        "ratesFallbacks",
    }
    assert "dates" not in receipt["emits"]["payment"]


def test_the_dates_count_is_present_for_an_emit_with_rates_and_absent_otherwise(
    bundle: Path, tmp_path: Path
) -> None:
    layout = _run_once(bundle, tmp_path / ".flux")
    receipt = json.loads(layout.receipt_path.read_text("utf-8"))
    assert "dates" in receipt["emits"]["invoice"] and "dates" in receipt["emits"]["account"]
    assert "dates" not in receipt["emits"]["payment"]
    plain = _scaled(tmp_path, "receipt-plain", BASE)
    other = json.loads(_run_once(plain, tmp_path / ".flux-plain").receipt_path.read_text("utf-8"))
    assert all("dates" not in entry for entry in other["emits"].values())


# ---------------------------------------------------------------------------
# The column path, jobs, and plan, apply and verify
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
            planned = [_outcome(result, e).plan for e in ("customer", "account", "invoice")]
            outputs[name] = repr(
                [generate_rows(p, root=result.root) for p in planned if p is not None]
            )
    assert any(taken)
    assert outputs["columns"] == outputs["rows"]


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
        "receipt": json.loads(layout.receipt_path.read_text("utf-8")),
    }


@pytest.mark.slow
def test_jobs_one_and_four_give_the_same_digests(tmp_path: Path) -> None:
    rows = 2 * parallel.MIN_ROWS_PER_JOB + 301
    big = _scaled(tmp_path, "big", KEYS, accounts=rows, invoices=rows)
    runs = [_loop(big, tmp_path / f"state-{jobs}", jobs) for jobs in (1, 4)]
    assert runs[0]["plan"] == runs[1]["plan"]
    assert runs[0]["digests"] == runs[1]["digests"]
    for expose_id in ("account", "invoice"):
        first = runs[0]["layout"].emit_path(expose_id).read_text("utf-8")
        assert len(first.splitlines()) == rows
        assert first == runs[1]["layout"].emit_path(expose_id).read_text("utf-8")
        assert (
            runs[0]["receipt"]["emits"][expose_id]["dates"]
            == runs[1]["receipt"]["emits"][expose_id]["dates"]
        )


@pytest.mark.slow
def test_the_command_line_plans_applies_and_verifies_a_bundle_with_rates(
    bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = str(tmp_path / ".flux")
    assert main(["-j", "plan", str(bundle), "--state-dir", state, "--kernel", "0.2.0"]) == 0
    planned = json.loads(capsys.readouterr().out)
    run_id = planned["artifacts"]["run_id"]
    assert main(["-j", "apply", str(bundle), "--state-dir", state, "--run-id", run_id]) == 0
    capsys.readouterr()
    layout = RunLayout.for_state_dir(state, run_id)
    issued: List[Optional[str]] = [
        json.loads(line)[2] for line in layout.emit_path("invoice").read_text("utf-8").splitlines()
    ]
    wednesday = sum(
        datetime.fromisoformat(value.replace("Z", "+00:00")).weekday() == 2
        for value in issued
        if value is not None
    )
    assert wednesday / len(issued) > 0.45
    verify_args = ["-j", "verify", str(bundle), "--state-dir", state, "--run-id", run_id]
    assert main([*verify_args, "--allow-declared-baseline-drift"]) == 0
    assert json.loads(capsys.readouterr().out)["stage"] == "verify"
    assert main(["-j", "plan", str(bundle), "--state-dir", state, "--kernel", "0.1.0"]) == 1
    refusal = capsys.readouterr().err
    assert "FLX-3105" in refusal and "rates" in refusal
