# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""World materialisation: cohorts, markets, personas, traits, derived attributes.

This is KERNEL-SPEC §5 (the largest-remainder cohort allocation), §3's four
``world`` seed paths, and the ``cohort`` / ``market`` / ``trait`` ops of §10, in
one pass over the population in ordinal order.

The order of the pass is the whole of its determinism, so it is stated once
here and pinned by tests:

1. **Cohorts** -- ``largest_remainder(size, World.spec.population.lifecycleMix)``.
   Exact rationals, ties broken by NFC-sorted key, "changing the seed never
   changes cohort sizes" (§5). Cohorts are laid down as contiguous blocks in
   NFC-sorted lifecycle order, so ordinal 0 is in the first lifecycle by name,
   not the first by declaration -- declaration order is a YAML accident and
   must not decide which subject is which.
2. **Personas** -- the same allocation over the Simulation's ``personaRefs``
   with equal weights. FLUX 0.5.0 declares no persona mix, so the split is
   engine-owned and, like the cohorts, consumes no randomness; it is therefore
   not a §10 op ("Kernel internals are never traced"), and it cannot drift
   without the referenced persona set changing.
3. **Markets** -- one ``bounded_uint`` on ``("world", id, "market", ordinal)``
   over the NFC-sorted market list. Uniform and *exact*: a categorical over
   equal weights would have to represent 1/3, and the last level would absorb
   the rounding remainder.
4. **Traits** -- one stream per ``("world", id, "subject", ordinal, "trait",
   name)``, traits visited in NFC-sorted name order. A scalar trait is a
   constant and materialises no stream at all.
5. **Derived attributes** -- one stream per ``("world", id, "subject", ordinal,
   "products")`` carrying the whole vertical table
   (:mod:`flux_engine.world.derived`).

**What is traced is what a draw decided.** A scalar trait and every worldview
value are document constants: they consume no randomness, they are already
inside ``inputDigest`` by way of the Persona document, and a ``trait`` op per
subject per constant would add one record per subject that no run could ever
make differ. So they are carried on the subject and not traced, and
``populationDigest`` covers exactly the values the seed tree decided -- cohorts,
markets, drawn traits, derived attributes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from functools import partial
from typing import Any, Dict, FrozenSet, Iterator, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.errors import FluxError
from flux_engine.core.lrm import largest_remainder
from flux_engine.core.rng import DrawStream, bounded_uint, equal_weights, kernel_context
from flux_engine.core.seedtree import (
    SeedTree,
    SeedTreeRoot,
    derive_identifier,
    market_path,
    subject_products_path,
    subject_trait_path,
)
from flux_engine.core.trace import TraceOp
from flux_engine.expr.attrs import VerticalPack
from flux_engine.expr.compile import View, make_view
from flux_engine.expr.types import ScopeModel, nfc
from flux_engine.world.derived import Derivation, derivation_for
from flux_engine.world.errors import FLX_5040
from flux_engine.world.families import BoundedNormal, Categorical, Constant, Family
from flux_engine.world.tracing import TraceLog
from flux_engine.world.values import decimal_of, int_of, probability_of, weight_map

__all__ = [
    "NORMAL_BOUND_SIGMAS",
    "PersonaSpec",
    "PopulationSpec",
    "SUBJECT_ID_LENGTH",
    "SUBJECT_ID_PREFIX",
    "Subject",
    "SubjectStore",
    "TRAIT_SCALE",
    "integral_trait_names",
    "materialise_population",
    "subject_id",
]

#: KERNEL-SPEC §4: "Result quantised to the trait's declared scale (default 6 dp)."
#: FLUX 0.5.0 has no per-trait scale field, so 6 is the scale every trait uses.
TRAIT_SCALE = 6

#: How wide a bounded normal is when the declaration omits ``min`` or ``max``.
#: Engine-owned, like ``tickMs`` in §7. Eight standard deviations leaves under
#: 1.3e-15 of the mass outside the interval -- far below the ``uniform53``
#: resolution the draw is mapped through, so the truncation is invisible to the
#: sampler while still giving ``truncated_normal`` the finite interval it
#: requires. A wider bound would cost accuracy in ``Φ`` for nothing.
NORMAL_BOUND_SIGMAS = Decimal(8)

