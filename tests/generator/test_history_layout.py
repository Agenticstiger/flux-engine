# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's emit is laid out as entities and their versions (ADR 0017 section 3, KERNEL-SPEC-2 section 8.4).

A ``history`` entry lays an emit's rows out with the counted layout twice, one inside the other:
entities per parent row, and versions per entity. The planner, the keys and the parent's foreign key
are tested here through :func:`flux_engine.generator.relations.relate_plans` and the generator's own
functions, since the bundle's key still refuses a declared history until the chain is complete.
What is pinned, on kernel 0.2.0 (the only kernel that generates it):

* **The counts.** Exact counts and ``c`` from declared values; the rank coupling (the entity with
  the most room takes the most versions) and the even spread without a shape.
* **The nesting.** A row belongs to an entity and an entity to a parent row; an entity's versions
  and a parent's entities are consecutive rows.
* **The keys.** The natural entity's column is one value per entity, copied on every version; the
  range check of a key counts the entities; the parent's foreign key draws nothing.
* **The refusals**, each before any row exists: no entity for the rows, a parent with no rows, an
  empty first-start interval; and a scale that takes the entities to zero.
* **No window end.** The planner never reads ``timeBounds.end``.
* **Two paths, one answer.** The column path's rows equal the per-row path's.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from fractions import Fraction
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SeedTreeRoot, derive_root, digest_bytes
from flux_engine.generator import history, relations
from flux_engine.generator.counts import (
    TooFewChildRows,
    expected_count_hooks,
    register_expected_count_hook,
    sigma_of,
)
from flux_engine.generator.history import (
    HistoryBinding,
    HistoryPlan,
    OuterInputs,
    first_start_at,
    largest_count,
    plan_history,
    unit_of,
    versions_key,
)
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.relations import (
    EntityColumn,
    ForeignKey,
    relate_plans,
    related_columns,
)
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_column,
    generate_row_slice,
    generate_rows,
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
from flux_engine.stages.scaling import _scaled_rows

from .test_relations import _column, _expose, _h, _primary, _rel
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit

SIM_ID = "sim-history"
EMIT = "sub_version"
BOUNDS = {"start": "2023-01-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"}
CUSTOMERS = 6
ENTITIES = 8
VERSIONS = 24
LENGTH = Histogram(("30", "90"), ("1",))
LEVELS = ChildShape("levels", ("1", "3"), ("0.5", "0.5"))
UNTIL = "2025-12-31"
PARENT = HistoryParent("customer", min_children=1)


def entry(
    *,
    emit: str = EMIT,
    entities: int = ENTITIES,
    length: Histogram = LENGTH,
    versions: Optional[ChildShape] = LEVELS,
    parent: Optional[HistoryParent] = PARENT,
    until: str = UNTIL,
    **rest: Any,
) -> HistoryEntry:
    """A history entry: eight entities under ``customer``, with the other fields as ``rest`` names."""
    return HistoryEntry(
        emit=emit,
        entities=entities,
        length=length,
        until=until,
        parent=parent,
        versions=versions,
        **rest,
    )


def root_for(seed: int = 5) -> SeedTreeRoot:
    return derive_root(
        digest_bytes("sha256:" + "a" * 64), seed, seed_tree_version=kernel2.SEED_TREE_VERSION
    )


