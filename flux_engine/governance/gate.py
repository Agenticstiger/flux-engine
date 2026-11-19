# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The sole authorized boundary onto the FLUX enforcement contract (RFC-04).

The architecture makes ``AIGate.call`` "the only path to a
``ModelPort``... first statement ``flux_spec.enforce.decide``". This
module is that boundary today: the import-linter contract "only
governance.gate may import spec.enforce" (pyproject.toml) keeps every
other package -- including the CLI -- from reaching
``flux_engine.spec.enforce`` directly, so there is exactly one place in
flux_engine that can make an enforcement decision.

:class:`AIGate` is that boundary as an object. Three things worth stating
about it up front.

**It never reimplements the decision.** :meth:`AIGate.evaluate` is a thin
pass-through to :func:`decide`, which is a thin pass-through to
``flux_spec.enforce.decide`` -- the reference implementation. Nothing in
this module re-derives "is this model allowed", "is this use case
denied", or "is this budget exceeded"; every one of those questions is
answered exactly once, downstream of this file. That is also why
:meth:`AIGate.evaluate` is what is bound as the ``gate`` callable for
:func:`run_conformance_vectors` / ``flux_spec.enforce.run_vectors`` --
the published conformance vectors exercise this class exactly as
delivered, never a hand-copied reimplementation of the rule it enforces.

**It is the only thing that reaches a ``ModelPort``.** :meth:`AIGate.invoke`
is the one call site in flux_engine that may hold a
:class:`flux_engine.api.agentic.ModelPort` and call
:meth:`~flux_engine.api.agentic.ModelPort.propose` on it -- and it is
gated behind :meth:`AIGate.call`, so a denied decision never reaches the
port at all. A deployment selects the stub port for local development or
CI with ``FLUX_MODEL_PORT=stub`` (see :data:`MODEL_PORT_ENV_VAR`); any
other value, or an unset variable, leaves the port unconfigured, so
:meth:`AIGate.invoke` fails loud rather than silently defaulting to a real
one. The bound port is a private attribute with no accessor: a holder can
ask whether one is configured (:attr:`AIGate.has_model_port`) and what it
calls itself (:attr:`AIGate.model_id`), but the only way to reach
``propose`` is through :meth:`AIGate.invoke`, which is gated behind
:meth:`AIGate.call`.

**Its budget accounting is cumulative, per instance.** :func:`decide`
narrows a call's token budget to ``min(skill, policy)`` -- but nothing in
the RFC-04 contract stops the same allowed call being made an unlimited
number of times. :meth:`AIGate.call` tracks tokens actually spent per
``(policyDigest, skillRef)`` across every call made through one instance
and refuses (``FLX-4004``) once the running total would exceed the
effective budget.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from flux_engine.api.agentic import ModelPort, SkillInvocation, SkillProposal
from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import ErrorCode, FluxError, register
from flux_engine.spec import enforce as _enforce

__all__ = [
    "AIGate",
    "FLX_4002",
    "FLX_4004",
    "MODEL_PORT_ENV_VAR",
    "PolicyDenied",
    "decide",
    "run_conformance_vectors",
]

FLX_4002 = register(
    "FLX-4002",
    "AI call denied by the document's agentPolicy",
    "flux_engine.governance.gate.AIGate.call evaluated the RFC-04 enforcement "
    "decision (flux_engine.spec.enforce.decide, the reference implementation) and "
    "it refused the call: allow was false. The decision's own reasonCode and "
    "reason explain which check failed -- model, use case, purpose, or token "
    "budget -- and travel with this error as PolicyDenied.decision.",
)

FLX_4004 = register(
    "FLX-4004",
    "cumulative token spend exceeds the effective budget across calls",
    "flux_engine.governance.gate.AIGate tracks tokens actually spent per "
    "(policyDigest, skillRef) across every call made through one instance. "
    "RFC-04's decide() enforces the budget per call; this is the budget "
    "enforced across calls -- a policy that allows N tokens does not allow "
    "the same allowed call an unlimited number of times.",
)

#: The environment variable that selects a development :class:`ModelPort`.
#: The only environment read in this module -- a deployment switch, never
#: a value that reaches a digest.
MODEL_PORT_ENV_VAR = "FLUX_MODEL_PORT"


