# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Periodic rows are one row per held version and period of a history (ADR 0027 section 5).

Built on a history's plans through :func:`flux_engine.generator.projections.project_plan`, as the
pipeline builds them. What is pinned, on kernel 0.2.0:

* **The periods.** They run from the day of ``timeBounds.start`` by the grain, each counted from
  that anchor and never from the period before, while they start before ``timeBounds.end``; the
  period column holds the period's first day.
* **The pairs.** A version holds a period under ``match``: ``overlap`` every version valid at some
  instant of it, ``start`` the one valid at its first instant, ``end`` the one valid at its last
  instant inside the window; an open version is valid up to the window's end.
* **The rows.** Period by period, then entity, then version; the count is the sum of the pairs and
  follows from the plan alone.
* **The slices.** A slice finds its first period by bisection, and is the slice of the whole across
  period boundaries.
* **Two paths, one answer.** The column path's rows equal the per-row path's.
* **The kinds.** In each period a kind's held pairs take ``LRM(M, weights)`` per level, laid out by
  ``permute/2(K, M, r)``, with ``K`` the key of the ``kind`` path of the kind and the period; a
  ``when`` is evaluated at plan time over the version and the period column; ``overrides`` are
  read as the column's type; no period's allotment reads another period.
* **The version.** A column that names a column of the history's emit holds the pair's version's
  final value, so a periodic row carries the version valid in its period.
* **A range.** ``from`` is the anchor the periods are counted from and ``through`` the last day a
  period may start on, inclusive; the count, the layout, the slices and the kinds follow, and a
  longer window appends no period past ``through``.
* **A longer window** with the same ``until`` begins with the shorter window's rows, byte for byte.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pytest

from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.expr2.functions import add_months
from flux_engine.generator import projections
from flux_engine.generator.periods import _period_range, kind_key, period_grid, period_matrix
from flux_engine.generator.projections import KINDS, project_plan, sources_of
from flux_engine.generator.rows import (
    GeneratorPlan,
    build_plan,
    generate_row_slice,
    generate_rows,
)
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.counts import ChildShape
from flux_engine.seam.history import HistoryEnd
from flux_engine.seam.projections import PERIODS_KEY, PeriodKind, PeriodsEntry
from tests.kernel2 import reference as k2

from .test_history_end import (
    CLOSE_WITH,
    EMIT,
    FINAL,
    entry,
    fresh_layouts,  # noqa: F401  (an autouse fixture)
    history_plan,
    plans_for,
    run_of,
)
from .test_history_layout import BOUNDS, SIM_ID, root_for, table
from .test_history_tracked import BOTH, tracked_entry
from .test_projection_plan import fresh_sources  # noqa: F401  (an autouse fixture)
from .test_relations import _column, _expose, _h, _primary
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit

PERIODIC = "sub_months"
DAY = 86_400_000
EPOCH = date(1970, 1, 1).toordinal()
#: What the periodic emit declares for its columns: the period column is set, the rest are drawn.
STATISTICS = {
    "month": {"range": {"min": "2023-01-01", "max": "2025-12-31"}},
    "channel": {"levels": {"web": "0.5", "app": "0.5"}},
    "amount": {"range": {"min": "1", "max": "99"}},
}


def held(
    end: Optional[HistoryEnd] = None, *, rows: int = 80, entities: int = 24, **options: Any
) -> Dict[str, GeneratorPlan]:
    options = {"activation": "activated", **options}
    return plans_for(
        tracked_entry(BOTH, end=end, **options), rows=rows, entities=entities, priced=True
    )


def periods_plan(
    plans: Mapping[str, GeneratorPlan],
    *,
    grain: str = "month",
    match: str = "overlap",
    bounds: Optional[Mapping[str, str]] = None,
    seed: int = 5,
    named: bool = False,
    **options: Any,
) -> GeneratorPlan:
    """The plan of a periodic emit of the versioned emit, as the pipeline makes it.

    With *named* the emit also has three columns of the history's emit: the entity, the plan and the price.
    """
    columns = [
        _column("month", "DATE"),
        _column("channel", "STRING", False),
        _column("amount", "INT", False),
        *(
            [
                _column("sub_id", "STRING"),
                _column("plan", "STRING", False),
                _column("price", "INT", False),
            ]
            if named
            else []
        ),
    ]
    statistics = dict(STATISTICS)
    if named:
        statistics |= {
            "plan": {"levels": {"a": "1"}},
            "price": {"range": {"min": "1", "max": "9"}},
        }
    plan = build_plan(
        compile_expose(_expose(PERIODIC, columns, _primary("sub", "sub_id") if named else {})),
        sim_id=SIM_ID,
        seam_digest=_h(PERIODIC),
        rows=1,
        fidelity={"columns": statistics},
        related={"sub_id"} if named else set(),
        time_bounds=BOUNDS if bounds is None else bounds,
        generator_version=plans[EMIT].generator_version,
    )
    found = PeriodsEntry(
        emit=PERIODIC, of=EMIT, column="month", grain=grain, match=match, **options
    )
    return project_plan(plan, PERIODS_KEY, EMIT, found, plans[EMIT], root_for(seed))