def contract(natural_type: str = "STRING", invoices: bool = False) -> Dict[str, Any]:
    customer = _expose(
        "customer",
        [
            _column("customer_id", "INT"),
            _column("c_name", "STRING", False),
            _column("c_score", "INT", False),
        ],
        _primary("customer", "customer_id"),
    )
    version = _expose(
        EMIT,
        [
            _column("version_id", "INT"),
            _column("sub_id", natural_type),
            _column("customer_id", "INT", False),
            _column("plan", "STRING", False),
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
    exposes = [customer, version]
    if invoices:
        # A table that reads the natural key: it makes the key a key the layout keeps unique.
        exposes.append(
            _expose(
                "invoice",
                [_column("i_id", "INT"), _column("i_sub", natural_type, False)],
                {
                    **_primary("invoice", "i_id"),
                    "relationships": [_rel("sub", EMIT, ["i_sub"], ["sub_id"])],
                },
            )
        )
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.history",
        "exposes": exposes,
    }


def plans_of(
    *,
    history_entry: Optional[HistoryEntry] = None,
    entities: Optional[int] = None,
    rows: int = VERSIONS,
    customers: int = CUSTOMERS,
    natural_type: str = "STRING",
    natural: Optional[Dict[str, Any]] = None,
    counter_based: bool = True,
    namespace: Optional[Mapping[str, Any]] = None,
    document: Optional[Dict[str, Any]] = None,
    bounds: Optional[Mapping[str, str]] = BOUNDS,
    no_history: bool = False,
    invoices: bool = False,
) -> Dict[str, GeneratorPlan]:
    document = contract(natural_type, invoices) if document is None else document
    keys = compile_keys(document)
    assert keys is not None
    held = history_entry if history_entry is not None else entry()
    declared: Dict[str, Tuple[Dict[str, Any], int]] = {
        "customer": (
            {"c_name": {"levels": {"n": "1"}}, "c_score": {"range": {"min": "1", "max": "9"}}},
            customers,
        ),
        EMIT: (
            {
                "plan": {"levels": {"a": "1"}},
                **({"sub_id": natural} if natural else {}),
                **(
                    {}
                    if bounds is not None
                    else {
                        "valid_from": {"range": {"min": "2023-01-01", "max": "2025-12-31"}},
                        "valid_to": {"range": {"min": "2023-01-01", "max": "2025-12-31"}},
                    }
                ),
            },
            rows,
        ),
        "invoice": ({}, 30),
    }
    version = GENERATOR_VERSION_2 if counter_based else GENERATOR_VERSION
    plans: Dict[str, GeneratorPlan] = {}
    for expose in document["exposes"]:
        expose_id = expose["exposeId"]
        expose_keys = keys.expose(expose_id)
        compiled = mark_key_columns(compile_expose(expose), expose_keys)
        columns_, count = declared[expose_id]
        related = related_columns(expose_keys) | ({"sub_id"} if expose_id == EMIT else set())
        plans[expose_id] = build_plan(
            compiled,
            sim_id=SIM_ID,
            seam_digest=_h(expose_id),
            rows=count,
            fidelity={"columns": columns_},
            time_bounds=bounds,
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
                counter_based=counter_based,
                links=(),
                copies=False,
            ),
            generator_version=version,
        )
    binding = {}
    if not no_history:
        binding = {
            EMIT: HistoryBinding(
                held, held.entities if entities is None else entities, "valid_from"
            )
        }
    return relate_plans(plans, keys, None, None, None, document, namespace, binding)


@pytest.fixture(autouse=True)
def fresh_layouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test works its layouts out afresh: the caches are per process."""
    monkeypatch.setattr(relations, "_LAYOUTS", {})
    monkeypatch.setattr(history, "_PLANS", {})
    monkeypatch.setattr(history, "_OUTERS", {})


def table(plan: GeneratorPlan, rows: Any) -> List[Dict[str, Any]]:
    names = [column_plan.column.name for column_plan in plan.columns]
    return [dict(zip(names, row, strict=True)) for row in rows]


def refused(change: Callable[[], Any], code: str = "FLX-3403") -> str:
    with pytest.raises(FluxError) as caught:
        change()
    assert caught.value.code.code == code, str(caught.value)
    return str(caught.value)


def layout_of(
    plans: Mapping[str, GeneratorPlan], seed: int = 5
) -> Tuple[HistoryPlan, ForeignKey, EntityColumn]:
    """The history plan the version emit's drivers give, through a reader as a pass has one."""
    plan = plans[EMIT]
    assert plan.relations is not None
    reader = relations.RelationReader(root_for(seed))
    reader.know(plan)
    owner = relations.owner_of(plan)
    foreign = plan.relations.driver("customer_id")
    natural = plan.relations.driver("sub_id")
    assert isinstance(foreign, ForeignKey) and isinstance(natural, EntityColumn)
    return reader._history_of(owner, foreign), foreign, natural


# -- the counts ---------------------------------------------------------------------------------


def test_the_counts_are_exact_and_c_is_worked_out_from_declared_values() -> None:
    # Eight entities take four slots of 1 and four of 3; with a minimum of 1 the 16 spare
    # versions weigh 0 and 2, so the heavy half takes 4 more each: 1 and 5.
    plans = plans_of()
    layout, _, _ = layout_of(plans)
    assert sorted(layout.versions.counts.tolist()) == [1, 1, 1, 1, 5, 5, 5, 5]
    assert layout.versions.children == VERSIONS == int(layout.versions.counts.sum())
    assert layout.c == largest_count(ENTITIES, VERSIONS, LEVELS) == 5
    # Without a shape, 25 versions over 8 entities: one takes 4, the others 3.
    assert largest_count(8, 25, None) == 4
    assert largest_count(8, 24, None) == 3
    assert largest_count(0, 0, LEVELS) == 0


def test_c_is_the_largest_count_whatever_entity_takes_it() -> None:
    bins = ChildShape("bins", ("1", "2", "4", "9"), ("0.45", "0.35", "0.20"))
    for shape in (LEVELS, bins):
        for seed in range(12):
            plans = plans_of(history_entry=entry(versions=shape), rows=40, entities=10)
            layout, _, _ = layout_of(plans, seed)
            assert int(layout.versions.counts.max()) <= layout.c
            assert layout.c == largest_count(10, 40, shape)


def test_without_a_shape_the_versions_spread_as_evenly_as_whole_counts_allow() -> None:
    plans = plans_of(history_entry=entry(versions=None), rows=25)
    layout, _, _ = layout_of(plans)
    counts = layout.versions.counts.tolist()
    assert sorted(counts) == [3] * 7 + [4]
    assert layout.c == 4


