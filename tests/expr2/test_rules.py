# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What flux-expr/2 decides about a rule from its texts (EXPR-SPEC-2 §1, §5, §6, §12).

The ``language`` key (``FLX-6101``, ``FLX-6102``), ids (``FLX-6201``), a
FLUX-side rule the contract can carry (``FLX-6209``), and the reading of a
contract ``derive`` with ``aggregate`` as the aggregate §5 maps it to, which
must give the same values as the same summary written in a FLUX-side text.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict

import pytest

from flux_engine.expr2 import (
    Expr2Error,
    Side,
    aggregate_tree,
    check,
    check_flux_side_rule,
    check_language,
    check_rule_ids,
    is_contract_aggregate,
    parse,
    uses_extension,
)

from .support import EXTENSION_VECTORS, load, models_of, rows_of

pytestmark = pytest.mark.unit

DOCUMENT = load(EXTENSION_VECTORS)
TELCO = models_of(DOCUMENT)["telco"]


def _code(call: Any) -> str:
    with pytest.raises(Expr2Error) as raised:
        call()
    return raised.value.code.code


def test_language_is_required_and_closed() -> None:
    assert _code(lambda: check_language({}, where="rule r")) == "FLX-6101"
    assert _code(lambda: check_language({"language": "flux-expr/1"}, where="rule r")) == "FLX-6102"
    check_language({"language": "flux-expr/2"}, where="rule r")


@pytest.mark.parametrize("ids", [[None], [""], ["1st"], ["a-b"], ["é"], ["a", "a"]])
def test_ids_are_present_identifiers_and_unique(ids: Any) -> None:
    assert _code(lambda: check_rule_ids(ids, where="contract c")) == "FLX-6201"


def test_ids_may_repeat_across_contracts() -> None:
    check_rule_ids(["gross_from_net", "due_after_issue"], where="contract a")
    check_rule_ids(["gross_from_net"], where="contract b")


def _rule(**entry: Any) -> Dict[str, Any]:
    return {"id": "r1", "language": "flux-expr/2", **entry}


def test_a_flux_side_rule_with_an_extension_is_checked() -> None:
    rule = check_flux_side_rule(
        _rule(
            derive="active",
            expr="ROW_NUMBER() OVER (PARTITION BY account ORDER BY start_date, subscription_id) = 1",
        ),
        TELCO,
        "subscriptions",
    )
    assert rule.derive == "active" and rule.when is None
    assert rule.value.canonical.startswith("ROW_NUMBER() OVER (PARTITION BY account")


def test_a_flux_side_rule_the_contract_can_carry_is_refused() -> None:
    assert (
        _code(lambda: check_flux_side_rule(_rule(check="quantity > 0"), TELCO, "subscriptions"))
        == "FLX-6209"
    )
    # The row. and link. spellings are not extensions.
    spelled = _rule(check="row.quantity > 0 AND link.account.name = 'Ada'")
    assert _code(lambda: check_flux_side_rule(spelled, TELCO, "subscriptions")) == "FLX-6209"
    for expr in (
        "COUNT(CHILDREN.subscriptions)",
        "COUNT(CHILDREN.subscriptions.account, active)",
        "SUM(CHILDREN.subscriptions, quantity)",
        "MAX(CHILDREN.subscriptions, CASE WHEN active THEN start_date END)",
        "COALESCE(SUM(CHILDREN.subscriptions, quantity), 0)",
    ):
        assert is_contract_aggregate(parse(expr, side=Side.FLUX).tree), expr
    assert (
        _code(
            lambda: check_flux_side_rule(
                _rule(derive="opened", expr="MIN(CHILDREN.subscriptions, start_date)"),
                TELCO,
                "accounts",
            )
        )
        == "FLX-6209"
    )


@pytest.mark.parametrize(
    "expr",
    [
        "AVG(CHILDREN.subscriptions, unit_price)",
        "SUM(CHILDREN.subscriptions, quantity * unit_price)",
        "SUM(CHILDREN.subscriptions, account.name = 'x')",
        "COUNT(CHILDREN.subscriptions, previous.x = 1)",
        "COUNT(CHILDREN.subscriptions) + 1",
        "COALESCE(SUM(CHILDREN.subscriptions, quantity), quantity)",
    ],
)
def test_summaries_a_contract_cannot_write_are_not_contract_aggregates(expr: str) -> None:
    assert not is_contract_aggregate(parse(expr, side=Side.FLUX).tree)


