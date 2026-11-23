# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Kernel 0.2.0's column path holds the rows the per-row path holds (ADR 0010, step 4).

:func:`flux_engine.generator.columns.column_rows` computes a kernel 0.2.0 slice a column at
a time. The per-row path is the oracle: the related-star-v2 golden and the determinism
manifest's ``run2`` pins were made by it. Each test below generates a plan both ways, the
per-row way by turning the column path off, and compares the rows by their ``repr``, so a
value of another class or a decimal of another exponent fails as a changed byte would.
Chunks are made small, so the tests cross their boundaries too.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Context, Decimal, localcontext
from functools import partial
from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SeedTreeRoot, derive_root, digest_bytes
from flux_engine.generator import columns, relations
from flux_engine.generator.families import ColumnType, Family
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.rows import (
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_row_slice,
    generate_rows,
)

from .conftest import SIM_ID, TIME_BOUNDS, H, expose_from_schema, fidelity
from .test_relations import _column, _expose, _pair_contract, _primary, _rel, related_plans

pytestmark = pytest.mark.unit


def _root() -> SeedTreeRoot:
    return derive_root(digest_bytes(H), 42, seed_tree_version=kernel2.SEED_TREE_VERSION)


def _both(monkeypatch: pytest.MonkeyPatch, make: Callable[[], Any]) -> Tuple[str, str, List[bool]]:
    """``repr`` of *make*'s rows by the column path and by the per-row path.

    The third item says, for each slice the column path was asked for, whether it computed
    it, so a test can tell a comparison of the two paths from one of the per-row path with
    itself.
    """
    taken: List[bool] = []
    column_rows = columns.column_rows

    def spy(*args: Any) -> Any:
        computed = column_rows(*args)
        taken.append(computed is not None)
        return computed

    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", spy)
        by_columns = repr(make())
    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", lambda *args: None)
        by_rows = repr(make())
    return by_columns, by_rows, taken


# ---------------------------------------------------------------------------
# Plain plans: every family and every branch of a family
# ---------------------------------------------------------------------------

_SCHEMA: List[Dict[str, Any]] = [
    {"name": "widget_id", "type": "STRING", "required": True, "semanticType": "identifier"},
    {"name": "token", "type": "UUID", "semanticType": "identifier"},
    {"name": "short_id", "type": "STRING", "semanticType": "identifier"},
    {"name": "market", "type": "STRING"},
    {"name": "tier", "type": "STRING"},
    {"name": "count", "type": "INT"},
    {"name": "none_yet", "type": "INT"},
    {"name": "crowd", "type": "INT"},
    {"name": "wait", "type": "DECIMAL(12,3)"},
    {"name": "amount", "type": "DECIMAL(18,4)"},
    {"name": "fine", "type": "DECIMAL(38,18)"},
    {"name": "score", "type": "DECIMAL(9,2)"},
    {"name": "above", "type": "DECIMAL(9,2)"},
    {"name": "flat", "type": "DECIMAL(9,2)"},
    {"name": "level", "type": "INT"},
    {"name": "spread", "type": "DECIMAL(12,4)"},
    {"name": "steady", "type": "INT"},
    {"name": "score_n", "type": "DECIMAL(9,2)"},
    {"name": "count_n", "type": "INT"},
    {"name": "wait_n", "type": "DECIMAL(12,3)"},
    {"name": "waited_at", "type": "TIMESTAMP"},
    {"name": "waited_on", "type": "DATE"},
    {"name": "day_bin", "type": "DATE"},
    {"name": "size", "type": "INT"},
    {"name": "price", "type": "DECIMAL(9,2)"},
    {"name": "spend", "type": "DECIMAL(18,2)"},
    {"name": "visits", "type": "INT"},
    {"name": "active", "type": "BOOL"},
    {"name": "seen_on", "type": "DATE"},
    {"name": "seen_at", "type": "TIMESTAMP"},
    {"name": "wide", "type": "INT"},
    {"name": "caf\u00e9", "type": "INT"},
]