def test_the_entity_with_the_most_room_takes_the_most_versions() -> None:
    bins = ChildShape("bins", ("1", "2", "4", "9"), ("0.45", "0.35", "0.20"))
    plans = plans_of(history_entry=entry(versions=bins), rows=40, entities=10)
    layout, _, _ = layout_of(plans)
    sigma = sigma_of(versions_key(root_for(), SIM_ID, EMIT), 10)
    order = sorted(range(10), key=lambda e: (int(layout.rooms[e]), sigma[e]))
    counts = [int(layout.versions.counts[e]) for e in order]
    assert counts == sorted(counts)
    assert counts[-1] > counts[0]


def test_every_entity_has_room_for_its_chain() -> None:
    plans = plans_of()
    layout, _, _ = layout_of(plans)
    assert int(layout.rooms.min()) >= layout.c + 1
    assert int(layout.first_starts.min()) >= unit_of("2023-01-01", LogicalKind.DATE)


# -- the first start ----------------------------------------------------------------------------


def test_the_first_start_is_drawn_inside_j_and_closing_the_last_version_shrinks_it() -> None:
    until = unit_of(UNTIL, LogicalKind.DATE)
    plain = layout_of(plans_of())[0]
    closed = layout_of(
        plans_of(history_entry=entry(end=HistoryEnd("closeLast", "0.1"))),
    )[0]
    assert int(plain.first_starts.max()) <= until - plain.c - 1
    assert int(closed.first_starts.max()) <= until - closed.c - 2
    assert int(closed.first_starts.min()) >= unit_of("2023-01-01", LogicalKind.DATE)


def test_the_start_histogram_decides_where_the_first_starts_fall() -> None:
    start = Histogram(("2023-01-01", "2024-01-01", "2025-07-01"), ("0", "1"))
    layout = layout_of(plans_of(history_entry=entry(start=start)))[0]
    low = unit_of("2024-01-01", LogicalKind.DATE)
    assert int(layout.first_starts.min()) >= low
    assert int(layout.first_starts.max()) <= unit_of(UNTIL, LogicalKind.DATE) - layout.c - 1


def test_a_start_histogram_no_bin_of_meets_j_falls_back_to_a_uniform_draw() -> None:
    # Every bin lies past the last day J holds, so the entity draws uniformly over J.
    start = Histogram(("2025-12-30", "2025-12-31"), ("1",))
    plain = layout_of(plans_of())[0]
    beyond = layout_of(plans_of(history_entry=entry(start=start)))[0]
    assert beyond.first_starts.tolist() == plain.first_starts.tolist()


@pytest.mark.parametrize("closed", ["lower", "upper"])
def test_the_per_entity_draw_is_the_planners(closed: str) -> None:
    start = Histogram(("2023-06-01", "2024-01-01", "2025-07-01"), ("0.55", "0.45"), closed)
    held = entry(start=start)
    plans = plans_of(history_entry=held)
    layout, _, _ = layout_of(plans)
    for entity in range(ENTITIES):
        assert (
            first_start_at(
                root_for(),
                SIM_ID,
                EMIT,
                held,
                LogicalKind.DATE,
                entity,
                c=layout.c,
                time_bounds=BOUNDS,
            )
            == layout.first_starts[entity]
        )
    plain = plans_of()
    plain_layout, _, _ = layout_of(plain)
    assert first_start_at(
        root_for(),
        SIM_ID,
        EMIT,
        entry(),
        LogicalKind.DATE,
        3,
        c=plain_layout.c,
        time_bounds=BOUNDS,
    ) == int(plain_layout.first_starts[3])


def test_a_timestamp_chain_draws_in_milliseconds() -> None:
    held = entry(until="2025-12-31T00:00:00Z")
    layout = plan_history(
        root_for(),
        SIM_ID,
        EMIT,
        held,
        kind=LogicalKind.TIMESTAMP,
        rows=VERSIONS,
        entities=ENTITIES,
        time_bounds=BOUNDS,
        outer=OuterInputs("customer", CUSTOMERS, minimum=1),
    )
    start = unit_of("2023-01-01T00:00:00Z", LogicalKind.TIMESTAMP)
    until = unit_of("2025-12-31T00:00:00Z", LogicalKind.TIMESTAMP)
    assert start <= int(layout.first_starts.min())
    assert int(layout.first_starts.max()) <= until - layout.c - 1
    assert len(set(layout.first_starts.tolist())) == ENTITIES


# -- nesting ------------------------------------------------------------------------------------