class PolicyDenied(FluxError):
    """Raised by :meth:`AIGate.call` when the RFC-04 decision refuses the call.

    Carries the whole decision object (``allow`` is always ``False`` on
    this path) so a caller can log or surface ``reasonCode``/``reason``
    without re-deriving them from the exception message.
    """

    def __init__(self, decision: Mapping[str, Any], *, code: ErrorCode = FLX_4002) -> None:
        reason = decision.get("reason") or decision.get("reasonCode") or "denied"
        super().__init__(code, str(reason))
        self.decision: Dict[str, Any] = dict(decision)


def decide(
    policy: Dict[str, Any],
    request: Dict[str, Any],
    skill: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The RFC-04 enforcement decision. See ``flux_spec.enforce.decide``."""
    return _enforce.decide(policy, request, skill)


def run_conformance_vectors(
    vectors_path: Union[str, Path],
    *,
    gate: Optional[Any] = None,
    label: str = "flux-engine gate",
) -> Tuple[int, List[str]]:
    """Run the RFC-04 conformance vector file at *vectors_path* against *gate*.

    Defaults to this module's own :func:`decide`. Returns
    ``(failure_count, messages)`` -- see ``flux_spec.enforce.run_vectors``.
    """
    return _enforce.run_vectors(Path(vectors_path), gate=gate or decide, label=label)


@dataclass
class _StubModelPort:
    """A deterministic :class:`ModelPort` with no external call.

    Selected only by ``FLUX_MODEL_PORT=stub`` (see :func:`_select_model_port`).
    ``propose`` derives its content from the invocation's own prompt
    digest, so two calls with the same invocation produce byte-identical
    proposals -- letting the ``AIGate -> ModelPort`` path be exercised end
    to end, including in this package's own tests, without depending on
    ``flux_engine_agentic`` or a network call.

    Not frozen: the :class:`~flux_engine.api.agentic.ModelPort` Protocol
    declares ``model_id: str`` as a settable variable (mypy: "Protocol
    member ModelPort.model_id expected settable variable, got read-only
    attribute"), which a frozen dataclass's field is not.
    """

    model_id: str = "stub-model-port/1"

    def propose(self, invocation: SkillInvocation) -> SkillProposal:
        content = f"stub-proposal:{invocation.skill_name}:{invocation.prompt_digest.value}"
        return SkillProposal(
            content=content,
            model_id=self.model_id,
            tokens_used=0,
            replayed=False,
            content_digest=digest_of_document({"content": content}, subject="stubProposal"),
        )


def _select_model_port(*, env: Optional[Mapping[str, str]] = None) -> Optional[ModelPort]:
    """Resolve the deployment's :class:`ModelPort` from :data:`MODEL_PORT_ENV_VAR`.

    ``FLUX_MODEL_PORT=stub`` selects the deterministic :class:`_StubModelPort`;
    anything else -- including the variable being unset -- resolves to
    ``None``. A caller wanting a real port passes one to
    ``AIGate(model_port=...)`` explicitly; there is no implicit default a
    misconfigured deployment could fall back on.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    if source.get(MODEL_PORT_ENV_VAR) == "stub":
        return _StubModelPort()
    return None


def _tokens_of(request: Mapping[str, Any]) -> int:
    """The JSON-integer value of ``request["tokens"]``.

    A decision's echoed ``request`` is unmodified from the caller's input
    (RFC-04's own ``decide`` never coerces it), so an ``ALLOWED`` decision
    can still carry a float-spelled integer such as ``1000.0``. Mirrors
    the coercion ``flux_spec.enforce.as_int`` applies before comparing
    against the budget, since that helper is private to a module only
    :mod:`flux_engine.governance.gate` may import and is not re-exported
    through the shim.
    """
    value = request.get("tokens")
    if isinstance(value, bool):
        raise TypeError(f"tokens must be a JSON integer, got a bool: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise TypeError(f"tokens must be a JSON integer, got {value!r}")


class AIGate:
    """The sole authorized boundary onto a :class:`ModelPort` (RFC-04 / ADR-0004).

    See the module docstring for the three properties this class holds:
    it never reimplements the RFC-04 decision, it is the only thing that
    may reach a bound port, and its budget accounting is cumulative across
    calls made through one instance.
    """

    def __init__(self, *, model_port: Optional[ModelPort] = None) -> None:
        self._model_port: Optional[ModelPort] = (
            model_port if model_port is not None else _select_model_port()
        )
        self._spent: Dict[Tuple[str, Optional[str]], int] = {}

    @property
    def has_model_port(self) -> bool:
        """Whether a port is bound. Not the port itself.

        This class published the bound port through a read-only-looking
        property, so any holder could call ``gate.model_port.propose(...)``
        and skip the RFC-04 decision entirely -- handing out the very
        capability the gate exists to fence. A call made that way writes no
        line into ``decisions.ndjson``, so the run's ``decisionsDigest``
        (KERNEL-SPEC section 10) and a "0 AI calls, 0 denials" summary would
        both be true statements about a run that made AI calls.

        The port is now reachable only through :meth:`invoke`, which is
        gated behind :meth:`call`. What a caller can still learn about it is
        that it exists (here) and what it identifies itself as
        (:attr:`model_id`) -- neither of which can propose anything.
        """
        return self._model_port is not None

    @property
    def model_id(self) -> Optional[str]:
        """The bound port's own identifier, or ``None`` when no port is bound."""
        port = self._model_port
        return None if port is None else str(getattr(port, "model_id", ""))

    def evaluate(
        self,
        policy: Dict[str, Any],
        request: Dict[str, Any],
        skill: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """The RFC-04 decision for *policy*/*request*/*skill*. Never raises.

        Bindable directly as the ``gate`` callable
        ``flux_spec.enforce.run_vectors`` (and :func:`run_conformance_vectors`)
        expect: ``gate(policy, request, skill=None) -> decision``.
        """
        return decide(policy, request, skill)

    def call(
        self,
        policy: Dict[str, Any],
        request: Dict[str, Any],
        skill: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Evaluate one call and enforce it. Raises :class:`PolicyDenied` on refusal.

        On an allowed decision, also accounts the call's tokens against
        the cumulative per-instance budget (see the class docstring) --
        raising :class:`PolicyDenied` with ``FLX-4004`` when the running
        total would exceed the effective budget, even though this one
        call's own tokens fit within it.
        """
        decision = self.evaluate(policy, request, skill)
        if not decision.get("allow"):
            raise PolicyDenied(decision)
        self._account(request, decision)
        return decision

    def invoke(
        self,
        policy: Dict[str, Any],
        request: Dict[str, Any],
        invocation: SkillInvocation,
        skill: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], SkillProposal]:
        """:meth:`call`, then dispatch the allowed call to the bound port.

        Raises :class:`PolicyDenied` on refusal -- before the port is ever
        reached -- and ``RuntimeError`` when no port is configured: an
        ``AIGate`` with no port can still answer the policy question, it
        just cannot produce content.
        """
        decision = self.call(policy, request, skill)
        if self._model_port is None:
            raise RuntimeError(
                "AIGate has no ModelPort configured -- pass one to "
                f"AIGate(model_port=...) or set {MODEL_PORT_ENV_VAR}=stub"
            )
        proposal = self._model_port.propose(invocation)
        return decision, proposal

    def budget_remaining(self, decision: Mapping[str, Any]) -> Optional[int]:
        """Tokens left under *decision*'s effective budget, after calls so far.

        ``None`` when *decision* carries no ``effectiveBudget`` at all --
        an ``INVALID_POLICY``/``INVALID_REQUEST`` decision never does.
        """
        budget = decision.get("effectiveBudget")
        if budget is None:
            return None
        spent = self._spent.get(self._spend_key(decision), 0)
        return max(budget - spent, 0)

    @staticmethod
    def _spend_key(decision: Mapping[str, Any]) -> Tuple[str, Optional[str]]:
        return (str(decision["policyDigest"]), decision.get("skillRef"))

    def _account(self, request: Mapping[str, Any], decision: Mapping[str, Any]) -> None:
        budget = decision.get("effectiveBudget")
        if budget is None:  # pragma: no cover - unreachable: allow=True always carries one
            return
        key = self._spend_key(decision)
        total = self._spent.get(key, 0) + _tokens_of(request)
        if total > budget:
            denial = {
                **decision,
                "allow": False,
                "reasonCode": "TOKEN_BUDGET_EXCEEDED",
                "reason": (
                    f"cumulative spend {total} exceeds effective budget {budget} "
                    "across calls made through this AIGate"
                ),
            }
            raise PolicyDenied(denial, code=FLX_4004)
        self._spent[key] = total
