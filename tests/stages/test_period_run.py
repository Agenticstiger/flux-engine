# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose lifetimes are periods within their linked rows' lifetimes (ADR 0024 section 5).

``tests/fixtures/temporal`` is copied with ``lifetimes`` and ``within`` blocks in which an emit's
lifetime is ``lifetime: true`` within another's: an account within its customer, an invoice within
its account, a payment within its invoice. The tests run the bundle through the pipeline every
command uses and recompute the rule of the ADR here, written out in integers:

* the start of a covered row lies in its parent's lifetime cut at the window;
* the end is drawn as the lifetime draws it, cut at the parent's end where the parent has ended,
  and then follows ``endPastWindow`` only where no parent has ended;
* a parent with no end, a NULL end or an end before its start cuts nothing, and a row no entry
  covers keeps the end its lifetime alone gives it;
* chains of periods hold at every level, with DATE and TIMESTAMP columns mixed;
* the plan lists the end's step only for an emit that has a period;
* the column path holds the rows of the per-row path, ``--jobs 1`` and ``--jobs 4`` give the same
  digests, and a bundle with no period gives the rows it gave before periods generated;
* ``plan``, ``apply`` and ``verify`` run on a bundle with a chain of periods and pass.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from flux_engine.cli import main
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator import columns
from flux_engine.generator.dates import (
    DAY_MS,
    Period,
    bound_in_units,
    cut_end,
    draw_end,
    duration_table,
)
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.rows import clear_memos, generate_rows
from flux_engine.seam.dates import Histogram
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages._module_checks import module_checks
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify as run_verify
from tests.fixtures.temporal.bundle import copy_uncut

pytestmark = pytest.mark.integration

EPOCH = date(1970, 1, 1)
WINDOW_DAYS = ((date(2025, 1, 1) - EPOCH).days, (date(2025, 12, 31) - EPOCH).days)
ACCOUNT_DURATION = Histogram(bins=("0", "300", "900"), bin_weights=("2", "1"))
OFFSET = "offset: { bins: ['0', '90', '365', '900'], binWeights: ['5', '3', '1'] }"

#: Customers whose end is the cell's own value, so some end before they start and 60 percent have
#: none; accounts that end by a duration and are periods within them, ``open`` past the window.
OPEN_KEYS = f"""\
lifetimes:
  - {{ emit: customer, start: cu_start, end: cu_end }}
  - {{ emit: account, start: a_open, end: a_close, duration: {{ bins: ['0', '300', '900'], binWeights: ['2', '1'] }} }}
  - {{ emit: invoice, start: i_issued }}
within:
  - {{ emit: account, relationship: customer, lifetime: true, {OFFSET} }}
  - {{ emit: invoice, relationship: account, date: i_issued }}
"""
#: The same with customers that end by a duration, kept past the window, and accounts kept too.
KEPT_KEYS = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, duration: { bins: ['0', '200', '900'], binWeights: ['3', '1'] } }
  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
  - { emit: invoice, start: i_issued }
within:
  - { emit: account, relationship: customer, lifetime: true }
  - { emit: invoice, relationship: account, date: i_issued }
"""
#: Customers ended by a duration kept past the window, so a parent can end after it; accounts ``open``.
ACTIVE_KEYS = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, duration: { bins: ['0', '200', '1500'], binWeights: ['1', '3'] } }
  - { emit: account, start: a_open, end: a_close, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
  - { emit: invoice, start: i_issued }
within:
  - { emit: account, relationship: customer, lifetime: true }
  - { emit: invoice, relationship: account, date: i_issued }
"""
#: Customers whose start is their end, NULL for most of them: their accounts are not covered.
UNCOVERED_KEYS = """\
lifetimes:
  - { emit: customer, start: cu_end, end: cu_start }
  - { emit: account, start: a_open, end: a_close, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
within:
  - { emit: account, relationship: customer, lifetime: true }
"""
#: A chain of periods with DATE and TIMESTAMP columns mixed: the invoices (TIMESTAMP) are periods
#: within their DATE accounts and customers, and the payments (DATE) within their TIMESTAMP invoices.
MIXED_KEYS = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, duration: { bins: ['0', '200', '900'], binWeights: ['3', '1'] } }
  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
  - { emit: invoice, start: i_issued, end: i_closed, endPastWindow: kept, duration: { bins: ['0', '30', '90'], binWeights: ['2', '1'] } }
  - { emit: payment, start: p_from, end: p_to, duration: { bins: ['0', '10', '40'], binWeights: ['2', '1'] } }