def test_rows_run_in_parent_order_then_entity_order_then_version_order() -> None:
    plans = plans_of()
    layout, _, _ = layout_of(plans)
    entities = [layout.entity_of(row) for row in range(VERSIONS)]
    assert entities == sorted(entities)
    parents = [int(layout.parent_row_of(row) or 0) for row in range(VERSIONS)]
    assert parents == sorted(parents)
    for entity in range(ENTITIES):
        rows = layout.versions.children_of(entity)
        assert [layout.entity_of(row) for row in rows] == [entity] * len(rows)
        assert [layout.version_of(row) for row in rows] == list(range(len(rows)))
    assert layout.per_parent is not None
    for parent in range(CUSTOMERS):
        run = layout.per_parent.children_of(parent)
        assert all(layout.parent_of_entity(entity) == parent for entity in run)
    assert all(count >= 1 for count in layout.per_parent.counts)  # minChildren 1
    assert int(layout.per_parent.counts.sum()) == ENTITIES


def test_the_array_functions_are_the_row_functions() -> None:
    layout, _, _ = layout_of(plans_of())
    rows = np.arange(VERSIONS, dtype=np.int64)
    assert layout.entities_of(rows).tolist() == [layout.entity_of(r) for r in range(VERSIONS)]
    assert layout.versions_of(rows).tolist() == [layout.version_of(r) for r in range(VERSIONS)]
    assert layout.parent_rows_of(rows).tolist() == [
        layout.parent_row_of(r) for r in range(VERSIONS)
    ]


def test_the_parent_entry_shape_gives_the_entities_per_parent() -> None:
    parent = HistoryParent(
        "customer", children=ChildShape("levels", ("1", "2"), ("0.5", "0.5")), min_children=1
    )
    plans = plans_of(history_entry=entry(parent=parent, entities=9), customers=6, rows=30)
    layout, _, _ = layout_of(plans)
    assert layout.per_parent is not None
    # Six parents take three slots of 1 and three of 2: with a minimum of 1 the three spare
    # entities weigh 0 and 1, so the heavy half takes them.
    assert sorted(layout.per_parent.counts.tolist()) == [1, 1, 1, 2, 2, 2]


def test_without_a_parent_the_entities_are_in_index_order_with_no_parent() -> None:
    document = contract()
    plans = plans_of(history_entry=entry(parent=None), document=document)
    plan = plans[EMIT]
    assert plan.relations is not None
    # The relationship is then an ordinary one: its foreign key is drawn, not laid out.
    foreign = plan.relations.driver("customer_id")
    assert isinstance(foreign, ForeignKey) and foreign.pick == "uniform"
    natural = plan.relations.driver("sub_id")
    assert isinstance(natural, EntityColumn)
    data = table(plan, generate_rows(plan, root=root_for()))
    assert len({row["sub_id"] for row in data}) == ENTITIES


# -- the keys -----------------------------------------------------------------------------------


def test_the_natural_key_is_one_value_per_entity_copied_on_every_version() -> None:
    plans = plans_of()
    layout, _, _ = layout_of(plans)
    data = table(plans[EMIT], generate_rows(plans[EMIT], root=root_for()))
    held = Counter(row["sub_id"] for row in data)
    assert len(held) == ENTITIES
    assert sorted(held.values()) == sorted(layout.versions.counts.tolist())
    for row_index, row in enumerate(data):
        assert (
            row["sub_id"]
            == data[layout.versions.children_of(layout.entity_of(row_index))[0]]["sub_id"]
        )
    # The version key stays laid out over the rows: unique per version.
    assert [row["version_id"] for row in data] == list(range(1, VERSIONS + 1))


def test_the_parent_foreign_key_copies_the_parent_row_on_every_version() -> None:
    plans = plans_of()
    layout, foreign, _ = layout_of(plans)
    assert foreign.pick == "history" and foreign.history is not None
    data = table(plans[EMIT], generate_rows(plans[EMIT], root=root_for()))
    customers = table(plans["customer"], generate_rows(plans["customer"], root=root_for()))
    for row_index, row in enumerate(data):
        parent = layout.parent_row_of(row_index)
        assert parent is not None
        assert row["customer_id"] == customers[parent]["customer_id"]


def test_the_foreign_key_draws_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = plans_of()[EMIT]
    key = CellStreams(root_for(), SIM_ID, EMIT).column_key("customer_id")
    streamed: List[str] = []
    keyed: List[Any] = []
    stream_at = CellStreams.stream_at

    def spy_stream(self: Any, row: int, column: str) -> Any:
        streamed.append(column)
        return stream_at(self, row, column)

    monkeypatch.setattr(CellStreams, "stream_at", spy_stream)
    for name in ("column_uniform53", "column_bounded", "column_words"):
        real = getattr(kernel2, name)

        def spy(column_key: Any, *args: Any, _real: Any = real) -> Any:
            keyed.append(column_key)
            return _real(column_key, *args)

        monkeypatch.setattr(kernel2, name, spy)
    generate_rows(plan, root=root_for())
    assert "customer_id" not in streamed and key not in keyed
    # The other columns draw as ever.
    assert CellStreams(root_for(), SIM_ID, EMIT).column_key("plan") in keyed


