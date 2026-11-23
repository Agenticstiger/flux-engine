# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Change events are one event per change of a history (ADR 0027 section 4).

Built on a history's plans through :func:`flux_engine.generator.projections.project_plan`, as the
pipeline builds them. What is pinned, on kernel 0.2.0:

* **The events.** In entity order, a ``c`` for each entity's first version, a ``u`` for each later
  one and a ``d`` for an ended entity: under ``finalVersion`` the change into the final version,
  under ``closeLast`` the close of the last version; ``on`` keeps the listed ops.
* **The columns.** ``op`` holds the code, ``at`` the change's instant, ``date`` the same instant
  while there is no lag, ``before`` and ``after`` the previous version's and this version's
  values (NULL where the op has none), a column that names a history column the event's version,
  and the others the emit's own cells.
* **The condition.** ``when`` is evaluated at plan time over the version's tracked columns, its
  validity, its activation and, through ``previous.``, those of the version before.
* **The lag.** ``date`` is ``at`` plus a lag drawn from ``lag`` with one ``uniform53`` at position 0
  of the ``lag`` path of the entity and version (a closing ``d`` takes the ordinal one past the last
  version), inside ``[0, until - at]``, or one ``bounded`` over that interval when no bin meets it;
  the column path's lags are the per-row path's; no event is dated after ``until``.
* **The type.** ``type`` maps ``c``, ``d``, ``end`` and each tracked column to a text, and an event's
  text follows its reason.
* **The count.** It follows from the plan alone.
* **Two paths, one answer.** The column path's rows equal the per-row path's, and a slice is the
  slice of the whole.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pytest

from flux_engine.generator import projections
from flux_engine.generator.events import lag_at, lags
from flux_engine.generator.projections import KINDS, project_plan, sources_of
from flux_engine.generator.rows import (
    GeneratorPlan,
    build_plan,
    generate_row_slice,
    generate_rows,
)
from flux_engine.seam.contract import LogicalKind, compile_expose
from flux_engine.seam.counts import ChildShape
from flux_engine.seam.history import Histogram, HistoryEnd
from flux_engine.seam.projections import EVENTS_KEY, EventsEntry