def period_rows(plan: GeneratorPlan, seed: int = 5) -> List[Dict[str, Any]]:
    return table(plan, generate_rows(plan, root=root_for(seed)))


def day_of(value: date) -> int:
    return value.toordinal() - EPOCH


def window_days(bounds: Mapping[str, str]) -> Tuple[int, int]:
    return (
        parse_rfc3339_ms(bounds["start"]) // DAY,
        parse_rfc3339_ms(bounds["end"]) // DAY,
    )


def grid(grain: str, bounds: Mapping[str, str]) -> List[Tuple[date, int, int]]:
    """Each period's first day and its first and last instant, counted from the anchor by hand."""
    first, stop = window_days(bounds)
    anchor = date.fromordinal(first + EPOCH)
    found: List[Tuple[date, int, int]] = []
    position = 0
    while True:
        if grain == "day":
            begins = anchor + timedelta(days=position)
        elif grain == "week":
            begins = anchor + timedelta(days=7 * position)
        else:
            begins = add_months(anchor, {"month": 1, "quarter": 3, "year": 12}[grain] * position)
        if day_of(begins) >= stop:
            return found
        found.append((begins, day_of(begins), 0))
        position += 1


def intervals(
    plans: Mapping[str, GeneratorPlan], bounds: Mapping[str, str]
) -> List[Tuple[int, int]]:
    """Each version row's validity in days, from its start up to its end excluded, an open one up to the window's end."""
    _, stop = window_days(bounds)
    found = []
    for version in run_of(plans):
        end = version["valid_to"]
        found.append((day_of(version["valid_from"]), stop if end is None else day_of(end)))
    return found


def expected_pairs(
    plans: Mapping[str, GeneratorPlan], grain: str, match: str, bounds: Mapping[str, str]
) -> List[List[int]]:
    """For each period, the version rows that hold it under *match*, by the definition (ADR 0027 section 5)."""
    periods = grid(grain, bounds)
    _, stop = window_days(bounds)
    spans = intervals(plans, bounds)
    found = []
    for index, (_, begins, _) in enumerate(periods):
        ends = min(periods[index + 1][1], stop) if index + 1 < len(periods) else stop
        rows = []
        for row, (valid_from, valid_to) in enumerate(spans):
            if match == "start":
                holds = valid_from <= begins < valid_to
            elif match == "end":
                holds = valid_from < ends <= valid_to
            else:
                holds = valid_from < ends and valid_to > begins
            if holds:
                rows.append(row)
        found.append(rows)
    return found


# -- the periods ----------------------------------------------------------------------------------


@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
def test_the_periods_run_from_the_windows_start_by_the_grain(grain: str) -> None:
    window = {"start": "2023-01-31T10:30:00Z", "end": "2025-03-01T00:00:00Z"}
    days, begins, ends = period_grid(
        grain, (parse_rfc3339_ms(window["start"]), parse_rfc3339_ms(window["end"]))
    )
    wanted = grid(grain, window)
    assert list(days) == [begin for begin, _, _ in wanted]
    assert begins.tolist() == [first * DAY for _, first, _ in wanted]
    # Each period ends where the next begins; the last one ends where the window does.
    assert ends[:-1].tolist() == begins[1:].tolist()
    assert int(ends[-1]) == parse_rfc3339_ms(window["end"])
    assert all(first < parse_rfc3339_ms(window["end"]) for first in begins.tolist())


def test_a_month_added_to_a_day_the_month_lacks_lands_on_its_last_day_from_the_anchor() -> None:
    window = {"start": "2023-01-31T00:00:00Z", "end": "2023-07-01T00:00:00Z"}
    days, _, _ = period_grid(
        "month", (parse_rfc3339_ms(window["start"]), parse_rfc3339_ms(window["end"]))
    )
    assert [day.isoformat() for day in days] == [
        "2023-01-31",
        "2023-02-28",
        "2023-03-31",
        "2023-04-30",
        "2023-05-31",
        "2023-06-30",
    ]


