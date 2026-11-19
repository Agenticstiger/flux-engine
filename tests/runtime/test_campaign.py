# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Campaign delivery: the gate order, the ledger, and the virtual-time bucket."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, List, Sequence

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import campaign_path
from flux_engine.core.trace import TraceOp
from flux_engine.runtime.campaign import (
    DEFAULT_OPT_IN_SHARE,
    DeliveryRequest,
    SuppressionReason,
    compile_campaign,
    requests_from_transitions,
    run_campaign,
)
from flux_engine.world import TraceLog

from .conftest import Staged, requires_example

# --- the gates ---------------------------------------------------------------


pytestmark = [pytest.mark.integration, requires_example]


def policy(staged: Staged, **kwargs: Any):
    return compile_campaign(staged.cast, staged.cast.ids("Campaign")[0], staged.model, **kwargs)


def deliver(staged: Staged, pol, requests: Sequence[DeliveryRequest], **kwargs: Any):
    log = TraceLog()
    seed_tree = kwargs.pop("seed_tree", None) or staged.fresh_tree()
    ledger = run_campaign(
        cast=staged.cast,
        policy=pol,
        requests=list(requests),
        store=staged.store,
        model=staged.model,
        pack=staged.pack,
        seed_tree=seed_tree,
        trace=log,
        **kwargs,
    )
    return ledger, log, seed_tree


def all_subjects(staged: Staged, t_ms: int = None) -> List[DeliveryRequest]:
    start = t_ms if t_ms is not None else staged.clock.start_ms
    return [DeliveryRequest(ordinal=o, t_ms=start) for o in staged.store.ordinals]


def test_the_compiled_policy_mirrors_the_documents(staged: Staged) -> None:
    pol = policy(staged)
    campaign = staged.cast.document(pol.campaign_id)["spec"]
    consent = staged.cast.document(campaign["consentRef"])["spec"]
    channel = staged.cast.document(campaign["channelRef"])["spec"]
    treatment = staged.cast.document(campaign["treatmentRef"])["spec"]
    assert pol.segment_id == campaign["segmentRef"]
    assert pol.channel_id == campaign["channelRef"]
    assert pol.rate_limit_per_second == channel["rateLimitPerSecond"]
    assert pol.allowed_jurisdictions == frozenset(consent["allowedJurisdictions"])
    assert pol.requires_explicit_opt_in is consent["requiresExplicitOptIn"]
    assert pol.allowed_use_cases == frozenset(consent["allowedUseCases"])
    assert pol.denied_use_cases == frozenset(consent["deniedUseCases"])
    assert pol.use_case == treatment["actionType"]


def test_an_unlisted_use_case_fails_closed(staged: Staged) -> None:
    """The Treatment's actionType is an action, not a purpose the profile lists."""
    pol = policy(staged)
    assert pol.use_case not in pol.allowed_use_cases
    assert pol.use_case_refusal() is SuppressionReason.USE_CASE_NOT_ALLOWED


def test_a_denied_use_case_is_named_as_denied(staged: Staged) -> None:
    consent = staged.cast.document(
        staged.cast.document(staged.cast.ids("Campaign")[0])["spec"]["consentRef"]
    )["spec"]
    pol = policy(staged, use_case=consent["deniedUseCases"][0])
    assert pol.use_case_refusal() is SuppressionReason.USE_CASE_DENIED


def test_an_allowed_use_case_passes_the_gate(staged: Staged) -> None:
    consent = staged.cast.document(
        staged.cast.document(staged.cast.ids("Campaign")[0])["spec"]["consentRef"]
    )["spec"]
    pol = policy(staged, use_case=consent["allowedUseCases"][0])
    assert pol.use_case_refusal() is None


def allowed_policy(staged: Staged, **kwargs: Any):
    consent = staged.cast.document(
        staged.cast.document(staged.cast.ids("Campaign")[0])["spec"]["consentRef"]
    )["spec"]
    return policy(staged, use_case=consent["allowedUseCases"][0], **kwargs)


def test_every_refusal_writes_exactly_one_ledger_row_and_one_trace_op(
    staged: Staged,
) -> None:
    requests = all_subjects(staged)
    ledger, log, _ = deliver(staged, allowed_policy(staged), requests)
    assert len(ledger.deliveries) + len(ledger.suppressions) == len(requests)
    assert len(log.of(TraceOp.DELIVER)) == len(ledger.deliveries)
    assert len(log.of(TraceOp.SUPPRESS)) == len(ledger.suppressions)


def test_a_subject_outside_the_segment_is_refused_first(staged: Staged) -> None:
    pol = allowed_policy(staged)
    outside = [
        o
        for o in staged.store.ordinals
        if not pol.segment(staged.store.view(o, staged.model.subject))
    ]
    assert outside
    ledger, _, _ = deliver(
        staged, pol, [DeliveryRequest(o, staged.clock.start_ms) for o in outside]
    )
    assert {row.reason for row in ledger.suppressions} == {SuppressionReason.NOT_IN_SEGMENT}


