# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Skill bindings: static validation at plan time, and the v0.3 fidelity gate.

Two independent checks, both named in the schema.

**Static validation** (``validate_skill_bindings`` / ``require_valid_skill_bindings``)
mirrors the FLUX offline reference validator's cross-document check --
schema ``$defs/skillBinding``: "``model``: Must be a member of
agentPolicy.allowedModels (validator-enforced)" and "``tokenBudget``:
Per-skill budget; must not exceed agentPolicy.tokenBudget
(validator-enforced)". This module runs the identical rule inside
flux_engine, at plan time, before any call the binding would authorize is
ever made -- it does not re-derive the rule from the RFC-04 decision
contract in :mod:`flux_engine.spec.enforce`, because a skill binding is a
document-shape question (does this binding fit inside the policy that
governs its document?), not a runtime enforcement decision (may this one
call proceed?). :func:`flux_engine.governance.gate.decide` answers the
second question, at call time, using the actual narrowed budget.

**The v0.3 fidelity gate** (``require_supported_fidelity``) is
independent of skill bindings: ``Simulation.spec.fidelity: agentic``
(schema, escalating: deterministic / stochastic / agentic) requires the
``flux_engine_agentic`` extension package, which is the only thing in
this engine that reaches a real language model (see
:mod:`flux_engine.api.agentic`'s module docstring: "the agentic tier
ships separately and can be uninstalled without touching anything
else"). The runtime skill registry itself is out of scope for v0.3 -- this
module's whole contribution to it is refusing to proceed when the
extension the registry would need is not installed.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Tuple

from flux_engine.core.errors import FluxError, register

__all__ = [
    "AGENTIC_EXTENSION_MODULE",
    "FLX_4101",
    "FLX_4102",
    "SkillBindingCheck",
    "SkillBindingViolation",
    "UnsupportedFidelity",
    "agentic_extension_available",
    "require_supported_fidelity",
    "require_valid_skill_bindings",
    "validate_skill_bindings",
]

FLX_4101 = register(
    "FLX-4101",
    "fidelity: agentic declared without the agentic extension installed",
    "Simulation.spec.fidelity: agentic (schema-declared) requires the "
    "flux_engine_agentic extension package -- the only thing in this "
    "engine that reaches a real language model. It is not installed in "
    "this environment. deterministic and stochastic fidelity need nothing "
    "beyond the core engine and are unaffected.",
)

FLX_4102 = register(
    "FLX-4102",
    "skill binding is not permitted by the document's agentPolicy",
    "A skill's declared model is not a member of agentPolicy.allowedModels, or "
    "its declared tokenBudget exceeds agentPolicy.tokenBudget (schema "
    "$defs/skillBinding: both are validator-enforced). This mirrors the FLUX "
    "offline validator's cross-document check and runs at plan time, before any "
    "call the binding would authorize is ever made.",
)

#: The extension package name checked by :func:`agentic_extension_available`.
#: A plain string, not an import, so checking for it never imports it.
AGENTIC_EXTENSION_MODULE = "flux_engine_agentic"


class UnsupportedFidelity(FluxError):
    """Raised for ``fidelity: agentic`` when the agentic extension is absent."""

    def __init__(self, fidelity: str) -> None:
        super().__init__(
            FLX_4101,
            f"fidelity {fidelity!r} requires the {AGENTIC_EXTENSION_MODULE!r} "
            "extension, which is not installed",
        )
        self.fidelity = fidelity


class SkillBindingViolation(FluxError):
    """Raised by :func:`require_valid_skill_bindings` for one failing binding."""

    def __init__(self, skill_name: str, detail: str) -> None:
        super().__init__(FLX_4102, f"skill {skill_name!r}: {detail}")
        self.skill_name = skill_name
        self.detail = detail


def agentic_extension_available(*, module_name: str = AGENTIC_EXTENSION_MODULE) -> bool:
    """Whether *module_name* can be imported, without importing it.

    ``importlib.util.find_spec`` answers "is this importable" by searching
    the same finders ``import`` would use, but does not execute the
    module -- checking availability never has the side effects of loading
    the agentic extension.
    """
    return importlib.util.find_spec(module_name) is not None


def require_supported_fidelity(fidelity: str, *, available: Optional[bool] = None) -> None:
    """Raise :class:`UnsupportedFidelity` for ``"agentic"`` without the extension.

    ``deterministic`` and ``stochastic`` (the schema's other two fidelity
    values) always pass -- this function does not validate that *fidelity*
    is one of the three; that is the schema's job. *available* overrides
    the live :func:`agentic_extension_available` check, for a caller (or a
    test) that wants to force either branch deterministically.
    """
    if fidelity != "agentic":
        return
    is_available = agentic_extension_available() if available is None else available
    if not is_available:
        raise UnsupportedFidelity(fidelity)


def _as_json_int(value: Any) -> Optional[int]:
    """The JSON notion of an integer (RFC-04's ``as_int``), duplicated narrowly.

    A budget is compared here the same way :mod:`flux_engine.spec.enforce`
    compares one -- ``200000.0`` is the integer 200000, a bool is never an
    integer -- but this module does not import that one's private helper
    (only ``decide``, ``run_vectors`` and ``DEFAULT_VECTORS`` are
    re-exported through the shim, and only
    :mod:`flux_engine.governance.gate` may reach the module underneath).
    Returns ``None`` for anything that is not a JSON integer.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


@dataclass(frozen=True)
class SkillBindingCheck:
    """The outcome of validating one skill binding against an ``agentPolicy``."""

    skill_name: str
    ok: bool
    detail: Optional[str] = None


def validate_skill_bindings(
    agent_policy: Mapping[str, Any], skills: Iterable[Mapping[str, Any]]
) -> Tuple[SkillBindingCheck, ...]:
    """Check every binding in *skills* against *agent_policy*. Never raises.

    Mirrors the FLUX offline validator's rule exactly: a binding with no
    declared ``model`` or ``tokenBudget`` passes (those fields are
    optional on ``skillBinding`` -- a skill that declares neither takes
    whatever the document's own default call site would use); a
    non-integer ``tokenBudget`` fails, since the schema declares it an
    integer and a caller that supplied something else is off-spec here
    too, not merely inside a request.
    """
    allowed_models = {str(model) for model in agent_policy.get("allowedModels", [])}
    policy_budget = _as_json_int(agent_policy.get("tokenBudget"))

    checks = []
    for skill in skills:
        name = str(skill.get("name", "<unnamed>"))
        model = skill.get("model")
        if model is not None and str(model) not in allowed_models:
            checks.append(
                SkillBindingCheck(
                    name, False, f"model {model!r} is not in agentPolicy.allowedModels"
                )
            )
            continue

        declared_budget = skill.get("tokenBudget")
        if declared_budget is None:
            checks.append(SkillBindingCheck(name, True))
            continue

        budget = _as_json_int(declared_budget)
        if budget is None:
            checks.append(
                SkillBindingCheck(
                    name, False, f"tokenBudget must be an integer, got {declared_budget!r}"
                )
            )
            continue
        if policy_budget is not None and budget > policy_budget:
            checks.append(
                SkillBindingCheck(
                    name,
                    False,
                    f"tokenBudget {budget} exceeds agentPolicy.tokenBudget {policy_budget}",
                )
            )
            continue
        checks.append(SkillBindingCheck(name, True))

    return tuple(checks)


def require_valid_skill_bindings(
    agent_policy: Mapping[str, Any], skills: Iterable[Mapping[str, Any]]
) -> None:
    """:func:`validate_skill_bindings`, raising :class:`SkillBindingViolation`
    on the first failing binding (bindings are checked in *skills* order)."""
    for check in validate_skill_bindings(agent_policy, skills):
        if not check.ok:
            raise SkillBindingViolation(check.skill_name, check.detail or "binding rejected")