def test_the_range_check_counts_the_entities_for_a_history() -> None:
    # Another table reads the natural key, so the key is a key: the layout keeps it unique.
    # Its range holds 8 values. On main the check counts the 24 rows and refuses it; for a
    # history it counts the 8 entities.
    natural = {"range": {"min": "1", "max": "8"}}
    message = refused(
        lambda: plans_of(natural_type="INT", natural=natural, invoices=True, no_history=True)
    )
    assert "takes 24 of its 24 rows" in message and "sub_id" in message
    plans = plans_of(natural_type="INT", natural=natural, invoices=True)
    data = table(plans[EMIT], generate_rows(plans[EMIT], root=root_for()))
    assert sorted({row["sub_id"] for row in data}) == list(range(1, 9))
    assert max(Counter(row["sub_id"] for row in data).values()) == 5
    invoices = table(plans["invoice"], generate_rows(plans["invoice"], root=root_for()))
    assert {row["i_sub"] for row in invoices} <= set(range(1, 9))
    # Seven values cannot hold eight entities.
    message = refused(
        lambda: plans_of(
            natural_type="INT", natural={"range": {"min": "1", "max": "7"}}, invoices=True
        )
    )
    assert "takes 8 of its 8 entities" in message and "sub_id" in message
    message = refused(
        lambda: plans_of(natural_type="INT", natural={"maxCardinality": 7}, invoices=True)
    )
    assert "8 entities" in message


def test_a_natural_key_over_a_declared_range_is_laid_out_over_the_entities() -> None:
    plans = plans_of(natural_type="INT")
    data = table(plans[EMIT], generate_rows(plans[EMIT], root=root_for()))
    assert sorted({row["sub_id"] for row in data}) == list(range(1, ENTITIES + 1))


SKEWED = {"bins": ["1", "4", "6", "16"], "binWeights": ["3", "3", "2"]}


def _bin_of(value: int) -> int:
    return 0 if value < 4 else 1 if value < 6 else 2


def test_a_natural_key_whose_bins_weigh_versions_is_filled_up_to_its_widths() -> None:
    # The bins hold 3, 2 and 11 values and weigh 3:3:2, as versions do. Over the 8 entities the
    # weights share 3, 3 and 2, so the middle bin takes one more than it holds. It takes its
    # two values, the other bins share the six left (4 and 2), the first takes only its three
    # values, and the last takes the three that remain.
    plans = plans_of(natural_type="INT", natural=SKEWED)
    data = table(plans[EMIT], generate_rows(plans[EMIT], root=root_for()))
    keys = sorted({row["sub_id"] for row in data})
    assert len(keys) == ENTITIES and all(1 <= key <= 15 for key in keys)
    assert Counter(_bin_of(key) for key in keys) == {0: 3, 1: 2, 2: 3}
    assert keys[:5] == [1, 2, 3, 4, 5]


def test_a_natural_key_whose_bins_do_not_overflow_keeps_the_weights_allotment() -> None:
    wide = {"bins": ["1", "10", "20", "40"], "binWeights": ["3", "3", "2"]}
    plans = plans_of(natural_type="INT", natural=wide)
    data = table(plans[EMIT], generate_rows(plans[EMIT], root=root_for()))
    keys = {row["sub_id"] for row in data}
    assert Counter(0 if k < 10 else 1 if k < 20 else 2 for k in keys) == {0: 3, 1: 3, 2: 2}


def test_a_natural_key_that_weighted_bins_cannot_hold_is_refused() -> None:
    # The last bin weighs zero: its values take no entity, so the 5 values of the others
    # cannot hold 8 entities, however wide it is.
    short = {"bins": ["1", "4", "6", "30"], "binWeights": ["3", "3", "0"]}
    message = refused(lambda: plans_of(natural_type="INT", natural=short))
    assert "hold 5 values, fewer than its 8 entities" in message and "sub_id" in message


def test_weighted_bins_one_value_short_of_the_entities_are_refused() -> None:
    # The bins that weigh hold 3 and 4 values: 7, one fewer than the 8 entities.
    short = {"bins": ["1", "4", "8", "30"], "binWeights": ["3", "3", "0"]}
    message = refused(lambda: plans_of(natural_type="INT", natural=short))
    assert "hold 7 values, fewer than its 8 entities" in message


def test_weighted_bins_that_hold_as_many_values_as_entities_take_every_one() -> None:
    exact = {"bins": ["1", "4", "9", "30"], "binWeights": ["3", "3", "0"]}
    plans = plans_of(natural_type="INT", natural=exact)
    data = table(plans[EMIT], generate_rows(plans[EMIT], root=root_for()))
    assert sorted({row["sub_id"] for row in data}) == list(range(1, ENTITIES + 1))


