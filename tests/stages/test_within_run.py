# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose dates are within their linked rows' lifetimes (ADR 0024 section 5).

``tests/fixtures/temporal`` is copied with ``lifetimes`` and ``within`` blocks: the chain customer,
account, invoice and payment. An account opens within its customer's lifetime (with an ``offset``),
an invoice is issued within its account's lifetime and within its customer's (the chain named
outright), and a payment is paid after its invoice (a lifetime with a start and no end, with an
``offset``). The tests run the bundle through the pipeline every command uses, recompute ``J`` for
every row from its parents' final rows with the rule of the ADR written out here in dates, and then
run ``plan``, ``apply`` and ``verify``:

* every covered date lies in its parent's lifetime cut as ``J`` says, and is the value the dates
  stream and the cut histogram or the ``bounded`` draw give;
* a row whose parent has no start keeps the value its own cell drew;
* the refusals: a period with no end, a target, an interval with no upper end, a chain with no tie, two sources;
* the column path of kernel 0.2.0 holds the rows the per-row path holds;
* ``--jobs 1`` and ``--jobs 4`` give the same digests;
* kernel 0.1.0 refuses the keys, and a bundle without them keeps its plan.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from flux_engine.cli import main
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator import columns
from flux_engine.generator.dates import cut_table, offset_table
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.rows import clear_memos, generate_rows
from flux_engine.seam.dates import Histogram
from flux_engine.seam.resolve import load_bundle, resolve_all_emits
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify as run_verify
from tests.fixtures.temporal.bundle import CHAIN, copy_uncut

pytestmark = pytest.mark.integration

DAY_MS = 86_400_000
EPOCH = date(1970, 1, 1)
OFFSET_ACCOUNT = Histogram(bins=("0", "90", "365", "900"), bin_weights=("5", "3", "1"))
OFFSET_PAYMENT = Histogram(bins=("0", "3", "30"), bin_weights=("4", "1"))
KEYS = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, duration: { bins: ['0', '100', '400', '900'], binWeights: ['5', '3', '1'], open: '2' } }
  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
  - { emit: invoice, start: i_issued }
within:
  - { emit: account, relationship: customer, date: a_open, offset: { bins: ['0', '90', '365', '900'], binWeights: ['5', '3', '1'] } }
  - { emit: invoice, relationship: account, date: i_issued }
  - { emit: invoice, relationship: account.customer, date: i_issued }
  - { emit: payment, relationship: invoice, date: p_paid, offset: { bins: ['0', '3', '30'], binWeights: ['4', '1'] } }
  - { emit: payment, relationship: invoice.account, date: p_paid }
"""
#: The customers' lifetime starts at their end, which is NULL for most of them: their accounts are not covered.
NULL_START_KEYS = """\
lifetimes:
  - { emit: customer, start: cu_end }
within:
  - { emit: account, relationship: customer, date: a_open }
"""
#: An offset no bin of which reaches a day the interval holds: every row falls back to uniform.
FALLBACK_KEYS = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end }
within:
  - { emit: account, relationship: customer, date: a_open, offset: { bins: ['5000', '5100'], binWeights: ['1'] } }
"""
PLAIN_WITHIN = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end }
within:
  - { emit: account, relationship: customer, date: a_open }