from .test_history_end import (
    CLOSE_WITH,
    EMIT,
    FINAL,
    by_entity,
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

EVENTS = "sub_events"
BEFORE = (("before_plan", "plan"), ("before_price", "price"))
AFTER = (("after_plan", "plan"), ("after_price", "price"))
TYPES = (
    ("c", "created"),
    ("d", "deleted"),
    ("end", "ended"),
    ("plan", "plan_change"),
    ("tier", "tier_change"),
)
LAG = Histogram(("0", "1", "4", "20"), ("0.5", "0.3", "0.2"))
OWN = {"channel": {"levels": {"web": "0.5", "app": "0.5"}}}
#: What the events declare for the columns the history fills: never drawn, so any statistics will do.
FILLED = (
    {name: {"levels": {"a": "1"}} for name in ("op", "before_plan", "after_plan")}
    | {
        name: {"range": {"min": "2023-01-01", "max": "2025-12-31"}}
        for name in ("changed_at", "recorded_at")
    }
    | {name: {"range": {"min": "1", "max": "9"}} for name in ("before_price", "after_price")}
)


def history_of(end: Optional[HistoryEnd] = None, *, tracked: bool = True) -> Any:
    """The history entry: two tracked columns (the plan and the tier), or none."""
    return (
        tracked_entry(BOTH, end=end, activation="activated")
        if tracked
        else entry(end=end, activation="activated")
    )


def held(
    end: Optional[HistoryEnd] = None,
    *,
    rows: int = 80,
    entities: int = 24,
    tracked: bool = True,
) -> Dict[str, GeneratorPlan]:
    return plans_for(history_of(end, tracked=tracked), rows=rows, entities=entities, priced=True)


def events_entry(**options: Any) -> EventsEntry:
    base: Dict[str, Any] = {
        "emit": EVENTS,
        "of": EMIT,
        "date": "recorded_at",
        "at": "changed_at",
        "op": "op",
        "before": BEFORE,
        "after": AFTER,
    }
    return EventsEntry(**{**base, **options})


def events_plan(
    plans: Mapping[str, GeneratorPlan],
    *,
    seed: int = 5,
    date_type: str = "DATE",
    own: bool = True,
    typed: bool = False,
    **options: Any,
) -> GeneratorPlan:
    """The plan of an events emit of the versioned emit, as the pipeline makes it."""
    columns = [
        _column("sub_id", "STRING"),
        _column("op", "STRING"),
        _column("changed_at", date_type),
        _column("recorded_at", date_type),
        _column("before_plan", "STRING", False),
        _column("after_plan", "STRING", False),
        _column("before_price", "INT", False),
        _column("after_price", "INT", False),
        *([_column("channel", "STRING", False)] if own else []),
        *([_column("change_type", "STRING", False)] if typed else []),
    ]
    filled = dict(FILLED) | ({"change_type": {"levels": {"a": "1"}}} if typed else {})
    if date_type == "TIMESTAMP":
        window = {"min": "2023-01-01T00:00:00Z", "max": "2025-12-31T00:00:00Z"}
        filled |= {name: {"range": window} for name in ("changed_at", "recorded_at")}
    plan = build_plan(
        compile_expose(_expose(EVENTS, columns, _primary("sub", "sub_id"))),
        sim_id=SIM_ID,
        seam_digest=_h(EVENTS),
        rows=1,
        fidelity={"columns": filled | (OWN if own else {})},
        time_bounds=BOUNDS,
        related={"sub_id"},
        generator_version=plans[EMIT].generator_version,
    )
    if typed:
        options = {"type_column": "change_type", "type_values": TYPES, **options}
    return project_plan(
        plan, EVENTS_KEY, EMIT, events_entry(**options), plans[EMIT], root_for(seed)
    )


def event_rows(plan: GeneratorPlan, seed: int = 5) -> List[Dict[str, Any]]:
    return table(plan, generate_rows(plan, root=root_for(seed)))


def expected(
    plans: Mapping[str, GeneratorPlan],
    end: Optional[HistoryEnd],
    ops: Tuple[str, ...] = ("c", "u", "d"),
) -> List[Dict[str, Any]]:
    """The events the history's rows make, worked out from the rows alone (ADR 0027 section 4)."""
    chain = history_plan(history_of(end), rows=80, entities=24)
    ended = [] if chain.ended is None else chain.ended.tolist()
    found: List[Dict[str, Any]] = []
    for number, versions in enumerate(by_entity(run_of(plans)).values()):
        gone = bool(ended and ended[number])
        final = gone and end is not None and end.mode == "finalVersion"
        for op, about, before, after, moment in _entity_changes(versions, gone, final):
            found.append(_event(op, about, before, after, moment))
    return [row for row in found if row["op"] in ops]


def _event(op: str, about: Dict[str, Any], before: Any, after: Any, moment: date) -> Dict[str, Any]:
    return {
        "sub_id": about["sub_id"],
        "op": op,
        "changed_at": moment,
        "recorded_at": moment,
        "before_plan": None if before is None else before["plan"],
        "after_plan": None if after is None else after["plan"],
        "before_price": None if before is None else before["price"],
        "after_price": None if after is None else after["price"],
    }


def _entity_changes(
    versions: List[Dict[str, Any]], gone: bool, final: bool
) -> List[Tuple[str, Dict[str, Any], Any, Any, date]]:
    """The (op, about, before, after, date) of each change of one entity's versions."""
    changes: List[Tuple[str, Dict[str, Any], Any, Any, date]] = []
    for index, version in enumerate(versions):
        if index == 0:
            changes.append(("c", version, None, version, version["valid_from"]))
        elif final and index == len(versions) - 1:
            changes.append(("d", version, versions[index - 1], None, version["valid_from"]))
        else:
            changes.append(("u", version, versions[index - 1], version, version["valid_from"]))
    if gone and not final:
        last = versions[-1]
        changes.append(("d", last, last, None, last["valid_to"]))
    return changes


def without_own(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{name: value for name, value in row.items() if name != "channel"} for row in rows]


# -- the events -----------------------------------------------------------------------------------


@pytest.mark.parametrize("end", [None, FINAL, CLOSE_WITH])
def test_each_change_is_one_event_in_entity_order(end: Optional[HistoryEnd]) -> None:
    plans = held(end)
    plan = events_plan(plans)
    rows = event_rows(plan)
    assert without_own(rows) == expected(plans, end)
    assert plan.rows == len(rows)
    assert {row["op"] for row in rows} >= {"c", "u"}
    if end is not None:
        assert "d" in {row["op"] for row in rows}


def test_the_ending_change_is_the_d_under_final_version_and_the_close_under_close_last() -> None:
    final = event_rows(events_plan(held(FINAL)))
    closed = event_rows(events_plan(held(CLOSE_WITH)))
    # Under finalVersion the final version has no u: it is the d. Under closeLast it has both.
    assert sum(row["op"] == "d" for row in final) == sum(row["op"] == "d" for row in closed) > 0
    assert sum(row["op"] == "u" for row in final) < sum(row["op"] == "u" for row in closed)


@pytest.mark.parametrize("on", [("c",), ("u", "d"), ("d",), ("c", "d")])
@pytest.mark.parametrize("end", [FINAL, CLOSE_WITH])
def test_on_keeps_the_listed_ops(on: Tuple[str, ...], end: HistoryEnd) -> None:
    plans = held(end)
    rows = event_rows(events_plan(plans, on=on, own=False))
    assert rows == expected(plans, end, on)


def test_the_before_and_after_columns_are_null_where_the_op_has_none() -> None:
    rows = event_rows(events_plan(held(CLOSE_WITH)))
    for row in rows:
        if row["op"] == "c":
            assert row["before_plan"] is None and row["after_plan"] is not None
        if row["op"] == "d":
            assert row["after_plan"] is None and row["before_plan"] is not None
        if row["op"] == "u":
            assert row["before_plan"] is not None and row["after_plan"] is not None


def test_a_column_that_names_a_history_column_holds_the_events_version() -> None:
    plans = held(CLOSE_WITH)
    versions = run_of(plans)
    plan = events_plan(plans)
    sources = sources_of(plan, root_for())
    rows = event_rows(plan)
    assert [row["sub_id"] for row in rows] == [versions[int(i)]["sub_id"] for i in sources.about]


def test_the_other_columns_are_the_emits_own_cells() -> None:
    plans = held(None)
    plan = events_plan(plans)
    own = generate_rows(
        events_plan(plans).__class__(**{**plan.__dict__, "projection": None}), root=root_for()
    )
    index = [column.column.name for column in plan.columns].index("channel")
    assert [row["channel"] for row in event_rows(plan)] == [cells[index] for cells in own]


def test_a_timestamp_date_holds_the_instant_in_milliseconds() -> None:
    plans = held(None)
    days = event_rows(events_plan(plans))
    stamped = event_rows(events_plan(plans, date_type="TIMESTAMP"))
    assert [row["changed_at"] for row in stamped] == [
        (row["changed_at"].toordinal() - date(1970, 1, 1).toordinal()) * 86_400_000 for row in days
    ]


# -- the count ------------------------------------------------------------------------------------


@pytest.mark.parametrize("end", [None, FINAL, CLOSE_WITH])
def test_the_count_follows_from_the_plan_alone(end: Optional[HistoryEnd]) -> None:
    plans = held(end)
    plan = events_plan(plans)
    chain = history_plan(history_of(end), rows=80, entities=24)
    ended = 0 if chain.ended is None else int(chain.ended.sum())
    # c and u are one per version; a finalVersion d replaces the u of the final version.
    assert plan.rows == 80 + (ended if end is CLOSE_WITH else 0)
    assert plan.rows == len(expected(plans, end))


def test_the_dispatch_has_the_events_kind() -> None:
    assert "events" in KINDS and KINDS["events"].rows is not None


# -- the condition --------------------------------------------------------------------------------


def test_when_keeps_the_events_whose_version_it_holds_for() -> None:
    plans = held(None)
    versions = run_of(plans)
    plan = events_plan(plans, when="plan = 'pro'")
    rows = event_rows(plan)
    everything = event_rows(events_plan(plans))
    assert 0 < len(rows) < len(everything)
    assert all(row["after_plan"] == "pro" for row in rows if row["op"] != "d")
    assert plan.rows == sum(1 for version in versions if version["plan"] == "pro")


def test_when_reads_the_version_before_through_previous() -> None:
    plans = held(None)
    plan = events_plan(plans, when="previous.plan = 'basic'")
    rows = event_rows(plan)
    assert rows and all(row["before_plan"] == "basic" for row in rows)
    assert {row["op"] for row in rows} == {"u"}


def test_when_reads_the_validity_and_the_activation() -> None:
    plans = held(None)
    versions = run_of(plans)
    cut = date(2024, 6, 1)
    plan = events_plan(plans, when="valid_from >= DATE '2024-06-01' AND activated < valid_from")
    rows = event_rows(plan)
    assert plan.rows == sum(
        1
        for version in versions
        if version["valid_from"] >= cut and version["activated"] < version["valid_from"]
    )
    assert all(row["changed_at"] >= cut for row in rows)


def test_a_closing_d_is_about_the_last_version() -> None:
    plans = held(CLOSE_WITH)
    versions = run_of(plans)
    rows = event_rows(events_plan(plans, on=("d",), when="plan = 'pro'"))
    last = {chain[-1]["sub_id"]: chain[-1] for chain in by_entity(versions).values()}
    assert rows and all(last[row["sub_id"]]["plan"] == "pro" for row in rows)
    assert all(row["before_plan"] == "pro" and row["after_plan"] is None for row in rows)


# -- the paths ------------------------------------------------------------------------------------


@pytest.mark.parametrize("end", [None, FINAL, CLOSE_WITH])
def test_a_slice_is_the_slice_of_the_whole(end: Optional[HistoryEnd]) -> None:
    plan = events_plan(held(end), when="plan <> 'team'")
    whole = generate_rows(plan, root=root_for())
    for start, stop in ((0, 1), (3, 9), (plan.rows - 1, plan.rows), (5, 5), (0, plan.rows)):
        assert generate_row_slice(plan, start, stop, root=root_for()) == whole[start:stop]


@pytest.mark.parametrize("end", [None, CLOSE_WITH])
def test_the_column_path_writes_the_rows_the_per_row_path_writes(
    monkeypatch: pytest.MonkeyPatch, end: Optional[HistoryEnd]
) -> None:
    plan = events_plan(held(end))
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_row_slice(plan, 0, plan.rows, root=root_for())
    )
    assert taken and all(taken)
    assert by_columns == by_rows and len(by_rows) == plan.rows
    monkeypatch.setattr("flux_engine.generator.columns.CHUNK_ROWS", 7)
    assert generate_row_slice(plan, 0, plan.rows, root=root_for()) == by_rows
    assert generate_row_slice(plan, 5, 17, root=root_for()) == by_rows[5:17]


