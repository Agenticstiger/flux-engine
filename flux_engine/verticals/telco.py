# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``telco`` vertical pack (EXPR-SPEC §3).

Every name here is quoted from EXPR-SPEC §3 and matches the shipped telco
example bundle (``examples/telco-payment-recovery`` in the FLUX spec repo),
whose ``World.spec.vertical`` is ``telco``:

* subject attributes -- "telco: ``products: set<string>``, ``tenureMonths:
  number``, ``latePaymentCount: number``, ``arpu: number``". These are what
  ``seg-atrisk-tv``'s query (``lifecycle == 'atrisk' && products.has('tv')``)
  resolves against.
* signal attributes -- "telco ``sig-payment-aborted``: ``amount: number``,
  ``attempts: number``, ``channel: string``". The Signal declares a
  ``dataschema`` URI that is not resolvable offline, so the pack is what types
  ``det-payment-aborted-burst``'s ``amount > 0`` and
  ``jr-payment-recovery``'s ``attempts >= 2``.
* observed features -- "telco: ``late_payment_count: number``,
  ``tenure_months: number``, ``arpu: number``". These type
  ``pb-billing-history``'s ``late_payment_count >= 2``.

Types only: a pack never carries a value, a rate or a distribution, so nothing
in this module can put a number into a hashed document.
"""

from __future__ import annotations

from typing import Mapping

from flux_engine.expr.attrs import VerticalPack
from flux_engine.expr.types import NUMBER, SET_STRING, STRING, Type

__all__ = ["TELCO", "TELCO_EMIT_ALIASES"]

#: EXPR-SPEC §3, ``subject`` scope, vertical-pack row.
_SUBJECT: Mapping[str, Type] = {
    "products": SET_STRING,
    "tenureMonths": NUMBER,
    "latePaymentCount": NUMBER,
    "arpu": NUMBER,
}

#: EXPR-SPEC §3, ``event`` scope: the payload fields of ``sig-payment-aborted``,
#: addressed as ``data.<field>`` with the bare spelling as sugar.
_SIGNALS: Mapping[str, Mapping[str, Type]] = {
    "sig-payment-aborted": {
        "amount": NUMBER,
        "attempts": NUMBER,
        "channel": STRING,
    },
}

#: EXPR-SPEC §3, ``observed`` scope: "Aggregate features per observed subject
#: computed by the ingest step ... Never raw records."
_OBSERVED: Mapping[str, Type] = {
    "late_payment_count": NUMBER,
    "tenure_months": NUMBER,
    "arpu": NUMBER,
}

#: Expose-column -> runtime-attribute aliases for this vertical, step 2 of
#: :func:`flux_engine.seam.mapping.resolve_column_mapping`.
#:
#: Every entry is a column whose contract name and whose runtime name
#: differ, so the by-name step (step 3) cannot reach it: a
#: ``payment_recovery_moment`` row calls the subject's identifier
#: ``subscriber_id`` and the moment's instant ``aborted_at``, and neither
#: name exists in any runtime scope. ``amount`` and ``market`` *would*
#: resolve by name -- they are listed anyway so the whole of the shipped
#: contract's row is declared in one place a reader can check against the
#: contract, rather than half declared here and half inferred.
#:
#: Types only, as everywhere else in a pack: an alias names a source, never
#: a value, a rate or a default.
TELCO_EMIT_ALIASES: Mapping[str, str] = {
    "subscriber_id": "subject.id",
    "aborted_at": "event.time",
    "amount": "event.data.amount",
    "market": "subject.market",
}

TELCO = VerticalPack(
    name="telco",
    subject_attributes=_SUBJECT,
    signal_attributes=_SIGNALS,
    observed_features=_OBSERVED,
)
