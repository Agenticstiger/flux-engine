# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The dates steps never write a value outside the column's own declared range (ADR 0024 sections 4 and 5).

``tests/fixtures/temporal`` is copied with ``lifetimes`` and ``within`` blocks, and its columns' declared
ranges are edited in the copy so that each case holds one situation. The tests run the bundle through the
pipeline every command uses, and check:

* a ``within`` date and a period start lie inside the parent's lifetime cut at the window and inside the
  date column's range ``R``, and a row where the two do not meet keeps its cell's value and counts as
  ``infeasible``;
* ``plan`` refuses an enforced ``within`` entry whose declared ranges allow such a row (``FLX-6204``,
  naming both remedies), and accepts it once the range is widened or the entry carries a target;
* a duration's end is cut to the end column's range, a nullable end under ``endPastWindow: open`` has no
  end where no value fits, and ``plan`` refuses the other cases on both sides;
* an exponential dwell is cut exactly at the boundaries of the interval;
* a period's end that its parent's end cuts below the end column's range counts as ``infeasible``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from flux_engine.core.kernel2 import exponential_units
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator.dates import UNIFORM53, Duration, fit_in_bounds
from flux_engine.generator.dwell import (
    ExponentialDwell,
    HistogramDwell,
    _exponential_span,
    _rate_of,
    draw_dwell_within,
)
from flux_engine.generator.rows import generate_rows
from flux_engine.seam.dates import Histogram
from flux_engine.stages import pipeline
from flux_engine.stages.plan import plan
from tests.fixtures.temporal.bundle import copy_bundle

pytestmark = pytest.mark.integration

EPOCH = date(1970, 1, 1)
WINDOW = ((date(2025, 1, 1) - EPOCH).days, (date(2025, 12, 31) - EPOCH).days)

#: The fixture's customer range, and the one these cases give it: every customer starts in the window.
CUSTOMER_START = "cu_start: { range: { min: '2023-01-01', max: '2025-06-30' } }"
IN_WINDOW_START = "cu_start: { range: { min: '2025-01-01', max: '2025-06-30' } }"
CUSTOMER_END = "cu_end: { nullFraction: '0.6', range: { min: '2024-01-01', max: '2025-12-31' } }"
ACCOUNT_OPEN = "a_open: { range: { min: '2023-01-01', max: '2025-09-30' } }"
ACCOUNT_CLOSE = "a_close: { nullFraction: '0.7', range: { min: '2024-01-01', max: '2025-12-31' } }"


def _bundle(root: Path, name: str, keys: str, *edits: Tuple[str, str]) -> Path:
    """The fixture with *keys*, and each ``(old, new)`` pair of its Simulation text replaced once."""
    bundle = copy_bundle(root, name, keys)
    path = bundle / "simulation.flux.yml"
    text = path.read_text("utf-8")
    for old, new in edits:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path.write_text(text, "utf-8")
    return bundle


def _run(bundle: Path) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def _outcome(run: pipeline.PipelineResult, expose_id: str) -> pipeline.EmitOutcome:
    return next(e for e in run.emits if e.resolved.expose_id == expose_id)


def _rows(run: pipeline.PipelineResult, expose_id: str) -> List[Dict[str, Any]]:
    emitted = _outcome(run, expose_id)
    return [dict(zip(emitted.expose.column_names, row, strict=True)) for row in emitted.rows]


def _cells(run: pipeline.PipelineResult, expose_id: str) -> List[Dict[str, Any]]:
    """The rows as their own cells draw them, before any rule sets a column."""
    planned = _outcome(run, expose_id).plan
    assert planned is not None
    rows = generate_rows(replace(planned, rules=None), root=run.root)
    return [dict(zip(planned.expose.column_names, row, strict=True)) for row in rows]


def _day(value: Optional[date]) -> Optional[int]:
    return None if value is None else (value - EPOCH).days


# ---------------------------------------------------------------------------
# A within date lies inside J and R, and an empty J and R keeps the cell
# ---------------------------------------------------------------------------

