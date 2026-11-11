# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Campaign delivery: four consent gates, a virtual-time token bucket, a ledger.

A delivery attempt passes through five checks, in this order, and the **first**
one that refuses it decides its reason code:

======  ==============================  ===================================================
order   reason code                     the check
======  ==============================  ===================================================
1       ``not_in_segment``              ``Campaign.spec.segmentRef`` -> ``Segment.spec.query``
2       ``jurisdiction_not_allowed``    the subject's market's jurisdiction is not in
                                        ``ConsentProfile.spec.allowedJurisdictions``
3       ``no_explicit_opt_in``          ``requiresExplicitOptIn`` and this subject has not
4       ``use_case_denied`` /           the campaign's use case is in ``deniedUseCases``,
        ``use_case_not_allowed``        or is not in ``allowedUseCases``
5       ``rate_limited``                ``Channel.spec.rateLimitPerSecond``
======  ==============================  ===================================================

The order is governance-first and cost-last: a subject outside the segment was
never a candidate, and a subject the consent profile forbids must be refused
before a channel budget is spent on it. **Every** refusal writes a
:class:`SuppressionRow` and a §10 ``suppress`` op; there is no silent drop.

**The use case.** FLUX 0.5.0 binds no use case to a ``Campaign``:
``ConsentProfile.spec.allowedUseCases`` is a vocabulary "aligned with FLUID
agentPolicy.allowedUseCases", and the ``Treatment`` carries an ``actionType``
which is an action, not a purpose. :func:`compile_campaign` therefore takes the
use case from the caller and falls back to the Treatment's ``actionType``, and a
use case the profile does not list is refused (``use_case_not_allowed``) rather
than allowed. Failing closed is the only defensible default: an unlisted purpose
is an unproven basis for contacting a person.

**The token bucket** is deterministic and integer-only. It refills in *virtual*
time at ``rateLimitPerSecond`` tokens per 1 000 ms, is capped at one second's
worth, and is accounted in thousandths of a token so that no division and no
float ever enters the decision. Requests are served in KERNEL-SPEC §7's key
order, so two runs of one plan spend the budget on the same subjects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Protocol, Sequence, Tuple

from flux_engine.core.clock import Phase, Scheduler
from flux_engine.core.rng import bernoulli
from flux_engine.core.seedtree import SeedTree, campaign_path
from flux_engine.core.trace import TraceOp
from flux_engine.expr.attrs import AttributeModel, VerticalPack
from flux_engine.expr.compile import Predicate, compile_source
from flux_engine.expr.types import nfc
from flux_engine.runtime.cast import Cast, strip_range
from flux_engine.runtime.jurisdiction import jurisdiction_of
from flux_engine.world.population import SubjectStore
from flux_engine.world.tracing import TraceLog

__all__ = [
    "DEFAULT_OPT_IN_SHARE",
    "CampaignPolicy",
    "Delivery",
    "DeliveryLedger",
    "DeliveryRequest",
    "SuppressionReason",
    "SuppressionRow",
    "Timed",
    "TokenBucket",
    "compile_campaign",
    "requests_from_transitions",
    "run_campaign",
]

#: Share of subjects carrying an explicit opt-in, drawn on §3's campaign path
#: ("delivery-side randomness"). Engine-owned: consent is a fact about a real
#: person and no FLUX document declares it, so the twin draws it.
DEFAULT_OPT_IN_SHARE = Decimal("0.80")


class SuppressionReason(str, Enum):
    """The closed set of reasons a delivery is refused. Values enter the trace."""

    NOT_IN_SEGMENT = "not_in_segment"
    JURISDICTION_NOT_ALLOWED = "jurisdiction_not_allowed"
    NO_EXPLICIT_OPT_IN = "no_explicit_opt_in"
    USE_CASE_DENIED = "use_case_denied"
    USE_CASE_NOT_ALLOWED = "use_case_not_allowed"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class DeliveryRequest:
    """One attempt to reach one subject at one virtual instant."""

    ordinal: int
    t_ms: int


@dataclass(frozen=True)
class Delivery:
    """An allowed delivery."""

    campaign_id: str
    ordinal: int
    t_ms: int
    channel_id: str


@dataclass(frozen=True)
class SuppressionRow:
    """One refusal, with the gate that refused it."""

    campaign_id: str
    ordinal: int
    t_ms: int
    reason: SuppressionReason


@dataclass(frozen=True)
class DeliveryLedger:
    """Everything one campaign run decided, in decision order."""

    deliveries: Tuple[Delivery, ...]
    suppressions: Tuple[SuppressionRow, ...]

    def by_reason(self) -> Dict[str, int]:
        """Suppression counts per reason code, in reason-code order."""
        counts: Dict[str, int] = {}
        for row in self.suppressions:
            counts[row.reason.value] = counts.get(row.reason.value, 0) + 1
        return {reason: counts[reason] for reason in sorted(counts)}