_DECLARED: Dict[str, Any] = {
    "token": {"identifierLength": 32},
    "short_id": {"identifierLength": 5},
    "market": {"support": {"values": ["IE", "GB", "FR"]}, "nullFraction": "0.1"},
    "tier": {"levels": {"gold": "0.2", "silver": "0.3", "bronze": "0.5"}},
    "count": {"mean": "3"},
    "none_yet": {"mean": "0"},
    # A Poisson count clamped to its declared range.
    "crowd": {"family": "rate", "mean": "2500", "range": {"min": "2450", "max": "2550"}},
    "wait": {"family": "rate", "rate": "0.25"},
    "amount": {"family": "uniform", "range": {"min": "0", "max": "10000"}, "nullFraction": "0.05"},
    # More than 2**63 grid points: the bounded draw's high words cross the sign bit.
    "fine": {"family": "uniform", "range": {"min": "0", "max": "10"}},
    "score": {"range": {"min": "1", "max": "99"}, "mean": "60", "stddev": "8"},
    # An interval above its mean, which the normal mirrors.
    "above": {"range": {"min": "2", "max": "6"}, "mean": "0", "stddev": "1"},
    # A constant normal whose mean rounds above the bound it is clamped to.
    "flat": {"range": {"min": "1", "max": "4.555"}, "mean": "4.555", "stddev": "0"},
    "level": {
        "family": "bounded-normal",
        "range": {"min": "1", "max": "10"},
        "mean": "5",
        "stddev": "2",
    },
    # A normal whose mean and standard deviation come from its range.
    "spread": {"family": "bounded-normal", "range": {"min": "0.0001", "max": "7.7777"}},
    "steady": {
        "family": "bounded-normal",
        "range": {"min": "1", "max": "9"},
        "mean": "4",
        "stddev": "0",
    },
    # Each family with nulls, so its value is drawn at index 1.
    "score_n": {
        "range": {"min": "1", "max": "99"},
        "mean": "60",
        "stddev": "8",
        "nullFraction": "0.1",
    },
    "count_n": {"mean": "3", "nullFraction": "0.1"},
    "wait_n": {"family": "rate", "rate": "0.25", "nullFraction": "0.1"},
    # A wait added to the start of a range of instants, and of days.
    "waited_at": {
        "family": "rate",
        "rate": "0.001",
        "range": {"min": "2026-07-01T00:00:00Z", "max": "2026-09-30T23:59:59Z"},
    },
    "waited_on": {
        "family": "rate",
        "rate": "0.1",
        "range": {"min": "2026-07-01", "max": "2026-09-30"},
    },
    "day_bin": {
        "range": {"min": "2026-07-01", "max": "2026-09-30"},
        "bins": ["2026-07-01", "2026-08-01", "2026-09-30"],
        "binWeights": ["1", "2"],
    },
    # Levels the column's type reads as numbers.
    "size": {"levels": {"1": "0.5", "2": "0.3", "10": "0.2"}},
    "price": {"levels": {"9.99": "0.5", "19.99": "0.5"}},
    "spend": {
        "range": {"min": "0", "max": "300"},
        "bins": ["0", "100", "200", "300"],
        "binWeights": ["1", "2", "1"],
    },
    "visits": {
        "range": {"min": "0", "max": "30"},
        "bins": ["0", "10", "30"],
        "binWeights": ["3", "1"],
    },
    "active": {"levels": {"true": "0.25", "false": "0.75"}, "nullFraction": "0.2"},
    "seen_on": {"range": {"min": "2026-07-01", "max": "2026-09-30"}},
    "seen_at": {"range": {"min": "2026-07-01T00:00:00Z", "max": "2026-09-30T23:59:59Z"}},
    # 2**64 grid points: the bounded draw is the word itself.
    "wide": {"range": {"min": str(-(1 << 63)), "max": str((1 << 63) - 1)}},
    "caf\u00e9": {"range": {"min": "-5", "max": "5"}},
}


def _plan(
    rows: int = 150,
    schema: Sequence[Mapping[str, Any]] = _SCHEMA,
    declared: Mapping[str, Any] = _DECLARED,
) -> GeneratorPlan:
    return build_plan(
        expose_from_schema(list(schema)),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=rows,
        fidelity=fidelity(declared),
        time_bounds=TIME_BOUNDS,
        generator_version=GENERATOR_VERSION_2,
    )