#: Subject ids are ``sub-`` plus 16 hex characters derived from the seed-tree
#: root (SEAM-FIDELITY-SPEC §1: identifiers are "derived deterministically from
#: ``(H, ordinal)`` -- never random").
SUBJECT_ID_PREFIX = "sub-"
SUBJECT_ID_LENGTH = 16


def subject_id(root: SeedTreeRoot, world_id: str, ordinal: int) -> str:
    """The deterministic id of subject *ordinal*.

    The label ``("world", world_id, "subject", ordinal, "id")`` is deliberately
    **not** a row of KERNEL-SPEC §3's canonical path table: no stream is derived
    from it, so it can never be materialised, can never appear in
    ``seedTreeDigest``, and cannot collide with a stream's entropy --
    :func:`~flux_engine.core.seedtree.derive_identifier` hashes it under its own
    ``b"id"`` domain separator.
    """
    tag = derive_identifier(
        root, ("world", nfc(world_id), "subject", ordinal, "id"), length=SUBJECT_ID_LENGTH
    )
    return f"{SUBJECT_ID_PREFIX}{tag}"


# ---------------------------------------------------------------------------
# Declarations read out of the documents
# ---------------------------------------------------------------------------


def _nfc_keyed(declared: Mapping[Any, Any], *, what: str) -> Tuple[Tuple[str, Any], ...]:
    """NFC-key *declared*'s entries in NFC-sorted order, refusing a collision.

    Two declared names that normalise onto one string would silently
    collapse to a single entry, dropping whichever the iteration order
    happened to reach first -- a run of a document the author did not
    write. :func:`flux_engine.core.canonical.canonicalise` refuses exactly
    this collision (``FLX-1007``), but ``inputDigest`` is taken over the
    raw file bytes, so no canonicalisation pass ever sees these mappings.
    They are checked here instead, the way
    :func:`flux_engine.world.values.weight_map` already checks a weight
    mapping (``FLX-5040``).
    """
    seen: Dict[str, Any] = {}
    for key in sorted(declared, key=lambda name: nfc(str(name))):
        name = nfc(str(key))
        if name in seen:
            raise FluxError(FLX_5040, f"{what}: two keys normalise to {name!r}")
        seen[name] = declared[key]
    return tuple(seen.items())


def _nfc_names(declared: Sequence[Any], *, what: str) -> Tuple[str, ...]:
    """NFC-normalise a declared name list, sorted, refusing a collision.

    Set-deduplicating the list would drop a declared name silently; see
    :func:`_nfc_keyed`.
    """
    seen: Dict[str, None] = {}
    for value in declared:
        name = nfc(str(value))
        if name in seen:
            raise FluxError(FLX_5040, f"{what}: two entries normalise to {name!r}")
        seen[name] = None
    return tuple(sorted(seen))


@dataclass(frozen=True)
class PopulationSpec:
    """The ``World`` fields population materialisation reads, as kernel values."""

    world_id: str
    size: int
    lifecycle_mix: Mapping[str, Decimal]
    markets: Tuple[str, ...]
    vertical: str
    temperature: Mapping[str, Decimal]

    @classmethod
    def from_document(cls, world: Mapping[str, Any]) -> "PopulationSpec":
        """Read a schema-valid ``World`` document. Every number becomes a ``Decimal``."""
        spec = world.get("spec", {})
        population = spec.get("population", {})
        size = int_of(population.get("size"), what="World.spec.population.size")
        if size < 1:
            raise FluxError(FLX_5040, "World.spec.population.size must be >= 1")
        markets = _nfc_names(
            population.get("markets", []) or [], what="World.spec.population.markets"
        )
        return cls(
            world_id=nfc(str(world.get("id", ""))),
            size=size,
            lifecycle_mix=weight_map(
                population.get("lifecycleMix", {}), what="World.spec.population.lifecycleMix"
            ),
            markets=markets,
            vertical=nfc(str(spec.get("vertical", ""))),
            temperature={
                dial: probability_of(value, what=f"World.spec.temperature[{dial}]")
                for dial, value in _nfc_keyed(
                    spec.get("temperature") or {}, what="World.spec.temperature"
                )
            },
        )

    def dial(self, name: str, default: Decimal = Decimal(0)) -> Decimal:
        """The declared temperature for *name*, or *default* when it is not declared."""
        return self.temperature.get(nfc(name), default)


