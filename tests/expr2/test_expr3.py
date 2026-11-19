# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""flux-expr/3: a date part, date arithmetic and string concatenation (EXPR-SPEC-3, ADR 0013).

``expr3-vectors.json`` is driven through the parser, the checker and the
evaluator like the other vector files; ``test_expr3_oracle.py`` runs every
accepted vector as a DuckDB query, and is the only part skipped without
DuckDB. The rest pins what the vectors cannot: that flux-expr/2 is unchanged under
flux-expr/3, that the new forms are ``FLX-6103`` under flux-expr/2 and in a
core text, that the canonical text is a fixed point, the language key and
``FLX-6209``, and the five rules of ADR 0011's date dimension.

DuckDB is a test dependency only; the engine never imports it.
"""

from __future__ import annotations

import json
from datetime import date
from fractions import Fraction
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.expr2 import (
    Expr2Error,
    Parsed,
    Side,
    add_days,
    add_years,
    check,
    check_flux_side_rule,
    check_language,
    months_between,
    parse,
    to_json,
    try_add_months,
    unparse,
    uses_extension,
)
from flux_engine.expr2.ast import is_extension

from .support import (
    CORE_VECTORS,
    EXTENSION_VECTORS,
    SHARED_VECTORS,
    DictRow,
    load,
    matches,
    models_of,
    normal,
    rows_of,
)

pytestmark = pytest.mark.unit

EXPR3_VECTORS = EXTENSION_VECTORS.with_name("expr3-vectors.json")
DOCUMENT = load(EXPR3_VECTORS)
CALENDAR = models_of(DOCUMENT)
LANGUAGE_3 = "flux-expr/3"


def _vectors() -> List[Any]:
    out = []
    for raw in DOCUMENT["vectors"]:
        vector = normal(raw)
        vector["language"] = raw.get("language", DOCUMENT["language"])
        out.append(pytest.param(vector, id=vector["id"]))
    return out


VECTORS = _vectors()


def _code(call: Any) -> str:
    with pytest.raises(Expr2Error) as raised:
        call()
    return str(raised.value.code.code)


def _parse(text: str, language: str = LANGUAGE_3, side: Side = Side.FLUX) -> Parsed:
    return parse(text, side=side, language=language)


# ------------------------------------------------------------------ vectors


@pytest.mark.golden
@pytest.mark.parametrize("vector", VECTORS)
def test_vector(vector: Dict[str, Any]) -> None:
    side, language = vector["side"], vector["language"]
    if vector["step"] == "parse":
        assert (
            _code(lambda: parse(vector["text"], side=side, language=language)) in vector["refused"]
        )
        return
    model = CALENDAR[vector["model"]]

    def checked_of(parsed: Parsed) -> Any:
        return check(
            parsed, model, vector["expose"], position=vector["position"], target=vector["target"]
        )

    if vector["refused"] is not None:
        with pytest.raises(Expr2Error) as raised:
            parsed = parse(vector["text"], side=side, language=language)
            assert to_json(parsed.tree) == vector["tree"]
            checked_of(parsed)
        assert raised.value.code.code in vector["refused"], raised.value
        return
    parsed = parse(vector["text"], side=side, language=language)
    assert to_json(parsed.tree) == vector["tree"]
    checked = checked_of(parsed)
    assert checked.type.kind is not None and checked.type.kind.value == vector["type"]
    if vector["type"] == "number":
        assert str(checked.type.scale) == vector["scale"]
    assert checked.canonical == vector["canonical"]
    again = parse(checked.canonical, side=side, language=language)
    assert again.tree == parsed.tree
    assert checked_of(again).canonical == checked.canonical
    rows = rows_of(DOCUMENT, CALENDAR, "calendar")
    for case in vector["eval"]:
        row = rows.row(vector["expose"], int(case["row"]))
        got = checked.evaluate(row)
        assert matches(case["result"], got), (case, got)


@pytest.mark.golden
def test_vector_ids_are_unique_and_every_refusal_code_is_covered() -> None:
    ids = [vector["id"] for vector in DOCUMENT["vectors"]]
    assert len(ids) == len(set(ids))
    raised = {code for param in VECTORS for code in (param.values[0]["refused"] or ())}
    # FLX-6209 and FLX-6102 are about rule entries (below), not texts.
    assert {"FLX-6103", "FLX-6104", "FLX-6109", "FLX-6110", "FLX-6206"} <= raised


@pytest.mark.golden
def test_the_vectors_cover_every_new_node_and_function() -> None:
    nodes = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "node" in value:
                nodes.add(value["node"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for param in VECTORS:
        walk(param.values[0]["tree"])
    assert {"extract", "date_add", "concat"} <= nodes
    units = {
        node["unit"]
        for raw in DOCUMENT["vectors"]
        for node in [raw["expect"].get("parse")]
        if isinstance(node, dict) and node.get("node") == "date_add"
    }
    assert units == {"day", "month", "year"}
    texts = " ".join(p.values[0]["text"].upper() for p in VECTORS if not p.values[0]["refused"])
    for word in ("EXTRACT(", "ADD_DAYS(", "ADD_MONTHS(", "ADD_YEARS(", " || "):
        assert word in texts, word


@pytest.mark.golden
def test_no_json_number_appears() -> None:
    def walk(value: Any) -> None:
        assert not isinstance(value, float)
        assert isinstance(value, bool) or not isinstance(value, int), value
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads(EXPR3_VECTORS.read_text(encoding="utf-8")))


# ----------------------------------------------------------- the DuckDB oracle


def test_months_between_is_the_inverse_of_add_months() -> None:
    for year in (1999, 2000, 2001, 2023, 2024):
        for month in range(1, 13):
            for day in (1, 15, 28, 29, 30, 31):
                try:
                    start = date(year, month, day)
                except ValueError:
                    continue
                for n in range(-30, 31):
                    moved = try_add_months(start, n)
                    assert moved is not None
                    assert months_between(start, moved) == n, (start, n)


def test_date_arithmetic_is_total() -> None:
    assert add_days(date(9999, 12, 31), 1) is None
    assert add_days(date(1, 1, 1), -1) is None
    assert add_days(date(2000, 1, 1), 10**30) is None
    assert try_add_months(date(2000, 1, 31), 10**30) is None
    assert add_years(date(9999, 1, 1), 1) is None
    assert add_years(date(1, 1, 1), -1) is None
    assert add_years(date(2000, 2, 29), -1) == date(1999, 2, 28)
    assert add_years(date(2000, 2, 29), 4) == date(2004, 2, 29)
    assert try_add_months(date(2001, 1, 31), 1) == date(2001, 2, 28)


# ------------------------------------------------- flux-expr/2 is unchanged


def _accepted_texts() -> List[Any]:
    out = []
    for path in (SHARED_VECTORS, CORE_VECTORS, EXTENSION_VECTORS):
        for raw in load(path)["vectors"]:
            vector = normal(raw)
            if vector["refused"] is None:
                out.append(
                    pytest.param(vector["text"], vector["side"], id=f"{path.stem}:{vector['id']}")
                )
    return out


@pytest.mark.parametrize(("text", "side"), _accepted_texts())
def test_a_flux_expr_2_text_is_the_same_tree_under_flux_expr_3(text: str, side: Side) -> None:
    two = parse(text, side=side)
    three = parse(text, side=side, language=LANGUAGE_3)
    assert two.tree == three.tree
    assert to_json(two.tree) == to_json(three.tree)
    flux_side = side is Side.FLUX
    assert unparse(two.tree, flux_side=flux_side) == unparse(three.tree, flux_side=flux_side)
    assert (two.language, three.language) == ("flux-expr/2", LANGUAGE_3)


def test_the_default_language_is_flux_expr_2() -> None:
    assert parse("1", side=Side.FLUX).language == "flux-expr/2"
    assert _parse("1", language="flux-expr/2").language == "flux-expr/2"


def test_an_unknown_language_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="flux-expr/4"):
        parse("1", side=Side.FLUX, language="flux-expr/4")


# ---------------------------------------------------------------- closedness

NEW_FORMS = [
    "EXTRACT(DAY FROM d)",
    "ADD_DAYS(d, 1)",
    "ADD_MONTHS(d, 1)",
    "ADD_YEARS(d, 1)",
    "label || 'Q'",
    "(label || 'Q') = 'xQ'",
    "COALESCE(label || 'Q', 'z')",
    "ROUND(EXTRACT(DAY FROM d), 0)",
]


@pytest.mark.parametrize("text", NEW_FORMS)
def test_the_new_forms_are_refused_under_flux_expr_2(text: str) -> None:
    assert _code(lambda: parse(text, side=Side.FLUX)) == "FLX-6103"
    assert _code(lambda: parse(text, side=Side.FLUX, language="flux-expr/2")) == "FLX-6103"


@pytest.mark.parametrize("text", NEW_FORMS)
def test_the_new_forms_are_refused_in_a_core_text(text: str) -> None:
    assert _code(lambda: parse(text, side=Side.CORE)) == "FLX-6103"
    assert _code(lambda: parse(text, side=Side.CORE, language=LANGUAGE_3)) == "FLX-6103"


@pytest.mark.parametrize("text", NEW_FORMS)
def test_the_new_forms_parse_under_flux_expr_3(text: str) -> None:
    assert parse(text, side=Side.FLUX, language=LANGUAGE_3).language == LANGUAGE_3


def test_a_tree_checked_under_another_language_is_refused() -> None:
    tree = _parse("EXTRACT(DAY FROM d)").tree
    for parsed in (
        Parsed("EXTRACT(DAY FROM d)", tree, Side.FLUX, 2, "flux-expr/2"),
        Parsed("EXTRACT(DAY FROM d)", tree, Side.CORE, 2, LANGUAGE_3),
    ):
        with pytest.raises(Expr2Error) as raised:
            check(parsed, CALENDAR["calendar"], "quirks", position="value")
        assert raised.value.code.code == "FLX-6103"


def test_the_field_words_are_ordinary_names_outside_extract() -> None:
    quirks = CALENDAR["calendar"]
    for text in ("year", "month", "day", "year + month + day"):
        checked = check(_parse(text), quirks, "quirks", position="value")
        assert checked.canonical == text
    # Inside EXTRACT too, after FROM.
    checked = check(_parse("EXTRACT(YEAR FROM d) + year"), quirks, "quirks", position="value")
    assert checked.canonical == "EXTRACT(YEAR FROM d) + year"


def test_a_function_name_without_a_call_is_a_column_even_in_a_core_text() -> None:
    for name in ("extract", "add_days", "add_months", "add_years"):
        assert to_json(parse(name, side=Side.CORE).tree) == {"node": "column", "name": name}
        assert to_json(_parse(name).tree) == {"node": "column", "name": name}


def test_a_lone_pipe_and_a_triple_pipe_stay_refused() -> None:
    for text in ("a | b", "a ||| b", "|| a", "a ||"):
        with pytest.raises(Expr2Error) as raised:
            _parse(text)
        assert raised.value.code.code == "FLX-6103", text


def test_depth_counts_the_new_operators() -> None:
    deep = "a" + " || a" * 70
    assert _code(lambda: _parse(deep)) == "FLX-6103"
    assert _parse("a" + " || a" * 30).depth <= 64


# ---------------------------------------------------------- canonical form

FIXED_POINTS = [
    "a || b",
    "a || b || c",
    "(a || b) || c",
    "a || (b || c)",
    "a || (b || c) || d",
    "a || b + c",
    "(a || b) + c",
    "a + (b || c)",
    "a * (b || c)",
    "-(a || b)",
    "a || -b",
    "a || b = c",
    "a = b || c",
    "a = (b = c || d)",
    "(a = b) || c",
    "a || b IS NULL",
    "(a || b) IS NOT DISTINCT FROM c || d",
    "a || b BETWEEN c || d AND e",
    "a || b IN ('x', 'y')",
    "NOT (a || b = c)",
    "CASE WHEN a || b = c THEN d || e ELSE f END",
    "COALESCE(a || b, c || d)",
    "ROUND((a || b), 2)",
    "DAYS_BETWEEN((a || b), c)",
    "EXTRACT(year FROM d)",
    "extract(Day from d + 1)",
    "EXTRACT(DAY FROM ADD_MONTHS(d, 1))",
    "EXTRACT(DAY FROM CASE WHEN a = b THEN d END)",
    "ADD_DAYS(d, n + 1)",
    "add_months(d, -n)",
    "ADD_YEARS(ADD_DAYS(d, 1), n * 2)",
    "ADD_DAYS(d, a || b)",
    "ADD_DAYS(d, 1) = ADD_MONTHS(e, 2)",
    "'D' || EXTRACT(DAY FROM d) || '-' || ADD_DAYS(d, 1)",
    "x || (EXTRACT(DAY FROM d) = 1)",
    '"extract" || "year"',
]


@pytest.mark.parametrize("text", FIXED_POINTS)
def test_unparse_is_a_fixed_point(text: str) -> None:
    parsed = _parse(text)
    canonical = unparse(parsed.tree, flux_side=True)
    again = _parse(canonical)
    assert again.tree == parsed.tree
    assert unparse(again.tree, flux_side=True) == canonical


def test_canonical_spelling() -> None:
    def canonical(text: str) -> str:
        return unparse(_parse(text).tree, flux_side=True)

    assert canonical("extract(year from d)") == "EXTRACT(YEAR FROM d)"
    assert canonical("add_days( d ,1 )") == "ADD_DAYS(d, 1)"
    assert canonical("a||b") == "a || b"
    assert canonical("(a || b) || c") == "a || b || c"
    assert canonical("a || (b || c)") == "a || (b || c)"
    assert canonical("a || (b + c)") == "a || b + c"
    assert canonical("(a || b) + c") == "(a || b) + c"
    assert canonical("(a || b) = c") == "a || b = c"
    assert canonical("a || (b = c)") == "a || (b = c)"


def test_the_tree_is_left_nested_and_looser_than_plus() -> None:
    tree = to_json(_parse("a || b + 1 || c").tree)
    assert tree["node"] == "concat"
    assert tree["right"] == {"node": "column", "name": "c"}
    assert tree["left"]["node"] == "concat"
    assert tree["left"]["right"]["node"] == "arith"


def test_the_extension_helpers_know_the_new_nodes() -> None:
    for text in ("EXTRACT(DAY FROM d)", "ADD_DAYS(d, 1)", "a || b", "ROUND(ADD_DAYS(d, 1) - d, 0)"):
        assert uses_extension(_parse(text).tree), text
    for text in ("d", "ROUND(n, 0)", "DAYS_BETWEEN(d, d)"):
        assert not uses_extension(_parse(text).tree), text
    assert is_extension(_parse("a || b").tree)
    assert is_extension(_parse("EXTRACT(DAY FROM d)").tree)


# ------------------------------------------------------- language and FLX-6209


def test_check_language_accepts_both_languages() -> None:
    assert check_language({"language": "flux-expr/2"}, where="rule r") == "flux-expr/2"
    assert check_language({"language": "flux-expr/3"}, where="rule r") == "flux-expr/3"


@pytest.mark.parametrize(
    "declared", ["flux-expr/1", "flux-expr/4", "FLUX-EXPR/3", "", None, 3, ["flux-expr/3"]]
)
def test_check_language_refuses_every_other_language(declared: Any) -> None:
    assert _code(lambda: check_language({"language": declared}, where="rule r")) == "FLX-6102"


def test_check_language_still_requires_the_key() -> None:
    assert _code(lambda: check_language({}, where="rule r")) == "FLX-6101"


def _rule(**entry: Any) -> Dict[str, Any]:
    return {"id": "r", "language": LANGUAGE_3, **entry}


def test_a_flux_expr_3_rule_that_uses_only_the_core_is_flx_6209() -> None:
    model = CALENDAR["calendar"]
    assert _code(lambda: check_flux_side_rule(_rule(check="n > 1"), model, "quirks")) == "FLX-6209"
    assert (
        _code(
            lambda: check_flux_side_rule(
                _rule(derive="year", expr="year + 1"),
                model,
                "quirks",
            )
        )
        == "FLX-6209"
    )
    with pytest.raises(Expr2Error, match="seven"):
        check_flux_side_rule(_rule(check="n > 1"), model, "quirks")


def test_a_flux_expr_2_rule_that_uses_only_the_core_still_says_four() -> None:
    model = CALENDAR["calendar"]
    with pytest.raises(Expr2Error, match="four"):
        check_flux_side_rule(_rule(language="flux-expr/2", check="n > 1"), model, "quirks")


@pytest.mark.parametrize(
    "entry",
    [
        {"check": "EXTRACT(DAY FROM d) = day"},
        {"check": "label || 'Q' = 'xQ'"},
        {"check": "ADD_DAYS(d, 1) > d"},
        {"derive": "label", "expr": "'a' || label"},
        {"derive": "d", "expr": "ADD_YEARS(d, -1)"},
        {"derive": "day", "expr": "EXTRACT(DAY FROM d)"},
        {"check": "n > 1", "when": "EXTRACT(DAY FROM d) = 1"},
        {"check": "n > 1", "when": "label || 'Q' = 'xQ'"},
    ],
)
def test_a_flux_expr_3_rule_with_an_extension_is_not_flx_6209(entry: Dict[str, Any]) -> None:
    rule = check_flux_side_rule(_rule(**entry), CALENDAR["calendar"], "quirks")
    assert rule.id == "r"


@pytest.mark.parametrize(
    "entry",
    [
        {"check": "EXTRACT(DAY FROM d) = day"},
        {"derive": "label", "expr": "'a' || label"},
        {"check": "n > 1", "when": "ADD_DAYS(d, 1) > d"},
    ],
)
def test_the_new_forms_in_a_flux_expr_2_rule_are_flx_6103(entry: Dict[str, Any]) -> None:
    rule = _rule(language="flux-expr/2", **entry)
    with pytest.raises(Expr2Error) as raised:
        check_flux_side_rule(rule, CALENDAR["calendar"], "quirks")
    assert raised.value.code.code == "FLX-6103"


def test_a_flux_expr_3_rule_with_an_extension_of_flux_expr_2_is_not_flx_6209() -> None:
    telco = models_of(load(EXTENSION_VECTORS))["telco"]
    rule = check_flux_side_rule(
        {
            "id": "r",
            "language": LANGUAGE_3,
            "derive": "quantity",
            "expr": "COUNT(CHILDREN.invoice_lines) + 1",
        },
        telco,
        "subscriptions",
    )
    assert rule.derive == "quantity"


# --------------------------------------------- the five rules of ADR 0011 step 1


def _dim(index: int) -> Any:
    return rows_of(DOCUMENT, CALENDAR, "calendar").row("date_dim", index)


def _value(text: str, index: int, target: Optional[str] = None) -> Any:
    model = CALENDAR["calendar"]
    checked = check(_parse(text), model, "date_dim", position="value", target=target)
    return checked.evaluate(_dim(index))


def test_the_day_of_the_month() -> None:
    assert _value("EXTRACT(DAY FROM d_date)", 3) == Fraction(15)  # 1998-03-15
    assert _value("EXTRACT(DAY FROM d_date)", 1) == Fraction(29)  # 2000-02-29
    assert _value("EXTRACT(DAY FROM d_date)", 4) is None


def test_the_key_of_the_same_day_a_year_back() -> None:
    rule = "d_date_sk - DAYS_BETWEEN(ADD_YEARS(d_date, -1), d_date)"
    # 2000-02-29 gives the key of 1999-02-28, which is row 2.
    assert _value(rule, 1) == Fraction(
        int(DOCUMENT["data"]["calendar"]["rows"]["date_dim"][2]["d_date_sk"])
    )
    assert _value(rule, 1) == Fraction(2451238)
    assert _value(rule, 0) == Fraction(2451545 - 365)  # 2000-01-01 back to 1999-01-01
    assert _value(rule, 4) is None


def test_the_quarter_name() -> None:
    rule = "d_year || 'Q' || d_qoy"
    assert _value(rule, 3, target="d_quarter_name") == "1998Q1"
    for index, row in enumerate(DOCUMENT["data"]["calendar"]["rows"]["date_dim"]):
        assert _value(rule, index, target="d_quarter_name") == row["d_quarter_name"]


def test_the_date_from_the_julian_day() -> None:
    rule = "ADD_DAYS(DATE '1970-01-01', d_date_sk - 2440588)"
    for index, row in enumerate(DOCUMENT["data"]["calendar"]["rows"]["date_dim"]):
        got = _value(rule, index, target="d_date")
        assert (None if got is None else got.isoformat()) == row["d_date"]


def test_a_month_move_clamps_the_day() -> None:
    assert _value("ADD_MONTHS(DATE '2001-01-31', 1)", 0) == date(2001, 2, 28)


def test_the_five_rules_are_flux_side_rules_that_the_contract_cannot_carry() -> None:
    model = CALENDAR["calendar"]
    rules = [
        _rule(check="EXTRACT(DAY FROM d_date) > 0"),
        _rule(derive="d_quarter_name", expr="d_year || 'Q' || d_qoy"),
        _rule(derive="d_date", expr="ADD_DAYS(DATE '1970-01-01', d_date_sk - 2440588)"),
        _rule(check="d_date_sk - DAYS_BETWEEN(ADD_YEARS(d_date, -1), d_date) > 0"),
        _rule(check="ADD_MONTHS(DATE '2001-01-31', 1) > d_date"),
    ]
    for entry in rules:
        assert check_flux_side_rule(entry, model, "date_dim").id == "r"


# ----------------------------------------------------------------- values


def test_a_concatenation_writes_an_integer_as_its_canonical_digits() -> None:
    model = CALENDAR["calendar"]
    row = DictRow(model, "quirks", {"n": "-7", "label": "L", "price": "1.50", "year": "0012"}, {})
    for text, expected in (
        ("n || label", "-7L"),
        ("label || n", "L-7"),
        ("year || 'Q'", "12Q"),
        ("'a' || 000 || 'b'", "a0b"),
        ("label || ROUND(price, 0)", "L2"),
    ):
        assert check(_parse(text), model, "quirks", position="value").evaluate(row) == expected


def test_a_string_is_taken_as_stored() -> None:
    model = CALENDAR["calendar"]
    row = DictRow(model, "quirks", {"label": "é", "note": " é "}, {})
    got = check(_parse("label || note"), model, "quirks", position="value").evaluate(row)
    assert got == "é é "


def test_a_concatenation_with_a_null_operand_is_null() -> None:
    model = CALENDAR["calendar"]
    for label, n in ((None, None), ("x", None), (None, "1"), ("x", "1")):
        row = DictRow(model, "quirks", {"label": label, "n": n}, {})
        for text, parts in (
            ("label || n", (label, n)),
            ("n || label", (n, label)),
            ("label || label", (label, label)),
            ("n || n", (n, n)),
        ):
            got = check(_parse(text), model, "quirks", position="value").evaluate(row)
            assert (got is None) == (None in parts), (text, label, n)


def test_the_checker_reports_the_new_types() -> None:
    model = CALENDAR["calendar"]
    for text, kind, scale in (
        ("EXTRACT(MONTH FROM d)", "number", 0),
        ("ADD_YEARS(d, 1)", "date", None),
        ("label || n", "string", None),
    ):
        checked = check(_parse(text), model, "quirks", position="value")
        assert checked.type.kind is not None and checked.type.kind.value == kind
        if kind == "number":
            assert checked.type.scale == scale


# ------------------------------------------ closedness is decided at the lexer


@pytest.mark.parametrize(
    "text",
    [
        "NULL = 1 AND a || b",
        "x IN (NULL) || 1",
        "CASE WHEN a THEN NULL = 1 ELSE 2 END || 1",
        "DATE '2000-13-45' || 1",
    ],
)
def test_a_pipe_pair_outside_flux_expr_3_is_flx_6103_whatever_precedes_it(text: str) -> None:
    for side, language in (
        (Side.FLUX, "flux-expr/2"),
        (Side.CORE, "flux-expr/2"),
        (Side.CORE, LANGUAGE_3),
    ):
        with pytest.raises(Expr2Error) as raised:
            parse(text, side=side, language=language)
        assert raised.value.code.code == "FLX-6103", (text, side, language)


# ------------------------------------------------------------------ totality

_WIDE = {
    "exposes": {
        "t": {
            "columns": {"id": {"type": "string"}, "big": {"type": "decimal(38,0)"}},
            "key": ["id"],
        }
    }
}


def _product(depth: int) -> str:
    text = "big"
    for _ in range(depth):
        text = f"({text} * {text})"
    return text


def test_a_concatenation_of_a_many_digit_product_never_raises() -> None:
    from decimal import Decimal

    from flux_engine.expr2 import model_from_json

    model = model_from_json(_WIDE)
    value = 10**38 - 1
    text = _product(7) + " || 'x'"
    assert len(text) < 4096
    row = DictRow(model, "t", {"id": "1", "big": str(value)}, {})
    got = check(_parse(text), model, "t", position="value").evaluate(row)
    expected = str(Decimal(value**128))
    assert len(expected) > 4300
    assert got == expected + "x"
    assert check(_parse("'x' || " + _product(7)), model, "t", position="value").evaluate(row) == (
        "x" + expected
    )


# ------------------------------------------------ an enum column, every route

_ENUM = {
    "exposes": {
        "parents": {
            "columns": {"p_id": {"type": "string"}, "k": {"type": "string", "enum": ["a", "b"]}},
            "key": ["p_id"],
        },
        "kids": {
            "columns": {
                "k_id": {"type": "string"},
                "p_id": {"type": "string"},
                "s": {"type": "string"},
                "e": {"type": "string", "enum": ["a", "b"]},
            },
            "key": ["k_id"],
            "links": {"parent": {"to": "parents", "fromColumns": ["p_id"], "toColumns": ["p_id"]}},
        },
    }
}


@pytest.mark.parametrize(
    ("expose", "target", "expr"),
    [
        ("parents", "k", "MAX(CHILDREN.kids, s || 'x')"),
        ("parents", "k", "MIN(CHILDREN.kids, s || 'x')"),
        ("parents", "k", "COALESCE(MAX(CHILDREN.kids, s || 'x'), 'a')"),
        ("parents", "k", "CASE WHEN p_id = 'q' THEN MIN(CHILDREN.kids, s || 'x') END"),
        ("kids", "e", "FIRST_VALUE(s || 'x') OVER (PARTITION BY parent ORDER BY k_id)"),
        ("kids", "e", "LAST_VALUE(s || 'x') OVER (PARTITION BY parent ORDER BY k_id)"),
        ("kids", "e", "LAG(s || 'x', 1) OVER (PARTITION BY parent ORDER BY k_id)"),
        ("kids", "e", "LEAD(s || 'x', 1) OVER (PARTITION BY parent ORDER BY k_id)"),
        ("kids", "e", "LAG(s, 1, 'a') OVER (PARTITION BY parent ORDER BY k_id) || 'x'"),
        ("kids", "e", "s || 'x'"),
    ],
)
def test_a_concatenation_reaches_no_enum_column_by_any_route(
    expose: str, target: str, expr: str
) -> None:
    from flux_engine.expr2 import model_from_json

    model = model_from_json(_ENUM)
    with pytest.raises(Expr2Error) as raised:
        check(_parse(expr), model, expose, position="value", target=target)
    assert raised.value.code.code == "FLX-6110", expr


def test_a_concatenation_is_no_lag_or_lead_default() -> None:
    # The default of LAG and LEAD is a literal, so a concatenation there is a literal of
    # the wrong form (FLX-6109) before it could reach an enum column.
    from flux_engine.expr2 import model_from_json

    model = model_from_json(_ENUM)
    for name in ("LAG", "LEAD"):
        text = f"{name}(s, 1, s || 'x') OVER (PARTITION BY parent ORDER BY k_id)"
        with pytest.raises(Expr2Error) as raised:
            check(_parse(text), model, "kids", position="value", target="e")
        assert raised.value.code.code == "FLX-6109", text


def test_a_window_or_aggregate_that_carries_no_concatenation_is_fine() -> None:
    from flux_engine.expr2 import model_from_json

    model = model_from_json(_ENUM)
    check(_parse("MAX(CHILDREN.kids, s)"), model, "parents", position="value", target="k")
    check(
        _parse("FIRST_VALUE(s) OVER (PARTITION BY parent ORDER BY k_id)"),
        model,
        "kids",
        position="value",
        target="e",
    )
    # A count of concatenations is a number, and a string column compared with one is a bool.
    check(_parse("COUNT(CHILDREN.kids, s || 'x' = 'ax')"), model, "parents", position="value")
