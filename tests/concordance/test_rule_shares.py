# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Every contract rule, checked on rows, as the share that holds (satisfied, total)."""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List

import pytest

from flux_engine.concordance.rule_shares import (
    RuleSource,
    rule_shares,
    stored_rows,
    tables_needed,
    within_band,
)
from flux_engine.concordance.shares import Holds
from flux_engine.generator.rules import model_from_contract

pytestmark = pytest.mark.unit


def _contract(invariants: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "t.product",
        "exposes": [
            {
                "exposeId": "t",
                "contract": {
                    "schema": [
                        {"name": "a", "type": "INT"},
                        {"name": "b", "type": "INT"},
                        {"name": "grp", "type": "STRING"},
                        {"name": "v", "type": "STRING"},
                        {"name": "d", "type": "DATE"},
                        {"name": "amount", "type": "DECIMAL(7,2)"},
                    ],
                    "invariants": invariants,
                },
            }
        ],
    }


NAMES = ["a", "b", "grp", "v", "d", "amount"]


def _shares(
    invariants: List[Dict[str, Any]], rows: List[List[Any]], **source: Any
) -> Dict[str, Any]:
    contract = _contract(invariants)
    model = model_from_contract(contract)
    tables = {"t": stored_rows(model, "t", NAMES, rows)}
    found = rule_shares(contract, "t", tables, RuleSource(**source))
    return {share.rule_id: share for share in found}


def _row(
    a: int, b: int = 0, grp: Any = "x", v: Any = "p", d: str = "2026-01-01", amount: str = "1.00"
):
    return [a, b, grp, v, d, amount]


def test_a_check_is_the_share_of_rows_that_satisfy_it() -> None:
    found = _shares([{"id": "pos", "check": "a >= 0"}], [_row(1), _row(-1), _row(5), _row(-3)])
    assert (found["pos"].holds.satisfied, found["pos"].holds.total) == (2, 4)
    assert found["pos"].holds.share == Fraction(1, 2)
    assert found["pos"].form == "check" and found["pos"].origin == "contract"


def test_a_when_limits_the_rows_a_rule_covers() -> None:
    rows = [_row(1, grp="x"), _row(50, grp="y"), _row(60, grp="y"), _row(2, grp="x")]
    found = _shares([{"id": "small", "check": "a < 10", "when": "grp = 'x'"}], rows)
    assert (found["small"].holds.satisfied, found["small"].holds.total) == (2, 2)


def test_a_derive_holds_where_the_column_is_the_expression() -> None:
    found = _shares(
        [{"id": "dbl", "derive": "b", "expr": "a * 2"}], [_row(1, 2), _row(2, 4), _row(3, 7)]
    )
    assert (found["dbl"].holds.satisfied, found["dbl"].holds.total) == (2, 3)
    assert found["dbl"].enforced and found["dbl"].met is False


def test_a_derive_over_a_decimal_and_a_date_reads_the_stored_values() -> None:
    rows = [_row(1, d="2026-01-31", amount="10.50"), _row(2, d="2026-02-01", amount="2.00")]
    found = _shares(
        [
            {"id": "tax", "derive": "amount", "expr": "ROUND(a * 10.5, 2)"},
            {"id": "early", "check": "d < DATE '2026-02-01'"},
        ],
        rows,
    )
    assert (found["tax"].holds.satisfied, found["tax"].holds.total) == (1, 2)
    assert (found["early"].holds.satisfied, found["early"].holds.total) == (1, 2)


def test_a_dependency_counts_the_rows_that_hold_their_key_s_modal_value() -> None:
    rows = [
        _row(1, grp="k1", v="p"),
        _row(2, grp="k1", v="p"),
        _row(3, grp="k1", v="q"),
        _row(4, grp="k2", v="r"),
        _row(5, grp=None, v="z"),
    ]
    found = _shares([{"id": "fd", "dependency": {"from": ["grp"], "to": ["v"]}}], rows)
    # k1 holds p twice and q once, k2 holds r; the row with a NULL key reaches no key.
    assert (found["fd"].holds.satisfied, found["fd"].holds.total) == (3, 4)
    assert found["fd"].form == "dependency" and found["fd"].enforced


def test_a_dependency_tie_goes_to_the_first_value_met() -> None:
    rows = [_row(1, grp="k", v="p"), _row(2, grp="k", v="q")]
    found = _shares([{"id": "fd", "dependency": {"from": ["grp"], "to": ["v"]}}], rows)
    assert (found["fd"].holds.satisfied, found["fd"].holds.total) == (1, 2)


def test_an_unknown_text_does_not_satisfy_a_check() -> None:
    found = _shares(
        [{"id": "pos", "check": "a >= 0"}], [_row(1), [None, 0, "x", "p", "2026-01-01", "1.00"]]
    )
    assert (found["pos"].holds.satisfied, found["pos"].holds.total) == (1, 2)


def test_a_check_with_no_generates_setting_promises_nothing() -> None:
    found = _shares([{"id": "pos", "check": "a >= 0"}], [_row(-1)])
    assert found["pos"].enforced is False and found["pos"].met is None
    enforced = _shares(
        [{"id": "pos", "check": "a >= 0"}],
        [_row(-1)],
        extension={"invariants": {"pos": {"generates": "b"}}},
    )
    assert enforced["pos"].enforced is True and enforced["pos"].met is False


