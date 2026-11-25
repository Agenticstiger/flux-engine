# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history keeps to its parent's lifetime (ADR 0017, amendment of 2026-11-24).

``parent.lifetime`` is the history's counterpart of ADR 0024's ``within`` with ``lifetime: true``.
With ``P = [ps, pe]`` the lifetime of an entity's parent row (``pe`` absent when the parent has no
end, a NULL end, or an end before its start), what is pinned, on kernel 0.2.0:

* **The first start** lies in ``J`` and in ``[ps, pe - c')``, or, where that is empty, in the room for
  one version, drawn by the same draw on the same path and position as without the bound, so an
  entity whose interval is ``J`` draws what it drew.
* **The room** ends at ``min(until, pe)``: every closed version ends before the parent's last day.
* **The parent that ended** inside the horizon forces its entities to end: the last version of
  an entity is closed (``closeLast``, or no ``end``) or is the ending version (``finalVersion``),
  at the latest on the parent's last day, and they count towards the end share.
* **The room.** An entity whose parent leaves no room for one version is ``FLX-3403``; one with
  room for fewer versions than it was given is capped at that room, and what it loses moves to
  entities with room to spare.
* **Two paths, one answer**: the column path's rows equal the per-row path's, and a slice is the
  slice of the run.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pytest

from flux_engine.generator import history, parent_bounds, relations
from flux_engine.generator.dates import days_of_date
from flux_engine.generator.history import (
    HistoryBinding,
    HistoryPlan,
    ParentBound,
    ended_entities,
    first_start_at,
    plan_history,
    unit_of,
)
from flux_engine.generator.rows import (
    GeneratorPlan,
    generate_row_slice,
)
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.counts import ChildShape
from flux_engine.seam.history import (
    Histogram,
    HistoryEnd,
    HistoryEntry,
    HistoryParent,
)

from ._history_plans import by_entity, plans_of
from .test_history_layout import BOUNDS, SIM_ID, UNTIL, root_for
from .test_history_layout import entry as layout_entry
from .test_relations import _column, _expose, _primary, _rel
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit

EMIT = "sub_version"
LENGTH = Histogram(("30", "90"), ("1",))
BOUND_PARENT = HistoryParent("customer", min_children=1, lifetime=True)
PLAIN_PARENT = HistoryParent("customer", min_children=1)
LIFETIME = ("opened", "closed")
STATUS = {"active": "1", "gone": "0"}
OPENED = {"range": {"min": "2023-01-01", "max": "2025-03-31"}}
CLOSED = {"nullFraction": "0.4", "range": {"min": "2023-06-01", "max": "2025-12-30"}}
FINAL = HistoryEnd("finalVersion", "0.25", "status", "gone")
CLOSE = HistoryEnd("closeLast", "0.25")


def contract() -> Dict[str, Any]:
    customer = _expose(
        "customer",
        [
            _column("customer_id", "INT"),
            _column("opened", "DATE"),
            _column("closed", "DATE", False),
        ],
        _primary("customer", "customer_id"),
    )
    version = _expose(
        EMIT,
        [
            _column("version_id", "INT"),
            _column("sub_id", "STRING"),
            _column("customer_id", "INT", False),
            _column("status", "STRING", False),
            _column("valid_from", "DATE"),
            _column("valid_to", "DATE", False),
        ],
        {
            "entities": [
                {"name": "version", "type": "primary", "expr": "version_id"},
                {"name": "sub", "type": "natural", "expr": "sub_id"},
            ],
            "dimensions": [
                {
                    "name": "valid_from",
                    "type": "time",
                    "typeParams": {"validityParams": {"isStart": True}},
                },
                {
                    "name": "valid_to",
                    "type": "time",
                    "typeParams": {"validityParams": {"isEnd": True}},
                },
            ],
            "relationships": [_rel("customer", "customer", ["customer_id"], ["customer_id"])],
        },
    )
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.history.bound",
        "exposes": [customer, version],
    }


