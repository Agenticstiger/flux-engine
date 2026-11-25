# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How a history ends, and the activation (ADR 0017 section 5, KERNEL-SPEC-2 section 8.4).

An ``end`` ends a share of every entity by ``until``: ``n = LRM(E, [share, 1 - share])[0]``, chosen
among the ``E'`` eligible ones, the entity of eligible rank ``r`` ending when
``permute/2(K_end, E', r) < n``; when fewer than ``n`` are eligible every eligible entity ends.
What is pinned, on kernel 0.2.0:

* **The count.** The ended entities are exactly ``n`` of all the entities (or every eligible one
  when fewer are eligible), from the layout alone, and which ones is the permutation's.
* **The eligibility.** ``closeLast`` ends any entity, or with a ``column`` those with at least two
  versions; ``finalVersion`` ends those with at least two versions. A share with no eligible
  entity is refused at plan time.
* **The ending version.** With a ``column`` an ended entity's last version takes ``value``, and no
  other version does.
* **The modes.** Under ``closeLast`` an ended entity's last version is closed with its own drawn
  length, at ``s_last + d_last`` (``nextStart``) or the day before (``dayBefore``), inside the fit.
  Under ``finalVersion`` it stays open, so every entity keeps one open version.
* **The fit.** ``c' = c + 1`` leaves every entity room for a chain whose last version is closed.
* **The activation** holds the first start on every version.
* **Two paths, one answer.** The column path's rows equal the per-row path's.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.core.lrm import largest_remainder
from flux_engine.core.permute import permute2
from flux_engine.generator import history, relations
from flux_engine.generator.history import (
    HistoryBinding,
    HistoryPlan,
    drawn_length,
    ended_count,
    ended_key,
    entity_chain,
    fit_lengths,
    plan_history,
    unit_of,
)
from flux_engine.generator.rows import (
    GeneratorPlan,
    generate_row_slice,
    generate_rows,
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
from .test_history_layout import (
    BOUNDS,
    SIM_ID,
    UNTIL,
    _column,
    _expose,
    _primary,
    _rel,
    entry,
    root_for,
    table,
)
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit

EMIT = "sub_version"
LENGTH = Histogram(("30", "90"), ("1",))
LONG = Histogram(("400", "900"), ("1",))
DAY_MS = 86_400_000
PARENT = HistoryParent("customer", min_children=1)
STATUS = {"active": "0.7", "paused": "0.3", "gone": "0"}
PLAN = {"basic": "0.5", "pro": "0.3", "team": "0.2"}
TIER = {"low": "0.6", "high": "0.4"}


PRICE = {
    "id": "price_of_plan",
    "derive": "price",
    "expr": "CASE WHEN plan = 'basic' THEN 10 WHEN plan = 'pro' THEN 30 ELSE 50 END",
}


def contract(priced: bool = False) -> Dict[str, Any]:
    """A contract whose versioned emit has tracked, ended, carried and foreign-key columns.

    With *priced* the emit also has a ``price`` that a rule derives from the plan.
    """
    customer = _expose(
        "customer",
        [_column("customer_id", "INT"), _column("c_name", "STRING", False)],
        _primary("customer", "customer_id"),
    )
    store = _expose(
        "store",
        [_column("store_id", "INT"), _column("s_name", "STRING", False)],
        _primary("store", "store_id"),
    )
    version = _expose(
        EMIT,
        [
            _column("version_id", "INT"),
            _column("sub_id", "STRING"),
            _column("customer_id", "INT", False),
            _column("store_id", "INT", False),
            _column("plan", "STRING", False),
            _column("tier", "STRING", False),
            _column("status", "STRING", False),
            _column("region", "INT", False),
            _column("activated", "DATE", False),
            _column("valid_from", "DATE"),
            _column("valid_to", "DATE", False),
            *([_column("price", "INT", False)] if priced else []),
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
            "relationships": [
                _rel("customer", "customer", ["customer_id"], ["customer_id"]),
                _rel("store", "store", ["store_id"], ["store_id"]),
            ],
        },
    )
    if priced:
        version["contract"]["invariants"] = [dict(PRICE)]
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.history.end",
        "exposes": [customer, store, version],
    }


def declared(
    rows: int, customers: int, plan: Optional[Mapping[str, str]] = None
) -> Dict[str, Tuple[Dict[str, Any], int]]:
    return {
        "customer": ({"c_name": {"levels": {"n": "1"}}}, customers),
        "store": ({"s_name": {"levels": {"n": "1"}}}, 9),
        EMIT: (
            {
                "plan": {"levels": dict(plan or PLAN)},
                "tier": {"levels": dict(TIER)},
                "status": {"levels": dict(STATUS)},
                "region": {"range": {"min": "1", "max": "1000000"}},
            },
            rows,
        ),
    }


def plans_for(
    held: HistoryEntry,
    *,
    rows: int = 24,
    entities: int = 8,
    customers: int = 6,
    plan: Optional[Mapping[str, str]] = None,
    counter_based: bool = True,
    priced: bool = False,
    status: Optional[Mapping[str, str]] = None,
) -> Dict[str, GeneratorPlan]:
    """The plans of the contract above with *held* declared on the versioned emit."""
    declarations = declared(rows, customers, plan)
    if status is not None:
        declarations[EMIT][0]["status"] = {"levels": dict(status)}
    binding = {EMIT: HistoryBinding(held, entities, "valid_from", "valid_to")}
    return plans_of(contract(priced), declarations, binding, emit=EMIT, counter_based=counter_based)


@pytest.fixture(autouse=True)
def fresh_layouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test works its layouts out afresh: the caches are per process."""
    monkeypatch.setattr(relations, "_LAYOUTS", {})
    monkeypatch.setattr(history, "_PLANS", {})
    monkeypatch.setattr(history, "_OUTERS", {})


def history_plan(held: HistoryEntry, *, rows: int, entities: int, seed: int = 5) -> HistoryPlan:
    return plan_history(
        root_for(seed),
        SIM_ID,
        EMIT,
        held,
        kind=LogicalKind.DATE,
        rows=rows,
        entities=entities,
        time_bounds=BOUNDS,
    )


def run_of(
    plans: Mapping[str, GeneratorPlan], seed: int = 5, *, start: int = 0, stop: Optional[int] = None
) -> List[Dict[str, Any]]:
    plan = plans[EMIT]
    return table(
        plan,
        generate_row_slice(plan, start, plan.rows if stop is None else stop, root=root_for(seed)),
    )


def ending_rows(data: List[Dict[str, Any]], value: str = "gone") -> List[Dict[str, Any]]:
    return [row for row in data if row["status"] == value]


CLOSE = HistoryEnd("closeLast", "0.25")
CLOSE_WITH = HistoryEnd("closeLast", "0.25", "status", "gone")
FINAL = HistoryEnd("finalVersion", "0.25", "status", "gone")


# -- the count ------------------------------------------------------------------------------------


@pytest.mark.parametrize("share", ["0", "0.1", "0.25", "0.33", "0.5", "0.9", "1"])
def test_the_ended_entities_are_the_largest_remainder_count_of_every_entity_up_to_the_eligible(
    share: str,
) -> None:
    for end in (
        HistoryEnd("closeLast", share),
        HistoryEnd("finalVersion", share, "status", "gone"),
    ):
        held = entry(versions=ChildShape("levels", ("1", "3"), ("0.5", "0.5")), end=end)
        layout = history_plan(held, rows=64, entities=32)
        assert layout.ended is not None
        counts = layout.versions.counts
        eligible = int((counts >= 2).sum()) if end.mode == "finalVersion" else 32
        of_all = largest_remainder(
            32, {"0": Fraction(share), "1": 1 - Fraction(share)}  # type: ignore[dict-item]
        )["0"]
        wanted = min(of_all, eligible)
        assert int(layout.ended.sum()) == wanted == ended_count(32, eligible, share)
        # Only eligible entities end.
        if end.mode == "finalVersion":
            assert bool(((counts >= 2) | ~layout.ended).all())


def test_which_entities_end_is_the_permutations_not_a_draw() -> None:
    held = entry(versions=None, end=HistoryEnd("closeLast", "0.25"))
    layout = history_plan(held, rows=24, entities=8)
    key = ended_key(root_for(), SIM_ID, EMIT)
    assert layout.ended is not None
    assert layout.ended.tolist() == [permute2(key, 8, rank) < 2 for rank in range(8)]
    # The array path of the permutation agrees.
    images = kernel2.permute2_array(key, 8, np.arange(8, dtype=np.int64))
    assert layout.ended.tolist() == [image < 2 for image in images]


def test_the_ended_entities_follow_the_rank_among_the_eligible_ones_only() -> None:
    held = entry(end=FINAL, versions=ChildShape("levels", ("1", "3"), ("0.5", "0.5")))
    layout = history_plan(held, rows=64, entities=32)
    assert layout.ended is not None
    eligible = np.nonzero(layout.versions.counts >= 2)[0]
    key = ended_key(root_for(), SIM_ID, EMIT)
    ranks = len(eligible)
    wanted = ended_count(32, ranks, "0.25")
    expected = sorted(int(eligible[r]) for r in range(ranks) if permute2(key, ranks, r) < wanted)
    assert np.nonzero(layout.ended)[0].tolist() == expected


def test_the_ended_entities_do_not_move_with_the_window_or_the_scale_of_nothing_else() -> None:
    held = entry(end=CLOSE)
    first = history_plan(held, rows=24, entities=8)
    again = history_plan(held, rows=24, entities=8)
    assert first.ended is not None and again.ended is not None
    assert first.ended.tolist() == again.ended.tolist()


def test_an_entry_without_an_end_ends_nothing() -> None:
    layout = history_plan(entry(), rows=24, entities=8)
    assert layout.ended is None and not layout.closes_last and not layout.forces_ending


# -- the eligibility ------------------------------------------------------------------------------


def test_close_last_without_a_column_makes_every_entity_eligible() -> None:
    held = entry(end=HistoryEnd("closeLast", "1"))
    layout = history_plan(held, rows=24, entities=8)
    assert layout.ended is not None and bool(layout.ended.all())
    # An entity of one version ends too: its only version is closed.
    ones = [e for e in range(8) if int(layout.versions.counts[e]) == 1]
    assert ones
    rows = by_entity(run_of(plans_for(held)))
    for entity in ones:
        (only,) = list(rows.values())[entity]
        assert only["valid_to"] is not None
    half = history_plan(entry(end=HistoryEnd("closeLast", "0.5")), rows=24, entities=8)
    assert half.ended is not None and int(half.ended.sum()) == 4


def test_close_last_with_a_column_ends_only_entities_of_two_versions() -> None:
    held = entry(end=HistoryEnd("closeLast", "1", "status", "gone"))
    layout = history_plan(held, rows=24, entities=8)
    assert layout.ended is not None
    assert layout.ended.tolist() == (layout.versions.counts >= 2).tolist()


def test_final_version_ends_only_entities_of_two_versions() -> None:
    held = entry(end=HistoryEnd("finalVersion", "1", "status", "gone"))
    layout = history_plan(held, rows=24, entities=8)
    assert layout.ended is not None
    assert layout.ended.tolist() == (layout.versions.counts >= 2).tolist()


def test_a_share_of_every_entity_ends_every_eligible_one_when_fewer_are_eligible() -> None:
    # Half the entities hold one version, so a share of 0.75 of all 32 asks for 24 and only about
    # 16 can end: every eligible entity ends, and the share yielded is lower than the declared one.
    held = entry(
        versions=ChildShape("levels", ("1", "3"), ("0.5", "0.5")),
        end=HistoryEnd("finalVersion", "0.75", "status", "gone"),
    )
    layout = history_plan(held, rows=64, entities=32)
    assert layout.ended is not None
    eligible = int((layout.versions.counts >= 2).sum())
    assert eligible < 24
    assert layout.ended.tolist() == (layout.versions.counts >= 2).tolist()


def test_a_share_with_no_eligible_entity_ends_none_and_plans() -> None:
    # Eight entities of one version each: none has two versions to end on, so none ends.
    for share in ("0.5", "0"):
        plans = plans_for(
            entry(versions=None, end=HistoryEnd("finalVersion", share, "status", "gone")),
            rows=8,
            entities=8,
        )
        assert plans[EMIT].rows == 8


# -- the ending version ---------------------------------------------------------------------------


def test_an_ended_entitys_last_version_takes_the_value_and_no_other_version_does() -> None:
    for end in (CLOSE_WITH, FINAL):
        plans = plans_for(entry(end=end))
        layout = history_plan(entry(end=end), rows=24, entities=8)
        data = run_of(plans)
        assert layout.ended is not None
        groups = list(by_entity(data).values())
        for entity, rows in enumerate(groups):
            values = [row["status"] for row in rows]
            if layout.ended[entity]:
                assert values[-1] == "gone" and "gone" not in values[:-1]
            else:
                assert "gone" not in values
        assert len(ending_rows(data)) == int(layout.ended.sum())


def test_close_last_without_a_column_writes_no_value() -> None:
    data = run_of(plans_for(entry(end=CLOSE)))
    assert ending_rows(data) == []


# -- the modes ------------------------------------------------------------------------------------


def day(value: Any) -> int:
    return unit_of(value.isoformat(), LogicalKind.DATE)


@pytest.mark.parametrize("chain", ["nextStart", "dayBefore"])
def test_close_last_closes_an_ended_entitys_last_version_at_its_own_length(chain: str) -> None:
    held = entry(end=CLOSE, chain=chain)
    plans = plans_for(held)
    layout = history_plan(held, rows=24, entities=8)
    assert layout.ended is not None and layout.closes_last
    root = root_for()
    step = 1 if chain == "dayBefore" else 0
    for entity, rows in enumerate(by_entity(run_of(plans)).values()):
        ends = [row["valid_to"] for row in rows]
        if layout.ended[entity]:
            assert None not in ends
            last = rows[-1]
            chain_ = entity_chain(root, SIM_ID, EMIT, held, LogicalKind.DATE, layout, entity)
            assert len(chain_.ends) == len(rows) and chain_.ends[-1] is not None
            # The closed end of the last version is its start plus its own length, less the step,
            # unless the fit compressed the chain.
            count = len(rows)
            drawn = [
                drawn_length(root, SIM_ID, EMIT, held, LogicalKind.DATE, entity, v)
                for v in range(count)
            ]
            room = layout.until - int(layout.first_starts[entity])
            fitted = fit_lengths(drawn, room)
            assert day(last["valid_to"]) == day(last["valid_from"]) + fitted[-1] - step
            assert day(last["valid_to"]) + step <= layout.until - 1
        else:
            assert [end is None for end in ends] == [False] * (len(rows) - 1) + [True]


@pytest.mark.parametrize("chain", ["nextStart", "dayBefore"])
def test_final_version_leaves_every_entity_one_open_version(chain: str) -> None:
    held = entry(end=FINAL, chain=chain)
    data = run_of(plans_for(held))
    for rows in by_entity(data).values():
        ends = [row["valid_to"] for row in rows]
        assert [end is None for end in ends] == [False] * (len(rows) - 1) + [True]
    assert sum(1 for row in data if row["valid_to"] is None) == 8


@pytest.mark.parametrize("chain", ["nextStart", "dayBefore"])
@pytest.mark.parametrize("mode", ["closeLast", "finalVersion"])
def test_the_chain_holds_under_both_modes_and_both_chains(mode: str, chain: str) -> None:
    end = HistoryEnd(mode, "0.5", "status", "gone")
    held = entry(end=end, chain=chain, versions=ChildShape("levels", ("1", "3"), ("0.5", "0.5")))
    for seed in (1, 2, 3):
        plans = plans_for(held, rows=64, entities=32, customers=6)
        data = run_of(plans, seed)
        until = unit_of(UNTIL, LogicalKind.DATE)
        step = 1 if chain == "dayBefore" else 0
        for rows in by_entity(data).values():
            starts = [day(row["valid_from"]) for row in rows]
            assert starts == sorted(set(starts)) and all(s < until for s in starts)
            for index, row in enumerate(rows[:-1]):
                assert day(row["valid_to"]) == starts[index + 1] - step
            last = rows[-1]["valid_to"]
            if last is not None:
                assert day(last) + step <= until - 1
        opened = sum(1 for row in data if row["valid_to"] is None)
        ended = int(history_plan(held, rows=64, entities=32, seed=seed).ended.sum())  # type: ignore[union-attr]
        assert opened == (32 - ended if mode == "closeLast" else 32)


def test_a_compressed_closed_last_version_still_ends_before_until() -> None:
    held = entry(end=HistoryEnd("closeLast", "1"), length=LONG)
    plans = plans_for(held, rows=24, entities=8)
    until = unit_of(UNTIL, LogicalKind.DATE)
    layout = history_plan(held, rows=24, entities=8)
    compressed = 0
    root = root_for()
    for entity, rows in enumerate(by_entity(run_of(plans)).values()):
        assert rows[-1]["valid_to"] is not None
        assert day(rows[-1]["valid_to"]) < until
        drawn = [
            drawn_length(root, SIM_ID, EMIT, held, LogicalKind.DATE, entity, v)
            for v in range(len(rows))
        ]
        if sum(drawn) > until - int(layout.first_starts[entity]) - 1:
            compressed += 1
            assert day(rows[-1]["valid_to"]) == until - 1
    assert compressed >= 1


def test_the_planners_extra_unit_keeps_the_fit_infallible_under_close_last() -> None:
    # The largest chain: c versions, all closed, in the least room the first starts leave: c' + 1.
    held = entry(end=HistoryEnd("closeLast", "1"), length=LONG)
    layout = history_plan(held, rows=24, entities=8)
    assert int(layout.rooms.min()) >= layout.c + 2
    # So every closed chain has a unit a version and one over.
    for entity in range(8):
        count = int(layout.versions.counts[entity])
        fit = fit_lengths([5000] * count, int(layout.rooms[entity]))
        assert len(fit) == count and sum(fit) == int(layout.rooms[entity]) - 1


@pytest.mark.parametrize("seed", range(6))
def test_no_chain_is_ever_refused_under_close_last(seed: int) -> None:
    held = entry(end=HistoryEnd("closeLast", "1"), length=LONG, versions=None)
    data = run_of(plans_for(held, rows=40, entities=8), seed)
    assert len(data) == 40


# -- the activation -------------------------------------------------------------------------------


def test_the_activation_holds_the_first_start_on_every_version() -> None:
    held = entry(activation="activated", end=CLOSE)
    data = run_of(plans_for(held))
    for rows in by_entity(data).values():
        assert {row["activated"] for row in rows} == {rows[0]["valid_from"]}
    layout = history_plan(held, rows=24, entities=8)
    firsts = [day(rows[0]["activated"]) for rows in by_entity(data).values()]
    assert firsts == layout.first_starts.tolist()


# -- two paths, one answer ------------------------------------------------------------------------


def variants() -> List[Tuple[str, HistoryEntry]]:
    return [
        ("closeLast", entry(end=CLOSE, activation="activated")),
        ("closeLast, dayBefore", entry(end=CLOSE, chain="dayBefore")),
        ("closeLast, a column", entry(end=CLOSE_WITH)),
        ("finalVersion", entry(end=FINAL, activation="activated")),
        ("finalVersion, dayBefore", entry(end=FINAL, chain="dayBefore", length=LONG)),
        ("no parent", entry(end=FINAL, parent=None)),
        ("no shape", entry(end=CLOSE_WITH, versions=None)),
    ]


@pytest.mark.parametrize("name", [name for name, _ in variants()])
def test_the_column_path_writes_the_rows_the_per_row_path_writes(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    held = dict(variants())[name]
    plans = plans_for(held, rows=60, entities=12)
    plan = plans[EMIT]
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_row_slice(plan, 0, plan.rows, root=root_for())
    )
    assert taken == [True]
    assert by_columns == by_rows
    monkeypatch.setattr("flux_engine.generator.columns.CHUNK_ROWS", 7)
    assert generate_row_slice(plan, 0, plan.rows, root=root_for()) == by_rows
    assert generate_row_slice(plan, 13, 41, root=root_for()) == by_rows[13:41]


def test_a_slice_is_the_slice_of_the_whole_run() -> None:
    plans = plans_for(entry(end=CLOSE_WITH, activation="activated"), rows=60, entities=12)
    whole = run_of(plans)
    assert run_of(plans, start=17, stop=43) == whole[17:43]


def test_generate_rows_is_the_run_of_slices() -> None:
    plan = plans_for(entry(end=FINAL), rows=60, entities=12)[EMIT]
    assert generate_rows(plan, root=root_for()) == generate_row_slice(plan, 0, 60, root=root_for())