@pytest.mark.parametrize("chunk", [7, 64, columns.CHUNK_ROWS])
def test_every_family_is_drawn_a_column_at_a_time_as_the_per_row_path_draws_it(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    plan, root = _plan(rows=300), _root()
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows
    assert taken == [True]
    data = generate_rows(plan, root=root)
    # The plan reached every value class, nulls among them.
    assert {type(value).__name__ for row in data for value in row} >= {
        "str",
        "int",
        "Decimal",
        "bool",
        "date",
        "NoneType",
    }


@pytest.mark.parametrize(
    ("start", "stop"),
    [(0, 0), (0, 1), (6, 8), (7, 14), (13, 29), (100, 150), (149, 150), (0, 150)],
)
def test_a_slice_anywhere_is_the_per_row_slice(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)
    plan, root = _plan(), _root()
    by_columns, by_rows, taken = _both(
        monkeypatch, lambda: generate_row_slice(plan, start, stop, root=root)
    )
    assert by_columns == by_rows
    assert taken == [True]


#: Declarations build_plan accepts and kernel 0.2.0 refuses when it draws (FLX-5021).
_REFUSED: Dict[str, Tuple[str, Dict[str, Any]]] = {
    "a normal finer than scale 22": (
        "DECIMAL(38,23)",
        {"range": {"min": "0", "max": "1"}, "mean": "0.5", "stddev": "0.1"},
    ),
    "a normal wider than 2**52 units": (
        "DECIMAL(38,6)",
        {"range": {"min": "0", "max": "1e20"}, "mean": "5e19", "stddev": "1e18"},
    ),
    "a rate below 2**-900": ("DECIMAL(38,6)", {"family": "rate", "rate": "1e-300"}),
}


@pytest.mark.parametrize("refused", sorted(_REFUSED))
def test_a_refused_declaration_is_refused_as_the_per_row_path_refuses_it(
    monkeypatch: pytest.MonkeyPatch, refused: str
) -> None:
    """The column path leaves the slice to the per-row path, which raises what it raises."""
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)
    type_, declared = _REFUSED[refused]
    schema = [*_SCHEMA[:4], {"name": "refused", "type": type_}, *_SCHEMA[4:8]]
    names = {column["name"] for column in schema}
    kept = {name: value for name, value in _DECLARED.items() if name in names}
    plan = _plan(schema=schema, declared={**kept, "refused": declared})
    root = _root()

    def raised() -> str:
        with pytest.raises(FluxError) as caught:
            generate_row_slice(plan, 9, 40, root=root)
        return f"{caught.value.code}: {caught.value}"

    by_columns, by_rows, taken = _both(monkeypatch, raised)
    assert by_columns == by_rows
    assert "FLX-5021" in by_columns
    assert taken == [False]


def test_a_refusal_only_the_column_path_meets_leaves_the_slice_to_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever the column path refuses, the slice is the per-row path's, rows or refusal."""
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)
    plan, root = _plan(), _root()
    expected = repr(generate_row_slice(plan, 3, 30, root=root))

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise FluxError(kernel2.FLX_5021, "refused by the column path alone")

    monkeypatch.setattr(kernel2, "column_poisson", refused)
    by_columns, by_rows, taken = _both(
        monkeypatch, lambda: generate_row_slice(plan, 3, 30, root=root)
    )
    assert by_columns == by_rows == expected
    assert taken == [False]


