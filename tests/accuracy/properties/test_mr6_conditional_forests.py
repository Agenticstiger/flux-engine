# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Metamorphic relation 6: a forest of conditional draws keeps every generator invariant.

Level: the generator, under one fixed seam digest ``H`` and one fixed seed-tree root.
A forest is a set of categorical and histogram columns in which any column may be drawn
``given`` an earlier categorical column (``levelWeightsGiven`` for a categorical child,
``binWeightsGiven`` for a histogram child). Hypothesis draws random valid forests: 2 to 7
columns, every categorical kind (STRING, INT, BOOL), both histogram kinds (DECIMAL(18,4),
INT), chains, mixed NFC and NFD spellings of a parent level's key, zero weights, and a
shuffled declared column order.

The relations, one test each:

* same bytes: two runs agree, and ``root=`` equals ``source=GeneratorStreams(root=...)``;
* column order: permuting the declared order leaves every column's values unchanged,
  compared by name;
* column-first: ``generate_column`` equals that column of the row-first table, under both
  stream sources;
* slices: random cut points over ``[0, rows]`` join to exactly the whole table;
* more rows: a longer plan has the shorter plan's table as its prefix;
* conditioning respected: every drawn level and bin carries positive weight in the row of
  its parent's drawn level, and unconditional histograms respect ``binWeights``;
* roots unchanged: replacing every conditional declaration with an unconditional one
  leaves every root column's values identical;
* refusals: exactly one corruption of a valid forest (a dropped or unknown parent level, a
  cycle, a histogram parent, a negative weight, an all-zero row, a missing parent column)
  is refused at plan time with ``FLX-3402`` and never with another exception type.

Pass criterion: every relation holds for every Hypothesis-drawn forest, seed and cut.
"""

from __future__ import annotations

import copy
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from tests.accuracy.harness.deps import require

require("hypothesis")

import pytest  # noqa: E402
from hypothesis import given, note  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.generator.paths import GeneratorStreams  # noqa: E402
from flux_engine.generator.rows import (  # noqa: E402
    GeneratorPlan,
    build_plan,
    generate_column,
    generate_row_slice,
    generate_rows,
)
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.properties import _support as support  # noqa: E402

FAMILY = "properties"

# ---------------------------------------------------------------------------
# The forest model
# ---------------------------------------------------------------------------

#: Level names for STRING categoricals; the non-ASCII ones are built with ``chr`` only.
STRING_POOL: Tuple[str, ...] = (
    "DE",
    "FR",
    "Bern",
    "Basel",
    "Z" + chr(0xFC) + "rich",
    "Gen" + chr(0xE8) + "ve",
    "Malm" + chr(0xF6),
    "Ma" + chr(0xF1) + "ana",
)
assert all(unicodedata.normalize("NFC", name) == name for name in STRING_POOL)

SHARE_PLACES = 20
UNKNOWN_LEVEL = {"STRING": "zz_unknown", "INT": "99999", "BOOL": "maybe"}


@dataclass(frozen=True)
class Col:
    """One column of a forest: its declaration and what that declaration means."""

    name: str
    kind: str  # "cat" or "hist"
    sql_type: str
    parent: Optional[str]
    levels: Tuple[str, ...]  # the canonical (NFC) level set; empty for a histogram
    bins: Tuple[int, ...]  # the edges; empty for a categorical
    decl: Dict[str, Any]
    shares: Optional[Dict[str, Fraction]]  # a root categorical declared by ``levels``
    weights: Optional[Tuple[int, ...]]  # a root histogram's ``binWeights``
    table: Optional[Dict[str, Any]]  # canonical parent level -> weights (list or dict of int)


@dataclass(frozen=True)
class Forest:
    cols: Tuple[Col, ...]
    order: Tuple[str, ...]  # the declared column order

    def by_name(self) -> Dict[str, Col]:
        return {col.name: col for col in self.cols}

    def declared(self) -> Dict[str, Dict[str, Any]]:
        return {col.name: col.decl for col in self.cols}


def _render_shares(weights: List[int]) -> List[str]:
    """Exact shares at 20 places that sum to exactly 1, the residue on the last level."""
    units = 10**SHARE_PLACES
    total = sum(weights)
    parts = [(weight * units) // total for weight in weights[:-1]]
    parts.append(units - sum(parts))
    return [f"{part // units}.{part % units:0{SHARE_PLACES}d}" for part in parts]


def _share_decl(levels: List[str], weights: List[int]) -> Dict[str, str]:
    return dict(zip(levels, _render_shares(weights), strict=True))


def _level_of(value: Any) -> str:
    """The declared level a drawn value renders from (BOOL before INT: bool is an int)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return unicodedata.normalize("NFC", str(value))


