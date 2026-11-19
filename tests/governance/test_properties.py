# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Property-based tests for the governance package.

Two properties earn Hypothesis rather than a handful of examples: skill
budget comparison must agree with RFC-04's own JSON-integer discipline
for *any* float-or-int spelling, and the audit log's byte output must be
independent of a decision dict's insertion order (since ``json.dumps``
is only deterministic here because ``sort_keys=True`` is set -- a
property that is easy to silently lose in a refactor and easy to pin with
a generated example instead of one hand-written dict).
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.governance.audit import DecisionAuditLog
from flux_engine.governance.gate import AIGate
from flux_engine.governance.skills import validate_skill_bindings

pytestmark = [pytest.mark.property]


# ---------------------------------------------------------------------------
# validate_skill_bindings: JSON-integer budget comparison
# ---------------------------------------------------------------------------


@given(
    policy_budget=st.integers(min_value=1, max_value=10**9),
    skill_budget=st.integers(min_value=0, max_value=10**9),
)
def test_skill_budget_check_agrees_with_int_comparison_int_spelling(
    policy_budget: int, skill_budget: int
) -> None:
    agent_policy = {"allowedModels": ["m"], "tokenBudget": policy_budget}
    (check,) = validate_skill_bindings(agent_policy, [{"name": "s", "tokenBudget": skill_budget}])
    assert check.ok == (skill_budget <= policy_budget)


@given(policy_budget=st.integers(min_value=1, max_value=10**6))
def test_skill_budget_check_treats_a_float_spelled_integer_the_same_as_the_int(
    policy_budget: int,
) -> None:
    agent_policy = {"allowedModels": ["m"], "tokenBudget": policy_budget}
    int_check = validate_skill_bindings(agent_policy, [{"name": "s", "tokenBudget": policy_budget}])
    float_check = validate_skill_bindings(
        agent_policy, [{"name": "s", "tokenBudget": float(policy_budget)}]
    )
    assert int_check[0].ok == float_check[0].ok is True


# ---------------------------------------------------------------------------
# DecisionAuditLog: byte output independent of key order
# ---------------------------------------------------------------------------


def _allow_decision() -> dict:
    return AIGate().evaluate(
        {"allowedModels": ["m"], "tokenBudget": 100, "purposeLimitation": False},
        {"model": "m", "useCase": "u", "tokens": 5},
    )


@given(st.permutations(list(_allow_decision().items())))
def test_decisions_bytes_is_independent_of_the_decision_dicts_key_order(
    shuffled_items: list,
) -> None:
    reordered = dict(shuffled_items)
    canonical = _allow_decision()
    assert reordered == canonical  # same content, just reinserted in a different order

    log_canonical = DecisionAuditLog()
    log_canonical.record(canonical, run_id="r", document_id="d", tick_ms=0, phase="SIGNAL")
    log_reordered = DecisionAuditLog()
    log_reordered.record(reordered, run_id="r", document_id="d", tick_ms=0, phase="SIGNAL")

    assert log_canonical.decisions_bytes() == log_reordered.decisions_bytes()
    assert log_canonical.decisions_digest() == log_reordered.decisions_digest()


# ---------------------------------------------------------------------------
# AIGate cumulative accounting: the running total is exactly the sum spent
# ---------------------------------------------------------------------------


@given(spends=st.lists(st.integers(min_value=1, max_value=200), min_size=1, max_size=8))
def test_cumulative_spend_tracks_exactly_the_sum_of_allowed_calls(spends: list[int]) -> None:
    budget = sum(spends) + 1  # always enough room for every call
    gate = AIGate()
    policy = {"allowedModels": ["m"], "tokenBudget": budget, "purposeLimitation": False}
    decision = None
    for tokens in spends:
        decision = gate.call(policy, {"model": "m", "useCase": "u", "tokens": tokens})
    assert decision is not None
    assert gate.budget_remaining(decision) == budget - sum(spends)