def test_the_windows_end_is_excluded() -> None:
    window = {"start": "2023-01-01T00:00:00Z", "end": "2023-04-01T00:00:00Z"}
    days, _, _ = period_grid(
        "month", (parse_rfc3339_ms(window["start"]), parse_rfc3339_ms(window["end"]))
    )
    assert [day.month for day in days] == [1, 2, 3]
    cut = {"start": "2023-01-01T00:00:00Z", "end": "2023-04-01T00:00:01Z"}
    days, _, _ = period_grid(
        "month", (parse_rfc3339_ms(cut["start"]), parse_rfc3339_ms(cut["end"]))
    )
    assert [day.month for day in days] == [1, 2, 3, 4]


def test_an_empty_window_has_no_period() -> None:
    days, begins, ends = period_grid("month", (5 * DAY, 5 * DAY))
    assert days == () and begins.shape == (0,) and ends.shape == (0,)


# -- the pairs ------------------------------------------------------------------------------------


@pytest.mark.parametrize("match", ["overlap", "start", "end"])
@pytest.mark.parametrize("grain", ["month", "quarter", "week"])
@pytest.mark.parametrize("end", [None, CLOSE_WITH])
def test_each_period_holds_the_versions_its_match_says(
    end: Optional[HistoryEnd], grain: str, match: str
) -> None:
    plans = held(end)
    plan = periods_plan(plans, grain=grain, match=match)
    layout = sources_of(plan, root_for())
    wanted = expected_pairs(plans, grain, match, BOUNDS)
    assert [layout.pairs(period).tolist() for period in range(len(wanted))] == wanted
    assert plan.rows == sum(len(rows) for rows in wanted) > 0


def test_an_open_version_is_valid_up_to_the_windows_end() -> None:
    plans = held(None)
    plan = periods_plan(plans, grain="quarter", match="end")
    layout = sources_of(plan, root_for())
    last = len(layout.days) - 1
    opened = [i for i, version in enumerate(run_of(plans)) if version["valid_to"] is None]
    assert set(opened) <= set(layout.pairs(last).tolist())


def test_the_end_match_holds_one_version_per_entity_at_most() -> None:
    plans = held(CLOSE_WITH)
    versions = run_of(plans)
    plan = periods_plan(plans, grain="month", match="end")
    layout = sources_of(plan, root_for())
    for period in range(len(layout.days)):
        entities = [versions[row]["sub_id"] for row in layout.pairs(period).tolist()]
        assert len(entities) == len(set(entities))


def test_a_version_holds_the_periods_it_overlaps_and_a_change_inside_one_holds_both() -> None:
    plans = held(None)
    plan = periods_plan(plans, grain="quarter", match="overlap")
    layout = sources_of(plan, root_for())
    versions = run_of(plans)
    by_period = [
        {versions[row]["sub_id"]: 0 for row in layout.pairs(p).tolist()}
        for p in range(len(layout.days))
    ]
    counts = [
        sum(1 for row in layout.pairs(p).tolist()) - len(by_period[p])
        for p in range(len(layout.days))
    ]
    assert any(counts)  # some period holds two versions of one entity


# -- the rows -------------------------------------------------------------------------------------


@pytest.mark.parametrize("end", [None, FINAL, CLOSE_WITH])
def test_the_rows_are_period_major_and_the_period_column_holds_the_first_day(
    end: Optional[HistoryEnd],
) -> None:
    plans = held(end)
    plan = periods_plan(plans, grain="month")
    layout = sources_of(plan, root_for())
    rows = period_rows(plan)
    wanted = [day for period, day in enumerate(layout.days) for _ in layout.pairs(period).tolist()]
    assert [row["month"] for row in rows] == wanted == sorted(wanted)
    assert plan.rows == len(rows) == int(layout.offsets[-1])


def test_the_other_columns_are_the_emits_own_cells() -> None:
    from dataclasses import replace

    plans = held(None)
    plan = periods_plan(plans)
    own = generate_rows(replace(plan, projection=None), root=root_for())
    rows = period_rows(plan)
    for index, name in ((1, "channel"), (2, "amount")):
        assert [row[name] for row in rows] == [cells[index] for cells in own]


def test_the_count_follows_from_the_plan_alone() -> None:
    plans = held(CLOSE_WITH)
    plan = periods_plan(plans, grain="month", match="overlap")
    wanted = expected_pairs(plans, "month", "overlap", BOUNDS)
    assert plan.rows == sum(len(rows) for rows in wanted)
    # No history row was generated for it: the history's cells were never drawn here.
    chain = history_plan(entry(end=CLOSE_WITH), rows=80, entities=24)
    assert chain.rows == 80


def test_a_grain_is_required() -> None:
    from flux_engine.core.errors import FluxError

    with pytest.raises(FluxError) as caught:
        periods_plan(held(None), grain=None)  # type: ignore[arg-type]
    assert caught.value.code.code == "FLX-3403" and "grain" in str(caught.value)