@st.composite
def _levels(draw: Any, sql_type: str) -> List[str]:
    if sql_type == "BOOL":
        return draw(st.permutations(["true", "false"]))
    count = draw(st.integers(min_value=2, max_value=4))
    if sql_type == "INT":
        numbers = draw(
            st.lists(
                st.integers(min_value=-5, max_value=20),
                min_size=count,
                max_size=count,
                unique=True,
            )
        )
        return [str(number) for number in numbers]
    return draw(st.lists(st.sampled_from(STRING_POOL), min_size=count, max_size=count, unique=True))


@st.composite
def _weight_row(draw: Any, size: int) -> List[int]:
    """Integer weights 0 to 9, zeros often, with a positive sum."""
    weights = draw(
        st.lists(
            st.sampled_from([0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9]), min_size=size, max_size=size
        )
    )
    if sum(weights) == 0:
        weights[draw(st.integers(min_value=0, max_value=size - 1))] = draw(
            st.integers(min_value=1, max_value=9)
        )
    return weights


def _spell(draw: Any, level: str) -> str:
    """The key spelling for *level*: its NFD form for some non-ASCII levels."""
    decomposed = unicodedata.normalize("NFD", level)
    if decomposed != level and draw(st.booleans()):
        return decomposed
    return level


@st.composite
def _categorical(draw: Any, name: str, parent: Optional[Col]) -> Col:
    sql_type = draw(st.sampled_from(["STRING", "INT", "BOOL"]))
    levels = draw(_levels(sql_type))
    if parent is None:
        if sql_type == "STRING" and draw(st.booleans()):
            decl: Dict[str, Any] = {"support": {"values": list(levels)}}
            shares = None
        else:
            weights = draw(_weight_row(len(levels)))
            rendered = _share_decl(levels, weights)
            decl = {"levels": rendered}
            shares = {level: Fraction(text) for level, text in rendered.items()}
        return Col(name, "cat", sql_type, None, tuple(levels), (), decl, shares, None, None)
    table: Dict[str, Any] = {}
    declared: Dict[str, Any] = {}
    for level in parent.levels:
        row = draw(_weight_row(len(levels)))
        table[level] = dict(zip(levels, row, strict=True))
        declared[_spell(draw, level)] = {lv: str(w) for lv, w in zip(levels, row, strict=True)}
    decl = {"given": parent.name, "levelWeightsGiven": declared}
    return Col(name, "cat", sql_type, parent.name, tuple(levels), (), decl, None, None, table)


@st.composite
def _histogram(draw: Any, name: str, parent: Optional[Col]) -> Col:
    sql_type = draw(st.sampled_from(["DECIMAL(18,4)", "INT"]))
    count = draw(st.integers(min_value=2, max_value=8))
    edges = sorted(
        draw(
            st.lists(
                st.integers(min_value=-50, max_value=500),
                min_size=count + 1,
                max_size=count + 1,
                unique=True,
            )
        )
    )
    bins = [str(edge) for edge in edges]
    if parent is None:
        weights = draw(_weight_row(count))
        decl: Dict[str, Any] = {"bins": bins, "binWeights": [str(w) for w in weights]}
        return Col(name, "hist", sql_type, None, (), tuple(edges), decl, None, tuple(weights), None)
    table: Dict[str, Any] = {}
    declared: Dict[str, Any] = {}
    for level in parent.levels:
        row = draw(_weight_row(count))
        table[level] = row
        declared[_spell(draw, level)] = [str(w) for w in row]
    decl = {"bins": bins, "given": parent.name, "binWeightsGiven": declared}
    return Col(name, "hist", sql_type, parent.name, (), tuple(edges), decl, None, None, table)


@st.composite
def forests(draw: Any, need_conditional: bool = False) -> Forest:
    """A random valid forest. With *need_conditional*, column 1 is drawn given column 0."""
    size = draw(st.integers(min_value=2, max_value=7))
    cols: List[Col] = []
    for index in range(size):
        name = f"c{index}"
        cats = [col for col in cols if col.kind == "cat"]
        parent: Optional[Col] = None
        if cats and (draw(st.booleans()) or (need_conditional and index == 1)):
            parent = cats[0] if (need_conditional and index == 1) else draw(st.sampled_from(cats))
        categorical = True if (need_conditional and index == 0) else draw(st.booleans())
        cols.append(draw(_categorical(name, parent) if categorical else _histogram(name, parent)))
    order = draw(st.permutations([col.name for col in cols]))
    return Forest(tuple(cols), tuple(order))


