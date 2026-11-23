# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The checker's report and the evaluator's contract, beyond what the vectors pin.

What a text reads (EXPR-SPEC-2 §10), where a NULL literal gets a type and
where it has none, the column-type table (RFC-data-rules.md §2.3) and
placeholders (§4), fitting a value to
its column at row time (``FLX-6112``), the evaluator's totality on rows of
the declared types, and the once-per-row reuse of aggregates and windows.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.expr2 import (
    Expr2Error,
    Kind,
    Read,
    Rows,
    Session,
    Side,
    check,
    column_type,
    fit,
    model_from_json,
    parse,
)

from .support import EXTENSION_VECTORS, load, models_of, rows_of

pytestmark = pytest.mark.unit

TELCO_DOCUMENT = load(EXTENSION_VECTORS)
TELCO = models_of(TELCO_DOCUMENT)["telco"]


def _check(text: str, expose: str, **options: Any) -> Any:
    return check(parse(text, side=Side.FLUX), TELCO, expose, **options)


def test_reads_name_every_column_and_how_it_is_reached() -> None:
    checked = _check(
        "SUM(CHILDREN.subscriptions, quantity * unit_price) > 0 AND group.segment.name = 'retail' "
        "AND name IS NOT NULL",
        "accounts",
        position="predicate",
    )
    assert checked.reads == tuple(
        sorted(
            {
                Read("children", "subscriptions", "quantity", ("subscriptions", "account")),
                Read("children", "subscriptions", "unit_price", ("subscriptions", "account")),
                Read("link", "segments", "name", ("group", "segment")),
                Read("row", "accounts", "name", ()),
            }
        )
    )
    window = _check(
        "LAG(plan_code) OVER (PARTITION BY account ORDER BY subscription_id)",
        "subscriptions",
        position="value",
    )
    assert {read.reach for read in window.reads} == {"siblings"}
    previous = _check("previous.price", "plans", position="value")
    assert previous.reads == (Read("previous", "plans", "price", ("previous",)),)


@pytest.mark.parametrize(
    ("text", "position"),
    [
        ("unit_price + NULL", "value"),
        ("NULL * unit_price", "value"),
        ("CASE WHEN active THEN unit_price ELSE NULL END", "value"),
        ("COALESCE(NULL, unit_price)", "value"),
        ("DAYS_BETWEEN(start_date, NULL)", "value"),
        ("unit_price IS DISTINCT FROM NULL", "predicate"),
        ("LAG(unit_price, 1, NULL) OVER (PARTITION BY account ORDER BY subscription_id)", "value"),
    ],
)
def test_a_null_literal_takes_the_type_of_a_typed_operand_beside_it(
    text: str, position: str
) -> None:
    checked = _check(text, "subscriptions", position=position)
    assert checked.type.kind is not None
    # A NULL operand adds nothing to a static scale, so a product keeps its other side's.
    if checked.type.kind is Kind.NUMBER and "DAYS_BETWEEN" not in text:
        assert checked.type.scale == 2


@pytest.mark.parametrize(
    ("text", "position"),
    [
        ("NULL", "predicate"),
        ("NULL", "value"),
        ("(NULL)", "value"),
        ("NOT NULL", "predicate"),
        ("active AND NULL", "predicate"),
        ("active OR (NULL)", "predicate"),
        ("CASE WHEN NULL THEN 1 ELSE 2 END", "value"),
        ("NULL IS NULL", "predicate"),
        ("NULL IS NOT NULL", "predicate"),
        ("NULL IS NOT DISTINCT FROM NULL", "predicate"),
        ("-NULL", "value"),
        ("NULL + NULL", "value"),
        ("(NULL + NULL) * unit_price", "value"),
        ("ROUND(NULL, 2)", "value"),
        ("COALESCE(NULL, NULL)", "value"),
        ("CASE WHEN active THEN NULL END", "value"),
        ("CASE WHEN active THEN NULL ELSE NULL END", "value"),
        ("DAYS_BETWEEN(NULL, NULL)", "value"),
        ("FIRST_VALUE(NULL) OVER (PARTITION BY account ORDER BY subscription_id)", "value"),
    ],
)
def test_a_null_literal_with_no_typed_operand_beside_it_is_refused(
    text: str, position: str
) -> None:
    # NULL never takes its type from its position (EXPR-SPEC-2 §4), as in
    # forge-cli's fluid-sql-vectors.json.
    with pytest.raises(Expr2Error) as raised:
        _check(text, "subscriptions", position=position)
    assert raised.value.code.code == "FLX-6104", raised.value


