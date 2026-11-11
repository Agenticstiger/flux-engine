# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Signal, detector, journey, campaign, experiment, and temperature executors.

Each executor is a pure function of the documents, the population and the seed
tree; each records its decisions as ``flux-trace/1`` ops (KERNEL-SPEC §10) on a
:class:`~flux_engine.world.tracing.TraceLog`; and each orders its work through
the §7 scheduler at its own phase, so the trace is in execution order rather
than in the order a loop happened to build it.

The stages, in scheduler-phase order::

    from flux_engine.runtime import (
        compile_campaign, compile_detectors, compile_journeys, compile_experiments,
        emit_signals, resolve_cast, run_campaign, run_detectors, run_journeys,
    )

    cast   = resolve_cast(bundle.flux_docs)                      # who is in the run
    events = emit_signals(...)                                   # Phase.SIGNAL
    fired  = run_detectors(detectors=..., events=events, ...)    # Phase.DETECT
    moved  = run_journeys(..., events=events, ...)               # Phase.JOURNEY
    ledger = run_campaign(..., requests=requests_from_transitions(moved), ...)
    arms   = assign_variants(...)                                # Phase.EXPERIMENT

:mod:`flux_engine.runtime.temperature` is KERNEL-SPEC §6 and is shared by the
signal and journey stages; :mod:`flux_engine.runtime.jurisdiction` and
:mod:`flux_engine.runtime.payloads` are the two v0.1 stopgap tables the FLUX 0.6
RFC is expected to move into the spec.
"""

from __future__ import annotations

from flux_engine.runtime.campaign import (
    DEFAULT_OPT_IN_SHARE,
    CampaignPolicy,
    Delivery,
    DeliveryLedger,
    DeliveryRequest,
    SuppressionReason,
    SuppressionRow,
    Timed,
    TokenBucket,
    compile_campaign,
    requests_from_transitions,
    run_campaign,
)
from flux_engine.runtime.cast import Cast, Reference, resolve_cast, strip_range
from flux_engine.runtime.detector import (
    CompiledDetector,
    Detection,
    PatternElement,
    compile_detectors,
    run_detectors,
)
from flux_engine.runtime.errors import FLX_2301, FLX_2302, FLX_2303, FLX_4110
from flux_engine.runtime.experiment import (
    DEFAULT_CONFIDENCE,
    ArmCounts,
    CompiledExperiment,
    ExperimentStatistics,
    ReadoutRow,
    TwoProportionReadout,
    arm_counts,
    assign_variants,
    compile_experiments,
    default_readout,
    read_out,
)
from flux_engine.runtime.journey import (
    DEFAULT_STEP_BUDGET,
    REACTION_DIAL,
    CompiledJourney,
    Reaction,
    Transition,
    compile_journeys,
    negated_outcome,
    run_journeys,
)
from flux_engine.runtime.jurisdiction import MARKET_JURISDICTIONS, jurisdiction_map, jurisdiction_of
from flux_engine.runtime.payloads import PAYLOAD_FAMILIES, payload_families
from flux_engine.runtime.signals import (
    ARRIVAL_DIAL,
    CLOUDEVENTS_ENVELOPE,
    DEFAULT_ARRIVAL_MODEL,
    ArrivalModel,
    SignalEvent,
    emit_signals,
)
from flux_engine.runtime.temperature import Dial, Partition, Realisation, partition

__all__ = [
    "ARRIVAL_DIAL",
    "CLOUDEVENTS_ENVELOPE",
    "DEFAULT_ARRIVAL_MODEL",
    "DEFAULT_CONFIDENCE",
    "DEFAULT_OPT_IN_SHARE",
    "DEFAULT_STEP_BUDGET",
    "FLX_2301",
    "FLX_2302",
    "FLX_2303",
    "FLX_4110",
    "MARKET_JURISDICTIONS",
    "PAYLOAD_FAMILIES",
    "REACTION_DIAL",
    "ArmCounts",
    "ArrivalModel",
    "CampaignPolicy",
    "Cast",
    "CompiledDetector",
    "CompiledExperiment",
    "CompiledJourney",
    "Delivery",
    "DeliveryLedger",
    "DeliveryRequest",
    "Detection",
    "Dial",
    "ExperimentStatistics",
    "Partition",
    "PatternElement",
    "Reaction",
    "ReadoutRow",
    "Realisation",
    "Reference",
    "SignalEvent",
    "SuppressionReason",
    "SuppressionRow",
    "Timed",
    "TokenBucket",
    "Transition",
    "TwoProportionReadout",
    "arm_counts",
    "assign_variants",
    "compile_campaign",
    "compile_detectors",
    "compile_experiments",
    "compile_journeys",
    "default_readout",
    "emit_signals",
    "jurisdiction_map",
    "jurisdiction_of",
    "negated_outcome",
    "partition",
    "payload_families",
    "read_out",
    "requests_from_transitions",
    "resolve_cast",
    "run_campaign",
    "run_detectors",
    "run_journeys",
    "strip_range",
]
