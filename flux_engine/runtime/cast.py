# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Resolving the run's cast: the closure of a Simulation's references.

A run executes exactly the documents its Simulation reaches, and nothing else.
This module computes that set once, at plan time, and hands the executors a
:class:`Cast` they can look documents up in without touching the bundle again.

**The Journey/Detector rule.** FLUX 0.5.0's ``Simulation`` has no
``journeyRefs`` and no ``detectorRefs``: the only way a Journey or a Detector
enters a run is ``Simulation.spec.moduleRefs`` -> ``Module.spec.resources``.
That is not an accident of the schema, it is the module boundary -- a module is
"the seams this module binds to; a capability can be swapped without touching
the documents that use it" -- so this module enforces it rather than relying on
the schema to have made it impossible. A Journey or Detector reached from any
other reference is ``FLX-2301`` naming the route, and after the closure every
Journey and Detector in the cast is checked to be a resource of an in-cast
Module.

**Kinds are checked too.** ``personaRefs`` naming a Segment is a document
mistake the JSON Schema cannot catch (every ``$ref`` is just an identifier), and
it would otherwise surface much later as an empty population or a missing
trait. Each edge below declares the kind it may name.

Reference strings may carry an RFC-03 semver range (``module-payment-recovery@^2.1``);
the range is resolved by ``flux.lock`` elsewhere, so only the identifier before
the ``@`` addresses a document here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.core.clock import document_ordinals
from flux_engine.core.errors import FluxError
from flux_engine.expr.types import nfc
from flux_engine.runtime.errors import FLX_2301

__all__ = ["Cast", "Reference", "resolve_cast", "strip_range"]

#: Kinds that may only be reached through ``Module.spec.resources``.
MODULE_ONLY_KINDS = ("Detector", "Journey")

Document = Mapping[str, Any]


def strip_range(ref: Any) -> str:
    """Drop an RFC-03 semver range from a reference (``mod@^2.1`` -> ``mod``)."""
    return nfc(str(ref)).split("@", 1)[0]


@dataclass(frozen=True)
class Reference:
    """One resolved edge: who named what, where, and what kind it had to be."""

    source: str
    where: str
    target: str
    expected_kind: Optional[str]


def _scalar(doc: Document, *path: str) -> Optional[Any]:
    node: Any = doc
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _list(doc: Document, *path: str) -> Sequence[Any]:
    node = _scalar(doc, *path)
    if node is None or isinstance(node, (str, bytes, Mapping)):
        return ()
    return tuple(node)


def _edges(doc_id: str, doc: Document) -> Tuple[Reference, ...]:
    """Every outgoing reference of *doc*, with the kind each one must name."""
    kind = str(doc.get("kind", ""))
    found: List[Reference] = []

    def add(where: str, ref: Any, expected: Optional[str]) -> None:
        found.append(
            Reference(source=doc_id, where=where, target=strip_range(ref), expected_kind=expected)
        )

    if kind == "Simulation":
        world = _scalar(doc, "spec", "worldRef")
        if world is not None:
            add("spec.worldRef", world, "World")
        for field, expected in (
            ("personaRefs", "Persona"),
            ("segmentRefs", "Segment"),
            ("signalRefs", "Signal"),
            ("campaignRefs", "Campaign"),
            ("experimentRefs", "Experiment"),
            ("moduleRefs", "Module"),
        ):
            for index, ref in enumerate(_list(doc, "spec", field)):
                add(f"spec.{field}[{index}]", ref, expected)
    elif kind == "Module":
        for index, ref in enumerate(_list(doc, "spec", "resources")):
            add(f"spec.resources[{index}]", ref, None)
    elif kind == "Campaign":
        for field, expected in (
            ("treatmentRef", "Treatment"),
            ("segmentRef", "Segment"),
            ("channelRef", "Channel"),
            ("consentRef", "ConsentProfile"),
        ):
            ref = _scalar(doc, "spec", field)
            if ref is not None:
                add(f"spec.{field}", ref, expected)
    elif kind == "Journey":
        trigger = _scalar(doc, "spec", "trigger", "signalRef")
        if trigger is not None:
            add("spec.trigger.signalRef", trigger, "Signal")
        treatment = _scalar(doc, "spec", "intervention", "treatmentRef")
        if treatment is not None:
            add("spec.intervention.treatmentRef", treatment, "Treatment")
        taxonomy = _scalar(doc, "spec", "taxonomyRef")
        if taxonomy is not None:
            add("spec.taxonomyRef", taxonomy, "JourneyTaxonomy")
    elif kind == "Detector":
        for index, element in enumerate(_list(doc, "spec", "pattern")):
            if isinstance(element, Mapping) and "signalRef" in element:
                add(f"spec.pattern[{index}].signalRef", element["signalRef"], "Signal")
    elif kind == "Experiment":
        for index, variant in enumerate(_list(doc, "spec", "variants")):
            if isinstance(variant, Mapping) and "treatmentRef" in variant:
                add(f"spec.variants[{index}].treatmentRef", variant["treatmentRef"], "Treatment")
    elif kind == "Offer":
        catalog = _scalar(doc, "spec", "catalogRef")
        if catalog is not None:
            add("spec.catalogRef", catalog, "Catalog")
    return tuple(found)


