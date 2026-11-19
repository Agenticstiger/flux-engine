# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``attrmodel/1`` builder and the vertical packs (EXPR-SPEC §3)."""

from __future__ import annotations

from typing import Any, Dict, Mapping

import pytest

from flux_engine.expr.attrs import (
    ATTR_MODEL_VERSION,
    CONTEXT_ATTRIBUTES,
    WORLDVIEW_ENUMS,
    build_attribute_model,
)
from flux_engine.expr.check import check
from flux_engine.expr.errors import FluxExprError
from flux_engine.expr.parser import parse
from flux_engine.expr.types import NUMBER, SET_STRING, STRING, Scope, TypeKind
from flux_engine.verticals import PACKS, get_pack, pack_names
from flux_engine.verticals.telco import TELCO


class FakeBundle:
    """The smallest thing satisfying ``BundleLike``: a mapping of documents."""

    def __init__(self, documents: Mapping[str, Any]) -> None:
        self._documents = dict(documents)

    @property
    def flux_docs(self) -> Mapping[str, Any]:
        return self._documents


def documents(**overrides: Any) -> Dict[str, Any]:
    """A minimal, schema-shaped bundle: one World, Persona, Signal, Simulation."""
    base: Dict[str, Any] = {
        "w": {
            "kind": "World",
            "id": "w",
            "spec": {
                "seed": 1,
                "vertical": "telco",
                "population": {
                    "size": 10,
                    "lifecycleMix": {"new": 0.5, "churned": 0.5},
                    "markets": ["mkt-a", "mkt-b"],
                },
            },
        },
        "p": {
            "kind": "Persona",
            "id": "p",
            "spec": {
                "traits": {
                    "scalarTrait": 0.6,
                    "normalTrait": {"dist": "normal", "mean": 1.0, "stddev": 1.0},
                    "categoricalTrait": {
                        "dist": "categorical",
                        "levels": {"low": 0.5, "high": 0.5},
                    },
                },
                "worldview": {"riskTolerance": "low", "trendFollowing": 0.3},
            },
        },
        "sig": {
            "kind": "Signal",
            "id": "sig",
            "spec": {"source": "urn:x", "type": "com.x.y"},
        },
        "sim": {
            "kind": "Simulation",
            "id": "sim",
            "spec": {"worldRef": "w", "personaRefs": ["p"], "signalRefs": ["sig"]},
        },
    }
    base.update(overrides)
    return base


def build(**overrides: Any) -> Any:
    return build_attribute_model(FakeBundle(documents(**overrides)), TELCO)


# --- the subject scope --------------------------------------------------------


@pytest.mark.unit
def test_the_subject_scope_carries_the_population_attributes() -> None:
    subject = build().subject
    assert subject.scope is Scope.SUBJECT
    assert subject.type_of("id") == STRING
    assert subject.type_of("ordinal") == NUMBER
    assert subject.type_of("lifecycle").levels == ("churned", "new")
    assert subject.type_of("market").levels == ("mkt-a", "mkt-b")
    assert subject.type_of("persona").levels == ("p",)


@pytest.mark.unit
def test_traits_are_typed_from_their_rfc01_shape() -> None:
    subject = build().subject
    assert subject.type_of("traits.scalarTrait") == NUMBER
    assert subject.type_of("traits.normalTrait") == NUMBER
    assert subject.type_of("traits.categoricalTrait").levels == ("high", "low")


@pytest.mark.unit
def test_worldview_riskTolerance_is_the_schema_declared_enum() -> None:
    """Resolution: the FLUX schema's closed set wins over §3's "number" default."""
    subject = build().subject
    assert subject.type_of("worldview.riskTolerance").levels == WORLDVIEW_ENUMS["riskTolerance"]
    assert subject.type_of("worldview.trendFollowing") == NUMBER
    assert check(parse("worldview.riskTolerance == 'high'"), subject) is not None
    with pytest.raises(FluxExprError) as raised:
        check(parse("worldview.riskTolerance > 3"), subject)
    assert raised.value.code == "FLX-6005"