def test_a_refusal_in_a_later_chunk_gives_the_whole_slice_to_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunks already computed are dropped, and the per-row path computes the slice again."""
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)
    plan, root = _plan(), _root()
    expected = repr(generate_row_slice(plan, 0, 40, root=root))
    calls: List[int] = []
    column_poisson = kernel2.column_poisson
    per_chunk = sum(
        1
        for column_plan in plan.columns
        if column_plan.family is Family.RATE and column_plan.column_type is ColumnType.INT
    )
    refusing = 2 * per_chunk + 1  # the third chunk's first Poisson column

    def refused_third(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == refusing:
            raise FluxError(kernel2.FLX_5021, "refused in the third chunk")
        return column_poisson(*args, **kwargs)

    monkeypatch.setattr(kernel2, "column_poisson", refused_third)
    by_columns, by_rows, taken = _both(
        monkeypatch, lambda: generate_row_slice(plan, 0, 40, root=root)
    )
    assert by_columns == by_rows == expected
    assert taken == [False]
    assert len(calls) == refusing


def test_any_other_exception_in_the_column_path_leaves_the_slice_to_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, root = _plan(), _root()
    expected = repr(generate_rows(plan, root=root))

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("not a refusal")

    monkeypatch.setattr(kernel2, "column_categorical", broken)
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows == expected
    assert taken == [False]


def test_an_ambient_decimal_context_reaches_neither_path(monkeypatch: pytest.MonkeyPatch) -> None:
    plan, root = _plan(), _root()
    expected = repr(generate_rows(plan, root=root))

    def under_a_coarse_context() -> Any:
        with localcontext(Context(prec=3, rounding=ROUND_DOWN)):
            return generate_rows(plan, root=root)

    by_columns, by_rows, taken = _both(monkeypatch, under_a_coarse_context)
    assert by_columns == by_rows == expected
    assert taken == [True]


_GIVEN_SCHEMA: List[Dict[str, Any]] = [
    {"name": "market", "type": "STRING"},
    {"name": "tier", "type": "STRING"},
    {"name": "plan", "type": "STRING"},
    {"name": "amount", "type": "DECIMAL(18,4)"},
    {"name": "visits", "type": "INT"},
    {"name": "size", "type": "INT"},
    {"name": "flag", "type": "BOOL"},
    {"name": "price", "type": "DECIMAL(9,2)"},
    {"name": "note", "type": "STRING"},
]

_LEVEL_TABLE = {
    "DE": {"gold": "0.7", "silver": "0.3", "bronze": "0"},
    "FR": {"gold": "0.1", "silver": "0.2", "bronze": "0.7"},
    "IE": {"gold": "0", "silver": "0", "bronze": "1"},
}

_GIVEN_DECLARED: Dict[str, Any] = {
    "market": {"levels": {"DE": "0.5", "FR": "0.3", "IE": "0.2"}},
    "tier": {"levelWeightsGiven": _LEVEL_TABLE, "given": "market"},
    # A grandchild: given a column that is itself given, and the parent of a histogram.
    "plan": {
        "given": "tier",
        "levelWeightsGiven": {
            "gold": {"a": "1", "b": "1"},
            "silver": {"a": "3", "b": "1"},
            "bronze": {"a": "0", "b": "1"},
        },
    },
    "amount": {
        "bins": ["0", "10", "20"],
        "given": "market",
        "binWeightsGiven": {"DE": ["1", "0"], "FR": ["0", "1"], "IE": ["1", "1"]},
    },
    # A histogram with nulls, whose value is drawn at index 1 given a level.
    "visits": {
        "range": {"min": "0", "max": "30"},
        "bins": ["0", "10", "30"],
        "given": "tier",
        "nullFraction": "0.2",
        "binWeightsGiven": {"gold": ["3", "1"], "silver": ["1", "1"], "bronze": ["0", "1"]},
    },
    # Levels a column's type reads as numbers, and as booleans, drawn given a level.
    "size": {
        "given": "market",
        "levelWeightsGiven": {
            "DE": {"1": "0.5", "10": "0.5"},
            "FR": {"1": "0.2", "10": "0.8"},
            "IE": {"1": "1", "10": "0"},
        },
    },
    "flag": {
        "given": "market",
        "levelWeightsGiven": {
            "DE": {"true": "1", "false": "0"},
            "FR": {"true": "0.5", "false": "0.5"},
            "IE": {"true": "0", "false": "1"},
        },
        "nullFraction": "0.3",
    },
    "price": {
        "given": "plan",
        "levelWeightsGiven": {
            "a": {"9.99": "0.5", "19.99": "0.5"},
            "b": {"9.99": "0.9", "19.99": "0.1"},
        },
    },
    "note": {"levels": {"x": "0.5", "y": "0.5"}},
}


def _given_plan(rows: int = 300) -> GeneratorPlan:
    return _plan(rows=rows, schema=_GIVEN_SCHEMA, declared=_GIVEN_DECLARED)


@pytest.mark.parametrize("chunk", [1, 7, 64, columns.CHUNK_ROWS])
def test_columns_drawn_given_another_stay_on_the_column_path(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    plan, root = _given_plan(), _root()
    assert (
        columns.column_rows(
            plan, 0, plan.rows, CellStreams(root, plan.sim_id, plan.expose.expose_id)
        )
        is not None
    )
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows
    assert taken == [True]
    data = generate_rows(plan, root=root)
    # Every level of every parent was drawn, nulls among them, so the check means something.
    assert {row[0] for row in data} == {"DE", "FR", "IE"}
    assert {row[2] for row in data} == {"a", "b"}
    assert {row[7] for row in data} == {Decimal("9.99"), Decimal("19.99")}
    assert any(row[4] is None for row in data) and any(row[6] is None for row in data)


@pytest.mark.parametrize(("start", "stop"), [(0, 0), (0, 1), (5, 9), (13, 29), (299, 300)])
def test_a_slice_of_a_given_plan_is_the_per_row_slice(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)
    plan, root = _given_plan(), _root()
    by_columns, by_rows, taken = _both(
        monkeypatch, lambda: generate_row_slice(plan, start, stop, root=root)
    )
    assert by_columns == by_rows
    assert taken == [True]


@pytest.mark.parametrize("seed", [0, 1, 7, 99])
def test_a_given_plan_agrees_with_the_per_row_path_under_any_seed(
    monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    plan = _given_plan(rows=200)
    root = derive_root(digest_bytes(H), seed, seed_tree_version=kernel2.SEED_TREE_VERSION)
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows
    assert taken == [True]


def test_a_given_column_with_a_single_level_parent_agrees(monkeypatch: pytest.MonkeyPatch) -> None:
    declared = {
        "market": {"levels": {"DE": "1"}},
        "amount": {
            "bins": ["0", "10", "20"],
            "given": "market",
            "binWeightsGiven": {"DE": ["1", "3"]},
        },
    }
    schema = [{"name": "market", "type": "STRING"}, {"name": "amount", "type": "DECIMAL(18,4)"}]
    plan, root = _plan(schema=schema, declared=declared), _root()
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows
    assert taken == [True]


def test_rows_past_an_int64_array_take_the_per_row_path(monkeypatch: pytest.MonkeyPatch) -> None:
    plan, root = _plan(rows=kernel2.ARRAY_ROWS + 4), _root()
    last = kernel2.ARRAY_ROWS
    by_columns, by_rows, taken = _both(
        monkeypatch, lambda: generate_row_slice(plan, last - 3, last, root=root)
    )
    assert by_columns == by_rows
    assert taken == [True]
    by_columns, by_rows, taken = _both(
        monkeypatch, lambda: generate_row_slice(plan, last - 1, last + 2, root=root)
    )
    assert by_columns == by_rows
    assert taken == [False]


def _ruled_plan(
    contract: Mapping[str, Any], expose_id: str, statistics: Mapping[str, Any], **kwargs: Any
) -> GeneratorPlan:
    """:func:`tests.generator.test_rules.plan_of` under kernel 0.2.0."""
    from flux_engine.generator.relations import related_columns
    from flux_engine.generator.rules import plan_rules
    from flux_engine.seam.contract import compile_expose
    from flux_engine.seam.keys import compile_keys, mark_key_columns

    from . import test_rules

    keys = compile_keys(contract)
    expose_keys = None if keys is None else keys.expose(expose_id)
    related = related_columns(expose_keys)
    (entry,) = [e for e in contract["exposes"] if e["exposeId"] == expose_id]
    rules = plan_rules(
        contract_document=contract,
        expose_id=expose_id,
        settings=kwargs.pop("settings", None),
        related=related,
        mapping=None,
        has_fidelity=True,
        strict=False,
        flux_rules=kwargs.pop("flux_rules", None),
    )
    return build_plan(
        mark_key_columns(compile_expose(entry), expose_keys),
        sim_id=test_rules.SIM_ID,
        seam_digest=test_rules.SEAM,
        fidelity={"columns": dict(statistics)},
        related=related,
        rules=rules,
        generator_version=GENERATOR_VERSION_2,
        **kwargs,
    )


@pytest.mark.parametrize("table", ["calendar", "clock", "flux-side calendar"])
def test_a_plan_with_rules_takes_them_on_the_column_path(
    monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    """The column path's rows take the contract's rules and the bundle's, as the per-row
    path's do."""
    from . import test_flux_side_rules, test_rules

    monkeypatch.setattr(columns, "CHUNK_ROWS", 64)
    if table == "calendar":
        contract = test_rules._contract(test_rules.calendar_expose())
        plan = _ruled_plan(
            contract,
            "calendar",
            test_rules.CALENDAR_STATISTICS,
            settings=test_rules.SETTINGS,
            rows=400,
        )
    elif table == "clock":
        contract = test_rules._contract(test_rules.clock_expose())
        plan = _ruled_plan(
            contract, "clock", {"t_sk": {"range": {"min": "0", "max": "86399"}}}, rows=500
        )
    else:
        contract = test_flux_side_rules.calendar_contract()
        plan = _ruled_plan(
            contract,
            "calendar",
            test_flux_side_rules.STATISTICS,
            settings=test_rules.SETTINGS,
            flux_rules=test_flux_side_rules.FLUX_RULES,
            rows=200,
        )
    assert plan.rules is not None and plan.rules.order
    root = derive_root(
        digest_bytes(test_rules.SEAM), 7, seed_tree_version=kernel2.SEED_TREE_VERSION
    )
    by_columns, by_rows, taken = _both(monkeypatch, partial(generate_rows, plan, root=root))
    assert by_columns == by_rows
    assert taken == [True]
    data = test_rules.table(plan, generate_rows(plan, root=root))
    if table == "flux-side calendar":
        # The FLUX-side rules module's own checks: every rule of both kinds holds on every
        # row, and the calendar follows its key, 29 February 2000 included.
        test_flux_side_rules.test_every_rule_holds_on_every_row_when_evaluated((plan, data))
        test_flux_side_rules.test_the_calendar_follows_its_key((plan, data))
    else:
        assert test_rules.assert_every_rule_holds(contract, table, data) == plan.rows * len(
            plan.rules.order
        )