def test_an_emit_whose_rows_do_not_come_from_the_generator_enforces_nothing() -> None:
    found = _shares([{"id": "dbl", "derive": "b", "expr": "a * 2"}], [_row(1, 9)], generated=False)
    assert found["dbl"].enforced is False and found["dbl"].met is None


def test_a_band_states_how_many_violating_rows_a_rule_allows() -> None:
    rows = [_row(1), _row(-1), _row(2), _row(3)]
    allowed = _shares([{"id": "pos", "check": "a >= 0", "mustBeLessOrEqualTo": 1}], rows)
    assert allowed["pos"].met is True
    refused = _shares([{"id": "pos", "check": "a >= 0", "mustBe": 0}], rows)
    assert refused["pos"].met is False
    percent = _shares(
        [{"id": "pos", "check": "a >= 0", "mustBeLessThan": 30, "unit": "percent"}], rows
    )
    assert percent["pos"].met is True  # 25 % violate, under 30 %
    tight = _shares(
        [{"id": "pos", "check": "a >= 0", "mustBeLessThan": 25, "unit": "percent"}], rows
    )
    assert tight["pos"].met is False


@pytest.mark.parametrize(
    ("operator", "values", "unit", "violating", "total", "within"),
    [
        ("mustBe", ["0"], "rows", 0, 10, True),
        ("mustBe", ["0"], "rows", 1, 10, False),
        ("mustBeGreaterThan", ["0"], "rows", 0, 10, False),
        ("mustBeGreaterOrEqualTo", ["2"], "rows", 2, 10, True),
        ("mustBeBetween", ["1", "3"], "rows", 4, 10, False),
        ("mustBeBetween", ["10", "20"], "percent", 1, 10, True),
        ("mustBe", ["0"], "percent", 0, 0, True),
    ],
)
def test_every_band_operator_reads_the_violating_rows(
    operator: str, values: List[str], unit: str, violating: int, total: int, within: bool
) -> None:
    band = {"operator": operator, "values": values, "unit": unit}
    assert within_band(band, Holds(total - violating, total)) is within


def test_a_rule_that_covers_no_row_holds_vacuously() -> None:
    found = _shares([{"id": "pos", "check": "a >= 0", "when": "grp = 'nope'"}], [_row(-1)])
    assert found["pos"].holds == Holds(0, 0) and found["pos"].holds.whole
    assert found["pos"].holds.share is None and found["pos"].holds.text() == "0 of 0"


def test_the_bundle_s_own_rules_are_checked_too() -> None:
    extension = {
        "rules": [
            {"id": "mine", "language": "flux-expr/3", "check": "EXTRACT(YEAR FROM d) = 2026"},
            {
                "id": "twice",
                "language": "flux-expr/3",
                "derive": "b",
                "expr": "a * 2 + EXTRACT(DAY FROM d) - 1",
            },
        ]
    }
    rows = [_row(1, 2, d="2026-03-01"), _row(5, 0, d="2025-03-01")]
    found = _shares([], rows, extension=extension)
    assert found["mine"].origin == "bundle" and found["mine"].holds == Holds(1, 2)
    assert found["twice"].holds == Holds(1, 2) and found["twice"].enforced


def test_the_rows_a_rule_reads_are_the_exposes_its_text_names() -> None:
    assert tables_needed(_contract([{"id": "pos", "check": "a >= 0"}]), "t") == {"t"}


def test_a_cell_that_is_not_the_type_s_spelling_is_a_problem_not_a_value() -> None:
    model = model_from_contract(_contract([]))
    with pytest.raises((TypeError, ValueError)) as refused:
        stored_rows(model, "t", NAMES, [["secret-1", 0, "x", "p", "2026-01-01", "1.00"]])
    assert "secret" not in str(refused.value)
    with pytest.raises(ValueError):
        stored_rows(model, "t", NAMES, [[1, 2]])


def test_a_share_is_spelled_down_so_a_miss_never_reads_as_whole() -> None:
    assert Holds(99_999, 100_000).text() == "99999 of 100000 (99.99%)"
    assert Holds(1, 3).to_document() == {"satisfied": 1, "total": 3, "share": "1/3"}
    assert Holds(1, 4).to_document()["share"] == "0.25"


def test_a_path_needs_the_rows_of_every_expose_it_crosses_and_the_bundles_rules_are_read() -> None:
    from tests.generator.test_rules_over_links import SEGMENT, chain_contract

    contract = chain_contract()
    assert tables_needed(contract, "invoice") == {"invoice"}
    # A two-hop path of a FLUX-side rule crosses the account on its way to the customer.
    assert tables_needed(contract, "invoice", {"rules": [SEGMENT]}) == {
        "invoice",
        "account",
        "customer",
    }


def test_a_string_sentinel_is_compared_in_nfc_with_the_cells_the_writer_wrote() -> None:
    from flux_engine.concordance.rule_shares import _sentinel_share

    class _View:
        def follow(self, relationship: str) -> None:
            return None

        def get(self, column: str) -> str:
            return "café"

    class _Rows:
        def rows(self, expose_id: str) -> List[_View]:
            return [_View(), _View()]

    model = model_from_contract(_contract([]))
    holds = _sentinel_share("v", "café", ("rel",), _Rows(), "t", model)  # type: ignore[arg-type]
    assert (holds.satisfied, holds.total) == (2, 2)
