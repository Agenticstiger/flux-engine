# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``flux_engine.governance.gate``: RFC-04 conformance, ``PolicyDenied``, the
``ModelPort`` boundary, and the six defective gates that must fail.

Two things this file resolves rather than assumes, both stated here so a
reader does not have to reconstruct the reasoning from the diff:

**Which callable is "the bound gate".** ``flux_spec.enforce.run_vectors``
calls ``gate(policy, request, skill)`` once per vector and compares the
*returned decision* against the vector's ``expect`` block -- it never
catches an exception from ``gate`` itself. :meth:`AIGate.call` raises
:class:`PolicyDenied` on a deny decision (that is its whole purpose as
"the sole authorized boundary onto a ModelPort" -- a caller of ``call``
wants an exception on refusal, not a decision object to branch on), so
``call`` cannot be the callable bound into ``run_vectors`` without every
deny vector crashing the run instead of being compared.
:meth:`AIGate.evaluate` is the un-raising counterpart -- a direct pass
through to :func:`~flux_engine.governance.gate.decide`, with no side
effect -- and it is what ``test_all_34_vectors_pass_through_aigate``
binds. ``test_call_raises_policydenied_on_every_deny_vector`` then proves
:meth:`AIGate.call` agrees with :meth:`AIGate.evaluate` on every vector:
raises exactly when ``evaluate`` returns ``allow: false``, and the raised
:class:`PolicyDenied` carries that same decision.

**How the six defective gates connect to this class.** The task names
them as gates "AIGate's bound gate" must reject -- but the defects
(fail-open ``purposeLimitation``, a skill widening rather than narrowing
a budget, ``isinstance(x, int)`` budget handling, case-folded matching, a
lenient off-spec-policy fallback, a fabricated ``policyDigest``) are all
defects *in the enforcement decision itself*, and :class:`AIGate`
deliberately never reimplements that decision (see the module docstring:
"it never reimplements the decision"). So the six broken gates are run
directly against :func:`~flux_engine.governance.gate.run_conformance_vectors`
(``test_the_six_defective_gates_each_fail_the_suite``), proving the
published vector file constrains conformance -- and a companion test
proves :class:`AIGate` itself is not one of the six: its own bound
``evaluate`` passes every vector with zero failures. Together they show
what matters: swapping in any of the six defects breaks the suite, and
``AIGate`` does not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.api.agentic import SkillInvocation
from flux_engine.core.digest import DigestDomain, DigestRef
from flux_engine.core.errors import FluxError
from flux_engine.governance.gate import (
    MODEL_PORT_ENV_VAR,
    AIGate,
    PolicyDenied,
    _select_model_port,
    decide,
    run_conformance_vectors,
)
from tests.governance.conftest import DEFECTS, broken_gate

# ---------------------------------------------------------------------------
# RFC-04 conformance through AIGate
# ---------------------------------------------------------------------------


def test_all_34_vectors_pass_through_aigate_bound_gate(vectors_path: Path, vectors) -> None:
    gate = AIGate()
    failures, messages = run_conformance_vectors(vectors_path, gate=gate.evaluate)
    assert failures == 0, messages
    assert len(vectors) == 34  # the published vector count this task names


def test_aigate_evaluate_is_a_pure_passthrough_to_decide() -> None:
    """``evaluate`` adds nothing: same policy/request/skill, same decision."""
    policy = {"allowedModels": ["m"], "tokenBudget": 100}
    request = {"model": "m", "useCase": "u", "tokens": 10, "purpose": "p"}
    assert AIGate().evaluate(policy, request) == decide(policy, request)


def test_the_six_defective_gates_each_fail_the_suite(vectors_path: Path) -> None:
    """THE meta-test: a gate with exactly one real defect must fail >=1 vector."""
    for defect in DEFECTS:
        failures, _messages = run_conformance_vectors(vectors_path, gate=broken_gate(defect))
        assert failures > 0, f"defect {defect!r} passed the whole vector suite"


def test_aigate_is_not_one_of_the_six_defects(vectors_path: Path) -> None:
    """The contrapositive of the meta-test: AIGate's own gate has zero failures."""
    failures, messages = run_conformance_vectors(vectors_path, gate=AIGate().evaluate)
    assert failures == 0, messages


# ---------------------------------------------------------------------------
# call(): raises PolicyDenied on every deny vector, returns on every allow
# ---------------------------------------------------------------------------


