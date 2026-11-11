# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Signal arrivals: CloudEvents 1.0 records on the seed tree's signal paths.

``Signal.spec.envelope`` is ``const: cloudevents/1.0`` in FLUX 0.5.0 and
``Module.spec.binds`` names ``event-envelope`` as a port, so the envelope is not
this engine's to invent: the records are built by the CNCF ``cloudevents``
SDK's own :class:`~cloudevents.core.v1.event.CloudEvent`, which owns the
required-attribute rules and the extension-name rules.

Two of that SDK's defaults are exactly what KERNEL-SPEC forbids: an absent
``id`` becomes ``str(uuid.uuid4())`` and an absent ``time`` becomes
``datetime.now(timezone.utc)`` (§14 names "wall-clock timestamp in a traced op"
and "uuid4 event id" as two of the six broken kernels). Both are therefore
always supplied here:

* ``id`` is :func:`~flux_engine.core.seedtree.derive_identifier` over the
  event's own seed path, so the id of occurrence *k* of signal *s* for subject
  *n* is a pure function of the contract, the seed and those three coordinates;
* ``time`` is the virtual clock's instant, built by arithmetic on the Unix
  epoch -- no wall clock is read anywhere in this module.

**The arrival model.** FLUX 0.5.0 declares no rate on a ``Signal``, so the
shape of arrivals is engine-owned (as ``tickMs`` is in §7) and declared in
:class:`ArrivalModel`. It has two stages, because KERNEL-SPEC §6 says
``engagement`` is the dial of "event-arrival rates" and §6's mechanism is a
dial over a *binary* outcome:

1. **Who emits.** Whether a subject emits signal *s* at all is a §6-dialled
   outcome at the authored rate :attr:`ArrivalModel.emitter_share`, under the
   ``engagement`` dial. At ``engagement = 0`` the emitter set is exactly the
   first ``LRM(n, share)`` ordinals and no draw is made.
2. **How often, and when.** An emitter draws its occurrence count and its
   burst start on the ``occurrence = 0`` stream, and each later occurrence
   draws its gap from the previous one on its own stream. Arrivals are bursty
   rather than uniform over the window because a ``Detector`` asks for *k*
   events inside a ``timeWindowMs`` -- 15 minutes in the shipped example -- and
   occurrences spread uniformly over a 92-day window would make every detector
   in every bundle dead code.

Draw order on an occurrence's stream is fixed and stated in
:func:`_occurrences`: count and start (occurrence 0) or gap (occurrence *k*),
then the payload fields in NFC-sorted order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from functools import partial
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from cloudevents.core.v1.event import CloudEvent

from flux_engine.core.clock import Phase, Scheduler, VirtualClock, format_rfc3339_ms
from flux_engine.core.digest import digest_of_document
from flux_engine.core.rng import DrawStream, bounded_uint, exponential, kernel_context, poisson
from flux_engine.core.seedtree import SeedTree, derive_identifier, signal_path, temperature_path
from flux_engine.core.trace import TraceOp
from flux_engine.expr.attrs import VerticalPack
from flux_engine.expr.compile import View, make_view
from flux_engine.expr.types import ScopeModel, nfc
from flux_engine.runtime.cast import Cast
from flux_engine.runtime.payloads import payload_families
from flux_engine.runtime.temperature import Dial
from flux_engine.world.families import Family
from flux_engine.world.population import PopulationSpec, SubjectStore
from flux_engine.world.tracing import TraceLog

__all__ = [
    "ARRIVAL_DIAL",
    "CLOUDEVENTS_ENVELOPE",
    "DEFAULT_ARRIVAL_MODEL",
    "EVENT_ID_LENGTH",
    "ArrivalModel",
    "SignalEvent",
    "emit_signals",
]

#: The envelope FLUX 0.5.0 pins ``Signal.spec.envelope`` to.
CLOUDEVENTS_ENVELOPE = "cloudevents/1.0"

#: KERNEL-SPEC §6: "``engagement`` scales event-arrival rates".
ARRIVAL_DIAL = "engagement"

