# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A histogram column drawn ``given`` a categorical column.

``given`` names the column, and ``binWeightsGiven`` holds one ``binWeights``
list per level of it: P(bin | level), as a grouped histogram's (group, bin)
counts state it. The properties pinned here:

* **Conditioning.** Each row's bin is drawn with the weights of the level
  its given column drew in that row. Degenerate weights make that exact.
* **Addressing survives.** Same root, same rows; column-first equals
  row-first; slices join into the whole table, with the conditional column
  declared before the column it is given.
* **Only the conditional column moves.** The given column draws exactly
  what it draws in a plan with no conditioning at all.
* **The digest covers it, and nothing else moves.** A plan without
  ``given`` documents no new key; a plan with it has a different
  ``generatorDigest``.
* **Refusals.** Every declaration the generator cannot draw is refused at
  plan time with ``FLX-3402``.
"""

from __future__ import annotations

import unicodedata
from decimal import Decimal
from typing import Any, Dict, List, Mapping

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator.families import ColumnType
from flux_engine.generator.paths import GeneratorStreams
from flux_engine.generator.rows import (
    GeneratorPlan,
    build_plan,
    generate_column,
    generate_row_slice,
    generate_rows,
)
from flux_engine.seam.contract import CompiledExpose
from tests.generator.conftest import SIM_ID, H, expose_from_schema, fidelity, root_for

pytestmark = pytest.mark.unit

# ``amount`` is declared before ``market`` on purpose: the column read from
# must be drawn first however the expose orders its columns.
SCHEMA: List[Dict[str, Any]] = [
    {"name": "amount", "type": "DECIMAL(18,4)", "required": True},
    {"name": "market", "type": "STRING", "required": True},
]

MARKET: Dict[str, Any] = {"levels": {"DE": "0.6", "FR": "0.4"}}

#: DE rows draw only bin 0, FR rows only bin 1.
GIVEN: Dict[str, Any] = {
    "bins": ["0", "10", "20"],
    "given": "market",
    "binWeightsGiven": {"DE": ["1", "0"], "FR": ["0", "1"]},
}


@pytest.fixture
def expose() -> CompiledExpose:
    return expose_from_schema(SCHEMA)


def _plan(
    expose: CompiledExpose,
    amount: Mapping[str, Any] = GIVEN,
    market: Mapping[str, Any] = MARKET,
    *,
    rows: int = 200,
) -> GeneratorPlan:
    return build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=rows,
        fidelity=fidelity({"amount": dict(amount), "market": dict(market)}),
    )


def _refused(expose: CompiledExpose, amount: Mapping[str, Any], **market: Any) -> str:
    with pytest.raises(FluxError) as error:
        _plan(expose, amount, {**MARKET, **market} if market else MARKET)
    assert error.value.code.code == "FLX-3402"
    return str(error.value)


def test_each_row_draws_with_the_weights_of_its_own_level(expose: CompiledExpose) -> None:
    rows = generate_rows(_plan(expose), root=root_for())
    markets = {market for _, market in rows}
    assert markets == {"DE", "FR"}, "both levels must be drawn for the check to mean anything"
    for amount, market in rows:
        if market == "DE":
            assert Decimal(0) <= amount < Decimal(10), (amount, market)
        else:
            assert Decimal(10) <= amount <= Decimal(20), (amount, market)


def test_the_same_root_gives_the_same_rows(expose: CompiledExpose) -> None:
    plan = _plan(expose)
    assert generate_rows(plan, root=root_for()) == generate_rows(plan, root=root_for())
    assert generate_rows(plan, source=GeneratorStreams(root=root_for())) == generate_rows(
        plan, root=root_for()
    )


@pytest.mark.parametrize("column", ["amount", "market"])
def test_column_first_and_row_first_agree(expose: CompiledExpose, column: str) -> None:
    plan = _plan(expose)
    index = expose.index_of[column]
    row_first = tuple(row[index] for row in generate_rows(plan, root=root_for()))
    assert generate_column(plan, column, root=root_for()) == row_first
    assert generate_column(plan, column, source=GeneratorStreams(root=root_for())) == row_first


def test_slices_join_into_the_whole_table(expose: CompiledExpose) -> None:
    plan = _plan(expose)
    whole = generate_rows(plan, root=root_for())
    cuts = (0, 7, 64, 65, plan.rows)
    joined = tuple(
        row
        for start, stop in zip(cuts, cuts[1:], strict=False)
        for row in generate_row_slice(plan, start, stop, root=root_for())
    )
    assert joined == whole


def test_the_given_column_draws_what_it_draws_unconditioned(expose: CompiledExpose) -> None:
    unconditioned = {"bins": ["0", "10", "20"], "binWeights": ["1", "1"]}
    conditioned = generate_rows(_plan(expose), root=root_for())
    plain = generate_rows(_plan(expose, unconditioned), root=root_for())
    assert [market for _, market in conditioned] == [market for _, market in plain]
    assert [amount for amount, _ in conditioned] != [amount for amount, _ in plain]


def test_a_different_root_moves_both_columns(expose: CompiledExpose) -> None:
    plan = _plan(expose)
    first = generate_rows(plan, root=root_for(seed=1))
    second = generate_rows(plan, root=root_for(seed=2))
    assert [row[0] for row in first] != [row[0] for row in second]
    assert [row[1] for row in first] != [row[1] for row in second]


def test_the_digest_documents_the_new_keys_only_when_declared(expose: CompiledExpose) -> None:
    plain = _plan(expose, {"bins": ["0", "10", "20"], "binWeights": ["1", "1"]})
    document = plain.column_plan("amount").statistics.as_document()
    assert "given" not in document and "binWeightsGiven" not in document
    conditioned = _plan(expose)
    document = conditioned.column_plan("amount").statistics.as_document()
    assert document["given"] == "market"
    assert document["binWeightsGiven"] == {"DE": ["1", "0"], "FR": ["0", "1"]}
    moved = _plan(expose, {**GIVEN, "binWeightsGiven": {"DE": ["1", "0"], "FR": ["1", "1"]}})
    assert moved.digest() != conditioned.digest(), "one conditional weight must move the digest"


NFC_LEVEL = "Z" + chr(0xFC) + "rich"
NFD_LEVEL = "Zu" + chr(0x308) + "rich"


def _level_of(value: Any) -> str:
    """The declared level a drawn value renders from (BOOL before INT: bool is an int)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return unicodedata.normalize("NFC", str(value))