def entry(
    *, entities: int = 40, parent: Optional[HistoryParent] = BOUND_PARENT, **rest: Any
) -> HistoryEntry:
    return layout_entry(entities=entities, parent=parent, **rest)


def plans_for(
    held: HistoryEntry,
    *,
    rows: int = 120,
    customers: int = 12,
    opened: Optional[Mapping[str, Any]] = None,
    closed: Optional[Mapping[str, Any]] = None,
    lifetime: Optional[Tuple[str, Optional[str]]] = LIFETIME,
    declared_rows: Optional[int] = None,
) -> Dict[str, GeneratorPlan]:
    declared: Dict[str, Tuple[Dict[str, Any], int]] = {
        "customer": (
            {"opened": dict(opened or OPENED), "closed": dict(closed or CLOSED)},
            customers,
        ),
        EMIT: ({"status": {"levels": dict(STATUS)}}, rows),
    }
    binding = HistoryBinding(
        held,
        held.entities,
        "valid_from",
        "valid_to",
        lifetime if held.parent is not None and held.parent.lifetime else None,
        **({} if declared_rows is None else {"declared_rows": declared_rows}),
    )
    return plans_of(contract(), declared, {EMIT: binding}, emit=EMIT)


@pytest.fixture(autouse=True)
def fresh_layouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test works its layouts out afresh: the caches are per process."""
    monkeypatch.setattr(relations, "_LAYOUTS", {})
    monkeypatch.setattr(parent_bounds, "_BOUNDS", {})
    monkeypatch.setattr(history, "_PLANS", {})
    monkeypatch.setattr(history, "_OUTERS", {})


def layout_of(plans: Mapping[str, GeneratorPlan], seed: int = 5) -> HistoryPlan:
    """The layout the history's own plan gives, through a reader as a pass has one."""
    plan = plans[EMIT]
    reader = relations.RelationReader(root_for(seed))
    reader.know(plan)
    pick = relations.history_pick_of(plan)
    assert pick is not None
    return reader._chain_of(relations.owner_of(plan), pick)


def rows_of(plan: GeneratorPlan, seed: int = 5) -> List[Dict[str, Any]]:
    names = [column_plan.column.name for column_plan in plan.columns]
    return [
        dict(zip(names, row, strict=True))
        for row in generate_row_slice(plan, 0, plan.rows, root=root_for(seed))
    ]


def lifetimes_of(plans: Mapping[str, GeneratorPlan], seed: int = 5) -> Dict[Any, Tuple[date, Any]]:
    """Each customer's ``(opened, closed)``, as the parent's own pass wrote them."""
    return {
        row["customer_id"]: (row["opened"], row["closed"])
        for row in rows_of(plans["customer"], seed)
    }


def chains(
    plans: Mapping[str, GeneratorPlan], seed: int = 5
) -> List[Tuple[Any, List[Dict[str, Any]]]]:
    """Each entity's parent lifetime and its versions, by start."""
    parents = lifetimes_of(plans, seed)
    found = []
    for versions in by_entity(rows_of(plans[EMIT], seed)).values():
        ordered = sorted(versions, key=lambda row: row["valid_from"])
        found.append((parents[ordered[0]["customer_id"]], ordered))
    return found


def last_day(closed: Optional[date], opened: date) -> Optional[date]:
    """The parent's last day, ``None`` for an open lifetime: no end, or an end before the start."""
    return closed if closed is not None and closed >= opened else None


# -- the room -----------------------------------------------------------------------------------


@pytest.mark.parametrize("end", [None, CLOSE, FINAL])
def test_every_version_keeps_to_its_parents_lifetime(end: Optional[HistoryEnd]) -> None:
    plans = plans_for(entry(end=end))
    layout = layout_of(plans)
    assert layout.cut is not None and layout.cut.shortfall == 0
    seen_closed = 0
    for (opened, closed), versions in chains(plans):
        stop = last_day(closed, opened)
        assert versions[0]["valid_from"] >= opened
        for row in versions:
            assert stop is None or row["valid_from"] <= stop
            if row["valid_to"] is not None and stop is not None:
                assert row["valid_to"] <= stop
        seen_closed += stop is not None
    assert seen_closed  # the declared lifetimes close some parents