# ---------------------------------------------------------------------------
# Related plans: keys, foreign keys and the parent rows they name
# ---------------------------------------------------------------------------


def _related_root() -> SeedTreeRoot:
    return derive_root(
        digest_bytes("sha256:" + "a" * 64), 42, seed_tree_version=kernel2.SEED_TREE_VERSION
    )


def _star_v2(**kwargs: Any) -> Dict[str, GeneratorPlan]:
    return related_plans(generator_version=GENERATOR_VERSION_2, **kwargs)


@pytest.mark.parametrize("chunk", [64, columns.CHUNK_ROWS])
def test_the_related_star_is_drawn_a_column_at_a_time_as_the_per_row_path_draws_it(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    """Grid, identifier and composite keys; uniform, declared and one-to-one picks; nesting."""
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    plans, root = _star_v2(), _related_root()
    for expose_id, plan in plans.items():
        by_columns, by_rows, taken = _both(monkeypatch, partial(generate_rows, plan, root=root))
        assert by_columns == by_rows, expose_id
        assert taken == [True], expose_id


_PER_ROW_READS = (
    (relations.RelationReader, "_pick"),
    (relations.IdentifierKey, "value_at"),
    (relations.CompositeKey, "indexes_at"),
)


def _per_row_reads(monkeypatch: pytest.MonkeyPatch) -> Dict[str, int]:
    """Count the calls a related row makes when nothing was worked out for it in advance."""
    counts: Dict[str, int] = {}
    for owner, name in _PER_ROW_READS:
        real = getattr(owner, name)

        def counted(*args: Any, _real: Any = real, _name: str = name) -> Any:
            counts[_name] = counts.get(_name, 0) + 1
            return _real(*args)

        monkeypatch.setattr(owner, name, counted)
    return counts


def test_the_prefetch_serves_every_related_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under the column path no pick, identifier or key point is worked out row by row."""
    monkeypatch.setattr(columns, "CHUNK_ROWS", 64)
    plans, root = _star_v2(), _related_root()
    counts = _per_row_reads(monkeypatch)
    for plan in plans.values():
        generate_rows(plan, root=root)
    assert counts == {}
    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", lambda *args: None)
        for plan in plans.values():
            generate_rows(plan, root=root)
    # The counters count: the per-row path reads all three kinds.
    assert set(counts) == {"_pick", "value_at", "indexes_at"}


@pytest.mark.parametrize(("start", "stop"), [(63, 65), (100, 1000), (2999, 3000)])
def test_a_related_slice_anywhere_is_the_per_row_slice(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", 64)
    plans, root = _star_v2(), _related_root()
    by_columns, by_rows, taken = _both(
        monkeypatch, lambda: generate_row_slice(plans["store_sales"], start, stop, root=root)
    )
    assert by_columns == by_rows
    assert taken == [True]


def test_a_refusing_prefetch_leaves_a_related_slice_to_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flux_engine.generator.relations import RelationReader

    monkeypatch.setattr(columns, "CHUNK_ROWS", 64)
    plans, root = _star_v2(), _related_root()
    expected = repr(generate_rows(plans["store_sales"], root=root))

    def refused(*args: Any, **kwargs: Any) -> None:
        raise FluxError(kernel2.FLX_5021, "refused by the prefetch alone")

    monkeypatch.setattr(RelationReader, "prefetch", refused)
    by_columns, by_rows, taken = _both(
        monkeypatch, lambda: generate_rows(plans["store_sales"], root=root)
    )
    assert by_columns == by_rows == expected
    assert taken == [False]


def test_a_foreign_key_to_its_own_table_is_the_per_row_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manager column naming rows of the same table, with nulls, read through its own view."""
    monkeypatch.setattr(columns, "CHUNK_ROWS", 64)
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose(
                "emp",
                [_column("emp_id", "INT"), _column("mgr_id", "INT", False)],
                {
                    **_primary("emp", "emp_id"),
                    "relationships": [_rel("manager", "emp", ["mgr_id"], ["emp_id"])],
                },
            ),
        ],
    }
    plans = related_plans(
        contract,
        {"emp": ({"mgr_id": {"nullFraction": "0.2"}}, 500)},
        generator_version=GENERATOR_VERSION_2,
    )
    root = _related_root()
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plans["emp"], root=root))
    assert by_columns == by_rows
    assert taken == [True]
    assert "None" in by_columns