@dataclass(frozen=True)
class PersonaSpec:
    """One ``Persona``'s trait families and worldview constants."""

    persona_id: str
    traits: Mapping[str, Family]
    worldview: Mapping[str, Any]

    @classmethod
    def from_document(cls, persona: Mapping[str, Any]) -> "PersonaSpec":
        spec = persona.get("spec", {})
        persona_id = nfc(str(persona.get("id", "")))
        traits = {
            name: _trait_family(name, declared, persona_id)
            for name, declared in _nfc_keyed(
                spec.get("traits") or {}, what=f"Persona {persona_id!r} spec.traits"
            )
        }
        worldview = {
            name: _worldview_value(name, value, persona_id)
            for name, value in _nfc_keyed(
                spec.get("worldview") or {}, what=f"Persona {persona_id!r} spec.worldview"
            )
        }
        return cls(persona_id=persona_id, traits=traits, worldview=worldview)

    @property
    def trait_names(self) -> Tuple[str, ...]:
        """Trait names in the NFC-sorted order they are drawn in."""
        return tuple(self.traits)


def _trait_family(name: str, declared: Any, persona_id: str) -> Family:
    """Type one RFC-01 trait declaration as a sampling family (KERNEL-SPEC §4)."""
    where = f"Persona {persona_id!r} trait {name!r}"
    if not isinstance(declared, Mapping):
        # Quantised in the kernel's context: the caller's could round
        # differently, or refuse a value with more digits than its precision.
        with localcontext(kernel_context()):
            return Constant(
                probability_of(declared, what=where).quantize(Decimal(1).scaleb(-TRAIT_SCALE))
            )
    dist = declared.get("dist")
    if dist == "categorical":
        return Categorical(weight_map(declared.get("levels") or {}, what=f"{where}.levels"))
    if dist == "normal":
        mean = decimal_of(declared.get("mean"), what=f"{where}.mean")
        stddev = decimal_of(declared.get("stddev"), what=f"{where}.stddev")
        if stddev < 0:
            raise FluxError(FLX_5040, f"{where}: stddev must be >= 0")
        lo = (
            decimal_of(declared["min"], what=f"{where}.min")
            if "min" in declared
            else mean - NORMAL_BOUND_SIGMAS * stddev
        )
        hi = (
            decimal_of(declared["max"], what=f"{where}.max")
            if "max" in declared
            else mean + NORMAL_BOUND_SIGMAS * stddev
        )
        if lo > hi:
            raise FluxError(FLX_5040, f"{where}: min exceeds max")
        return BoundedNormal(mean=mean, stddev=stddev, lo=lo, hi=hi, scale=TRAIT_SCALE)
    raise FluxError(FLX_5040, f"{where}: unknown trait dist {dist!r}")


def _worldview_value(name: str, declared: Any, persona_id: str) -> Any:
    """A worldview entry: a closed-set string, or a unit-interval number."""
    if isinstance(declared, str):
        return nfc(declared)
    return probability_of(declared, what=f"Persona {persona_id!r} worldview {name!r}")


def integral_trait_names(personas: Sequence[Mapping[str, Any]]) -> Tuple[str, ...]:
    """The traits every subject that carries them holds as a whole number, NFC-sorted.

    *personas* are the documents :func:`materialise_population` draws from.
    A trait qualifies when every Persona that declares it draws it from an
    :attr:`~flux_engine.world.families.Family.integral` family. A subject
    whose Persona does not declare the trait carries no value for it, so that
    Persona neither qualifies nor disqualifies the trait. Decided from the
    declarations alone, before a subject exists.
    """
    specs = [PersonaSpec.from_document(document) for document in personas]
    names = sorted({name for spec in specs for name in spec.traits}, key=nfc)
    return tuple(
        name
        for name in names
        if all(spec.traits[name].integral for spec in specs if name in spec.traits)
    )