@pytest.mark.unit
def test_the_vertical_pack_contributes_its_own_subject_attributes() -> None:
    subject = build().subject
    for name, declared in TELCO.subject_attributes.items():
        assert subject.type_of(name) == declared
    assert subject.type_of("products") == SET_STRING


@pytest.mark.unit
def test_bare_names_are_sugar_for_the_canonical_subject_path() -> None:
    """EXPR-SPEC §3: "Bare ``lifecycle``, ``products``, ... are sugar for ``subject.<name>``"."""
    subject = build().subject
    for canonical in subject.attributes:
        assert canonical.startswith("subject.")
        assert subject.resolve(canonical.removeprefix("subject.")) == canonical


@pytest.mark.unit
def test_a_persona_that_is_not_referenced_contributes_no_traits() -> None:
    extra = {
        "other": {
            "kind": "Persona",
            "id": "other",
            "spec": {"traits": {"unreferenced": 0.5}},
        }
    }
    subject = build(**extra).subject
    assert subject.resolve("traits.unreferenced") is None
    assert subject.type_of("persona").levels == ("p",)


@pytest.mark.unit
def test_two_personas_disagreeing_about_a_trait_type_is_a_build_failure() -> None:
    extra = {
        "q": {
            "kind": "Persona",
            "id": "q",
            "spec": {
                "traits": {
                    "categoricalTrait": 0.5,
                }
            },
        },
        "sim": {
            "kind": "Simulation",
            "id": "sim",
            "spec": {"worldRef": "w", "personaRefs": ["p", "q"], "signalRefs": ["sig"]},
        },
    }
    with pytest.raises(ValueError) as raised:
        build(**extra)
    assert "categoricalTrait" in str(raised.value)


@pytest.mark.unit
def test_two_personas_agreeing_on_a_categorical_trait_union_their_levels() -> None:
    extra = {
        "q": {
            "kind": "Persona",
            "id": "q",
            "spec": {
                "traits": {
                    "categoricalTrait": {
                        "dist": "categorical",
                        "levels": {"high": 0.5, "extreme": 0.5},
                    }
                }
            },
        },
        "sim": {
            "kind": "Simulation",
            "id": "sim",
            "spec": {"worldRef": "w", "personaRefs": ["p", "q"], "signalRefs": ["sig"]},
        },
    }
    levels = build(**extra).subject.type_of("traits.categoricalTrait").levels
    assert levels == ("extreme", "high", "low")


# --- the event scope ----------------------------------------------------------


@pytest.mark.unit
def test_the_event_scope_carries_the_four_cloudevents_context_attributes() -> None:
    event = build().event_scope("sig")
    assert event.scope is Scope.EVENT
    for name, declared in CONTEXT_ATTRIBUTES.items():
        assert event.type_of(name) == declared
    assert event.resolve("id") is None, "the rest of the CE envelope is not addressable"


@pytest.mark.unit
def test_payload_fields_come_from_the_pack_and_are_addressed_as_data_field() -> None:
    documents_with_the_telco_signal = {
        "sig-payment-aborted": {
            "kind": "Signal",
            "id": "sig-payment-aborted",
            "spec": {"source": "urn:x", "type": "com.x.y"},
        }
    }
    model = build_attribute_model(
        FakeBundle(documents(**documents_with_the_telco_signal)), TELCO, simulation_id="sim"
    )
    event = model.event_scope("sig-payment-aborted")
    for name, declared in TELCO.signal_attributes["sig-payment-aborted"].items():
        assert event.type_of(f"data.{name}") == declared
        assert event.resolve(name) == f"data.{name}"