def test_the_plan_document_holds_the_entry() -> None:
    plans = held(None)
    document = periods_plan(plans).as_document()
    assert document["projection"]["entry"]["column"] == "month"
    assert periods_plan(plans, match="end").digest() != periods_plan(plans).digest()


def test_the_dispatch_has_the_periods_kind() -> None:
    assert "periods" in KINDS and KINDS["periods"].rows is not None


# -- from and through -----------------------------------------------------------------------------


def span_of(grain: str, from_day: Optional[str], through_day: Optional[str]) -> List[date]:
    window = (parse_rfc3339_ms(BOUNDS["start"]), parse_rfc3339_ms(BOUNDS["end"]))
    return list(period_grid(grain, window, from_day, through_day)[0])


def test_from_is_the_anchor_every_period_is_counted_from() -> None:
    days = span_of("month", "2023-03-31", None)
    assert days[:4] == [date(2023, 3, 31), date(2023, 4, 30), date(2023, 5, 31), date(2023, 6, 30)]
    assert days[-1] < date(2026, 1, 1) <= add_months(days[-1], 1)


def test_through_is_inclusive_and_cuts_a_period_it_falls_inside() -> None:
    assert span_of("month", "2023-03-01", "2023-06-01")[-1] == date(2023, 6, 1)
    assert span_of("month", "2023-03-01", "2023-05-31")[-1] == date(2023, 5, 1)
    assert span_of("month", "2023-03-01", "2023-06-15")[-1] == date(2023, 6, 1)
    assert span_of("day", None, "2023-01-03") == [date(2023, 1, d) for d in (1, 2, 3)]


def test_the_last_period_ends_where_the_next_would_start_and_the_window_cuts_it() -> None:
    window = (parse_rfc3339_ms(BOUNDS["start"]), parse_rfc3339_ms(BOUNDS["end"]))
    _, begins, ends = period_grid("month", window, "2023-03-01", "2023-06-01")
    assert ends[:-1].tolist() == begins[1:].tolist()
    assert int(ends[-1]) == (date(2023, 7, 1).toordinal() - EPOCH) * DAY
    _, _, last = period_grid("month", window, "2025-11-01", "2025-12-01")
    assert int(last[-1]) == window[1]
    _, _, wide = period_grid("month", window, "2025-12-01", "2030-01-01")
    assert int(wide[-1]) == window[1] and len(wide) == 1


def test_the_count_is_the_grids_length_over_any_range() -> None:
    from flux_engine.generator.periods import period_count

    window = (parse_rfc3339_ms(BOUNDS["start"]), parse_rfc3339_ms(BOUNDS["end"]))
    for grain in ("day", "week", "month", "quarter", "year"):
        for from_day, through_day in (
            (None, None),
            ("2023-02-10", None),
            ("2023-02-10", "2025-01-20"),
        ):
            assert period_count(grain, window, from_day, through_day) == len(
                period_grid(grain, window, from_day, through_day)[0]
            )


RANGE = {"from_day": "2023-04-01", "through_day": "2025-08-01"}


@pytest.mark.parametrize("match", ["overlap", "start", "end"])
@pytest.mark.parametrize("end", [None, CLOSE_WITH])
def test_a_ranged_entry_holds_the_pairs_of_the_periods_in_its_range(
    end: Optional[HistoryEnd], match: str
) -> None:
    plans = held(end)
    whole = expected_pairs(plans, "month", match, BOUNDS)
    plan = periods_plan(plans, grain="month", match=match, **RANGE)
    layout = sources_of(plan, root_for())
    # Period 3 of the window is April 2023; August 2025 is period 31.
    assert layout.days[0] == date(2023, 4, 1) and layout.days[-1] == date(2025, 8, 1)
    assert [layout.pairs(p).tolist() for p in range(len(layout.days))] == whole[3:32]
    rows = period_rows(plan)
    assert plan.rows == len(rows) == sum(len(pairs) for pairs in whole[3:32])
    assert all(date(2023, 4, 1) <= row["month"] <= date(2025, 8, 1) for row in rows)


def _every_slice_is_the_whole(plan: Any, *, every: int, wide: bool) -> Any:
    """The plan's rows, once every slice of them has been checked against them.

    The slices are the ends of the plan, and those that begin and end on a period's edge every
    ``every`` periods, and one row either side of it; ``wide`` adds an empty one and those that
    end on the edge.
    """
    whole = generate_rows(plan, root=root_for())
    offsets = sources_of(plan, root_for()).offsets.tolist()
    cuts = [(0, 1), (3, 9), (plan.rows - 1, plan.rows), (0, plan.rows)]
    cuts += [(5, 5)] if wide else []
    for edge in offsets[1:-1:every]:
        cuts += [(edge, edge + 4), (edge - 1, edge + 1)]
        cuts += [(max(edge - 4, 0), edge)] if wide else []
    for start, stop in cuts:
        assert generate_row_slice(plan, start, stop, root=root_for()) == whole[start:stop]
    return whole


