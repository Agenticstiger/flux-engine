# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's validity chain (ADR 0017 section 4, KERNEL-SPEC-2 section 8.4).

The chain of an entity is drawn on its own streams: one length per version at position 0 of the
``change`` path, the starts as running sums from the first start, a fit that compresses the closed
lengths when they overrun the room before ``until``, and the ends. What is pinned, on kernel 0.2.0:

* **The chain.** Per entity no gap and no overlap, exactly one open end, every start before
  ``until`` and every closed end before it, for both layouts of the versions.
* **The fit.** Compressed chains keep one unit per closed version and fill the room, with the
  remainder to the largest fraction and ties to the lower version; a chain that fits is untouched.
* **The ends.** ``nextStart``, ``dayBefore`` (the day before, a one-day version starts and ends on
  one day), the open end as NULL or as ``openEnd``, and a TIMESTAMP chain at the millisecond.
* **Two paths, one answer.** The column path's chains are the per-row path's, row for row.
* **No window end.** The same ``until`` under two windows gives the same rows, and the planner
  works a history out with a ``timeBounds`` whose end raises when it is read.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Iterator, List, Mapping, Tuple

import numpy as np
import pytest

from flux_engine.core.seedtree import counter_stream
from flux_engine.generator import history as history_module
from flux_engine.generator.history import (
    HistoryBinding,
    build_chain,
    chain_columns,
    drawn_length,
    entity_chain,
    fit_lengths,
    plan_history,
    unit_of,
)
from flux_engine.generator.paths import CHANGE_PATH_SHAPE, change_path
from flux_engine.generator.rows import generate_row_slice, generate_rows
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.history import Histogram, HistoryEntry

from . import test_history_layout as layout_tests
from ._history_plans import by_entity
from .test_history_layout import (  # noqa: F401  (fresh_layouts is an autouse fixture)
    BOUNDS,
    EMIT,
    ENTITIES,
    SIM_ID,
    UNTIL,
    VERSIONS,
    contract,
    entry,
    fresh_layouts,
    layout_of,
    plans_of,
    root_for,
    table,
)
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit

LONG = Histogram(("400", "900"), ("1",))
DAY_MS = 86_400_000
EPOCH = date(1970, 1, 1)


