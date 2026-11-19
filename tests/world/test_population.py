# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Population materialisation over the shipped bundle and over miniature worlds."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any, Dict, List

import pytest

from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.errors import FluxError
from flux_engine.core.lrm import largest_remainder
from flux_engine.core.seedtree import market_path, subject_products_path, subject_trait_path
from flux_engine.core.trace import TraceOp
from flux_engine.expr.attrs import build_attribute_model
from flux_engine.expr.compile import compile_source
from flux_engine.verticals import get_pack
from flux_engine.world import (
    NORMAL_BOUND_SIGMAS,
    PersonaSpec,
    PopulationSpec,
    SubjectStore,
    TraceLog,
    materialise_population,
    subject_id,
)

from .conftest import load_bundle, requires_example, resized, tree

pytestmark = [pytest.mark.integration, requires_example]

SMALL = 240


def materialise(world: Dict[str, Any], personas: List[Dict[str, Any]], **kwargs: Any):
    log = TraceLog()
    seed_tree = kwargs.pop("seed_tree", None) or tree()
    store = materialise_population(
        world=world,
        personas=personas,
        pack=get_pack(world["spec"]["vertical"]),
        seed_tree=seed_tree,
        trace=log,
        **kwargs,
    )
    return store, log, seed_tree


@pytest.fixture()
def small(telco_world: Dict[str, Any]) -> Dict[str, Any]:
    return resized(telco_world, SMALL)


# --- cohorts (KERNEL-SPEC §5) ----------------------------------------------