def test_call_raises_policydenied_on_every_deny_vector(vectors) -> None:
    # A fresh AIGate per vector: this test is about call()/evaluate() agreeing
    # on each vector in isolation, not about cumulative budget accounting
    # across vectors that happen to share a policyDigest (that is its own
    # test below) -- reusing one instance here would make an ALLOWED vector
    # spuriously deny once the running total from *other* vectors caught up.
    for vector in vectors:
        gate = AIGate()
        policy, request, skill = vector["policy"], vector["request"], vector.get("skill")
        decision = gate.evaluate(policy, request, skill)
        if decision["allow"]:
            returned = gate.call(policy, request, skill)
            assert returned == decision
        else:
            with pytest.raises(PolicyDenied) as excinfo:
                gate.call(policy, request, skill)
            assert excinfo.value.decision == decision
            assert isinstance(excinfo.value, FluxError)
            assert excinfo.value.code.code == "FLX-4002"


def test_policy_denied_carries_reason_code_in_the_message() -> None:
    policy = {"allowedModels": ["a"], "tokenBudget": 10}
    request = {"model": "not-allowed", "useCase": "u", "tokens": 1, "purpose": "p"}
    with pytest.raises(PolicyDenied) as excinfo:
        AIGate().call(policy, request)
    assert excinfo.value.decision["reasonCode"] == "MODEL_NOT_ALLOWED"
    assert "not-allowed" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Cumulative, per-instance budget accounting (FLX-4004)
# ---------------------------------------------------------------------------


def _allowed_policy_request(tokens: int) -> tuple[Dict[str, Any], Dict[str, Any]]:
    policy = {"allowedModels": ["m"], "tokenBudget": 1000, "purposeLimitation": False}
    request = {"model": "m", "useCase": "u", "tokens": tokens}
    return policy, request


def test_single_call_within_budget_is_accounted_and_reduces_remaining() -> None:
    gate = AIGate()
    policy, request = _allowed_policy_request(400)
    decision = gate.call(policy, request)
    assert gate.budget_remaining(decision) == 600


def test_cumulative_spend_across_calls_can_exceed_a_per_call_valid_budget() -> None:
    """Each call is individually within budget; the running total is not."""
    gate = AIGate()
    policy, request = _allowed_policy_request(400)
    gate.call(policy, request)
    gate.call(policy, request)  # 800 total, still <= 1000
    with pytest.raises(PolicyDenied) as excinfo:
        gate.call(policy, request)  # 1200 total > 1000
    assert excinfo.value.decision["reasonCode"] == "TOKEN_BUDGET_EXCEEDED"
    assert excinfo.value.code.code == "FLX-4004"
    assert "cumulative spend" in excinfo.value.decision["reason"]


def test_budget_accounting_is_scoped_per_policy_digest_and_skill_ref() -> None:
    """A different policy (different digest) gets its own budget, not a shared one."""
    gate = AIGate()
    policy_a, request = _allowed_policy_request(900)
    policy_b = {**policy_a, "tokenBudget": 1000, "allowedModels": ["other-model"]}
    request_b = {**request, "model": "other-model"}
    gate.call(policy_a, request)
    gate.call(policy_b, request_b)  # different policyDigest -- must not collide with A


def test_a_denied_call_is_never_accounted() -> None:
    """A call that never allows must not consume any of the running budget."""
    gate = AIGate()
    policy, request = _allowed_policy_request(999999)  # exceeds tokenBudget outright
    with pytest.raises(PolicyDenied):
        gate.call(policy, request)
    # the accounting map must still be empty -- nothing was spent
    assert gate._spent == {}  # noqa: SLF001 - white-box check of the invariant


def test_budget_remaining_is_none_for_an_invalid_policy_decision() -> None:
    # MODEL_NOT_ALLOWED etc. still carry effectiveBudget (decide() computes it
    # before those checks); only INVALID_POLICY / INVALID_REQUEST -- checked
    # first, before a budget can even be computed -- omit it.
    gate = AIGate()
    denial = decide(
        {"allowedModels": ["a"]}, {"model": "b", "useCase": "u", "tokens": 1}
    )  # no tokenBudget
    assert denial["reasonCode"] == "INVALID_POLICY"
    assert gate.budget_remaining(denial) is None