within:
  - { emit: account, relationship: customer, lifetime: true }
  - { emit: invoice, relationship: account, lifetime: true }
  - { emit: invoice, relationship: account.customer, lifetime: true }
  - { emit: payment, relationship: invoice, lifetime: true }
  - { emit: payment, relationship: invoice.account, lifetime: true }
  - { emit: payment, relationship: invoice.account.customer, lifetime: true }
"""
#: The bundle of ``tests/stages/test_within_run.py``, which has no period.
PLAIN_KEYS = """\
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
#: The rows of ``PLAIN_KEYS`` on the fixture of :func:`copy_uncut`, as the generator drew them before periods
#: generated and before the dates steps kept to a column's declared range: the code of the commit before
#: both gives these digests for the same bundle.
PLAIN_DIGESTS = {
    "customer": "66c633b4857bc3fe4934b28430356bd2f778b280c294e5d1b197e0fcad6e76ad",
    "account": "7c3f4a9db70387776e85ba23077f030c74c64d8680dd3dcc84ce33ee2aa2f99f",
    "invoice": "969deb74633d73378111174721f22b20b81d63fb7ffd891e119ed021d97285c1",
    "payment": "5b6e7a9059202893dc3a27aa4d4ef68c229519827e280e127385296a220a3150",
}


#: The contract digest the Simulation declares, and the one of the contract with the added columns.
FIXTURE_DIGEST = "8d0bfac56a029742791402300989494668526814465d3f1e4df5ad1bb4d093bd"
MIXED_DIGEST = "cf2043bec9ba779f46c1a567ad35c41cc34120914fde9d98ef7741c6af85248a"


def _mixed(root: Path, name: str, keys: str) -> Path:
    """The fixture with an end for the invoices (TIMESTAMP) and a DATE lifetime for the payments."""
    bundle = copy_uncut(root, name, keys)
    contract = bundle / "billing.fluid.yml"
    text = contract.read_text("utf-8")
    for old, new in (
        (
            "        - { name: i_amount, type: INT, required: false }\n",
            "        - { name: i_amount, type: INT, required: false }\n"
            "        - { name: i_closed, type: TIMESTAMP, required: false }\n",
        ),
        (
            "        - { name: p_amount, type: INT, required: false }\n",
            "        - { name: p_amount, type: INT, required: false }\n"
            "        - { name: p_from, type: DATE, required: true }\n"
            "        - { name: p_to, type: DATE, required: false }\n",
        ),
    ):
        assert text.count(old) == 1
        text = text.replace(old, new)
    contract.write_text(text, "utf-8")
    simulation = bundle / "simulation.flux.yml"
    simulation.write_text(
        simulation.read_text("utf-8").replace(FIXTURE_DIGEST, MIXED_DIGEST), "utf-8"
    )
    return bundle


def _run(bundle: Path) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