@pytest.mark.parametrize("end", [None, CLOSE, FINAL])
def test_the_entities_of_an_ended_parent_end(end: Optional[HistoryEnd]) -> None:
    plans = plans_for(entry(end=end))
    layout = layout_of(plans)
    assert layout.cut is not None and layout.ended is not None
    assert bool(layout.ended[layout.cut.forced].all())
    forced = 0
    for (opened, closed), versions in chains(plans):
        stop = last_day(closed, opened)
        if stop is None or stop > date.fromisoformat(UNTIL):
            continue
        forced += 1
        last = versions[-1]
        if end is not None and end.mode == "finalVersion":
            assert last["status"] == "gone" and last["valid_to"] is None
        else:
            assert last["valid_to"] is not None and last["valid_to"] <= stop
    assert forced == int(layout.cut.forced.sum()) > 0


def test_a_history_with_no_end_ends_only_the_entities_of_an_ended_parent() -> None:
    plans = plans_for(entry(end=None))
    layout = layout_of(plans)
    assert layout.closes_last and layout.ended is not None
    assert np.array_equal(layout.ended, layout.cut.forced)
    for (_opened, closed), versions in chains(plans):
        if closed is None:
            assert versions[-1]["valid_to"] is None


def test_the_forced_entities_count_towards_the_end_share() -> None:
    plans = plans_for(entry(end=FINAL))
    layout = layout_of(plans)
    assert layout.cut is not None and layout.ended is not None
    assert FINAL.share is not None
    forced = int(layout.cut.forced.sum())
    assert forced > 0
    # n = min(LRM(E, share), E'): a share of every entity, capped at those that can end (the
    # eligible ones and the forced ones), and the forced ones end whatever n is.
    can_end = int(((layout.versions.counts >= 2) | layout.cut.forced).sum())
    asked = min(history.declared_ended(layout.entities, FINAL.share), can_end)
    assert int(layout.ended.sum()) == max(asked, forced)


def test_forced_entities_beyond_the_share_all_end_and_no_other_does() -> None:
    held = entry(end=HistoryEnd("finalVersion", "0.01", "status", "gone"))
    layout = layout_of(plans_for(held))
    assert layout.cut is not None and layout.ended is not None
    assert int(layout.cut.forced.sum()) > 0
    assert np.array_equal(layout.ended, layout.cut.forced)


def test_forced_entities_end_whatever_the_count_of_their_versions() -> None:
    # An entity with one version, whose parent ended, has that version as its ending version.
    held = entry(versions=ChildShape("levels", ("1",), ("1",)), end=FINAL, entities=40)
    plans = plans_for(held, rows=40)
    layout = layout_of(plans)
    assert layout.cut is not None and int(layout.cut.forced.sum()) > 0
    assert int(layout.versions.counts.max()) == 1
    rows = rows_of(plans[EMIT])
    gone = {row["sub_id"] for row in rows if row["status"] == "gone"}
    assert len(gone) >= int(layout.cut.forced.sum())
    # An ending version is no change, and a one-version entity has none to give.
    offsets = history._change_offsets(layout.versions.counts, layout.ended)
    assert int(offsets[-1]) == 0


def test_forcing_none_leaves_the_end_permutation_as_it_was() -> None:
    held = entry(end=FINAL, parent=PLAIN_PARENT)
    counts = np.array([1, 3, 2, 5, 1, 4, 2, 2], dtype=np.int64)
    plain = ended_entities(root_for(), SIM_ID, EMIT, held, counts)
    nothing = ended_entities(root_for(), SIM_ID, EMIT, held, counts, np.zeros(8, dtype=bool))
    assert np.array_equal(plain, nothing)
    some = np.zeros(8, dtype=bool)
    some[0] = True  # a one-version entity, which only a parent can end
    forced = ended_entities(root_for(), SIM_ID, EMIT, held, counts, some)
    assert bool(forced[0])


