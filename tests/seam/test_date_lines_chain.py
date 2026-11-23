# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A long chain of periods plans its lines in polynomial work (ADR 0024 section 8)."""

from __future__ import annotations

from unittest import mock

import pytest

from flux_engine.seam import date_lines as date_lines_module
from flux_engine.seam.date_lines import date_lines
from flux_engine.seam.dates import LifetimeEntry, WithinEntry

pytestmark = pytest.mark.unit


def _chain(levels: int):
    lives = [LifetimeEntry(emit=f"e{i}", start="s", end="e") for i in range(levels + 1)]
    within = [
        WithinEntry(emit=f"e{i}", relationship=("r",) * k, lifetime=True)
        for i in range(levels)
        for k in range(1, levels - i + 1)
    ]

    def step(expose: str, name: str):
        n = int(expose[1:])
        return f"e{n + 1}" if n < levels else None

    return lives, within, step


def _planning_calls(levels: int) -> int:
    """The calls of ``_extend`` that planning a chain of *levels* makes, counted, not timed."""
    lives, within, step = _chain(levels)
    with mock.patch.object(date_lines_module, "_extend", wraps=date_lines_module._extend) as spy:
        date_lines(lives, within, step)
    return spy.call_count


def test_a_twenty_four_level_chain_plans_its_lines_in_polynomial_work() -> None:
    lives, within, step = _chain(24)
    lines = date_lines(lives, within, step)
    names = [line.name for line in lines]
    assert len(names) == len(set(names))
    assert "e0.within:r.r.r.r.lifetime" in names
    # The chain makes about N^2 / 2 lines, one for each (expose, suffix of the chain).
    assert len(names) == 24 * 25 // 2 + 25
    # Each state is planned once and tried against each entry of its expose: at most N^3 calls.
    assert _planning_calls(24) <= 24**3


def test_doubling_a_chain_does_not_multiply_its_planning_work_beyond_a_cube() -> None:
    assert _planning_calls(24) < 8 * _planning_calls(12)