#: Hex characters of a derived CloudEvents ``id``. 32 hex characters is 128
#: bits -- the width ``uuid4`` would have had, without the entropy.
EVENT_ID_LENGTH = 32

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ArrivalModel:
    """The engine-owned shape of signal arrivals. Every field is an exact decimal."""

    #: Authored rate of the ``engagement``-dialled "does this subject emit?" outcome.
    emitter_share: Decimal
    #: Poisson mean of the occurrences an emitter has *beyond* its first.
    extra_occurrences_mean: Decimal
    #: Mean milliseconds between consecutive occurrences of one emitter's burst.
    gap_mean_ms: Decimal

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.emitter_share <= Decimal(1):
            raise ValueError("emitter_share must lie in [0, 1]")
        if self.extra_occurrences_mean < 0:
            raise ValueError("extra_occurrences_mean must be >= 0")
        if self.gap_mean_ms <= 0:
            raise ValueError("gap_mean_ms must be > 0")


#: A third of the population emits; a burst is 2-3 occurrences a few minutes
#: apart, which is inside ``det-payment-aborted-burst``'s 900 000 ms window.
DEFAULT_ARRIVAL_MODEL = ArrivalModel(
    emitter_share=Decimal("0.35"),
    extra_occurrences_mean=Decimal("1.50"),
    gap_mean_ms=Decimal("300000"),
)


@dataclass(frozen=True)
class SignalEvent:
    """One arrival: the CloudEvent, its place in the seed tree, and its digest."""

    signal_id: str
    ordinal: int
    occurrence: int
    t_ms: int
    payload: Mapping[str, Any]
    payload_digest: str
    event: CloudEvent

    @property
    def id(self) -> str:
        """The CloudEvents ``id``, derived from the seed path."""
        return self.event.get_id()

    def event_values(self) -> Dict[str, Any]:
        """This event as EXPR-SPEC §3 ``event``-scope canonical paths."""
        values: Dict[str, Any] = {
            "type": self.event.get_type(),
            "source": self.event.get_source(),
            "time": format_rfc3339_ms(self.t_ms),
            "subject": self.event.get_subject() or "",
        }
        for name, value in self.payload.items():
            values[f"data.{name}"] = value
        return values

    def view(self, model: ScopeModel) -> View:
        """The event's ``event``-scope view, ready for a compiled predicate."""
        values = self.event_values()
        return make_view(model, {k: v for k, v in values.items() if model.resolve(k) is not None})


def _instant(t_ms: int) -> datetime:
    """The UTC ``datetime`` for a virtual-clock instant, by epoch arithmetic."""
    return _UNIX_EPOCH + timedelta(milliseconds=t_ms)


def _draw_payload(families: Mapping[str, Family], stream: DrawStream) -> Dict[str, Any]:
    """Draw every payload field on one stream, in the NFC-sorted order *families* is in."""
    return {name: family.draw(lambda: stream) for name, family in families.items()}


def _occurrences(
    *,
    sim_id: str,
    signal_id: str,
    ordinal: int,
    clock: VirtualClock,
    seed_tree: SeedTree,
    arrival: ArrivalModel,
    families: Mapping[str, Family],
) -> Tuple[Tuple[int, Dict[str, Any]], ...]:
    """One emitter's burst: ``(t_ms, payload)`` per occurrence, in time order.

    Draw order, fixed:

    * occurrence 0 -- the extra-occurrence count, then the burst start offset,
      then the payload fields in NFC-sorted order;
    * occurrence *k* -- the gap from occurrence *k-1*, then the payload fields.

    ``bounded_uint`` gives the start offset because it is §4's named uniform
    integer draw and its word budget is fixed; the gap is §4's ``exponential``,
    quantised to whole milliseconds and floored at 1 so two occurrences never
    share an instant (which would make their order depend on the scheduler's
    ``seq`` rather than on the draw).
    """
    window = clock.duration_ms
    first = seed_tree.stream(signal_path(sim_id, signal_id, ordinal, 0))
    count = 1 + poisson(first, mean=arrival.extra_occurrences_mean)
    t_ms = clock.start_ms + (bounded_uint(first, window + 1) if window > 0 else 0)
    built: List[Tuple[int, Dict[str, Any]]] = [(t_ms, _draw_payload(families, first))]
    # In the kernel's context, never the caller's: at another precision this
    # quotient rounds differently and every later gap moves (KERNEL-SPEC §4).
    with localcontext(kernel_context()):
        rate = Decimal(1) / arrival.gap_mean_ms
    for occurrence in range(1, count):
        path = signal_path(sim_id, signal_id, ordinal, occurrence)
        stream = seed_tree.stream(path)
        gap = int(exponential(stream, rate=rate, scale=0))
        t_ms = t_ms + max(gap, 1)
        if t_ms > clock.end_ms:
            break
        built.append((t_ms, _draw_payload(families, stream)))
    return tuple(built)


