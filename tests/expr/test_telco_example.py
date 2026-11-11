# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end over the shipped telco bundle: the four example expressions.

EXPR-SPEC's own worked example is ``examples/telco-payment-recovery`` in the
FLUX spec repo. These tests load the real YAML through
``flux_engine.spec.validate.Bundle``, build the ``attrmodel/1`` model from the
real documents, and then parse, type-check and evaluate the four expressions
the bundle actually ships -- read out of the documents, never retyped here.

Every count is computed from the bundle. Nothing in this module states a
number that came from reading the example.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

import flux_engine._vendor.flux_spec.validate as vendored_validate
from flux_engine.expr import compile_expr, make_view, parse, unparse
from flux_engine.expr.attrs import build_attribute_model
from flux_engine.expr.types import ScopeModel
from flux_engine.spec.validate import Bundle
from flux_engine.verticals import get_pack

from .conftest import scope_model_from_json, telco_example_dir

pytestmark = pytest.mark.integration

EXAMPLE = telco_example_dir()
requires_example = pytest.mark.skipif(
    EXAMPLE is None,
    reason="the FLUX spec repo's telco example is not checked out beside this repo",
)


@pytest.fixture(scope="module")
def bundle() -> Bundle:
    from jsonschema import Draft202012Validator, FormatChecker

    assert EXAMPLE is not None
    schema = json.loads(Path(vendored_validate.FLUX_SCHEMA_PATH).read_text())
    loaded = Bundle(EXAMPLE, Draft202012Validator(schema, format_checker=FormatChecker()))
    errors = loaded.run()
    assert errors == [], errors
    return loaded


@pytest.fixture(scope="module")
def model(bundle: Bundle) -> Any:
    world = next(doc for doc in bundle.flux_docs.values() if doc["kind"] == "World")
    return build_attribute_model(bundle, get_pack(world["spec"]["vertical"]))


def shipped_expressions(bundle: Bundle) -> List[Tuple[str, str, str]]:
    """Read every flux-expr/1 string the bundle carries: (document id, where, text)."""
    found: List[Tuple[str, str, str]] = []
    for doc_id in sorted(bundle.flux_docs):
        doc = bundle.flux_docs[doc_id]
        spec = doc.get("spec", {})
        kind = doc["kind"]
        if kind == "Segment":
            found.append((doc_id, "spec.query", spec["query"]))
        elif kind == "Detector":
            for index, step in enumerate(spec.get("pattern", [])):
                if "predicate" in step:
                    found.append((doc_id, f"spec.pattern[{index}].predicate", step["predicate"]))
        elif kind == "Journey":
            condition = spec.get("trigger", {}).get("condition")
            if condition:
                found.append((doc_id, "spec.trigger.condition", condition))
        elif kind == "Playback":
            for index, mapping in enumerate(spec.get("personaMapping", [])):
                found.append(
                    (
                        doc_id,
                        f"spec.personaMapping[{index}].behaviorPattern",
                        mapping["behaviorPattern"],
                    )
                )
    return found


def scope_for(bundle: Bundle, model: Any, doc_id: str, where: str) -> ScopeModel:
    """Pick the namespace EXPR-SPEC §3 assigns to the field the expression sits in."""
    doc = bundle.flux_docs[doc_id]
    kind = doc["kind"]
    if kind == "Segment":
        return model.subject
    if kind == "Playback":
        return model.observed
    if kind == "Detector":
        index = int(where.split("[")[1].split("]")[0])
        return model.event_scope(doc["spec"]["pattern"][index]["signalRef"])
    assert kind == "Journey"
    return model.event_scope(doc["spec"]["trigger"]["signalRef"])


@requires_example
def test_the_bundle_ships_exactly_the_expressions_the_spec_walks_through(
    bundle: Bundle,
) -> None:
    """Four expressions, in four documents, in three scopes -- all computed."""
    found = shipped_expressions(bundle)
    assert len(found) == 4
    assert {doc_id for doc_id, _, _ in found} == {
        doc_id
        for doc_id, doc in bundle.flux_docs.items()
        if doc["kind"] in ("Segment", "Detector", "Journey", "Playback")
    }


@requires_example
def test_every_shipped_expression_parses_type_checks_and_is_canonical(
    bundle: Bundle, model: Any
) -> None:
    """EXPR-SPEC §4: the canonical form is what ``plan.json`` would store."""
    for doc_id, where, source in shipped_expressions(bundle):
        node = parse(source)
        scope = scope_for(bundle, model, doc_id, where)
        compile_expr(node, scope)  # type-checks; raises FLX-600n on failure
        canonical = unparse(node)
        assert unparse(parse(canonical)) == canonical, f"{doc_id} {where}"


