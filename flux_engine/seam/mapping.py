# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Which runtime attribute fills which expose column, decided once at plan time.

A scenario-driven emit has no aggregate statistics to draw from: its rows
are the run's own moments. The detector fires, and *that firing* is the
row -- one aborted-payment burst for one subject is one
``payment_recovery_moment``. What this module owns is the last step: which
runtime attribute lands in which declared column.

**The detector fire is the moment, not the journey transition.** Both are
observable (KERNEL-SPEC §10 traces ``detect`` and ``transition``), and the
choice matters because they do not count the same things: a journey
transition exists only where a Journey's trigger condition held and a
persona reaction was drawn, so journey-driven rows would silently drop
every burst the detector found outside a journey's reach. The detection is
the event the contract describes -- "the aborted-payment burst happened,
here is when and for how much" -- and it carries the matched events, so
every column below has a source. A run that also wants the journey's
outcome declares a column for it and maps it; that is an additive
declaration, not a different row.

**Three resolution steps, in this fixed order, first match wins.**

1. **An override on the emit.** ``mapping: { <column>: <source> }`` in the
   emit's entry of the document-level extension block
   (SEAM-FIDELITY-SPEC §2). The author's own word, so it wins.
2. **The vertical's alias table.** ``flux_engine.verticals.emit_aliases``
   -- for ``telco``: ``subscriber_id -> subject.id``, ``aborted_at ->
   event.time``, ``amount -> event.data.amount``, ``market ->
   subject.market``. Curated per vertical, not per bundle, so two contracts
   in one vertical spell one column one way.
3. **The column's own name.** A column called ``amount`` resolves to
   ``event.data.amount`` if the moment's event declares that payload field,
   else to ``subject.amount`` if the subject carries that attribute, else
   to ``subject.traits.amount``. The search order is fixed and the
   namespaces are checked against what the run *declares*, never against
   what one row happens to hold.

A required column no step resolves is ``FLX-3302`` at plan time. An
optional one renders JSON ``null`` -- the contract said the column may be
absent, and inventing a value for it is the one thing this module must not
do.

**Nothing here reads a run.** :func:`resolve_column_mapping` takes the
compiled expose, the alias table, the overrides and the set of source
paths the run declares; :meth:`ColumnMapping.row` takes a mapping from
source path to value. The gluing -- which subject, which event, which
detection -- is :mod:`flux_engine.stages.pipeline`'s, so this module stays
free of every runtime type and can be tested with two dictionaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError
from flux_engine.seam.contract import CompiledExpose
from flux_engine.seam.emit import FLX_3302

__all__ = [
    "EVENT_SCALARS",
    "MOMENT_SCALARS",
    "SUBJECT_SCALARS",
    "BY_NAME_PREFIXES",
    "ColumnBinding",
    "ColumnMapping",
    "resolve_column_mapping",
    "source_namespace",
]

#: The closed subject-scope source paths every run declares, whatever its
#: vertical. They are :class:`flux_engine.world.population.Subject`'s own
#: fields, spelled the way EXPR-SPEC §3 spells them.
SUBJECT_SCALARS: Tuple[str, ...] = (
    "subject.id",
    "subject.ordinal",
    "subject.lifecycle",
    "subject.persona",
    "subject.market",
)

#: The closed event-scope source paths, taken from the CloudEvents 1.0
#: envelope every signal arrival carries
#: (:meth:`flux_engine.runtime.signals.SignalEvent.event_values`).
EVENT_SCALARS: Tuple[str, ...] = (
    "event.id",
    "event.time",
    "event.type",
    "event.source",
    "event.subject",
)

#: The closed moment-scope source paths: the detection's own coordinates
#: (KERNEL-SPEC §10's ``detect`` op fields).
MOMENT_SCALARS: Tuple[str, ...] = (
    "moment.detectorId",
    "moment.firstTime",
    "moment.lastTime",
    "moment.eventCount",
)

#: Step 3's search order. A bare column name is tried under each prefix in
#: turn and the first namespace that *declares* an attribute of that name
#: wins. Ordered most-specific-to-the-moment first: the event that
#: completed the moment describes the moment, and a subject attribute of
#: the same name describes the subject the moment happened to.
BY_NAME_PREFIXES: Tuple[str, ...] = ("event.data.", "subject.", "subject.traits.")


def source_namespace(
    *,
    subject_attributes: Iterable[str] = (),
    subject_traits: Iterable[str] = (),
    event_payload_fields: Iterable[str] = (),
) -> FrozenSet[str]:
    """Every source path a run declares, as one flat, sorted-safe set.

    The three arguments are *bare names*: the subject's vertical-pack and
    derived attributes, its trait names, and the payload fields the
    vertical pack declares for the signals in the cast. The closed scalars
    of all three scopes are always present.
    """
    paths = set(SUBJECT_SCALARS) | set(EVENT_SCALARS) | set(MOMENT_SCALARS)
    paths.update(f"subject.{nfc(str(name))}" for name in subject_attributes)
    paths.update(f"subject.traits.{nfc(str(name))}" for name in subject_traits)
    paths.update(f"event.data.{nfc(str(name))}" for name in event_payload_fields)
    return frozenset(paths)


