# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Generating columns from a contract's rules (EXPR-SPEC-2 §6, ADR 0011 step 1).

What is pinned:

* **The model.** :func:`model_from_contract` reads every expose's columns, enum
  levels, links and key.
* **What generates.** A small calendar and a clock are made from ``derive`` rules
  and one difference-form ``check``, and every rule is evaluated on every row.
* **Covered rows.** ``when`` selects the rows a rule sets; the first covering
  ``derive`` wins and an uncovered row keeps the column's own value.
* **Empty intervals.** A NULL input to a difference form keeps the column's own value.
* **Refusals.** Every rule that would determine a column and is not generated yet is
  ``FLX-6210``, and the settings and sources checks are ``FLX-3105``, ``FLX-6202``,
  ``FLX-6203``, ``FLX-6204`` and ``FLX-6208``, all before a row is drawn.
* **Determinism.** The same rows for any slicing, in a worker process, and from a
  pickled plan.
"""

from __future__ import annotations

import pickle
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SeedTreeRoot, derive_root, digest_bytes
from flux_engine.expr2 import PREDICATE, VALUE, Rows, Side, check, parse
from flux_engine.expr2.errors import Expr2Error
from flux_engine.expr2.evaluate import decoder
from flux_engine.generator.coverage import coverage_of_plan
from flux_engine.generator.relations import relate_plans, related_columns
from flux_engine.generator.rows import (
    GeneratorPlan,
    build_plan,
    generate_column,
    generate_row_slice,
    generate_rows,
)
from flux_engine.generator.rules import (
    RulePlan,
    model_from_contract,
    plan_rules,
)
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.coverage import Origin
from flux_engine.seam.keys import compile_keys, mark_key_columns

pytestmark = pytest.mark.unit

SIM_ID = "sim-rules"
SEAM = "sha256:" + "5" * 64

DAYS = ("Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri")
ENUM = "enum(" + ", ".join(f"'{day}'" for day in DAYS) + ")"
_DOW = "DATE '2000-01-01'"
_DAYS_SINCE = f"DAYS_BETWEEN({_DOW}, d_date)"

# 2000-01-01 was a Saturday. n - 7 * ROUND(n / 7, 0) is n mod 7 in -3..3, and a
# remainder of -1 is a Friday, so each remainder names the day that many past a Saturday.
_REMAINDER = f"{_DAYS_SINCE} - 7 * ROUND({_DAYS_SINCE} / 7, 0)"
_DAY_NAME = (
    "CASE WHEN d_rem = 0 THEN 'Sat' WHEN d_rem = 1 THEN 'Sun' WHEN d_rem = 2 THEN 'Mon' "
    "WHEN d_rem = 3 THEN 'Tue' WHEN d_rem = -3 THEN 'Wed' WHEN d_rem = -2 THEN 'Thu' "
    "ELSE 'Fri' END"
)


def _column(name: str, type_: str, required: bool = False) -> Dict[str, Any]:
    return {"name": name, "type": type_, "required": required}


def _expose(
    expose_id: str,
    schema: List[Dict[str, Any]],
    invariants: Optional[List[Dict[str, Any]]] = None,
    semantics: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    contract: Dict[str, Any] = {"schema": schema}
    if invariants is not None:
        contract["invariants"] = invariants
    expose: Dict[str, Any] = {
        "exposeId": expose_id,
        "kind": "table",
        "contract": contract,
        "binding": {"platform": "local", "format": "parquet", "location": {"path": expose_id}},
    }
    if semantics is not None:
        expose["semantics"] = dict(semantics)
    return expose


def _contract(*exposes: Dict[str, Any], version: str = "0.7.7") -> Dict[str, Any]:
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "test.rules",
        "exposes": [*exposes],
    }


def calendar_invariants() -> List[Dict[str, Any]]:
    return [
        {
            "id": "date_of_key",
            "check": f"d_sk - 1 = DAYS_BETWEEN({_DOW}, d_date)",
        },
        {"id": "year_of_date", "derive": "d_year", "expr": f"YEARS_BETWEEN({_DOW}, d_date) + 2000"},
        {
            "id": "month_of_date",
            "derive": "d_month",
            "expr": f"MONTHS_BETWEEN({_DOW}, d_date) - 12 * YEARS_BETWEEN({_DOW}, d_date) + 1",
        },
        {"id": "weekend", "derive": "d_weekend", "expr": "d_day IN ('Sat', 'Sun')"},
        {"id": "day_name", "derive": "d_day", "expr": _DAY_NAME},
        {"id": "remainder", "derive": "d_rem", "expr": _REMAINDER},
    ]


def calendar_expose(invariants: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    # Declared so that every rule reads a column declared after it: the order the
    # columns are set in is the rules', not the declaration's.
    return _expose(
        "calendar",
        [
            _column("d_sk", "INT", True),
            _column("d_weekend", "BOOL"),
            _column("d_day", ENUM),
            _column("d_rem", "INT"),
            _column("d_year", "INT"),
            _column("d_month", "INT"),
            _column("d_date", "DATE"),
        ],
        calendar_invariants() if invariants is None else invariants,
    )


SETTINGS = {"date_of_key": {"generates": "d_date"}}
# d_date declares its own range too: a row whose interval is empty keeps its own value.
CALENDAR_STATISTICS = {
    "d_sk": {"range": {"min": "1", "max": "5000"}},
    "d_date": {"range": {"min": "2000-01-01", "max": "2013-09-08"}},
}


def clock_expose() -> Dict[str, Any]:
    return _expose(
        "clock",
        [
            _column("t_sk", "INT", True),
            _column("t_hour", "INT"),
            _column("t_minute", "INT"),
            _column("t_second", "INT"),
            _column("t_am_pm", "STRING"),
        ],
        [
            {"id": "hour", "derive": "t_hour", "expr": "ROUND((t_sk * 10 - 17995) / 36000, 0)"},
            {
                "id": "minute",
                "derive": "t_minute",
                "expr": "ROUND(((t_sk - 3600 * t_hour) * 10 - 295) / 600, 0)",
            },
            {
                "id": "second",
                "derive": "t_second",
                "expr": "t_sk - 3600 * t_hour - 60 * t_minute",
            },
            {
                "id": "am_pm",
                "derive": "t_am_pm",
                "expr": "CASE WHEN t_hour < 12 THEN 'AM' ELSE 'PM' END",
            },
        ],
    )


def plan_of(
    contract: Mapping[str, Any],
    expose_id: str,
    statistics: Mapping[str, Any],
    *,
    rows: int = 200,
    settings: Any = None,
    time_bounds: Optional[Mapping[str, str]] = None,
    strict: bool = False,
    has_fidelity: bool = True,
    mapping: Optional[Mapping[str, Any]] = None,
    fidelity_extra: Optional[Mapping[str, Any]] = None,
    flux_rules: Any = None,
) -> GeneratorPlan:
    keys = compile_keys(contract)
    expose_keys = None if keys is None else keys.expose(expose_id)
    related = related_columns(expose_keys)
    (entry,) = [e for e in contract["exposes"] if e["exposeId"] == expose_id]
    rules = plan_rules(
        contract_document=contract,
        expose_id=expose_id,
        settings=settings,
        related=related,
        mapping=mapping,
        has_fidelity=has_fidelity,
        strict=strict,
        flux_rules=flux_rules,
    )
    fidelity = {"columns": dict(statistics), **dict(fidelity_extra or {})}
    return build_plan(
        mark_key_columns(compile_expose(entry), expose_keys),
        sim_id=SIM_ID,
        seam_digest=SEAM,
        rows=rows,
        fidelity=fidelity,
        time_bounds=time_bounds,
        related=related,
        rules=rules,
    )


def root_for(seed: int = 7) -> SeedTreeRoot:
    return derive_root(digest_bytes(SEAM), seed)


def calendar_plan(**kwargs: Any) -> GeneratorPlan:
    kwargs.setdefault("settings", SETTINGS)
    return plan_of(_contract(calendar_expose()), "calendar", CALENDAR_STATISTICS, **kwargs)


def table(plan: GeneratorPlan, rows: Sequence[Tuple[Any, ...]]) -> List[Dict[str, Any]]:
    names = [column_plan.column.name for column_plan in plan.columns]
    return [dict(zip(names, row, strict=True)) for row in rows]


def assert_every_rule_holds(
    contract: Mapping[str, Any], expose_id: str, data: Sequence[Mapping[str, Any]]
) -> int:
    """Evaluate every rule of *expose_id* on every row; return how many evaluations held."""
    model = model_from_contract(contract)
    scope = model.exposes[expose_id]
    memory = Rows(model, {expose_id: list(data)})
    (entry,) = [e for e in contract["exposes"] if e["exposeId"] == expose_id]
    held = 0
    for invariant in entry["contract"]["invariants"]:
        if "derive" in invariant:
            column = invariant["derive"]
            checked = check(
                parse(invariant["expr"], side=Side.CORE),
                model,
                expose_id,
                position=VALUE,
                target=column,
            )
            read = decoder(scope.columns[column])
            for row in memory.rows(expose_id):
                assert checked.evaluate(row) == read(row.get(column)), (invariant["id"], row.index)
                held += 1
        elif invariant["id"] in SETTINGS:
            checked = check(
                parse(invariant["check"], side=Side.CORE), model, expose_id, position=PREDICATE
            )
            for row in memory.rows(expose_id):
                assert checked.evaluate(row) is True, (invariant["id"], row.index)
                held += 1
    return held


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def test_the_model_reads_every_expose_of_the_contract() -> None:
    contract = _contract(
        _expose(
            "dim",
            [_column("k", "INT", True), _column("kind", ENUM), _column("name", "STRING")],
            semantics={"entities": [{"name": "dim", "type": "primary", "expr": "k"}]},
        ),
        _expose(
            "fact",
            [_column("id", "INT", True), _column("dim_k", "INT"), _column("x", "DECIMAL(10,2)")],
            semantics={
                "relationships": [
                    {
                        "name": "dim",
                        "to": "dim",
                        "fromColumns": ["dim_k"],
                        "toColumns": ["k"],
                        "cardinality": "many_to_one",
                    }
                ]
            },
        ),
    )
    model = model_from_contract(contract)
    assert list(model.exposes) == ["dim", "fact"]
    dim, fact = model.exposes["dim"], model.exposes["fact"]
    assert list(dim.columns) == ["k", "kind", "name"]
    assert dim.key == ("k",)
    assert dim.columns["kind"].enum == DAYS
    assert dim.columns["name"].enum is None
    assert fact.columns["x"].scale == 2 and fact.columns["x"].precision == 10
    assert fact.key == ()
    (link,) = fact.links.values()
    assert (link.name, link.target, link.from_columns, link.to_columns) == (
        "dim",
        "dim",
        ("dim_k",),
        ("k",),
    )
    assert model.children_links("dim", "fact") == [link]
    assert all(expose.versions is None for expose in model.exposes.values())


def test_an_older_contract_has_no_links_and_no_key() -> None:
    contract = _contract(
        _expose("dim", [_column("k", "INT")], semantics={"entities": []}), version="0.7.5"
    )
    (dim,) = model_from_contract(contract).exposes.values()
    assert dim.links == {} and dim.key == ()


# ---------------------------------------------------------------------------
# What generates
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def calendar() -> Tuple[GeneratorPlan, List[Dict[str, Any]]]:
    plan = calendar_plan(rows=400)
    return plan, table(plan, generate_rows(plan, root=root_for()))


def test_a_calendar_is_made_from_rules(
    calendar: Tuple[GeneratorPlan, List[Dict[str, Any]]],
) -> None:
    plan, data = calendar
    assert plan.rules is not None
    assert plan.rules.order == ("d_date", "d_rem", "d_day", "d_weekend", "d_year", "d_month")
    contract = _contract(calendar_expose())
    assert assert_every_rule_holds(contract, "calendar", data) == 400 * 6
    for row in data:
        day = date(2000, 1, 1) + timedelta(days=row["d_sk"] - 1)
        assert row["d_date"] == day
        assert (row["d_year"], row["d_month"]) == (day.year, day.month)
        assert row["d_day"] == DAYS[(day.weekday() - 5) % 7]
        assert row["d_weekend"] is (day.weekday() >= 5)
        assert type(row["d_year"]) is int and type(row["d_weekend"]) is bool


def test_a_clock_is_made_from_rules() -> None:
    contract = _contract(clock_expose())
    plan = plan_of(contract, "clock", {"t_sk": {"range": {"min": "0", "max": "86399"}}}, rows=500)
    assert plan.rules is not None
    assert plan.rules.order == ("t_hour", "t_minute", "t_second", "t_am_pm")
    data = table(plan, generate_rows(plan, root=root_for()))
    assert assert_every_rule_holds(contract, "clock", data) == 500 * 4
    for row in data:
        n = row["t_sk"]
        assert (row["t_hour"], row["t_minute"], row["t_second"]) == (
            n // 3600,
            n % 3600 // 60,
            n % 60,
        )
        assert row["t_am_pm"] == ("AM" if n < 43200 else "PM")


def test_a_decimal_derive_is_written_at_its_scale() -> None:
    contract = _contract(
        _expose(
            "invoice",
            [_column("net", "DECIMAL(12,2)"), _column("gross", "DECIMAL(12,2)")],
            [{"id": "gross", "derive": "gross", "expr": "ROUND(net * 1.2, 2)"}],
        )
    )
    plan = plan_of(contract, "invoice", {"net": {"range": {"min": "1", "max": "100"}}}, rows=50)
    for row in table(plan, generate_rows(plan, root=root_for())):
        expected = (row["net"] * Decimal("1.2")).quantize(Decimal("0.01"), ROUND_HALF_UP)
        assert row["gross"] == expected
        assert row["gross"].as_tuple().exponent == -2


def test_a_column_a_whenless_derive_sets_needs_no_draw_of_its_own() -> None:
    contract = _contract(
        _expose(
            "t",
            [_column("a", "INT"), _column("b", "INT")],
            [{"id": "b_of_a", "derive": "b", "expr": "a + 1"}],
        )
    )
    plan = plan_of(contract, "t", {"a": {"range": {"min": "0", "max": "10"}}}, rows=100)
    data = table(plan, generate_rows(plan, root=root_for()))
    assert all(row["b"] == row["a"] + 1 for row in data)


def test_a_column_its_rules_leave_uncovered_keeps_its_own_source() -> None:
    # Rows a `when` leaves out take the column's own source (EXPR-SPEC-2 section 9), so a
    # column with none is refused as it would be with no rule, never written NULL.
    invariant = {"id": "b_of_a", "derive": "b", "expr": "a + 1", "when": "a > 5"}
    contract = _contract(_expose("t", [_column("a", "INT"), _column("b", "INT")], [invariant]))
    with pytest.raises(FluxError, match="FLX-3402"):
        plan_of(contract, "t", {"a": {"range": {"min": "0", "max": "10"}}}, rows=100)
    declared = {
        "a": {"range": {"min": "0", "max": "10"}},
        "b": {"range": {"min": "50", "max": "60"}},
    }
    plan = plan_of(contract, "t", declared, rows=100)
    data = table(plan, generate_rows(plan, root=root_for()))
    assert any(row["a"] <= 5 for row in data)
    for row in data:
        if row["a"] > 5:
            assert row["b"] == row["a"] + 1
        else:
            assert 50 <= row["b"] <= 60


# ---------------------------------------------------------------------------
# Covered rows
# ---------------------------------------------------------------------------


def _banded_contract() -> Dict[str, Any]:
    return _contract(
        _expose(
            "t",
            [_column("n", "INT"), _column("band", "STRING")],
            [
                {"id": "low", "derive": "band", "expr": "'low'", "when": "n < 10"},
                {"id": "mid", "derive": "band", "expr": "'mid'", "when": "n < 20"},
            ],
        )
    )


def test_the_first_derive_whose_when_holds_covers_the_row() -> None:
    plan = plan_of(
        _banded_contract(),
        "t",
        {
            "n": {"range": {"min": "0", "max": "29"}},
            "band": {"support": {"values": ["x", "y"]}},
        },
        rows=300,
    )
    data = table(plan, generate_rows(plan, root=root_for()))
    seen = set()
    for row in data:
        n = row["n"]
        if n < 10:
            assert row["band"] == "low"
        elif n < 20:
            assert row["band"] == "mid"
        else:
            # No rule covers it: it keeps the value its own statistics gave it.
            assert row["band"] in ("x", "y")
        seen.add("low" if n < 10 else "mid" if n < 20 else "own")
    assert seen == {"low", "mid", "own"}
    unruled = plan_of(
        _contract(_expose("t", [_column("n", "INT"), _column("band", "STRING")])),
        "t",
        {"n": {"range": {"min": "0", "max": "29"}}, "band": {"support": {"values": ["x", "y"]}}},
        rows=300,
    )
    own = table(unruled, generate_rows(unruled, root=root_for()))
    assert [r["band"] for r, plain in zip(data, own, strict=True) if r["n"] >= 20] == [
        r["band"] for r in own if r["n"] >= 20
    ]


def test_coverage_names_the_rules_that_determine_a_column() -> None:
    plan = plan_of(
        _banded_contract(),
        "t",
        {"n": {"range": {"min": "0", "max": "29"}}, "band": {"support": {"values": ["x", "y"]}}},
    )
    coverage = {row.column: row for row in coverage_of_plan(plan).columns}
    assert (coverage["band"].origin, coverage["band"].decided_by) == (Origin.DERIVED, "rule")
    assert coverage["band"].source == "contract.invariants.low,mid"
    assert coverage["n"].decided_by == "fidelity"
    assert "contract invariant" in coverage["band"].detail
    plan = calendar_plan()
    by_column = {row.column: row for row in coverage_of_plan(plan).columns}
    assert by_column["d_date"].source == "contract.invariants.date_of_key"
    assert {c for c, row in by_column.items() if row.decided_by == "rule"} == {
        "d_date",
        "d_rem",
        "d_year",
        "d_month",
        "d_day",
        "d_weekend",
    }


# ---------------------------------------------------------------------------
# Empty intervals
# ---------------------------------------------------------------------------


def _offset_contract() -> Dict[str, Any]:
    return _contract(
        _expose(
            "t",
            [_column("off", "INT"), _column("d", "DATE")],
            [{"id": "shift", "check": "off = DAYS_BETWEEN(DATE '2026-01-01', d)"}],
        )
    )


def test_a_null_input_keeps_the_columns_own_value() -> None:
    contract = _offset_contract()
    statistics = {
        "off": {"range": {"min": "0", "max": "30"}, "nullFraction": "0.4"},
        "d": {"range": {"min": "2026-01-01", "max": "2026-12-31"}},
    }
    settings = {"shift": {"generates": "d"}}
    plan = plan_of(contract, "t", statistics, rows=300, settings=settings)
    plain = plan_of(
        _contract(_expose("t", contract["exposes"][0]["contract"]["schema"])),
        "t",
        statistics,
        rows=300,
    )
    data = table(plan, generate_rows(plan, root=root_for()))
    own = table(plain, generate_rows(plain, root=root_for()))
    nulls = 0
    for row, theirs in zip(data, own, strict=True):
        if row["off"] is None:
            nulls += 1
            assert row["d"] == theirs["d"]
        else:
            assert row["d"] == date(2026, 1, 1) + timedelta(days=row["off"])
    assert 0 < nulls < 300


def test_a_date_with_no_weight_keeps_the_columns_own_value() -> None:
    contract = _offset_contract()
    settings = {"shift": {"generates": "d"}}
    ranged = {
        "off": {"range": {"min": "0", "max": "100"}},
        "d": {"range": {"min": "2026-01-01", "max": "2026-02-09"}},
    }
    plan = plan_of(contract, "t", ranged, rows=300, settings=settings)
    for row in table(plan, generate_rows(plan, root=root_for())):
        assert date(2026, 1, 1) <= row["d"] <= date(2026, 2, 9)
        if row["off"] <= 39:
            assert row["d"] == date(2026, 1, 1) + timedelta(days=row["off"])
    # A bin with no weight gives it none either.
    binned = {
        "off": {"range": {"min": "0", "max": "29"}},
        "d": {
            "range": {"min": "2026-01-01", "max": "2026-01-31"},
            "bins": ["2026-01-01", "2026-01-11", "2026-01-21", "2026-01-31"],
            "binWeights": ["1", "0", "1"],
        },
    }
    plan = plan_of(contract, "t", binned, rows=300, settings=settings)
    moved = 0
    for row in table(plan, generate_rows(plan, root=root_for())):
        if date(2026, 1, 1) + timedelta(days=row["off"]) == row["d"]:
            moved += 1
        else:
            assert 10 <= row["off"] < 20
    assert moved > 0


def test_the_difference_reads_either_way_round() -> None:
    contract = _contract(
        _expose(
            "t",
            [_column("off", "INT"), _column("d", "DATE")],
            [{"id": "back", "check": "DAYS_BETWEEN(d, DATE '2026-01-31') = off"}],
        )
    )
    plan = plan_of(
        contract,
        "t",
        {
            "off": {"range": {"min": "0", "max": "9"}},
            "d": {"range": {"min": "2026-01-22", "max": "2026-01-31"}},
        },
        rows=60,
        settings={"back": {"generates": "d"}},
    )
    for row in table(plan, generate_rows(plan, root=root_for())):
        assert row["d"] == date(2026, 1, 31) - timedelta(days=row["off"])


# ---------------------------------------------------------------------------
# Rows that do not fit, and the order of columns
# ---------------------------------------------------------------------------


def test_a_value_that_does_not_fit_its_column_is_refused_when_the_row_is_made() -> None:
    contract = _contract(
        _expose(
            "t",
            [_column("n", "INT"), _column("big", "DECIMAL(5,0)")],
            [{"id": "big", "derive": "big", "expr": "n * 1000"}],
        )
    )
    plan = plan_of(contract, "t", {"n": {"range": {"min": "0", "max": "500"}}}, rows=50)
    with pytest.raises(Expr2Error) as caught:
        generate_rows(plan, root=root_for())
    assert caught.value.code.code == "FLX-6112"
    assert "row " in str(caught.value) and "big" in str(caught.value)


def test_a_cycle_is_refused_and_names_its_rules() -> None:
    contract = _contract(
        _expose(
            "t",
            [_column("a", "INT"), _column("b", "INT"), _column("c", "INT")],
            [
                {"id": "a_of_b", "derive": "a", "expr": "b + 1"},
                {"id": "b_of_a", "derive": "b", "expr": "a + 1"},
                {"id": "c_of_a", "derive": "c", "expr": "a + 1"},
            ],
        )
    )
    with pytest.raises(Expr2Error) as caught:
        plan_of(contract, "t", {})
    assert caught.value.code.code == "FLX-6208"
    assert "a_of_b" in str(caught.value) and "b_of_a" in str(caught.value)
    own = _contract(
        _expose("t", [_column("a", "INT")], [{"id": "a_of_a", "derive": "a", "expr": "a + 1"}])
    )
    with pytest.raises(Expr2Error) as caught:
        plan_of(own, "t", {})
    assert caught.value.code.code == "FLX-6208"


def test_ties_between_ready_columns_follow_the_declared_column_order() -> None:
    contract = _contract(
        _expose(
            "t",
            [_column("z", "INT"), _column("y", "INT"), _column("x", "INT"), _column("s", "INT")],
            [
                {"id": "x_of_s", "derive": "x", "expr": "s + 1"},
                {"id": "y_of_s", "derive": "y", "expr": "s + 2"},
                {"id": "z_of_y", "derive": "z", "expr": "y + 1"},
            ],
        )
    )
    plan = plan_of(contract, "t", {"s": {"range": {"min": "0", "max": "9"}}})
    assert plan.rules is not None and plan.rules.order == ("y", "z", "x")
    for row in table(plan, generate_rows(plan, root=root_for())):
        assert (row["x"], row["y"], row["z"]) == (row["s"] + 1, row["s"] + 2, row["s"] + 3)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_slices_columns_and_a_second_pass_agree(
    calendar: Tuple[GeneratorPlan, List[Dict[str, Any]]],
) -> None:
    plan, data = calendar
    whole = generate_rows(plan, root=root_for())
    assert whole == generate_rows(plan, root=root_for())
    parts = (
        generate_row_slice(plan, 0, 137, root=root_for())
        + generate_row_slice(plan, 137, 301, root=root_for())
        + generate_row_slice(plan, 301, 400, root=root_for())
    )
    assert parts == whole
    names = [column_plan.column.name for column_plan in plan.columns]
    for column in ("d_date", "d_weekend", "d_sk", "d_year"):
        assert generate_column(plan, column, root=root_for()) == tuple(
            row[names.index(column)] for row in whole
        )


def test_a_plan_with_rules_pickles_and_makes_the_same_rows() -> None:
    plan = calendar_plan()
    rows = generate_rows(plan, root=root_for())  # compiles the rules
    assert plan.rules is not None and plan.rules._cache
    copy = pickle.loads(pickle.dumps(plan))
    assert isinstance(copy.rules, RulePlan) and copy.rules._cache == {}
    assert copy.as_document() == plan.as_document()
    assert generate_rows(copy, root=root_for()) == rows


@pytest.mark.slow
@pytest.mark.integration
def test_worker_processes_make_the_rows_one_process_makes() -> None:
    from flux_engine.seam.emit import render_row
    from flux_engine.stages import _parallel as parallel

    if parallel.cpu_jobs() < 2:
        pytest.skip("one CPU: every emit stays in one process")
    rows = 3 * parallel.MIN_ROWS_PER_JOB + 11
    plan = calendar_plan(rows=rows)
    expose = plan.expose
    one = generate_rows(plan, root=root_for())
    many, lines = parallel.generate_framed(plan, root=root_for(), expose=expose, workers=2)
    assert many == one
    assert len(lines) == len(one)
    render_row(one[0], expose, row_index=0)


# ---------------------------------------------------------------------------
# The plan document
# ---------------------------------------------------------------------------


def test_a_plan_without_rules_keeps_its_document_and_digest() -> None:
    schema = [_column("a", "INT")]
    statistics = {"a": {"range": {"min": "0", "max": "9"}}}
    plan = plan_of(_contract(_expose("t", schema)), "t", statistics)
    assert plan.rules is None and "rules" not in plan.as_document()
    empty = plan_of(_contract(_expose("t", schema, [])), "t", statistics)
    assert empty.rules is None and empty.as_document() == plan.as_document()
    assert empty.digest() == plan.digest()


def test_the_plan_records_every_rule() -> None:
    plan = calendar_plan()
    document = plan.as_document()["rules"]
    assert document["contractId"] == "test.rules"
    rules = {rule["id"]: rule for rule in document["rules"]}
    assert list(rules) == [i["id"] for i in calendar_invariants()]
    assert rules["date_of_key"]["form"] == "check-difference-days"
    assert rules["date_of_key"]["column"] == "d_date" and rules["date_of_key"]["order"] == 0
    assert rules["weekend"]["order"] == 3 and rules["weekend"]["form"] == "derive"
    assert rules["year_of_date"]["text"] == f"YEARS_BETWEEN({_DOW}, d_date) + 2000"
    assert plan.digest().value != calendar_plan(rows=201).digest().value


def test_a_check_with_no_setting_generates_nothing_and_is_recorded() -> None:
    contract = _contract(
        _expose(
            "t",
            [_column("a", "INT"), _column("b", "INT")],
            [
                {"id": "ordered", "check": "a <= b", "when": "a IS NOT NULL"},
                {"id": "within", "check": "a >= 0 AND a <= 9"},
                {"id": "either", "check": "a = 1 OR b = 1"},
            ],
        )
    )
    plan = plan_of(
        contract,
        "t",
        {"a": {"range": {"min": "0", "max": "9"}}, "b": {"range": {"min": "0", "max": "9"}}},
    )
    assert plan.rules is not None and plan.rules.order == ()
    forms = {rule["id"]: rule["form"] for rule in plan.as_document()["rules"]["rules"]}
    assert forms == {"ordered": "check-bounds", "within": "check-bounds", "either": "check-other"}
    before = generate_rows(plan, root=root_for())
    bare = plan_of(
        _contract(_expose("t", contract["exposes"][0]["contract"]["schema"])),
        "t",
        {"a": {"range": {"min": "0", "max": "9"}}, "b": {"range": {"min": "0", "max": "9"}}},
    )
    assert before == generate_rows(bare, root=root_for())


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


#: Statistics for the columns `one` declares, so that a column no rule determines can draw.
BASE_STATISTICS: Dict[str, Any] = {
    "a": {"range": {"min": "0", "max": "9"}},
    "b": {"range": {"min": "0", "max": "9"}},
    "d": {"range": {"min": "2026-01-01", "max": "2026-12-31"}},
    "e": {"range": {"min": "2026-01-01", "max": "2026-12-31"}},
    "s": {"support": {"values": ["x", "y"]}},
}


def refused(code: str, contract: Mapping[str, Any], expose_id: str = "t", **kwargs: Any) -> str:
    statistics = kwargs.pop("statistics", BASE_STATISTICS)
    with pytest.raises((Expr2Error, FluxError)) as caught:
        plan_of(contract, expose_id, statistics, **kwargs)
    assert caught.value.code.code == code, str(caught.value)
    return str(caught.value)


def one(
    *invariants: Dict[str, Any],
    columns: Optional[List[Dict[str, Any]]] = None,
    semantics: Any = None,
) -> Dict[str, Any]:
    schema = columns or [
        _column("a", "INT"),
        _column("b", "INT"),
        _column("d", "DATE"),
        _column("e", "DATE"),
        _column("s", "STRING"),
    ]
    return _contract(_expose("t", schema, list(invariants), semantics))


@pytest.mark.parametrize(
    "invariant",
    [
        pytest.param(
            {
                "id": "r",
                "derive": "a",
                "aggregate": {"function": "count", "from": "t", "via": "x"},
            },
            id="an-aggregate",
        ),
        pytest.param(
            {"id": "r", "dependency": {"from": ["a"], "to": ["b"], "bothWays": True}},
            id="a-both-ways-dependency",
        ),
        pytest.param(
            {
                "id": "r",
                "dependency": {"from": ["a"], "to": ["b"]},
                "mustBeGreaterThan": 5,
                "unit": "percent",
            },
            id="a-dependency-whose-band-needs-violations",
        ),
    ],
)
def test_an_aggregate_and_a_dependency_that_cannot_generate_are_refused(
    invariant: Dict[str, Any],
) -> None:
    refused("FLX-6210", one(invariant))


def test_a_rule_that_reads_another_row_is_refused() -> None:
    contract = _contract(
        _expose(
            "parent",
            [_column("pk", "INT", True), _column("v", "INT")],
            semantics={"entities": [{"name": "p", "type": "primary", "expr": "pk"}]},
        ),
        _expose(
            "t",
            [_column("fk", "INT"), _column("copy", "INT")],
            [{"id": "copy_v", "derive": "copy", "expr": "p.v"}],
            semantics={
                "relationships": [
                    {
                        "name": "p",
                        "to": "parent",
                        "fromColumns": ["fk"],
                        "toColumns": ["pk"],
                        "cardinality": "many_to_one",
                    }
                ]
            },
        ),
    )
    assert "another row" in refused("FLX-6210", contract)
    over_when = _contract(
        contract["exposes"][0],
        _expose(
            "t",
            [_column("fk", "INT"), _column("copy", "INT")],
            [{"id": "copy_v", "derive": "copy", "expr": "1", "when": "p.v > 0"}],
            semantics=contract["exposes"][1]["semantics"],
        ),
    )
    refused("FLX-6210", over_when)


@pytest.mark.parametrize("unit", ["months", "years"])
def test_a_month_or_year_difference_that_generates_is_refused(unit: str) -> None:
    contract = one(
        {"id": "r", "check": f"a = {unit.upper()}_BETWEEN(e, d)"},
        columns=[_column("a", "INT"), _column("d", "DATE"), _column("e", "DATE")],
    )
    statistics = {name: BASE_STATISTICS[name] for name in ("a", "d", "e")}
    assert unit[:-1] in refused(
        "FLX-6210", contract, statistics=statistics, settings={"r": {"generates": "d"}}
    )
    # Without the setting it only checks.
    plan_of(contract, "t", {name: BASE_STATISTICS[name] for name in ("a", "d", "e")})


def test_a_bounds_form_that_generates_is_refused() -> None:
    contract = one({"id": "r", "check": "d >= e AND d <= DATE '2030-01-01'"})
    assert "bounds" in refused("FLX-6210", contract, settings={"r": {"generates": "d"}})


def test_a_percent_band_of_positive_width_is_refused() -> None:
    derive = {"id": "r", "derive": "b", "expr": "a + 1"}
    for band in (
        {"mustBeBetween": [1, 2], "unit": "percent"},
        {"mustBeLessOrEqualTo": 0.5, "unit": "percent"},
        {"mustBeGreaterThan": 90, "unit": "percent"},
    ):
        refused("FLX-6210", one({**derive, **band}))
    # A zero-width percent band, any rows band and no band all generate no violation.
    for band in (
        {"mustBe": 2, "unit": "percent"},
        {"mustBeLessOrEqualTo": 0, "unit": "percent"},
        {"mustBeLessOrEqualTo": 10},
        {"mustBeBetween": [1, 9], "unit": "rows"},
        {},
    ):
        plan = plan_of(one({**derive, **band}), "t", BASE_STATISTICS)
        assert plan.rules is not None and plan.rules.order == ("b",)


def test_a_strict_run_refuses_a_band_that_zero_violations_fail() -> None:
    derive = {"id": "r", "derive": "b", "expr": "a + 1"}
    statistics = BASE_STATISTICS
    failing = (
        {"mustBe": 2, "unit": "percent"},
        {"mustBeGreaterThan": 5},
        {"mustBeBetween": [3, 9]},
        {"mustBeLessThan": 0},
    )
    for band in failing:
        plan_of(one({**derive, **band}), "t", statistics)
        refused("FLX-6204", one({**derive, **band}), statistics=statistics, strict=True)
    meeting = ({"mustBe": 0}, {"mustBeLessThan": 3}, {"mustBeBetween": [0, 4]}, {})
    for band in meeting:
        plan_of(one({**derive, **band}), "t", statistics, strict=True)


def test_a_key_a_foreign_key_and_a_targeted_column_are_not_determined() -> None:
    keyed = {
        "entities": [{"name": "k", "type": "primary", "expr": "k"}],
    }
    contract = _contract(
        _expose(
            "t",
            [_column("k", "INT", True), _column("a", "INT")],
            [{"id": "key_from_a", "derive": "k", "expr": "a + 1"}],
            keyed,
        )
    )
    assert "key" in refused("FLX-6210", contract)
    fk = _contract(
        _expose(
            "parent",
            [_column("pk", "INT", True)],
            semantics={"entities": [{"name": "p", "type": "primary", "expr": "pk"}]},
        ),
        _expose(
            "t",
            [_column("fk", "INT"), _column("a", "INT")],
            [{"id": "fk_from_a", "derive": "fk", "expr": "a"}],
            {
                "relationships": [
                    {
                        "name": "p",
                        "to": "parent",
                        "fromColumns": ["fk"],
                        "toColumns": ["pk"],
                        "cardinality": "many_to_one",
                    }
                ]
            },
        ),
    )
    refused("FLX-6210", fk)
    targeted = _contract(
        _expose(
            "parent",
            [_column("pk", "INT"), _column("a", "INT")],
            [{"id": "pk_from_a", "derive": "pk", "expr": "a"}],
        ),
        _expose(
            "t",
            [_column("fk", "INT")],
            semantics={
                "relationships": [
                    {
                        "name": "p",
                        "to": "parent",
                        "fromColumns": ["fk"],
                        "toColumns": ["pk"],
                        "cardinality": "many_to_one",
                    }
                ]
            },
        ),
    )
    assert "toColumns" in refused("FLX-6210", targeted, expose_id="parent")


def test_a_column_another_is_drawn_given_is_not_determined() -> None:
    contract = one(
        {"id": "r", "derive": "s", "expr": "'x'"},
        columns=[_column("a", "INT"), _column("s", "STRING"), _column("b", "INT")],
    )
    statistics = {
        "a": {"range": {"min": "0", "max": "9"}},
        "s": {"support": {"values": ["x", "y"]}},
        "b": {
            "given": "s",
            "range": {"min": "0", "max": "9"},
            "bins": ["0", "5", "9"],
            "binWeightsGiven": {"x": ["1", "1"], "y": ["1", "1"]},
        },
    }
    assert "given" in refused("FLX-6210", contract, statistics=statistics)


def test_a_given_column_a_whenless_derive_sets_is_refused_as_determined() -> None:
    # The determined column declares no draw of its own, so the conditionals check would
    # refuse it as not categorical first; the rules' own refusal comes before it.
    contract = _contract(
        _expose(
            "t",
            [_column("a", "INT"), _column("s", "STRING"), _column("b", "INT")],
            [{"id": "s_of_a", "derive": "s", "expr": "CASE WHEN a > 5 THEN 'hi' ELSE 'lo' END"}],
        )
    )
    statistics = {
        "a": {"range": {"min": "0", "max": "10"}},
        "b": {
            "given": "s",
            "range": {"min": "0", "max": "9"},
            "bins": ["0", "5", "9"],
            "binWeightsGiven": {"hi": ["1", "1"], "lo": ["1", "1"]},
        },
    }
    assert "given" in refused("FLX-6210", contract, statistics=statistics)


def test_a_whenless_derive_column_with_no_draw_is_not_an_undriven_key_part() -> None:
    # An expose with a primary key relates its plan, which refuses a column that nothing
    # produces; a column that a derive without `when` sets is produced by its rule.
    contract = _contract(
        _expose(
            "dim",
            [_column("k", "INT", True), _column("j", "INT")],
            [{"id": "j_of_k", "derive": "j", "expr": "k + 1"}],
            semantics={"entities": [{"name": "dim", "type": "primary", "expr": "k"}]},
        )
    )
    plan = plan_of(contract, "dim", {"k": {"range": {"min": "1", "max": "50"}}}, rows=20)
    related = relate_plans({"dim": plan}, compile_keys(contract))
    data = table(related["dim"], generate_rows(related["dim"], root=root_for()))
    assert all(row["j"] == row["k"] + 1 for row in data)


def test_a_solved_column_drawn_given_another_is_refused() -> None:
    contract = _contract(
        _expose(
            "t",
            [_column("off", "INT"), _column("g", "STRING"), _column("d", "DATE")],
            [{"id": "back", "check": "off = DAYS_BETWEEN(DATE '2000-01-10', d)"}],
        )
    )
    statistics = {
        "off": {"range": {"min": "0", "max": "9"}},
        "g": {"support": {"values": ["x", "y"]}},
        "d": {
            "given": "g",
            "range": {"min": "2000-01-01", "max": "2000-01-20"},
            "bins": ["2000-01-01", "2000-01-10", "2000-01-20"],
            "binWeightsGiven": {"x": ["1", "0"], "y": ["0", "1"]},
        },
    }
    settings = {"back": {"generates": "d"}}
    message = refused("FLX-6210", contract, statistics=statistics, settings=settings)
    assert "drawn given" in message


def test_one_rule_plan_in_two_plans_reads_each_plans_statistics() -> None:
    contract = _offset_contract()
    settings = {"shift": {"generates": "d"}}
    wide = {
        "off": {"range": {"min": "0", "max": "29"}},
        "d": {"range": {"min": "2026-01-01", "max": "2026-12-31"}},
    }
    narrow = {
        "off": {"range": {"min": "0", "max": "29"}},
        "d": {"range": {"min": "2026-01-12", "max": "2026-01-15"}},
    }
    first = plan_of(contract, "t", wide, rows=120, settings=settings)
    second = build_plan(
        first.expose,
        sim_id=SIM_ID,
        seam_digest=SEAM,
        rows=120,
        fidelity={"columns": narrow},
        rules=first.rules,
    )
    generate_rows(first, root=root_for())
    for row in table(second, generate_rows(second, root=root_for())):
        assert date(2026, 1, 12) <= row["d"] <= date(2026, 1, 15)


def test_an_emit_with_no_fidelity_block_cannot_have_a_determined_column() -> None:
    contract = one({"id": "r", "derive": "b", "expr": "a + 1"})
    assert "fidelity" in refused("FLX-6210", contract, has_fidelity=False)
    # Rules that determine nothing are not a refusal, and need no plan.
    only_checks = one({"id": "r", "check": "a <= b"})
    assert (
        plan_rules(
            contract_document=only_checks,
            expose_id="t",
            settings=None,
            related=frozenset(),
            mapping=None,
            has_fidelity=False,
            strict=False,
        )
        is None
    )


def test_a_mapping_override_on_a_determined_column_is_two_sources() -> None:
    contract = one({"id": "r", "derive": "b", "expr": "a + 1"})
    message = refused("FLX-6203", contract, mapping={"b": "subject.id"}, has_fidelity=False)
    assert "'b'" in message and "'r'" in message


def test_a_derive_and_a_generates_on_one_column_are_two_sources() -> None:
    contract = one(
        {"id": "from_a", "derive": "d", "expr": "DATE '2026-01-01'"},
        {"id": "from_b", "check": "b = DAYS_BETWEEN(DATE '2026-01-01', d)"},
    )
    message = refused("FLX-6203", contract, settings={"from_b": {"generates": "d"}})
    assert "from_a" in message and "from_b" in message
    two = one(
        {"id": "one", "check": "a = DAYS_BETWEEN(DATE '2026-01-01', d)"},
        {"id": "two", "check": "b = DAYS_BETWEEN(DATE '2026-01-01', d)"},
    )
    refused("FLX-6203", two, settings={"one": {"generates": "d"}, "two": {"generates": "d"}})


def test_a_generates_setting_that_cannot_generate_is_refused() -> None:
    cases = {
        "a derive": one({"id": "r", "derive": "b", "expr": "a + 1"}),
        "a dependency": one({"id": "r", "dependency": {"from": ["a"], "to": ["b"]}}),
        "another form of check": one({"id": "r", "check": "a = 1 OR b = 1"}),
        "a bounds form that does not compare the column": one({"id": "r", "check": "a <= b"}),
    }
    for contract in cases.values():
        refused("FLX-6202", contract, settings={"r": {"generates": "d"}})
    reads_it = one(
        {"id": "r", "check": "a = DAYS_BETWEEN(DATE '2026-01-01', d)", "when": "d IS NOT NULL"}
    )
    assert "when" in refused("FLX-6202", reads_it, settings={"r": {"generates": "d"}})
    value_side = one(
        {"id": "r", "check": "DAYS_BETWEEN(e, d) = DAYS_BETWEEN(DATE '2026-01-01', d)"}
    )
    refused("FLX-6202", value_side, settings={"r": {"generates": "d"}})
    neither = one({"id": "r", "check": "a = DAYS_BETWEEN(e, DATE '2026-01-01')"})
    refused("FLX-6202", neither, settings={"r": {"generates": "d"}})
    both = one({"id": "r", "check": "a = DAYS_BETWEEN(d, d)"})
    refused("FLX-6202", both, settings={"r": {"generates": "d"}})


def test_the_settings_are_checked_against_the_contracts_rules() -> None:
    contract = one({"id": "r", "check": "a = DAYS_BETWEEN(DATE '2026-01-01', d)"})
    refused("FLX-3105", contract, settings={"nope": {"generates": "d"}})
    refused("FLX-3105", contract, settings={"r": {"generates": "d", "target": "1"}})
    refused("FLX-3105", contract, settings={"r": "d"})
    refused("FLX-3105", contract, settings=["r"])
    refused("FLX-3105", contract, settings={"r": {"generates": 3}})
    refused("FLX-6113", contract, settings={"r": {"generates": "nowhere"}})
    # An expose with no rule has no id for a setting to name.
    bare = _contract(_expose("t", [_column("a", "INT")]))
    refused("FLX-3105", bare, settings={"r": {"generates": "a"}})
    assert (
        plan_rules(
            contract_document=bare,
            expose_id="t",
            settings=None,
            related=frozenset(),
            mapping=None,
            has_fidelity=True,
            strict=False,
        )
        is None
    )


def test_ids_and_texts_keep_their_own_codes() -> None:
    refused(
        "FLX-6201",
        one({"id": "r", "derive": "b", "expr": "a"}, {"id": "r", "derive": "a", "expr": "b"}),
    )
    refused("FLX-6201", one({"derive": "b", "expr": "a"}))
    refused("FLX-6201", one({"id": "1x", "derive": "b", "expr": "a"}))
    refused("FLX-6103", one({"id": "r", "derive": "b", "expr": "a +"}))
    refused("FLX-6113", one({"id": "r", "derive": "b", "expr": "missing + 1"}))
    refused("FLX-6206", one({"id": "r", "check": "a + 1"}))
    refused("FLX-6110", one({"id": "r", "derive": "b", "expr": "a / 3"}))
    refused("FLX-6110", one({"id": "r", "derive": "a", "expr": "'text'"}))
    # An id repeated in another expose of the contract is a repeat in the contract.
    twice = _contract(
        _expose("u", [_column("a", "INT")], [{"id": "r", "check": "a > 0"}]),
        _expose("t", [_column("a", "INT")], [{"id": "r", "check": "a > 0"}]),
    )
    refused("FLX-6201", twice)
