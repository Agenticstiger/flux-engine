# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``attrmodel/1`` attribute model: three scopes derived from a bundle.

EXPR-SPEC §3: "A versioned, canonical JSON document (``attrmodel/1``) whose
digest is inside ``inputDigest``." This module derives that document from a
loaded FLUX bundle (``flux_engine.spec.validate.Bundle``) plus a vertical
pack, and hands the three scopes to the checker and the compiler.

The three scopes and where each name comes from:

``subject`` -- ``Segment.spec.query``, ``Journey`` guards
    ``id`` / ``ordinal`` from the population; ``persona`` from the
    Simulation's ``personaRefs``; ``lifecycle`` from
    ``World.spec.population.lifecycleMix``; ``market`` from
    ``World.spec.population.markets``; ``traits.<name>`` and
    ``worldview.<name>`` from the referenced Personas; the vertical pack's
    own attributes on top.

``event`` -- ``Detector.pattern[].predicate``, ``Journey.spec.trigger.condition``
    The four CloudEvents context attributes plus ``data.<field>``, typed from
    the Signal's ``dataschema`` when the caller resolved one, else from the
    vertical pack's signal attributes. One scope per Signal, because a
    predicate is written against one signal's payload.

``observed`` -- ``Playback.spec.personaMapping[].behaviorPattern``
    The vertical pack's aggregate features per observed subject. "Never raw
    records."

Two resolutions worth stating:

* **``dataschema`` is resolved by the caller, not here.** EXPR-SPEC §3 types
  payload fields "from the Signal's ``dataschema`` when it is a
  bundled/fetchable JSON Schema". Fetching is I/O, and
  ``flux_engine.expr`` is a kernel package that may not import ``os`` or
  ``socket`` (the import-linter contract in pyproject.toml). The plan step
  resolves the URI and passes the parsed schemas in through *dataschemas*;
  a signal with no resolved schema falls back to the vertical pack.
* **``worldview.riskTolerance`` is an enum, not a number.** The §3 table types
  ``worldview.<name>`` as ``number``, but the FLUX 0.5.0 schema declares
  ``worldview.riskTolerance`` as ``{"enum": ["low", "medium", "high"]}`` -- a
  string. The type is taken from the schema's closed set rather than from the
  table's general case, so ``worldview.riskTolerance == 'high'`` type-checks
  and ``worldview.riskTolerance > 3`` is ``FLX-6005``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from flux_engine.expr.types import (
    BOOL,
    NUMBER,
    SET_STRING,
    STRING,
    Scope,
    ScopeModel,
    Type,
    enum_of,
    nfc,
)

__all__ = [
    "ATTR_MODEL_VERSION",
    "AttributeModel",
    "BundleLike",
    "CONTEXT_ATTRIBUTES",
    "VerticalPack",
    "WORLDVIEW_ENUMS",
    "build_attribute_model",
]

#: The document version EXPR-SPEC §3 names. Bumping it is a spec change.
ATTR_MODEL_VERSION = "attrmodel/1"

#: The CloudEvents context attributes EXPR-SPEC §3 exposes in the ``event``
#: scope: "``type``, ``source``, ``time`` (string, RFC 3339), ``subject`` (the
#: subject id)". Closed -- ``id``, ``specversion`` and the rest of the CE
#: envelope are deliberately not addressable.
CONTEXT_ATTRIBUTES: Mapping[str, Type] = {
    "type": STRING,
    "source": STRING,
    "time": STRING,
    "subject": STRING,
}

#: Worldview keys the FLUX 0.5.0 schema declares as a closed string set. Every
#: other worldview key is a ``unitInterval`` and therefore ``number``.
WORLDVIEW_ENUMS: Mapping[str, Tuple[str, ...]] = {
    "riskTolerance": ("high", "low", "medium"),
}

Document = Mapping[str, Any]


class BundleLike(Protocol):
    """The one thing this module needs of a bundle: its schema-valid documents.

    ``flux_engine.spec.validate.Bundle`` satisfies this structurally
    (``Bundle.flux_docs`` is ``dict[str, dict]``, keyed by document id). Taking
    the protocol rather than the class keeps the kernel free of the validator's
    own imports and lets a test build a model from documents it holds in
    memory.
    """

    @property
    def flux_docs(self) -> Mapping[str, Any]:
        """Schema-valid FLUX documents of the bundle, keyed by document id."""


@dataclass(frozen=True)
class VerticalPack:
    """A vertical's contribution to the attribute model (EXPR-SPEC §3).

    "vertical-pack attributes ... ``World.spec.vertical`` ->
    ``verticals/<pack>.py`` (v0.1 stopgap; FLUX 0.6 RFC moves the declaration
    into the spec)". The pack declares types only -- it never carries values,
    so nothing here can leak a number into a hashed document.

    ``signal_attributes`` is keyed by Signal document id; its inner mapping is
    the payload field names, addressed as ``data.<field>``.
    """

    name: str
    subject_attributes: Mapping[str, Type] = field(default_factory=dict)
    signal_attributes: Mapping[str, Mapping[str, Type]] = field(default_factory=dict)
    observed_features: Mapping[str, Type] = field(default_factory=dict)


