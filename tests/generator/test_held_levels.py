# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A level the row holds stands in for a given column's own draw (ADR 0027, amendment of 2026-11-25).

An events or periods emit sets a column of the version before it draws its own cells, and hands
:func:`~flux_engine.generator.rows.cells_before_rules` the level each row holds. A column drawn
given it, and the columns below that one, read the held level, by the column path and by the
per-row path alike, in any slice and across chunk boundaries.
"""

from __future__ import annotations

from typing import Any, List, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator import columns
from flux_engine.generator.rows import cells_before_rules, held_levels

from .test_columns import _both, _given_plan, _root

pytestmark = pytest.mark.unit

MARKETS = ("DE", "FR", "IE")


def _held(start: int, stop: int) -> List[str]:
    """The market each row of a slice holds: ``IE`` on every third row, ``DE`` and ``FR`` between."""
    return [MARKETS[(row * 7) % 3] for row in range(start, stop)]


def _rows(start: int, stop: int) -> Tuple[Tuple[Any, ...], ...]:
    plan = _given_plan()
    return cells_before_rules(plan, start, stop, root=_root(), held={"market": _held(start, stop)})


@pytest.mark.parametrize(("start", "stop"), [(0, 300), (5, 9), (13, 29), (299, 300)])
def test_a_held_level_is_drawn_given_by_both_paths_alike(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)
    by_columns, by_rows, taken = _both(monkeypatch, lambda: _rows(start, stop))
    assert by_columns == by_rows
    assert taken == [True]


def test_a_column_given_a_held_level_follows_it_and_so_does_the_one_below() -> None:
    data = _rows(0, 300)
    held = _held(0, 300)
    # The table gives IE only bronze, and bronze only the plan b.
    tiers = [row[1] for row, market in zip(data, held, strict=True) if market == "IE"]
    plans = [row[2] for row, market in zip(data, held, strict=True) if market == "IE"]
    assert tiers and set(tiers) == {"bronze"}
    assert set(plans) == {"b"}
    # Without the held levels the same rows draw their own markets, and the tiers differ.
    plain = cells_before_rules(_given_plan(), 0, 300, root=_root())
    assert [row[1] for row in plain] != [row[1] for row in data]


def test_a_held_value_that_is_no_level_is_refused() -> None:
    with pytest.raises(FluxError, match="FLX-3403.*'market' holds 'NL'"):
        held_levels(_given_plan(), {"market": ["DE", "NL"]})


def test_held_values_are_read_as_their_levels() -> None:
    assert held_levels(_given_plan(), {"market": ["IE", "DE"]}) == {"market": ["IE", "DE"]}