class TokenBucket:
    """A deterministic token bucket in virtual time, accounted in milli-tokens."""

    __slots__ = ("_capacity", "_rate", "_tokens", "_last_ms")

    def __init__(self, rate_per_second: int, *, start_ms: int) -> None:
        if rate_per_second < 1:
            raise ValueError("rateLimitPerSecond must be >= 1")
        self._rate = rate_per_second
        self._capacity = rate_per_second * 1000
        self._tokens = self._capacity
        self._last_ms = start_ms

    @property
    def tokens(self) -> int:
        """Milli-tokens currently held (1 000 milli-tokens = one delivery)."""
        return self._tokens

    def take(self, t_ms: int) -> bool:
        """Refill to *t_ms* and spend one token; ``False`` when the budget is dry.

        A request at an instant before the last one served does not refill (the
        elapsed term is floored at zero), so an out-of-order caller cannot mint
        tokens; :func:`run_campaign` serves requests in §7 key order regardless.
        """
        elapsed = max(t_ms - self._last_ms, 0)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_ms = max(self._last_ms, t_ms)
        if self._tokens < 1000:
            return False
        self._tokens -= 1000
        return True


@dataclass(frozen=True)
class CampaignPolicy:
    """A ``Campaign`` with its segment compiled and its consent profile read."""

    campaign_id: str
    segment_id: str
    segment: Predicate
    channel_id: str
    rate_limit_per_second: Optional[int]
    consent_id: str
    allowed_jurisdictions: FrozenSet[str]
    requires_explicit_opt_in: bool
    allowed_use_cases: FrozenSet[str]
    denied_use_cases: FrozenSet[str]
    use_case: str
    treatment_id: str
    stream_ordinal: int
    opt_in_share: Decimal = DEFAULT_OPT_IN_SHARE

    def use_case_refusal(self) -> Optional[SuppressionReason]:
        """The reason this campaign's use case is refused, or ``None``.

        Computed once per campaign rather than per subject: the use case is a
        property of the campaign, so every subject gets the same answer and a
        per-subject evaluation would only invite it to drift.
        """
        if self.use_case in self.denied_use_cases:
            return SuppressionReason.USE_CASE_DENIED
        if self.allowed_use_cases and self.use_case not in self.allowed_use_cases:
            return SuppressionReason.USE_CASE_NOT_ALLOWED
        return None


def compile_campaign(
    cast: Cast,
    campaign_id: str,
    model: AttributeModel,
    *,
    use_case: Optional[str] = None,
    opt_in_share: Decimal = DEFAULT_OPT_IN_SHARE,
) -> CampaignPolicy:
    """Compile one ``Campaign`` of *cast* into an executable policy."""
    document = cast.document(campaign_id)
    spec = document.get("spec", {})
    segment_id = strip_range(spec.get("segmentRef", ""))
    segment = cast.document(segment_id)
    channel_id = strip_range(spec.get("channelRef", ""))
    channel = cast.document(channel_id)
    consent_id = strip_range(spec.get("consentRef", ""))
    consent = cast.document(consent_id).get("spec", {})
    treatment_id = strip_range(spec.get("treatmentRef", ""))
    treatment = cast.document(treatment_id).get("spec", {})
    rate_limit = channel.get("spec", {}).get("rateLimitPerSecond")
    return CampaignPolicy(
        campaign_id=strip_range(campaign_id),
        segment_id=segment_id,
        segment=compile_source(str(segment.get("spec", {}).get("query", "true")), model.subject),
        channel_id=channel_id,
        rate_limit_per_second=int(rate_limit) if rate_limit is not None else None,
        consent_id=consent_id,
        allowed_jurisdictions=frozenset(
            nfc(str(code)) for code in consent.get("allowedJurisdictions", []) or []
        ),
        requires_explicit_opt_in=bool(consent.get("requiresExplicitOptIn", False)),
        allowed_use_cases=frozenset(
            nfc(str(name)) for name in consent.get("allowedUseCases", []) or []
        ),
        denied_use_cases=frozenset(
            nfc(str(name)) for name in consent.get("deniedUseCases", []) or []
        ),
        use_case=nfc(str(use_case if use_case is not None else treatment.get("actionType", ""))),
        treatment_id=treatment_id,
        stream_ordinal=cast.stream_ordinal(campaign_id),
        opt_in_share=opt_in_share,
    )


class Timed(Protocol):
    """Anything that happened to one subject at one instant."""

    @property
    def ordinal(self) -> int:
        """The subject's population ordinal."""

    @property
    def t_ms(self) -> int:
        """The virtual instant it happened at."""


