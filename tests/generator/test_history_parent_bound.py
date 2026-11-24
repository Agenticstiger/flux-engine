# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history keeps to its parent's lifetime (ADR 0017, amendment of 2026-11-24).

``parent.lifetime`` is the history's counterpart of ADR 0024's ``within`` with ``lifetime: true``.
With ``P = [ps, pe]`` the lifetime of an entity's parent row (``pe`` absent when the parent has no
end, a NULL end, or an end before its start), what is pinned, on kernel 0.2.0:

* **The first start** lies in ``J`` and in ``[ps, pe - c')``, drawn by the same draw on the same
  path and position as without the bound, so an entity whose interval is ``J`` draws what it drew.
* **The room** ends at ``min(until, pe)``: every closed version ends before the parent's last day.
* **The parent that ended** inside the horizon forces its entities to end: the last version of
  an entity is closed (``closeLast``, or no ``end``) or is the ending version (``finalVersion``),
  at the latest on the parent's last day, and they count towards the end share.
* **An empty intersection** leaves the entity the draw it would have without the bound, with no
  parent bound on its room, and counts it ``infeasible``.
* **Two paths, one answer**: the column path's rows equal the per-row path's, and a slice is the
  slice of the run.
"""

from __future__ import annotations

from collections import defaultdict
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
from flux_engine.generator.relations import relate_plans, related_columns
from flux_engine.generator.rows import (
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_row_slice,
)
from flux_engine.generator.rules import plan_rules
from flux_engine.seam.contract import LogicalKind, compile_expose
from flux_engine.seam.counts import ChildShape
from flux_engine.seam.history import (
    Histogram,
    HistoryEnd,
    HistoryEntry,
    HistoryParent,
)
from flux_engine.seam.keys import compile_keys, mark_key_columns

from .test_history_layout import BOUNDS, LEVELS, SIM_ID, UNTIL, root_for
from .test_relations import _column, _expose, _h, _primary, _rel
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
    *,
    entities: int = 40,
    versions: Optional[ChildShape] = LEVELS,
    parent: Optional[HistoryParent] = BOUND_PARENT,
    end: Optional[HistoryEnd] = None,
    until: str = UNTIL,
) -> HistoryEntry:
    return HistoryEntry(
        emit=EMIT,
        entities=entities,
        length=LENGTH,
        until=until,
        parent=parent,
        versions=versions,
        end=end,
    )


def plans_for(
    held: HistoryEntry,
    *,
    rows: int = 120,
    customers: int = 12,
    opened: Optional[Mapping[str, Any]] = None,
    closed: Optional[Mapping[str, Any]] = None,
    lifetime: Optional[Tuple[str, Optional[str]]] = LIFETIME,
) -> Dict[str, GeneratorPlan]:
    document = contract()
    keys = compile_keys(document)
    assert keys is not None
    declared: Dict[str, Tuple[Dict[str, Any], int]] = {
        "customer": (
            {"opened": dict(opened or OPENED), "closed": dict(closed or CLOSED)},
            customers,
        ),
        EMIT: ({"status": {"levels": dict(STATUS)}}, rows),
    }
    plans: Dict[str, GeneratorPlan] = {}
    for expose in document["exposes"]:
        expose_id = expose["exposeId"]
        expose_keys = keys.expose(expose_id)
        compiled = mark_key_columns(compile_expose(expose), expose_keys)
        columns, count = declared[expose_id]
        related = related_columns(expose_keys) | ({"sub_id"} if expose_id == EMIT else set())
        plans[expose_id] = build_plan(
            compiled,
            sim_id=SIM_ID,
            seam_digest=_h(expose_id),
            rows=count,
            fidelity={"columns": columns},
            time_bounds=BOUNDS,
            related=related,
            rules=plan_rules(
                contract_document=document,
                expose_id=expose_id,
                settings=None,
                related=related,
                mapping=None,
                has_fidelity=True,
                strict=False,
                flux_rules=None,
                keys=expose_keys,
                rows=count,
                null_fractions={},
                counter_based=True,
                links=(),
                copies=False,
            ),
            generator_version=GENERATOR_VERSION_2,
        )
    binding = HistoryBinding(
        held,
        held.entities,
        "valid_from",
        "valid_to",
        lifetime if held.parent is not None and held.parent.lifetime else None,
    )
    return relate_plans(plans, keys, None, None, None, document, None, {EMIT: binding})


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


def by_entity(data: List[Dict[str, Any]]) -> Dict[Any, List[Dict[str, Any]]]:
    found: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for row in data:
        found[row["sub_id"]].append(row)
    return found


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
    assert layout.cut is not None and not bool(layout.cut.infeasible.any())
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
    assert int(layout.cut.forced.sum()) > 0
    # 25 % of every eligible entity, the forced ones included, end; the rest are the permutation's.
    eligible = int(((layout.versions.counts >= 2) | layout.cut.forced).sum())
    assert int(layout.ended.sum()) == max(eligible // 4, int(layout.cut.forced.sum())) or (
        abs(int(layout.ended.sum()) - eligible / 4) < 1
    )


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


def test_an_empty_intersection_keeps_the_draw_without_the_bound_and_is_infeasible() -> None:
    # Every parent opens in the last days before until, where no chain has room for its versions.
    late = {"range": {"min": "2025-12-25", "max": "2025-12-30"}}
    never = {"nullFraction": "1", "range": {"min": "2025-12-30", "max": "2025-12-30"}}
    end = HistoryEnd("closeLast", "0")
    bound = layout_of(plans_for(entry(end=end), opened=late, closed=never))
    plain = layout_of(plans_for(entry(end=end, parent=PLAIN_PARENT), opened=late, closed=never))
    assert bound.cut is not None and bool(bound.cut.infeasible.all())
    assert bound.cut is not None and not bool(bound.cut.forced.any())
    assert np.array_equal(bound.first_starts, plain.first_starts)
    assert bound.cut is not None and bool((bound.cut.limits == bound.until).all())


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


def test_plan_counts_the_entities_whose_parent_leaves_no_room() -> None:
    late = {"range": {"min": "2025-12-25", "max": "2025-12-30"}}
    never = {"nullFraction": "1", "range": {"min": "2025-12-30", "max": "2025-12-30"}}
    layout = layout_of(
        plans_for(entry(end=HistoryEnd("closeLast", "0")), opened=late, closed=never)
    )
    notes = history.parent_bound_notes(EMIT, layout)
    assert len(notes) == 1 and f"{layout.entities} of {layout.entities} entities" in notes[0]


def test_the_refusal_for_a_bound_history_with_no_end_does_not_name_closelast() -> None:
    early = {"range": {"min": "2025-12-30", "max": "2025-12-30"}}
    with pytest.raises(Exception, match="parent ended") as caught:
        plans_for(entry(end=None, until="2023-01-02"), opened=early)
    assert "closeLast" not in str(caught.value)