def test_a_bin_of_weight_zero_takes_no_entity_however_much_room_it_has() -> None:
    # The third bin weighs zero and holds ten values. The entities the first two bins cannot
    # take go to the last bin, by its weight, and none to the third.
    gap = {"bins": ["1", "4", "6", "16", "40"], "binWeights": ["3", "3", "0", "2"]}
    plans = plans_of(natural_type="INT", natural=gap)
    data = table(plans[EMIT], generate_rows(plans[EMIT], root=root_for()))
    keys = {row["sub_id"] for row in data}
    assert Counter(0 if k < 4 else 1 if k < 6 else 2 if k < 16 else 3 for k in keys) == {
        0: 3,
        1: 2,
        3: 3,
    }


def test_the_rows_path_keeps_its_refusal_for_overflowing_bins() -> None:
    message = refused(
        lambda: plans_of(natural_type="INT", natural=SKEWED, invoices=True, no_history=True, rows=8)
    )
    assert "bin 1 of its histogram holds 2 values but takes 3 of its 8 rows" in message


def test_a_natural_entity_a_relationship_also_sets_is_refused() -> None:
    document = contract("INT")
    version = next(e for e in document["exposes"] if e["exposeId"] == EMIT)
    version["semantics"]["relationships"].append(
        _rel("self", "customer", ["sub_id"], ["customer_id"])
    )
    message = refused(lambda: plans_of(document=document, natural_type="INT"))
    assert "natural entity" in message


# -- the plan -----------------------------------------------------------------------------------


def test_a_plan_without_a_history_keeps_its_document_and_digest() -> None:
    plain = plans_of(
        no_history=True, natural_type="INT", natural={"range": {"min": "1", "max": "99"}}
    )
    other = plans_of(
        no_history=True, natural_type="INT", natural={"range": {"min": "1", "max": "99"}}
    )
    assert plain[EMIT].digest().value == other[EMIT].digest().value
    relations_of = plain[EMIT].relations
    assert relations_of is None or relations_of.driver("sub_id") is None


def test_a_declared_history_moves_the_digest_of_its_plan() -> None:
    first = plans_of()[EMIT].digest().value
    again = plans_of()[EMIT].digest().value
    changed = plans_of(history_entry=entry(until="2025-12-30"))[EMIT].digest().value
    assert first == again != changed
    document = plans_of()[EMIT].as_document()
    assert document["relations"]["customer_id"]["pick"] == "history"
    assert document["relations"]["sub_id"]["layout"] == "entity"


def test_kernel_010_refuses_a_history() -> None:
    message = refused(lambda: plans_of(counter_based=False), "FLX-3105")
    assert "kernel 0.2.0" in message


def test_a_history_needs_a_window_and_one_natural_entity() -> None:
    assert "timeBounds" in refused(lambda: plans_of(bounds=None))
    document = contract()
    version = next(e for e in document["exposes"] if e["exposeId"] == EMIT)
    version["semantics"]["entities"] = [version["semantics"]["entities"][0]]
    assert "natural" in refused(lambda: plans_of(document=document))


# -- the refusals -------------------------------------------------------------------------------


def test_no_entity_for_the_rows_is_refused_and_names_the_scale_through_its_class() -> None:
    # TooFewChildRows is the class a plan under --scale annotates with the scale's note.
    with pytest.raises(TooFewChildRows) as caught:
        plans_of(history_entry=entry(entities=1), entities=0)
    assert "E = 0" in str(caught.value) and caught.value.code.code == "FLX-3403"


def test_more_entities_than_versions_is_refused() -> None:
    with pytest.raises(TooFewChildRows):
        plans_of(entities=VERSIONS + 1, customers=30)


def test_a_parent_with_no_rows_and_entities_is_refused() -> None:
    refused(lambda: plans_of(customers=0))
    # The planner refuses it too, for a caller that has not read the contract.
    with pytest.raises(TooFewChildRows) as caught:
        plan_history(
            root_for(),
            SIM_ID,
            EMIT,
            entry(),
            kind=LogicalKind.DATE,
            rows=VERSIONS,
            entities=ENTITIES,
            time_bounds=BOUNDS,
            outer=OuterInputs("customer", 0),
        )
    assert "no rows" in str(caught.value)


def test_no_entity_and_no_rows_lays_nothing_out() -> None:
    bare = entry(parent=HistoryParent("customer"), entities=1)
    plans = plans_of(entities=0, rows=0, customers=0, history_entry=bare)
    assert generate_rows(plans[EMIT], root=root_for()) == ()
    layout = plan_history(
        root_for(),
        SIM_ID,
        EMIT,
        entry(),
        kind=LogicalKind.DATE,
        rows=0,
        entities=0,
        time_bounds=BOUNDS,
        outer=OuterInputs("customer", 0),
    )
    assert layout.c == 0 and layout.first_starts.shape == (0,)
    assert layout.per_parent is not None and layout.per_parent.children == 0


