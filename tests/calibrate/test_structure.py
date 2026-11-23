# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for flux_engine.calibrate.structure."""

from __future__ import annotations

import itertools
from decimal import ROUND_HALF_EVEN, ROUND_UP, Context, Decimal, getcontext, localcontext
from fractions import Fraction
from types import SimpleNamespace
from typing import List

import pytest

from flux_engine.calibrate.structure import (
    PairTable,
    TreeEdge,
    declarations,
    mutual_information,
    pair_table_from_groups,
    pair_table_from_level_groups,
    suggest_tree,
)
from flux_engine.core.errors import FluxError

pytestmark = pytest.mark.unit

N = 10000
P_CHANNEL = [Fraction(2, 5), Fraction(3, 5)]
P_MARKET = [
    [Fraction(1, 2), Fraction(3, 10), Fraction(1, 5)],
    [Fraction(1, 10), Fraction(3, 10), Fraction(3, 5)],
]
P_AMOUNT = [
    [Fraction(1, 2), Fraction(1, 4), Fraction(1, 8), Fraction(1, 8)],
    [Fraction(1, 10), Fraction(2, 10), Fraction(3, 10), Fraction(4, 10)],
    [Fraction(1, 20), Fraction(1, 20), Fraction(1, 10), Fraction(4, 5)],
]


def _exact(value: Fraction) -> int:
    assert value.denominator == 1
    return int(value)


def chain_tables() -> List[PairTable]:
    cm = {(f"c{c}", f"m{m}"): 0 for c in range(2) for m in range(3)}
    ma = {(f"m{m}", str(a)): 0 for m in range(3) for a in range(4)}
    ca = {(f"c{c}", str(a)): 0 for c in range(2) for a in range(4)}
    # Every cell is an exact integer: Fractions are summed before conversion.
    for c, m in itertools.product(range(2), range(3)):
        cm[(f"c{c}", f"m{m}")] = _exact(N * P_CHANNEL[c] * P_MARKET[c][m])
    for m, a in itertools.product(range(3), range(4)):
        total = sum(N * P_CHANNEL[c] * P_MARKET[c][m] * P_AMOUNT[m][a] for c in range(2))
        ma[(f"m{m}", str(a))] = _exact(total)
    for c, a in itertools.product(range(2), range(4)):
        total = sum(N * P_CHANNEL[c] * P_MARKET[c][m] * P_AMOUNT[m][a] for m in range(3))
        ca[(f"c{c}", str(a))] = _exact(total)
    return [
        PairTable(("channel", "market"), cm),
        PairTable(("market", "amount"), ma),
        PairTable(("channel", "amount"), ca),
    ]


def edge_pairs(edges):
    return [(e.parent, e.child) for e in edges]


def refusal(call) -> None:
    with pytest.raises(FluxError) as error:
        call()
    assert error.value.code.code == "FLX-3402"


# --- mutual information --------------------------------------------------


def test_independent_table_is_exactly_zero() -> None:
    table = PairTable(("a", "b"), {("x", "u"): 6, ("x", "v"): 3, ("y", "u"): 4, ("y", "v"): 2})
    assert mutual_information(table) == Decimal(0)
    assert mutual_information(table).is_zero()


def test_uniform_diagonal_is_ln2() -> None:
    table = PairTable(("a", "b"), {("x", "x"): 5, ("y", "y"): 5, ("x", "y"): 0, ("y", "x"): 0})
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        expected = Decimal(2).ln()
    assert mutual_information(table) == expected


def test_result_ignores_ambient_context() -> None:
    table = chain_tables()[0]
    baseline = mutual_information(table)
    saved = getcontext().copy()
    try:
        getcontext().prec = 5
        getcontext().rounding = ROUND_UP
        assert mutual_information(table) == baseline
    finally:
        getcontext().prec = saved.prec
        getcontext().rounding = saved.rounding


# --- chain recovery -------------------------------------------------------


