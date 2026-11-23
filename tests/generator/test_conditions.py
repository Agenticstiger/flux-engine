# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Rows that hold fixed values are drawn exactly from the joint the conditionals describe (ADR 0028).

The oracle here shares no code with the module under test. It enumerates the joint of a small model
column by column, as the product of the conditionals the declaration states, and for each column and
each combination of the columns drawn before it adds up, over every completion of the columns drawn
after it (a fixed one only at its fixed value), the product of their conditionals. The tables
:class:`~flux_engine.generator.conditions.ConditionPlan` searches a cell's uniform in must be the
thresholds of those exact weights. A sum over a clique's later new columns that was missed would
move a weight, and so a threshold.
"""

from __future__ import annotations

import math
import random
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import derive_root, digest_bytes
from flux_engine.generator import columns, conditions
from flux_engine.generator.conditions import (
    FLX_3646,
    ConditionPlan,
    scaled_counts,
    thresholds_of,
    with_conditions,
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
from flux_engine.seam.conditions import FLX_3641, FLX_3642, FLX_3643, ConditionGroup

from .conftest import SIM_ID, TIME_BOUNDS, H, expose_from_schema, fidelity

pytestmark = pytest.mark.unit

TWO53 = 1 << 53

# ---------------------------------------------------------------------------
# Hand-made models: cliques as `given` lists (ADR 0021 rule 3)
# ---------------------------------------------------------------------------

#: ``(column, levels, the columns it is drawn given)`` in declared order. A clique with separator
#: ``S`` and new columns ``n_1 .. n_m`` makes ``n_j`` given ``S`` and ``n_1 .. n_(j-1)``.
Spec = Sequence[Tuple[str, Sequence[str], Sequence[str]]]

#: Two cliques {a, b, c} and {c, d, e}: a separator of one column.
TWO_CLIQUES: Spec = (
    ("a", ("x0", "x1"), ()),
    ("b", ("x0", "x1"), ("a",)),
    ("c", ("x0", "x1", "x2"), ("a", "b")),
    ("d", ("x0", "x1"), ("c",)),
    ("e", ("x0", "x1"), ("c", "d")),
)

#: Three cliques {a, b, c}, {b, c, d} and {c, d, e}: separators of two columns.
THREE_CLIQUES: Spec = (
    ("a", ("x0", "x1"), ()),
    ("b", ("x0", "x1"), ("a",)),
    ("c", ("x0", "x1"), ("a", "b")),
    ("d", ("x0", "x1", "x2"), ("b", "c")),
    ("e", ("x0", "x1"), ("c", "d")),
)

#: A Chow-Liu forest, each pair a clique: a to b to c, a to d, d to e, and f alone.
FOREST: Spec = (
    ("a", ("x0", "x1"), ()),
    ("b", ("x0", "x1", "x2"), ("a",)),
    ("c", ("x0", "x1"), ("b",)),
    ("d", ("x0", "x1"), ("a",)),
    ("e", ("x0", "x1"), ("d",)),
    ("f", ("x0", "x1"), ()),
)

_ROOT_SHARES = {
    2: {"x0": "0.25", "x1": "0.75"},
    3: {"x0": "0.5", "x1": "0.25", "x2": "0.25"},
}


def _nested(
    rng: random.Random,
    parents: Sequence[str],
    levels: Mapping[str, Sequence[str]],
    own: Sequence[str],
) -> Dict[str, Any]:
    """One table of counts per combination of the parents' levels, a few of them zero."""
    if not parents:
        counts = [rng.randint(0, 4) for _ in own]
        if not any(counts):
            counts[rng.randrange(len(own))] = 1
        return {level: str(count) for level, count in zip(own, counts, strict=True)}
    first, rest = parents[0], parents[1:]
    return {level: _nested(rng, rest, levels, own) for level in levels[first]}