@pytest.mark.unit
def test_a_resolved_dataschema_overrides_the_pack() -> None:
    """EXPR-SPEC §3 prefers the Signal's ``dataschema`` when the caller resolved one."""
    schema = {
        "properties": {
            "amount": {"type": "number"},
            "attempts": {"type": "integer"},
            "channel": {"type": "string", "enum": ["web", "app"]},
            "flagged": {"type": "boolean"},
            "labels": {"type": "array", "items": {"type": "string"}},
            "nested": {"type": "object"},
        }
    }
    model = build_attribute_model(FakeBundle(documents()), TELCO, dataschemas={"sig": schema})
    event = model.event_scope("sig")
    assert event.type_of("data.amount") == NUMBER
    assert event.type_of("data.attempts") == NUMBER
    assert event.type_of("data.channel").levels == ("app", "web")
    assert event.type_of("data.flagged").kind is TypeKind.BOOL
    assert event.type_of("data.labels") == SET_STRING
    assert event.resolve("data.nested") is None, "an untypeable property is not addressable"


@pytest.mark.unit
def test_a_property_with_no_flux_expr_type_is_simply_not_addressable() -> None:
    """Better an FLX-6004 on reference than a type invented for an odd shape."""
    schema = {
        "properties": {
            "objectValued": {"type": "object"},
            "unionValued": {"type": ["string", "number"]},
            "mixedEnum": {"type": "string", "enum": ["a", 1]},
            "tupleValued": {"type": "array", "items": [{"type": "string"}]},
            "untyped": {"description": "no type at all"},
            "plainString": {"type": "string"},
            "notAnObject": "nonsense",
            "typedFine": {"type": "number"},
        }
    }
    event = build_attribute_model(
        FakeBundle(documents()), TELCO, dataschemas={"sig": schema}
    ).event_scope("sig")
    assert event.type_of("data.typedFine") == NUMBER
    assert event.type_of("data.plainString") == STRING
    for name in (
        "objectValued",
        "unionValued",
        "mixedEnum",
        "tupleValued",
        "untyped",
        "notAnObject",
    ):
        assert event.resolve(f"data.{name}") is None, name


@pytest.mark.unit
def test_a_payload_field_colliding_with_a_context_name_loses_its_sugar() -> None:
    schema = {"properties": {"type": {"type": "number"}}}
    event = build_attribute_model(
        FakeBundle(documents()), TELCO, dataschemas={"sig": schema}
    ).event_scope("sig")
    assert event.type_of("type") == STRING, "the context attribute keeps the bare name"
    assert event.type_of("data.type") == NUMBER


@pytest.mark.unit
def test_an_event_scope_is_built_for_every_signal_in_the_bundle() -> None:
    extra = {
        "sig-two": {
            "kind": "Signal",
            "id": "sig-two",
            "spec": {"source": "urn:x", "type": "com.x.z"},
        }
    }
    model = build(**extra)
    assert sorted(model.event) == ["sig", "sig-two"]


# --- the observed scope -------------------------------------------------------


@pytest.mark.unit
def test_the_observed_scope_is_exactly_the_packs_aggregate_features() -> None:
    observed = build().observed
    assert observed.scope is Scope.OBSERVED
    assert dict(observed.attributes) == dict(TELCO.observed_features)
    assert observed.aliases == {}


# --- the document -------------------------------------------------------------


@pytest.mark.unit
def test_the_document_is_versioned_free_of_numbers_and_sorted_where_data_drives_it() -> None:
    """KERNEL-SPEC §2: no numbers at all, and every data-driven mapping sorted.

    The document's *structural* keys (``scopes``, ``paths``, ``aliases``) are
    fixed and JCS sorts them at canonicalisation time; what matters here is
    that the mappings whose keys come from the bundle -- the signal ids, the
    path names, the aliases -- are emitted in sorted order, so two builds over
    one bundle cannot differ.
    """
    document = build().to_document()
    assert document["attrModelVersion"] == ATTR_MODEL_VERSION
    assert document["vertical"] == "telco"
    assert sorted(document["scopes"]) == ["event", "observed", "subject"]

    data_driven = ["scopes.event"]
    scopes = document["scopes"]
    assert list(scopes["event"]) == sorted(scopes["event"])
    for scope_name in ("subject", "observed"):
        fragment = scopes[scope_name]
        assert list(fragment["paths"]) == sorted(fragment["paths"])
        assert list(fragment["aliases"]) == sorted(fragment["aliases"])
        data_driven.append(scope_name)
    for signal_id, fragment in scopes["event"].items():
        assert list(fragment["paths"]) == sorted(fragment["paths"]), signal_id
        assert list(fragment["aliases"]) == sorted(fragment["aliases"]), signal_id
    assert len(data_driven) == 3

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        else:
            assert not isinstance(node, (int, float)), node

    walk(document)