def test_a_when_that_uses_an_extension_makes_the_rule_flux_side() -> None:
    checked = check_flux_side_rule(
        _rule(check="price > 0", when="previous.price IS NOT NULL"), TELCO, "plans"
    )
    assert checked.when is not None and uses_extension(checked.when.parsed.tree)


def test_a_malformed_entry_is_the_schemas_problem() -> None:
    with pytest.raises(ValueError):
        check_flux_side_rule(_rule(check="price > 0", derive="price", expr="price"), TELCO, "plans")
    with pytest.raises(ValueError):
        check_flux_side_rule(_rule(derive="price"), TELCO, "plans")


@pytest.mark.parametrize(
    ("aggregate", "flux_text"),
    [
        (
            {"function": "count", "from": "subscriptions", "via": "account"},
            "COUNT(CHILDREN.subscriptions.account)",
        ),
        (
            {"function": "count", "from": "subscriptions", "via": "account", "filter": "active"},
            "COUNT(CHILDREN.subscriptions.account, active)",
        ),
        (
            {
                "function": "sum",
                "column": "unit_price",
                "from": "subscriptions",
                "via": "account",
                "filter": "plan_code IN ('A', 'B')",
                "ifNone": 0,
            },
            "COALESCE(SUM(CHILDREN.subscriptions.account, CASE WHEN plan_code IN ('A', 'B') THEN unit_price END), 0)",
        ),
        (
            {
                "function": "min",
                "column": "start_date",
                "from": "subscriptions",
                "via": "account",
                "ifNone": date(2000, 1, 1),
            },
            "COALESCE(MIN(CHILDREN.subscriptions.account, start_date), DATE '2000-01-01')",
        ),
        (
            {
                "function": "max",
                "column": "quantity",
                "from": "subscriptions",
                "via": "account",
                "ifNone": -1.5,
            },
            "COALESCE(MAX(CHILDREN.subscriptions.account, quantity), -1.5)",
        ),
    ],
)
def test_a_contract_aggregate_reads_as_its_flux_side_spelling(
    aggregate: Dict[str, Any], flux_text: str
) -> None:
    parsed = aggregate_tree(aggregate)
    assert is_contract_aggregate(parsed.tree)
    expected = parse(flux_text, side=Side.FLUX)
    assert parsed.tree == expected.tree
    contract = check(parsed, TELCO, "accounts", position="value")
    written = check(expected, TELCO, "accounts", position="value")
    assert contract.canonical == written.canonical
    rows = rows_of(DOCUMENT, {"telco": TELCO}, "telco")
    for view in rows.rows("accounts"):
        assert contract.evaluate(view) == written.evaluate(view)


def test_a_contract_aggregate_with_a_bad_function_is_refused() -> None:
    with pytest.raises(ValueError):
        aggregate_tree(
            {"function": "avg", "column": "x", "from": "subscriptions", "via": "account"}
        )


def test_a_contract_aggregate_gets_the_root_check_its_canonical_text_needs() -> None:
    # The canonical text writes CHILDREN.kids.up, which on this expose would read as
    # both a root and a column, so the aggregate is refused like the written one.
    from flux_engine.expr2 import model_from_json

    model = model_from_json(
        {
            "exposes": {
                "parents": {
                    "columns": {"id": {"type": "string"}, "CHILDREN": {"type": "int"}},
                    "key": ["id"],
                },
                "kids": {
                    "columns": {"kid": {"type": "string"}, "parent": {"type": "string"}},
                    "key": ["kid"],
                    "links": {
                        "up": {"to": "parents", "fromColumns": ["parent"], "toColumns": ["id"]}
                    },
                },
            }
        }
    )
    parsed = aggregate_tree({"function": "count", "from": "kids", "via": "up"})
    assert _code(lambda: check(parsed, model, "parents", position="value")) == "FLX-6105"
    written = parse("COUNT(CHILDREN.kids.up)", side=Side.FLUX)
    assert _code(lambda: check(written, model, "parents", position="value")) == "FLX-6105"