def test_a_scale_below_one_scales_the_entities_and_the_rows() -> None:
    scale = Decimal("0.25")
    rows, entities = _scaled_rows(VERSIONS, scale), _scaled_rows(ENTITIES, scale)
    assert (rows, entities) == (6, 2)
    plans = plans_of(rows=rows, entities=entities, customers=_scaled_rows(CUSTOMERS, scale))
    layout, _, _ = layout_of(plans)
    assert int(layout.versions.counts.sum()) == rows and layout.versions.counts.shape == (2,)
    data = table(plans[EMIT], generate_rows(plans[EMIT], root=root_for()))
    assert len(data) == 6 and len({row["sub_id"] for row in data}) == 2


def test_a_scale_far_below_one_leaves_one_entity_and_one_row() -> None:
    scale = Decimal("0.01")
    rows, entities = _scaled_rows(VERSIONS, scale), _scaled_rows(ENTITIES, scale)
    assert rows == entities == 1
    plans = plans_of(rows=rows, entities=entities, customers=1)
    assert len(generate_rows(plans[EMIT], root=root_for())) == 1


def test_an_empty_first_start_interval_is_refused_at_plan_time() -> None:
    # Five versions per entity at most, so J ends five days before until, and until is before start.
    message = refused(lambda: plans_of(history_entry=entry(until="2023-01-03")))
    assert "empty" in message and "first starts" in message
    # c is 5, so J = [start, until - 5): one day when until is 2023-01-07.
    ok = plans_of(history_entry=entry(until="2023-01-07"))
    assert set(layout_of(ok)[0].first_starts.tolist()) == {unit_of("2023-01-01", LogicalKind.DATE)}
    refused(lambda: plans_of(history_entry=entry(until="2023-01-06")))
    # closeLast takes one more unit, so the same horizon is then too near.
    close = HistoryEnd("closeLast", "0.1")
    refused(lambda: plans_of(history_entry=entry(until="2023-01-07", end=close)))
    plans_of(history_entry=entry(until="2023-01-08", end=close))


# -- the window's end ---------------------------------------------------------------------------


class _NoEnd(dict):
    """A ``spec.timeBounds`` whose end raises when it is read in any way."""

    def __getitem__(self, key: str) -> Any:
        if key == "end":
            raise AssertionError("the planner read timeBounds.end")
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "end":
            raise AssertionError("the planner read timeBounds.end")
        return super().get(key, default)

    def __iter__(self) -> Iterator[str]:
        return iter(["start"])

    def items(self) -> Any:
        raise AssertionError("the planner read the whole of timeBounds")

    def values(self) -> Any:
        raise AssertionError("the planner read the whole of timeBounds")


def test_the_planner_never_reads_the_end_of_the_window() -> None:
    kwargs: Dict[str, Any] = dict(
        kind=LogicalKind.DATE,
        rows=VERSIONS,
        entities=ENTITIES,
        outer=OuterInputs("customer", CUSTOMERS, minimum=1),
    )
    guarded = plan_history(
        root_for(),
        SIM_ID,
        EMIT,
        entry(),
        time_bounds=_NoEnd(start=BOUNDS["start"], end="never read"),
        **kwargs,
    )
    longer = plan_history(
        root_for(),
        SIM_ID,
        EMIT,
        entry(),
        time_bounds={"start": BOUNDS["start"], "end": "2031-01-01T00:00:00Z"},
        **kwargs,
    )
    assert guarded.first_starts.tolist() == longer.first_starts.tolist()
    assert guarded.versions.counts.tolist() == longer.versions.counts.tolist()
    # A plan's own bounds are the pair (startMs, endMs): the end is never indexed.

    class Pair(tuple):
        def __getitem__(self, index: Any) -> Any:
            if index != 0:
                raise AssertionError("the planner read the window's end")
            return super().__getitem__(index)

    pair = Pair((1672531200000, 1767225600000))
    pairs = plan_history(root_for(), SIM_ID, EMIT, entry(), time_bounds=pair, **kwargs)
    assert pairs.first_starts.tolist() == guarded.first_starts.tolist()


def test_a_longer_window_moves_no_row_of_the_layout() -> None:
    short = plans_of()
    longer = plans_of(bounds={"start": BOUNDS["start"], "end": "2031-01-01T00:00:00Z"})
    first, _, _ = layout_of(short)
    second, _, _ = layout_of(longer)
    assert first.first_starts.tolist() == second.first_starts.tolist()
    assert first.versions.counts.tolist() == second.versions.counts.tolist()
    # The keys and the parent are the layout's, so they are the same rows under both windows.
    keep = ("version_id", "sub_id", "customer_id")
    rows = [
        table(plans[EMIT], generate_rows(plans[EMIT], root=root_for())) for plans in (short, longer)
    ]
    assert [[row[name] for name in keep] for row in rows[0]] == [
        [row[name] for name in keep] for row in rows[1]
    ]


# -- the process --------------------------------------------------------------------------------


def test_the_layout_is_worked_out_once_per_process() -> None:
    plans = plans_of()
    first, _, _ = layout_of(plans)
    second, _, _ = layout_of(plans)
    assert first.versions is second.versions and first.first_starts is second.first_starts


