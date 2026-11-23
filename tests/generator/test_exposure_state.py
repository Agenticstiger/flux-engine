# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The exposure hook's state and the cost of its tables (ADR 0025 section 4).

A declaration is kept under ``(sim id, relationship, parent expose id)``: the plan made last wins,
the state is bounded, and two declarations of one plan under one key are refused at plan time. A
window's table of weights is bounded in cells and in how many are kept.
"""

from __future__ import annotations

import pickle
from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator import exposure, relations
from flux_engine.generator.dates import KIND_DATE, WithinLevel
from flux_engine.generator.exposure import ExposureHook
from flux_engine.generator.rates import Rates

pytestmark = pytest.mark.unit

WINDOW = (1735689600000, 1767225599000)


def _child(
    sim_id: str, emit: str, start: str, end: Optional[str], *, relationship: str = "customer"
) -> Any:
    """A child plan holding one exposure-bearing ``within`` level through *relationship*."""
    level = WithinLevel(
        column="opened",
        kind=KIND_DATE,
        relationship=(relationship,),
        start=start,
        end=end,
        parent_kind=KIND_DATE,
    )
    return SimpleNamespace(
        sim_id=sim_id,
        expose=SimpleNamespace(expose_id=emit),
        time_bounds=WINDOW,
        rules=SimpleNamespace(rules=[SimpleNamespace(within=level)], rates=[]),
    )


def _parent(sim_id: str, expose_id: str, columns: List[str]) -> Any:
    return SimpleNamespace(
        sim_id=sim_id,
        expose=SimpleNamespace(expose_id=expose_id),
        time_bounds=WINDOW,
        columns=[SimpleNamespace(column=SimpleNamespace(name=name)) for name in columns],
    )


def _namespace(*emits: str, relationship: str = "customer") -> Dict[str, Any]:
    return {
        "within": [
            {"emit": emit, "relationship": relationship, "date": "opened", "exposure": True}
            for emit in emits
        ]
    }


def _day(value: str) -> date:
    return date.fromisoformat(value)


def test_the_plan_made_last_is_the_one_a_parent_is_counted_by() -> None:
    hook = ExposureHook()
    parent = _parent("sim", "customer", ["id", "start_a", "start_b"])
    first = _child("sim", "account", "start_a", None)
    second = _child("sim", "ledger", "start_b", None)
    hook.member_to(_namespace("account"), first, "customer", "customer")
    row = (None, _day("2025-01-01"), _day("2025-07-01"))
    assert hook.expected(parent, 0, row, "customer") == 365
    # a later plan of the same simulation, a different child over the same two columns of one parent
    hook.member_to(_namespace("ledger"), second, "customer", "customer")
    assert hook.expected(parent, 0, row, "customer") == 184


def test_a_declaration_is_kept_under_the_parent_it_reaches() -> None:
    hook = ExposureHook()
    hook.member_to(_namespace("a"), _child("sim", "a", "s", None), "customer", "one")
    hook.member_to(_namespace("b"), _child("sim", "b", "t", None), "customer", "two")
    assert {key[2]: spec.start for key, spec in hook._specs.items()} == {"one": "s", "two": "t"}
    row = (None, _day("2025-01-01"))
    assert hook.expected(_parent("sim", "one", ["id", "s"]), 0, row, "customer") == 365
    assert hook.expected(_parent("sim", "two", ["id", "t"]), 0, row, "customer") == 365


def test_a_parent_nothing_declared_for_is_refused() -> None:
    hook = ExposureHook()
    hook.member_to(_namespace("a"), _child("sim", "a", "s", None), "customer", "one")
    with pytest.raises(FluxError) as refused:
        hook.expected(_parent("sim", "other", ["id", "s"]), 0, (None, 0), "customer")
    assert refused.value.code.code == "FLX-3403"


def test_the_state_is_bounded_and_drops_the_oldest() -> None:
    hook = ExposureHook()
    for number in range(exposure._MOST_SPECS + 10):
        hook.member_to(
            _namespace("a"), _child(f"sim-{number}", "a", "s", None), "customer", "customer"
        )
    assert len(hook._specs) == exposure._MOST_SPECS
    assert ("sim-0", "customer", "customer") not in hook._specs
    assert (f"sim-{exposure._MOST_SPECS + 9}", "customer", "customer") in hook._specs
    # writing a key again makes it the newest
    hook.member_to(_namespace("a"), _child("sim-10", "a", "s", None), "customer", "customer")
    assert list(hook._specs)[-1] == ("sim-10", "customer", "customer")


def test_the_pickled_hook_keeps_what_expected_needs() -> None:
    hook = ExposureHook()
    hook.member_to(_namespace("a"), _child("sim", "a", "s", None), "customer", "customer")
    hook._found[("x", "y", "z")] = (next(iter(hook._specs.values())), 0, None)
    copy = pickle.loads(pickle.dumps(hook))
    assert copy._specs == hook._specs and copy._found == {}
    assert (
        copy.expected(
            _parent("sim", "customer", ["id", "s"]), 0, (None, _day("2025-01-01")), "customer"
        )
        == 365
    )


def _contract_keys(*sources: str, relationship: str = "customer", target: str = "customer") -> Any:
    exposes = {
        source: SimpleNamespace(
            relationships=[SimpleNamespace(name=relationship, target=target, source=source)]
        )
        for source in sources
    }
    return SimpleNamespace(expose=lambda expose_id: exposes[expose_id])


def test_two_exposes_that_share_a_relationship_and_a_parent_are_refused_at_plan() -> None:
    plans = {
        "account": _child("sim", "account", "start_a", None),
        "ledger": _child("sim", "ledger", "start_b", None),
    }
    with pytest.raises(FluxError) as refused:
        relations._hooked(
            plans, _contract_keys("account", "ledger"), _namespace("account", "ledger")
        )
    assert refused.value.code.code == "FLX-3403"
    message = str(refused.value)
    assert "'account'" in message and "'ledger'" in message


def test_one_expose_per_relationship_and_parent_is_planned() -> None:
    plans = {"account": _child("sim", "account", "start_a", None)}
    found = relations._hooked(plans, _contract_keys("account"), _namespace("account"))
    assert set(found["account"]) == {"customer"}
    # the same relationship name to two different parents is two keys
    plans = {
        "account": _child("sim", "account", "start_a", None),
        "ledger": _child("sim", "ledger", "start_b", None),
    }
    keys = SimpleNamespace(
        expose=lambda expose_id: SimpleNamespace(
            relationships=[
                SimpleNamespace(
                    name="customer",
                    target="customer" if expose_id == "account" else "company",
                    source=expose_id,
                )
            ]
        )
    )
    found = relations._hooked(plans, keys, _namespace("account", "ledger"))
    assert set(found) == {"account", "ledger"}


# -- the cost of a table --------------------------------------------------------------------------


def test_the_table_of_a_window_may_hold_a_million_cells() -> None:
    rates = Rates(column="d", kind=KIND_DATE, month=tuple("1" for _ in range(12)))
    assert exposure._MOST_CELLS == 1_000_000
    low = 0
    exposure._table.cache_clear()
    assert len(exposure._table(KIND_DATE, low, low + 999_999, rates).prefix) == 1_000_001
    exposure._table.cache_clear()
    with pytest.raises(FluxError) as refused:
        exposure._table(KIND_DATE, low, low + 1_000_000, rates)
    assert refused.value.code.code == "FLX-3403"
    assert "1000000" in str(refused.value)


def test_only_the_last_two_tables_are_kept() -> None:
    assert exposure._table.cache_info().maxsize == 2