@dataclass(frozen=True)
class ColumnBinding:
    """One declared column and the source path that fills it.

    ``source`` is ``None`` for an optional column no step resolved: the row
    renders JSON ``null`` there. ``how`` names which of the three steps
    decided, so a plan can report the mapping rather than merely apply it.
    """

    column: str
    source: Optional[str]
    required: bool
    how: str

    def as_document(self) -> Dict[str, Any]:
        """The binding as canonical-ready JSON, for ``plan.json``."""
        return {
            "column": self.column,
            "source": self.source,
            "required": self.required,
            "resolvedBy": self.how,
        }


@dataclass(frozen=True)
class ColumnMapping:
    """One expose's column bindings, aligned to its declared column order."""

    expose_id: str
    bindings: Tuple[ColumnBinding, ...]

    @property
    def sources(self) -> Tuple[Optional[str], ...]:
        """The source path per column, positionally aligned to the expose."""
        return tuple(binding.source for binding in self.bindings)

    def row(self, values: Mapping[str, Any]) -> Tuple[Any, ...]:
        """Render one moment as the positional row the emit stream takes.

        *values* maps a source path to the value the run holds for it. A
        path the mapping wants and *values* does not carry yields ``None``
        -- which a required column turns into ``FLX-3202`` at the seam,
        where a missing value in an otherwise well-mapped run belongs.
        Nothing here renders: the returned tuple carries the runtime
        values, and :func:`flux_engine.seam.conform.render_row` decides
        how each one is spelled.
        """
        return tuple(
            None if binding.source is None else values.get(binding.source)
            for binding in self.bindings
        )

    def as_document(self) -> Dict[str, Any]:
        """The whole mapping as canonical-ready JSON, for ``plan.json``."""
        return {
            "exposeId": self.expose_id,
            "bindings": [binding.as_document() for binding in self.bindings],
        }


def _resolve_one(
    column: str,
    *,
    overrides: Mapping[str, str],
    aliases: Mapping[str, str],
    declared: FrozenSet[str],
    expose_id: str,
) -> Tuple[Optional[str], str]:
    """Run the three steps for one column; return ``(source, how)``."""
    if column in overrides:
        source = nfc(str(overrides[column]))
        if source not in declared:
            raise FluxError(
                FLX_3302,
                f"expose {expose_id!r} column {column!r}: the emit maps it onto "
                f"{source!r}, which this run declares no attribute for",
            )
        return source, "override"
    if column in aliases:
        source = nfc(str(aliases[column]))
        if source not in declared:
            raise FluxError(
                FLX_3302,
                f"expose {expose_id!r} column {column!r}: the vertical maps it onto "
                f"{source!r}, which this run declares no attribute for",
            )
        return source, "alias"
    for prefix in BY_NAME_PREFIXES:
        candidate = f"{prefix}{column}"
        if candidate in declared:
            return candidate, "name"
    return None, "unmapped"


def resolve_column_mapping(
    expose: CompiledExpose,
    *,
    declared_sources: FrozenSet[str],
    aliases: Optional[Mapping[str, str]] = None,
    overrides: Optional[Mapping[str, Any]] = None,
) -> ColumnMapping:
    """Decide, for every column of *expose*, which runtime attribute fills it.

    Raises ``FLX-3302`` naming the column when a **required** column
    resolves to nothing, and when an override or an alias names a source
    path the run declares no attribute for -- both at plan time, before a
    single row exists.

    *overrides* is the emit's ``mapping`` block, read defensively: a
    non-mapping value, or an entry whose key is not a column of the expose
    or whose value is not a string, is refused here rather than ignored,
    because an override that silently does nothing is an author's
    declaration the engine did not honour.
    """
    expose_id = expose.expose_id
    normalised_overrides: Dict[str, str] = {}
    if overrides is not None:
        if not isinstance(overrides, Mapping):
            raise FluxError(
                FLX_3302,
                f"expose {expose_id!r}: the emit's mapping block is "
                f"{type(overrides).__name__}, not a mapping of column to source",
            )
        known = set(expose.column_names)
        for raw_key in sorted(str(key) for key in overrides):
            original = next(key for key in overrides if str(key) == raw_key)
            key = nfc(raw_key)
            if key not in known:
                raise FluxError(
                    FLX_3302,
                    f"expose {expose_id!r}: the emit maps a column {key!r} the expose "
                    f"does not declare ({sorted(known)})",
                )
            value = overrides[original]
            if not isinstance(value, str):
                raise FluxError(
                    FLX_3302,
                    f"expose {expose_id!r} column {key!r}: a mapping source is a source "
                    f"path string, got {type(value).__name__}",
                )
            normalised_overrides[key] = value

    normalised_aliases = {nfc(str(key)): str(value) for key, value in (aliases or {}).items()}

    bindings: List[ColumnBinding] = []
    for column in expose.columns:
        source, how = _resolve_one(
            column.name,
            overrides=normalised_overrides,
            aliases=normalised_aliases,
            declared=declared_sources,
            expose_id=expose_id,
        )
        if source is None and column.required:
            raise FluxError(
                FLX_3302,
                f"expose {expose_id!r} column {column.name!r} is required and no "
                f"attribute is mapped onto it: no override, no alias for this "
                f"vertical, and no attribute named {column.name!r} under "
                f"{', '.join(prefix.rstrip('.') for prefix in BY_NAME_PREFIXES)}",
            )
        bindings.append(
            ColumnBinding(column=column.name, source=source, required=column.required, how=how)
        )
    return ColumnMapping(expose_id=expose_id, bindings=tuple(bindings))
