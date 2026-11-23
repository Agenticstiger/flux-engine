# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The shipped telco bundle, executed end to end at its declared population size.

Every number here is computed from the documents. The world's declared size,
its lifecycle mix, its markets, the detector's window and depth, the journey's
reactions, the consent profile's vocabulary and the experiment's weights are all
read out of the bundle; nothing that came from reading the example is typed into
this file.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

import pytest

from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.clock import VirtualClock
from flux_engine.core.lrm import largest_remainder
from flux_engine.core.seedtree import SeedTree, derive_root
from flux_engine.core.trace import COMPONENT_OPS, TraceOp
from flux_engine.expr.attrs import build_attribute_model
from flux_engine.runtime import (
    arm_counts,
    assign_variants,
    compile_campaign,
    compile_detectors,
    compile_experiments,
    compile_journeys,
    emit_signals,
    read_out,
    requests_from_transitions,
    resolve_cast,
    run_campaign,
    run_detectors,
    run_journeys,
)
from flux_engine.verticals import get_pack
from flux_engine.world import PopulationSpec, TraceLog, materialise_population

from .conftest import load_bundle, requires_example

pytestmark = [pytest.mark.integration, requires_example]


def full_run(size: int) -> Dict[str, Any]:
    """Execute every stage of the shipped bundle at *size* subjects."""
    bundle = load_bundle()
    cast = resolve_cast(bundle.flux_docs)
    pack = get_pack(cast.world["spec"]["vertical"])
    model = build_attribute_model(bundle, pack, simulation_id=cast.simulation_id)

    world = {**cast.world, "spec": {**cast.world["spec"]}}
    world["spec"]["population"] = {**world["spec"]["population"], "size": size}
    spec = PopulationSpec.from_document(world)

    seed_tree = SeedTree(root=derive_root(b"H", 42))
    trace = TraceLog()
    store = materialise_population(
        world=world,
        personas=[bundle.flux_docs[i] for i in cast.ids("Persona")],
        pack=pack,
        seed_tree=seed_tree,
        trace=trace,
    )
    bounds = cast.simulation["spec"]["timeBounds"]
    clock = VirtualClock.from_time_bounds(bounds["start"], bounds["end"])
    events = emit_signals(
        cast=cast,
        world=spec,
        store=store,
        clock=clock,
        seed_tree=seed_tree,
        trace=trace,
        pack=pack,
    )
    detections = run_detectors(detectors=compile_detectors(cast, model), events=events, trace=trace)
    transitions = run_journeys(
        cast=cast,
        world=spec,
        journeys=compile_journeys(cast, model),
        events=events,
        store=store,
        seed_tree=seed_tree,
        trace=trace,
    )
    consent_id = cast.document(cast.ids("Campaign")[0])["spec"]["consentRef"]
    use_case = cast.document(consent_id)["spec"]["allowedUseCases"][0]
    policy = compile_campaign(cast, cast.ids("Campaign")[0], model, use_case=use_case)
    ledger = run_campaign(
        cast=cast,
        policy=policy,
        requests=requests_from_transitions(transitions),
        store=store,
        model=model,
        pack=pack,
        seed_tree=seed_tree,
        trace=trace,
    )
    experiment = compile_experiments(cast)[0]
    eligible = [o for o in store.ordinals if policy.segment(store.view(o, model.subject))]
    assignment = assign_variants(
        experiment=experiment, eligible=eligible, run_root=seed_tree.root, trace=trace
    )
    counts = arm_counts(assignment, frozenset(t.ordinal for t in transitions if t.success))
    return {
        "bundle": bundle,
        "cast": cast,
        "world": world,
        "spec": spec,
        "store": store,
        "trace": trace,
        "events": events,
        "detections": detections,
        "transitions": transitions,
        "policy": policy,
        "ledger": ledger,
        "experiment": experiment,
        "eligible": eligible,
        "assignment": assignment,
        "counts": counts,
        "seed_tree": seed_tree,
    }


@pytest.fixture(scope="module")
def small() -> Dict[str, Any]:
    return full_run(400)


@pytest.fixture(scope="module")
def declared_size() -> int:
    world = next(d for d in load_bundle().flux_docs.values() if d["kind"] == "World")
    return int(world["spec"]["population"]["size"])


# --- the whole population at its declared size -------------------------------


@pytest.mark.slow
def test_the_population_is_the_declared_size_split_by_the_declared_mix(
    declared_size: int,
) -> None:
    """The one run at the bundle's own ``population.size``."""
    run = full_run(declared_size)
    store = run["store"]
    spec = run["spec"]
    assert len(store) == declared_size
    expected = largest_remainder(declared_size, dict(spec.lifecycle_mix))
    assert store.cohort_counts() == {name: expected[name] for name in sorted(expected)}
    assert sum(store.cohort_counts().values()) == declared_size
    cohorts = run["trace"].of(TraceOp.COHORT)
    assert sum(record["count"] for record in cohorts) == declared_size
    assert len(run["trace"].of(TraceOp.MARKET)) == declared_size