# -- the first start ----------------------------------------------------------------------------


def test_an_entity_whose_interval_is_j_draws_what_it_drew_without_the_bound() -> None:
    # Parents that opened before the window and never close leave every interval J. A closeLast end
    # reserves the same room with and without the key, so the draws are the same ones.
    far = {"range": {"min": "2023-01-01", "max": "2023-01-01"}}
    never = {"nullFraction": "1", "range": {"min": "2023-06-01", "max": "2025-12-30"}}
    end = HistoryEnd("closeLast", "0")
    bound = layout_of(plans_for(entry(end=end), opened=far, closed=never))
    plain = layout_of(plans_for(entry(end=end, parent=PLAIN_PARENT), opened=far, closed=never))
    assert np.array_equal(bound.first_starts, plain.first_starts)
    assert np.array_equal(bound.versions.counts, plain.versions.counts)
    assert bound.cut is not None and not bool(bound.cut.forced.any())


def test_the_first_start_is_the_draw_over_the_cut_interval_on_the_same_path() -> None:
    plans = plans_for(entry(end=CLOSE))
    layout = layout_of(plans)
    parents = relations_parents(plans, layout)
    for entity in range(layout.entities):
        opened, closed = parents[entity]
        found = first_start_at(
            root_for(),
            SIM_ID,
            EMIT,
            entry(end=CLOSE),
            LogicalKind.DATE,
            entity,
            c=layout.c,
            time_bounds=(unit_of("2023-01-01", LogicalKind.DATE) * 86_400_000,),
            parent=(days_of_date(opened), None if closed is None else days_of_date(closed)),
        )
        assert found == int(layout.first_starts[entity])


def relations_parents(
    plans: Mapping[str, GeneratorPlan], layout: HistoryPlan
) -> Dict[int, Tuple[date, Any]]:
    """Each entity's parent's ``(opened, closed)``, the end open when it is before the start."""
    customers = rows_of(plans["customer"])
    found: Dict[int, Tuple[date, Any]] = {}
    for entity in range(layout.entities):
        row = customers[int(layout.per_parent.parent_of(entity))]  # type: ignore[union-attr]
        closed = row["closed"]
        found[entity] = (
            row["opened"],
            closed if closed is not None and closed >= row["opened"] else None,
        )
    return found


def test_an_end_before_the_start_cuts_nothing() -> None:
    before = {"range": {"min": "2023-02-01", "max": "2023-03-01"}}
    late = {"range": {"min": "2024-06-01", "max": "2024-09-01"}}
    layout = layout_of(plans_for(entry(end=CLOSE), opened=late, closed=before))
    assert layout.cut is not None and not bool(layout.cut.forced.any())
    assert layout.cut is not None and bool((layout.cut.limits == layout.until).all())
    assert int(layout.first_starts.min()) >= unit_of("2024-06-01", LogicalKind.DATE)


def test_a_parent_with_no_end_cuts_nothing() -> None:
    plans = plans_for(entry(end=CLOSE), lifetime=("opened", None))
    layout = layout_of(plans)
    assert layout.cut is not None and not bool(layout.cut.forced.any())
    assert layout.cut is not None and bool((layout.cut.limits == layout.until).all())
    assert int(layout.first_starts.min()) >= unit_of("2023-01-01", LogicalKind.DATE)


# -- the plan -----------------------------------------------------------------------------------


def test_the_plan_reads_each_parents_lifetime_in_the_chains_unit() -> None:
    bound = ParentBound.of([(10, 20), None, (5, None)], ("x",))
    assert bound.has_start.tolist() == [True, False, True]
    assert bound.has_end.tolist() == [True, False, False]
    assert bound.starts.tolist()[0] == 10 and bound.ends.tolist()[0] == 20


def test_a_bound_entry_is_refused_without_the_lifetimes_and_the_other_way_round() -> None:
    with pytest.raises(Exception, match="parent lifetimes|parents' lifetimes"):
        plan_history(
            root_for(),
            SIM_ID,
            EMIT,
            entry(),
            kind=LogicalKind.DATE,
            rows=120,
            entities=40,
            time_bounds=BOUNDS,
        )