# ---------------------------------------------------------------------------
# The population
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Subject:
    """One materialised subject, addressed by its population ordinal."""

    ordinal: int
    id: str
    lifecycle: str
    persona: str
    market: Optional[str]
    traits: Mapping[str, Any]
    worldview: Mapping[str, Any]
    derived: Mapping[str, Any]

    def canonical_values(self) -> Dict[str, Any]:
        """This subject as EXPR-SPEC §3 ``subject``-scope canonical paths."""
        values: Dict[str, Any] = {
            "subject.id": self.id,
            "subject.ordinal": Decimal(self.ordinal),
            "subject.lifecycle": self.lifecycle,
            "subject.persona": self.persona,
        }
        if self.market is not None:
            values["subject.market"] = self.market
        for name, value in self.traits.items():
            values[f"subject.traits.{name}"] = value
        for name, value in self.worldview.items():
            values[f"subject.worldview.{name}"] = value
        for name, value in self.derived.items():
            values[f"subject.{name}"] = value
        return values


class SubjectStore:
    """A columnar population, iterated by ordinal.

    Columns are tuples, one per attribute, all of length ``len(store)``; a
    :class:`Subject` is a row view built on demand rather than an object held
    per subject, which is what keeps a 25 000-subject world a few dozen tuples
    instead of 25 000 dataclass instances. Iteration is by ordinal and nothing
    here iterates a mapping in insertion order: every column mapping is built in
    NFC-sorted key order (KERNEL-SPEC §7).
    """

    __slots__ = (
        "_ids",
        "_lifecycles",
        "_personas",
        "_markets",
        "_traits",
        "_worldview",
        "_derived",
    )

    def __init__(
        self,
        *,
        ids: Sequence[str],
        lifecycles: Sequence[str],
        personas: Sequence[str],
        markets: Sequence[Optional[str]],
        traits: Mapping[str, Sequence[Any]],
        worldview: Mapping[str, Sequence[Any]],
        derived: Mapping[str, Sequence[Any]],
    ) -> None:
        size = len(ids)
        for label, column in (
            ("lifecycles", lifecycles),
            ("personas", personas),
            ("markets", markets),
        ):
            if len(column) != size:
                raise FluxError(
                    FLX_5040, f"column {label!r} has {len(column)} rows, expected {size}"
                )
        for block, label in ((traits, "traits"), (worldview, "worldview"), (derived, "derived")):
            for name, column in block.items():
                if len(column) != size:
                    raise FluxError(
                        FLX_5040, f"{label} column {name!r} has {len(column)} rows, expected {size}"
                    )
        self._ids: Tuple[str, ...] = tuple(ids)
        self._lifecycles: Tuple[str, ...] = tuple(lifecycles)
        self._personas: Tuple[str, ...] = tuple(personas)
        self._markets: Tuple[Optional[str], ...] = tuple(markets)
        self._traits = {name: tuple(traits[name]) for name in sorted(traits, key=nfc)}
        self._worldview = {name: tuple(worldview[name]) for name in sorted(worldview, key=nfc)}
        self._derived = {name: tuple(derived[name]) for name in sorted(derived, key=nfc)}

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def ordinals(self) -> range:
        """``range(len(self))`` -- the canonical ordinal order everything iterates in."""
        return range(len(self._ids))

    @property
    def trait_names(self) -> Tuple[str, ...]:
        return tuple(self._traits)

    @property
    def derived_names(self) -> Tuple[str, ...]:
        return tuple(self._derived)

    def lifecycle(self, ordinal: int) -> str:
        return self._lifecycles[ordinal]

    def market(self, ordinal: int) -> Optional[str]:
        return self._markets[ordinal]

    def persona(self, ordinal: int) -> str:
        return self._personas[ordinal]

    def identifier(self, ordinal: int) -> str:
        return self._ids[ordinal]

    def column(self, path: str) -> Tuple[Any, ...]:
        """One column by canonical name (``lifecycle``, ``traits.x``, ``arpu``, ...)."""
        key = nfc(path)
        if key == "id":
            return self._ids
        if key == "lifecycle":
            return self._lifecycles
        if key == "persona":
            return self._personas
        if key == "market":
            return self._markets
        if key.startswith("traits."):
            return self._traits[key[len("traits.") :]]
        if key.startswith("worldview."):
            return self._worldview[key[len("worldview.") :]]
        if key in self._derived:
            return self._derived[key]
        raise KeyError(f"no column named {path!r}")

    def subject(self, ordinal: int) -> Subject:
        """The row view of subject *ordinal*."""
        return Subject(
            ordinal=ordinal,
            id=self._ids[ordinal],
            lifecycle=self._lifecycles[ordinal],
            persona=self._personas[ordinal],
            market=self._markets[ordinal],
            traits={name: column[ordinal] for name, column in self._traits.items()},
            worldview={name: column[ordinal] for name, column in self._worldview.items()},
            derived={name: column[ordinal] for name, column in self._derived.items()},
        )

    def __iter__(self) -> Iterator[Subject]:
        for ordinal in self.ordinals:
            yield self.subject(ordinal)

    def view(self, ordinal: int, model: ScopeModel) -> View:
        """The subject's ``subject``-scope view, ready for a compiled predicate.

        Paths the model does not declare are dropped rather than passed to
        :func:`~flux_engine.expr.compile.make_view`, which would raise
        ``FLX-6004``: a model built for a Simulation that references one Persona
        legitimately has no path for another Persona's traits, and a subject
        carrying one is not an error -- the expression simply cannot mention it.
        """
        values = self.subject(ordinal).canonical_values()
        return make_view(model, {k: v for k, v in values.items() if model.resolve(k) is not None})

    def cohort_counts(self) -> Dict[str, int]:
        """Subjects per lifecycle, in NFC-sorted lifecycle order."""
        counts: Dict[str, int] = {}
        for lifecycle in self._lifecycles:
            counts[lifecycle] = counts.get(lifecycle, 0) + 1
        return {name: counts[name] for name in sorted(counts, key=nfc)}


