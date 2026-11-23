# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Signal arrivals: derived ids, virtual time, the engagement dial, determinism."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import timezone
from decimal import Decimal
from typing import Dict

import pytest

from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.clock import format_rfc3339_ms
from flux_engine.core.seedtree import derive_identifier, signal_path, temperature_path
from flux_engine.core.trace import TraceOp
from flux_engine.runtime.signals import (
    ARRIVAL_DIAL,
    CLOUDEVENTS_ENVELOPE,
    DEFAULT_ARRIVAL_MODEL,
    EVENT_ID_LENGTH,
    ArrivalModel,
    emit_signals,
)

from .conftest import Staged, requires_example, stage

pytestmark = [pytest.mark.integration, requires_example]


def test_the_envelope_the_engine_builds_is_the_one_the_document_pins(staged: Staged) -> None:
    for _signal_id, document in staged.cast.of_kind("Signal"):
        assert document["spec"]["envelope"] == CLOUDEVENTS_ENVELOPE


def test_every_event_id_is_derived_from_its_seed_path_and_never_random(
    staged: Staged,
) -> None:
    """KERNEL-SPEC §14's "uuid4 event id" broken kernel, ruled out by construction."""
    for event in staged.events:
        expected = derive_identifier(
            staged.seed_tree.root,
            signal_path(
                staged.cast.simulation_id, event.signal_id, event.ordinal, event.occurrence
            ),
            length=EVENT_ID_LENGTH,
        )
        assert event.id == expected
    assert len({event.id for event in staged.events}) == len(staged.events)


def test_no_event_id_looks_like_a_uuid(staged: Staged) -> None:
    for event in staged.events:
        assert re.fullmatch(r"[0-9a-f]{32}", event.id)
        assert "-" not in event.id


def test_every_event_time_is_a_virtual_clock_instant(staged: Staged) -> None:
    """KERNEL-SPEC §14's "wall-clock timestamp in a traced op", likewise."""
    for event in staged.events:
        assert staged.clock.contains(event.t_ms)
        stamped = event.event.get_time()
        assert stamped is not None and stamped.tzinfo == timezone.utc
        assert format_rfc3339_ms(event.t_ms).startswith(stamped.strftime("%Y-%m-%dT%H:%M:%S"))


def test_context_attributes_come_from_the_signal_document(staged: Staged) -> None:
    documents = dict(staged.cast.of_kind("Signal"))
    for event in staged.events:
        spec = documents[event.signal_id]["spec"]
        assert event.event.get_source() == spec["source"]
        assert event.event.get_type() == spec["type"]
        assert event.event.get_dataschema() == spec["dataschema"]
        assert event.event.get_subject() == staged.store.identifier(event.ordinal)


def test_events_arrive_in_the_scheduler_key_order(staged: Staged) -> None:
    keys = [(event.t_ms, event.ordinal) for event in staged.events]
    assert keys == sorted(keys)


def test_one_signal_op_is_traced_per_event_in_arrival_order(staged: Staged) -> None:
    traced = staged.trace.of(TraceOp.SIGNAL)
    assert len(traced) == len(staged.events)
    assert [record["t"] for record in traced] == [event.t_ms for event in staged.events]
    assert [record["payloadDigest"] for record in traced] == [
        event.payload_digest for event in staged.events
    ]


def test_the_payload_digest_is_a_digest_of_the_payload(staged: Staged) -> None:
    from flux_engine.core.digest import digest_of_document

    for event in staged.events[:20]:
        expected = digest_of_document(dict(event.payload), subject="x").value
        assert event.payload_digest == expected


def test_payload_fields_are_the_ones_the_vertical_pack_declares(staged: Staged) -> None:
    declared = staged.pack.signal_attributes
    for event in staged.events[:20]:
        assert set(event.payload) == set(declared[event.signal_id])


def test_a_burst_is_ordered_and_inside_the_time_bounds(staged: Staged) -> None:
    by_subject: Dict[int, list] = {}
    for event in staged.events:
        by_subject.setdefault(event.ordinal, []).append(event)
    bursts = [events for events in by_subject.values() if len(events) > 1]
    assert bursts, "no subject emitted twice; the burst model is untested"
    for burst in bursts:
        burst.sort(key=lambda e: e.occurrence)
        times = [event.t_ms for event in burst]
        assert times == sorted(times)
        assert len(set(times)) == len(times), "two occurrences shared an instant"
        assert all(staged.clock.contains(t) for t in times)


def test_the_emitter_share_is_close_to_the_authored_rate(staged: Staged) -> None:
    emitters = {event.ordinal for event in staged.events}
    share = Decimal(len(emitters)) / Decimal(len(staged.store))
    assert abs(share - DEFAULT_ARRIVAL_MODEL.emitter_share) < Decimal("0.06")