def test_chain_recovers_channel_market_amount() -> None:
    cm, ma, ca = chain_tables()
    assert mutual_information(ca) < mutual_information(cm)
    assert mutual_information(ca) < mutual_information(ma)
    edges = suggest_tree(
        [cm, ma, ca], categorical=["channel", "market"], numeric=["amount"], root="channel"
    )
    assert edge_pairs(edges) == [("channel", "market"), ("market", "amount")]
    assert all(isinstance(e, TreeEdge) for e in edges)
    assert edges[0].mutual_information == mutual_information(cm)


def test_kruskal_keeps_the_strongest_edges_among_three_categoricals() -> None:
    """a -> b -> c, so I(a;b) > I(b;c) > I(a;c): the maximum tree is a-b, b-c, not a-c."""
    ab = PairTable(
        ("a", "b"), {("a0", "b0"): 450, ("a0", "b1"): 50, ("a1", "b0"): 50, ("a1", "b1"): 450}
    )
    bc = PairTable(
        ("b", "c"), {("b0", "c0"): 400, ("b0", "c1"): 100, ("b1", "c0"): 100, ("b1", "c1"): 400}
    )
    ac = PairTable(
        ("a", "c"), {("a0", "c0"): 370, ("a0", "c1"): 130, ("a1", "c0"): 130, ("a1", "c1"): 370}
    )
    assert mutual_information(ac) < mutual_information(bc) < mutual_information(ab)
    edges = suggest_tree([ac, bc, ab], categorical=["c", "a", "b"])
    assert edge_pairs(edges) == [("a", "b"), ("b", "c")]


def test_a_declared_bin_count_covers_bins_a_sparse_table_leaves_out() -> None:
    edges = [TreeEdge("a", "n", Decimal(1))]
    sparse = PairTable(("a", "n"), {("x", "0"): 2, ("x", "1"): 3, ("y", "0"): 1})
    out = declarations(edges, [sparse], categorical=["a"], numeric=["n"], bins={"n": 3})
    assert out["n"]["binWeightsGiven"] == {"x": ["2", "3", "0"], "y": ["1", "0", "0"]}
    outside = PairTable(("a", "n"), {("x", "0"): 1, ("x", "5"): 1})
    refusal(lambda: declarations(edges, [outside], categorical=["a"], numeric=["n"], bins={"n": 3}))


def test_numeric_never_parent_and_numeric_pairs_ignored() -> None:
    cm, ma, _ = chain_tables()
    # Same totals; a numeric-numeric table that would otherwise be the best edge.
    nn = PairTable(
        ("amount", "score"),
        {(str(i), str(i)): _exact(ma_total) for i, ma_total in enumerate(_amount_margin(ma))},
    )
    edges = suggest_tree(
        [cm, ma, nn], categorical=["channel", "market"], numeric=["amount", "score"]
    )
    assert edge_pairs(edges) == [("channel", "market"), ("market", "amount")]
    assert all(e.parent not in {"amount", "score"} for e in edges)


def _amount_margin(table: PairTable) -> List[Fraction]:
    margin = table.margin("amount")
    return [Fraction(margin[str(i)]) for i in range(len(margin))]


def test_root_choice_reorients_only() -> None:
    cm, ma, _ = chain_tables()
    args = {"categorical": ["channel", "market"], "numeric": ["amount"]}
    edges = suggest_tree([cm, ma], root="market", **args)
    assert edge_pairs(edges) == [("market", "amount"), ("market", "channel")]
    default = suggest_tree([cm, ma], **args)
    assert edge_pairs(default) == [("channel", "market"), ("market", "amount")]


# --- determinism ----------------------------------------------------------


def _diag(a: str, b: str) -> PairTable:
    return PairTable((a, b), {("p", "p"): 4, ("q", "q"): 4})


def test_ties_break_by_name_and_order_is_irrelevant() -> None:
    tables = [_diag("a", "b"), _diag("a", "c"), _diag("b", "c")]
    expected = [("a", "b"), ("a", "c")]
    for perm in itertools.permutations(tables):
        for cats in itertools.permutations(["a", "b", "c"]):
            assert edge_pairs(suggest_tree(list(perm), categorical=list(cats))) == expected