@pytest.mark.parametrize(
    ("parent_type", "parent", "keys"),
    [
        ("INT", {"levels": {"1": "0.5", "2": "0.5"}}, ("1", "2")),
        ("BOOL", {"levels": {"false": "0.5", "true": "0.5"}}, ("false", "true")),
        ("STRING", {"support": {"values": ["DE", "FR"]}}, ("DE", "FR")),
        ("STRING", {"levels": {NFC_LEVEL: "0.5", "Bern": "0.5"}}, (NFD_LEVEL, "Bern")),
    ],
    ids=["int-levels", "bool-levels", "string-support", "nfd-key"],
)
def test_each_kind_of_given_column_conditions_exactly(
    parent_type: str, parent: Mapping[str, Any], keys: Any
) -> None:
    expose = expose_from_schema(
        [
            {"name": "amount", "type": "DECIMAL(18,4)", "required": True},
            {"name": "market", "type": parent_type, "required": True},
        ]
    )
    first, second = keys
    conditioned = {
        "bins": ["0", "10", "20"],
        "given": "market",
        "binWeightsGiven": {first: ["1", "0"], second: ["0", "1"]},
    }
    unconditioned = {"bins": ["0", "10", "20"], "binWeights": ["1", "1"]}

    def rows_for(amount: Mapping[str, Any]) -> Any:
        plan = build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=200,
            fidelity=fidelity({"amount": dict(amount), "market": dict(parent)}),
        )
        return generate_rows(plan, root=root_for())

    rows = rows_for(conditioned)
    assert [market for _, market in rows] == [market for _, market in rows_for(unconditioned)]
    low = unicodedata.normalize("NFC", first)
    assert {_level_of(market) for _, market in rows} == {low, second}
    for amount, market in rows:
        if _level_of(market) == low:
            assert Decimal(0) <= amount < Decimal(10), (amount, market)
        else:
            assert Decimal(10) <= amount <= Decimal(20), (amount, market)