#: Customers whose lifetime has no end, so ``J`` runs to the window's end, and accounts whose range ends
#: before it: the draw is cut at the range's maximum.
CUT_AT_RANGE = (
    ("customer", "lifetimes:\n  - { emit: customer, start: cu_start }\n"),
    (CUSTOMER_START, IN_WINDOW_START),
    (ACCOUNT_OPEN, "a_open: { range: { min: '2025-01-01', max: '2025-09-30' } }"),
)
WITHIN_ACCOUNT = "within:\n  - { emit: account, relationship: customer, date: a_open }\n"


def test_a_within_date_is_never_outside_its_columns_range(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "cut", CUT_AT_RANGE[0][1] + WITHIN_ACCOUNT, *CUT_AT_RANGE[1:])
    run = _run(bundle)
    customers = {row["cu_id"]: row for row in _rows(run, "customer")}
    low, high = _day(date(2025, 1, 1)), _day(date(2025, 9, 30))
    assert low is not None and high is not None
    cut = 0
    for account in _rows(run, "account"):
        opened = _day(account["a_open"])
        parent_start = _day(customers[account["a_customer"]]["cu_start"])
        assert opened is not None and parent_start is not None
        assert max(low, parent_start) <= opened <= high
        # J is the customer's start to the window's end: the draw leaves it where R ends first.
        cut += WINDOW[1] > high
    assert cut == 150


#: Customers that all start before the window and end before it ends, accounts whose range lies before
#: the window: ``J`` and ``R`` do not meet on any row, and the entry carries a target so that it plans.
EMPTY = (
    "lifetimes:\n  - { emit: customer, start: cu_start }\n"
    "within:\n  - { emit: account, relationship: customer, date: a_open, target: '0.001' }\n"
)
EMPTY_EDITS = (
    (CUSTOMER_START, "cu_start: { range: { min: '2023-01-01', max: '2025-06-30' } }"),
    (ACCOUNT_OPEN, "a_open: { range: { min: '2023-01-01', max: '2023-03-31' } }"),
)


def test_a_row_where_the_lifetime_and_the_range_do_not_meet_keeps_its_cell_and_counts_infeasible(
    tmp_path: Path,
) -> None:
    run = _run(_bundle(tmp_path, "empty", EMPTY, *EMPTY_EDITS))
    customers = {row["cu_id"]: row for row in _rows(run, "customer")}
    cells = _cells(run, "account")
    low, high = _day(date(2023, 1, 1)), _day(date(2023, 3, 31))
    assert low is not None and high is not None
    empty = 0
    for account, cell in zip(_rows(run, "account"), cells, strict=True):
        parent_start = _day(customers[account["a_customer"]]["cu_start"])
        assert parent_start is not None
        opened = _day(account["a_open"])
        assert opened is not None and low <= opened <= high  # never outside R
        # J is the window, or the start alone for a customer that starts after it; R ends in 2023.
        window_low, window_high = WINDOW
        j = (max(parent_start, window_low), window_high) if parent_start <= window_high else None
        assert j is not None and fit_in_bounds((j[0], j[1]), (low, high)) is None
        assert account["a_open"] == cell["a_open"]
        empty += 1
    assert empty == 150
    counts = _outcome(run, "account").dates
    assert counts is not None
    found = counts.as_document()["within:customer.a_open"]
    assert found["infeasible"] + found["drawn"] == 150
    assert found["infeasible"] >= 148  # a share of 0.001 percent violates at most a row or two


# ---------------------------------------------------------------------------
# The plan refuses an enforced entry the declared ranges can leave empty
# ---------------------------------------------------------------------------

ENFORCED = (
    "lifetimes:\n  - { emit: customer, start: cu_start }\n"
    "within:\n  - { emit: account, relationship: customer, date: a_open }\n"
)
#: Every customer starts by 2025-06-30, and the accounts' range ends on 2025-03-31: a customer that
#: starts after it leaves no date in the range.
NARROW_RANGE = (
    (CUSTOMER_START, IN_WINDOW_START),
    (ACCOUNT_OPEN, "a_open: { range: { min: '2025-01-01', max: '2025-03-31' } }"),
)