@pytest.fixture(scope="module")
def open_run(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    return _run(copy_uncut(tmp_path_factory.mktemp("open"), "bundle", OPEN_KEYS))


@pytest.fixture(scope="module")
def kept_run(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    return _run(copy_uncut(tmp_path_factory.mktemp("kept"), "bundle", KEPT_KEYS))


@pytest.fixture(scope="module")
def active_run(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    return _run(copy_uncut(tmp_path_factory.mktemp("active"), "bundle", ACTIVE_KEYS))


@pytest.fixture(scope="module")
def uncovered_run(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    return _run(copy_uncut(tmp_path_factory.mktemp("uncovered"), "bundle", UNCOVERED_KEYS))


@pytest.fixture(scope="module")
def mixed_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _mixed(tmp_path_factory.mktemp("mixed"), "bundle", MIXED_KEYS)


@pytest.fixture(scope="module")
def mixed_run(mixed_bundle: Path) -> pipeline.PipelineResult:
    return _run(mixed_bundle)


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
# The rule of the ADR, written out here in integers
# ---------------------------------------------------------------------------

Span = Tuple[int, Optional[int]]


def _unit(value: Any, kind: str) -> Optional[int]:
    """A cell as epoch days (``DATE``) or epoch milliseconds (``TIMESTAMP``)."""
    if value is None:
        return None
    return (value - EPOCH).days if kind == "DATE" else int(value)


def _span(start: Any, end: Any, parent: str, kind: str) -> Optional[Span]:
    """A parent's closed lifetime in a child column's units: ``None`` without a start, open past a NULL or crossed end."""
    low = _unit(start, parent)
    if low is None:
        return None
    high = _unit(end, parent)
    if high is not None and high < low:
        high = None
    if parent == kind:
        return low, high
    if parent == "DATE":
        return low * DAY_MS, None if high is None else (high + 1) * DAY_MS - 1
    return low // DAY_MS, None if high is None else high // DAY_MS


def _expected_end(
    drawn: Optional[int], parent_high: Optional[int], window_high: int, past: str
) -> Optional[int]:
    """The end of the ADR: the drawn end, cut at the parent's end, and then ``endPastWindow`` where no parent ended."""
    if parent_high is not None:
        return parent_high if drawn is None or drawn > parent_high else drawn
    if drawn is not None and drawn > window_high and past == "open":
        return None
    return drawn


def _accounts(
    run: pipeline.PipelineResult,
    past: str,
    parent_columns: Tuple[str, str] = ("cu_start", "cu_end"),
) -> List[Tuple[Dict[str, Any], Optional[Span], Optional[int], Optional[int]]]:
    """Each account with its parent's lifetime, its drawn end and the end the ADR gives it.

    The drawn end is the duration's ``uniform53`` on the account's dates stream, ``None`` for a
    row whose own cell is NULL (NULLs come first).
    """
    planned = _outcome(run, "account").plan
    assert planned is not None
    streams = CellStreams(run.root, planned.sim_id, "account")
    customers = {row["cu_id"]: row for row in _rows(run, "customer")}
    cells = _cells(run, "account")
    table = duration_table(ACCOUNT_DURATION)
    found = []
    for index, account in enumerate(_rows(run, "account")):
        parent = customers[account["a_customer"]]
        lifetime = _span(parent[parent_columns[0]], parent[parent_columns[1]], "DATE", "DATE")
        start = _unit(account["a_open"], "DATE")
        assert start is not None
        drawn = (
            None
            if cells[index]["a_close"] is None
            else draw_end(table, start, streams.dates_stream_at(index, "a_close", 1).uniform53(), 1)
        )
        high = None if lifetime is None else lifetime[1]
        found.append((account, lifetime, drawn, _expected_end(drawn, high, WINDOW_DAYS[1], past)))
    return found


# ---------------------------------------------------------------------------
# What the rows hold
# ---------------------------------------------------------------------------


def test_every_covered_account_opens_within_its_customers_lifetime(
    open_run: pipeline.PipelineResult,
) -> None:
    seen = 0
    for account, lifetime, _, _ in _accounts(open_run, "open"):
        assert lifetime is not None  # every customer has a start
        opened = _unit(account["a_open"], "DATE")
        assert opened is not None and lifetime[0] <= opened
        if lifetime[1] is not None:
            assert opened <= lifetime[1]
        seen += 1
    assert seen == 150


@pytest.mark.parametrize(
    ("name", "past"), [("open_run", "open"), ("kept_run", "kept"), ("active_run", "open")]
)
def test_an_account_ends_as_its_lifetime_draws_it_cut_at_its_customers_end(
    name: str, past: str, request: pytest.FixtureRequest
) -> None:
    run = request.getfixturevalue(name)
    for account, _, _, expected in _accounts(run, past):
        assert _unit(account["a_close"], "DATE") == expected


def test_a_customer_that_has_ended_ends_every_account_not_after_it(
    kept_run: pipeline.PipelineResult,
) -> None:
    customers = {row["cu_id"]: row for row in _rows(kept_run, "customer")}
    ended = 0
    for account in _rows(kept_run, "account"):
        parent = customers[account["a_customer"]]
        if parent["cu_end"] is not None and parent["cu_end"] >= parent["cu_start"]:
            assert account["a_close"] is not None and account["a_close"] <= parent["cu_end"]
            ended += 1
    assert ended > 20


def test_an_open_or_later_drawn_end_becomes_the_parents_end(
    kept_run: pipeline.PipelineResult,
) -> None:
    cut_open = cut_later = 0
    for account, lifetime, drawn, _ in _accounts(kept_run, "kept"):
        if lifetime is None or lifetime[1] is None:
            continue
        if drawn is None:
            assert _unit(account["a_close"], "DATE") == lifetime[1]
            cut_open += 1
        elif drawn > lifetime[1]:
            assert _unit(account["a_close"], "DATE") == lifetime[1]
            cut_later += 1
        else:
            assert _unit(account["a_close"], "DATE") == drawn
    assert cut_open > 0 and cut_later > 0


def test_past_the_window_open_gives_null_only_where_the_parent_is_still_active(
    open_run: pipeline.PipelineResult,
) -> None:
    ended_open = active_nulled = 0
    for account, lifetime, drawn, _ in _accounts(open_run, "open"):
        assert lifetime is not None
        if lifetime[1] is not None:
            assert account["a_close"] is not None  # the parent has ended: no NULL
            ended_open += 1
        elif drawn is not None and drawn > WINDOW_DAYS[1]:
            assert account["a_close"] is None  # the parent is active past the window
            active_nulled += 1
    assert ended_open > 0 and active_nulled > 0


def test_past_the_window_kept_keeps_an_end_where_the_parent_is_still_active(
    kept_run: pipeline.PipelineResult,
) -> None:
    kept = 0
    for account, lifetime, drawn, _ in _accounts(kept_run, "kept"):
        if (lifetime is None or lifetime[1] is None) and drawn is not None:
            assert _unit(account["a_close"], "DATE") == drawn
            kept += drawn > WINDOW_DAYS[1]
    assert kept > 0


def test_a_parent_that_ended_after_the_window_keeps_its_open_child_an_end(
    active_run: pipeline.PipelineResult,
) -> None:
    """The window rule does not null the end of a child whose parent has ended: the fact needs one."""
    after = 0
    customers = {row["cu_id"]: row for row in _rows(active_run, "customer")}
    for account in _rows(active_run, "account"):
        parent = customers[account["a_customer"]]
        end = parent["cu_end"]
        if end is not None and end >= parent["cu_start"] and (end - EPOCH).days > WINDOW_DAYS[1]:
            assert account["a_close"] is not None and account["a_close"] <= end
            after += 1
    assert after > 0


def test_a_parent_with_no_end_a_null_end_or_an_end_before_its_start_cuts_nothing(
    open_run: pipeline.PipelineResult,
) -> None:
    none = crossed = 0
    for _account, lifetime, drawn, expected in _accounts(open_run, "open"):
        assert lifetime is not None
        if lifetime[1] is None:
            assert expected == _expected_end(drawn, None, WINDOW_DAYS[1], "open")
            none += 1
    customers = {row["cu_id"]: row for row in _rows(open_run, "customer")}
    for account, _, drawn, _ in _accounts(open_run, "open"):
        parent = customers[account["a_customer"]]
        if parent["cu_end"] is not None and parent["cu_end"] < parent["cu_start"]:
            # the parent reads as open: the end is the lifetime's, with the window rule
            assert _unit(account["a_close"], "DATE") == _expected_end(
                drawn, None, WINDOW_DAYS[1], "open"
            )
            crossed += 1
    assert none > 0 and crossed > 0


def test_a_row_no_entry_covers_keeps_the_end_its_lifetime_gives_it(
    uncovered_run: pipeline.PipelineResult,
) -> None:
    uncovered = covered = 0
    for account, lifetime, drawn, expected in _accounts(
        uncovered_run, "open", ("cu_end", "cu_start")
    ):
        if lifetime is None:
            assert _unit(account["a_close"], "DATE") == _expected_end(
                drawn, None, WINDOW_DAYS[1], "open"
            )
            uncovered += 1
        else:
            assert _unit(account["a_close"], "DATE") == expected
            covered += 1
    assert uncovered > 50 and covered > 0


# ---------------------------------------------------------------------------
# Chains, with DATE and TIMESTAMP mixed
# ---------------------------------------------------------------------------

#: The parent each emit's foreign key reaches.
PARENTS = {
    "account": ("customer", "a_customer"),
    "invoice": ("account", "i_account"),
    "payment": ("invoice", "p_invoice"),
}
KEYS_OF = {"customer": "cu_id", "account": "a_id", "invoice": "i_id"}
COLUMNS = {
    "customer": ("cu_start", "cu_end", "DATE"),
    "account": ("a_open", "a_close", "DATE"),
    "invoice": ("i_issued", "i_closed", "TIMESTAMP"),
    "payment": ("p_from", "p_to", "DATE"),
}


def _ancestors(
    run: pipeline.PipelineResult, expose_id: str, row: Dict[str, Any]
) -> List[Tuple[str, Dict[str, Any]]]:
    """The rows the chain reaches from *row*, nearest first."""
    tables = {e: {r[KEYS_OF[e]]: r for r in _rows(run, e)} for e in KEYS_OF}
    found: List[Tuple[str, Dict[str, Any]]] = []
    here, current = expose_id, row
    while here in PARENTS:
        parent, foreign = PARENTS[here]
        current = tables[parent][current[foreign]]
        found.append((parent, current))
        here = parent
    return found


def test_a_chain_of_periods_holds_at_every_level_with_dates_and_timestamps_mixed(
    mixed_run: pipeline.PipelineResult,
) -> None:
    checked = {"account": 0, "invoice": 0, "payment": 0}
    cut = {"account": 0, "invoice": 0, "payment": 0}
    for expose_id in ("account", "invoice", "payment"):
        start_column, end_column, kind = COLUMNS[expose_id]
        for row in _rows(mixed_run, expose_id):
            start, end = _unit(row[start_column], kind), _unit(row[end_column], kind)
            assert start is not None
            for parent_id, parent in _ancestors(mixed_run, expose_id, row):
                p_start, p_end, p_kind = COLUMNS[parent_id]
                lifetime = _span(parent[p_start], parent[p_end], p_kind, kind)
                assert lifetime is not None
                assert lifetime[0] <= start  # the start is within every level
                if lifetime[1] is not None:
                    assert start <= lifetime[1]
                    assert end is not None and end <= lifetime[1]  # and the end is cut at it
                    cut[expose_id] += end == lifetime[1]
            checked[expose_id] += 1
    assert checked == {"account": 150, "invoice": 900, "payment": 700}
    assert all(count > 0 for count in cut.values())


def test_a_timestamp_end_under_a_date_parent_is_bound_to_the_last_millisecond_of_its_day(
    mixed_run: pipeline.PipelineResult,
) -> None:
    accounts = {row["a_id"]: row for row in _rows(mixed_run, "account")}
    equal = 0
    for invoice in _rows(mixed_run, "invoice"):
        account = accounts[invoice["i_account"]]
        if account["a_close"] is not None and account["a_close"] >= account["a_open"]:
            last = ((account["a_close"] - EPOCH).days + 1) * DAY_MS - 1
            assert invoice["i_closed"] is not None and invoice["i_closed"] <= last
            equal += invoice["i_closed"] == last
    assert equal > 0


def test_a_date_end_under_a_timestamp_parent_is_bound_to_the_day_that_holds_it(
    mixed_run: pipeline.PipelineResult,
) -> None:
    invoices = {row["i_id"]: row for row in _rows(mixed_run, "invoice")}
    equal = 0
    for payment in _rows(mixed_run, "payment"):
        invoice = invoices[payment["p_invoice"]]
        if invoice["i_closed"] is not None and invoice["i_closed"] >= invoice["i_issued"]:
            day = invoice["i_closed"] // DAY_MS
            assert payment["p_to"] is not None and (payment["p_to"] - EPOCH).days <= day
            equal += (payment["p_to"] - EPOCH).days == day
    assert equal > 0


def test_the_cut_in_a_child_column_s_units() -> None:
    day = 20_000
    assert bound_in_units(day, "DATE", "TIMESTAMP", end=True) == (day + 1) * DAY_MS - 1
    assert bound_in_units(day * DAY_MS + 5, "TIMESTAMP", "DATE", end=True) == day
    assert cut_end(None, [day], None, "open") == day
    assert cut_end(day + 5, [day + 9, day], None, "open") == day
    assert cut_end(day - 5, [day], None, "open") == day - 5
    # No parent has ended: the window rule decides.
    assert cut_end(day + 5, [None], (0, day), "open") is None
    assert cut_end(day + 5, [None], (0, day), "kept") == day + 5
    # A parent that ended after the window keeps a child that would be NULL an end.
    assert cut_end(day + 5, [day + 9], (0, day), "open") == day + 5
    assert cut_end(None, [day + 9], (0, day), "open") == day + 9


# ---------------------------------------------------------------------------
# The plan, the column path and a bundle with no period
# ---------------------------------------------------------------------------


def _period_rules(run: pipeline.PipelineResult, expose_id: str) -> List[Dict[str, Any]]:
    planned = _outcome(run, expose_id).plan
    assert planned is not None
    rules = planned.as_document().get("rules")
    return [] if rules is None else [r for r in rules["rules"] if "period" in r]


def test_the_plan_lists_the_ends_step_only_for_an_emit_with_a_period(
    open_run: pipeline.PipelineResult,
) -> None:
    (period,) = _period_rules(open_run, "account")
    assert period["column"] == "a_close" and period["id"] == "lifetime:a_open.a_close"
    assert period["period"]["levels"][0]["period"] is True
    assert period["lifetime"]["duration"]["binWeights"] == ["2", "1"]
    for other in ("customer", "invoice", "payment"):
        assert _period_rules(open_run, other) == []


def test_the_plan_lists_the_start_as_a_level_of_the_period(
    open_run: pipeline.PipelineResult,
) -> None:
    planned = _outcome(open_run, "account").plan
    assert planned is not None
    levels = [r for r in planned.as_document()["rules"]["rules"] if r["form"] == "within"]
    assert [(r["id"], r["column"]) for r in levels] == [("within:customer.lifetime", "a_open")]


def test_the_coverage_names_the_lifetime_for_the_end(open_run: pipeline.PipelineResult) -> None:
    report = _outcome(open_run, "account").coverage
    assert report is not None
    sources = {entry.column: entry.source for entry in report.columns}
    assert sources["a_close"] == "bundle.lifetimes.a_close"
    assert sources["a_open"] == "bundle.within.a_open"


def test_the_period_class_holds_the_end_and_its_levels(open_run: pipeline.PipelineResult) -> None:
    planned = _outcome(open_run, "account").plan
    assert planned is not None and planned.rules is not None
    (rule,) = [r for r in planned.rules.rules if r.period is not None]
    assert isinstance(rule.period, Period) and rule.period.end_past_window == "open"
    assert rule.paths == (("customer",),) and rule.period.start == "a_open"


def test_the_column_path_gives_the_rows_of_the_per_row_path(
    mixed_run: pipeline.PipelineResult, monkeypatch: pytest.MonkeyPatch
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
            planned = [
                _outcome(mixed_run, e).plan for e in ("customer", "account", "invoice", "payment")
            ]
            outputs[name] = repr(
                [generate_rows(p, root=mixed_run.root) for p in planned if p is not None]
            )
    assert any(taken)
    assert outputs["columns"] == outputs["rows"]


def test_a_bundle_with_no_period_gives_the_rows_it_gave_before_periods_generated(
    tmp_path: Path,
) -> None:
    run = _run(copy_uncut(tmp_path, "plain", PLAIN_KEYS))
    digests = {
        e.resolved.expose_id: hashlib.sha256(repr(e.rows).encode()).hexdigest() for e in run.emits
    }
    assert digests == PLAIN_DIGESTS


def test_a_period_whose_parent_declares_no_lifetime_is_refused(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: account, start: a_open, end: a_close }\n"
        "within:\n  - { emit: account, relationship: customer, lifetime: true }\n"
    )
    planned = plan(copy_uncut(tmp_path, "bare", keys), state_dir=tmp_path / ".flux", kernel="0.2.0")
    assert not planned.success and planned.error_code == "FLX-3105"


def test_a_period_and_a_date_entry_for_its_start_count_together(tmp_path: Path) -> None:
    """Two offsets for the start column are refused, whether they come from a period or a date entry."""
    keys = f"""\
lifetimes:
  - {{ emit: customer, start: cu_start, end: cu_end }}
  - {{ emit: account, start: a_open, end: a_close }}
within:
  - {{ emit: account, relationship: customer, lifetime: true, {OFFSET} }}
  - {{ emit: account, relationship: customer, date: a_open, {OFFSET} }}
"""
    planned = plan(copy_uncut(tmp_path, "two", keys), state_dir=tmp_path / ".flux", kernel="0.2.0")
    assert not planned.success and planned.error_code == "FLX-3105"
    assert "period" in planned.stderr


# ---------------------------------------------------------------------------
# plan, apply and verify
# ---------------------------------------------------------------------------


def _big(tmp_path: Path) -> Path:
    """The bundle with enough accounts and invoices for ``--jobs 4`` to cut them into slices."""
    big = copy_uncut(tmp_path, "big", KEPT_KEYS)
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
def test_the_command_line_plans_applies_and_verifies_a_chain_of_periods(
    mixed_bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = str(tmp_path / ".flux")
    assert main(["-j", "plan", str(mixed_bundle), "--state-dir", state, "--kernel", "0.2.0"]) == 0
    run_id = json.loads(capsys.readouterr().out)["artifacts"]["run_id"]
    assert main(["-j", "apply", str(mixed_bundle), "--state-dir", state, "--run-id", run_id]) == 0
    capsys.readouterr()
    verify_args = ["-j", "verify", str(mixed_bundle), "--state-dir", state, "--run-id", run_id]
    assert main([*verify_args, "--allow-declared-baseline-drift"]) == 0
    assert json.loads(capsys.readouterr().out)["stage"] == "verify"
    layout = RunLayout.for_state_dir(state, run_id)
    receipt = json.loads(layout.receipt_path.read_text("utf-8"))
    document = json.loads(layout.plan_path.read_text("utf-8"))
    checks = module_checks(mixed_bundle, layout, receipt, document, artifacts_ok=True)
    scored = {share.name: share.holds for share in checks.rules}
    for name, total in (
        ("account.within:customer.lifetime", 150),
        ("invoice.within:account.lifetime", 900),
        ("invoice.within:account.customer.lifetime", 900),
        ("payment.within:invoice.lifetime", 700),
        ("payment.within:invoice.account.lifetime", 700),
        ("payment.within:invoice.account.customer.lifetime", 700),
    ):
        assert (scored[name].satisfied, scored[name].total) == (total, total), name


@pytest.mark.slow
def test_verify_makes_the_implied_outer_line_of_a_period_tie_and_it_passes(
    tmp_path: Path,
) -> None:
    bundle = copy_uncut(tmp_path, "implied", KEPT_KEYS)
    state = tmp_path / ".flux"
    planned = plan(bundle, state_dir=state, kernel="0.2.0")
    assert planned.success, planned.stderr
    applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]))
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(planned.artifacts["run_id"]))
    receipt = json.loads(layout.receipt_path.read_text("utf-8"))
    document = json.loads(layout.plan_path.read_text("utf-8"))
    checks = module_checks(bundle, layout, receipt, document, artifacts_ok=True)
    scored = {share.name: share.holds for share in checks.rules}
    implied = scored["invoice.within:account.customer.i_issued"]
    assert implied.total == 900 and implied.satisfied == implied.total
    assert scored["account.within:customer.lifetime"].satisfied == 150