def test_a_null_count_filter_is_refused() -> None:
    with pytest.raises(Expr2Error) as raised:
        _check("COUNT(CHILDREN.subscriptions, NULL)", "accounts", position="value")
    assert raised.value.code.code == "FLX-6104"


def test_a_placeholder_is_exempt_from_the_enum_check() -> None:
    model = model_from_json(
        {
            "exposes": {
                "products": {
                    "columns": {
                        "code": {
                            "type": "string",
                            "enum": ["A1", "B2"],
                            "placeholders": {"values": ["UNKNOWN_CODE"], "prefixes": ["TBD_"]},
                        },
                        "price": {"type": "decimal(10,2)"},
                    }
                }
            }
        }
    )

    def checked(text: str) -> Any:
        return check(parse(text, side=Side.CORE), model, "products", position="predicate")

    for text in (
        "code = 'A1'",
        "code = 'TBD_0042'",
        "code IN ('B2', 'UNKNOWN_CODE', 'TBD_')",
        "'TBD_7' <> code",
    ):
        assert checked(text).type.kind is Kind.BOOL
    # Matching is exact and case-sensitive on the stored text (RFC §4).
    for text in ("code = 'tbd_1'", "code = 'UNKNOWN'", "code = 'XTBD_1'"):
        with pytest.raises(Expr2Error) as raised:
            checked(text)
        assert raised.value.code.code == "FLX-6109"
    # A placeholder is the contract's own text, so it is NFC-normalized like the text.
    composed = column_type(
        "string",
        enum=["A1"],
        placeholder_values=["Cafe\u0301"],
        placeholder_prefixes=["Pre\u0301_"],
    )
    assert composed.is_placeholder("Caf\u00e9") and composed.is_placeholder("Pr\u00e9_9")
    # So are the enum's levels, or a decomposed level would never match a literal.
    assert column_type("string", enum=["Cafe\u0301"]).enum == ("Caf\u00e9",)
    # column_type never raises: a level a direct caller gives as a non-string stays as given.
    assert column_type("text", enum=["a", 1]).enum == ("a", 1)  # type: ignore[list-item]
    # Only a string column takes placeholders.
    assert column_type("int", placeholder_values=["0"]).placeholder_values == ()
    with pytest.raises(ValueError):
        model_from_json(
            {"exposes": {"p": {"columns": {"c": {"type": "string", "placeholders": ["TBD"]}}}}}
        )
    with pytest.raises(ValueError):
        model_from_json(
            {
                "exposes": {
                    "p": {"columns": {"c": {"type": "string", "placeholders": {"prefixes": [""]}}}}
                }
            }
        )


def test_an_unknown_derive_target_is_refused() -> None:
    # Not a shared vector: fluid-sql-vectors.json's derive names a column of its model.
    with pytest.raises(Expr2Error) as raised:
        _check("unit_price", "subscriptions", position="value", target="nope")
    assert raised.value.code.code == "FLX-6113"


def test_bad_arguments_are_programming_errors() -> None:
    with pytest.raises(ValueError):
        _check("name", "accounts", position="neither")
    with pytest.raises(ValueError):
        _check("name", "nowhere")


@pytest.mark.parametrize(
    ("declared", "kind", "scale"),
    [
        ("DECIMAL(12, 2)", Kind.NUMBER, 2),
        ("numeric(5)", Kind.NUMBER, 0),
        ("numeric", Kind.NUMBER, None),
        ("int(11)", Kind.NUMBER, 0),
        ("double precision", Kind.NUMBER, None),
        ("VARCHAR(255)", Kind.STRING, None),
        ("timestamp_ltz", Kind.TIMESTAMPTZ, None),
        ("datetime2", Kind.TIMESTAMP, None),
        ("bit", Kind.BOOL, None),
        ("bit(1)", Kind.BOOL, None),
        ("bit(8)", None, None),
        ("boolean(1)", None, None),
        ("money", None, None),
        ("interval", None, None),
        ("decimal(2,5)", None, None),
        ("timestamp with time zone", Kind.TIMESTAMPTZ, None),
        ("TIMESTAMP  WITHOUT TIME ZONE", Kind.TIMESTAMP, None),
        ("time with time zone", None, None),
    ],
)
def test_column_types_follow_the_rfc_table(declared: str, kind: Any, scale: Any) -> None:
    found = column_type(declared)
    assert found.kind is kind
    if kind is Kind.NUMBER:
        assert found.scale == scale