def test_tokens_of_rejects_a_bool_and_a_non_integral_float() -> None:
    from flux_engine.governance.gate import _tokens_of

    with pytest.raises(TypeError):
        _tokens_of({"tokens": True})
    with pytest.raises(TypeError):
        _tokens_of({"tokens": 5.5})
    assert _tokens_of({"tokens": 5}) == 5
    assert _tokens_of({"tokens": 5.0}) == 5


# ---------------------------------------------------------------------------
# The ModelPort boundary
# ---------------------------------------------------------------------------


def test_no_model_port_by_default() -> None:
    assert AIGate().has_model_port is False
    assert AIGate().model_id is None


def test_flux_model_port_stub_selects_the_stub_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODEL_PORT_ENV_VAR, "stub")
    gate = AIGate()
    assert gate.has_model_port is True
    assert gate.model_id == "stub-model-port/1"


def test_the_bound_port_itself_is_not_reachable_from_the_gate() -> None:
    """What a holder learns is that a port exists and what it calls itself.

    A published ``model_port`` property let any holder call
    ``gate.model_port.propose(...)`` and skip the RFC-04 decision -- a call
    that writes no line into ``decisions.ndjson``, so the run's
    ``decisionsDigest`` and a "0 AI calls, 0 denials" summary would both be
    true statements about a run that made AI calls.
    """
    from flux_engine.governance.gate import _StubModelPort

    gate = AIGate(model_port=_StubModelPort())
    assert gate.has_model_port is True
    assert not hasattr(gate, "model_port")


def test_an_unrecognised_flux_model_port_value_selects_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MODEL_PORT_ENV_VAR, "some-other-value")
    assert AIGate().has_model_port is False


def test_select_model_port_reads_an_explicit_env_mapping_not_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Prove the env read is injectable: os.environ carries "stub" but the
    # explicit mapping passed in does not, and the explicit mapping wins.
    monkeypatch.setenv(MODEL_PORT_ENV_VAR, "stub")
    assert _select_model_port(env={}) is None
    assert _select_model_port(env={MODEL_PORT_ENV_VAR: "stub"}) is not None


def _invocation(*, budget: int = 100) -> SkillInvocation:
    digest = DigestRef(
        value="sha256:" + "ab" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="prompt"
    )
    policy_digest = DigestRef(
        value="sha256:" + "cd" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="policy"
    )
    return SkillInvocation(
        skill_name="trait-synth",
        seed_path=("sim", "s1", "agentic", "trait-synth", "subject", 0, 0),
        subject_ordinal=0,
        call_ordinal=0,
        prompt_digest=digest,
        policy_digest=policy_digest,
        token_budget=budget,
    )


def test_invoke_dispatches_to_the_stub_port_only_when_allowed() -> None:
    from flux_engine.governance.gate import _StubModelPort

    gate = AIGate(model_port=_StubModelPort())
    policy = {"allowedModels": ["m"], "tokenBudget": 100, "purposeLimitation": False}
    request = {"model": "m", "useCase": "u", "tokens": 5}
    decision, proposal = gate.invoke(policy, request, _invocation())
    assert decision["allow"] is True
    assert proposal.model_id == "stub-model-port/1"
    assert proposal.replayed is False


def test_invoke_never_reaches_the_port_on_a_denied_call() -> None:

    class _Exploding:
        model_id = "should-never-be-called"

        def propose(self, invocation: SkillInvocation) -> Any:  # pragma: no cover - must not run
            raise AssertionError("ModelPort.propose was reached on a denied call")

    gate = AIGate(model_port=_Exploding())
    policy = {"allowedModels": ["m"], "tokenBudget": 100}
    request = {"model": "not-m", "useCase": "u", "tokens": 5, "purpose": "p"}
    with pytest.raises(PolicyDenied):
        gate.invoke(policy, request, _invocation())


def test_invoke_raises_runtime_error_with_no_port_configured() -> None:
    gate = AIGate(model_port=None)
    policy = {"allowedModels": ["m"], "tokenBudget": 100, "purposeLimitation": False}
    request = {"model": "m", "useCase": "u", "tokens": 5}
    with pytest.raises(RuntimeError, match="no ModelPort configured"):
        gate.invoke(policy, request, _invocation())


def test_stub_model_port_is_deterministic_across_calls() -> None:
    from flux_engine.governance.gate import _StubModelPort

    port = _StubModelPort()
    invocation = _invocation()
    first = port.propose(invocation)
    second = port.propose(invocation)
    assert first.content == second.content
    assert first.content_digest == second.content_digest