def test_the_cohorts_are_the_largest_remainder_of_the_declared_mix(
    telco_world: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    """Both sides computed: the expected split is LRM of the document's own mix."""
    world = resized(telco_world, SMALL)
    spec = PopulationSpec.from_document(world)
    expected = largest_remainder(spec.size, dict(spec.lifecycle_mix))
    store, log, _ = materialise(world, [telco_persona])
    assert store.cohort_counts() == {name: expected[name] for name in sorted(expected)}
    assert sum(store.cohort_counts().values()) == spec.size


def test_the_cohorts_sum_to_the_declared_population_size(
    telco_world: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    """The shipped world's declared size, read from the document, not typed here."""
    declared = telco_world["spec"]["population"]["size"]
    spec = PopulationSpec.from_document(telco_world)
    assert sum(largest_remainder(declared, dict(spec.lifecycle_mix)).values()) == declared


def test_changing_the_seed_never_changes_a_cohort_size(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    first, _, _ = materialise(small, [telco_persona], seed_tree=tree(seed=1))
    second, _, _ = materialise(small, [telco_persona], seed_tree=tree(seed=999))
    assert first.cohort_counts() == second.cohort_counts()


def test_one_cohort_op_per_lifecycle_in_nfc_sorted_order(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    _, log, _ = materialise(small, [telco_persona])
    cohorts = log.of(TraceOp.COHORT)
    assert [record["lifecycle"] for record in cohorts] == sorted(
        small["spec"]["population"]["lifecycleMix"]
    )


def test_cohorts_are_laid_down_as_contiguous_blocks(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    store, _, _ = materialise(small, [telco_persona])
    seen = [store.lifecycle(0)]
    for ordinal in store.ordinals:
        if store.lifecycle(ordinal) != seen[-1]:
            seen.append(store.lifecycle(ordinal))
    assert seen == sorted(set(seen)), "a lifecycle appears in two separate blocks"


# --- determinism ------------------------------------------------------------


def test_two_runs_of_one_plan_produce_byte_identical_populations(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    first, first_log, _ = materialise(small, [telco_persona])
    second, second_log, _ = materialise(small, [telco_persona])
    assert canonical_bytes(list(first_log.records)) == canonical_bytes(list(second_log.records))
    assert [s.id for s in first] == [s.id for s in second]


def test_a_different_seed_moves_the_drawn_values_but_not_the_shape(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    first, first_log, _ = materialise(small, [telco_persona], seed_tree=tree(seed=1))
    second, second_log, _ = materialise(small, [telco_persona], seed_tree=tree(seed=2))
    assert first.cohort_counts() == second.cohort_counts()
    assert (
        first_log.component_digests()["population"] != second_log.component_digests()["population"]
    )


def test_a_different_contract_digest_moves_every_stream(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    """§3: "Changing the contract changes every stream"."""
    first, _, _ = materialise(small, [telco_persona], seed_tree=tree(seam=b"H"))
    second, _, _ = materialise(small, [telco_persona], seed_tree=tree(seam=b"H2"))
    assert [s.id for s in first] != [s.id for s in second]


def test_the_population_is_independent_of_the_order_personas_were_listed(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    other = deepcopy(telco_persona)
    other["id"] = "persona-other"
    forward, forward_log, _ = materialise(small, [telco_persona, other])
    backward, backward_log, _ = materialise(small, [other, telco_persona])
    assert canonical_bytes(list(forward_log.records)) == canonical_bytes(list(backward_log.records))
    assert [s.persona for s in forward] == [s.persona for s in backward]


# --- seed paths -------------------------------------------------------------


def test_only_the_paths_a_draw_needs_are_materialised(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    """A scalar trait is a constant, so its path must never be derived."""
    store, _, seed_tree = materialise(small, [telco_persona])
    world_id = small["id"]
    materialised = {path for path in seed_tree.materialised_paths}
    assert market_path(world_id, 0) in materialised
    assert subject_products_path(world_id, 0) in materialised
    assert subject_trait_path(world_id, 0, "priceSensitivity") in materialised
    assert subject_trait_path(world_id, 0, "digitalFluency") not in materialised


def test_the_number_of_materialised_paths_is_a_function_of_the_declarations(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    store, _, seed_tree = materialise(small, [telco_persona])
    drawn_traits = [
        name
        for name, declared in telco_persona["spec"]["traits"].items()
        if isinstance(declared, dict)
    ]
    per_subject = 1 + 1 + len(drawn_traits)  # market + products + each drawn trait
    assert len(seed_tree.materialised_paths) == per_subject * len(store)


def test_subject_ids_are_derived_and_unique(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    store, _, seed_tree = materialise(small, [telco_persona])
    assert len({s.id for s in store}) == len(store)
    assert store.identifier(0) == subject_id(seed_tree.root, small["id"], 0)


def test_the_subject_id_label_is_not_a_stream_path(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    """It is an identifier label; deriving a stream from it must be impossible."""
    _, _, seed_tree = materialise(small, [telco_persona])
    with pytest.raises(FluxError) as raised:
        seed_tree.stream(("world", small["id"], "subject", 0, "id"))
    assert "FLX-2201" in str(raised.value)


# --- traits -----------------------------------------------------------------


def test_a_scalar_trait_is_the_declared_constant_for_every_subject(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    store, _, _ = materialise(small, [telco_persona])
    declared = telco_persona["spec"]["traits"]["digitalFluency"]
    column = set(store.column("traits.digitalFluency"))
    assert column == {Decimal(repr(declared)).quantize(Decimal("0.000001"))}


def test_a_categorical_trait_only_takes_declared_levels(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    store, _, _ = materialise(small, [telco_persona])
    levels = set(telco_persona["spec"]["traits"]["priceSensitivity"]["levels"])
    assert set(store.column("traits.priceSensitivity")) <= levels


def test_a_bounded_normal_trait_respects_its_declared_minimum(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    declared = telco_persona["spec"]["traits"]["monthlySpend"]
    store, _, _ = materialise(small, [telco_persona])
    lowest = min(store.column("traits.monthlySpend"))
    assert lowest >= Decimal(repr(declared["min"]))


def test_an_unbounded_normal_gets_the_engine_owned_sigma_window(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    persona = deepcopy(telco_persona)
    del persona["spec"]["traits"]["monthlySpend"]["min"]
    spec = PersonaSpec.from_document(persona)
    family = spec.traits["monthlySpend"]
    mean = Decimal(repr(persona["spec"]["traits"]["monthlySpend"]["mean"]))
    stddev = Decimal(repr(persona["spec"]["traits"]["monthlySpend"]["stddev"]))
    assert family.lo == mean - NORMAL_BOUND_SIGMAS * stddev
    assert family.hi == mean + NORMAL_BOUND_SIGMAS * stddev


def test_every_drawn_value_is_traced_and_every_constant_is_not(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    store, log, _ = materialise(small, [telco_persona])
    traced = {record["name"] for record in log.of(TraceOp.TRAIT)}
    assert "digitalFluency" not in traced
    assert {"priceSensitivity", "monthlySpend"} <= traced
    assert set(store.derived_names) <= traced


def test_a_trait_the_engine_cannot_sample_is_refused(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    persona = deepcopy(telco_persona)
    persona["spec"]["traits"]["monthlySpend"]["dist"] = "lognormal"
    with pytest.raises(FluxError) as raised:
        materialise(small, [persona])
    assert "FLX-5040" in str(raised.value)


def test_a_normal_whose_bounds_cross_is_refused(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    persona = deepcopy(telco_persona)
    persona["spec"]["traits"]["monthlySpend"]["max"] = -1
    with pytest.raises(FluxError):
        materialise(small, [persona])


def test_a_negative_stddev_is_refused(small: Dict[str, Any], telco_persona: Dict[str, Any]) -> None:
    persona = deepcopy(telco_persona)
    persona["spec"]["traits"]["monthlySpend"]["stddev"] = -1
    with pytest.raises(FluxError):
        materialise(small, [persona])


def test_a_subject_missing_a_trait_another_persona_declares_carries_null(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    other = deepcopy(telco_persona)
    other["id"] = "persona-other"
    other["spec"]["traits"] = {"onlyHere": 0.5}
    store, _, _ = materialise(small, [telco_persona, other])
    column = store.column("traits.onlyHere")
    assert None in column and Decimal("0.500000") in column


# --- markets and worldview --------------------------------------------------


def test_markets_come_only_from_the_declared_list_and_are_all_used(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    declared = set(small["spec"]["population"]["markets"])
    store, log, _ = materialise(small, [telco_persona])
    assert set(store.column("market")) == declared
    assert len(log.of(TraceOp.MARKET)) == len(store)


def test_a_world_with_no_markets_carries_none_and_traces_nothing(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    world = deepcopy(small)
    del world["spec"]["population"]["markets"]
    store, log, _ = materialise(world, [telco_persona])
    assert set(store.column("market")) == {None}
    assert log.of(TraceOp.MARKET) == ()
    assert "subject.market" not in store.subject(0).canonical_values()


def test_worldview_values_are_carried_but_not_traced(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    store, log, _ = materialise(small, [telco_persona])
    declared = telco_persona["spec"]["worldview"]
    assert set(store.column("worldview.riskTolerance")) == {declared["riskTolerance"]}
    assert {"riskTolerance"} & {record["name"] for record in log.of(TraceOp.TRAIT)} == set()


# --- the store ---------------------------------------------------------------


def test_the_store_is_columnar_and_every_column_has_one_row_per_subject(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    store, _, _ = materialise(small, [telco_persona])
    for name in ("id", "lifecycle", "persona", "market"):
        assert len(store.column(name)) == len(store)
    for name in store.trait_names:
        assert len(store.column(f"traits.{name}")) == len(store)
    for name in store.derived_names:
        assert len(store.column(name)) == len(store)


def test_an_unknown_column_fails_by_name(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    store, _, _ = materialise(small, [telco_persona])
    with pytest.raises(KeyError, match="no column named"):
        store.column("nope")


def test_a_column_of_the_wrong_length_is_refused_at_construction() -> None:
    with pytest.raises(FluxError) as raised:
        SubjectStore(
            ids=["a", "b"],
            lifecycles=["x"],
            personas=["p", "p"],
            markets=[None, None],
            traits={},
            worldview={},
            derived={},
        )
    assert "expected 2" in str(raised.value)


def test_a_trait_column_of_the_wrong_length_is_refused_at_construction() -> None:
    with pytest.raises(FluxError):
        SubjectStore(
            ids=["a"],
            lifecycles=["x"],
            personas=["p"],
            markets=[None],
            traits={"t": []},
            worldview={},
            derived={},
        )


def test_iteration_is_by_ordinal(small: Dict[str, Any], telco_persona: Dict[str, Any]) -> None:
    store, _, _ = materialise(small, [telco_persona])
    assert [s.ordinal for s in store] == list(range(len(store)))


# --- the expression view -----------------------------------------------------


def test_the_shipped_segment_query_runs_against_the_materialised_population(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    """``lifecycle == 'atrisk' && products.has('tv')``, read from the bundle."""
    bundle = load_bundle()
    segment = next(d for d in bundle.flux_docs.values() if d["kind"] == "Segment")
    model = build_attribute_model(bundle, get_pack("telco"))
    predicate = compile_source(segment["spec"]["query"], model.subject)
    store, _, _ = materialise(small, [telco_persona])
    members = [o for o in store.ordinals if predicate(store.view(o, model.subject))]
    assert members, "the shipped query selects nobody; the population is not telco-shaped"
    for ordinal in members:
        assert store.lifecycle(ordinal) == "atrisk"
        assert "tv" in store.subject(ordinal).derived["products"]


def test_a_view_drops_paths_the_model_does_not_declare(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    bundle = load_bundle()
    model = build_attribute_model(bundle, get_pack("telco"))
    other = deepcopy(telco_persona)
    other["id"] = "persona-other"
    other["spec"]["traits"] = {"undeclared": 0.5}
    store, _, _ = materialise(small, [telco_persona, other])
    view = store.view(0, model.subject)
    assert "subject.traits.undeclared" not in view


# --- the spec reader ---------------------------------------------------------


def test_a_population_of_zero_is_refused(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    world = resized(small, 0)
    with pytest.raises(FluxError) as raised:
        PopulationSpec.from_document(world)
    assert "FLX-5040" in str(raised.value)


def test_the_dial_reader_defaults_an_undeclared_dial(telco_world: Dict[str, Any]) -> None:
    spec = PopulationSpec.from_document(telco_world)
    assert spec.dial("engagement") == Decimal(
        repr(telco_world["spec"]["temperature"]["engagement"])
    )
    assert spec.dial("undeclared") == Decimal(0)


def test_a_simulation_with_no_persona_is_refused(small: Dict[str, Any]) -> None:
    with pytest.raises(FluxError) as raised:
        materialise(small, [])
    assert "FLX-5040" in str(raised.value)


def test_each_subjects_trait_ops_are_in_nfc_sorted_name_order(
    small: Dict[str, Any], telco_persona: Dict[str, Any]
) -> None:
    """KERNEL-SPEC §7: "iteration over any mapping is by sorted key".

    Nothing about a *value* would move if the traits were visited in another
    order -- each trait draws on its own ``("world", id, "subject", ordinal,
    "trait", name)`` stream, so the values are order-independent by
    construction. What does move is the order the ``trait`` ops enter the log,
    and ``populationDigest`` is taken over that order. So the order is the thing
    to pin: per subject, the drawn traits in NFC-sorted name order, then the
    vertical-derived attributes in theirs -- the pass order this module's
    docstring states.
    """
    from flux_engine.world.derived import derivation_for

    spec = PersonaSpec.from_document(telco_persona)
    drawn = tuple(name for name in spec.trait_names if spec.traits[name].word_cost)
    derived = derivation_for(small["spec"]["vertical"]).names
    expected = list(drawn) + list(derived)
    assert expected == sorted(drawn) + sorted(derived), "the fixture itself must be sorted"
    assert drawn and derived

    _store, log, _ = materialise(small, [telco_persona])
    per_subject: Dict[int, List[str]] = {}
    for record in log.of(TraceOp.TRAIT):
        per_subject.setdefault(record["ordinal"], []).append(record["name"])
    assert per_subject
    for ordinal, names in per_subject.items():
        assert names == expected, f"subject {ordinal} traced its traits out of order"