def _planned(bundle: Path, tmp_path: Path) -> Any:
    return plan(bundle, state_dir=tmp_path / "state", kernel="0.2.0")


def test_an_enforced_within_entry_the_ranges_can_leave_empty_is_refused_naming_both_remedies(
    tmp_path: Path,
) -> None:
    planned = _planned(_bundle(tmp_path, "narrow", ENFORCED, *NARROW_RANGE), tmp_path)
    assert not planned.success
    assert planned.error_code == "FLX-6204"
    assert "widen the declared range" in planned.stderr.lower()
    assert "target" in planned.stderr


def test_the_refusal_ends_once_the_range_is_widened(tmp_path: Path) -> None:
    widened = (
        NARROW_RANGE[0],
        (ACCOUNT_OPEN, "a_open: { range: { min: '2025-01-01', max: '2025-09-30' } }"),
    )
    assert _planned(_bundle(tmp_path, "widened", ENFORCED, *widened), tmp_path).success


def test_the_refusal_ends_once_the_entry_carries_a_target(tmp_path: Path) -> None:
    targeted = ENFORCED.replace("date: a_open }", "date: a_open, target: '5' }")
    assert _planned(_bundle(tmp_path, "targeted", targeted, *NARROW_RANGE), tmp_path).success


def test_a_customer_that_may_end_before_the_columns_range_refuses_an_enforced_entry(
    tmp_path: Path,
) -> None:
    # The customers' ends start in 2024-01; an account opened inside a customer that ended then
    # could not also open inside a range that starts in 2024-06.
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end }\n"
        "within:\n  - { emit: account, relationship: customer, date: a_open }\n"
    )
    late = ((ACCOUNT_OPEN, "a_open: { range: { min: '2024-06-01', max: '2025-09-30' } }"),)
    planned = _planned(_bundle(tmp_path, "early", keys, *late), tmp_path)
    assert planned.error_code == "FLX-6204"
    assert "earliest end" in planned.stderr


def test_a_customer_that_may_end_before_the_window_is_not_refused(tmp_path: Path) -> None:
    # A lifetime that ended before the window is wholly J, which meets the account's range.
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end }\n"
        "within:\n  - { emit: account, relationship: customer, date: a_open }\n"
    )
    assert _planned(_bundle(tmp_path, "before", keys), tmp_path).success


# ---------------------------------------------------------------------------
# A duration's end is cut to the end column's range
# ---------------------------------------------------------------------------

#: The customers' end range is cut short of what 900 days after a 2023 start reaches, so an end that
#: an uncut duration draws past it is cut.
DURATION = (
    "lifetimes:\n"
    "  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, "
    "duration: { bins: ['0', '100', '400', '900'], binWeights: ['5', '3', '1'] } }\n"
)
KEPT_CUT = (
    (
        CUSTOMER_END,
        "cu_end: { nullFraction: '0.6', range: { min: '2024-01-01', max: '2025-12-31' } }",
    ),
    (CUSTOMER_START, "cu_start: { range: { min: '2024-01-01', max: '2025-06-30' } }"),
)


def test_a_duration_end_is_cut_inside_the_end_columns_range(tmp_path: Path) -> None:
    run = _run(_bundle(tmp_path, "cut-end", DURATION, *KEPT_CUT))
    low, high = _day(date(2024, 1, 1)), _day(date(2025, 12, 31))
    assert low is not None and high is not None
    ends = 0
    cells = _cells(run, "customer")
    for customer, cell in zip(_rows(run, "customer"), cells, strict=True):
        end = _day(customer["cu_end"])
        if cell["cu_end"] is None:
            assert end is None  # the row's own NULL stands
            continue
        start = _day(customer["cu_start"])
        assert end is not None and start is not None
        assert low <= end <= high and start <= end
        ends += 1
    assert ends > 10