def _blocks(allocation: Mapping[str, int]) -> Tuple[str, ...]:
    """Expand ``{name: count}`` into one contiguous label per ordinal, NFC-sorted."""
    labels: List[str] = []
    for name in sorted(allocation, key=nfc):
        labels.extend([name] * allocation[name])
    return tuple(labels)


def _equal_share(names: Sequence[str], size: int) -> Mapping[str, Decimal]:
    """Equal largest-remainder weights over *names* (:func:`~flux_engine.core.rng.equal_weights`)."""
    return equal_weights(names)


def materialise_population(
    *,
    world: Mapping[str, Any],
    personas: Sequence[Mapping[str, Any]],
    pack: VerticalPack,
    seed_tree: SeedTree,
    trace: TraceLog,
    derivation: Optional[Derivation] = None,
) -> SubjectStore:
    """Materialise the whole population of *world*, tracing every drawn value.

    *personas* are the Simulation's referenced ``Persona`` documents; they are
    de-duplicated and visited in NFC-sorted id order, so the persona split does
    not depend on the order ``personaRefs`` happened to list them in.
    *derivation* defaults to the table registered for ``World.spec.vertical``
    and is checked against *pack* before the first subject is drawn, so a pack
    and a derivation that disagree fail before any value is hashed.
    """
    spec = PopulationSpec.from_document(world)
    table = derivation if derivation is not None else derivation_for(spec.vertical)
    table.check_against(pack)

    persona_specs = [
        PersonaSpec.from_document(document)
        for document in sorted(personas, key=lambda d: nfc(str(d.get("id", ""))))
    ]
    if not persona_specs:
        raise FluxError(FLX_5040, "the Simulation references no Persona; a subject needs one")
    by_id = {persona.persona_id: persona for persona in persona_specs}
    persona_ids = tuple(sorted(by_id, key=nfc))

    cohorts = largest_remainder(spec.size, dict(spec.lifecycle_mix))
    for lifecycle in sorted(cohorts, key=nfc):
        trace.record(
            TraceOp.COHORT, worldId=spec.world_id, lifecycle=lifecycle, count=cohorts[lifecycle]
        )
    lifecycles = _blocks(cohorts)
    persona_labels = _blocks(
        largest_remainder(spec.size, dict(_equal_share(persona_ids, spec.size)))
    )

    trait_names = tuple(
        sorted({name for persona in persona_specs for name in persona.traits}, key=nfc)
    )
    worldview_names = tuple(
        sorted({name for persona in persona_specs for name in persona.worldview}, key=nfc)
    )

    ids: List[str] = []
    markets: List[Optional[str]] = []
    trait_columns: Dict[str, List[Any]] = {name: [] for name in trait_names}
    worldview_columns: Dict[str, List[Any]] = {name: [] for name in worldview_names}
    derived_columns: Dict[str, List[Any]] = {name: [] for name in table.names}

    for ordinal in range(spec.size):
        lifecycle = lifecycles[ordinal]
        persona = by_id[persona_labels[ordinal]]
        ids.append(subject_id(seed_tree.root, spec.world_id, ordinal))
        markets.append(_draw_market(spec, seed_tree, trace, ordinal))
        _draw_traits(spec, seed_tree, trace, ordinal, persona, trait_names, trait_columns)
        for name in worldview_names:
            worldview_columns[name].append(persona.worldview.get(name))
        products_path = subject_products_path(spec.world_id, ordinal)
        drawn = table.draw(partial(seed_tree.stream, products_path), lifecycle=lifecycle)
        for name in table.names:
            derived_columns[name].append(drawn[name])
            trace.record(TraceOp.TRAIT, ordinal=ordinal, name=name, value=_traced(drawn[name]))

    return SubjectStore(
        ids=ids,
        lifecycles=lifecycles,
        personas=persona_labels,
        markets=markets,
        traits=trait_columns,
        worldview=worldview_columns,
        derived=derived_columns,
    )