def test_a_ranged_entry_keeps_every_slice_and_both_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = periods_plan(held(CLOSE_WITH), **RANGE)
    whole = _every_slice_is_the_whole(plan, every=4, wide=False)
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_row_slice(plan, 0, plan.rows, root=root_for())
    )
    assert taken and all(taken) and by_columns == by_rows == whole


def test_a_ranged_entry_with_kinds_counts_its_kinds_over_the_range() -> None:
    plan = periods_plan(held(CLOSE_WITH), kinds=(PeriodKind(count=2), PeriodKind(count=1)), **RANGE)
    layout = sources_of(plan, root_for())
    pairs = sum(len(layout.pairs(p)) for p in range(len(layout.days)))
    assert plan.rows == 3 * pairs == len(period_rows(plan))


def test_a_range_moves_the_plan_and_an_entry_without_one_keeps_its_member() -> None:
    plans = held(None)
    assert "from" not in periods_plan(plans).as_document()["projection"]["entry"]
    ranged = periods_plan(plans, **RANGE)
    entry = ranged.as_document()["projection"]["entry"]
    assert entry["from"] == "2023-04-01" and entry["through"] == "2025-08-01"
    assert ranged.digest() != periods_plan(plans).digest()
    assert periods_plan(plans, from_day="2023-01-01").digest() != periods_plan(plans).digest()


def test_an_entry_without_a_range_writes_the_rows_it_wrote_before() -> None:
    plans = held(CLOSE_WITH)
    plain = periods_plan(plans)
    wide = periods_plan(plans, from_day="2023-01-01", through_day="2025-12-31")
    assert generate_rows(plain, root=root_for()) == generate_rows(wide, root=root_for())
    assert PeriodsEntry(emit="e", of="o", column="c").as_member() == {
        "of": "o",
        "column": "c",
        "match": "overlap",
    }


def test_a_longer_window_appends_no_period_past_through() -> None:
    plans = held(CLOSE_WITH)
    through = {"through_day": "2025-06-01"}
    short = periods_plan(plans, bounds=BOUNDS, **through)
    long = periods_plan(plans, bounds=LONGER, **through)
    assert long.rows == short.rows
    assert generate_rows(long, root=root_for()) == generate_rows(short, root=root_for())
    open_ended = periods_plan(plans, bounds=LONGER)
    assert open_ended.rows > short.rows


# -- the slices -----------------------------------------------------------------------------------


def test_a_slice_finds_its_first_period_by_bisection() -> None:
    offsets = np.array([0, 3, 3, 7, 7, 12], dtype=np.int64)

    class Layout:
        pass

    layout = Layout()
    layout.offsets = offsets  # type: ignore[attr-defined]
    assert list(_period_range(layout, 0, 1)) == [0]  # type: ignore[arg-type]
    assert list(_period_range(layout, 2, 4)) == [0, 1, 2]  # type: ignore[arg-type]
    assert list(_period_range(layout, 3, 7)) == [2]  # type: ignore[arg-type]
    assert list(_period_range(layout, 7, 12)) == [4]  # type: ignore[arg-type]
    assert list(_period_range(layout, 6, 8)) == [2, 3, 4]  # type: ignore[arg-type]