def make_plan(
    forest: Forest,
    rows: int,
    *,
    order: Optional[Tuple[str, ...]] = None,
    decls: Optional[Dict[str, Any]] = None,
) -> GeneratorPlan:
    by_name = forest.by_name()
    schema = [
        {"name": name, "type": by_name[name].sql_type, "required": True}
        for name in (order or forest.order)
    ]
    return build_plan(
        support.expose_of(schema),
        sim_id=support.SIM_ID,
        seam_digest=support.H,
        rows=rows,
        fidelity={"columns": forest.declared() if decls is None else decls},
    )


def columns_of(rows: Tuple[Any, ...], order: Tuple[str, ...]) -> Dict[str, Tuple[Any, ...]]:
    """Row-first table to name -> value sequence."""
    return {name: tuple(row[index] for row in rows) for index, name in enumerate(order)}


def reprs(table: Dict[str, Tuple[Any, ...]]) -> Dict[str, List[str]]:
    return {name: [repr(value) for value in values] for name, values in table.items()}


# ---------------------------------------------------------------------------
# Corruptions
# ---------------------------------------------------------------------------


def _table_key(col: Col) -> str:
    return "levelWeightsGiven" if col.kind == "cat" else "binWeightsGiven"


def _weight_rows(forest: Forest, decls: Dict[str, Any]) -> List[Any]:
    """Every mutable weight row (list or dict) in the declarations."""
    found: List[Any] = []
    for col in forest.cols:
        if col.parent is not None:
            found.extend(decls[col.name][_table_key(col)].values())
        elif col.kind == "hist":
            found.append(decls[col.name]["binWeights"])
    return found


def _descendants(forest: Forest, root: Col) -> List[Col]:
    found: List[Col] = []
    frontier = [root.name]
    while frontier:
        children = [
            col
            for col in forest.cols
            if col.parent in frontier and col.kind == "cat" and col not in found
        ]
        found.extend(children)
        frontier = [col.name for col in children]
    return found


@st.composite
def corrupted(draw: Any) -> Tuple[Forest, Dict[str, Any], str]:
    """A valid forest with exactly one corruption applied to its declarations."""
    forest = draw(forests(need_conditional=True))
    decls = copy.deepcopy(forest.declared())
    cond = [col for col in forest.cols if col.parent is not None]
    hists = [col for col in forest.cols if col.kind == "hist"]
    kinds = ["drop_outer", "unknown_level", "missing_given", "negative", "all_zero"]
    if any(col.kind == "cat" for col in cond):
        kinds.append("drop_inner")
    if any(hist.name != col.name for hist in hists for col in cond):
        kinds.append("given_hist")
    cycles = [
        (root, desc)
        for root in forest.cols
        if root.kind == "cat" and root.parent is None
        for desc in _descendants(forest, root)
    ]
    if cycles:
        kinds.append("cycle")
    kind = draw(st.sampled_from(kinds))
    if kind in ("drop_outer", "drop_inner", "unknown_level", "missing_given"):
        pool = [col for col in cond if kind != "drop_inner" or col.kind == "cat"]
        col = draw(st.sampled_from(pool))
        decl = decls[col.name]
        if kind == "missing_given":
            decl["given"] = "no_such_column"
        elif kind == "unknown_level":
            table = decl[_table_key(col)]
            parent_type = forest.by_name()[col.parent].sql_type  # type: ignore[index]
            table[UNKNOWN_LEVEL[parent_type]] = copy.deepcopy(next(iter(table.values())))
        else:
            table = decl[_table_key(col)]
            outer = draw(st.sampled_from(sorted(table)))
            if kind == "drop_outer":
                del table[outer]
            else:
                del table[outer][draw(st.sampled_from(sorted(table[outer])))]
    elif kind == "given_hist":
        pairs = [(hist, col) for hist in hists for col in cond if hist.name != col.name]
        hist, col = draw(st.sampled_from(pairs))
        decls[col.name]["given"] = hist.name
    elif kind == "cycle":
        root, desc = draw(st.sampled_from(cycles))
        decls[root.name] = {
            "given": desc.name,
            "levelWeightsGiven": {level: {lv: "1" for lv in root.levels} for level in desc.levels},
        }
    else:
        row = draw(st.sampled_from(_weight_rows(forest, decls)))
        keys = list(range(len(row))) if isinstance(row, list) else sorted(row)
        if kind == "negative":
            row[draw(st.sampled_from(keys))] = "-1"
        else:
            for key in keys:
                row[key] = "0"
    note(f"corruption: {kind}")
    return forest, decls, kind


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _bin_of(value: Any, edges: Tuple[int, ...]) -> Optional[int]:
    """The half-open bin holding *value*, the last bin closed at the top."""
    for index in range(len(edges) - 1):
        if edges[index] <= value < edges[index + 1]:
            return index
    return len(edges) - 2 if value == edges[-1] else None