@requires_example
def test_the_segment_query_selects_the_subjects_it_says_it_does(bundle: Bundle, model: Any) -> None:
    """``lifecycle == 'atrisk' && products.has('tv')`` over hand-built subjects."""
    segment = next(doc for doc in bundle.flux_docs.values() if doc["kind"] == "Segment")
    predicate = compile_expr(parse(segment["spec"]["query"]), model.subject)

    lifecycles = sorted(
        next(doc for doc in bundle.flux_docs.values() if doc["kind"] == "World")["spec"][
            "population"
        ]["lifecycleMix"]
    )
    population: List[Dict[str, Any]] = [
        {
            "ordinal": Decimal(ordinal),
            "lifecycle": lifecycle,
            "products": frozenset(products),
        }
        for ordinal, (lifecycle, products) in enumerate(
            [(lifecycle, ("tv",)) for lifecycle in lifecycles]
            + [(lifecycle, ("broadband",)) for lifecycle in lifecycles]
        )
    ]
    selected = [row for row in population if predicate(make_view(model.subject, row))]

    assert [row["lifecycle"] for row in selected] == ["atrisk"]
    assert len(selected) == 1


@requires_example
def test_the_detector_predicate_and_the_journey_condition_run_on_event_views(
    bundle: Bundle, model: Any
) -> None:
    detector = next(doc for doc in bundle.flux_docs.values() if doc["kind"] == "Detector")
    journey = next(doc for doc in bundle.flux_docs.values() if doc["kind"] == "Journey")
    step = next(step for step in detector["spec"]["pattern"] if "predicate" in step)

    event_scope = model.event_scope(step["signalRef"])
    burst = compile_expr(parse(step["predicate"]), event_scope)
    trigger = compile_expr(
        parse(journey["spec"]["trigger"]["condition"]),
        model.event_scope(journey["spec"]["trigger"]["signalRef"]),
    )

    aborted = make_view(
        event_scope,
        {
            "type": next(doc for doc in bundle.flux_docs.values() if doc["kind"] == "Signal")[
                "spec"
            ]["type"],
            "amount": Decimal("19.99"),
            "attempts": Decimal("2"),
            "channel": "app",
        },
    )
    assert burst(aborted) is True
    assert trigger(aborted) is True

    first_attempt = make_view(event_scope, {"amount": Decimal("0"), "attempts": Decimal("1")})
    assert burst(first_attempt) is False
    assert trigger(first_attempt) is False


@requires_example
def test_the_playback_pattern_runs_on_observed_features(bundle: Bundle, model: Any) -> None:
    playback = next(doc for doc in bundle.flux_docs.values() if doc["kind"] == "Playback")
    pattern = playback["spec"]["personaMapping"][0]["behaviorPattern"]
    predicate = compile_expr(parse(pattern), model.observed)

    assert predicate(make_view(model.observed, {"late_payment_count": Decimal("3")})) is True
    assert predicate(make_view(model.observed, {"late_payment_count": Decimal("0")})) is False
    assert predicate(make_view(model.observed, {})) is False


@requires_example
def test_the_published_model_fragments_match_the_model_built_from_the_bundle(
    model: Any, models: Any
) -> None:
    """The vectors file's telco fragments are the real model, not a hand copy."""
    signal_id = next(iter(sorted(model.event)))
    published = {
        "telco-subject": model.subject,
        f"telco-event-{signal_id.removeprefix('sig-')}": model.event_scope(signal_id),
        "telco-observed": model.observed,
    }
    for name, built in published.items():
        assert (
            scope_model_from_json({"scope": built.scope.value, **models[name].to_json()}).to_json()
            == built.to_json()
        ), name


@requires_example
def test_the_worlds_vertical_names_a_pack_this_engine_ships(bundle: Bundle) -> None:
    world = next(doc for doc in bundle.flux_docs.values() if doc["kind"] == "World")
    pack = get_pack(world["spec"]["vertical"])
    assert pack.name == world["spec"]["vertical"]
    signal_ids = {doc_id for doc_id, doc in bundle.flux_docs.items() if doc["kind"] == "Signal"}
    assert set(pack.signal_attributes) <= signal_ids