# --- every stage produced something ------------------------------------------


def test_every_stage_of_the_shipped_bundle_produces_records(small: Dict[str, Any]) -> None:
    assert small["events"], "no signal arrived"
    assert small["detections"], "no detector fired"
    assert small["transitions"], "no journey stepped"
    assert small["ledger"].deliveries, "no delivery was allowed"
    assert small["ledger"].suppressions, "no delivery was refused"
    assert small["assignment"], "no subject was assigned to an arm"


def test_every_component_digest_is_present_and_distinct(small: Dict[str, Any]) -> None:
    digests = small["trace"].component_digests()
    assert set(digests) == set(COMPONENT_OPS)
    assert len({ref.value for ref in digests.values()}) == len(digests)


def test_the_whole_run_is_reproducible_byte_for_byte() -> None:
    first = full_run(300)
    second = full_run(300)
    assert canonical_bytes(list(first["trace"].records)) == canonical_bytes(
        list(second["trace"].records)
    )
    assert first["seed_tree"].digest() == second["seed_tree"].digest()


def test_no_traced_value_is_a_float(small: Dict[str, Any]) -> None:
    """The trace is canonicalised on the way in; this shows the pipe is closed."""
    canonical_bytes(list(small["trace"].records))


# --- the stages agree with each other ----------------------------------------


def test_every_detection_consumed_events_of_its_own_subject(small: Dict[str, Any]) -> None:
    for detection in small["detections"]:
        assert {event.ordinal for event in detection.events} == {detection.ordinal}


def test_every_transition_belongs_to_a_subject_that_emitted_a_trigger(
    small: Dict[str, Any],
) -> None:
    emitters = {event.ordinal for event in small["events"]}
    assert {t.ordinal for t in small["transitions"]} <= emitters


def test_every_delivery_is_to_a_segment_member(small: Dict[str, Any]) -> None:
    members = set(small["eligible"])
    assert {d.ordinal for d in small["ledger"].deliveries} <= members


def test_every_suppression_carries_a_reason_from_the_closed_set(
    small: Dict[str, Any],
) -> None:
    from flux_engine.runtime.campaign import SuppressionReason

    reasons = {row.reason for row in small["ledger"].suppressions}
    assert reasons <= set(SuppressionReason)
    assert set(small["ledger"].by_reason()) == {reason.value for reason in reasons}


def test_the_delivery_channel_is_the_campaigns_own(small: Dict[str, Any]) -> None:
    declared = small["cast"].document(small["policy"].campaign_id)["spec"]["channelRef"]
    assert {d.channel_id for d in small["ledger"].deliveries} == {declared}


def test_the_arms_partition_the_segment(small: Dict[str, Any]) -> None:
    counts = small["counts"]
    assert sum(arm.trials for arm in counts) == len(small["eligible"])
    weights = dict(small["experiment"].weights)
    expected = largest_remainder(len(small["eligible"]), weights)
    assert {arm.variant: arm.trials for arm in counts} == {
        name: expected[name] for name in sorted(expected)
    }


def test_the_readout_reads_only_the_raw_counts(small: Dict[str, Any]) -> None:
    rows = read_out(small["counts"], control=small["experiment"].control)
    assert rows
    for row in rows:
        assert row.control == small["experiment"].control
        assert isinstance(row.difference, Decimal)
        assert row.interval_low <= row.difference <= row.interval_high


def test_the_trace_holds_only_ops_the_spec_names(small: Dict[str, Any]) -> None:
    seen = {record["op"] for record in small["trace"].records}
    assert seen <= {op.value for op in TraceOp}
    expected = {
        TraceOp.COHORT.value,
        TraceOp.MARKET.value,
        TraceOp.TRAIT.value,
        TraceOp.SIGNAL.value,
        TraceOp.DETECT.value,
        TraceOp.TRANSITION.value,
        TraceOp.DELIVER.value,
        TraceOp.SUPPRESS.value,
        TraceOp.ASSIGN.value,
    }
    assert seen == expected


def test_every_materialised_seed_path_is_a_canonical_one(small: Dict[str, Any]) -> None:
    from flux_engine.core.seedtree import match_shape

    paths = small["seed_tree"].materialised_paths
    assert paths
    assert all(match_shape(path) is not None for path in paths)


def test_the_seed_tree_digest_covers_the_paths_the_run_touched(
    small: Dict[str, Any],
) -> None:
    from flux_engine.core.seedtree import seed_tree_digest

    tree = small["seed_tree"]
    assert tree.digest() == seed_tree_digest(tree.materialised_paths)