def declare(spec: Spec, seed: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The schema and the fidelity columns that draw *spec*, with seeded counts."""
    rng = random.Random(seed)
    levels = {name: tuple(own) for name, own, _ in spec}
    schema: List[Dict[str, Any]] = []
    declared: Dict[str, Any] = {}
    for name, own, parents in spec:
        schema.append({"name": name, "type": "STRING"})
        if not parents:
            declared[name] = {"levels": dict(_ROOT_SHARES[len(own)])}
        else:
            declared[name] = {
                "given": list(parents) if len(parents) > 1 else parents[0],
                "levelWeightsGiven": _nested(rng, parents, levels, own),
            }
    return schema, declared


def plan_of(
    spec: Spec, seed: int = 1, rows: int = 100, generator_version: str = GENERATOR_VERSION_2
) -> GeneratorPlan:
    schema, declared = declare(spec, seed)
    return build_plan(
        expose_from_schema(schema),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=rows,
        fidelity=fidelity(declared),
        time_bounds=TIME_BOUNDS,
        generator_version=generator_version,
    )


def group(values: Mapping[str, str], count: Optional[int] = None) -> ConditionGroup:
    return ConditionGroup(values=tuple(sorted(values.items())), count=count)


# ---------------------------------------------------------------------------
# The oracle
# ---------------------------------------------------------------------------


def _conditional(
    declared: Mapping[str, Any], name: str, level: str, drawn: Mapping[str, str]
) -> Fraction:
    """P(name = level | its parents), read off the declaration."""
    block = declared[name]
    if "levels" in block:
        weights = block["levels"]
    else:
        given = block["given"]
        weights = block["levelWeightsGiven"]
        for parent in [given] if isinstance(given, str) else given:
            weights = weights[drawn[parent]]
    total = sum(Fraction(w) for w in weights.values())
    return Fraction(weights[level]) / total


def _later(
    order: Sequence[str],
    levels: Mapping[str, Sequence[str]],
    declared: Mapping[str, Any],
    fixed: Mapping[str, str],
    drawn: Dict[str, str],
    index: int,
) -> Fraction:
    """The sum over every completion of the columns from *index* on of the product of their conditionals."""
    if index == len(order):
        return Fraction(1)
    name = order[index]
    total = Fraction(0)
    for level in [fixed[name]] if name in fixed else levels[name]:
        weight = _conditional(declared, name, level, drawn)
        if weight:
            total += weight * _later(
                order, levels, declared, fixed, {**drawn, name: level}, index + 1
            )
    return total


def expected_tables(
    spec: Spec, seed: int, fixed: Mapping[str, str]
) -> List[Tuple[str, Dict[str, str], Dict[str, Fraction]]]:
    """``(column, columns drawn before it, exact weights of its levels)`` for every reachable prefix.

    Enumerates the prefixes in draw order; a fixed column takes its value and is not a choice. A
    prefix from which no completion has weight is not reachable and is left out.
    """
    schema, declared = declare(spec, seed)
    order = [name for name, _, _ in spec]
    levels = {name: tuple(own) for name, own, _ in spec}
    found: List[Tuple[str, Dict[str, str], Dict[str, Fraction]]] = []

    def walk(index: int, drawn: Dict[str, str]) -> None:
        if index == len(order):
            return
        name = order[index]
        if name in fixed:
            walk(index + 1, {**drawn, name: fixed[name]})
            return
        weights = {
            level: _conditional(declared, name, level, drawn)
            * _later(order, levels, declared, fixed, {**drawn, name: level}, index + 1)
            for level in levels[name]
        }
        if not any(weights.values()):
            return
        found.append((name, dict(drawn), weights))
        for level, weight in weights.items():
            if weight:
                walk(index + 1, {**drawn, name: level})

    walk(0, {})
    return found


def unconditioned_shares(
    spec: Spec, seed: int, name: str, drawn: Mapping[str, str]
) -> Dict[str, Fraction]:
    schema, declared = declare(spec, seed)
    levels = next(own for column, own, _ in spec if column == name)
    return {level: _conditional(declared, name, level, drawn) for level in levels}


def _first_row_of(conditioned: ConditionPlan, index: int) -> int:
    return 0 if index == 0 else conditioned.ends[index - 1]


EVIDENCE: Sequence[Mapping[str, str]] = (
    {"e": "x1"},
    {"e": "x0"},
    {"a": "x1"},
    {"c": "x2"},
    {"b": "x0", "e": "x1"},
    {"a": "x0", "d": "x1"},
    {"c": "x1", "d": "x0", "e": "x0"},
    {"d": "x1", "b": "x1"},
)


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize(
    "spec", [TWO_CLIQUES, THREE_CLIQUES, FOREST], ids=["two-cliques", "three-cliques", "forest"]
)
def test_the_tables_are_the_thresholds_of_the_exact_conditionals(spec: Spec, seed: int) -> None:
    levels_of = {name: own for name, own, _ in spec}
    for evidence in EVIDENCE:
        fixed = {name: level for name, level in evidence.items() if name in levels_of}
        if not fixed or any(level not in levels_of[name] for name, level in fixed.items()):
            continue
        try:
            conditioned = with_conditions(plan_of(spec, seed), [group(fixed)]).conditions
        except FluxError as refused:
            # A hand-made table that rules the values out: the oracle must say the same.
            assert refused.code == FLX_3646
            order = [name for name, _, _ in spec]
            levels = {name: tuple(own) for name, own, _ in spec}
            _, declared = declare(spec, seed)
            assert _later(order, levels, declared, fixed, {}, 0) == 0
            continue
        assert conditioned is not None
        checked = 0
        for name, drawn, weights in expected_tables(spec, seed, fixed):
            decided = conditioned.decide(0, name, {**fixed, **drawn})
            if decided is None:
                # The group leaves this column as it would be drawn: its conditional is untouched.
                shares = unconditioned_shares(spec, seed, name, drawn)
                total = sum(weights.values(), Fraction(0))
                assert {k: w / total for k, w in weights.items()} == shares
                assert name not in conditioned.touched
            else:
                assert decided == thresholds_of(weights), (name, drawn, fixed)
            checked += 1
        assert checked
        for name, level in fixed.items():
            assert conditioned.decide(0, name, dict(fixed)) == level
            assert name in conditioned.touched


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_a_missed_sum_over_the_later_new_columns_would_move_a_weight(seed: int) -> None:
    """In two cliques {a, b, c} and {c, d, e}, fixing e moves the weights of a, b, c and d.

    The weight of a value of c carries the sum over d (a later new column of the child clique) of
    d's conditional times e's, which depends on c. A table that held d at one level instead of
    summing over it is another table, so the exact one is not the one a missed sum gives.
    """
    plan = with_conditions(plan_of(TWO_CLIQUES, seed=seed), [group({"e": "x1"})])
    conditioned = plan.conditions
    assert conditioned is not None
    assert conditioned.touched == {"a", "b", "c", "d", "e"}
    _, declared = declare(TWO_CLIQUES, seed)
    differs = 0
    for column, drawn, weights in expected_tables(TWO_CLIQUES, seed, {"e": "x1"}):
        if column != "c":
            continue
        exact = conditioned.decide(0, "c", {"e": "x1", **drawn})
        assert exact == thresholds_of(weights)
        held_at_x0 = {
            level: _conditional(declared, "c", level, drawn)
            * _conditional(declared, "e", "x1", {"c": level, "d": "x0"})
            for level in ("x0", "x1", "x2")
        }
        if sum(held_at_x0.values()) and exact != thresholds_of(held_at_x0):
            differs += 1
    assert differs


# ---------------------------------------------------------------------------
# The table, pinned
# ---------------------------------------------------------------------------


def test_the_thresholds_of_a_table_are_pinned() -> None:
    third = thresholds_of({"a": Fraction(1), "b": Fraction(1), "c": Fraction(1)})
    assert third == (
        ("a", "b", "c"),
        (3002399751580331, 6004799503160662, 9007199254740992),
    )
    # The ceiling of each cumulative share times 2**53; the last threshold is 2**53.
    assert third[1][0] == math.ceil(Fraction(TWO53, 3)) == -(-TWO53 // 3)
    assert third[1][1] == math.ceil(Fraction(2 * TWO53, 3))
    assert third[1][-1] == TWO53
    # Shares, not weights: 2 : 6 : 0 : 2 is a fifth, three fifths and a fifth, and a level with no
    # weight has no threshold of its own.
    assert thresholds_of(
        {"b": Fraction(6), "a": Fraction(2), "c": Fraction(0), "d": Fraction(2)}
    ) == (
        ("a", "b", "d"),
        (math.ceil(Fraction(TWO53, 5)), math.ceil(Fraction(4 * TWO53, 5)), TWO53),
    )
    # Levels are in NFC order: a decomposed e-acute sorts as the composed one does.
    decomposed = "e" + chr(0x301)  # NFC "\u00e9", which sorts after "f"
    levels, _ = thresholds_of({decomposed: Fraction(1), "f": Fraction(1), "d": Fraction(1)})
    assert levels == ("d", "f", decomposed)


def test_the_table_of_one_small_conditioned_emit_is_pinned() -> None:
    """a is 1 : 1; b is y0 : y1 as 1 : 3 given a = x0 and 3 : 1 given a = x1. With b = y0 fixed,
    a is x0 : x1 as 1/8 : 3/8, a quarter and three quarters: the threshold is exactly 2**51."""
    schema = [{"name": "a", "type": "STRING"}, {"name": "b", "type": "STRING"}]
    declared = {
        "a": {"levels": {"x0": "0.5", "x1": "0.5"}},
        "b": {
            "given": "a",
            "levelWeightsGiven": {"x0": {"y0": "1", "y1": "3"}, "x1": {"y0": "3", "y1": "1"}},
        },
    }
    plan = build_plan(
        expose_from_schema(schema),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=10,
        fidelity=fidelity(declared),
        time_bounds=TIME_BOUNDS,
        generator_version=GENERATOR_VERSION_2,
    )
    conditioned = with_conditions(plan, [group({"b": "y0"})]).conditions
    assert conditioned is not None
    assert conditioned.decide(0, "a", {"b": "y0"}) == (("x0", "x1"), (2**51, TWO53))
    assert conditioned.decide(3, "b", {"b": "y0", "a": "x1"}) == "y0"
    third = with_conditions(plan, [group({"b": "y1"})]).conditions  # 1/2 * 3/4 : 1/2 * 1/4 = 3 : 1
    assert third is not None
    assert third.decide(0, "a", {}) == (("x0", "x1"), (math.ceil(Fraction(3 * TWO53, 4)), TWO53))


def test_a_level_with_no_weight_is_never_drawn_even_by_the_largest_uniform() -> None:
    table = thresholds_of({"a": Fraction(1), "b": Fraction(0), "c": Fraction(1)})
    assert table[0] == ("a", "c")
    assert conditions.level_at(table, 0) == "a"
    assert conditions.level_at(table, TWO53 // 2 - 1) == "a"
    assert conditions.level_at(table, TWO53 // 2) == "c"
    assert conditions.level_at(table, TWO53 - 1) == "c"


# ---------------------------------------------------------------------------
# Counts and groups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("counts", "total", "expected"),
    [
        ((400, 600), 500, (200, 300)),
        ((400, 600), 2000, (800, 1200)),
        ((3, 3, 4), 5, (2, 1, 2)),
        ((1, 1, 1), 2, (1, 1, 0)),
        ((1, 1, 1), 3, (1, 1, 1)),
        ((1, 1, 1), 4, (2, 1, 1)),
        ((5, 1), 1, (1, 0)),
        ((7,), 3, (3,)),
        ((2, 3), 0, (0, 0)),
    ],
)
def test_counts_scale_by_the_largest_remainder(
    counts: Tuple[int, ...], total: int, expected: Tuple[int, ...]
) -> None:
    scaled = scaled_counts(counts, total)
    assert scaled == expected
    assert sum(scaled) == total


def test_scaled_counts_are_never_more_than_one_from_their_exact_share() -> None:
    rng = random.Random(7)
    for _ in range(200):
        counts = [rng.randint(1, 50) for _ in range(rng.randint(1, 9))]
        total = rng.randint(0, 400)
        scaled = scaled_counts(counts, total)
        assert sum(scaled) == total
        for count, got in zip(counts, scaled, strict=True):
            assert abs(Fraction(count * total, sum(counts)) - got) < 1


def test_a_rows_group_is_a_function_of_its_ordinal() -> None:
    plan = with_conditions(
        plan_of(TWO_CLIQUES, rows=10),
        [group({"a": "x0"}, 3), group({"a": "x1"}, 0), group({"e": "x1"}, 7)],
    )
    conditioned = plan.conditions
    assert conditioned is not None
    assert [conditioned.group_of(row) for row in range(10)] == [0, 0, 0, 2, 2, 2, 2, 2, 2, 2]
    assert conditioned.rows == 10


def test_a_group_with_no_count_covers_every_row() -> None:
    plan = with_conditions(plan_of(TWO_CLIQUES, rows=25), [group({"a": "x1"})])
    assert plan.conditions is not None
    assert plan.conditions.ends == (25,)
    rows = generate_rows(plan, root=_root())
    assert {row[0] for row in rows} == {"x1"}


def _root(seed: int = 42) -> Any:
    return derive_root(digest_bytes(H), seed, seed_tree_version=kernel2.SEED_TREE_VERSION)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_values_with_no_weight_together_are_refused_at_plan_naming_the_group() -> None:
    schema = [{"name": "a", "type": "STRING"}, {"name": "b", "type": "STRING"}]
    declared = {
        "a": {"levels": {"x0": "0.5", "x1": "0.5"}},
        "b": {
            "given": "a",
            "levelWeightsGiven": {"x0": {"y0": "1", "y1": "0"}, "x1": {"y0": "0", "y1": "1"}},
        },
    }
    plan = build_plan(
        expose_from_schema(schema),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=4,
        fidelity=fidelity(declared),
        time_bounds=TIME_BOUNDS,
        generator_version=GENERATOR_VERSION_2,
    )
    ok = with_conditions(plan, [group({"a": "x0", "b": "y0"}, 2), group({"a": "x1"}, 2)])
    assert ok.conditions is not None
    with pytest.raises(FluxError) as refused:
        with_conditions(plan, [group({"a": "x0", "b": "y0"}, 2), group({"a": "x0", "b": "y1"}, 2)])
    assert refused.value.code == FLX_3646
    assert "group 1" in str(refused.value) and "a=x0, b=y1" in str(refused.value)


def test_a_group_with_a_missing_column_a_missing_level_or_a_wrong_total_is_refused() -> None:
    plan = plan_of(TWO_CLIQUES, rows=10)
    with pytest.raises(FluxError) as refused:
        with_conditions(plan, [group({"zzz": "x0"})])
    assert refused.value.code == FLX_3643
    with pytest.raises(FluxError) as refused:
        with_conditions(plan, [group({"a": "nope"})])
    assert refused.value.code == FLX_3642
    with pytest.raises(FluxError) as refused:
        with_conditions(plan, [group({"a": "x0"}, 4), group({"a": "x1"}, 4)])
    assert refused.value.code == FLX_3641


def test_kernel_0_1_0_does_not_take_conditions() -> None:
    plan = plan_of(TWO_CLIQUES, generator_version=GENERATOR_VERSION)
    with pytest.raises(FluxError) as refused:
        with_conditions(plan, [group({"a": "x0"})])
    assert "kernel 0.1.0" in str(refused.value)


def test_a_plan_without_conditions_has_no_entry_and_keeps_its_digest() -> None:
    plain = plan_of(TWO_CLIQUES)
    assert "conditions" not in plain.as_document()
    conditioned = with_conditions(plain, [group({"a": "x0"})])
    document = conditioned.as_document()
    assert document["conditions"] == {"groups": [{"values": {"a": "x0"}, "count": 100}]}
    assert conditioned.digest() != plain.digest()
    other = with_conditions(plain, [group({"a": "x1"})])
    assert other.digest() != conditioned.digest()


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

_GROUPS = [
    group({"e": "x1"}, 120),
    group({"a": "x0", "d": "x1"}, 100),
    group({"c": "x1", "b": "x1"}, 80),
]


def test_fixed_cells_hold_their_values_on_every_row_of_their_group() -> None:
    plan = with_conditions(plan_of(TWO_CLIQUES, rows=300), _GROUPS)
    rows = generate_rows(plan, root=_root())
    names = [column.column.name for column in plan.columns]
    start = 0
    for spec in _GROUPS:
        for row in rows[start : start + (spec.count or 0)]:
            for column, level in spec.values:
                assert row[names.index(column)] == level
        start += spec.count or 0
    assert start == len(rows) == 300


def test_no_fixed_cell_draws(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = with_conditions(plan_of(TWO_CLIQUES, rows=300), _GROUPS)
    asked: List[Tuple[int, str]] = []
    from flux_engine.generator.paths import CellStreams

    original = CellStreams.stream_at

    def spy(self: Any, row_index: int, column: str) -> Any:
        asked.append((row_index, column))
        return original(self, row_index, column)

    monkeypatch.setattr(CellStreams, "stream_at", spy)
    monkeypatch.setattr(columns, "column_rows", lambda *args: None)
    generate_rows(plan, root=_root())
    assert asked
    start = 0
    for spec in _GROUPS:
        for row in range(start, start + (spec.count or 0)):
            for column, _level in spec.values:
                assert (row, column) not in asked
        start += spec.count or 0
    # Every other cell of the table draws once.
    assert (0, "a") in asked and (150, "b") in asked and len(asked) == len(set(asked))


def test_a_drawn_cell_draws_on_its_own_path_at_the_usual_index() -> None:
    """A group that fixes a column nothing else is drawn given changes no other cell: each one is
    the cell an unconditioned run draws, from its own stream at its first word."""
    plain = plan_of(FOREST, rows=200)
    root = _root()
    base = generate_rows(plain, root=root)
    conditioned = with_conditions(plain, [group({"f": "x1"}, 200)])
    rows = generate_rows(conditioned, root=root)
    assert [row[:-1] for row in rows] == [row[:-1] for row in base]
    assert {row[-1] for row in rows} == {"x1"} and {row[-1] for row in base} == {"x0", "x1"}
    # Fixing a column that others are drawn given moves those columns' tables and not their
    # streams: a, the root of the chain, draws the level its own uniform picks in a new table.
    moved = with_conditions(plain, [group({"c": "x1"}, 200)])
    assert generate_column(moved, "a", root=root) == tuple(
        row[0] for row in generate_rows(moved, root=root)
    )


def test_the_per_row_path_and_the_column_path_agree_on_a_conditioned_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .test_columns import _both

    for seed in (1, 2):
        plan = with_conditions(plan_of(THREE_CLIQUES, seed=seed, rows=300), _GROUPS)
        for chunk in (1, 7, 64, columns.CHUNK_ROWS):
            monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
            by_columns, by_rows, taken = _both(
                monkeypatch, lambda plan=plan: generate_rows(plan, root=_root())
            )
            assert by_columns == by_rows
            assert taken == [True]


def test_a_slice_holds_the_rows_the_whole_run_does() -> None:
    plan = with_conditions(plan_of(FOREST, rows=300), _GROUPS[:2] + [group({"c": "x1"}, 80)])
    root = _root()
    whole = generate_rows(plan, root=root)
    assert (
        generate_row_slice(plan, 0, 150, root=root) + generate_row_slice(plan, 150, 300, root=root)
        == whole
    )
    assert generate_row_slice(plan, 119, 121, root=root) == whole[119:121]


# ---------------------------------------------------------------------------
# Frequencies
# ---------------------------------------------------------------------------


def _marginals(spec: Spec, seed: int, fixed: Mapping[str, str]) -> Dict[Tuple[str, ...], Fraction]:
    """The exact probability of every assignment of every column, given *fixed*."""
    _, declared = declare(spec, seed)
    order = [name for name, _, _ in spec]
    levels = {name: tuple(own) for name, own, _ in spec}
    joint: Dict[Tuple[str, ...], Fraction] = {}

    def walk(index: int, drawn: Dict[str, str], weight: Fraction) -> None:
        if index == len(order):
            joint[tuple(drawn[name] for name in order)] = weight
            return
        name = order[index]
        for level in [fixed[name]] if name in fixed else levels[name]:
            part = _conditional(declared, name, level, drawn)
            if part:
                walk(index + 1, {**drawn, name: level}, weight * part)

    walk(0, {}, Fraction(1))
    total = sum(joint.values(), Fraction(0))
    return {key: weight / total for key, weight in joint.items()}


@pytest.mark.parametrize(
    ("spec", "fixed"),
    [
        (TWO_CLIQUES, {"e": "x1"}),
        (THREE_CLIQUES, {"d": "x2", "a": "x1"}),
        (FOREST, {"c": "x1"}),
    ],
    ids=["two-cliques", "three-cliques", "forest"],
)
def test_the_frequencies_of_a_large_run_fall_within_binomial_bounds_of_the_exact_conditionals(
    spec: Spec, fixed: Mapping[str, str]
) -> None:
    n = 30_000
    plan = with_conditions(plan_of(spec, seed=3, rows=n), [group(fixed, n)])
    rows = generate_rows(plan, root=_root(11))
    joint = _marginals(spec, 3, fixed)
    names = [name for name, _, _ in spec]

    def check(positions: Tuple[int, ...]) -> None:
        exact: Dict[Tuple[str, ...], Fraction] = {}
        for key, weight in joint.items():
            part = tuple(key[i] for i in positions)
            exact[part] = exact.get(part, Fraction(0)) + weight
        seen: Dict[Tuple[str, ...], int] = {}
        for row in rows:
            part = tuple(row[i] for i in positions)
            seen[part] = seen.get(part, 0) + 1
        assert set(seen) <= set(exact)  # nothing the conditionals rule out is drawn
        for part, probability in exact.items():
            p = float(probability)
            sigma = math.sqrt(n * p * (1 - p))
            assert abs(seen.get(part, 0) - n * p) <= 5 * sigma + 1, (positions, part)

    for position in range(len(names)):
        check((position,))
    check((0, 1))
    check((len(names) - 2, len(names) - 1))


def test_what_a_group_does_not_fix_is_drawn_as_the_conditionals_say_without_it() -> None:
    """A group that fixes nothing a column depends on leaves that column's frequencies alone."""
    n = 20_000
    plan = with_conditions(plan_of(FOREST, seed=5, rows=n), [group({"f": "x1"}, n)])
    conditioned = plan.conditions
    assert conditioned is not None
    assert conditioned.touched == {"f"}
    rows = generate_rows(plan, root=_root(3))
    joint = _marginals(FOREST, 5, {"f": "x1"})
    for position in range(5):
        exact: Dict[str, Fraction] = {}
        for key, weight in joint.items():
            exact[key[position]] = exact.get(key[position], Fraction(0)) + weight
        for level, probability in exact.items():
            p = float(probability)
            count = sum(1 for row in rows if row[position] == level)
            assert abs(count - n * p) <= 5 * math.sqrt(n * p * (1 - p)) + 1