def check_conditioning(forest: Forest, table: Dict[str, Tuple[Any, ...]], rows: int) -> None:
    for col in forest.cols:
        for row in range(rows):
            value = table[col.name][row]
            parent_level = (
                _level_of(table[col.parent][row]) if col.parent is not None else None  # type: ignore[index]
            )
            where = (col.name, row, value, parent_level)
            if col.kind == "cat":
                level = _level_of(value)
                assert level in col.levels, where
                if col.parent is None:
                    if col.shares is not None:
                        assert col.shares[level] > 0, where
                else:
                    assert col.table[parent_level][level] > 0, where  # type: ignore[index]
            else:
                index = _bin_of(value, col.bins)
                assert index is not None, where
                weights = col.weights if col.parent is None else col.table[parent_level]  # type: ignore[index]
                assert weights[index] > 0, where  # type: ignore[index]


def unconditional_decls(forest: Forest) -> Dict[str, Any]:
    """Every conditional declaration replaced by an unconditional one (roots untouched)."""
    decls = copy.deepcopy(forest.declared())
    for col in forest.cols:
        if col.parent is None:
            continue
        if col.kind == "cat":
            levels = list(col.levels)
            decls[col.name] = {"levels": _share_decl(levels, [1] * len(levels))}
        else:
            decls[col.name] = {
                "bins": [str(edge) for edge in col.bins],
                "binWeights": ["1"] * (len(col.bins) - 1),
            }
    return decls


# ---------------------------------------------------------------------------
# The relations
# ---------------------------------------------------------------------------

seeds = st.integers(min_value=0, max_value=2**62)


def test_same_bytes_and_both_stream_sources_agree(tier: Tier, accuracy_log: ResultLog) -> None:
    rows = tier.pick(pr=16, nightly=60)
    examples = {"n": 0}

    @tier.property_settings(pr=120, nightly=300, label="mr6-same-bytes")
    @given(forest=forests(), seed=seeds)
    def prop(forest: Forest, seed: int) -> None:
        plan = make_plan(forest, rows)
        first = generate_rows(plan, root=support.root_of(seed))
        assert generate_rows(plan, root=support.root_of(seed)) == first
        streams = GeneratorStreams(root=support.root_of(seed))
        assert generate_rows(plan, source=streams) == first
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr6-same-bytes",
        criterion="two runs agree and root= equals source=GeneratorStreams(root=)",
        passed=True,
        n=rows,
        metrics={"examples": examples["n"]},
    )


def test_permuting_declared_order_leaves_each_column_unchanged(
    tier: Tier, accuracy_log: ResultLog
) -> None:
    rows = tier.pick(pr=16, nightly=60)
    examples = {"n": 0}

    @tier.property_settings(pr=120, nightly=300, label="mr6-column-order")
    @given(forest=forests(), data=st.data(), seed=seeds)
    def prop(forest: Forest, data: Any, seed: int) -> None:
        other = tuple(data.draw(st.permutations(list(forest.order))))
        base = columns_of(
            generate_rows(make_plan(forest, rows), root=support.root_of(seed)), forest.order
        )
        moved = columns_of(
            generate_rows(make_plan(forest, rows, order=other), root=support.root_of(seed)),
            other,
        )
        assert reprs(base) == reprs(moved)
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr6-column-order",
        criterion="every column's values identical under any permutation of the declared order",
        passed=True,
        n=rows,
        metrics={"examples": examples["n"]},
    )


def test_column_first_equals_row_first_under_both_sources(
    tier: Tier, accuracy_log: ResultLog
) -> None:
    rows = tier.pick(pr=12, nightly=40)
    examples = {"n": 0}

    @tier.property_settings(pr=100, nightly=250, label="mr6-column-first")
    @given(forest=forests(), seed=seeds)
    def prop(forest: Forest, seed: int) -> None:
        plan = make_plan(forest, rows)
        whole = columns_of(generate_rows(plan, root=support.root_of(seed)), forest.order)
        for name in forest.order:
            by_root = generate_column(plan, name, root=support.root_of(seed))
            by_source = generate_column(
                plan, name, source=GeneratorStreams(root=support.root_of(seed))
            )
            assert by_root == whole[name], name
            assert by_source == whole[name], name
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr6-column-first",
        criterion="generate_column equals the row-first column under root= and source=",
        passed=True,
        n=rows,
        metrics={"examples": examples["n"]},
    )