def test_numeric_tie_prefers_name_order() -> None:
    x = PairTable(("x", "z"), {("p", "0"): 4, ("q", "1"): 4})
    y = PairTable(("z", "y"), {("0", "p"): 4, ("1", "q"): 4})
    xy = PairTable(("x", "y"), {("p", "p"): 4, ("q", "q"): 4})
    for perm in itertools.permutations([x, y, xy]):
        edges = suggest_tree(list(perm), categorical=["x", "y"], numeric=["z"])
        assert edge_pairs(edges) == [("x", "y"), ("x", "z")]


def test_tables_and_iterables_permutation_invariant() -> None:
    tables = chain_tables()
    first = suggest_tree(tables, categorical=["channel", "market"], numeric=["amount"])
    for perm in itertools.permutations(tables):
        for cats in itertools.permutations(["channel", "market"]):
            got = suggest_tree(list(perm), categorical=iter(cats), numeric=iter(["amount"]))
            assert got == first


# --- refusals -------------------------------------------------------------


def test_refuses_inconsistent_margins() -> None:
    a = PairTable(("a", "b"), {("x", "u"): 3, ("y", "v"): 3})
    b = PairTable(("a", "c"), {("x", "u"): 2, ("y", "v"): 4})
    refusal(lambda: suggest_tree([a, b], categorical=["a", "b", "c"]))


def test_refuses_different_totals() -> None:
    a = PairTable(("a", "b"), {("x", "u"): 3, ("y", "v"): 3})
    b = PairTable(("a", "c"), {("x", "u"): 3, ("y", "v"): 4})
    refusal(lambda: suggest_tree([a, b], categorical=["a", "b", "c"]))


def test_refuses_column_in_both_sets_and_unknown_column() -> None:
    t = _diag("a", "b")
    refusal(lambda: suggest_tree([t], categorical=["a", "b"], numeric=["b"]))
    refusal(lambda: suggest_tree([t], categorical=["a"]))
    refusal(lambda: suggest_tree([t], categorical=["a", "b"], root="zzz"))


def test_refuses_bad_counts_and_empty_tables() -> None:
    refusal(lambda: PairTable(("a", "b"), {("x", "u"): -1, ("y", "v"): 3}))
    refusal(lambda: PairTable(("a", "b"), {("x", "u"): True, ("y", "v"): 3}))
    refusal(lambda: PairTable(("a", "b"), {("x", "u"): 1.5}))  # type: ignore[dict-item]
    refusal(lambda: PairTable(("a", "b"), {}))
    refusal(lambda: PairTable(("a", "b"), {("x", "u"): 0}))
    refusal(lambda: PairTable(("a", "a"), {("x", "u"): 1}))


def test_declarations_refuse_numeric_parent_and_missing_table() -> None:
    cm, ma, _ = chain_tables()
    kwargs = {"categorical": ["channel", "market"], "numeric": ["amount"]}
    bad = [TreeEdge("amount", "market", Decimal(1))]
    refusal(lambda: declarations(bad, [cm, ma], **kwargs))
    missing = [TreeEdge("channel", "amount", Decimal(1))]
    refusal(lambda: declarations(missing, [cm, ma], **kwargs))


# --- declarations ---------------------------------------------------------


def test_root_shares_sum_to_exactly_one() -> None:
    table = PairTable(("a", "b"), {("x", "u"): 1, ("y", "u"): 1, ("z", "v"): 1})
    out = declarations([TreeEdge("b", "a", Decimal(1))], [table], categorical=["a", "b"])
    shares = out["b"]["levels"]
    assert sum(Decimal(v) for v in shares.values()) == Decimal(1)
    assert shares["u"] == "0.66666666666666666667"
    assert shares["v"] == "0.33333333333333333333"
    assert all(len(v.split(".")[1]) == 20 for v in shares.values())