def _draw_market(
    spec: PopulationSpec, seed_tree: SeedTree, trace: TraceLog, ordinal: int
) -> Optional[str]:
    """One market per subject, uniform over the NFC-sorted market list."""
    if not spec.markets:
        return None
    stream: DrawStream = seed_tree.stream(market_path(spec.world_id, ordinal))
    market = spec.markets[bounded_uint(stream, len(spec.markets))]
    trace.record(TraceOp.MARKET, ordinal=ordinal, market=market)
    return market


def _draw_traits(
    spec: PopulationSpec,
    seed_tree: SeedTree,
    trace: TraceLog,
    ordinal: int,
    persona: PersonaSpec,
    trait_names: Sequence[str],
    columns: Dict[str, List[Any]],
) -> None:
    """Draw every trait of one subject, in NFC-sorted trait order."""
    for name in trait_names:
        family = persona.traits.get(name)
        if family is None:
            columns[name].append(None)
            continue
        trait_path = subject_trait_path(spec.world_id, ordinal, name)
        value = family.draw(partial(seed_tree.stream, trait_path))
        columns[name].append(value)
        if family.word_cost:
            trace.record(TraceOp.TRAIT, ordinal=ordinal, name=name, value=_traced(value))


def _traced(value: Any) -> Any:
    """Render a drawn value for a §10 ``trait`` op.

    A ``set<string>`` becomes its NFC-sorted list: canonical JSON has no set,
    and sorting is the only rendering that does not let membership order -- an
    artefact of the draw loop -- into the digest.
    """
    if isinstance(value, (set, frozenset)):
        members: FrozenSet[str] = frozenset(value)
        return sorted(members)
    return value