@dataclass(frozen=True)
class AttributeModel:
    """The three scopes of EXPR-SPEC §3, plus their canonical JSON document."""

    vertical: str
    subject: ScopeModel
    event: Mapping[str, ScopeModel]
    observed: ScopeModel

    def event_scope(self, signal_id: str) -> ScopeModel:
        """Return the ``event`` scope for *signal_id*; raise ``KeyError`` if absent."""
        return self.event[nfc(signal_id)]

    def to_document(self) -> Dict[str, Any]:
        """Return the canonical ``attrmodel/1`` document.

        Every mapping is emitted in sorted-key order and carries no numbers at
        all, so the document is stable under KERNEL-SPEC §2 canonicalisation
        and its digest can sit inside ``inputDigest`` (§11).
        """
        return {
            "attrModelVersion": ATTR_MODEL_VERSION,
            "vertical": self.vertical,
            "scopes": {
                "subject": self.subject.to_json(),
                "event": {
                    signal_id: self.event[signal_id].to_json() for signal_id in sorted(self.event)
                },
                "observed": self.observed.to_json(),
            },
        }


# --- helpers ----------------------------------------------------------------


def _strip_range(ref: str) -> str:
    """Drop an RFC-03 semver range from a reference (``mod@^2.1`` -> ``mod``)."""
    return ref.split("@", 1)[0]


def _documents_of_kind(bundle: BundleLike, kind: str) -> Sequence[Tuple[str, Document]]:
    """Return the bundle's documents of *kind*, in sorted-id order."""
    return [
        (doc_id, bundle.flux_docs[doc_id])
        for doc_id in sorted(bundle.flux_docs, key=nfc)
        if bundle.flux_docs[doc_id].get("kind") == kind
    ]


def _select_simulation(bundle: BundleLike, simulation_id: Optional[str]) -> Tuple[str, Document]:
    simulations = _documents_of_kind(bundle, "Simulation")
    if simulation_id is not None:
        for doc_id, doc in simulations:
            if doc_id == simulation_id:
                return doc_id, doc
        raise ValueError(f"the bundle has no Simulation with id {simulation_id!r}")
    if len(simulations) != 1:
        raise ValueError(
            "the bundle declares " f"{len(simulations)} Simulations; name one with simulation_id="
        )
    return simulations[0]


def _trait_type(name: str, declared: Any, seen: Dict[str, Type]) -> Type:
    """Type one Persona trait (RFC-01: scalar, categorical or bounded normal)."""
    if isinstance(declared, Mapping):
        if declared.get("dist") == "categorical":
            levels = sorted(nfc(level) for level in declared.get("levels", {}))
            previous = seen.get(name)
            if previous is not None and previous.levels:
                levels = sorted(set(levels) | set(previous.levels))
            return enum_of(levels)
        return NUMBER  # dist: normal
    return NUMBER  # 0.3.0-compatible scalar


def _merge_trait(name: str, new: Type, seen: Dict[str, Type]) -> None:
    previous = seen.get(name)
    if previous is not None and previous.kind is not new.kind:
        raise ValueError(
            f"trait '{name}' is declared as {previous.name} by one Persona and "
            f"{new.name} by another; the attribute model needs one type per path"
        )
    seen[name] = new


def _subject_scope(
    world: Document,
    personas: Sequence[Tuple[str, Document]],
    persona_ids: Sequence[str],
    pack: VerticalPack,
) -> ScopeModel:
    attributes: Dict[str, Type] = {
        "subject.id": STRING,
        "subject.ordinal": NUMBER,
    }
    if persona_ids:
        attributes["subject.persona"] = enum_of(persona_ids)

    population = world.get("spec", {}).get("population", {})
    lifecycle_mix = population.get("lifecycleMix", {})
    if lifecycle_mix:
        attributes["subject.lifecycle"] = enum_of(lifecycle_mix)
    markets = population.get("markets", [])
    if markets:
        attributes["subject.market"] = enum_of(markets)

    traits: Dict[str, Type] = {}
    for _persona_id, persona in personas:
        spec = persona.get("spec", {})
        for name in sorted(spec.get("traits", {}) or {}, key=nfc):
            _merge_trait(name, _trait_type(name, spec["traits"][name], traits), traits)
        for name in sorted(spec.get("worldview", {}) or {}, key=nfc):
            levels = WORLDVIEW_ENUMS.get(name)
            declared = enum_of(levels) if levels is not None else NUMBER
            attributes[f"subject.worldview.{nfc(name)}"] = declared
    for name in sorted(traits, key=nfc):
        attributes[f"subject.traits.{nfc(name)}"] = traits[name]

    for name in sorted(pack.subject_attributes, key=nfc):
        attributes[f"subject.{nfc(name)}"] = pack.subject_attributes[name]

    return ScopeModel(Scope.SUBJECT, attributes, _bare_aliases(attributes, "subject."))