def test_child_tables_match_in_both_orientations() -> None:
    cm, ma, _ = chain_tables()
    kwargs = {"categorical": ["channel", "market"], "numeric": ["amount"]}
    forward = declarations(suggest_tree([cm, ma], **kwargs), [cm, ma], **kwargs)
    flipped_cm = PairTable(("market", "channel"), {(m, c): v for (c, m), v in cm.counts.items()})
    flipped_ma = PairTable(("amount", "market"), {(a, m): v for (m, a), v in ma.counts.items()})
    assert (
        declarations(
            suggest_tree([flipped_cm, flipped_ma], **kwargs), [flipped_cm, flipped_ma], **kwargs
        )
        == forward
    )
    assert forward["market"]["given"] == "channel"
    assert forward["market"]["levelWeightsGiven"]["c0"] == {
        m: str(cm.counts[("c0", m)]) for m in ("m0", "m1", "m2")
    }
    assert forward["amount"]["given"] == "market"
    assert forward["amount"]["binWeightsGiven"]["m1"] == [
        str(ma.counts[("m1", str(i))]) for i in range(4)
    ]
    assert sum(Decimal(v) for v in forward["channel"]["levels"].values()) == Decimal(1)
    assert set(forward) == {"channel", "market", "amount"}


def test_levels_that_never_occur_are_left_out() -> None:
    table = PairTable(
        ("a", "b"),
        {("x", "u"): 2, ("x", "v"): 1, ("x", "w"): 0, ("y", "u"): 0, ("y", "v"): 0, ("y", "w"): 0},
    )
    out = declarations([TreeEdge("a", "b", Decimal(1))], [table], categorical=["a", "b"])
    assert out["b"]["levelWeightsGiven"] == {"x": {"u": "2", "v": "1"}}
    assert out["a"]["levels"] == {"x": "1.00000000000000000000"}
    num = PairTable(("a", "n"), {("x", "0"): 2, ("x", "1"): 0, ("y", "0"): 0, ("y", "1"): 0})
    out = declarations([TreeEdge("a", "n", Decimal(1))], [num], categorical=["a"], numeric=["n"])
    assert out["n"]["binWeightsGiven"] == {"x": ["2", "0"]}


def test_a_numeric_child_with_more_than_ten_bins() -> None:
    counts = {("x", str(i)): i + 1 for i in range(12)}
    counts.update({("y", str(i)): 12 - i for i in range(12)})
    table = PairTable(("a", "n"), counts)
    out = declarations([TreeEdge("a", "n", Decimal(1))], [table], categorical=["a"], numeric=["n"])
    assert out["n"]["binWeightsGiven"]["x"] == [str(i + 1) for i in range(12)]
    assert out["n"]["binWeightsGiven"]["y"] == [str(12 - i) for i in range(12)]


def test_two_spellings_of_one_cell_are_refused() -> None:
    nfd = "u" + chr(0x308)
    refusal(lambda: PairTable(("a", "b"), {(nfd, "k"): 1, (chr(0xFC), "k"): 2}))


def test_numeric_without_edge_declares_nothing() -> None:
    table = PairTable(("a", "b"), {("x", "u"): 2, ("y", "v"): 1})
    out = declarations([TreeEdge("a", "b", Decimal(1))], [table], categorical=["a", "b"])
    assert "n" not in out


def test_keys_are_nfc() -> None:
    nfd = "u" + chr(0x308)
    nfc_form = chr(0xFC)
    table = PairTable(("a", "b"), {(nfd, "k"): 3, ("v", "k"): 1, (nfc_form, "j"): 2})
    assert table.counts[(nfc_form, "k")] == 3
    assert table.counts[(nfc_form, "j")] == 2
    out = declarations([TreeEdge("a", "b", Decimal(1))], [table], categorical=["a", "b"])
    assert set(out["b"]["levelWeightsGiven"]) == {nfc_form, "v"}
    assert all(key == key.encode().decode() for key in out["b"]["levelWeightsGiven"])
    roots = declarations([TreeEdge("b", "a", Decimal(1))], [table], categorical=["a", "b"])
    assert set(roots["b"]["levels"]) == {"k", "j"}
    assert set(out["a"]["levels"]) == {nfc_form, "v"}


