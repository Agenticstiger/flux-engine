# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The run's cast: the closure of a Simulation's references, and its rules."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

import pytest

from flux_engine.core.clock import document_ordinals
from flux_engine.core.errors import FluxError
from flux_engine.runtime.cast import MODULE_ONLY_KINDS, resolve_cast, strip_range

from .conftest import requires_example

pytestmark = [pytest.mark.integration, requires_example]


def test_a_semver_range_is_stripped_from_a_reference() -> None:
    assert strip_range("module-payment-recovery@^2.1") == "module-payment-recovery"
    assert strip_range("plain") == "plain"


def test_the_cast_is_the_closure_and_nothing_else(bundle: Any, docs: Dict[str, Any]) -> None:
    """Documents the Simulation cannot reach stay out; computed, not listed."""
    cast = resolve_cast(docs)
    reachable = set(cast.documents)
    unreached = set(docs) - reachable
    assert unreached, "the example has documents outside the run; the test would pass vacuously"
    for doc_id in unreached:
        assert doc_id not in cast.documents


def test_the_cast_holds_exactly_one_world(docs: Dict[str, Any]) -> None:
    cast = resolve_cast(docs)
    assert cast.world_id == strip_range(cast.simulation["spec"]["worldRef"])


def test_every_reference_of_every_cast_document_resolves(docs: Dict[str, Any]) -> None:
    cast = resolve_cast(docs)
    for reference in cast.references:
        assert reference.target in cast.documents


def test_journeys_and_detectors_arrive_through_module_resources(docs: Dict[str, Any]) -> None:
    cast = resolve_cast(docs)
    resources = {
        strip_range(ref)
        for _module_id, module in cast.of_kind("Module")
        for ref in module["spec"]["resources"]
    }
    for kind in MODULE_ONLY_KINDS:
        found = cast.ids(kind)
        assert found, f"the example has no {kind}; the rule is untested"
        assert set(found) <= resources


def test_a_journey_referenced_by_any_other_route_is_refused(docs: Dict[str, Any]) -> None:
    journey_id = next(i for i, d in docs.items() if d["kind"] == "Journey")
    simulation = next(d for d in docs.values() if d["kind"] == "Simulation")
    simulation["spec"]["segmentRefs"] = [journey_id]
    with pytest.raises(FluxError) as raised:
        resolve_cast(docs)
    assert "FLX-2301" in str(raised.value)


def test_a_reference_of_the_wrong_kind_is_refused(docs: Dict[str, Any]) -> None:
    simulation = next(d for d in docs.values() if d["kind"] == "Simulation")
    channel_id = next(i for i, d in docs.items() if d["kind"] == "Channel")
    simulation["spec"]["personaRefs"] = [channel_id]
    with pytest.raises(FluxError) as raised:
        resolve_cast(docs)
    assert "not a Persona" in str(raised.value)


def test_a_dangling_reference_names_where_it_came_from(docs: Dict[str, Any]) -> None:
    simulation = next(d for d in docs.values() if d["kind"] == "Simulation")
    simulation["spec"]["segmentRefs"] = ["seg-does-not-exist"]
    with pytest.raises(FluxError) as raised:
        resolve_cast(docs)
    assert "spec.segmentRefs[0]" in str(raised.value)


def test_stream_ordinals_are_taken_over_the_whole_bundle(docs: Dict[str, Any]) -> None:
    """§7: "the canonical ordinal of the producing document in the bundle"."""
    cast = resolve_cast(docs)
    assert cast.stream_ordinals == document_ordinals(docs)
    assert len(cast.stream_ordinals) > len(cast.documents)


def test_an_ordinal_is_stable_when_an_unrelated_document_is_added(docs: Dict[str, Any]) -> None:
    """Adding a document later in sort order must not move an earlier ordinal."""
    before = resolve_cast(docs)
    extended = deepcopy(docs)
    extended["zzz-unrelated"] = {"kind": "Catalog", "id": "zzz-unrelated", "spec": {"items": []}}
    after = resolve_cast(extended)
    for doc_id in before.documents:
        assert before.stream_ordinal(doc_id) == after.stream_ordinal(doc_id)


def test_an_unknown_stream_ordinal_fails_by_name(docs: Dict[str, Any]) -> None:
    cast = resolve_cast(docs)
    with pytest.raises(FluxError):
        cast.stream_ordinal("not-in-the-bundle")


def test_a_document_outside_the_cast_cannot_be_looked_up(docs: Dict[str, Any]) -> None:
    cast = resolve_cast(docs)
    outside = next(i for i in docs if i not in cast.documents)
    with pytest.raises(FluxError) as raised:
        cast.document(outside)
    assert "not in the run's cast" in str(raised.value)


def test_naming_a_simulation_is_required_when_there_are_several(docs: Dict[str, Any]) -> None:
    simulation = next(d for d in docs.values() if d["kind"] == "Simulation")
    twin = deepcopy(simulation)
    twin["id"] = "sim-second"
    docs["sim-second"] = twin
    with pytest.raises(FluxError) as raised:
        resolve_cast(docs)
    assert "name one with simulation_id=" in str(raised.value)
    assert resolve_cast(docs, simulation_id="sim-second").simulation_id == "sim-second"


def test_naming_a_simulation_that_is_not_there_is_refused(docs: Dict[str, Any]) -> None:
    with pytest.raises(FluxError):
        resolve_cast(docs, simulation_id="sim-nope")


def test_of_kind_is_sorted_and_ids_agree_with_it(docs: Dict[str, Any]) -> None:
    cast = resolve_cast(docs)
    for kind in ("Signal", "Treatment", "Persona"):
        assert [doc_id for doc_id, _ in cast.of_kind(kind)] == list(cast.ids(kind))
        assert list(cast.ids(kind)) == sorted(cast.ids(kind))


def test_the_module_boundary_is_the_reason_given_when_both_rules_are_broken(
    docs: Dict[str, Any],
) -> None:
    """A Journey in ``segmentRefs`` breaks two rules; the useful one is reported."""
    journey_id = next(i for i, d in docs.items() if d["kind"] == "Journey")
    simulation = next(d for d in docs.values() if d["kind"] == "Simulation")
    simulation["spec"]["segmentRefs"] = [journey_id]
    with pytest.raises(FluxError) as raised:
        resolve_cast(docs)
    assert "only through moduleRefs" in str(raised.value)


def test_a_missing_spec_block_yields_no_references(docs: Dict[str, Any]) -> None:
    """A document that is not schema-valid must not crash the closure walk."""
    from flux_engine.runtime.cast import _edges

    assert _edges("x", {"kind": "Journey"}) == ()
    assert _edges("x", {"kind": "Campaign", "spec": {}}) == ()
    assert _edges("x", {"kind": "Detector", "spec": {"pattern": "not-a-list"}}) == ()
    assert _edges("x", {"kind": "Catalog", "spec": {}}) == ()


def test_a_cast_with_no_world_is_refused(docs: Dict[str, Any]) -> None:
    simulation = next(d for d in docs.values() if d["kind"] == "Simulation")
    world_id = simulation["spec"]["worldRef"]
    del simulation["spec"]["worldRef"]
    del docs[world_id]
    with pytest.raises(FluxError) as raised:
        resolve_cast(docs)
    assert "must hold one" in str(raised.value)