def test_slices_join_into_the_whole_table(tier: Tier, accuracy_log: ResultLog) -> None:
    rows = tier.pick(pr=24, nightly=80)
    examples = {"n": 0}

    @tier.property_settings(pr=120, nightly=300, label="mr6-slices")
    @given(
        forest=forests(),
        seed=seeds,
        cuts=st.lists(st.integers(min_value=0, max_value=rows), max_size=6),
    )
    def prop(forest: Forest, seed: int, cuts: List[int]) -> None:
        plan = make_plan(forest, rows)
        whole = generate_rows(plan, root=support.root_of(seed))
        edges = sorted({0, rows, *cuts})
        joined = tuple(
            row
            for start, stop in zip(edges, edges[1:], strict=False)
            for row in generate_row_slice(plan, start, stop, root=support.root_of(seed))
        )
        assert joined == whole, edges
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr6-slices",
        criterion="slices at random cut points join to exactly the whole table",
        passed=True,
        n=rows,
        metrics={"examples": examples["n"]},
    )


def test_more_rows_extend_the_shorter_table(tier: Tier, accuracy_log: ResultLog) -> None:
    rows = tier.pick(pr=12, nightly=40)
    examples = {"n": 0}

    @tier.property_settings(pr=120, nightly=300, label="mr6-more-rows")
    @given(forest=forests(), seed=seeds, extra=st.integers(min_value=1, max_value=rows))
    def prop(forest: Forest, seed: int, extra: int) -> None:
        short = generate_rows(make_plan(forest, rows), root=support.root_of(seed))
        long = generate_rows(make_plan(forest, rows + extra), root=support.root_of(seed))
        assert long[:rows] == short
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr6-more-rows",
        criterion="a plan with more rows has the shorter plan's table as its prefix",
        passed=True,
        n=rows,
        metrics={"examples": examples["n"]},
    )


def test_every_draw_respects_its_conditional_weights(tier: Tier, accuracy_log: ResultLog) -> None:
    rows = tier.pick(pr=40, nightly=120)
    examples = {"n": 0}

    @tier.property_settings(pr=160, nightly=400, label="mr6-conditioning")
    @given(forest=forests(), seed=seeds)
    def prop(forest: Forest, seed: int) -> None:
        table = columns_of(
            generate_rows(make_plan(forest, rows), root=support.root_of(seed)), forest.order
        )
        check_conditioning(forest, table, rows)
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr6-conditioning",
        criterion="every drawn level and bin has positive weight in its parent's row",
        passed=True,
        n=rows,
        metrics={"examples": examples["n"]},
    )


def test_root_columns_are_unchanged_by_conditioning(tier: Tier, accuracy_log: ResultLog) -> None:
    rows = tier.pick(pr=16, nightly=60)
    examples = {"n": 0}

    @tier.property_settings(pr=120, nightly=300, label="mr6-roots-unchanged")
    @given(forest=forests(), seed=seeds)
    def prop(forest: Forest, seed: int) -> None:
        conditioned = columns_of(
            generate_rows(make_plan(forest, rows), root=support.root_of(seed)), forest.order
        )
        plain = columns_of(
            generate_rows(
                make_plan(forest, rows, decls=unconditional_decls(forest)),
                root=support.root_of(seed),
            ),
            forest.order,
        )
        for col in forest.cols:
            if col.parent is None:
                assert reprs({col.name: conditioned[col.name]}) == reprs(
                    {col.name: plain[col.name]}
                ), col.name
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr6-roots-unchanged",
        criterion="making every conditional column unconditional leaves each root column unchanged",
        passed=True,
        n=rows,
        metrics={"examples": examples["n"]},
    )


def test_every_corruption_is_refused_with_flx_3402(tier: Tier, accuracy_log: ResultLog) -> None:
    examples = {"n": 0}

    @tier.property_settings(pr=240, nightly=600, label="mr6-refusals")
    @given(case=corrupted())
    def prop(case: Tuple[Forest, Dict[str, Any], str]) -> None:
        forest, decls, kind = case
        with pytest.raises(FluxError) as refused:
            make_plan(forest, 4, decls=decls)
        assert refused.value.code.code == "FLX-3402", (kind, str(refused.value))
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr6-refusals",
        criterion="exactly one corruption of a valid forest is refused with FLX-3402",
        passed=True,
        n=4,
        metrics={"examples": examples["n"]},
    )