def test_a_frozen_engagement_dial_makes_the_emitter_set_exact_and_draws_nothing() -> None:
    """§6: ``t = 0`` consumes no draw, so no temperature path is materialised."""
    staged = stage(size=120)
    frozen = deepcopy(staged.world)
    frozen["spec"]["temperature"][ARRIVAL_DIAL] = 0
    from flux_engine.world import PopulationSpec, TraceLog

    spec = PopulationSpec.from_document(frozen)
    trace = TraceLog()
    events = emit_signals(
        cast=staged.cast,
        world=spec,
        store=staged.store,
        clock=staged.clock,
        seed_tree=staged.seed_tree,
        trace=trace,
        pack=staged.pack,
    )
    emitters = sorted({event.ordinal for event in events})
    expected = int(len(staged.store) * DEFAULT_ARRIVAL_MODEL.emitter_share)
    assert len(emitters) == expected
    assert emitters == list(range(expected))
    materialised = set(staged.seed_tree.materialised_paths)
    assert temperature_path(staged.cast.simulation_id, ARRIVAL_DIAL, 0, 0) not in materialised


def test_the_run_is_reproducible_and_seed_sensitive() -> None:
    first = stage(size=120, seed=7)
    same = stage(size=120, seed=7)
    other = stage(size=120, seed=8)
    assert canonical_bytes(list(first.trace.records)) == canonical_bytes(list(same.trace.records))
    assert canonical_bytes(list(first.trace.records)) != canonical_bytes(list(other.trace.records))


def test_changing_the_contract_digest_moves_every_event() -> None:
    """§3: "Changing the contract changes every stream"."""
    first = stage(size=120, seam=b"H")
    other = stage(size=120, seam=b"H2")
    assert [e.id for e in first.events] != [e.id for e in other.events]


def test_restricting_the_eligible_set_restricts_the_emitters(staged: Staged) -> None:
    from flux_engine.world import TraceLog

    eligible = tuple(range(0, len(staged.store), 3))
    events = emit_signals(
        cast=staged.cast,
        world=staged.spec,
        store=staged.store,
        clock=staged.clock,
        seed_tree=staged.seed_tree,
        trace=TraceLog(),
        pack=staged.pack,
        eligible=eligible,
    )
    assert {event.ordinal for event in events} <= set(eligible)


def test_the_event_view_type_checks_against_the_event_scope(staged: Staged) -> None:
    for event in staged.events[:10]:
        scope = staged.model.event_scope(event.signal_id)
        view = event.view(scope)
        assert view["data.amount"] == event.payload["amount"]
        assert view["subject"] == staged.store.identifier(event.ordinal)


def test_an_arrival_model_refuses_impossible_parameters() -> None:
    with pytest.raises(ValueError):
        ArrivalModel(Decimal("1.5"), Decimal(1), Decimal(1000))
    with pytest.raises(ValueError):
        ArrivalModel(Decimal("0.5"), Decimal(-1), Decimal(1000))
    with pytest.raises(ValueError):
        ArrivalModel(Decimal("0.5"), Decimal(1), Decimal(0))


def test_a_single_occurrence_model_emits_exactly_one_event_per_emitter(
    staged: Staged,
) -> None:
    from flux_engine.world import TraceLog

    events = emit_signals(
        cast=staged.cast,
        world=staged.spec,
        store=staged.store,
        clock=staged.clock,
        seed_tree=staged.seed_tree,
        trace=TraceLog(),
        pack=staged.pack,
        arrival=ArrivalModel(Decimal("0.2"), Decimal(0), Decimal("300000")),
    )
    assert {event.occurrence for event in events} == {0}


def test_a_signal_that_declares_no_dataschema_emits_none(staged: Staged) -> None:
    from copy import deepcopy

    from flux_engine.runtime.cast import Cast
    from flux_engine.world import TraceLog

    documents = deepcopy(dict(staged.cast.documents))
    signal_id = staged.cast.ids("Signal")[0]
    del documents[signal_id]["spec"]["dataschema"]
    patched = Cast(
        simulation_id=staged.cast.simulation_id,
        world_id=staged.cast.world_id,
        documents=documents,
        stream_ordinals=staged.cast.stream_ordinals,
        references=staged.cast.references,
    )
    events = emit_signals(
        cast=patched,
        world=staged.spec,
        store=staged.store,
        clock=staged.clock,
        seed_tree=staged.fresh_tree(),
        trace=TraceLog(),
        pack=staged.pack,
    )
    assert events
    assert {event.event.get_dataschema() for event in events} == {None}
