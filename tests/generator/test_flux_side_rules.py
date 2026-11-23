# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Generating columns from a bundle's FLUX-side rules (EXPR-SPEC-2 §1, EXPR-SPEC-3, ADR 0011 step 1).

What is pinned:

* **A calendar follows its key.** The day of the month, the same day a year back and a quarter's
  name are FLUX-side rules in flux-expr/3, the first day of the month and the quarter are contract
  rules that read the day of the month, and every rule holds on every row, 2000-02-29 included.
* **One order.** Contract and FLUX-side rules are set in one order, and a cycle across them is
  ``FLX-6208``.
* **One source.** A column a contract rule determines is ``FLX-6203`` for a FLUX-side rule.
* **Refusals.** Languages, ids and texts keep their codes; a FLUX-side rule that reads beyond its
  row, or a FLUX-side ``check`` that would generate, is ``FLX-6210``.
* **The plan.** Coverage names ``bundle.rules.<ids>``, the plan records origin and language, and a
  plan with no FLUX-side rule keeps its document and digest.
"""

from __future__ import annotations

import pickle
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.expr2 import VALUE, Rows, check, parse
from flux_engine.expr2.errors import Expr2Error
from flux_engine.expr2.evaluate import decoder
from flux_engine.generator.coverage import coverage_of_plan
from flux_engine.generator.rows import GeneratorPlan, generate_rows
from flux_engine.generator.rules import model_from_contract, plan_rules
from flux_engine.seam.coverage import Origin
from flux_engine.stages.pipeline import check_flux_side_rule_ids
from tests.generator.test_rules import (
    _DOW,
    BASE_STATISTICS,
    SETTINGS,
    _column,
    _contract,
    _expose,
    one,
    plan_of,
    refused,
    root_for,
    table,
)

pytestmark = pytest.mark.unit

_FIRST = date(2000, 1, 1)

CONTRACT_RULES: List[Dict[str, Any]] = [
    {"id": "date_of_key", "check": f"d_sk - 1 = DAYS_BETWEEN({_DOW}, d_date)"},
    {"id": "year_of_date", "derive": "d_year", "expr": f"YEARS_BETWEEN({_DOW}, d_date) + 2000"},
    {
        "id": "month_of_date",
        "derive": "d_moy",
        "expr": f"MONTHS_BETWEEN({_DOW}, d_date) - 12 * YEARS_BETWEEN({_DOW}, d_date) + 1",
    },
    {"id": "quarter", "derive": "d_qoy", "expr": "ROUND((d_moy + 1) / 3, 0)"},
    {"id": "first_day_of_month", "derive": "d_first_dom", "expr": "d_sk - d_dom + 1"},
]

FLUX_RULES: List[Dict[str, Any]] = [
    {
        "id": "quarter_name",
        "language": "flux-expr/3",
        "derive": "d_quarter_name",
        "expr": "d_year || 'Q' || d_qoy",
    },
    {
        "id": "day_of_month",
        "language": "flux-expr/3",
        "derive": "d_dom",
        "expr": "EXTRACT(DAY FROM d_date)",
    },
    {
        "id": "same_day_last_year",
        "language": "flux-expr/3",
        "derive": "d_same_day_ly",
        "expr": "d_sk - DAYS_BETWEEN(ADD_YEARS(d_date, -1), d_date)",
    },
]

STATISTICS = {
    "d_sk": {"range": {"min": "50", "max": "70"}},
    "d_date": {"range": {"min": "2000-01-01", "max": "2013-09-08"}},
}


def calendar_contract() -> Dict[str, Any]:
    # Declared so that no rule reads a column declared before it, and so that the declared order
    # is not the order the columns are set in.
    names = "d_sk d_quarter_name d_same_day_ly d_first_dom d_qoy d_dom d_moy d_year d_date".split()
    kinds = {"d_date": "DATE", "d_quarter_name": "STRING"}
    schema = [_column(name, kinds.get(name, "INT"), name == "d_sk") for name in names]
    return _contract(_expose("calendar", schema, CONTRACT_RULES))


def calendar_plan(**kwargs: Any) -> GeneratorPlan:
    kwargs.setdefault("settings", SETTINGS)
    kwargs.setdefault("flux_rules", FLUX_RULES)
    kwargs.setdefault("rows", 200)
    return plan_of(calendar_contract(), "calendar", STATISTICS, **kwargs)


@pytest.fixture(scope="module")
def calendar() -> Tuple[GeneratorPlan, List[Dict[str, Any]]]:
    plan = calendar_plan()
    return plan, table(plan, generate_rows(plan, root=root_for()))


def one_flux(*rules: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    """The base table ``t`` and the FLUX-side rules to plan with it, as ``refused`` takes them."""
    kwargs["flux_rules"] = list(rules)
    return kwargs


def flux_refused(code: str, contract: Mapping[str, Any], *rules: Dict[str, Any], **kw: Any) -> str:
    return refused(code, contract, **one_flux(*rules, **kw))


# ---------------------------------------------------------------------------
# What generates
# ---------------------------------------------------------------------------


def test_the_calendar_follows_its_key(
    calendar: Tuple[GeneratorPlan, List[Dict[str, Any]]],
) -> None:
    plan, data = calendar
    days = {row["d_date"] for row in data}
    assert date(2000, 2, 29) in days
    for row in data:
        day = row["d_date"]
        assert day == _FIRST + timedelta(days=row["d_sk"] - 1)
        assert row["d_dom"] == day.day
        assert row["d_quarter_name"] == f"{day.year}Q{(day.month + 2) // 3}"
        try:
            back = day.replace(year=day.year - 1)
        except ValueError:  # 29 February has no day a year back; the day is clamped
            back = day.replace(year=day.year - 1, day=28)
        assert row["d_same_day_ly"] == row["d_sk"] - (day - back).days
        assert row["d_first_dom"] == row["d_sk"] - day.day + 1
    leap = next(row for row in data if row["d_date"] == date(2000, 2, 29))
    assert leap["d_same_day_ly"] == leap["d_sk"] - 366


def test_every_rule_holds_on_every_row_when_evaluated(
    calendar: Tuple[GeneratorPlan, List[Dict[str, Any]]],
) -> None:
    _, data = calendar
    contract = calendar_contract()
    model = model_from_contract(contract)
    scope = model.exposes["calendar"]
    memory = Rows(model, {"calendar": list(data)})
    held = 0
    for rule in CONTRACT_RULES:
        if "derive" not in rule:
            continue
        checked = check(
            parse(rule["expr"], side="core"),
            model,
            "calendar",
            position=VALUE,
            target=rule["derive"],
        )
        read = decoder(scope.columns[rule["derive"]])
        for row in memory.rows("calendar"):
            assert checked.evaluate(row) == read(row.get(rule["derive"])), (rule["id"], row.index)
            held += 1
    for rule in FLUX_RULES:
        checked = check(
            parse(rule["expr"], side="flux", language=rule["language"]),
            model,
            "calendar",
            position=VALUE,
            target=rule["derive"],
        )
        read = decoder(scope.columns[rule["derive"]])
        for row in memory.rows("calendar"):
            assert checked.evaluate(row) == read(row.get(rule["derive"])), (rule["id"], row.index)
            held += 1
    assert held == len(data) * 7


def test_the_columns_are_set_in_one_order_across_both_kinds_of_rule() -> None:
    plan = calendar_plan()
    assert plan.rules is not None
    order = plan.rules.order
    assert set(order) == {
        "d_date",
        "d_year",
        "d_moy",
        "d_qoy",
        "d_dom",
        "d_first_dom",
        "d_same_day_ly",
        "d_quarter_name",
    }
    # d_dom (FLUX-side) before d_first_dom (contract), and d_qoy (contract) and d_year before
    # d_quarter_name (FLUX-side).
    assert order.index("d_dom") < order.index("d_first_dom")
    assert order.index("d_qoy") < order.index("d_quarter_name")
    assert order.index("d_year") < order.index("d_quarter_name")
    assert order.index("d_date") < order.index("d_dom")


def test_a_flux_side_when_covers_some_rows_and_the_column_keeps_its_own_source_elsewhere() -> None:
    contract = _contract(
        _expose("t", [_column("a", "INT"), _column("b", "INT"), _column("d", "DATE")])
    )
    rules = [
        {
            "id": "dom_when_big",
            "language": "flux-expr/3",
            "derive": "b",
            "expr": "EXTRACT(DAY FROM d)",
            "when": "a > 4",
        }
    ]
    plan = plan_of(
        contract,
        "t",
        {k: BASE_STATISTICS[k] for k in ("a", "b", "d")},
        flux_rules=rules,
    )
    assert plan.rules is not None
    assert plan.rules.covers_every_row == frozenset()
    data = table(plan, generate_rows(plan, root=root_for()))
    covered = [row for row in data if row["a"] > 4]
    assert covered and all(row["b"] == row["d"].day for row in covered)


def test_a_whenless_flux_side_derive_needs_no_draw_of_its_own() -> None:
    contract = _contract(
        _expose("t", [_column("a", "INT"), _column("b", "INT"), _column("d", "DATE")])
    )
    rules = [{"id": "dom", "language": "flux-expr/3", "derive": "b", "expr": "EXTRACT(DAY FROM d)"}]
    plan = plan_of(contract, "t", {k: BASE_STATISTICS[k] for k in ("a", "d")}, flux_rules=rules)
    assert plan.rules is not None and plan.rules.covers_every_row == frozenset({"b"})
    data = table(plan, generate_rows(plan, root=root_for()))
    assert all(row["b"] == row["d"].day for row in data)


def test_flux_side_derives_with_whens_on_one_column_are_one_source() -> None:
    contract = _contract(
        _expose("t", [_column("a", "INT"), _column("b", "INT"), _column("d", "DATE")])
    )
    rules = [
        {
            "id": "low",
            "language": "flux-expr/3",
            "derive": "b",
            "expr": "EXTRACT(DAY FROM d)",
            "when": "a < 5",
        },
        {
            "id": "high",
            "language": "flux-expr/3",
            "derive": "b",
            "expr": "EXTRACT(MONTH FROM d)",
            "when": "a >= 5",
        },
    ]
    plan = plan_of(contract, "t", {k: BASE_STATISTICS[k] for k in "abd"}, flux_rules=rules)
    data = table(plan, generate_rows(plan, root=root_for()))
    for row in data:
        assert row["b"] == (row["d"].day if row["a"] < 5 else row["d"].month)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def test_coverage_names_a_flux_side_rule_and_a_contract_rule_apart() -> None:
    plan = calendar_plan()
    by_column = {row.column: row for row in coverage_of_plan(plan).columns}
    assert by_column["d_dom"].source == "bundle.rules.day_of_month"
    assert by_column["d_quarter_name"].source == "bundle.rules.quarter_name"
    assert by_column["d_first_dom"].source == "contract.invariants.first_day_of_month"
    for column in ("d_dom", "d_quarter_name", "d_same_day_ly"):
        assert (by_column[column].origin, by_column[column].decided_by) == (
            Origin.DERIVED,
            "rule",
        )


def test_the_plan_records_a_flux_side_rule_with_its_origin_and_language() -> None:
    plan = calendar_plan()
    document = plan.as_document()["rules"]
    rules = {rule["id"]: rule for rule in document["rules"]}
    assert list(rules) == [
        *(rule["id"] for rule in CONTRACT_RULES),
        *(rule["id"] for rule in FLUX_RULES),
    ]
    assert rules["day_of_month"]["origin"] == "bundle"
    assert rules["day_of_month"]["language"] == "flux-expr/3"
    assert rules["day_of_month"]["text"] == "EXTRACT(DAY FROM d_date)"
    assert rules["day_of_month"]["column"] == "d_dom"
    assert "origin" not in rules["first_day_of_month"]
    assert "language" not in rules["first_day_of_month"]


def test_a_flux_side_check_is_recorded_and_generates_nothing() -> None:
    contract = one()
    rules = [
        {
            "id": "later",
            "language": "flux-expr/3",
            "check": "EXTRACT(DAY FROM d) = 1 OR a >= 0",
        }
    ]
    plan = plan_of(contract, "t", BASE_STATISTICS, flux_rules=rules)
    assert plan.rules is not None and plan.rules.order == ()
    (rule,) = plan.rules.rules
    assert (rule.origin, rule.column, rule.language) == ("bundle", None, "flux-expr/3")


def test_a_plan_with_no_flux_side_rule_keeps_its_document_and_digest() -> None:
    contract = _contract(
        _expose("t", [_column("a", "INT"), _column("b", "INT"), _column("d", "DATE")])
    )
    statistics = {k: BASE_STATISTICS[k] for k in "abd"}
    without = plan_of(contract, "t", statistics)
    empty = plan_of(contract, "t", statistics, flux_rules=[])
    assert without.rules is None and "rules" not in without.as_document()
    assert empty.as_document() == without.as_document()
    assert empty.digest() == without.digest()
    ruled = plan_of(contract, "t", statistics, flux_rules=[{**_DOM, "derive": "b"}])
    assert ruled.digest() != without.digest()


def test_a_plan_with_flux_side_rules_pickles_and_makes_the_same_rows() -> None:
    plan = calendar_plan()
    rows = generate_rows(plan, root=root_for())  # compiles the rules
    copy = pickle.loads(pickle.dumps(plan))
    assert copy.as_document() == plan.as_document()
    assert generate_rows(copy, root=root_for()) == rows


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

_DOM = {"id": "dom", "language": "flux-expr/3", "derive": "a", "expr": "EXTRACT(DAY FROM d)"}


def test_a_missing_or_unknown_language_is_refused() -> None:
    contract = one()
    no_language = {k: v for k, v in _DOM.items() if k != "language"}
    flux_refused("FLX-6101", contract, no_language)
    flux_refused("FLX-6102", contract, {**_DOM, "language": "flux-expr/9"})
    flux_refused("FLX-6102", contract, {**_DOM, "language": "flux-expr/1"})
    flux_refused("FLX-6102", contract, {**_DOM, "language": None})


def test_an_extension_of_flux_expr_3_under_flux_expr_2_is_refused() -> None:
    flux_refused("FLX-6103", one(), {**_DOM, "language": "flux-expr/2"})


def test_ids_keep_their_own_code() -> None:
    contract = one()
    flux_refused("FLX-6201", contract, {k: v for k, v in _DOM.items() if k != "id"})
    flux_refused("FLX-6201", contract, {**_DOM, "id": "déjà"})
    flux_refused("FLX-6201", contract, {**_DOM, "id": "1x"})
    flux_refused("FLX-6201", contract, _DOM, {**_DOM, "derive": "b"})
    flux_refused("FLX-6201", contract, "not a mapping")  # type: ignore[arg-type]


def test_rules_that_are_not_a_list_are_refused() -> None:
    with pytest.raises(FluxError) as caught:
        plan_of(one(), "t", BASE_STATISTICS, flux_rules={"id": "x"})
    assert caught.value.code.code == "FLX-3105"


def test_an_id_repeated_across_two_emits_is_refused() -> None:
    def emit(expose_id: str, rules: Any) -> Any:
        return SimpleNamespace(expose_id=expose_id, flux_side_rules=rules)

    check_flux_side_rule_ids([emit("a", [_DOM]), emit("b", None), emit("c", [])], where="sim")
    with pytest.raises(Expr2Error) as caught:
        check_flux_side_rule_ids(
            [emit("a", [_DOM]), emit("b", [{**_DOM, "derive": "b"}])], where="s"
        )
    assert caught.value.code.code == "FLX-6201" and "'dom' repeats" in str(caught.value)
    with pytest.raises(Expr2Error) as caught:
        check_flux_side_rule_ids([emit("a", [{**_DOM, "id": "déjà"}])], where="s")
    assert caught.value.code.code == "FLX-6201"
    with pytest.raises(Expr2Error) as caught:
        check_flux_side_rule_ids([emit("a", ["text"])], where="s")
    assert caught.value.code.code == "FLX-6201"
    with pytest.raises(FluxError) as bad:
        check_flux_side_rule_ids([emit("a", "text")], where="s")
    assert bad.value.code.code == "FLX-3105"


def test_a_flux_expr_3_rule_that_uses_no_extension_belongs_in_the_contract() -> None:
    rule = {"id": "r", "language": "flux-expr/3", "derive": "a", "expr": "b + 1"}
    assert "contract" in flux_refused("FLX-6209", one(), rule)


def test_a_flux_side_rule_on_a_contract_derived_column_is_two_sources() -> None:
    contract = one({"id": "c", "derive": "a", "expr": "b + 1"})
    message = flux_refused("FLX-6203", contract, _DOM)
    assert "'c'" in message and "'dom'" in message
    generated = one({"id": "c", "check": "DAYS_BETWEEN(e, d) = a"})
    on_date = {**_DOM, "id": "on_date", "derive": "d", "expr": "ADD_DAYS(e, 3)"}
    message = flux_refused("FLX-6203", generated, on_date, settings={"c": {"generates": "d"}})
    assert "'c'" in message and "'on_date'" in message


def test_a_flux_side_rule_on_a_mapped_column_is_two_sources() -> None:
    flux_refused("FLX-6203", one(), _DOM, mapping={"a": "x.y"})


def test_a_cycle_across_a_contract_rule_and_a_flux_side_rule_is_refused() -> None:
    contract = one({"id": "b_of_a", "derive": "b", "expr": "a + 1"})
    rule = {
        "id": "a_of_b",
        "language": "flux-expr/3",
        "derive": "a",
        "expr": "b + EXTRACT(DAY FROM d)",
    }
    message = flux_refused("FLX-6208", contract, rule)
    assert "a_of_b" in message and "b_of_a" in message


def _tree() -> Dict[str, Any]:
    """``t`` points at ``p``, which points at ``g``."""

    def pointing(name: str, target: str, column: str, key: str) -> Dict[str, Any]:
        return {
            "relationships": [
                {
                    "name": name,
                    "to": target,
                    "fromColumns": [column],
                    "toColumns": [key],
                    "cardinality": "many_to_one",
                }
            ]
        }

    return _contract(
        _expose(
            "g",
            [_column("gk", "INT", True), _column("w", "INT")],
            semantics={"entities": [{"name": "g", "type": "primary", "expr": "gk"}]},
        ),
        _expose(
            "p",
            [_column("pk", "INT", True), _column("gfk", "INT"), _column("v", "INT")],
            semantics={
                "entities": [{"name": "p", "type": "primary", "expr": "pk"}],
                **pointing("g", "g", "gfk", "gk"),
            },
        ),
        _expose(
            "t",
            [_column("fk", "INT"), _column("b", "INT"), _column("d", "DATE")],
            semantics=pointing("p", "p", "fk", "pk"),
        ),
    )


@pytest.mark.parametrize(
    ("expose", "column", "expr"),
    [
        pytest.param("t", "b", "link.p.v + EXTRACT(DAY FROM d)", id="a-link"),
        pytest.param("t", "b", "p.g.w + 1", id="a-path"),
        pytest.param("t", "b", "RANK() OVER (PARTITION BY p ORDER BY d)", id="a-window"),
        pytest.param("p", "v", "COUNT(CHILDREN.t) + 1", id="children"),
    ],
)
def test_a_flux_side_derive_that_reads_beyond_its_row_is_not_generated_yet(
    expose: str, column: str, expr: str
) -> None:
    rule = {"id": "r", "language": "flux-expr/3", "derive": column, "expr": expr}
    with pytest.raises(Expr2Error) as caught:
        plan_of(_tree(), expose, {}, flux_rules=[rule])
    assert caught.value.code.code == "FLX-6210", str(caught.value)
    assert "another row" in str(caught.value)


def test_a_flux_side_when_that_reads_another_row_is_not_generated_yet() -> None:
    rule = {**_DOM, "derive": "b", "when": "link.p.v > EXTRACT(DAY FROM d)"}
    with pytest.raises(Expr2Error) as caught:
        plan_of(_tree(), "t", {}, flux_rules=[rule])
    assert caught.value.code.code == "FLX-6210"


def _versioned() -> Dict[str, Any]:
    window = {"typeParams": {"validityParams": {"isStart": True}}}
    end = {"typeParams": {"validityParams": {"isEnd": True}}}
    return _contract(
        _expose(
            "v",
            [
                _column("customer_id", "INT"),
                _column("valid_from", "DATE"),
                _column("valid_to", "DATE"),
                _column("b", "INT"),
            ],
            semantics={
                "entities": [{"name": "customer", "type": "natural", "expr": "customer_id"}],
                "dimensions": [
                    {"name": "valid_from", "type": "time", **window},
                    {"name": "valid_to", "type": "time", **end},
                ],
            },
        )
    )


def test_previous_on_a_versioned_expose_is_not_generated_yet() -> None:
    versions = model_from_contract(_versioned()).exposes["v"].versions
    assert versions is not None
    assert (versions.entity, versions.start, versions.end) == (
        ("customer_id",),
        "valid_from",
        "valid_to",
    )
    rule = {"id": "r", "language": "flux-expr/3", "derive": "b", "expr": "previous.b + 1"}
    with pytest.raises(Expr2Error) as caught:
        plan_of(_versioned(), "v", {}, flux_rules=[rule])
    assert caught.value.code.code == "FLX-6210", str(caught.value)


def test_a_contract_path_into_a_versioned_expose_is_refused_as_the_rfc_says() -> None:
    # RFC-data-rules.md section 2.2: no path crosses a relationship whose target declares a
    # validity window. The model used to leave versions out, so such a text passed.
    contract = _versioned()
    contract["exposes"].append(
        _expose(
            "t",
            [_column("t_id", "INT"), _column("customer_id", "INT"), _column("n", "INT")],
            [{"id": "r", "check": "p.b > 0"}],
            semantics={
                "relationships": [
                    {
                        "name": "p",
                        "to": "v",
                        "fromColumns": ["customer_id"],
                        "toColumns": ["customer_id"],
                        "cardinality": "many_to_one",
                    }
                ]
            },
        )
    )
    with pytest.raises(Expr2Error) as caught:
        plan_of(contract, "t", {"n": {"range": {"min": "0", "max": "9"}}})
    assert caught.value.code.code == "FLX-6106", str(caught.value)


def test_previous_on_an_unversioned_expose_is_refused_as_unreachable() -> None:
    assert model_from_contract(_tree()).exposes["t"].versions is None
    rule = {"id": "r", "language": "flux-expr/3", "derive": "b", "expr": "previous.b + 1"}
    with pytest.raises(Expr2Error) as caught:
        plan_of(_tree(), "t", {}, flux_rules=[rule])
    assert caught.value.code.code == "FLX-6106"


def test_a_flux_side_derive_refuses_generates_and_unknown_keys() -> None:
    flux_refused("FLX-6202", one(), {**_DOM, "generates": "d"})
    assert "colour" in flux_refused("FLX-3105", one(), {**_DOM, "colour": "red"})


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({"colour": "red"}, id="an-unknown-key"),
        pytest.param({3: "x"}, id="a-key-that-is-not-text"),
        pytest.param({"check": "a > 0"}, id="both-check-and-derive"),
        pytest.param({"derive": None}, id="derive-null"),
        pytest.param({"derive": 5}, id="derive-not-text"),
        pytest.param({"expr": 7}, id="expr-not-text"),
        pytest.param({"when": 7}, id="when-not-text"),
        pytest.param({"mustBeBetween": 5}, id="a-band-that-is-not-a-pair"),
        pytest.param({"mustBe": "many"}, id="a-band-that-is-not-a-number"),
        pytest.param({"unit": "days"}, id="a-band-unit-that-is-not-known"),
        pytest.param({"mustBe": 1, "mustBeLessThan": 2}, id="two-band-operators"),
    ],
)
def test_a_malformed_flux_side_entry_is_a_flux_error_naming_it(entry: Dict[str, Any]) -> None:
    message = flux_refused("FLX-3105", one(), {**_DOM, **entry})
    assert "'dom'" in message


def test_a_flux_side_check_with_neither_text_or_a_non_text_check_is_refused() -> None:
    bare = {"id": "r", "language": "flux-expr/3"}
    assert "'r'" in flux_refused("FLX-3105", one(), bare)
    assert "'r'" in flux_refused("FLX-3105", one(), {**bare, "check": 5})
    assert "'r'" in flux_refused("FLX-3105", one(), {**bare, "derive": "a"})


def test_a_flux_side_derive_target_is_normalised_as_the_contract_side_is() -> None:
    contract = _contract(
        _expose("t", [_column("caf\u00e9", "INT"), _column("d", "DATE")]),
    )
    rule = {**_DOM, "derive": "cafe\u0301"}  # NFD spelling of the NFC column name
    plan = plan_of(contract, "t", {"d": BASE_STATISTICS["d"]}, flux_rules=[rule])
    assert plan.rules is not None and plan.rules.order == ("caf\u00e9",)


def test_a_flux_side_rule_is_checked_against_the_model_and_its_column() -> None:
    flux_refused("FLX-6113", one(), {**_DOM, "expr": "EXTRACT(DAY FROM missing)"})
    flux_refused("FLX-6113", one(), {**_DOM, "derive": "missing"})
    flux_refused("FLX-6110", one(), {**_DOM, "derive": "s"})
    flux_refused(
        "FLX-6206", one(), {"id": "r", "language": "flux-expr/3", "check": "EXTRACT(DAY FROM d)"}
    )


def test_a_flux_side_rule_on_a_key_or_with_no_fidelity_is_refused() -> None:
    flux_refused("FLX-6210", one(), _DOM, has_fidelity=False)


def test_plan_rules_reads_flux_side_rules_with_no_contract_invariant() -> None:
    contract = _contract(_expose("t", [_column("a", "INT"), _column("d", "DATE")]))
    rules = plan_rules(
        contract_document=contract,
        expose_id="t",
        settings=None,
        related=frozenset(),
        mapping=None,
        has_fidelity=True,
        strict=False,
        flux_rules=[_DOM],
    )
    assert rules is not None and rules.order == ("a",)
    assert (
        plan_rules(
            contract_document=contract,
            expose_id="t",
            settings=None,
            related=frozenset(),
            mapping=None,
            has_fidelity=True,
            strict=False,
            flux_rules=[],
        )
        is None
    )