def test_the_chain_and_the_parents_layout_are_one_pass_in_every_path() -> None:
    plans = plans_for(entry(end=FINAL))
    plan = plans[EMIT]
    assert plan.reads, "the history plan reads its parent's plan"
    again = plans_for(entry(end=FINAL))
    assert plan.digest() == again[EMIT].digest()
    assert plans_for(entry(end=FINAL, parent=PLAIN_PARENT))[EMIT].digest() != plan.digest()


def test_both_paths_and_every_slice_give_the_same_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    plans = plans_for(entry(end=FINAL))
    plan = plans[EMIT]
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_row_slice(plan, 0, plan.rows, root=root_for())
    )
    assert by_columns == by_rows and any(taken)
    assert generate_row_slice(plan, 17, 71, root=root_for()) == by_rows[17:71]


def test_the_rows_are_one_run_whatever_the_reader() -> None:
    # A fresh reader for each slice, as a pass has, reads the same parents and the same layout.
    plans = plans_for(entry(end=FINAL))
    plan = plans[EMIT]
    whole = generate_row_slice(plan, 0, plan.rows, root=root_for())
    pieces: List[Any] = []
    for start in range(0, plan.rows, 25):
        pieces.extend(generate_row_slice(plan, start, min(start + 25, plan.rows), root=root_for()))
    assert tuple(pieces) == tuple(whole)


# -- what plan says --------------------------------------------------------------------------------


def test_plan_counts_the_one_version_entities_that_end_with_their_parent() -> None:
    held = entry(versions=ChildShape("levels", ("1",), ("1",)), end=FINAL, entities=40)
    layout = layout_of(plans_for(held, rows=40))
    assert layout.cut is not None
    forced = int(layout.cut.forced.sum())
    assert forced > 0
    notes = history.parent_bound_notes(EMIT, layout)
    assert len(notes) == 1
    assert EMIT in notes[0] and f"{forced} entities end with their parent" in notes[0]


def test_plan_is_silent_when_the_end_carries_no_value_to_set() -> None:
    # closeLast closes the one version of such an entity: nothing changes for it.
    assert history.parent_bound_notes(EMIT, layout_of(plans_for(entry(end=CLOSE)))) == []


def test_the_refusal_for_a_bound_history_with_no_end_does_not_name_closelast() -> None:
    early = {"range": {"min": "2025-12-30", "max": "2025-12-30"}}
    with pytest.raises(Exception, match="parent ended") as caught:
        plans_for(entry(end=None, until="2023-01-02"), opened=early)
    assert "closeLast" not in str(caught.value)


# -- the room an entity's parent leaves it ----------------------------------------------------------

#: Parents that open on one of two days and, four fifths of them, never close; the others close
#: within a few days. With no shape every entity is given three versions.
SHORT_OPENED = {"range": {"min": "2024-03-01", "max": "2024-03-02"}}
SHORT_CLOSED = {"nullFraction": "0.8", "range": {"min": "2024-03-05", "max": "2024-03-12"}}
#: Parents that all close three to five days after they open.
TIGHT_CLOSED = {"range": {"min": "2024-03-05", "max": "2024-03-06"}}
#: With no closing unit to keep, a parent can close a day earlier and still leave room for a version.
SHORT_FINAL_CLOSED = {"nullFraction": "0.5", "range": {"min": "2024-03-04", "max": "2024-03-07"}}


def fitted(end: Optional[HistoryEnd] = None, **options: Any) -> Dict[str, GeneratorPlan]:
    options.setdefault("opened", SHORT_OPENED)
    options.setdefault("closed", SHORT_FINAL_CLOSED if end is FINAL else SHORT_CLOSED)
    return plans_for(entry(end=end, versions=None), **options)


def room_of(layout: HistoryPlan, held: HistoryEntry) -> "np.ndarray":
    """What an entity's chain holds: its room less the unit the first start and a closed end take."""
    return layout.rooms - (2 if layout.closes_last else 1)