"""


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return copy_uncut(tmp_path_factory.mktemp("within"), "bundle", KEYS)


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
# The rule of the ADR, written out here in dates and milliseconds
# ---------------------------------------------------------------------------

Span = Tuple[int, Optional[int]]


def _day(value: date) -> int:
    return (value - EPOCH).days


def _span(start: Any, end: Any, unit: str, kind: str) -> Optional[Span]:
    """A parent's closed lifetime in a column's units, ``None`` without a start; open past a missing or crossed end.

    *unit* is the parent's grain and *kind* the column's: a DATE start bounds a TIMESTAMP from its
    midnight and a DATE end up to the last millisecond of its day; a TIMESTAMP bounds a DATE by its day.
    """
    if start is None:
        return None
    lo = _day(start) if unit == "DATE" else int(start)
    hi = None if end is None else (_day(end) if unit == "DATE" else int(end))
    if hi is not None and hi < lo:
        hi = None
    if unit == kind:
        return lo, hi
    if unit == "DATE":
        return lo * DAY_MS, None if hi is None else (hi + 1) * DAY_MS - 1
    return lo // DAY_MS, None if hi is None else hi // DAY_MS


def _joint(spans: List[Span]) -> Span:
    lo = max(s for s, _ in spans)
    ends = [e for _, e in spans if e is not None]
    return lo, (min(ends) if ends else None)


def _cut(span: Span, window: Optional[Span]) -> Span:
    """``J``: the part of the lifetime inside the window; else the whole lifetime if it ended before; else its start."""
    lo, hi = span
    assert window is not None
    first, last = window
    if hi is not None and hi < first:
        return span
    if lo > last:
        return lo, lo
    return max(lo, first), last if hi is None else min(hi, last)


WINDOW_DAYS = (_day(date(2025, 1, 1)), _day(date(2025, 12, 31)))
WINDOW_MS = (1_735_689_600_000, 1_767_225_599_000)


# ---------------------------------------------------------------------------
# What the rows hold
# ---------------------------------------------------------------------------


def test_every_account_opens_within_its_customers_lifetime_cut_at_the_window(
    result: pipeline.PipelineResult,
) -> None:
    customers = {row["cu_id"]: row for row in _rows(result, "customer")}
    seen = 0
    for account in _rows(result, "account"):
        parent = customers[account["a_customer"]]
        lifetime = _span(parent["cu_start"], parent["cu_end"], "DATE", "DATE")
        assert lifetime is not None
        lo, hi = _cut(lifetime, WINDOW_DAYS)
        assert hi is not None and lo <= _day(account["a_open"]) <= hi
        seen += 1
    assert seen == 150


def test_an_account_open_is_the_value_the_dates_stream_and_the_cut_histogram_give(
    result: pipeline.PipelineResult,
) -> None:
    planned = _outcome(result, "account").plan
    assert planned is not None
    streams = CellStreams(result.root, planned.sim_id, "account")
    customers = {row["cu_id"]: row for row in _rows(result, "customer")}
    table = offset_table(OFFSET_ACCOUNT)
    for index, account in enumerate(_rows(result, "account")):
        parent = customers[account["a_customer"]]
        lifetime = _span(parent["cu_start"], parent["cu_end"], "DATE", "DATE")
        assert lifetime is not None
        interval = _cut(lifetime, WINDOW_DAYS)
        cut = cut_table(table, lifetime[0], interval, 1)
        if cut is None:  # no bin keeps a value inside: uniform over J, one bounded
            n = interval[1] - interval[0] + 1  # type: ignore[operator]
            expected = interval[0] + streams.dates_stream_at(index, "a_open", 1).bounded(n)
        else:
            expected = cut.value(streams.dates_stream_at(index, "a_open", 1).uniform53())
        assert _day(account["a_open"]) == expected


def test_an_invoice_is_issued_within_its_account_and_its_customer_through_the_chain(
    result: pipeline.PipelineResult,
) -> None:
    customers = {row["cu_id"]: row for row in _rows(result, "customer")}
    accounts = {row["a_id"]: row for row in _rows(result, "account")}
    planned = _outcome(result, "invoice").plan
    assert planned is not None
    streams = CellStreams(result.root, planned.sim_id, "invoice")
    for index, invoice in enumerate(_rows(result, "invoice")):
        account = accounts[invoice["i_account"]]
        customer = customers[account["a_customer"]]
        near = _span(account["a_open"], account["a_close"], "DATE", "TIMESTAMP")
        far = _span(customer["cu_start"], customer["cu_end"], "DATE", "TIMESTAMP")
        assert near is not None and far is not None
        interval = _cut(_joint([near, far]), WINDOW_MS)
        assert interval[1] is not None and interval[0] <= invoice["i_issued"] <= interval[1]
        # The account opens within its customer's lifetime, so the levels meet at its start.
        assert far[0] <= near[0]
        n = interval[1] - interval[0] + 1
        expected = interval[0] + streams.dates_stream_at(index, "i_issued", 1).bounded(n)
        assert invoice["i_issued"] == expected


def test_a_payment_is_paid_after_its_invoice_and_inside_its_invoices_and_its_accounts_lifetimes(
    result: pipeline.PipelineResult,
) -> None:
    invoices = {row["i_id"]: row for row in _rows(result, "invoice")}
    accounts = {row["a_id"]: row for row in _rows(result, "account")}
    planned = _outcome(result, "payment").plan
    assert planned is not None
    streams = CellStreams(result.root, planned.sim_id, "payment")
    table = offset_table(OFFSET_PAYMENT)
    for index, payment in enumerate(_rows(result, "payment")):
        invoice = invoices[payment["p_invoice"]]
        lifetime = _span(invoice["i_issued"], None, "TIMESTAMP", "TIMESTAMP")
        account = accounts[invoice["i_account"]]
        near = _span(account["a_open"], account["a_close"], "DATE", "TIMESTAMP")
        assert lifetime is not None and near is not None
        interval = _cut(_joint([lifetime, near]), WINDOW_MS)
        assert payment["p_paid"] >= invoice["i_issued"]
        assert interval[1] is not None and interval[0] <= payment["p_paid"] <= interval[1]
        cut = cut_table(table, lifetime[0], interval, DAY_MS)
        if cut is None:
            n = interval[1] - interval[0] + 1
            expected = interval[0] + streams.dates_stream_at(index, "p_paid", 1).bounded(n)
        else:
            expected = cut.value(streams.dates_stream_at(index, "p_paid", 1).uniform53())
        assert payment["p_paid"] == expected


def test_a_row_whose_parent_has_no_start_keeps_the_value_its_own_cell_drew(
    tmp_path: Path,
) -> None:
    outcome = pipeline.execute(copy_uncut(tmp_path, "nullstart", NULL_START_KEYS), kernel=KERNEL_V2)
    customers = {row["cu_id"]: row for row in _rows(outcome, "customer")}
    cells = _cells(outcome, "account")
    kept = moved = 0
    for account, cell in zip(_rows(outcome, "account"), cells, strict=True):
        parent = customers[account["a_customer"]]
        if parent["cu_end"] is None:
            assert account["a_open"] == cell["a_open"]
            kept += 1
            continue
        lo, hi = _cut((_day(parent["cu_end"]), None), WINDOW_DAYS)
        assert hi is not None and lo <= _day(account["a_open"]) <= hi
        moved += 1
    assert kept > 20 and moved > 20


def test_an_offset_that_no_interval_holds_falls_back_to_uniform_on_every_row(
    tmp_path: Path,
) -> None:
    outcome = pipeline.execute(copy_uncut(tmp_path, "fallback", FALLBACK_KEYS), kernel=KERNEL_V2)
    customers = {row["cu_id"]: row for row in _rows(outcome, "customer")}
    planned = _outcome(outcome, "account").plan
    assert planned is not None
    streams = CellStreams(outcome.root, planned.sim_id, "account")
    for index, account in enumerate(_rows(outcome, "account")):
        parent = customers[account["a_customer"]]
        lifetime = _span(parent["cu_start"], parent["cu_end"], "DATE", "DATE")
        assert lifetime is not None
        lo, hi = _cut(lifetime, WINDOW_DAYS)
        assert hi is not None
        expected = lo + streams.dates_stream_at(index, "a_open", 1).bounded(hi - lo + 1)
        assert _day(account["a_open"]) == expected


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def test_the_plan_lists_the_step_only_for_an_emit_that_declares_an_entry(
    result: pipeline.PipelineResult, tmp_path: Path
) -> None:
    documents = {e.resolved.expose_id: e.plan for e in result.emits}
    expected = {
        "customer": ["lifetime"],  # its duration, and no within entry
        "account": ["lifetime", "within"],
        "invoice": ["within", "within"],
        "payment": ["within", "within"],
    }
    for expose_id, forms in expected.items():
        plan_ = documents[expose_id]
        assert plan_ is not None
        listed = plan_.as_document()["rules"]["rules"]
        assert [rule["form"] for rule in listed] == forms
    invoice = documents["invoice"]
    assert invoice is not None
    assert [r["id"] for r in invoice.as_document()["rules"]["rules"]] == [
        "within:account.i_issued",
        "within:account.customer.i_issued",
    ]
    # The same lifetimes with no within entry add no step to the invoices and payments, and the
    # generatorDigest moves for the emits that declare one.
    lifetimes_only = KEYS.split("within:")[0]
    plain = pipeline.execute(copy_uncut(tmp_path, "plain", lifetimes_only), kernel=KERNEL_V2)
    plain_plans = {e.resolved.expose_id: e.plan for e in plain.emits}
    for expose_id in ("invoice", "payment"):
        assert plain_plans[expose_id] is not None and plain_plans[expose_id].rules is None  # type: ignore[union-attr]
    plain_digests = {e.resolved.expose_id: e.plan.digest().value for e in plain.emits if e.plan}
    digests = {e.resolved.expose_id: e.plan.digest().value for e in result.emits if e.plan}
    assert digests["customer"] == plain_digests["customer"]
    for expose_id in ("account", "invoice", "payment"):
        assert digests[expose_id] != plain_digests[expose_id]


def test_the_coverage_names_the_column_a_within_entry_draws(
    result: pipeline.PipelineResult,
) -> None:
    rows = {row.column: row for row in _outcome(result, "account").coverage.columns}
    assert rows["a_open"].decided_by == "rule"
    assert rows["a_open"].source == "bundle.within.a_open"
    assert rows["a_close"].source == "bundle.lifetimes.a_close"
    assert rows["a_limit"].decided_by == "fidelity"
    invoice = {row.column: row for row in _outcome(result, "invoice").coverage.columns}
    assert invoice["i_issued"].decided_by == "rule"
    assert invoice["i_issued"].source == "bundle.within.i_issued"


def test_the_plan_reads_each_parent_through_its_path(result: pipeline.PipelineResult) -> None:
    invoice = _outcome(result, "invoice").plan
    assert invoice is not None and invoice.rules is not None
    assert sorted(rule.paths for rule in invoice.rules.rules) == [
        (("account",),),
        (("account", "customer"),),
    ]
    assert {expose_id for expose_id, _ in invoice.reads} == {"account", "customer"}


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _planned(bundle: Path, tmp_path: Path) -> Any:
    return plan(bundle, state_dir=tmp_path / ".flux", kernel="0.2.0")


def test_a_period_whose_emit_has_a_lifetime_with_no_end_is_refused(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end }\n"
        "  - { emit: account, start: a_open }\n"
        "within:\n  - { emit: account, relationship: customer, lifetime: true }\n"
    )
    planned = _planned(copy_uncut(tmp_path, "period", keys), tmp_path)
    assert not planned.success and planned.error_code == "FLX-3105"
    assert "lifetime: true" in planned.stderr and "declares no end" in planned.stderr


def test_an_entry_with_a_target_plans(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end }\n"
        "within:\n  - { emit: account, relationship: customer, date: a_open, target: '3' }\n"
    )
    planned = _planned(copy_uncut(tmp_path, "target", keys), tmp_path)
    assert planned.success, planned.stderr


def _plan_account_rules(bundle: Path, *, windowed: bool) -> Any:
    """The account's rules as ``plan`` plans them, for a run that does or does not declare time bounds.

    A whole run always declares ``spec.timeBounds`` (``FLX-5023``), so the refusal is the planner's:
    :func:`~flux_engine.stages.pipeline.plan_emit_rules` is what ``plan`` and ``validate`` call.
    """
    resolved = resolve_all_emits(load_bundle(bundle), simulation_id="sim-billing-dates")
    account = next(e for e in resolved if e.expose_id == "account")
    return pipeline.plan_emit_rules(
        account, strict=False, where="sim", counter_based=True, emits=resolved, windowed=windowed
    )


def test_without_time_bounds_a_date_with_no_offset_is_refused(tmp_path: Path) -> None:
    bundle = copy_uncut(tmp_path, "open", PLAIN_WITHIN)
    assert _plan_account_rules(bundle, windowed=True) is not None
    with pytest.raises(FluxError) as caught:
        _plan_account_rules(bundle, windowed=False)
    assert caught.value.code.code == "FLX-6205" and "a_open" in str(caught.value)


def test_without_time_bounds_an_offset_with_no_weight_from_day_zero_up_is_refused(
    tmp_path: Path,
) -> None:
    keys = PLAIN_WITHIN.replace(
        "date: a_open }", "date: a_open, offset: { bins: ['-30', '-1'], binWeights: ['1'] } }"
    )
    with pytest.raises(FluxError) as caught:
        _plan_account_rules(copy_uncut(tmp_path, "behind", keys), windowed=False)
    assert caught.value.code.code == "FLX-6205"


def test_without_time_bounds_an_offset_that_reaches_forward_is_planned(tmp_path: Path) -> None:
    keys = PLAIN_WITHIN.replace(
        "date: a_open }",
        "date: a_open, offset: { bins: ['0', '30', '90'], binWeights: ['2', '1'] } }",
    )
    bundle = copy_uncut(tmp_path, "forward", keys)
    assert _plan_account_rules(bundle, windowed=False) is not None
    # The rows are drawn over the open interval from the cut histogram alone.
    outcome = pipeline.execute(bundle, kernel=KERNEL_V2)
    planned = _outcome(outcome, "account").plan
    assert planned is not None
    customers = {row["cu_id"]: row for row in _rows(outcome, "customer")}
    for row, account in zip(
        generate_rows(replace(planned, time_bounds=None), root=outcome.root),
        _rows(outcome, "account"),
        strict=True,
    ):
        parent = customers[account["a_customer"]]
        lifetime = _span(parent["cu_start"], parent["cu_end"], "DATE", "DATE")
        assert lifetime is not None
        opened = _day(row[planned.expose.column_names.index("a_open")])
        assert lifetime[0] <= opened <= (lifetime[0] + 90 if lifetime[1] is None else lifetime[1])


def test_a_row_with_an_open_interval_and_no_offset_inside_it_is_refused_at_generation(
    tmp_path: Path,
) -> None:
    outcome = pipeline.execute(copy_uncut(tmp_path, "nowindow", PLAIN_WITHIN), kernel=KERNEL_V2)
    planned = _outcome(outcome, "account").plan
    assert planned is not None
    clear_memos()
    with pytest.raises(FluxError) as caught:
        generate_rows(replace(planned, time_bounds=None), root=outcome.root)
    assert caught.value.code.code == "FLX-6205" and "no upper end" in str(caught.value)


def test_a_chain_of_three_levels_with_a_date_tie_deeper_down_is_refused(tmp_path: Path) -> None:
    """A date tie on the account's start keeps the payment after the customer's start, not before its end."""
    keys = KEYS + "  - { emit: payment, relationship: invoice.account.customer, date: p_paid }\n"
    planned = _planned(copy_uncut(tmp_path, "three", keys), tmp_path)
    assert not planned.success and planned.error_code == "FLX-6204"
    assert "'account' needs a period (lifetime: true)" in planned.stderr


def test_entries_along_a_chain_whose_middle_expose_does_not_tie_its_start_are_refused(
    tmp_path: Path,
) -> None:
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end }\n"
        "  - { emit: account, start: a_open, end: a_close }\n"
        "within:\n  - { emit: invoice, relationship: account, date: i_issued }\n"
        "  - { emit: invoice, relationship: account.customer, date: i_issued }\n"
    )
    planned = _planned(copy_uncut(tmp_path, "untied", keys), tmp_path)
    assert not planned.success and planned.error_code == "FLX-6204"
    assert "'account' needs a within entry for its start" in planned.stderr


def test_a_within_entry_on_a_column_a_rule_also_sets_is_refused(tmp_path: Path) -> None:
    bundle = copy_uncut(tmp_path, "two", PLAIN_WITHIN)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    simulation.write_text(
        text.replace(
            "      account:\n        rows: 150\n",
            "      account:\n        rows: 150\n        rules:\n"
            "          - id: open_copy\n            language: flux-expr/3\n"
            "            derive: a_open\n            expr: ADD_DAYS(a_close, 0)\n",
            1,
        ),
        "utf-8",
    )
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-6203"


def test_a_within_entry_on_a_column_a_duration_also_sets_is_refused(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end }\n"
        "  - { emit: account, start: a_open, end: a_close, duration: { bins: ['0', '90'], binWeights: ['1'] } }\n"
        "within:\n  - { emit: account, relationship: customer, date: a_close, target: '5' }\n"
    )
    with pytest.raises(FluxError) as caught:
        pipeline.execute(copy_uncut(tmp_path, "duration", keys), kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-6203"


def test_a_duration_may_start_at_a_date_a_within_entry_sets(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end }\n"
        "  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, "
        "duration: { bins: ['0', '90'], binWeights: ['1'] } }\n"
        "within:\n  - { emit: account, relationship: customer, date: a_open }\n"
    )
    outcome = pipeline.execute(copy_uncut(tmp_path, "ordered", keys), kernel=KERNEL_V2)
    ended = 0
    for account in _rows(outcome, "account"):
        if account["a_close"] is not None:
            assert account["a_open"] <= account["a_close"]
            ended += 1
    assert ended > 0
    plan_ = _outcome(outcome, "account").plan
    assert plan_ is not None and plan_.rules is not None
    assert plan_.rules.order.index("a_open") < plan_.rules.order.index("a_close")


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
            planned = [_outcome(result, e).plan for e in ("account", "invoice", "payment")]
            outputs[name] = repr(
                [generate_rows(p, root=result.root) for p in planned if p is not None]
            )
    assert any(taken)
    assert outputs["columns"] == outputs["rows"]


def test_kernel_0_1_0_refuses_the_keys_naming_the_kernel(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105"
    assert "kernel 0.1.0" in str(caught.value)


def test_planning_a_within_entry_for_kernel_0_1_0_names_the_kernel(bundle: Path) -> None:
    resolved = resolve_all_emits(load_bundle(bundle), simulation_id="sim-billing-dates")
    account = next(e for e in resolved if e.expose_id == "account")
    with pytest.raises(FluxError) as caught:
        pipeline.plan_emit_rules(
            account, strict=False, where="sim", counter_based=False, emits=resolved
        )
    assert caught.value.code.code == "FLX-3105"
    assert "kernel 0.1.0" in str(caught.value) and "within" in str(caught.value)


def test_a_bundle_without_the_keys_is_the_bundle_it_was(tmp_path: Path) -> None:
    for name, kernel in (("v1", KERNEL_V1), ("v2", KERNEL_V2)):
        plain = pipeline.execute(copy_uncut(tmp_path, f"plain-{name}"), kernel=kernel)
        assert all(e.plan is not None and e.plan.rules is None for e in plain.emits)
        assert all("rules" not in e.plan.as_document() for e in plain.emits if e.plan)


def test_the_lifetimes_alone_leave_every_emit_without_a_rule(tmp_path: Path) -> None:
    plain = pipeline.execute(
        copy_uncut(tmp_path, "chain", CHAIN.split("within:")[0]), kernel=KERNEL_V2
    )
    assert all(e.plan is not None and e.plan.rules is None for e in plain.emits)


# ---------------------------------------------------------------------------
# plan, apply and verify
# ---------------------------------------------------------------------------


def _big(tmp_path: Path) -> Path:
    """The bundle with enough accounts and invoices for ``--jobs 4`` to cut them into slices."""
    big = copy_uncut(tmp_path, "big", KEYS)
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
    for expose_id in ("account", "invoice"):
        first = runs[0]["layout"].emit_path(expose_id).read_text("utf-8")
        assert len(first.splitlines()) == 2 * parallel.MIN_ROWS_PER_JOB + 301
        assert first == runs[1]["layout"].emit_path(expose_id).read_text("utf-8")


@pytest.mark.slow
def test_the_command_line_plans_applies_and_verifies_a_bundle_with_within_entries(
    bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = str(tmp_path / ".flux")
    assert main(["-j", "plan", str(bundle), "--state-dir", state, "--kernel", "0.2.0"]) == 0
    planned = json.loads(capsys.readouterr().out)
    run_id = planned["artifacts"]["run_id"]
    assert main(["-j", "apply", str(bundle), "--state-dir", state, "--run-id", run_id]) == 0
    capsys.readouterr()
    layout = RunLayout.for_state_dir(state, run_id)
    invoices = {}
    for line in layout.emit_path("invoice").read_text("utf-8").splitlines():
        invoice_id, _account, issued, _amount = json.loads(line)
        invoices[invoice_id] = issued
    paid = [
        json.loads(line) for line in layout.emit_path("payment").read_text("utf-8").splitlines()
    ]
    assert paid and all(row[2] >= invoices[row[1]] for row in paid)
    verify_args = ["-j", "verify", str(bundle), "--state-dir", state, "--run-id", run_id]
    assert main([*verify_args, "--allow-declared-baseline-drift"]) == 0
    assert json.loads(capsys.readouterr().out)["stage"] == "verify"
    assert main(["-j", "plan", str(bundle), "--state-dir", state, "--kernel", "0.1.0"]) == 1
    assert "FLX-3105" in capsys.readouterr().err