def requests_from_transitions(transitions: Sequence[Timed]) -> Tuple[DeliveryRequest, ...]:
    """Turn anything carrying ``ordinal`` and ``t_ms`` into delivery requests.

    The intervention a Journey names and the treatment a Campaign delivers are
    the same document in the shipped bundle, so a journey transition is the
    natural driver of a delivery attempt. Taking the structural
    :class:`Timed` protocol rather than importing
    :class:`~flux_engine.runtime.journey.Transition` keeps this module usable
    from a detector-driven or scheduled campaign too, and keeps the import graph
    between the two stages one-directional.
    """
    return tuple(DeliveryRequest(ordinal=item.ordinal, t_ms=item.t_ms) for item in transitions)


@dataclass
class _OptIn:
    """Per-subject opt-in, drawn at most once per subject per campaign."""

    seed_tree: SeedTree
    sim_id: str
    campaign_id: str
    share: Decimal
    _decided: Dict[int, bool] = field(default_factory=dict)

    def holds(self, ordinal: int) -> bool:
        """Whether subject *ordinal* has opted in.

        Memoised: the campaign stream is one stream per subject, so drawing
        twice would consume a second word and could answer the same question
        two different ways for one subject in one run.
        """
        known = self._decided.get(ordinal)
        if known is None:
            stream = self.seed_tree.stream(campaign_path(self.sim_id, self.campaign_id, ordinal))
            known = bernoulli(stream, self.share)
            self._decided[ordinal] = known
        return known


def run_campaign(
    *,
    cast: Cast,
    policy: CampaignPolicy,
    requests: Sequence[DeliveryRequest],
    store: SubjectStore,
    model: AttributeModel,
    pack: VerticalPack,
    seed_tree: SeedTree,
    trace: TraceLog,
) -> DeliveryLedger:
    """Decide every request, tracing one ``deliver`` or ``suppress`` op each."""
    scheduler: Scheduler[DeliveryRequest] = Scheduler()
    for request in requests:
        scheduler.schedule(
            t_ms=request.t_ms,
            phase=Phase.CAMPAIGN,
            entity_ordinal=request.ordinal,
            stream_ordinal=policy.stream_ordinal,
            payload=request,
        )

    bucket = (
        TokenBucket(policy.rate_limit_per_second, start_ms=min(r.t_ms for r in requests))
        if policy.rate_limit_per_second is not None and requests
        else None
    )
    opt_in = _OptIn(
        seed_tree=seed_tree,
        sim_id=cast.simulation_id,
        campaign_id=policy.campaign_id,
        share=policy.opt_in_share,
    )
    use_case_refusal = policy.use_case_refusal()

    deliveries: List[Delivery] = []
    suppressions: List[SuppressionRow] = []
    for item in scheduler.drain():
        request = item.payload
        reason = _refuse(
            request=request,
            policy=policy,
            store=store,
            model=model,
            pack=pack,
            opt_in=opt_in,
            use_case_refusal=use_case_refusal,
            bucket=bucket,
        )
        if reason is None:
            deliveries.append(
                Delivery(
                    campaign_id=policy.campaign_id,
                    ordinal=request.ordinal,
                    t_ms=request.t_ms,
                    channel_id=policy.channel_id,
                )
            )
            trace.record(
                TraceOp.DELIVER,
                t=request.t_ms,
                campaignId=policy.campaign_id,
                ordinal=request.ordinal,
                channelId=policy.channel_id,
            )
        else:
            suppressions.append(
                SuppressionRow(
                    campaign_id=policy.campaign_id,
                    ordinal=request.ordinal,
                    t_ms=request.t_ms,
                    reason=reason,
                )
            )
            trace.record(
                TraceOp.SUPPRESS,
                t=request.t_ms,
                campaignId=policy.campaign_id,
                ordinal=request.ordinal,
                reasonCode=reason.value,
            )
    return DeliveryLedger(deliveries=tuple(deliveries), suppressions=tuple(suppressions))


def _refuse(
    *,
    request: DeliveryRequest,
    policy: CampaignPolicy,
    store: SubjectStore,
    model: AttributeModel,
    pack: VerticalPack,
    opt_in: _OptIn,
    use_case_refusal: Optional[SuppressionReason],
    bucket: Optional[TokenBucket],
) -> Optional[SuppressionReason]:
    """The first gate that refuses *request*, or ``None`` when all five pass."""
    if not policy.segment(store.view(request.ordinal, model.subject)):
        return SuppressionReason.NOT_IN_SEGMENT
    if policy.allowed_jurisdictions:
        jurisdiction = jurisdiction_of(pack, store.market(request.ordinal))
        if jurisdiction not in policy.allowed_jurisdictions:
            return SuppressionReason.JURISDICTION_NOT_ALLOWED
    if policy.requires_explicit_opt_in and not opt_in.holds(request.ordinal):
        return SuppressionReason.NO_EXPLICIT_OPT_IN
    if use_case_refusal is not None:
        return use_case_refusal
    if bucket is not None and not bucket.take(request.t_ms):
        return SuppressionReason.RATE_LIMITED
    return None