def test_a_composite_foreign_key_with_nulls_is_the_per_row_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = (
        {"a": {"range": {"min": "1", "max": "20"}}, "b": {"range": {"min": "1", "max": "20"}}},
        50,
    )
    plans = related_plans(
        _pair_contract(),
        {"p": parent, "c": ({"ca": {"nullFraction": "0.5"}}, 400)},
        generator_version=GENERATOR_VERSION_2,
    )
    root = _related_root()
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plans["c"], root=root))
    assert by_columns == by_rows
    assert taken == [True]
    assert "None" in by_columns


def test_a_nullable_one_to_one_foreign_key_is_the_per_row_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("u", [_column("u_id", "INT")], _primary("u", "u_id")),
            _expose(
                "p",
                [_column("p_id", "INT"), _column("p_user", "INT", False)],
                {
                    **_primary("p", "p_id"),
                    "relationships": [_rel("user", "u", ["p_user"], ["u_id"], "one_to_one")],
                },
            ),
        ],
    }
    plans = related_plans(
        contract,
        {"u": ({}, 300), "p": ({"p_user": {"nullFraction": "0.3"}}, 200)},
        generator_version=GENERATOR_VERSION_2,
    )
    root = _related_root()
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plans["p"], root=root))
    assert by_columns == by_rows
    assert taken == [True]
    assert "None" in by_columns