#: A duration of at most 10 days from a start that is before the end range's minimum: the end is NULL
#: for the rows that no value of the duration reaches the range for.
SHORT_OPEN = (
    "lifetimes:\n"
    "  - { emit: customer, start: cu_start, end: cu_end, "
    "duration: { bins: ['0', '10'], binWeights: ['1'] } }\n"
)


def test_a_nullable_open_end_is_null_where_no_value_fits(tmp_path: Path) -> None:
    run = _run(_bundle(tmp_path, "open-end", SHORT_OPEN))
    low = _day(date(2024, 1, 1))
    assert low is not None
    nulls = ends = 0
    cells = _cells(run, "customer")
    for customer, cell in zip(_rows(run, "customer"), cells, strict=True):
        if cell["cu_end"] is None:
            continue
        start = _day(customer["cu_start"])
        assert start is not None
        if start + 9 < low:
            assert customer["cu_end"] is None  # no day of 0 to 9 reaches the range
            nulls += 1
        else:
            end = _day(customer["cu_end"])
            assert end is not None and low <= end
            ends += 1
    assert nulls > 5 and ends > 0


@pytest.mark.parametrize(
    ("bins", "message"),
    [
        (
            "['600', '900']",
            "after the",
        ),  # the latest start plus the shortest duration is past the range
        ("['0', '200']", "before the"),  # the earliest start plus the longest duration is before it
    ],
)
def test_a_kept_end_no_start_can_fit_is_refused_at_plan(
    tmp_path: Path, bins: str, message: str
) -> None:
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, "
        f"duration: {{ bins: {bins}, binWeights: ['1'] }} }}\n"
    )
    planned = _planned(_bundle(tmp_path, "refused", keys), tmp_path)
    assert planned.error_code == "FLX-6204"
    assert message in planned.stderr
    assert "endPastWindow: open" in planned.stderr
    # The same duration under open, on a nullable end, is a row with no end and plans.
    opened = keys.replace("endPastWindow: kept, ", "")
    assert _planned(_bundle(tmp_path, "opened", opened), tmp_path).success