def test_fit_writes_exact_values_and_refuses_what_does_not_fit() -> None:
    decimal = column_type("decimal(5,2)")
    assert fit(Fraction("123.40"), decimal, column_name="net") == Decimal("123.40")
    assert fit(None, decimal, column_name="net") is None
    smallint = column_type("smallint")
    assert fit(Fraction(32767), smallint, column_name="qty") == 32767
    for value, column in (
        (Fraction("1234.5"), decimal),
        (Fraction(1, 3), decimal),
        (Fraction(32768), smallint),
    ):
        with pytest.raises(Expr2Error) as raised:
            fit(value, column, column_name="c", row=7)
        assert raised.value.code.code == "FLX-6112"
        assert "row 7" in str(raised.value)
        assert "1234" not in str(raised.value) and "32768" not in str(raised.value)


def test_aggregates_and_windows_are_computed_once_per_row() -> None:
    rows = rows_of(TELCO_DOCUMENT, {"telco": TELCO}, "telco")
    calls: Dict[str, int] = {"children": 0, "siblings": 0}

    class Counting:
        def __init__(self, view: Any) -> None:
            self._view = view
            self.key = view.key

        def get(self, column: str) -> Any:
            return self._view.get(column)

        def follow(self, relationship: str) -> Any:
            found = self._view.follow(relationship)
            return None if found is None else Counting(found)

        def previous(self) -> Any:
            return self._view.previous()

        def children(self, expose: str, relationship: str) -> List[Any]:
            calls["children"] += 1
            return [Counting(row) for row in self._view.children(expose, relationship)]

        def siblings(self, relationship: str) -> List[Any]:
            calls["siblings"] += 1
            return [Counting(row) for row in self._view.siblings(relationship)]

    checked = _check(
        "COUNT(CHILDREN.subscriptions) + COUNT(CHILDREN.subscriptions)",
        "accounts",
        position="value",
    )
    session = Session()
    values = [checked.evaluate(Counting(view), session) for view in rows.rows("accounts")]
    assert values == [Fraction(6), Fraction(2), Fraction(0), Fraction(0)]
    assert calls["children"] == 8  # one per aggregate node and row, never per use of the result
    again = [checked.evaluate(Counting(view), session) for view in rows.rows("accounts")]
    assert again == values and calls["children"] == 8
    window = _check(
        "ROW_NUMBER() OVER (PARTITION BY account ORDER BY subscription_id)",
        "subscriptions",
        position="value",
    )
    session = Session()
    [window.evaluate(Counting(view), session) for view in rows.rows("subscriptions")]
    assert calls["siblings"] == 3  # one per partition: a1, a2 and the NULL partition


MODEL = model_from_json(
    {
        "exposes": {
            "t": {
                "columns": {
                    "n": {"type": "decimal(6,2)"},
                    "m": {"type": "int"},
                    "s": {"type": "string"},
                    "d": {"type": "date"},
                    "b": {"type": "boolean"},
                },
                "key": ["m"],
            }
        }
    }
)
TOTAL_TEXTS = [
    "n / m",
    "ROUND(n / (m - m), 2)",
    "CASE WHEN b THEN n WHEN n > m THEN m END",
    "COALESCE(n, m, 0) * -1",
    "s IN ('a', 'b') OR NOT b OR d BETWEEN DATE '2020-01-01' AND DATE '2030-12-31'",
    "YEARS_BETWEEN(d, DATE '2026-11-16') + MONTHS_BETWEEN(DATE '0001-01-01', d)",
    "n IS DISTINCT FROM m AND s IS NOT NULL",
]
VALUES = st.fixed_dictionaries(
    {
        "n": st.one_of(st.none(), st.decimals(min_value=-9999, max_value=9999, places=2)),
        "m": st.one_of(st.none(), st.integers(-5, 5)),
        "s": st.one_of(st.none(), st.text(max_size=3)),
        "d": st.one_of(st.none(), st.dates()),
        "b": st.one_of(st.none(), st.booleans()),
    }
)


@given(st.lists(VALUES, min_size=1, max_size=4))
def test_evaluation_is_total_on_rows_of_the_declared_types(table: List[Dict[str, Any]]) -> None:
    rows = Rows(MODEL, {"t": table})
    for text in TOTAL_TEXTS:
        checked = check(parse(text, side=Side.CORE), MODEL, "t", position="value")
        for view in rows.rows("t"):
            value = checked.evaluate(view)
            assert value is None or isinstance(value, (Fraction, bool, str, date))