def test_a_one_to_one_pick_over_more_than_2_128_parent_rows_is_read_per_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A layout too wide for an array permutation is left to the per-row code, row by row."""
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("u", [_column("u_id", "STRING")], _primary("u", "u_id")),
            _expose(
                "p",
                [_column("p_user", "STRING")],
                {"relationships": [_rel("user", "u", ["p_user"], ["u_id"], "one_to_one")]},
            ),
        ],
    }
    plans = related_plans(
        contract,
        {"u": ({"u_id": {"identifierLength": 40}}, (1 << 128) + 7), "p": ({}, 30)},
        generator_version=GENERATOR_VERSION_2,
    )
    root = _related_root()
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plans["p"], root=root))
    assert by_columns == by_rows
    assert taken == [True]


def _composite_contract(parts: int) -> Dict[str, Any]:
    names = [f"k{index}" for index in range(parts)]
    return {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("p", [_column(name, "INT") for name in names], {}),
            _expose(
                "c",
                [_column("c_" + name, "INT") for name in names],
                {"relationships": [_rel("r", "p", ["c_" + name for name in names], names)]},
            ),
        ],
    }


@pytest.mark.parametrize(
    ("parts", "top"),
    [(2, 1 << 32), (3, 1 << 43)],
    ids=["a key space of 2**64, past int64", "a key space past 2**128"],
)
def test_a_composite_key_too_wide_for_the_arrays_is_read_per_row(
    monkeypatch: pytest.MonkeyPatch, parts: int, top: int
) -> None:
    declared = {f"k{index}": {"range": {"min": "1", "max": str(top)}} for index in range(parts)}
    plans = related_plans(
        _composite_contract(parts),
        {"p": (declared, 60), "c": ({}, 90)},
        generator_version=GENERATOR_VERSION_2,
    )
    root = _related_root()
    for expose_id in ("p", "c"):
        by_columns, by_rows, taken = _both(
            monkeypatch, partial(generate_rows, plans[expose_id], root=root)
        )
        assert by_columns == by_rows, expose_id
        assert taken == [True], expose_id


def test_an_identifier_key_too_wide_for_an_array_permutation_is_read_per_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forty hex digits are 2**160 values, past what permute2_array covers."""
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("u", [_column("u_id", "STRING")], _primary("u", "u_id")),
            _expose(
                "e",
                [_column("e_user", "STRING")],
                {"relationships": [_rel("user", "u", ["e_user"], ["u_id"])]},
            ),
        ],
    }
    plans = related_plans(
        contract,
        {"u": ({"u_id": {"identifierLength": 40}}, 1000), "e": ({}, 300)},
        generator_version=GENERATOR_VERSION_2,
    )
    root = _related_root()
    for expose_id in ("u", "e"):
        by_columns, by_rows, taken = _both(
            monkeypatch, partial(generate_rows, plans[expose_id], root=root)
        )
        assert by_columns == by_rows, expose_id
        assert taken == [True], expose_id


