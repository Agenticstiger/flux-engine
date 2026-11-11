# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``flux_engine.governance.skills``: static skill-binding validation and
the v0.3 ``fidelity: agentic`` gate.

The static-validation cases mirror the FLUX offline reference validator's
own regression fixtures for the same rule (``tests/test_regression.py``
in the FLUX spec repo: ``"skill model outside agentPolicy"`` /
``_bad_skill_model``) so this module's rule agrees with the validator it
is documented to mirror, not merely with its own idea of the rule.
"""

from __future__ import annotations

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.governance.skills import (
    AGENTIC_EXTENSION_MODULE,
    SkillBindingCheck,
    SkillBindingViolation,
    UnsupportedFidelity,
    agentic_extension_available,
    require_supported_fidelity,
    require_valid_skill_bindings,
    validate_skill_bindings,
)

# ---------------------------------------------------------------------------
# validate_skill_bindings / require_valid_skill_bindings
# ---------------------------------------------------------------------------

AGENT_POLICY = {"allowedModels": ["local-sovereign-8b"], "tokenBudget": 5000}


def test_a_binding_with_no_model_or_budget_passes() -> None:
    (check,) = validate_skill_bindings(AGENT_POLICY, [{"name": "s"}])
    assert check == SkillBindingCheck("s", True, None)


def test_a_binding_whose_model_is_allowed_passes() -> None:
    (check,) = validate_skill_bindings(AGENT_POLICY, [{"name": "s", "model": "local-sovereign-8b"}])
    assert check.ok is True


def test_a_binding_whose_model_is_not_allowed_fails() -> None:
    # Mirrors the FLUX validator's own regression fixture: "skill model
    # outside agentPolicy" (_bad_skill_model sets model="gpt-oss-999b").
    (check,) = validate_skill_bindings(AGENT_POLICY, [{"name": "s", "model": "gpt-oss-999b"}])
    assert check.ok is False
    assert "not in agentPolicy.allowedModels" in check.detail


def test_a_binding_budget_within_the_policy_budget_passes() -> None:
    (check,) = validate_skill_bindings(AGENT_POLICY, [{"name": "s", "tokenBudget": 4000}])
    assert check.ok is True


def test_a_binding_budget_above_the_policy_budget_fails() -> None:
    (check,) = validate_skill_bindings(AGENT_POLICY, [{"name": "s", "tokenBudget": 6000}])
    assert check.ok is False
    assert "exceeds agentPolicy.tokenBudget" in check.detail


def test_a_binding_budget_equal_to_the_policy_budget_passes() -> None:
    (check,) = validate_skill_bindings(AGENT_POLICY, [{"name": "s", "tokenBudget": 5000}])
    assert check.ok is True


def test_a_float_spelled_integer_budget_is_accepted_like_rfc04() -> None:
    (check,) = validate_skill_bindings(AGENT_POLICY, [{"name": "s", "tokenBudget": 5000.0}])
    assert check.ok is True


def test_a_non_integral_budget_fails() -> None:
    (check,) = validate_skill_bindings(AGENT_POLICY, [{"name": "s", "tokenBudget": "a lot"}])
    assert check.ok is False
    assert "must be an integer" in check.detail


def test_a_boolean_budget_is_never_treated_as_an_integer() -> None:
    (check,) = validate_skill_bindings(AGENT_POLICY, [{"name": "s", "tokenBudget": True}])
    assert check.ok is False


def test_multiple_bindings_are_each_checked_independently() -> None:
    checks = validate_skill_bindings(
        AGENT_POLICY,
        [
            {"name": "good", "model": "local-sovereign-8b"},
            {"name": "bad-model", "model": "unknown"},
            {"name": "bad-budget", "tokenBudget": 999999},
        ],
    )
    by_name = {c.skill_name: c.ok for c in checks}
    assert by_name == {"good": True, "bad-model": False, "bad-budget": False}


def test_require_valid_skill_bindings_passes_silently_when_all_ok() -> None:
    require_valid_skill_bindings(AGENT_POLICY, [{"name": "s", "model": "local-sovereign-8b"}])


def test_require_valid_skill_bindings_raises_flx_4102_on_the_first_failure() -> None:
    with pytest.raises(SkillBindingViolation) as excinfo:
        require_valid_skill_bindings(
            AGENT_POLICY,
            [
                {"name": "ok", "model": "local-sovereign-8b"},
                {"name": "bad", "model": "unknown-model"},
            ],
        )
    assert isinstance(excinfo.value, FluxError)
    assert excinfo.value.code.code == "FLX-4102"
    assert excinfo.value.skill_name == "bad"
    assert "bad" in str(excinfo.value)


def test_an_empty_skills_iterable_yields_no_checks() -> None:
    assert validate_skill_bindings(AGENT_POLICY, []) == ()
    require_valid_skill_bindings(AGENT_POLICY, [])  # never raises


# ---------------------------------------------------------------------------
# require_supported_fidelity / agentic_extension_available
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fidelity", ["deterministic", "stochastic"])
def test_non_agentic_fidelities_never_raise_regardless_of_availability(fidelity: str) -> None:
    require_supported_fidelity(fidelity, available=False)
    require_supported_fidelity(fidelity, available=True)


def test_agentic_fidelity_raises_when_unavailable() -> None:
    with pytest.raises(UnsupportedFidelity) as excinfo:
        require_supported_fidelity("agentic", available=False)
    assert isinstance(excinfo.value, FluxError)
    assert excinfo.value.code.code == "FLX-4101"
    assert excinfo.value.fidelity == "agentic"


def test_agentic_fidelity_passes_when_available() -> None:
    require_supported_fidelity("agentic", available=True)


def test_agentic_extension_available_reflects_a_real_find_spec_lookup() -> None:
    # The extension is not installed in this environment (Stage A / W0 ships
    # no agentic package): a live lookup must say so, and requiring agentic
    # fidelity with the live check (no override) must therefore raise.
    assert agentic_extension_available() is False
    assert agentic_extension_available(module_name="flux_engine.core") is True
    with pytest.raises(UnsupportedFidelity):
        require_supported_fidelity("agentic")  # available=None -> live check


def test_agentic_extension_module_constant_is_the_documented_name() -> None:
    assert AGENTIC_EXTENSION_MODULE == "flux_engine_agentic"