def test_fit_keeps_every_digit_of_a_wide_decimal() -> None:
    wide = column_type("decimal(38,2)")
    value = Fraction("123456789012345678901234567890123456.78")
    assert fit(value, wide, column_name="amount") == Decimal(
        "123456789012345678901234567890123456.78"
    )
    # Unary minus on a Decimal rounds to the context too, so the expectation is a literal.
    assert fit(-value, wide, column_name="amount") == Decimal(
        "-123456789012345678901234567890123456.78"
    )


def test_a_view_whose_siblings_omit_the_row_is_a_contract_error() -> None:
    rows = rows_of(TELCO_DOCUMENT, {"telco": TELCO}, "telco")
    view = rows.row("subscriptions", 0)

    class Broken:
        key = view.key

        def get(self, column: str) -> Any:
            return view.get(column)

        def siblings(self, relationship: str) -> List[Any]:
            return [row for row in view.siblings(relationship) if row.key != view.key]

    checked = _check(
        "ROW_NUMBER() OVER (PARTITION BY account ORDER BY subscription_id)",
        "subscriptions",
        position="value",
    )
    with pytest.raises(LookupError) as raised:
        checked.evaluate(Broken())  # type: ignore[arg-type]
    assert "sub1" not in str(raised.value)


def test_a_text_whose_canonical_form_is_too_long_is_refused() -> None:
    model = model_from_json({"exposes": {"t": {"columns": {"n": {"type": "int"}}}}})
    text = "n IN (" + ",".join(["1"] * 2040) + ")"
    assert len(text) <= 4096
    with pytest.raises(Expr2Error) as raised:
        check(parse(text, side=Side.CORE), model, "t", position="predicate")
    assert raised.value.code.code == "FLX-6103"
    fits = "n IN (" + ",".join(["1"] * 1300) + ")"
    canonical = check(parse(fits, side=Side.CORE), model, "t", position="predicate").canonical
    assert parse(canonical, side=Side.CORE).tree == parse(fits, side=Side.CORE).tree


ROOTS_MODEL = model_from_json(
    {
        "exposes": {
            "e": {
                "columns": {
                    "id": {"type": "string"},
                    "previous": {"type": "int"},
                    "x": {"type": "int"},
                },
                "key": ["id"],
                "versions": {"entity": ["id"], "start": "x"},
            },
            "f": {
                "columns": {"id": {"type": "string"}, "CHILDREN": {"type": "int"}},
                "key": ["id"],
            },
            "c": {
                "columns": {"cid": {"type": "string"}, "f_id": {"type": "string"}},
                "key": ["cid"],
                "links": {"f": {"to": "f", "fromColumns": ["f_id"], "toColumns": ["id"]}},
            },
        }
    }
)


@pytest.mark.parametrize(
    ("expose", "text"),
    [
        ("e", "PREVIOUS.x = 1"),
        ("e", "previous.x = 1"),
        ("f", "COUNT(children.c) = 1"),
        ("f", "COUNT(CHILDREN.c) = 1"),
    ],
)
def test_a_root_conflicts_with_a_name_in_any_case(expose: str, text: str) -> None:
    with pytest.raises(Expr2Error) as raised:
        check(parse(text, side=Side.FLUX), ROOTS_MODEL, expose, position="predicate")
    assert raised.value.code.code == "FLX-6105"


def test_relationships_to_one_target_never_share_an_index() -> None:
    model = model_from_json(
        {
            "exposes": {
                "t": {
                    "columns": {
                        "a,b": {"type": "int"},
                        "a": {"type": "int"},
                        "b": {"type": "int"},
                        "lbl": {"type": "string"},
                    }
                },
                "s": {
                    "columns": {"x": {"type": "int"}, "y": {"type": "int"}, "z": {"type": "int"}},
                    "links": {
                        "one": {"to": "t", "fromColumns": ["x"], "toColumns": ["a,b"]},
                        "two": {"to": "t", "fromColumns": ["y", "z"], "toColumns": ["a", "b"]},
                    },
                },
            }
        }
    )
    rows = Rows(
        model,
        {
            "t": [
                {"a,b": 2, "a": 1, "b": 1, "lbl": "first"},
                {"a,b": 3, "a": 11, "b": 21, "lbl": "second"},
            ],
            "s": [{"x": 2, "y": 11, "z": 21}],
        },
    )
    view = rows.row("s", 0)
    one = check(parse("one.lbl", side=Side.CORE), model, "s", position="value")
    two = check(parse("two.lbl", side=Side.CORE), model, "s", position="value")
    assert (one.evaluate(view), two.evaluate(view)) == ("first", "second")