def _bare_aliases(attributes: Mapping[str, Type], prefix: str) -> Dict[str, str]:
    """Sugar table: ``lifecycle`` -> ``subject.lifecycle`` "where unambiguous".

    An alias is dropped when its bare spelling is already a canonical path of
    the same scope, or when two canonical paths would claim it; a dropped
    alias makes the bare spelling ``FLX-6004`` rather than a coin toss.
    """
    claims: Dict[str, list] = {}
    for canonical in attributes:
        if not canonical.startswith(prefix):
            continue
        claims.setdefault(canonical[len(prefix) :], []).append(canonical)
    return {
        bare: targets[0]
        for bare, targets in sorted(claims.items())
        if len(targets) == 1 and bare not in attributes
    }


def _json_schema_type(declared: Mapping[str, Any]) -> Optional[Type]:
    """Map one JSON Schema property to a flux-expr/1 type, or ``None``.

    A property whose shape has no flux-expr/1 type (an object, a tuple, a
    union) is simply not addressable: referencing it is ``FLX-6004``, which is
    a better failure than inventing a type for it.
    """
    kind = declared.get("type")
    if kind in ("number", "integer"):
        return NUMBER
    if kind == "boolean":
        return BOOL
    if kind == "string":
        levels = declared.get("enum")
        if isinstance(levels, Sequence) and not isinstance(levels, (str, bytes)) and levels:
            if all(isinstance(level, str) for level in levels):
                return enum_of(levels)
            return None
        return STRING
    if kind == "array":
        items = declared.get("items")
        if isinstance(items, Mapping) and items.get("type") == "string":
            return SET_STRING
    return None


def _payload_types(
    signal_id: str,
    pack: VerticalPack,
    dataschemas: Mapping[str, Document],
) -> Mapping[str, Type]:
    schema = dataschemas.get(signal_id)
    if schema is not None:
        properties = schema.get("properties", {})
        typed: Dict[str, Type] = {}
        for name in sorted(properties, key=nfc):
            declared = properties[name]
            if not isinstance(declared, Mapping):
                continue
            resolved = _json_schema_type(declared)
            if resolved is not None:
                typed[nfc(name)] = resolved
        return typed
    return {nfc(name): t for name, t in pack.signal_attributes.get(signal_id, {}).items()}


def _event_scope(
    signal_id: str,
    pack: VerticalPack,
    dataschemas: Mapping[str, Document],
) -> ScopeModel:
    attributes: Dict[str, Type] = dict(CONTEXT_ATTRIBUTES)
    payload = _payload_types(signal_id, pack, dataschemas)
    for name in sorted(payload, key=nfc):
        attributes[f"data.{name}"] = payload[name]
    return ScopeModel(Scope.EVENT, attributes, _bare_aliases(attributes, "data."))


def build_attribute_model(
    bundle: BundleLike,
    pack: VerticalPack,
    *,
    simulation_id: Optional[str] = None,
    dataschemas: Optional[Mapping[str, Document]] = None,
) -> AttributeModel:
    """Derive the ``attrmodel/1`` model for *bundle* under *pack*.

    *simulation_id* names the Simulation whose ``personaRefs`` and ``worldRef``
    scope the ``subject`` namespace; it may be omitted when the bundle holds
    exactly one Simulation. *dataschemas* maps a Signal id to an already
    resolved JSON Schema for its payload (see the module docstring for why
    resolution is the caller's job).

    An ``event`` scope is built for every Signal in the bundle, not only the
    Simulation's ``signalRefs``, so a Detector or Journey guard written against
    any bundled Signal can be checked from the same model.
    """
    _, simulation = _select_simulation(bundle, simulation_id)
    spec = simulation.get("spec", {})

    world_ref = _strip_range(str(spec.get("worldRef", "")))
    world = bundle.flux_docs.get(world_ref)
    if world is None:
        raise ValueError(f"the Simulation's worldRef {world_ref!r} is not in the bundle")

    persona_ids = [_strip_range(str(ref)) for ref in spec.get("personaRefs", []) or []]
    personas = [
        (persona_id, bundle.flux_docs[persona_id])
        for persona_id in sorted(set(persona_ids), key=nfc)
        if persona_id in bundle.flux_docs and bundle.flux_docs[persona_id].get("kind") == "Persona"
    ]

    resolved_schemas: Mapping[str, Document] = dataschemas or {}
    event = {
        signal_id: _event_scope(signal_id, pack, resolved_schemas)
        for signal_id, _doc in _documents_of_kind(bundle, "Signal")
    }

    observed = ScopeModel(
        Scope.OBSERVED,
        {nfc(name): t for name, t in pack.observed_features.items()},
    )

    return AttributeModel(
        vertical=pack.name,
        subject=_subject_scope(world, personas, sorted(set(persona_ids), key=nfc), pack),
        event=event,
        observed=observed,
    )