@pytest.fixture(autouse=True)
def chain_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every plan of this module binds the validity end as well as the start."""
    monkeypatch.setattr(
        layout_tests,
        "HistoryBinding",
        lambda held, entities, start: HistoryBinding(held, entities, start, "valid_to"),
    )


def with_changes(base: HistoryEntry, **changes: Any) -> HistoryEntry:
    from dataclasses import replace

    return replace(base, **changes)


def timestamped() -> Dict[str, Any]:
    """The contract with TIMESTAMP validity columns."""
    document = contract()
    version = next(e for e in document["exposes"] if e["exposeId"] == EMIT)
    for column in version["contract"]["schema"]:
        if column["name"] in ("valid_from", "valid_to"):
            column["type"] = "TIMESTAMP"
    return document


def days(value: Any) -> int:
    return (value - EPOCH).days


def assert_chain_holds(
    data: List[Dict[str, Any]], until: int, *, day_before: bool = False, unit: Any = days
) -> None:
    """Per entity no gap and no overlap, one open end, every start and closed end before *until*."""
    for rows in by_entity(data).values():
        starts = [unit(row["valid_from"]) for row in rows]
        assert starts == sorted(starts) and len(set(starts)) == len(starts)
        assert all(start < until for start in starts)
        ends = [row["valid_to"] for row in rows]
        assert [end is None for end in ends] == [False] * (len(rows) - 1) + [True]
        for index, end in enumerate(ends[:-1]):
            assert unit(end) < until
            assert unit(end) == starts[index + 1] - (1 if day_before else 0)


def run_of(plans: Mapping[str, Any], seed: int = 5) -> List[Dict[str, Any]]:
    return table(plans[EMIT], generate_rows(plans[EMIT], root=root_for(seed)))


# -- the chain ------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("shaped", [True, False])
def test_every_entity_has_a_chain_with_no_gap_no_overlap_and_one_open_end(
    seed: int, shaped: bool
) -> None:
    held = entry(versions=layout_tests.LEVELS if shaped else None)
    plans = plans_of(history_entry=held, rows=60, entities=12, customers=6)
    data = run_of(plans, seed)
    assert len(data) == 60 and len(by_entity(data)) == 12
    assert_chain_holds(data, unit_of(UNTIL, LogicalKind.DATE))


def test_a_chain_of_one_version_is_open_at_its_first_start() -> None:
    held = entry(versions=None)
    plans = plans_of(history_entry=held, rows=8, entities=8, customers=6)
    data = run_of(plans)
    assert [row["valid_to"] for row in data] == [None] * 8
    first_starts = layout_of(plans)[0].first_starts.tolist()
    assert sorted(days(row["valid_from"]) for row in data) == sorted(first_starts)


def test_the_first_version_starts_at_the_first_start_and_lengths_sit_in_their_bins() -> None:
    plans = plans_of(history_entry=entry(versions=None), rows=60, entities=12, customers=6)
    layout = layout_of(plans)[0]
    until = unit_of(UNTIL, LogicalKind.DATE)
    for entity, rows in enumerate(by_entity(run_of(plans)).values()):
        assert days(rows[0]["valid_from"]) in layout.first_starts.tolist()
        # A chain that was not compressed keeps its drawn lengths, which sit in the bin.
        low = 1 if days(rows[-1]["valid_from"]) == until - 1 else 30
        for row in rows[:-1]:
            length = days(row["valid_to"]) - days(row["valid_from"])
            assert low <= length <= 90, entity


def test_a_length_is_one_uniform53_at_position_zero_of_the_change_path() -> None:
    root = root_for()
    held = entry()
    for entity, version in ((0, 0), (3, 2), (7, 5)):
        stream = counter_stream(root, change_path(SIM_ID, EMIT, entity, version), CHANGE_PATH_SHAPE)
        u = stream.uniform53()
        length = drawn_length(root, SIM_ID, EMIT, held, LogicalKind.DATE, entity, version)
        # 30 to 90, the last bin closed above: 61 values.
        assert length == 30 + (u * 61) // (1 << 53)


def test_a_length_does_not_depend_on_the_number_of_versions() -> None:
    root = root_for()
    held = entry()
    before = [drawn_length(root, SIM_ID, EMIT, held, LogicalKind.DATE, 2, j) for j in range(5)]
    again = [drawn_length(root, SIM_ID, EMIT, held, LogicalKind.DATE, 2, j) for j in range(5)]
    assert before == again


# -- the fit --------------------------------------------------------------------------------------


def test_lengths_that_fit_are_left_as_drawn() -> None:
    assert fit_lengths([10, 20, 30], 100) == [10, 20, 30]
    assert fit_lengths([10, 20, 30], 61) == [10, 20, 30]
    assert fit_lengths([], 5) == []


def test_lengths_that_overrun_keep_a_unit_and_fill_the_room_by_largest_remainder() -> None:
    # Room 10 leaves 9 units: three versions keep one each and 6 are allotted over (9, 4, 1) / 14.
    fitted = fit_lengths([10, 5, 2], 10)
    assert sum(fitted) == 9 and min(fitted) >= 1
    # 6 * 9 / 14 = 3.857, 6 * 4 / 14 = 1.714, 6 * 1 / 14 = 0.428: floors 3, 1, 0, two left over.
    # The two units left over go to the largest remainders, 12 and 10 of 14.
    assert fitted == [1 + 4, 1 + 2, 1 + 0]


def test_a_tie_in_the_remainders_goes_to_the_lower_version() -> None:
    # Three equal lengths of 4 over room 6 (5 units): each keeps one, 2 are allotted: 2/3 each,
    # floors 0, remainders equal, so the first two versions take the two units.
    assert fit_lengths([4, 4, 4], 6) == [2, 2, 1]


def test_the_fit_never_fails_for_a_chain_that_has_its_room() -> None:
    for closed in range(1, 6):
        for room in range(closed + 1, closed + 9):
            fitted = fit_lengths([1000] * closed, room)
            assert len(fitted) == closed and min(fitted) >= 1 and sum(fitted) == room - 1


def test_the_fit_refuses_a_chain_with_less_room_than_one_unit_a_version() -> None:
    with pytest.raises(Exception, match="does not fit"):
        fit_lengths([5, 5, 5], 3)


def test_a_compressed_chain_still_holds_every_property() -> None:
    held = entry(versions=layout_tests.LEVELS)
    held = with_changes(held, length=LONG)
    plans = plans_of(history_entry=held, rows=60, entities=12, customers=6)
    data = run_of(plans)
    until = unit_of(UNTIL, LogicalKind.DATE)
    assert_chain_holds(data, until)
    layout = layout_of(plans)[0]
    root = root_for()
    compressed = 0
    for entity in range(12):
        count = int(layout.versions.counts[entity])
        drawn = [
            drawn_length(root, SIM_ID, EMIT, held, LogicalKind.DATE, entity, j)
            for j in range(count - 1)
        ]
        room = until - int(layout.first_starts[entity])
        if sum(drawn) > room - 1:
            compressed += 1
            chain = entity_chain(root, SIM_ID, EMIT, held, LogicalKind.DATE, layout, entity)
            assert chain.starts[-1] == until - 1
    assert compressed >= 1


def test_the_fit_reads_until_and_drawn_values_only() -> None:
    chain = build_chain(100, [50, 50, 50], 200)
    assert chain.starts == (100, 133, 166, 199)
    assert chain.ends == (133, 166, 199, None)
    again = build_chain(100, [50, 50, 50], 200)
    assert again == chain


# -- the ends -------------------------------------------------------------------------------------


def test_day_before_ends_a_version_the_day_before_the_next_starts() -> None:
    held = with_changes(entry(), chain="dayBefore")
    plans = plans_of(history_entry=held, rows=60, entities=12, customers=6)
    data = run_of(plans)
    assert_chain_holds(data, unit_of(UNTIL, LogicalKind.DATE), day_before=True)
    assert any(len(rows) > 1 for rows in by_entity(data).values())


def test_a_compressed_day_before_chain_may_start_and_end_on_one_day() -> None:
    held = with_changes(entry(), chain="dayBefore", length=LONG)
    plans = plans_of(history_entry=held, rows=60, entities=12, customers=6)
    data = run_of(plans)
    assert_chain_holds(data, unit_of(UNTIL, LogicalKind.DATE), day_before=True)


def test_day_before_is_refused_on_timestamp_columns() -> None:
    from flux_engine.core.errors import FluxError

    held = with_changes(entry(until="2025-12-31T00:00:00Z"), chain="dayBefore")
    with pytest.raises(FluxError, match="dayBefore"):
        plans = plans_of(document=timestamped(), history_entry=held)
        run_of(plans)


def test_an_open_end_is_null_or_the_declared_sentinel() -> None:
    held = with_changes(entry(), open_end="9999-12-31")
    plans = plans_of(history_entry=held, rows=60, entities=12, customers=6)
    data = run_of(plans)
    opened = [row for row in data if row["valid_to"] == date(9999, 12, 31)]
    assert len(opened) == 12 and all(row["valid_to"] is not None for row in data)
    assert len({row["sub_id"] for row in opened}) == 12
    closed = [row for row in data if row["valid_to"] != date(9999, 12, 31)]
    assert all(row["valid_to"] < date(2026, 1, 1) for row in closed)


def test_a_timestamp_chain_runs_at_the_millisecond() -> None:
    held = entry(until="2025-12-31T00:00:00Z")
    plans = plans_of(document=timestamped(), history_entry=held, rows=60, entities=12, customers=6)
    data = run_of(plans)
    until = unit_of("2025-12-31T00:00:00Z", LogicalKind.TIMESTAMP)
    assert_chain_holds(data, until, unit=int)
    gaps = [
        rows[j]["valid_to"] - rows[j]["valid_from"]
        for rows in by_entity(data).values()
        for j in range(len(rows) - 1)
    ]
    assert gaps and all(30 * DAY_MS <= gap < 90 * DAY_MS for gap in gaps)
    assert any(gap % DAY_MS for gap in gaps)  # whole milliseconds inside the day bins


def test_a_timestamp_open_end_sentinel_is_read_as_a_timestamp() -> None:
    held = with_changes(entry(until="2025-12-31T00:00:00Z"), open_end="9999-12-31T00:00:00Z")
    plans = plans_of(document=timestamped(), history_entry=held, rows=24, entities=8, customers=6)
    data = run_of(plans)
    sentinel = unit_of("9999-12-31T00:00:00Z", LogicalKind.TIMESTAMP)
    assert sum(1 for row in data if row["valid_to"] == sentinel) == 8


# -- the two paths --------------------------------------------------------------------------------


def variants() -> Iterator[Tuple[str, Dict[str, Any]]]:
    yield "plain", {}
    yield "no shape", {"history_entry": entry(versions=None)}
    yield "compressed", {"history_entry": with_changes(entry(), length=LONG)}
    yield "dayBefore", {"history_entry": with_changes(entry(), chain="dayBefore")}
    yield "openEnd", {"history_entry": with_changes(entry(), open_end="9999-12-31")}
    yield "timestamp", {
        "document": timestamped(),
        "history_entry": entry(until="2025-12-31T00:00:00Z"),
    }
    yield "timestamp, long", {
        "document": timestamped(),
        "history_entry": with_changes(entry(until="2025-12-31T00:00:00Z"), length=LONG),
    }


@pytest.mark.parametrize("name", [name for name, _ in variants()])
def test_the_column_path_writes_the_chain_the_per_row_path_writes(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    options = dict(variants())[name]
    plans = plans_of(rows=60, entities=12, customers=6, **options)
    plan = plans[EMIT]
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_row_slice(plan, 0, plan.rows, root=root_for())
    )
    assert taken == [True]
    assert by_columns == by_rows
    # In chunks that cut entities in two, and from the middle of a table.
    monkeypatch.setattr("flux_engine.generator.columns.CHUNK_ROWS", 7)
    assert generate_row_slice(plan, 0, plan.rows, root=root_for()) == by_rows
    assert generate_row_slice(plan, 13, 41, root=root_for()) == by_rows[13:41]


@pytest.mark.parametrize("name", [name for name, _ in variants()])
def test_the_arrays_are_the_per_entity_chains(name: str) -> None:
    options = dict(variants())[name]
    plans = plans_of(rows=60, entities=12, customers=6, **options)
    held = options.get("history_entry", entry())
    kind = LogicalKind.TIMESTAMP if "document" in options else LogicalKind.DATE
    layout = layout_of(plans)[0]
    root = root_for()
    rows = np.arange(60, dtype=np.int64)
    starts, ends, opened = chain_columns(root, SIM_ID, EMIT, held, kind, layout, rows)
    for row in range(60):
        entity = layout.entity_of(row)
        chain = entity_chain(root, SIM_ID, EMIT, held, kind, layout, entity)
        version = layout.version_of(row)
        assert int(starts[row]) == chain.starts[version]
        end = chain.ends[version]
        assert bool(opened[row]) == (end is None)
        if end is not None:
            assert int(ends[row]) == end


def test_a_slice_of_the_arrays_is_the_slice_of_the_whole() -> None:
    plans = plans_of(rows=60, entities=12, customers=6)
    layout = layout_of(plans)[0]
    root = root_for()
    whole = chain_columns(root, SIM_ID, EMIT, entry(), LogicalKind.DATE, layout, np.arange(60))
    part = chain_columns(
        root, SIM_ID, EMIT, entry(), LogicalKind.DATE, layout, np.arange(17, 33, dtype=np.int64)
    )
    for found, wanted in zip(part, whole, strict=True):
        assert found.tolist() == wanted[17:33].tolist()


# -- no window end --------------------------------------------------------------------------------


def test_two_windows_with_one_until_give_the_same_rows() -> None:
    narrow = {"start": BOUNDS["start"], "end": "2026-01-01T00:00:00Z"}
    wide = {"start": BOUNDS["start"], "end": "2031-06-30T00:00:00Z"}
    for options in (
        {},
        {"history_entry": entry(versions=None)},
        {"history_entry": with_changes(entry(), length=LONG)},
    ):
        first = run_of(plans_of(bounds=narrow, rows=60, entities=12, customers=6, **options))
        second = run_of(plans_of(bounds=wide, rows=60, entities=12, customers=6, **options))
        # The window moves the cells nothing sets (the plan column) but not a history's columns.
        for column in ("version_id", "sub_id", "customer_id", "valid_from", "valid_to"):
            assert [row[column] for row in first] == [row[column] for row in second], column


class _WindowEndRaises(dict):  # type: ignore[type-arg]
    """A ``timeBounds`` whose end raises when it is read."""

    def __getitem__(self, key: str) -> Any:
        if key == "end":
            raise AssertionError("the planner read timeBounds.end")
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "end":
            raise AssertionError("the planner read timeBounds.end")
        return super().get(key, default)


def test_the_planner_and_the_chain_never_read_the_window_end() -> None:
    bounds = _WindowEndRaises(start=BOUNDS["start"], end=BOUNDS["end"])
    root = root_for()
    layout = plan_history(
        root,
        SIM_ID,
        EMIT,
        entry(),
        kind=LogicalKind.DATE,
        rows=VERSIONS,
        entities=ENTITIES,
        time_bounds=bounds,
    )
    chain = chain_columns(
        root, SIM_ID, EMIT, entry(), LogicalKind.DATE, layout, np.arange(VERSIONS, dtype=np.int64)
    )
    assert chain[0].shape == (VERSIONS,)
    assert history_module.window_start(bounds) == unit_of("2023-01-01", LogicalKind.DATE) * DAY_MS


def test_the_same_entity_keeps_its_chain_when_the_table_grows() -> None:
    # More rows with the same entities: the first versions of an entity keep their lengths.
    held = entry(versions=None, parent=None)
    root = root_for()
    small = plan_history(
        root, SIM_ID, EMIT, held, kind=LogicalKind.DATE, rows=16, entities=8, time_bounds=BOUNDS
    )
    chain = entity_chain(root, SIM_ID, EMIT, held, LogicalKind.DATE, small, 0)
    assert [chain.starts[j + 1] - chain.starts[j] for j in range(len(chain.starts) - 1)] == [
        drawn_length(root, SIM_ID, EMIT, held, LogicalKind.DATE, 0, j)
        for j in range(len(chain.starts) - 1)
    ]


def test_the_last_start_is_before_until_by_a_day_at_most_when_compressed() -> None:
    held = with_changes(entry(), length=LONG)
    plans = plans_of(history_entry=held, rows=60, entities=12, customers=6)
    until = unit_of(UNTIL, LogicalKind.DATE)
    for rows in by_entity(run_of(plans)).values():
        assert days(rows[-1]["valid_from"]) <= until - 1
        assert rows[-1]["valid_from"] <= date(2025, 12, 30)
    assert date(2025, 12, 31) - timedelta(days=1) == date(2025, 12, 30)