def test_a_parent_row_array_holds_rows_below_2_63_only() -> None:
    assert relations._row_array({1 << 63}) is None
    held = relations._row_array({0, (1 << 63) - 1})
    assert held is not None and sorted(held.tolist()) == [0, (1 << 63) - 1]


def test_parent_rows_past_an_int64_array_are_read_per_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A uniform pick over 2**64 parent rows names rows an int64 array does not hold."""
    contract = {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("u", [_column("u_id", "STRING")], _primary("u", "u_id")),
            _expose(
                "e",
                [_column("e_user", "STRING")],
                {"relationships": [_rel("user", "u", ["e_user"], ["u_id"])]},
            ),
        ],
    }
    plans = related_plans(
        contract,
        {"u": ({}, 1 << 64), "e": ({}, 40)},
        generator_version=GENERATOR_VERSION_2,
    )
    root = _related_root()
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plans["e"], root=root))
    assert by_columns == by_rows
    assert taken == [True]


@pytest.mark.parametrize(
    ("declared_type", "mean", "low", "high"),
    [
        ("SMALLINT", "40000", -32768, 32767),
        ("TINYINT", "300", -128, 127),
        ("SMALLINT", "32760", -32768, 32767),
        ("TINYINT", "126", -128, 127),
    ],
)
def test_an_integer_rate_column_stays_inside_the_contract_width_as_the_per_row_path_does(
    monkeypatch: pytest.MonkeyPatch, declared_type: str, mean: str, low: int, high: int
) -> None:
    """A count at and beyond the width is clamped to it on both paths."""
    plan = _plan(
        rows=120,
        schema=[{"name": "c", "type": declared_type}],
        declared={"c": {"family": "rate", "mean": mean}},
    )
    root = _root()
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert taken == [True]
    assert by_columns == by_rows
    values = [row[0] for row in generate_rows(plan, root=root)]
    assert all(low <= value <= high for value in values)