@dataclass(frozen=True)
class Cast:
    """Every document one run executes, plus the §7 stream ordinals."""

    simulation_id: str
    world_id: str
    documents: Mapping[str, Document]
    stream_ordinals: Mapping[str, int]
    references: Tuple[Reference, ...]

    @property
    def simulation(self) -> Document:
        return self.documents[self.simulation_id]

    @property
    def world(self) -> Document:
        return self.documents[self.world_id]

    def document(self, doc_id: str) -> Document:
        """One document of the cast; ``FLX-2301`` when it is not in it."""
        key = strip_range(doc_id)
        if key not in self.documents:
            raise FluxError(FLX_2301, f"{key!r} is not in the run's cast")
        return self.documents[key]

    def ids(self, kind: str) -> Tuple[str, ...]:
        """Cast document ids of one kind, NFC-sorted."""
        return tuple(
            doc_id
            for doc_id in sorted(self.documents, key=nfc)
            if self.documents[doc_id].get("kind") == kind
        )

    def of_kind(self, kind: str) -> Tuple[Tuple[str, Document], ...]:
        """``(id, document)`` pairs of one kind, NFC-sorted by id."""
        return tuple((doc_id, self.documents[doc_id]) for doc_id in self.ids(kind))

    def stream_ordinal(self, doc_id: str) -> int:
        """§7's ``stream_ordinal``: the document's canonical ordinal in the bundle."""
        key = strip_range(doc_id)
        if key not in self.stream_ordinals:
            raise FluxError(FLX_2301, f"{key!r} has no stream ordinal in this bundle")
        return self.stream_ordinals[key]


def _select_simulation(docs: Mapping[str, Document], simulation_id: Optional[str]) -> str:
    simulations = [
        doc_id for doc_id in sorted(docs, key=nfc) if docs[doc_id].get("kind") == "Simulation"
    ]
    if simulation_id is not None:
        wanted = strip_range(simulation_id)
        if wanted not in simulations:
            raise FluxError(FLX_2301, f"the bundle has no Simulation with id {wanted!r}")
        return wanted
    if len(simulations) != 1:
        raise FluxError(
            FLX_2301,
            f"the bundle declares {len(simulations)} Simulations; name one with simulation_id=",
        )
    return simulations[0]


def resolve_cast(flux_docs: Mapping[str, Document], *, simulation_id: Optional[str] = None) -> Cast:
    """Resolve the closure of *simulation_id*'s references over *flux_docs*.

    *flux_docs* is the whole bundle (``Bundle.flux_docs``), because §7's
    ``stream_ordinal`` is "the canonical ordinal of the producing document in
    the **bundle**" -- an ordinal computed over the cast alone would move when an
    unrelated document was added to the bundle, which is exactly the instability
    §3's insertion-stability property forbids.
    """
    start = _select_simulation(flux_docs, simulation_id)
    documents: Dict[str, Document] = {}
    references: List[Reference] = []
    module_resources: Set[str] = set()
    pending: List[Tuple[str, Optional[Reference]]] = [(start, None)]

    while pending:
        doc_id, arrived_by = pending.pop(0)
        if doc_id in documents:
            continue
        document = flux_docs.get(doc_id)
        if document is None:
            origin = (
                f"{arrived_by.source}.{arrived_by.where}"
                if arrived_by is not None
                else "simulation_id"
            )
            raise FluxError(FLX_2301, f"{origin} names {doc_id!r}, which the bundle does not carry")
        kind = str(document.get("kind", ""))
        if arrived_by is not None:
            # The module boundary is checked before the expected kind: a
            # reference that names a Journey where a Segment belongs is wrong
            # for two reasons at once, and "a Journey enters a run only through
            # moduleRefs" is the one that tells the author what the rule is.
            if kind in MODULE_ONLY_KINDS and flux_docs[arrived_by.source].get("kind") != "Module":
                raise FluxError(
                    FLX_2301,
                    f"{arrived_by.source}.{arrived_by.where} names the {kind} {doc_id!r}; a "
                    f"{kind} enters a run only through moduleRefs -> Module.spec.resources",
                )
            if arrived_by.expected_kind is not None and kind != arrived_by.expected_kind:
                raise FluxError(
                    FLX_2301,
                    f"{arrived_by.source}.{arrived_by.where} names {doc_id!r}, which is a "
                    f"{kind or 'document of unknown kind'}, not a {arrived_by.expected_kind}",
                )
        documents[doc_id] = document
        if kind == "Module":
            module_resources.update(strip_range(r) for r in _list(document, "spec", "resources"))
        for edge in _edges(doc_id, document):
            references.append(edge)
            pending.append((edge.target, edge))

    for kind in MODULE_ONLY_KINDS:
        for doc_id in sorted(documents, key=nfc):
            if documents[doc_id].get("kind") == kind and doc_id not in module_resources:
                raise FluxError(  # pragma: no cover - unreachable while the routes above hold
                    FLX_2301,
                    f"the {kind} {doc_id!r} is in the cast but is not a resource of any "
                    "Module the Simulation references",
                )

    world_ids = [d for d in sorted(documents, key=nfc) if documents[d].get("kind") == "World"]
    if len(world_ids) != 1:
        raise FluxError(
            FLX_2301, f"the Simulation's cast holds {len(world_ids)} Worlds; it must hold one"
        )

    return Cast(
        simulation_id=start,
        world_id=world_ids[0],
        documents={doc_id: documents[doc_id] for doc_id in sorted(documents, key=nfc)},
        stream_ordinals=_ordinals(flux_docs),
        references=tuple(references),
    )


def _ordinals(flux_docs: Iterable[str]) -> Mapping[str, int]:
    """§7's document ordinals over the whole bundle (documents sorted by NFC id)."""
    return document_ordinals(flux_docs)