@pytest.mark.parametrize("match", ["overlap", "end"])
@pytest.mark.parametrize("end", [None, CLOSE_WITH])
def test_a_slice_is_the_slice_of_the_whole_across_period_boundaries(
    end: Optional[HistoryEnd], match: str
) -> None:
    plan = periods_plan(held(end), grain="month", match=match)
    whole = _every_slice_is_the_whole(plan, every=3, wide=True)
    # And every share of seven, as `--shard` takes them.
    step = -(-plan.rows // 7)
    joined: List[Any] = []
    for start in range(0, plan.rows, step):
        joined.extend(
            generate_row_slice(plan, start, min(start + step, plan.rows), root=root_for())
        )
    assert tuple(joined) == whole


def test_a_process_works_out_its_layout_once(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = periods_plan(held(CLOSE_WITH))
    made: List[int] = []
    real = KINDS["periods"].sources

    def spy(chain: Any, projection: Any) -> Any:
        made.append(1)
        return real(chain, projection)

    monkeypatch.setitem(
        KINDS, "periods", type(KINDS["periods"])(KINDS["periods"].count, spy, KINDS["periods"].rows)
    )
    for start in range(0, plan.rows, 13):
        generate_row_slice(plan, start, min(start + 13, plan.rows), root=root_for())
    assert made == [1]
    assert projections._SOURCES


@pytest.mark.parametrize("end", [None, CLOSE_WITH])
def test_the_column_path_writes_the_rows_the_per_row_path_writes(
    monkeypatch: pytest.MonkeyPatch, end: Optional[HistoryEnd]
) -> None:
    plan = periods_plan(held(end))
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_row_slice(plan, 0, plan.rows, root=root_for())
    )
    assert taken and all(taken)
    assert by_columns == by_rows and len(by_rows) == plan.rows
    monkeypatch.setattr("flux_engine.generator.columns.CHUNK_ROWS", 7)
    assert generate_row_slice(plan, 0, plan.rows, root=root_for()) == by_rows
    assert generate_row_slice(plan, 5, 17, root=root_for()) == by_rows[5:17]


# -- a longer window ------------------------------------------------------------------------------

LONGER = {"start": BOUNDS["start"], "end": "2027-07-01T00:00:00Z"}
#: An end inside a period, which cuts the last period of a month or a quarter in two.
CUT = {"start": BOUNDS["start"], "end": "2026-01-15T00:00:00Z"}


@pytest.mark.parametrize("shorter", [BOUNDS, CUT], ids=["on a period's edge", "inside a period"])
@pytest.mark.parametrize("match", ["overlap", "start", "end"])
@pytest.mark.parametrize("grain", ["month", "quarter"])
@pytest.mark.parametrize("end", [None, CLOSE_WITH])
def test_a_longer_window_with_one_until_begins_with_the_shorter_windows_rows(
    end: Optional[HistoryEnd], grain: str, match: str, shorter: Mapping[str, str]
) -> None:
    """No version starts or ends after ``until``, so a period the old end cut in two holds the same pairs."""
    plans = held(end)
    short = periods_plan(plans, grain=grain, match=match, bounds=shorter)
    long = periods_plan(plans, grain=grain, match=match, bounds=LONGER)
    assert long.rows > short.rows
    assert generate_rows(long, root=root_for())[: short.rows] == generate_rows(
        short, root=root_for()
    )
    # The periods after the old end are new rows, and every old period keeps its pairs.
    old, new = sources_of(short, root_for()), sources_of(long, root_for())
    for period in range(len(old.days)):
        assert new.pairs(period).tolist() == old.pairs(period).tolist()
    assert new.offsets[: len(old.offsets)].tolist() == old.offsets.tolist()


# -- the kinds ------------------------------------------------------------------------------------

LEVELS = ChildShape("levels", ("0", "1", "3"), ("0.2", "0.5", "0.3"))
BINS = ChildShape("bins", ("1", "3", "6"), ("0.6", "0.4"))


def kinds_plan(
    plans: Mapping[str, GeneratorPlan], kinds: Tuple[PeriodKind, ...], **options: Any
) -> GeneratorPlan:
    return periods_plan(plans, kinds=kinds, **options)


def test_the_one_kind_of_a_row_per_pair_is_the_default() -> None:
    plans = held(CLOSE_WITH)
    plain = periods_plan(plans)
    explicit = kinds_plan(plans, (PeriodKind(),))
    assert plain.rows == explicit.rows
    assert period_rows(plain) == period_rows(explicit)


@pytest.mark.parametrize("number", [0, 1, 3])
def test_a_whole_count_is_that_many_rows_per_pair_in_row_order(number: int) -> None:
    plans = held(None)
    base = periods_plan(plans)
    plan = kinds_plan(plans, (PeriodKind(count=number),))
    assert plan.rows == base.rows * number
    layout = sources_of(plan, root_for())
    for period in (0, 5, len(layout.days) - 1):
        pairs, matrix = period_matrix(layout, period)
        assert matrix.tolist() == [[number]] * len(pairs)


def reference_matrix(
    layout: Any, period: int, kind: int, shape: ChildShape, held_pairs: Any
) -> List[int]:
    """What each held pair takes in *period*: the slot of rank r is ``permute/2(K, M, r)``, by the reference."""
    number = len(held_pairs)
    slots = k2.slot_values(number, shape.as_member())
    key = kind_key(root_for(), SIM_ID, PERIODIC, kind, period)
    return [slots[k2.permute(key, number, rank)] for rank in range(number)]


@pytest.mark.parametrize("shape", [LEVELS, BINS])
def test_the_pairs_take_the_levels_of_their_permuted_slots_in_each_period(
    shape: ChildShape,
) -> None:
    plans = held(CLOSE_WITH)
    plan = kinds_plan(plans, (PeriodKind(count=shape),))
    layout = sources_of(plan, root_for())
    total = 0
    for period in range(len(layout.days)):
        pairs, matrix = period_matrix(layout, period)
        wanted = reference_matrix(layout, period, 0, shape, pairs)
        assert matrix[:, 0].tolist() == wanted
        total += sum(wanted)
        if len(pairs):
            # Every period meets the kind's shares exactly, up to the largest remainder.
            levels = k2._lrm(len(pairs), [k2.Fraction(w) for w in shape.weights])  # type: ignore[attr-defined]
            assert sum(levels) == len(pairs)
    assert plan.rows == total == len(period_rows(plan))


def test_a_periods_allotment_reads_no_other_period() -> None:
    plans = held(CLOSE_WITH)
    short = kinds_plan(plans, (PeriodKind(count=LEVELS),))
    long = kinds_plan(plans, (PeriodKind(count=LEVELS),), bounds=LONGER)
    first, second = sources_of(short, root_for()), sources_of(long, root_for())
    for period in range(len(first.days)):
        assert period_matrix(first, period)[1].tolist() == period_matrix(second, period)[1].tolist()
    assert generate_rows(long, root=root_for())[: short.rows] == generate_rows(
        short, root=root_for()
    )


def test_two_kinds_hold_their_own_pairs_by_when_and_the_rows_follow_pair_then_kind() -> None:
    plans = held(None)
    versions = run_of(plans)
    pro = PeriodKind(count=2, when="plan = 'pro'", overrides=(("channel", "web"),))
    rest = PeriodKind(count=1, when="plan <> 'pro'", overrides=(("channel", "app"),))
    plan = kinds_plan(plans, (pro, rest))
    layout = sources_of(plan, root_for())
    rows = period_rows(plan)
    expected: List[Tuple[Any, ...]] = []
    for period in range(len(layout.days)):
        for version in layout.pairs(period).tolist():
            held_by_pro = versions[version]["plan"] == "pro"
            expected += [(layout.days[period], "web" if held_by_pro else "app")] * (
                2 if held_by_pro else 1
            )
    assert [(row["month"], row["channel"]) for row in rows] == expected
    assert plan.rows == len(expected)


def test_a_when_that_reads_the_period_column_holds_pairs_by_period() -> None:
    plans = held(None)
    plan = kinds_plan(plans, (PeriodKind(count=1, when="month >= DATE '2024-07-01'"),))
    layout = sources_of(plan, root_for())
    rows = period_rows(plan)
    assert rows and all(row["month"] >= date(2024, 7, 1) for row in rows)
    wanted = sum(
        len(layout.pairs(p)) for p, day in enumerate(layout.days) if day >= date(2024, 7, 1)
    )
    assert plan.rows == wanted


def test_a_when_reads_the_versions_tracked_columns_validity_and_activation() -> None:
    plans = held(None)
    versions = run_of(plans)
    plan = kinds_plan(
        plans, (PeriodKind(count=1, when="activated < valid_from AND plan <> 'team'"),)
    )
    layout = sources_of(plan, root_for())
    wanted = sum(
        1
        for period in range(len(layout.days))
        for v in layout.pairs(period).tolist()
        if versions[v]["activated"] < versions[v]["valid_from"] and versions[v]["plan"] != "team"
    )
    assert plan.rows == wanted > 0


def test_overrides_are_constants_read_as_the_columns_type() -> None:
    plans = held(None)
    kind = PeriodKind(count=1, overrides=(("channel", "kiosk"), ("amount", "7")))
    rows = period_rows(kinds_plan(plans, (kind,)))
    assert (
        rows
        and {row["channel"] for row in rows} == {"kiosk"}
        and {row["amount"] for row in rows} == {7}
    )


@pytest.mark.parametrize("end", [None, CLOSE_WITH])
def test_kinds_keep_the_slices_the_paths_and_the_shares(
    monkeypatch: pytest.MonkeyPatch, end: Optional[HistoryEnd]
) -> None:
    plans = held(end)
    kinds = (
        PeriodKind(count=LEVELS, when="plan <> 'team'", overrides=(("channel", "web"),)),
        PeriodKind(count=BINS, overrides=(("channel", "app"),)),
    )
    plan = kinds_plan(plans, kinds, match="overlap")
    whole = generate_rows(plan, root=root_for())
    offsets = sources_of(plan, root_for()).offsets.tolist()
    for edge in offsets[1:-1:2]:
        for start, stop in ((edge - 2, edge + 3), (edge, edge + 1), (max(edge - 5, 0), edge)):
            assert generate_row_slice(plan, start, stop, root=root_for()) == whole[start:stop]
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_row_slice(plan, 0, plan.rows, root=root_for())
    )
    assert taken and all(taken) and by_columns == by_rows == whole


