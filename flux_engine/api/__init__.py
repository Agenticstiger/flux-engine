# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The governed extension contract -- Protocols and frozen dataclasses only.

Everything in ``flux_engine.api`` is the surface a third-party
implementation targets and an in-process consumer imports against.
Everything outside it is internal and moves without notice.

Three properties hold across the whole package, and the surface test in
``tests/api/test_api_surface.py`` pins each of them.

**No logic.** Every callable here is a Protocol member with an empty body,
a property that reads a field, or the one place a typed failure is raised.
There is nothing to execute and therefore nothing to disagree with the
kernel about.

**Frozen.** Every dataclass is ``frozen=True``, and every plural field is
a tuple or a read-only mapping view. A record that can be edited after it
is handed over is not a record.

**No float, anywhere.** Numbers are :class:`~decimal.Decimal` or ``int``
(KERNEL-SPEC section 2.1), because anything on this surface can end up
inside a digest.

**Nothing collapses two answers into one.**
:class:`~flux_engine.api.verdict.VerifyVerdict` reports reproducibility and
validity as separate fields and offers no way to reduce them to a single
boolean; :class:`~flux_engine.api.calibration.Scorecard` reports a missing
grade as missing.

The package takes no runtime import on any kernel module. The digest type
every port speaks in is carried here structurally, as
:class:`~flux_engine.api.digests.DigestRef`.

``__api_version__`` is ``"0.1"``. It moves independently of the engine's
own version: a change that removes or renames anything exported here is a
breaking change to this number, whatever the engine's release does.
"""

from __future__ import annotations

from flux_engine.api.accuracy import (
    AccuracyGate,
    AccuracyOutcome,
    AccuracyVerdict,
    ObservedReadout,
    PredictionRecord,
)
from flux_engine.api.agentic import ModelPort, SkillInvocation, SkillProposal, SkillRunner
from flux_engine.api.calibration import Calibrator, Credibility, Provenance, Scorecard
from flux_engine.api.claims import Claim, ClaimBasis, ClaimType
from flux_engine.api.digests import DIGEST_DOMAINS, DigestRef
from flux_engine.api.emit import EmitHeader, EmitSink
from flux_engine.api.expressions import ExpressionEvaluator, ExpressionScope
from flux_engine.api.fidelity import (
    FidelityProver,
    FidelityStatement,
    FidelityVerifier,
    ProofSystem,
)
from flux_engine.api.lineage import DatasetRef, LineageEmitter, LineageEvent, LineageEventType
from flux_engine.api.stages import (
    ApplyFailed,
    EngineError,
    PlanFailed,
    ReplayFailed,
    RunFailed,
    ShipFailed,
    StageName,
    StageResult,
    ValidateFailed,
    VerifyFailed,
)
from flux_engine.api.verdict import Reproducibility, Validity, VerifyVerdict

#: The ports this build declares and does not itself implement or inject.
#: Not in ``__all__``: it is a fact *about* the surface, not a member of
#: it, and the frozen surface is Protocols and records.
#:
#: Every other Protocol here has an in-tree implementation that satisfies
#: it structurally and a place the engine actually uses one -- ``EmitSink``
#: has the sinks, ``ModelPort`` the stub port, ``Calibrator`` the declared
#: calibrator, ``LineageEmitter`` the ndjson emitter, ``ProofSystem`` the
#: ``none`` system. These four have neither: nothing in the engine
#: implements their shape, and there is no parameter through which a
#: consumer could inject one. The real expression compiler, for example, is
#: a pair of module-level functions whose shape differs from
#: :class:`~flux_engine.api.expressions.ExpressionEvaluator`'s method.
#:
#: They are published as reserved, not as available: a port with no
#: injection point is a shape to design against, not an extension point to
#: ship against. ``tests/api`` pins this set, so a port that gains an
#: implementation and a caller has to leave it deliberately.
RESERVED_PORTS = frozenset(
    {
        "AccuracyGate",
        "ExpressionEvaluator",
        "FidelityProver",
        "SkillRunner",
    }
)

__api_version__ = "0.1"

__all__ = [
    "__api_version__",
    # digests
    "DIGEST_DOMAINS",
    "DigestRef",
    # expressions
    "ExpressionEvaluator",
    "ExpressionScope",
    # emit
    "EmitHeader",
    "EmitSink",
    # calibration
    "Calibrator",
    "Credibility",
    "Provenance",
    "Scorecard",
    # outcome accuracy
    "AccuracyGate",
    "AccuracyOutcome",
    "AccuracyVerdict",
    "ObservedReadout",
    "PredictionRecord",
    # claims
    "Claim",
    "ClaimBasis",
    "ClaimType",
    # fidelity
    "FidelityProver",
    "FidelityStatement",
    "FidelityVerifier",
    "ProofSystem",
    # agentic tier
    "ModelPort",
    "SkillInvocation",
    "SkillProposal",
    "SkillRunner",
    # lineage
    "DatasetRef",
    "LineageEmitter",
    "LineageEvent",
    "LineageEventType",
    # verify readout
    "Reproducibility",
    "Validity",
    "VerifyVerdict",
    # stage results
    "StageName",
    "StageResult",
    "EngineError",
    "ValidateFailed",
    "PlanFailed",
    "ApplyFailed",
    "RunFailed",
    "VerifyFailed",
    "ShipFailed",
    "ReplayFailed",
]