def test_a_contract_aggregate_whose_filter_is_too_deep_is_refused() -> None:
    deep = " AND ".join(["active"] * 63)
    assert parse(deep, side=Side.CORE).depth == 63
    aggregate = {
        "function": "sum",
        "column": "unit_price",
        "from": "subscriptions",
        "via": "account",
    }
    assert _code(lambda: aggregate_tree({**aggregate, "filter": deep})) == "FLX-6103"
    shallow = " AND ".join(["active"] * 60)
    assert aggregate_tree({**aggregate, "filter": shallow}).depth <= 64


def _negations(k: int) -> str:
    return "NOT " * k + "quantity IN (-1)"


def _nested_ors(k: int) -> str:
    # Each step needs its parentheses in the canonical text: NOT binds tighter than OR.
    text = "active"
    for _ in range(k):
        text = f"NOT ({text} OR active)"
    return text


@pytest.mark.parametrize("function", ["count", "sum"])
@pytest.mark.parametrize(
    ("build", "sizes"), [(_negations, range(56, 64)), (_nested_ors, range(17, 23))]
)
def test_a_contract_aggregate_is_measured_as_its_canonical_text(
    function: str, build: Any, sizes: Any
) -> None:
    # The limits are the canonical text's, which the plan stores: an IN item -1 is
    # one level and every pair of parentheses one more, so an aggregate is accepted
    # exactly when its canonical text parses, at the depth that text has.
    aggregate = {"function": function, "from": "subscriptions", "via": "account"}
    if function == "sum":
        aggregate["column"] = "unit_price"
    accepted = refused = 0
    for size in sizes:
        built = {**aggregate, "filter": build(size)}
        try:
            parsed = aggregate_tree(built)
        except Expr2Error as error:
            assert error.code.code == "FLX-6103"
            refused += 1
            continue
        accepted += 1
        assert parse(parsed.text, side=Side.FLUX).depth == parsed.depth <= 64
    assert accepted and refused


def test_a_contract_aggregate_is_the_tree_its_canonical_text_parses_to() -> None:
    # The contract's names and literals are NFC-normalized like every text (RFC
    # §2.3), so a decomposed spelling reads as the column the model names.
    from flux_engine.expr2 import model_from_json

    decomposed, composed = "cafe" + chr(0x301), "caf" + chr(0xE9)
    model = model_from_json(
        {
            "exposes": {
                "parents": {"columns": {"id": {"type": "string"}}, "key": ["id"]},
                "kids": {
                    "columns": {
                        "kid": {"type": "string"},
                        "parent": {"type": "string"},
                        composed: {"type": "string"},
                    },
                    "key": ["kid"],
                    "links": {
                        "up": {"to": "parents", "fromColumns": ["parent"], "toColumns": ["id"]}
                    },
                },
            }
        }
    )
    parsed = aggregate_tree(
        {
            "function": "min",
            "from": "kids",
            "via": "up",
            "column": decomposed,
            "filter": f"\"{decomposed}\" <> 'x'",
            "ifNone": decomposed,
        }
    )
    assert parsed.tree == parse(parsed.text, side=Side.FLUX).tree
    checked = check(parsed, model, "parents", position="value")
    assert checked.canonical == parsed.text
    assert composed in parsed.text and decomposed not in parsed.text


def test_an_if_none_keeps_every_digit() -> None:
    aggregate = {
        "function": "sum",
        "column": "unit_price",
        "from": "subscriptions",
        "via": "account",
        "ifNone": 12345678901234567890123456789012345,
    }
    assert aggregate_tree(aggregate).text.endswith(", 12345678901234567890123456789012345)")


@pytest.mark.parametrize(
    "aggregate",
    [
        None,
        "sum",
        [],
        {"function": "sum", "column": "unit_price", "via": "account"},
        {"function": "sum", "column": 5, "from": "subscriptions", "via": "account"},
        {"function": "count", "from": None, "via": "account"},
        {"function": "count", "from": "subscriptions", "via": "account", "filter": 5},
        {
            "function": "min",
            "column": "start_date",
            "from": "subscriptions",
            "via": "account",
            "ifNone": __import__("datetime").datetime(2020, 1, 1),
        },
    ],
)
def test_a_malformed_contract_aggregate_is_a_value_error(aggregate: Dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        aggregate_tree(aggregate)