def test_the_kind_path_is_the_decimal_positions() -> None:
    from flux_engine.generator.paths import kind_path

    assert kind_path("s", "e", 2, 11)[-3:] == ("kind", "2", "11")
    assert kind_key(root_for(), "s", "e", 2, 11) != kind_key(root_for(), "s", "e", 11, 2)


# -- the version a row carries --------------------------------------------------------------------


def expected_versions(layout: Any, kinds: int = 1) -> List[Tuple[int, int]]:
    """Each row's period and version row: period by period, then pair, then kind, then the kind's rows."""
    found: List[Tuple[int, int]] = []
    for period in range(len(layout.days)):
        pairs, matrix = period_matrix(layout, period)
        for version, takes in zip(pairs.tolist(), matrix.tolist(), strict=True):
            for count in takes:
                found += [(period, version)] * count
    return found


@pytest.mark.parametrize("match", ["overlap", "start", "end"])
@pytest.mark.parametrize("end", [None, CLOSE_WITH])
def test_a_row_carries_the_version_valid_in_its_period(
    end: Optional[HistoryEnd], match: str
) -> None:
    plans = held(end)
    versions = run_of(plans)
    plan = periods_plan(plans, match=match, named=True)
    layout = sources_of(plan, root_for())
    rows = period_rows(plan)
    wanted = expected_versions(layout)
    assert len(rows) == len(wanted) == plan.rows
    for row, (period, version) in zip(rows, wanted, strict=True):
        about = versions[version]
        assert (row["sub_id"], row["plan"], row["price"]) == (
            about["sub_id"],
            about["plan"],
            about["price"],
        )
        assert row["month"] == layout.days[period]