# ---------------------------------------------------------------------------
# An exponential dwell is cut exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mean", [30, 400])
@pytest.mark.parametrize(("low", "high"), [(0, 0), (5, 5), (10, 200), (0, 1000), (365, 700)])
def test_an_exponential_span_is_the_exact_set_of_u_that_land_in_the_interval(
    mean: int, low: int, high: int
) -> None:
    rate = _rate_of(Fraction(mean), 1)
    span = _exponential_span(rate, low, high)
    assert span is not None
    first, last = span
    value = lambda v: exponential_units(v, rate, 0)  # noqa: E731
    assert low <= value(first) <= high and low <= value(last) <= high
    assert first == 0 or value(first - 1) < low
    assert last == UNIFORM53 - 1 or value(last + 1) > high
    # The draw maps u onto the span, so the least and the greatest u stay inside the interval.
    dwell = ExponentialDwell(Fraction(mean))
    for u in (0, 1, UNIFORM53 // 2, UNIFORM53 - 1):
        fits, units = draw_dwell_within(dwell, u, 1, low, high)
        assert fits and units is not None and low <= units <= high
    assert draw_dwell_within(dwell, 0, 1, low, high)[1] == value(first)


def test_an_exponential_span_is_none_where_nothing_fits() -> None:
    rate = _rate_of(Fraction(30), 1)
    # The top of the transform at mean 30 is far below 10 ** 9 days.
    assert _exponential_span(rate, 10**9, 10**9 + 5) is None
    fits, units = draw_dwell_within(ExponentialDwell(Fraction(30)), 7, 1, 10**9, 10**9 + 5)
    assert (fits, units) == (False, None)


def test_a_histogram_dwell_cut_to_no_bin_is_the_open_slot_or_nothing() -> None:
    plain = HistogramDwell(Histogram(("0", "10"), ("1",)))
    assert draw_dwell_within(plain, 5, 1, 50, 60) == (False, None)
    opened = HistogramDwell(Histogram(("0", "10"), ("1",), None, "1"))
    assert draw_dwell_within(opened, 5, 1, 50, 60) == (True, None)


def test_a_duration_cut_to_the_end_range_draws_the_cut_histogram() -> None:
    histogram = Histogram(("0", "100", "200"), ("1", "1"))
    duration = Duration("s", "e", "DATE", histogram, "kept", end_range=(1050, 1130))
    # Starting on day 1000: bin 0 holds days 1000-1099 and bin 1 days 1100-1199; the range keeps the
    # values 1050-1099 of the first and 1100-1130 of the second, in the weights 50 and 31.
    ends = {duration.draw(1000, u, None)[1] for u in range(0, UNIFORM53, UNIFORM53 // 97)}
    assert ends and all(end is not None and 1050 <= end <= 1130 for end in ends)
    applies, end = duration.draw(1000, 0, None)
    assert applies and end == 1050
    applies, end = duration.draw(1000, UNIFORM53 - 1, None)
    assert applies and end == 1130
    # A start whose every end is past the range fits nothing: kept keeps the cell, open has no end.
    assert duration.draw(5000, 0, None) == (False, None)
    nullable = replace(duration, end_past_window="open")
    assert nullable.draw(5000, 0, None) == (True, None)


def test_an_exponential_lifetime_runs_with_every_end_inside_the_range(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end, "
        "duration: { mean: '400' } }\n"
    )
    run = _run(_bundle(tmp_path, "exponential", keys))
    low, high = _day(date(2024, 1, 1)), _day(date(2025, 12, 31))
    assert low is not None and high is not None
    seen = 0
    for customer in _rows(run, "customer"):
        end = _day(customer["cu_end"])
        if end is not None:
            assert low <= end <= high
            seen += 1
    assert seen > 5


# ---------------------------------------------------------------------------
# A period's end that its parent's end cuts below the end column's range
# ---------------------------------------------------------------------------

PERIOD = (
    "lifetimes:\n"
    "  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept, "
    "duration: { bins: ['0', '200', '900'], binWeights: ['3', '1'] } }\n"
    "  - { emit: account, start: a_open, end: a_close, "
    "duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }\n"
    "within:\n"
    "  - { emit: account, relationship: customer, lifetime: true, target: '0.001' }\n"
)
PERIOD_EDITS = (
    (CUSTOMER_START, IN_WINDOW_START),
    (
        CUSTOMER_END,
        "cu_end: { nullFraction: '0.1', range: { min: '2025-01-01', max: '2025-12-31' } }",
    ),
    (
        ACCOUNT_CLOSE,
        "a_close: { nullFraction: '0.1', range: { min: '2025-09-01', max: '2025-12-31' } }",
    ),
)


def test_a_period_end_cut_below_the_end_columns_range_keeps_its_cell_and_counts_infeasible(
    tmp_path: Path,
) -> None:
    run = _run(_bundle(tmp_path, "period", PERIOD, *PERIOD_EDITS))
    customers = {row["cu_id"]: row for row in _rows(run, "customer")}
    low, high = _day(date(2025, 9, 1)), _day(date(2025, 12, 31))
    assert low is not None and high is not None
    cells = _cells(run, "account")
    kept = 0
    for account, cell in zip(_rows(run, "account"), cells, strict=True):
        parent_end = _day(customers[account["a_customer"]]["cu_end"])
        end = _day(account["a_close"])
        if end is not None:
            assert low <= end <= high or end == _day(cell["a_close"])
        if parent_end is not None and parent_end < low and account["a_close"] == cell["a_close"]:
            kept += 1
    assert kept > 0
    counts = _outcome(run, "account").dates
    assert counts is not None
    found = counts.as_document()["within:customer.lifetime"]
    assert found["infeasible"] > 0


def test_an_enforced_period_whose_parent_can_end_below_the_end_range_is_refused(
    tmp_path: Path,
) -> None:
    keys = PERIOD.replace(", target: '0.001'", "")
    planned = _planned(_bundle(tmp_path, "enforced-period", keys, *PERIOD_EDITS), tmp_path)
    assert planned.error_code == "FLX-6204"