@pytest.mark.parametrize("end", [None, CLOSE, FINAL])
def test_an_entity_is_capped_at_its_room_and_its_versions_move_to_entities_with_room(
    end: Optional[HistoryEnd],
) -> None:
    plans = fitted(end)
    layout = layout_of(plans)
    held = entry(end=end, versions=None)
    for (opened, closed), versions in chains(plans):
        stop = last_day(closed, opened)
        assert versions[0]["valid_from"] >= opened
        for row in versions:
            assert stop is None or row["valid_from"] <= stop
            if row["valid_to"] is not None and stop is not None:
                assert row["valid_to"] <= stop
    cap = room_of(layout, held)
    assert bool((layout.versions.counts <= cap).all())
    assert int(layout.versions.counts.sum()) == layout.rows == 120
    assert int(layout.versions.counts.max()) > 3  # the entities with room took what the others lost
    assert layout.cut is not None
    assert layout.cut.capped > 0 and layout.cut.moved > 0 and layout.cut.shortfall == 0


def test_the_versions_an_entity_loses_go_one_at_a_time_to_the_entities_with_the_most_room() -> None:
    layout = layout_of(fitted())
    counts, cap = layout.versions.counts, room_of(layout, entry(versions=None))
    taken = int((3 - counts[counts < 3]).sum())
    gained = counts - 3
    assert int(gained[gained > 0].sum()) == taken > 0
    # One at a time, the largest room first: an entity that gained has at least the room of one
    # that has room left and did not, and no entity gained two while another with as much room gained none.
    rooms = layout.rooms
    losers_free = np.flatnonzero((counts < cap) & (gained == 0))
    winners = np.flatnonzero(gained > 0)
    assert losers_free.shape[0] == 0 or int(rooms[winners].min()) >= int(rooms[losers_free].max())


def test_the_capped_layout_is_the_same_by_the_column_path_and_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = fitted(FINAL)[EMIT]
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_row_slice(plan, 0, plan.rows, root=root_for())
    )
    assert by_columns == by_rows and any(taken)
    assert generate_row_slice(plan, 17, 71, root=root_for()) == by_rows[17:71]


def test_a_parent_with_no_room_for_one_version_is_refused_naming_the_key() -> None:
    # Lifetimes of one or two days: the first start is at the latest the third day before the last.
    short = {"nullFraction": "0.5", "range": {"min": "2024-03-02", "max": "2024-03-03"}}
    with pytest.raises(Exception, match=r"FLX-3403") as caught:
        layout_of(fitted(closed=short))
    assert f"history ({EMIT}).parent.lifetime" in str(caught.value)
    assert "of 40 entities" in str(caught.value)


def test_the_rows_shrink_only_when_no_entity_has_room_and_a_plan_at_those_rows_agrees() -> None:
    layout = layout_of(fitted(closed=TIGHT_CLOSED))
    assert layout.cut is not None
    assert layout.rows < 120 and layout.cut.shortfall == 120 - layout.rows
    assert bool((layout.versions.counts == room_of(layout, entry())).all())
    notes = history.parent_bound_notes(EMIT, layout)
    assert len(notes) == 1 and f"{layout.rows} rows" in notes[0] and "120 declared" in notes[0]
    # Laid out again at the rows it has, the layout is the same one.
    again = layout_of(fitted(closed=TIGHT_CLOSED, rows=layout.rows, declared_rows=120))
    assert np.array_equal(again.versions.counts, layout.versions.counts)
    assert np.array_equal(again.first_starts, layout.first_starts)


def test_plan_counts_the_entities_capped_and_the_versions_moved() -> None:
    layout = layout_of(fitted())
    assert layout.cut is not None
    notes = history.parent_bound_notes(EMIT, layout)
    assert len(notes) == 1
    assert f"{layout.cut.capped} of {layout.entities} entities" in notes[0]
    assert f"{layout.cut.moved} versions moved" in notes[0]
