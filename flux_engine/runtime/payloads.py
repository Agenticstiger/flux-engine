# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Signal payload shapes: what a ``Signal``'s ``data`` fields are drawn from.

EXPR-SPEC §3 types a signal's payload from "the Signal's ``dataschema`` when it
is a bundled/fetchable JSON Schema, else from a ``signalAttributes`` block in
the vertical pack". Both sources give **types** -- the shipped
``sig-payment-aborted`` declares ``amount: number``, ``attempts: number``,
``channel: string`` -- and neither gives a shape to draw from. This module
supplies the shapes, and is the same v0.1 stopgap as
:mod:`flux_engine.world.derived`: engine-owned, exact decimal strings, part of
``kernelVersion``.

Two rules:

* **A field the table does not name still gets drawn.** Its family comes from
  :func:`default_family`, keyed on the declared ``flux-expr/1`` type, so a
  vertical pack can grow a payload field without the engine emitting an event
  whose ``data`` is missing it -- a missing field is null, and a Detector
  predicate over it would silently evaluate to false for every event.
* **The table may not contradict the declared type.** A family whose
  :attr:`~flux_engine.world.families.Family.expr_type` disagrees with the pack
  is refused at build time (``FLX-5040``), because a Detector predicate
  type-checked against the pack would then be compiled against a value of
  another type.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Mapping

from flux_engine.core.errors import FluxError
from flux_engine.expr.attrs import VerticalPack
from flux_engine.expr.types import Type, TypeKind, nfc
from flux_engine.world.errors import FLX_5040
from flux_engine.world.families import (
    Bernoulli,
    BoundedNormal,
    Categorical,
    Family,
    RateCount,
    drawn_type_satisfies,
)

__all__ = ["PAYLOAD_FAMILIES", "default_family", "payload_families"]


def _d(literal: str) -> Decimal:
    return Decimal(literal)


#: Per-Signal payload families, keyed by Signal document id then field name.
#: ``attempts`` is a ``RateCount`` with ``offset=1``: an aborted payment has been
#: attempted at least once, and ``jr-payment-recovery``'s trigger
#: ``attempts >= 2`` is a retry, so the count must be shifted rather than
#: clamped (see :class:`~flux_engine.world.families.RateCount`).
PAYLOAD_FAMILIES: Mapping[str, Mapping[str, Family]] = {
    "sig-payment-aborted": {
        "amount": BoundedNormal(
            mean=_d("45.00"), stddev=_d("20.00"), lo=_d("1.00"), hi=_d("500.00"), scale=2
        ),
        "attempts": RateCount(mean=_d("1.20"), offset=1),
        "channel": Categorical({"card": _d("0.60"), "sepa_dd": _d("0.30"), "wallet": _d("0.10")}),
    },
}

#: The generic families a field falls back to, one per ``flux-expr/1`` type.
#: Deliberately dull: a fallback exists so an undeclared field is still drawn,
#: not so that it is drawn well. A vertical that cares about a field's shape
#: puts it in :data:`PAYLOAD_FAMILIES`.
_DEFAULT_NUMBER = BoundedNormal(mean=_d("1"), stddev=_d("1"), lo=_d("0"), hi=_d("1000"), scale=6)
_DEFAULT_BOOL = Bernoulli(_d("0.5"))


def default_family(declared: Type) -> Family:
    """The fallback family for a payload field of type *declared*.

    ``string``, ``enum`` and ``set<string>`` have no defensible generic shape --
    the levels are the whole content of such a field -- so a field of one of
    those types must be named in :data:`PAYLOAD_FAMILIES` and is ``FLX-5040``
    otherwise.
    """
    if declared.kind is TypeKind.NUMBER:
        return _DEFAULT_NUMBER
    if declared.kind is TypeKind.BOOL:
        return _DEFAULT_BOOL
    raise FluxError(
        FLX_5040,
        f"a payload field of type {declared.name} has no generic shape; declare it in "
        "PAYLOAD_FAMILIES",
    )


def payload_families(signal_id: str, pack: VerticalPack) -> Mapping[str, Family]:
    """The family per payload field of *signal_id*, in NFC-sorted field order.

    The field set comes from the pack -- it is what EXPR-SPEC §3 type-checks
    predicates against -- and the shapes from :data:`PAYLOAD_FAMILIES`. The
    returned mapping's iteration order is the draw order (KERNEL-SPEC §7).
    """
    key = nfc(signal_id)
    declared = {nfc(name): t for name, t in (pack.signal_attributes.get(key) or {}).items()}
    table = {nfc(name): family for name, family in (PAYLOAD_FAMILIES.get(key) or {}).items()}
    unknown = sorted(set(table) - set(declared))
    if unknown:
        raise FluxError(
            FLX_5040,
            f"signal {key!r}: PAYLOAD_FAMILIES draws {unknown} that vertical "
            f"{pack.name!r} does not declare",
        )
    built: Dict[str, Family] = {}
    for name in sorted(declared):
        family = table.get(name) or default_family(declared[name])
        if not drawn_type_satisfies(declared[name], family.expr_type):
            raise FluxError(
                FLX_5040,
                f"signal {key!r} field {name!r} is declared {declared[name].name} by "
                f"vertical {pack.name!r} and drawn as {family.expr_type.name}",
            )
        built[name] = family
    return built