@pytest.mark.parametrize(
    ("amount", "fragment"),
    [
        ({**GIVEN, "binWeightsGiven": None}, "binWeightsGiven maps each level"),
        ({key: GIVEN[key] for key in ("bins", "given")}, "declared together"),
        ({key: GIVEN[key] for key in ("bins", "binWeightsGiven")}, "declared together"),
        ({**GIVEN, "binWeights": ["1", "1"]}, "not both"),
        ({**GIVEN, "given": "amount"}, "not drawn conditionally on itself"),
        ({**GIVEN, "given": ""}, "given names the column"),
        ({**GIVEN, "given": "region"}, "not a column of this expose"),
        ({**GIVEN, "binWeightsGiven": {"DE": ["1", "0"]}}, "declares levels"),
        ({**GIVEN, "binWeightsGiven": {"DE": ["1"], "FR": ["0", "1"]}}, "carries 1"),
        ({**GIVEN, "binWeightsGiven": {"DE": ["1", "-1"], "FR": ["0", "1"]}}, "negative"),
        ({**GIVEN, "binWeightsGiven": {"DE": ["0", "0"], "FR": ["0", "1"]}}, "sums to zero"),
        ({**GIVEN, "binWeightsGiven": {"DE": "1", "FR": ["0", "1"]}}, "is a list of weights"),
        ({**GIVEN, "family": "bounded_normal", "range": {"min": "0", "max": "20"}}, "histogram"),
    ],
)
def test_an_undrawable_conditional_is_refused(
    expose: CompiledExpose, amount: Mapping[str, Any], fragment: str
) -> None:
    declaration = {key: value for key, value in amount.items() if value is not None}
    if "binWeightsGiven" in amount and amount["binWeightsGiven"] is None:
        declaration["binWeightsGiven"] = {}
    assert fragment in _refused(expose, declaration)


def test_conditional_weights_need_bins(expose: CompiledExpose) -> None:
    declaration = {key: GIVEN[key] for key in ("given", "binWeightsGiven")}
    declaration["range"] = {"min": "0", "max": "20"}
    assert "without bins" in _refused(expose, declaration)


def test_an_identifier_column_cannot_be_given() -> None:
    """An identifier is derived from ``(H, ordinal)``, never drawn, so it has no level.

    ``_given_cell`` draws a given column the way a categorical cell is drawn,
    so the plan must never let an identifier be one.
    """
    schema: List[Dict[str, Any]] = [
        {"name": "widget_id", "type": "STRING", "required": True, "semanticType": "identifier"},
        {"name": "amount", "type": "DECIMAL(18,4)", "required": True},
    ]
    expose = expose_from_schema(schema)
    histogram = {"bins": ["0", "10", "20"], "binWeights": ["1", "1"]}
    plain = build_plan(
        expose, sim_id=SIM_ID, seam_digest=H, rows=4, fidelity=fidelity({"amount": histogram})
    )
    assert plain.column_plan("widget_id").column_type is ColumnType.IDENTIFIER
    conditional = {
        "bins": ["0", "10", "20"],
        "given": "widget_id",
        "binWeightsGiven": {"a": ["1", "1"]},
    }
    with pytest.raises(FluxError) as error:
        build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=4,
            fidelity=fidelity({"amount": conditional}),
        )
    assert error.value.code.code == "FLX-3402"
    assert "given column 'widget_id' is not categorical" in str(error.value)


def test_the_given_column_must_be_categorical() -> None:
    expose = expose_from_schema(
        [
            {"name": "amount", "type": "DECIMAL(18,4)", "required": True},
            {"name": "score", "type": "DECIMAL(18,4)", "required": True},
        ]
    )
    with pytest.raises(FluxError) as error:
        build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=4,
            fidelity=fidelity(
                {
                    "amount": {**GIVEN, "given": "score"},
                    "score": {"range": {"min": "0", "max": "1"}},
                }
            ),
        )
    assert error.value.code.code == "FLX-3402"
    assert "not categorical" in str(error.value)


def test_a_given_column_with_nulls_is_refused() -> None:
    expose = expose_from_schema(
        [
            {"name": "amount", "type": "DECIMAL(18,4)", "required": True},
            {"name": "market", "type": "STRING", "required": False},
        ]
    )
    with pytest.raises(FluxError) as error:
        build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=4,
            fidelity=fidelity({"amount": GIVEN, "market": {**MARKET, "nullFraction": "0.1"}}),
        )
    assert error.value.code.code == "FLX-3402"
    assert "nullFraction" in str(error.value)


# ---------------------------------------------------------------------------
# Trees: a categorical drawn given a categorical, and chains of them
# ---------------------------------------------------------------------------

