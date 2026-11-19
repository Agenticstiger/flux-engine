# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""AI-call gating (RFC-04), decision audit, and sovereignty enforcement.

Four modules, each owning one governance concern:

* :mod:`flux_engine.governance.gate` -- :class:`~flux_engine.governance.gate.AIGate`,
  the sole authorized boundary onto a
  :class:`~flux_engine.api.agentic.ModelPort`, wrapping the RFC-04
  enforcement decision from :mod:`flux_engine.spec.enforce` (never a
  reimplementation of it).
* :mod:`flux_engine.governance.audit` -- the decision audit trail:
  ``decisions.ndjson`` (the RFC-04 decision object, verbatim, schema-
  validated), its ``decisions.meta.ndjson`` sidecar, and the raw-bytes
  ``decisionsDigest`` KERNEL-SPEC section 10 names.
* :mod:`flux_engine.governance.sovereignty` -- World/Simulation
  sovereignty (schema ``$defs/sovereignty``): block or audit a run whose
  declared zone falls outside the document's allowed zones.
* :mod:`flux_engine.governance.skills` -- static skill-binding validation
  against ``agentPolicy`` at plan time, and the v0.3 fidelity gate that
  refuses ``fidelity: agentic`` without the agentic extension installed.

This package's ``FLX-4nnn`` codes (KERNEL-SPEC section 13's
"governance/sovereignty" band) are registered by the module that raises
each one, not aggregated here -- the same discipline
:mod:`flux_engine.core.errors` documents for the core bands.
"""

from __future__ import annotations

from flux_engine.governance.audit import (
    AuditedDecision,
    DecisionAuditLog,
    DecisionAuditWrite,
    DecisionMeta,
    decision_schema_validator,
)
from flux_engine.governance.gate import (
    MODEL_PORT_ENV_VAR,
    AIGate,
    PolicyDenied,
    decide,
    run_conformance_vectors,
)
from flux_engine.governance.skills import (
    SkillBindingCheck,
    SkillBindingViolation,
    UnsupportedFidelity,
    agentic_extension_available,
    require_supported_fidelity,
    require_valid_skill_bindings,
    validate_skill_bindings,
)
from flux_engine.governance.sovereignty import (
    SovereigntyAuditRow,
    SovereigntyPolicy,
    SovereigntyViolation,
    declared_zone,
    enforce_sovereignty,
)

__all__ = [
    "AIGate",
    "AuditedDecision",
    "DecisionAuditLog",
    "DecisionAuditWrite",
    "DecisionMeta",
    "MODEL_PORT_ENV_VAR",
    "PolicyDenied",
    "SkillBindingCheck",
    "SkillBindingViolation",
    "SovereigntyAuditRow",
    "SovereigntyPolicy",
    "SovereigntyViolation",
    "UnsupportedFidelity",
    "agentic_extension_available",
    "decide",
    "decision_schema_validator",
    "declared_zone",
    "enforce_sovereignty",
    "require_supported_fidelity",
    "require_valid_skill_bindings",
    "run_conformance_vectors",
    "validate_skill_bindings",
]
