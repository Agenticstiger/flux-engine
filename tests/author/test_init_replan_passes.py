# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The authoring run plans again, once for each table whose history is left out, and no longer.

A history that is not written holds no relationship back, so the run plans again with that table's
history unwritten (``_laid_out``). The tables left out so far must only grow: a pass that forgot
them would leave one out for ever. Pinned here with the stages around the loop replaced, so that
the tables of two passes can be chosen: the second table is left out only once the first is.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, FrozenSet, List, Tuple

import pytest

from flux_engine.author import bundle

pytestmark = pytest.mark.unit

TABLES = frozenset({"member", "visit"})


def _stages(
    monkeypatch: pytest.MonkeyPatch,
    written: Any,
    seen: List[FrozenSet[str]],
    keeps: bool = True,
) -> Tuple[Any, Any, Any]:
    """The loop's inputs, its fallbacks and what it is called with, around *written*.

    *written* says which histories a pass writes, from the tables left out before it. Without
    *keeps*, a pass holds every table, whatever was left out before it.
    """

    def plan_bundle(inputs: Any, unwritten: FrozenSet[str] = frozenset()) -> Any:
        seen.append(unwritten)
        return SimpleNamespace(
            held=TABLES - unwritten if keeps else TABLES, authored=[], contract=None
        )

    def histories(planned: Any, *rest: Any) -> Any:
        entries = [{"emit": name} for name in sorted(written(seen[-1]) & planned.held)]
        return SimpleNamespace(entries=entries), "projections"

    window = ("2024-01-01", "2024-12-31")
    monkeypatch.setattr(bundle, "_plan_bundle", plan_bundle)
    monkeypatch.setattr(bundle, "_bundle_window", lambda planned, inputs: (window, None, None))
    monkeypatch.setattr(bundle, "_widen_ranges", lambda authored, widened: None)
    monkeypatch.setattr(bundle, "_histories_and_projections", histories)
    return SimpleNamespace(history=None), SimpleNamespace(widened=None), (None, None)


def test_a_table_left_out_only_once_another_is_has_its_history_left_out_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: List[FrozenSet[str]] = []

    def written(unwritten: FrozenSet[str]) -> FrozenSet[str]:
        # The visits' history is written only while the members' is: with it left out, no
        # history of the visits is written either.
        return frozenset({"visit"}) if not unwritten else frozenset()

    inputs, fallbacks, declined = _stages(monkeypatch, written, seen)
    planned, _, _, (_, projections) = bundle._laid_out(inputs, fallbacks, declined)
    # The first pass leaves out the members, the second the visits, and the third leaves out none.
    assert seen == [frozenset(), frozenset({"member"}), TABLES]
    assert planned.held == frozenset() and projections == "projections"


def test_a_pass_that_forgets_the_tables_left_out_stops_with_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: List[FrozenSet[str]] = []
    # Whatever is held is never written, so a run that does not keep its tables could not end.
    inputs, fallbacks, declined = _stages(monkeypatch, lambda unwritten: frozenset(), seen, False)
    with pytest.raises(RuntimeError, match="laid out 3 times for 2 tables"):
        bundle._laid_out(inputs, fallbacks, declined)