# Declared child first, so the draw order cannot be the declared order.
TREE_SCHEMA: List[Dict[str, Any]] = [
    {"name": "amount", "type": "DECIMAL(18,4)", "required": True},
    {"name": "market", "type": "STRING", "required": True},
    {"name": "channel", "type": "STRING", "required": True},
]

#: web rows draw only DE, store rows only FR; DE rows only bin 0, FR rows only bin 1.
TREE: Dict[str, Dict[str, Any]] = {
    "channel": {"levels": {"store": "0.5", "web": "0.5"}},
    "market": {
        "given": "channel",
        "levelWeightsGiven": {"web": {"DE": "1", "FR": "0"}, "store": {"DE": "0", "FR": "1"}},
    },
    "amount": GIVEN,
}


@pytest.fixture
def tree_expose() -> CompiledExpose:
    return expose_from_schema(TREE_SCHEMA)


def _tree_plan(
    expose: CompiledExpose, rows: int = 200, **columns: Mapping[str, Any]
) -> GeneratorPlan:
    declared = {name: dict(value) for name, value in {**TREE, **columns}.items()}
    return build_plan(expose, sim_id=SIM_ID, seam_digest=H, rows=rows, fidelity=fidelity(declared))


def test_a_chain_conditions_every_link_exactly(tree_expose: CompiledExpose) -> None:
    rows = generate_rows(_tree_plan(tree_expose), root=root_for())
    assert {channel for _, _, channel in rows} == {"store", "web"}
    for amount, market, channel in rows:
        assert market == ("DE" if channel == "web" else "FR"), (market, channel)
        if market == "DE":
            assert Decimal(0) <= amount < Decimal(10), (amount, market)
        else:
            assert Decimal(10) <= amount <= Decimal(20), (amount, market)


def test_tree_addressing_survives(tree_expose: CompiledExpose) -> None:
    plan = _tree_plan(tree_expose)
    whole = generate_rows(plan, root=root_for())
    assert generate_rows(plan, source=GeneratorStreams(root=root_for())) == whole
    for index, column in enumerate(("amount", "market", "channel")):
        row_first = tuple(row[index] for row in whole)
        assert generate_column(plan, column, root=root_for()) == row_first, column
        assert (
            generate_column(plan, column, source=GeneratorStreams(root=root_for())) == row_first
        ), column
    cuts = (0, 3, 99, 100, plan.rows)
    joined = tuple(
        row
        for start, stop in zip(cuts, cuts[1:], strict=False)
        for row in generate_row_slice(plan, start, stop, root=root_for())
    )
    assert joined == whole


def test_the_root_draws_what_it_draws_unconditioned(tree_expose: CompiledExpose) -> None:
    plain = _tree_plan(
        tree_expose,
        market={"levels": {"DE": "0.5", "FR": "0.5"}},
        amount={"bins": ["0", "10", "20"], "binWeights": ["1", "1"]},
    )
    conditioned = _tree_plan(tree_expose)
    assert [row[2] for row in generate_rows(conditioned, root=root_for())] == [
        row[2] for row in generate_rows(plain, root=root_for())
    ]


def test_a_declared_support_may_list_levels_never_drawn(tree_expose: CompiledExpose) -> None:
    market = {**TREE["market"], "support": {"values": ["DE", "FR", "GB"]}}
    rows = generate_rows(_tree_plan(tree_expose, market=market), root=root_for())
    assert {row[1] for row in rows} == {"DE", "FR"}


def test_the_digest_covers_level_weights_given(tree_expose: CompiledExpose) -> None:
    plan = _tree_plan(tree_expose)
    document = plan.column_plan("market").statistics.as_document()
    assert document["levelWeightsGiven"] == {
        "store": {"DE": "0", "FR": "1"},
        "web": {"DE": "1", "FR": "0"},
    }
    assert "levelWeightsGiven" not in plan.column_plan("channel").statistics.as_document()
    moved_table = {"web": {"DE": "1", "FR": "1"}, "store": {"DE": "0", "FR": "1"}}
    moved = _tree_plan(tree_expose, market={**TREE["market"], "levelWeightsGiven": moved_table})
    assert moved.digest() != plan.digest()


_BACK = {"DE": {"store": "1", "web": "1"}, "FR": {"store": "1", "web": "1"}}