# --- pair_table_from_groups ----------------------------------------------


def test_pair_table_from_groups_duck_typed() -> None:
    groups = [
        SimpleNamespace(group="east", bin_counts=[3, 1, 0]),
        SimpleNamespace(group="west", bin_counts=(0, 2, 4)),
    ]
    table = pair_table_from_groups("region", "amount", groups)
    assert table.columns == ("region", "amount")
    assert table.counts[("east", "0")] == 3
    assert table.counts[("west", "2")] == 4
    assert table.total == 10


def test_pair_table_from_groups_refuses_ragged_and_empty() -> None:
    ragged = [
        SimpleNamespace(group="east", bin_counts=[3, 1]),
        SimpleNamespace(group="west", bin_counts=[1]),
    ]
    refusal(lambda: pair_table_from_groups("region", "amount", ragged))
    refusal(lambda: pair_table_from_groups("region", "amount", []))


# --- pair_table_from_level_groups ----------------------------------------


def test_pair_table_from_level_groups_names_the_counts_in_s_order() -> None:
    # S counts a support column's declared levels sorted, whatever order they
    # were declared in, so the declared order here does not matter either.
    groups = [
        SimpleNamespace(group="store", level_counts=(6, 3, 1)),
        SimpleNamespace(group="web", level_counts=[1, 3, 8]),
    ]
    table = pair_table_from_level_groups("channel", "plan", ["pro", "basic", "plus"], groups)
    assert table.columns == ("channel", "plan")
    assert dict(table.counts) == {
        ("store", "basic"): 6,
        ("store", "plus"): 3,
        ("store", "pro"): 1,
        ("web", "basic"): 1,
        ("web", "plus"): 3,
        ("web", "pro"): 8,
    }
    assert table.margin("plan") == {"basic": 7, "plus": 6, "pro": 9}
    assert table.margin("channel") == {"store": 10, "web": 12}


def test_pair_table_from_level_groups_keeps_a_zero_cell() -> None:
    groups = [SimpleNamespace(group="store", level_counts=(6, 3, 0))]
    table = pair_table_from_level_groups("channel", "plan", ("basic", "plus", "pro"), groups)
    assert table.counts[("store", "pro")] == 0
    assert table.total == 9


def test_pair_table_from_level_groups_normalises_names() -> None:
    decomposed, composed = "e\u0301", "\u00e9"
    groups = [SimpleNamespace(group=decomposed, level_counts=(2, 1))]
    table = pair_table_from_level_groups("channel", "plan", [decomposed, "a"], groups)
    assert dict(table.counts) == {(composed, "a"): 2, (composed, composed): 1}


@pytest.mark.parametrize(
    ("levels", "groups"),
    [
        ([], [SimpleNamespace(group="store", level_counts=())]),
        (["basic", 3], [SimpleNamespace(group="store", level_counts=(1, 1))]),
        (["e\u0301", "\u00e9"], [SimpleNamespace(group="store", level_counts=(1, 1))]),
        (["basic"], [SimpleNamespace(group=1, level_counts=(1,))]),
        (["basic"], [SimpleNamespace(group="store")]),
        (["basic", "plus"], [SimpleNamespace(group="store", level_counts=(1, 2, 3))]),
        (
            ["basic"],
            [
                SimpleNamespace(group="store", level_counts=(1,)),
                SimpleNamespace(group="store", level_counts=(2,)),
            ],
        ),
        (
            ["basic"],
            [
                SimpleNamespace(group="e\u0301", level_counts=(1,)),
                SimpleNamespace(group="\u00e9", level_counts=(2,)),
            ],
        ),
        (["basic"], []),
        (["basic"], [SimpleNamespace(group="store", level_counts=(-1,))]),
        (["basic"], [SimpleNamespace(group="store", level_counts=(0,))]),
    ],
)
def test_pair_table_from_level_groups_refuses(levels, groups) -> None:
    refusal(lambda: pair_table_from_level_groups("channel", "plan", levels, groups))