def test_a_market_outside_the_allowed_jurisdictions_is_refused(staged: Staged) -> None:
    pol = allowed_policy(staged)
    narrowed = type(pol)(**{**vars(pol), "allowed_jurisdictions": frozenset({"XX"})})
    members = [
        o for o in staged.store.ordinals if pol.segment(staged.store.view(o, staged.model.subject))
    ]
    assert members
    ledger, _, _ = deliver(
        staged, narrowed, [DeliveryRequest(o, staged.clock.start_ms) for o in members]
    )
    assert {row.reason for row in ledger.suppressions} == {
        SuppressionReason.JURISDICTION_NOT_ALLOWED
    }


def test_a_market_the_vertical_does_not_map_is_an_unanswerable_question(
    staged: Staged,
) -> None:
    from flux_engine.expr.attrs import VerticalPack

    empty = VerticalPack(name="unmapped", signal_attributes=staged.pack.signal_attributes)
    pol = allowed_policy(staged)
    members = [
        o for o in staged.store.ordinals if pol.segment(staged.store.view(o, staged.model.subject))
    ]
    log = TraceLog()
    with pytest.raises(FluxError) as raised:
        run_campaign(
            cast=staged.cast,
            policy=pol,
            requests=[DeliveryRequest(members[0], staged.clock.start_ms)],
            store=staged.store,
            model=staged.model,
            pack=empty,
            seed_tree=staged.fresh_tree(),
            trace=log,
        )
    assert "FLX-4110" in str(raised.value)


def test_opt_in_is_drawn_on_the_campaign_path_and_only_once_per_subject(
    staged: Staged,
) -> None:
    pol = allowed_policy(staged)
    members = [
        o for o in staged.store.ordinals if pol.segment(staged.store.view(o, staged.model.subject))
    ]
    assert members
    twice = [DeliveryRequest(members[0], staged.clock.start_ms) for _ in range(2)]
    ledger, _, seed_tree = deliver(staged, pol, twice)
    outcomes = {row.reason for row in ledger.suppressions} | {
        "delivered" for _ in ledger.deliveries
    }
    assert len(outcomes) == 1, "one subject got two different consent answers in one run"
    path = campaign_path(staged.cast.simulation_id, pol.campaign_id, members[0])
    assert path in set(seed_tree.materialised_paths)


def test_a_profile_that_needs_no_opt_in_draws_nothing(staged: Staged) -> None:
    pol = allowed_policy(staged)
    relaxed = type(pol)(**{**vars(pol), "requires_explicit_opt_in": False})
    members = [
        o for o in staged.store.ordinals if pol.segment(staged.store.view(o, staged.model.subject))
    ]
    _, _, seed_tree = deliver(
        staged, relaxed, [DeliveryRequest(o, staged.clock.start_ms) for o in members]
    )
    assert not [p for p in seed_tree.materialised_paths if p[2] == "campaign"]


def test_the_opt_in_share_is_close_to_the_declared_rate(staged: Staged) -> None:
    pol = allowed_policy(staged)
    relaxed = type(pol)(**{**vars(pol), "allowed_jurisdictions": frozenset()})
    requests = all_subjects(staged)
    ledger, _, _ = deliver(staged, relaxed, requests)
    refused = [r for r in ledger.suppressions if r.reason is SuppressionReason.NO_EXPLICIT_OPT_IN]
    considered = len(ledger.deliveries) + len(refused)
    assert considered
    share = Decimal(len(ledger.deliveries)) / Decimal(considered)
    assert abs(share - DEFAULT_OPT_IN_SHARE) < Decimal("0.15")


def test_the_rate_limit_bites_when_every_request_lands_at_one_instant(
    staged: Staged,
) -> None:
    pol = allowed_policy(staged)
    relaxed = type(pol)(
        **{
            **vars(pol),
            "allowed_jurisdictions": frozenset(),
            "requires_explicit_opt_in": False,
            "rate_limit_per_second": 2,
        }
    )
    requests = all_subjects(staged)
    ledger, _, _ = deliver(staged, relaxed, requests)
    assert len(ledger.deliveries) == 2
    assert ledger.by_reason()[SuppressionReason.RATE_LIMITED.value] > 0


def test_a_channel_with_no_rate_limit_never_rate_limits(staged: Staged) -> None:
    pol = allowed_policy(staged)
    relaxed = type(pol)(
        **{
            **vars(pol),
            "allowed_jurisdictions": frozenset(),
            "requires_explicit_opt_in": False,
            "rate_limit_per_second": None,
        }
    )
    ledger, _, _ = deliver(staged, relaxed, all_subjects(staged))
    assert SuppressionReason.RATE_LIMITED.value not in ledger.by_reason()