@pytest.mark.parametrize(
    ("columns", "fragment"),
    [
        ({"channel": {"given": "market", "levelWeightsGiven": _BACK}}, "cycle"),
        ({"market": {**TREE["market"], "levels": {"DE": "0.5", "FR": "0.5"}}}, "not both"),
        (
            {"amount": {**GIVEN, "levelWeightsGiven": {"DE": {"x": "1"}, "FR": {"x": "1"}}}},
            "not both",
        ),
        (
            {"market": {"given": "channel", "levelWeightsGiven": {"web": {"DE": "1"}}}},
            "declares levels",
        ),
        (
            {
                "market": {
                    "given": "channel",
                    "levelWeightsGiven": {"web": {"DE": "1"}, "store": {"FR": "1"}},
                }
            },
            "names levels",
        ),
        ({"market": {**TREE["market"], "support": {"values": ["DE"]}}}, "outside the declared"),
        (
            {
                "market": {
                    "given": "amount",
                    "levelWeightsGiven": {"0": {"DE": "1"}, "1": {"DE": "1"}},
                },
                "amount": {"bins": ["0", "10", "20"], "binWeights": ["1", "1"]},
            },
            "not categorical",
        ),
        (
            {
                "amount": {
                    "family": "bounded_normal",
                    "range": {"min": "0", "max": "20"},
                    "given": "market",
                    "levelWeightsGiven": {"DE": {"1": "1"}, "FR": {"1": "1"}},
                }
            },
            "drawn by a categorical column",
        ),
        (
            {
                "market": {
                    "given": "channel",
                    "levelWeightsGiven": {"web": {"DE": "0", "FR": "0"}, "store": {"DE": "1"}},
                }
            },
            "sums to zero",
        ),
        (
            {
                "market": {
                    "given": "channel",
                    "levelWeightsGiven": {"web": {"DE": "-1", "FR": "2"}, "store": {"DE": "1"}},
                }
            },
            "negative",
        ),
        (
            {"market": {"given": "channel", "levelWeightsGiven": {"web": "1", "store": {}}}},
            "non-empty level -> weight mapping",
        ),
        (
            {
                "market": {
                    "given": "channel",
                    "levelWeightsGiven": {
                        "web": {NFC_LEVEL: "1", NFD_LEVEL: "1"},
                        "store": {NFC_LEVEL: "1"},
                    },
                }
            },
            "normalise onto",
        ),
        ({"market": {"given": "channel", "levelWeightsGiven": {}}}, "maps each level"),
    ],
)
def test_an_undrawable_tree_is_refused(
    tree_expose: CompiledExpose, columns: Mapping[str, Mapping[str, Any]], fragment: str
) -> None:
    with pytest.raises(FluxError) as error:
        _tree_plan(tree_expose, **columns)
    assert error.value.code.code == "FLX-3402"
    assert fragment in str(error.value)


def test_a_nullable_middle_of_a_chain_is_refused() -> None:
    expose = expose_from_schema(
        [
            {"name": "amount", "type": "DECIMAL(18,4)", "required": True},
            {"name": "market", "type": "STRING", "required": False},
            {"name": "channel", "type": "STRING", "required": True},
        ]
    )
    declared = {**TREE, "market": {**TREE["market"], "nullFraction": "0.1"}}
    with pytest.raises(FluxError) as error:
        build_plan(expose, sim_id=SIM_ID, seam_digest=H, rows=4, fidelity=fidelity(declared))
    assert error.value.code.code == "FLX-3402"
    assert "nullFraction" in str(error.value)


def test_a_conditional_level_a_decimal_column_cannot_hold_is_refused_at_plan_time() -> None:
    """The plan-time level check reads ``levelWeightsGiven``'s levels too.

    A non-numeric level on a DECIMAL column is refused while the plan is
    formed, whether it is declared in ``levels`` or only in a conditional
    weight table.
    """
    expose = expose_from_schema(
        [
            {"name": "amount", "type": "DECIMAL(18,2)", "required": True},
            {"name": "market", "type": "STRING", "required": True},
        ]
    )
    declaration = {"given": "market", "levelWeightsGiven": {"DE": {"p": "1"}, "FR": {"p": "1"}}}
    with pytest.raises(FluxError) as error:
        build_plan(
            expose,
            sim_id=SIM_ID,
            seam_digest=H,
            rows=4,
            fidelity=fidelity({"amount": declaration, "market": MARKET}),
        )
    assert error.value.code.code == "FLX-3402"
    assert "level 'p' is not a valid DECIMAL value" in str(error.value)