@pytest.mark.unit
def test_the_document_is_stable_across_builds() -> None:
    assert build().to_document() == build().to_document()


@pytest.mark.unit
def test_document_order_does_not_depend_on_the_bundles_mapping_order() -> None:
    forward = documents()
    reversed_order = {key: forward[key] for key in reversed(list(forward))}
    assert (
        build_attribute_model(FakeBundle(reversed_order), TELCO).to_document()
        == build_attribute_model(FakeBundle(forward), TELCO).to_document()
    )


# --- selecting the Simulation --------------------------------------------------


@pytest.mark.unit
def test_a_bundle_with_several_simulations_requires_a_name() -> None:
    extra = {
        "sim2": {
            "kind": "Simulation",
            "id": "sim2",
            "spec": {"worldRef": "w", "personaRefs": [], "signalRefs": []},
        }
    }
    with pytest.raises(ValueError) as raised:
        build(**extra)
    assert "simulation_id" in str(raised.value)
    model = build_attribute_model(FakeBundle(documents(**extra)), TELCO, simulation_id="sim2")
    assert model.subject.resolve("persona") is None, "sim2 references no Persona"


@pytest.mark.unit
def test_naming_a_simulation_that_is_not_there_fails_by_name() -> None:
    with pytest.raises(ValueError) as raised:
        build_attribute_model(FakeBundle(documents()), TELCO, simulation_id="nope")
    assert "nope" in str(raised.value)


@pytest.mark.unit
def test_a_dangling_worldref_fails_by_name() -> None:
    extra = {
        "sim": {
            "kind": "Simulation",
            "id": "sim",
            "spec": {"worldRef": "gone", "personaRefs": ["p"], "signalRefs": ["sig"]},
        }
    }
    with pytest.raises(ValueError) as raised:
        build(**extra)
    assert "gone" in str(raised.value)


@pytest.mark.unit
def test_a_versioned_worldref_resolves_after_the_range_is_stripped() -> None:
    extra = {
        "sim": {
            "kind": "Simulation",
            "id": "sim",
            "spec": {"worldRef": "w@^1.0", "personaRefs": ["p"], "signalRefs": ["sig"]},
        }
    }
    assert build(**extra).subject.type_of("lifecycle").levels == ("churned", "new")


# --- the vertical packs --------------------------------------------------------


@pytest.mark.unit
def test_the_registry_is_keyed_by_the_world_vertical_string() -> None:
    assert get_pack("telco") is TELCO
    assert set(pack_names()) == set(PACKS)


@pytest.mark.unit
def test_an_unknown_vertical_fails_by_name() -> None:
    with pytest.raises(KeyError) as raised:
        get_pack("banking")
    assert "banking" in str(raised.value)
    assert "telco" in str(raised.value)


@pytest.mark.unit
def test_the_telco_pack_declares_exactly_the_names_the_spec_lists() -> None:
    """EXPR-SPEC §3's telco rows, quoted."""
    assert set(TELCO.subject_attributes) == {
        "products",
        "tenureMonths",
        "latePaymentCount",
        "arpu",
    }
    assert set(TELCO.signal_attributes) == {"sig-payment-aborted"}
    assert set(TELCO.signal_attributes["sig-payment-aborted"]) == {
        "amount",
        "attempts",
        "channel",
    }
    assert set(TELCO.observed_features) == {
        "late_payment_count",
        "tenure_months",
        "arpu",
    }


@pytest.mark.unit
def test_a_pack_carries_types_and_never_values() -> None:
    """A pack that carried a number could put one into a hashed document."""
    for mapping in (TELCO.subject_attributes, TELCO.observed_features):
        for declared in mapping.values():
            assert declared.kind in TypeKind
    for fields in TELCO.signal_attributes.values():
        for declared in fields.values():
            assert declared.kind in TypeKind