def emit_signals(
    *,
    cast: Cast,
    world: PopulationSpec,
    store: SubjectStore,
    clock: VirtualClock,
    seed_tree: SeedTree,
    trace: TraceLog,
    pack: VerticalPack,
    eligible: Optional[Sequence[int]] = None,
    arrival: ArrivalModel = DEFAULT_ARRIVAL_MODEL,
) -> Tuple[SignalEvent, ...]:
    """Emit every subject's arrivals for every ``Signal`` in the cast.

    Events are ordered by KERNEL-SPEC §7's scheduling key -- through the real
    :class:`~flux_engine.core.clock.Scheduler`, at ``phase=SIGNAL`` -- and the
    ``signal`` trace ops are appended in that drained order, so ``eventDigest``
    is a digest of the run's execution order and not of the order the loops
    below happen to build events in.
    """
    sim_id = cast.simulation_id
    ordinals = tuple(eligible) if eligible is not None else tuple(store.ordinals)
    scheduler: Scheduler[SignalEvent] = Scheduler()

    for index, (signal_id, document) in enumerate(cast.of_kind("Signal")):
        families = payload_families(signal_id, pack)
        dial = Dial.declared(world.temperature, ARRIVAL_DIAL)
        emitters = dial.realise(
            ordinals,
            arrival.emitter_share,
            partial(_dial_stream, seed_tree, sim_id, dial.name, index),
        )
        stream_ordinal = cast.stream_ordinal(signal_id)
        for ordinal in ordinals:
            if ordinal not in emitters:
                continue
            for occurrence, (t_ms, payload) in enumerate(
                _occurrences(
                    sim_id=sim_id,
                    signal_id=signal_id,
                    ordinal=ordinal,
                    clock=clock,
                    seed_tree=seed_tree,
                    arrival=arrival,
                    families=families,
                )
            ):
                scheduler.schedule(
                    t_ms=clock.require_within(t_ms, "signal arrival"),
                    phase=Phase.SIGNAL,
                    entity_ordinal=ordinal,
                    stream_ordinal=stream_ordinal,
                    payload=_build(
                        signal_id=signal_id,
                        document=document,
                        ordinal=ordinal,
                        occurrence=occurrence,
                        t_ms=t_ms,
                        payload=payload,
                        subject=store.identifier(ordinal),
                        sim_id=sim_id,
                        seed_tree=seed_tree,
                    ),
                )

    emitted: List[SignalEvent] = []
    for item in scheduler.drain():
        event = item.payload
        emitted.append(event)
        trace.record(
            TraceOp.SIGNAL,
            t=event.t_ms,
            signalId=event.signal_id,
            ordinal=event.ordinal,
            occurrence=event.occurrence,
            payloadDigest=event.payload_digest,
        )
    return tuple(emitted)


def _dial_stream(
    seed_tree: SeedTree, sim_id: str, dial: str, occurrence: int, ordinal: int
) -> DrawStream:
    return seed_tree.stream(temperature_path(sim_id, dial, ordinal, occurrence))


def _build(
    *,
    signal_id: str,
    document: Mapping[str, Any],
    ordinal: int,
    occurrence: int,
    t_ms: int,
    payload: Mapping[str, Any],
    subject: str,
    sim_id: str,
    seed_tree: SeedTree,
) -> SignalEvent:
    """Wrap one drawn payload in the SDK's CloudEvents 1.0 envelope."""
    spec = document.get("spec", {})
    path = signal_path(sim_id, signal_id, ordinal, occurrence)
    attributes: Dict[str, Any] = {
        "id": derive_identifier(seed_tree.root, path, length=EVENT_ID_LENGTH),
        "source": nfc(str(spec.get("source", ""))),
        "type": nfc(str(spec.get("type", ""))),
        "subject": subject,
        "time": _instant(t_ms),
    }
    dataschema = spec.get("dataschema")
    if dataschema:
        attributes["dataschema"] = nfc(str(dataschema))
    digest = digest_of_document(dict(payload), subject=f"{signal_id}/{ordinal}/{occurrence}")
    return SignalEvent(
        signal_id=signal_id,
        ordinal=ordinal,
        occurrence=occurrence,
        t_ms=t_ms,
        payload=dict(payload),
        payload_digest=digest.value,
        event=CloudEvent(attributes, dict(payload)),
    )