def test_requests_are_served_in_the_scheduler_key_order(staged: Staged) -> None:
    pol = allowed_policy(staged)
    relaxed = type(pol)(
        **{
            **vars(pol),
            "allowed_jurisdictions": frozenset(),
            "requires_explicit_opt_in": False,
        }
    )
    shuffled = list(reversed(all_subjects(staged)))
    ledger, log, _ = deliver(staged, relaxed, shuffled)
    ordinals = [record["ordinal"] for record in log.records]
    assert ordinals == sorted(ordinals)


def test_the_ledger_summarises_by_reason(staged: Staged) -> None:
    ledger, _, _ = deliver(staged, allowed_policy(staged), all_subjects(staged))
    counts = ledger.by_reason()
    assert list(counts) == sorted(counts)
    assert sum(counts.values()) == len(ledger.suppressions)


def test_requests_can_be_built_from_journey_transitions(staged: Staged) -> None:
    from flux_engine.runtime.journey import compile_journeys, run_journeys

    moved = run_journeys(
        cast=staged.cast,
        world=staged.spec,
        journeys=compile_journeys(staged.cast, staged.model),
        events=staged.events,
        store=staged.store,
        seed_tree=staged.fresh_tree(),
        trace=TraceLog(),
    )
    requests = requests_from_transitions(moved)
    assert [(r.ordinal, r.t_ms) for r in requests] == [(t.ordinal, t.t_ms) for t in moved]


def test_no_request_produces_an_empty_ledger(staged: Staged) -> None:
    ledger, log, _ = deliver(staged, allowed_policy(staged), [])
    assert ledger.deliveries == () and ledger.suppressions == ()
    assert len(log) == 0


def test_the_use_case_gate_is_reached_after_the_consent_gates(staged: Staged) -> None:
    """The default policy's use case is unlisted; it must be the reported reason."""
    pol = policy(staged)
    relaxed = type(pol)(
        **{
            **vars(pol),
            "allowed_jurisdictions": frozenset(),
            "requires_explicit_opt_in": False,
        }
    )
    members = [
        o for o in staged.store.ordinals if pol.segment(staged.store.view(o, staged.model.subject))
    ]
    assert members
    ledger, _, _ = deliver(
        staged, relaxed, [DeliveryRequest(o, staged.clock.start_ms) for o in members]
    )
    assert ledger.deliveries == ()
    assert {row.reason for row in ledger.suppressions} == {SuppressionReason.USE_CASE_NOT_ALLOWED}


def test_the_opt_in_gate_is_reached_before_the_use_case_gate(staged: Staged) -> None:
    """A subject failing both gates is refused for the one the docstring orders first.

    The gate table in :mod:`flux_engine.runtime.campaign` is governance-first:
    consent (rows 2-3) is a fact about the *subject*, the use case (row 4) is a
    property of the *campaign*. A subject with no opt-in must therefore be
    reported as ``no_explicit_opt_in`` even when the campaign's use case would
    have refused every subject anyway -- otherwise one unlisted use case would
    erase every consent fact the run established.
    """
    pol = policy(staged)  # the shipped Treatment's actionType: unlisted
    assert pol.requires_explicit_opt_in
    assert pol.use_case_refusal() is SuppressionReason.USE_CASE_NOT_ALLOWED
    relaxed = type(pol)(**{**vars(pol), "allowed_jurisdictions": frozenset()})
    members = [
        o for o in staged.store.ordinals if pol.segment(staged.store.view(o, staged.model.subject))
    ]
    assert members
    ledger, _, _ = deliver(
        staged, relaxed, [DeliveryRequest(o, staged.clock.start_ms) for o in members]
    )
    reasons = {row.reason for row in ledger.suppressions}
    assert SuppressionReason.NO_EXPLICIT_OPT_IN in reasons
    assert reasons == {
        SuppressionReason.NO_EXPLICIT_OPT_IN,
        SuppressionReason.USE_CASE_NOT_ALLOWED,
    }


def test_the_use_case_gate_is_reached_before_the_rate_limit(staged: Staged) -> None:
    """Cost is spent last: a refused campaign must not consume a channel token.

    Every request below lands at one instant against a one-per-second channel,
    so a rate limit reached before the use-case gate would report
    ``rate_limited`` for all but the first.
    """
    pol = policy(staged)
    refused = type(pol)(
        **{
            **vars(pol),
            "allowed_jurisdictions": frozenset(),
            "requires_explicit_opt_in": False,
            "rate_limit_per_second": 1,
        }
    )
    members = [
        o for o in staged.store.ordinals if pol.segment(staged.store.view(o, staged.model.subject))
    ]
    assert len(members) > 1
    ledger, _, _ = deliver(
        staged, refused, [DeliveryRequest(o, staged.clock.start_ms) for o in members]
    )
    assert ledger.deliveries == ()
    assert {row.reason for row in ledger.suppressions} == {SuppressionReason.USE_CASE_NOT_ALLOWED}
