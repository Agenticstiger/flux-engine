# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A ``one_to_one`` relationship on the counted layout (ADR 0016 section 2, cardinality).

No parent has more than one child: the layout caps the allotment and moves the spare children to
parents with none. What is pinned, on kernel 0.2.0: the rows hold each parent's key at most once,
the column path's rows are the per-row path's, the slices of the table are the whole table, a
cached layout carries the flag, and each plan-time refusal.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Mapping, Optional, Sequence

import pytest

from flux_engine.generator import columns, relations
from flux_engine.generator.counts import count_key, counted_layout
from flux_engine.generator.rows import generate_row_slice, generate_rows

from .test_counted_picks import ACCOUNTS, SIM_ID, entry, one_to_one, plans_of, refused, root_for
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit

LEVELS = {"levels": ["0", "1"], "levelWeights": ["0.5", "0.5"]}
#: Twenty of the 40 accounts are of class 1; one more invoice is the one the cap moves.
INVOICES = 21


@pytest.fixture(autouse=True)
def fresh_layouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relations, "_LAYOUTS", {})


def counted(
    shape: Optional[Mapping[str, Sequence[str]]] = LEVELS, minimum: Optional[int] = None
) -> Dict[str, Dict[str, Any]]:
    return {"invoice": {"account": entry(shape=shape, minimum=minimum)}}


def plans(rows: int = INVOICES, **options: Any) -> Dict[str, Any]:
    return plans_of(document=one_to_one(), rows=rows, counts=counted(**options))


def test_the_rows_hold_each_account_at_most_once_and_the_layout_is_the_capped_one() -> None:
    built = plans()
    rows = generate_rows(built["invoice"], root=root_for())
    names = [column_plan.column.name for column_plan in built["invoice"].columns]
    held = [row[names.index("i_account")] for row in rows]
    assert len(held) == len(set(held)) == INVOICES
    layout = counted_layout(
        count_key(root_for(), SIM_ID, "invoice", "account"),
        ACCOUNTS,
        INVOICES,
        shape=LEVELS,
        one_to_one=True,
    )
    assert max(layout.counts.tolist()) == 1
    # Without the cap an account of class 1 would hold two.
    assert (
        max(
            counted_layout(
                count_key(root_for(), SIM_ID, "invoice", "account"),
                ACCOUNTS,
                INVOICES,
                shape=LEVELS,
            ).counts.tolist()
        )
        == 2
    )
    accounts = [row[0] for row in generate_rows(built["account"], root=root_for())]
    assert Counter(held) == Counter(
        accounts[parent] for parent, count in enumerate(layout.counts) if count
    )


def test_a_hook_free_entry_without_a_shape_spreads_the_children_one_each() -> None:
    built = plans_of(document=one_to_one(), rows=17, counts=counted(shape=None))
    rows = generate_rows(built["invoice"], root=root_for())
    names = [column_plan.column.name for column_plan in built["invoice"].columns]
    held = [row[names.index("i_account")] for row in rows]
    assert len(set(held)) == 17


@pytest.mark.parametrize("chunk", [5, 64])
def test_the_column_path_equals_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    built = plans()
    root = root_for()
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_rows(built["invoice"], root=root)
    )
    assert by_columns == by_rows and taken and all(taken)


def test_the_slices_of_the_table_are_the_whole_table_each_with_a_layout_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = plans()
    root = root_for()
    whole = generate_rows(built["invoice"], root=root)
    joined = []
    for start in range(0, INVOICES, 4):
        monkeypatch.setattr(relations, "_LAYOUTS", {})
        joined.extend(
            generate_row_slice(built["invoice"], start, min(start + 4, INVOICES), root=root)
        )
    assert tuple(joined) == tuple(whole)


# -- the plan-time refusals -------------------------------------------------------------------


@pytest.mark.parametrize(
    "shape",
    [
        {"levels": ["0", "1", "2"], "levelWeights": ["0.2", "0.6", "0.2"]},
        {"bins": ["0", "1", "3"], "binWeights": ["0.5", "0.5"]},
    ],
)
def test_a_shape_that_allows_a_count_above_one_is_refused(
    shape: Mapping[str, Sequence[str]],
) -> None:
    assert "children shape allows" in refused(lambda: plans(shape=shape))


def test_a_bin_that_reaches_one_and_stops_is_accepted() -> None:
    shape = {"bins": ["0", "1"], "binWeights": ["1"]}
    assert plans(shape=shape, rows=ACCOUNTS)


def test_a_minimum_above_one_is_refused() -> None:
    assert "minChildren is 2" in refused(lambda: plans(shape=None, minimum=2, rows=ACCOUNTS))


def test_more_child_rows_than_parents_is_refused_with_a_type_the_scale_note_reads() -> None:
    from flux_engine.generator.counts import TooManyChildRows

    with pytest.raises(TooManyChildRows):
        plans(rows=ACCOUNTS + 1)


def test_a_condition_leaves_the_refusal_to_the_layout() -> None:
    # 30 invoices fit the 40 accounts, but not the eligible ones (a_weight > 5).
    built = plans_of(
        document=one_to_one(),
        rows=30,
        counts={"invoice": {"account": entry(shape=None, when="a_weight > 5")}},
    )
    message = refused(lambda: generate_rows(built["invoice"], root=root_for()))
    assert "more than its" in message


def test_the_columns_of_a_key_of_the_child_stay_refused() -> None:
    # i_account is a unique key of the invoice as well as its relationship's column.
    document = one_to_one()
    for expose in document["exposes"]:
        if expose["exposeId"] == "invoice":
            expose["semantics"]["entities"].append(
                {"name": "by_account", "type": "unique", "expr": "i_account"}
            )
    assert "a key of the expose" in refused(
        lambda: plans_of(document=document, rows=ACCOUNTS, counts=counted())
    )