def test_an_aggregate_is_never_nested_over_an_enclosing_collection() -> None:
    model = model_from_json(
        {
            "exposes": {
                "p": {"columns": {"id": {"type": "string"}}, "key": ["id"]},
                "a": {
                    "columns": {
                        "aid": {"type": "string"},
                        "p_id": {"type": "string"},
                        "b_id": {"type": "string"},
                    },
                    "key": ["aid"],
                    "links": {
                        "p": {"to": "p", "fromColumns": ["p_id"], "toColumns": ["id"]},
                        "b": {"to": "b", "fromColumns": ["b_id"], "toColumns": ["bid"]},
                    },
                },
                "b": {
                    "columns": {"bid": {"type": "string"}, "a_id": {"type": "string"}},
                    "key": ["bid"],
                    "links": {"a": {"to": "a", "fromColumns": ["a_id"], "toColumns": ["aid"]}},
                },
            }
        }
    )
    # From a: CHILDREN.b is (b, via a), inside it CHILDREN.a is (a, via b), and inside
    # that CHILDREN.b is (b, via a) again, an aggregate over an enclosing collection.
    nested = "SUM(CHILDREN.b, SUM(CHILDREN.a, COUNT(CHILDREN.b)))"
    with pytest.raises(Expr2Error) as raised:
        check(parse(nested, side=Side.FLUX), model, "a", position="value")
    assert raised.value.code.code == "FLX-6107"
    # From p the three collections are (a, via p), (b, via a) and (a, via b): all different.
    distinct = "SUM(CHILDREN.a, SUM(CHILDREN.b, COUNT(CHILDREN.a)))"
    check(parse(distinct, side=Side.FLUX), model, "p", position="value")


def test_a_numpy_float_reads_as_its_shortest_decimal() -> None:
    numpy = pytest.importorskip("numpy")
    from flux_engine.expr2.evaluate import decoder

    assert decoder(column_type("double"))(numpy.float64(0.1)) == Fraction(1, 10)


@pytest.mark.parametrize(
    "declared", ["decimal(1001,2)", "decimal(" + "9" * 5000 + ")", "numeric(10," + "9" * 5000 + ")"]
)
def test_a_decimal_wider_than_any_engine_has_no_type(declared: str) -> None:
    assert column_type(declared).kind is None


@pytest.mark.parametrize(
    "declared, precision, scale",
    [
        ("numeric(000012, 00002)", 12, 2),
        ("decimal(" + "0" * 5000 + "12, " + "0" * 5000 + "2)", 12, 2),
        ("decimal(" + "0" * 5000 + "1000)", 1000, 0),
    ],
    ids=["padded", "padded-past-int-digit-limit", "padded-widest"],
)
def test_leading_zeros_read_by_value(declared: str, precision: int, scale: int) -> None:
    # SQL reads p and s as unsigned integers, so the zeros never count toward the
    # four-digit cap, and int() never sees a run longer than its digit limit.
    read = column_type(declared)
    assert (read.kind, read.precision, read.scale) == (Kind.NUMBER, precision, scale)


def test_fit_at_the_widest_decimal_is_cheap() -> None:
    widest = column_type("decimal(1000,500)")
    assert widest.kind is Kind.NUMBER
    assert fit(Fraction(1, 4), widest, column_name="x") == Decimal("0.25")


@pytest.mark.parametrize(
    "blob",
    [
        [],
        {"exposes": {"a": 5}},
        {"exposes": {"a": {"columns": {"x": {}}}}},
        {"exposes": {"a": {"columns": {"x": {"type": 5}}}}},
        {"exposes": {"a": {"columns": {"x": {"type": "text", "enum": 5}}}}},
        {"exposes": {"a": {"links": {"l": 3}}}},
        {"exposes": {"a": {"links": {"l": {"to": "a"}}}}},
        {"exposes": {"a": {"links": {"l": {"to": "a", "fromColumns": ["x"], "toColumns": []}}}}},
        {"exposes": {"a": {"versions": {}}}},
        {"exposes": {"a": {"versions": {"entity": ["x"], "start": 5}}}},
        {"exposes": {"a": {"links": {"l": {"to": "b", "fromColumns": ["x"], "toColumns": ["y"]}}}}},
    ],
)
def test_a_malformed_model_fragment_is_a_value_error(blob: Any) -> None:
    with pytest.raises(ValueError):
        model_from_json(blob)