def test_another_seed_is_another_layout() -> None:
    plans = plans_of()
    a, _, _ = layout_of(plans, 5)
    b, _, _ = layout_of(plans, 6)
    assert a.first_starts.tolist() != b.first_starts.tolist()


# -- two paths, one answer ----------------------------------------------------------------------


@pytest.mark.parametrize("natural_type", ["STRING", "INT"])
def test_the_column_path_is_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch, natural_type: str
) -> None:
    plans = plans_of(natural_type=natural_type)
    plan = plans[EMIT]
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_row_slice(plan, 0, plan.rows, root=root_for())
    )
    assert taken == [True]
    assert by_columns == by_rows
    # And the same through one column at a time.
    for column in ("sub_id", "customer_id"):
        assert generate_column(plan, column, root=root_for()) == tuple(
            row[[c.column.name for c in plan.columns].index(column)] for row in by_rows
        )


def test_a_slice_anywhere_is_the_rows_of_the_whole_table(monkeypatch: pytest.MonkeyPatch) -> None:
    plans = plans_of()
    plan = plans[EMIT]
    whole = generate_row_slice(plan, 0, plan.rows, root=root_for())
    for start, stop in ((0, 7), (5, 17), (17, 24), (23, 24)):
        monkeypatch.setattr(history, "_PLANS", {})
        assert generate_row_slice(plan, start, stop, root=root_for()) == whole[start:stop]


def test_a_reader_of_the_natural_key_reads_the_entity_s_value() -> None:
    plans = plans_of()
    layout, _, natural = layout_of(plans)
    plan = plans[EMIT]
    reader = relations.RelationReader(root_for())
    reader.know(plan)
    owner = relations.owner_of(plan)
    names = [c.column.name for c in plan.columns]
    column_plan = plan.columns[names.index("sub_id")]
    for row in (0, 5, 23):
        assert reader.cell(owner, column_plan, natural, row) == natural.layout.value_at(
            layout.entity_of(row)
        )
    cells = reader.column_values(owner, column_plan, natural, np.arange(VERSIONS, dtype=np.int64))
    assert cells == [reader.cell(owner, column_plan, natural, r) for r in range(VERSIONS)]


# -- a parent's condition and an expected-count hook --------------------------------------------


class Hook:
    """An expected-count hook for ``sub_version.customer``: the parent's ``c_score``."""

    def __init__(self) -> None:
        self.rows: List[int] = []

    def member(
        self, namespace: Mapping[str, Any], child: GeneratorPlan, relationship: str
    ) -> Optional[Mapping[str, Any]]:
        if child.expose.expose_id != EMIT or relationship != "customer":
            return None
        return {"from": "c_score"}

    def expected(
        self, parent: GeneratorPlan, row: int, final_row: Tuple[Any, ...], relationship: str
    ) -> Fraction:
        self.rows.append(row)
        names = [column_plan.column.name for column_plan in parent.columns]
        return Fraction(int(final_row[names.index("c_score")]))


@pytest.fixture
def hook() -> Iterator[Hook]:
    before = expected_count_hooks()
    stub = Hook()
    unregister = register_expected_count_hook(stub)
    yield stub
    unregister()
    assert expected_count_hooks() == before


def test_an_expected_count_hook_gives_the_entity_layout_its_expected_counts(hook: Hook) -> None:
    held = entry(parent=HistoryParent("customer"), entities=12)
    plans = plans_of(history_entry=held, customers=6, rows=40)
    layout, foreign, _ = layout_of(plans)
    assert foreign.pick == "history" and foreign.counted is not None
    assert foreign.counted.hook is hook and foreign.counted.child_rows == 12
    assert sorted(hook.rows) == list(range(6))
    assert layout.per_parent is not None and int(layout.per_parent.counts.sum()) == 12
    customers = table(plans["customer"], generate_rows(plans["customer"], root=root_for()))
    scores = [int(row["c_score"]) for row in customers]
    counts = layout.per_parent.counts.tolist()
    # A parent never has fewer entities than one with a lower expected count.
    for a in range(6):
        for b in range(6):
            if scores[a] < scores[b]:
                assert counts[a] <= counts[b] + 1
    # The member the hook binds is part of the foreign key's document, so it moves the digest.
    document = plans[EMIT].as_document()["relations"]["customer_id"]
    assert document["counts"]["expectedCount"] == {"from": "c_score"}


def test_a_parent_condition_leaves_the_other_parents_without_entities() -> None:
    held = entry(parent=HistoryParent("customer", parent_when="c_score > 4"))
    plans = plans_of(history_entry=held, entities=4, rows=16)
    layout, _, _ = layout_of(plans)
    customers = table(plans["customer"], generate_rows(plans["customer"], root=root_for()))
    assert layout.per_parent is not None
    for row, customer in enumerate(customers):
        eligible = int(customer["c_score"]) > 4
        assert (int(layout.per_parent.counts[row]) > 0) <= eligible
    assert int(layout.per_parent.counts.sum()) == 4