def test_a_period_with_a_change_carries_both_versions_under_overlap() -> None:
    plans = held(None)
    rows = period_rows(periods_plan(plans, grain="quarter", named=True))
    seen: Dict[Tuple[Any, Any], set] = {}
    for row in rows:
        seen.setdefault((row["sub_id"], row["month"]), set()).add(row["plan"])
    counts = {key: len([r for r in rows if (r["sub_id"], r["month"]) == key]) for key in seen}
    assert any(count > 1 for count in counts.values())


def test_an_override_wins_over_the_versions_column_and_other_kinds_keep_it() -> None:
    plans = held(None)
    versions = run_of(plans)
    kinds = (
        PeriodKind(count=1, when="plan = 'pro'", overrides=(("plan", "gold"),)),
        PeriodKind(count=1, when="plan <> 'pro'"),
    )
    plan = periods_plan(plans, named=True, kinds=kinds)
    rows = period_rows(plan)
    wanted = expected_versions(sources_of(plan, root_for()))
    assert {row["plan"] for row in rows} >= {"gold"}
    for row, (_period, version) in zip(rows, wanted, strict=True):
        assert row["plan"] == (
            "gold" if versions[version]["plan"] == "pro" else versions[version]["plan"]
        )


@pytest.mark.parametrize("end", [None, CLOSE_WITH])
def test_the_versions_columns_keep_the_slices_and_the_paths(
    monkeypatch: pytest.MonkeyPatch, end: Optional[HistoryEnd]
) -> None:
    plan = periods_plan(held(end), named=True, kinds=(PeriodKind(count=LEVELS),))
    whole = generate_rows(plan, root=root_for())
    for start, stop in ((0, 1), (7, 30), (plan.rows - 5, plan.rows)):
        assert generate_row_slice(plan, start, stop, root=root_for()) == whole[start:stop]
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_row_slice(plan, 0, plan.rows, root=root_for())
    )
    assert taken and all(taken) and by_columns == by_rows == whole


def test_a_longer_window_keeps_the_versions_of_the_old_rows() -> None:
    plans = held(CLOSE_WITH)
    short = periods_plan(plans, named=True, bounds=CUT)
    long = periods_plan(plans, named=True, bounds=LONGER)
    assert generate_rows(long, root=root_for())[: short.rows] == generate_rows(
        short, root=root_for()
    )


# -- the end of the calendar -------------------------------------------------------------------


@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
@pytest.mark.parametrize("through", [None, "9999-12-31", "9999-06-01"])
def test_a_window_or_a_through_near_year_9999_ends_the_periods_at_the_calendar(
    grain: str, through: Optional[str]
) -> None:
    from flux_engine.concordance.projection_checks import _periods as verify_periods
    from flux_engine.generator.periods import period_count

    window = (parse_rfc3339_ms("9998-06-01T00:00:00Z"), parse_rfc3339_ms("9999-12-31T23:59:59Z"))
    days, _, _ = period_grid(grain, window, None, through)
    assert len(days) == period_count(grain, window, None, through) > 0
    # verify's grid lists the same periods, each starting on the day the generator's does.
    from datetime import datetime

    found = verify_periods(
        grain, (datetime(9998, 6, 1), datetime(9999, 12, 31, 23, 59, 59)), None, through
    )
    assert [begins.date() for begins in found] == list(days)