def test_the_events_draw_nothing_the_history_does_not() -> None:
    plans = held(FINAL)
    before = run_of(plans)
    event_rows(events_plan(plans))
    assert run_of(plans) == before


def test_a_process_works_out_its_layout_once(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = events_plan(held(FINAL))
    made: List[int] = []
    real = KINDS["events"].sources

    def spy(chain: Any, projection: Any) -> Any:
        made.append(1)
        return real(chain, projection)

    monkeypatch.setitem(
        KINDS, "events", type(KINDS["events"])(KINDS["events"].count, spy, KINDS["events"].rows)
    )
    for start in range(0, plan.rows, 11):
        generate_row_slice(plan, start, min(start + 11, plan.rows), root=root_for())
    assert made == [1]
    assert projections._SOURCES


def test_an_events_plan_document_holds_the_entry() -> None:
    plans = held(None)
    document = events_plan(plans).as_document()
    assert document["projection"]["entry"]["date"] == "recorded_at"
    assert events_plan(plans, on=("c",)).digest() != events_plan(plans).digest()


# -- the lag --------------------------------------------------------------------------------------

UNTIL_DAY = date(2025, 12, 31)
DAY = 86_400_000


def days_between(first: Any, second: Any) -> int:
    return (second - first).days


@pytest.mark.parametrize("end", [None, FINAL, CLOSE_WITH])
def test_date_is_at_plus_a_lag_inside_the_room_to_until(end: Optional[HistoryEnd]) -> None:
    plans = held(end)
    plain = event_rows(events_plan(plans))
    rows = event_rows(events_plan(plans, lag=LAG))
    assert len(rows) == len(plain)
    lagged = [days_between(row["changed_at"], row["recorded_at"]) for row in rows]
    assert [row["changed_at"] for row in rows] == [row["changed_at"] for row in plain]
    assert all(
        0 <= value <= days_between(row["changed_at"], UNTIL_DAY)
        for value, row in zip(lagged, rows, strict=True)
    )
    assert any(lagged) and not all(lagged)
    assert all(row["recorded_at"] <= UNTIL_DAY for row in rows)


def test_a_lag_is_the_draw_of_its_entity_and_version() -> None:
    plans = held(CLOSE_WITH)
    plan = events_plan(plans, lag=LAG)
    layout = sources_of(plan, root_for())
    rows = event_rows(plan)
    entity_of = history_plan(history_of(CLOSE_WITH), rows=80, entities=24)
    for index in (0, 1, 7, len(rows) - 1):
        about = int(layout.about[index])
        entity = entity_of.entity_of(about)
        hi = days_between(rows[index]["changed_at"], UNTIL_DAY)
        drawn = lag_at(
            root_for(),
            SIM_ID,
            EVENTS,
            LAG,
            LogicalKind.DATE,
            entity,
            int(layout.version[index]),
            hi,
        )
        assert days_between(rows[index]["changed_at"], rows[index]["recorded_at"]) == drawn


def test_a_closing_d_is_drawn_at_the_version_one_past_the_last() -> None:
    plans = held(CLOSE_WITH)
    plan = events_plan(plans, lag=LAG)
    layout = sources_of(plan, root_for())
    chain = history_plan(history_of(CLOSE_WITH), rows=80, entities=24)
    closing = np.flatnonzero(layout.closing)
    assert closing.shape[0] == int(chain.ended.sum()) > 0  # type: ignore[union-attr]
    for index in closing.tolist():
        entity = chain.entity_of(int(layout.about[index]))
        assert layout.version[index] == chain.versions.counts[entity]


@pytest.mark.parametrize("hi_max", [0, 3, 40, 500])
def test_the_column_path_draws_the_lags_the_per_row_path_draws(hi_max: int) -> None:
    entities = np.arange(60, dtype=np.int64) % 24
    versions = np.arange(60, dtype=np.int64) % 5
    his = (np.arange(60, dtype=np.int64) * 7) % (hi_max + 1)
    for kind, histogram in (
        (LogicalKind.DATE, LAG),
        (LogicalKind.TIMESTAMP, LAG),
        (LogicalKind.DATE, Histogram(("30", "90"), ("1",))),  # no bin meets a small interval
    ):
        scale = DAY if kind is LogicalKind.TIMESTAMP else 1
        by_columns = lags(
            root_for(), SIM_ID, EVENTS, histogram, kind, entities, versions, his * scale
        )
        by_rows = [
            lag_at(root_for(), SIM_ID, EVENTS, histogram, kind, int(e), int(v), int(hi) * scale)
            for e, v, hi in zip(entities, versions, his, strict=True)
        ]
        assert by_columns.tolist() == by_rows
        assert all(0 <= value <= hi * scale for value, hi in zip(by_rows, his, strict=True))


def test_a_lag_falls_inside_the_bins_that_meet_the_room() -> None:
    his = np.full(400, 30, dtype=np.int64)
    entities = np.arange(400, dtype=np.int64)
    versions = np.zeros(400, dtype=np.int64)
    values = lags(root_for(), SIM_ID, EVENTS, LAG, LogicalKind.DATE, entities, versions, his)
    assert values.min() >= 0 and values.max() <= 20
    # The bins [0,1), [1,4), [4,20] with weights 0.5, 0.3, 0.2.
    assert abs((values == 0).mean() - 0.5) < 0.1
    assert abs(((values >= 1) & (values < 4)).mean() - 0.3) < 0.1


def test_a_lag_with_no_bin_inside_the_room_is_uniform_over_it() -> None:
    far = Histogram(("100", "200"), ("1",))
    his = np.full(600, 4, dtype=np.int64)
    entities = np.arange(600, dtype=np.int64)
    values = lags(
        root_for(),
        SIM_ID,
        EVENTS,
        far,
        LogicalKind.DATE,
        entities,
        np.zeros(600, dtype=np.int64),
        his,
    )
    assert set(values.tolist()) == {0, 1, 2, 3, 4}


def test_a_timestamp_lag_is_whole_milliseconds_inside_its_day_bins() -> None:
    plans = held(None)
    rows = event_rows(events_plan(plans, date_type="TIMESTAMP", lag=LAG))
    lagged = [row["recorded_at"] - row["changed_at"] for row in rows]
    until = (UNTIL_DAY.toordinal() - date(1970, 1, 1).toordinal()) * DAY
    assert any(value % DAY for value in lagged)
    assert all(0 <= value <= 20 * DAY for value in lagged)
    assert all(row["recorded_at"] <= until for row in rows)


def test_a_lag_moves_no_other_cell_and_no_count() -> None:
    plans = held(CLOSE_WITH)
    plain = events_plan(plans)
    lagged = events_plan(plans, lag=LAG)
    assert plain.rows == lagged.rows

    def drop(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [{k: v for k, v in row.items() if k != "recorded_at"} for row in rows]

    assert drop(event_rows(plain)) == drop(event_rows(lagged))


# -- the type -------------------------------------------------------------------------------------


@pytest.mark.parametrize("end", [None, FINAL, CLOSE_WITH])
def test_the_type_follows_the_reason_of_the_change(end: Optional[HistoryEnd]) -> None:
    plans = held(end)
    versions = run_of(plans)
    plan = events_plan(plans, typed=True)
    rows = event_rows(plan)
    layout = sources_of(plan, root_for())
    assert len(rows) == plan.rows
    for row, about, before in zip(rows, layout.about.tolist(), layout.before.tolist(), strict=True):
        if row["op"] == "c":
            assert row["change_type"] == "created"
        elif row["op"] == "d":
            assert row["change_type"] == "deleted"
        else:
            old, new = versions[before], versions[about]
            if new["status"] == "gone":
                assert row["change_type"] == "ended"
            elif old["plan"] != new["plan"]:
                assert row["change_type"] == "plan_change"
            else:
                assert old["tier"] != new["tier"] and row["change_type"] == "tier_change"
    assert {"plan_change", "tier_change"} <= {row["change_type"] for row in rows}
    if end is CLOSE_WITH:
        assert "ended" in {row["change_type"] for row in rows}


def test_a_type_without_a_text_for_a_reason_is_refused() -> None:
    from flux_engine.core.errors import FluxError

    with pytest.raises(FluxError) as caught:
        plan = events_plan(held(None), typed=True, type_values=(("c", "created"), ("d", "deleted")))
        event_rows(plan)
    assert caught.value.code.code == "FLX-3403"


# Fix: the type of an events emit over a history that tracks two columns, across seeds, ends and shapes.
NO_CHANGE = ChildShape("levels", ("1",), ("1",))


@pytest.mark.parametrize("end", [None, FINAL, CLOSE_WITH])
@pytest.mark.parametrize("versions", [None, NO_CHANGE], ids=["default shape", "one version each"])
def test_a_type_block_plans_and_names_the_column_that_changed_across_seeds(
    end: Optional[HistoryEnd], versions: Optional[ChildShape]
) -> None:
    options = {} if versions is None else {"versions": versions}
    shown = tracked_entry(BOTH, end=end, activation="activated", **options)
    plans = plans_for(shown, rows=80, entities=24, priced=True)
    for seed in range(1, 9):
        plan = events_plan(plans, seed=seed, typed=True)
        rows = event_rows(plan, seed)
        data = run_of(plans, seed)
        layout = sources_of(plan, root_for(seed))
        assert len(rows) == plan.rows
        for row, about, before in zip(
            rows, layout.about.tolist(), layout.before.tolist(), strict=True
        ):
            if row["op"] != "u":
                assert row["change_type"] == {"c": "created", "d": "deleted"}[row["op"]]
                continue
            old, new = data[before], data[about]
            if end is not None and new["status"] == "gone":
                assert row["change_type"] == "ended"
                continue
            moved_columns = [name for name in ("plan", "tier") if old[name] != new[name]]
            assert [row["change_type"]] == [
                {"plan": "plan_change", "tier": "tier_change"}[name] for name in moved_columns
            ]


@pytest.mark.parametrize("mode", ["finalVersion", "closeLast"])
def test_an_ended_last_entity_with_no_change_left_takes_its_ending_text(mode: str) -> None:
    """Fix 1: the last entity's ending version has change index ``T``, one past the reasons' range."""
    end = HistoryEnd(mode, "1", "status", "gone")
    two = ChildShape("levels", ("2",), ("1",))
    shown = tracked_entry(BOTH, end=end, activation="activated", versions=two)
    plans = plans_for(shown, rows=48, entities=24, priced=True)
    chain = history_plan(shown, rows=48, entities=24)
    assert chain.ended is not None and bool(chain.ended[-1])
    for seed in (1, 2, 3):
        plan = events_plan(plans, seed=seed, typed=True)
        rows = event_rows(plan, seed)
        layout = sources_of(plan, root_for(seed))
        last = int(chain.versions.offsets[-1] + chain.versions.counts[-1] - 1)
        ending = [
            row for row, about in zip(rows, layout.about.tolist(), strict=True) if about == last
        ]
        assert ending
        texts = {"u": "ended", "d": "deleted"} if mode == "closeLast" else {"d": "deleted"}
        assert all(row["change_type"] == texts[row["op"]] for row in ending if row["op"] != "c")
        assert any(
            row["change_type"] == ("ended" if mode == "closeLast" else "deleted") for row in ending
        )
