# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``towers`` vertical pack: a tower company and its tenants.

A tower company leases space on its towers to tenants: mobile network
operators, internet service providers, broadcasters. In this vertical a
subject is **one tenant in one market**, the relationship a master lease
agreement governs, and the shipped example bundle
(``examples/towers-tenant-renewal`` in the FLUX spec repo, whose
``World.spec.vertical`` is ``towers``) follows tenant renewal risk as SLA
breaches rise.

Every name below is taken from the tower-management FLUID contracts the
example's emitted product consumes: ``silver.commercial.active_tenancy_v1``
(dp005), ``gold.ops.uptime_slas_v1`` (dp014),
``silver.finance.tenant_billing_v1`` (dp016) and ``gold.asset.site_360_v1``
(dp019).

* subject attributes -- ``tenancyCount`` (the tenant's active tenancies in its
  market: dp005 rows, which dp019 ``tenancy_count`` counts per site),
  ``monthlyRecurringCharge`` (dp005 ``monthly_recurring_charge``, per tenancy,
  before energy pass-through), ``slaTargetPct`` (dp014 ``sla_target_pct``),
  ``chronicSlaBreaches`` (how many of the tenant's sites dp014 flags
  ``chronic_site_flag``: three or more breached months in the last six) and
  ``monthsToLeaseExpiry`` (whole months to the end of the lease term).
  The tenant's country is not a separate attribute: the World names its
  markets by ISO 3166-1 alpha-2 code, as dp019 ``country_code`` ("market of
  the site") does, so ``subject.market`` *is* the country and is never drawn
  twice.
* signal attributes -- ``sig-sla-breach`` (dp014 ``availability_pct``,
  ``downtime_minutes``, ``worst_outage_cause``), ``sig-site-outage`` (an
  outage-class alarm: minutes, sites affected, cause) and
  ``sig-billing-dispute`` (a dp016 line on a ``DISPUTED`` invoice: amount and
  ``charge_type``).
* observed features -- the per-tenant aggregates a Playback would calibrate
  against, spelled as the source columns spell them.

Types only, exactly as in :mod:`flux_engine.verticals.telco`: a pack never
carries a value, a rate or a distribution. The shapes live in
:mod:`flux_engine.world.derived` and :mod:`flux_engine.runtime.payloads`.
"""

from __future__ import annotations

from typing import Mapping

from flux_engine.expr.attrs import VerticalPack
from flux_engine.expr.types import NUMBER, STRING, Type

__all__ = ["TOWERS", "TOWERS_EMIT_ALIASES"]

#: The ``subject`` scope's vertical-pack row for ``towers``.
_SUBJECT: Mapping[str, Type] = {
    "chronicSlaBreaches": NUMBER,
    "monthlyRecurringCharge": NUMBER,
    "monthsToLeaseExpiry": NUMBER,
    "slaTargetPct": NUMBER,
    "tenancyCount": NUMBER,
}

#: The ``event`` scope: each Signal's payload fields, addressed as
#: ``data.<field>`` with the bare spelling as sugar.
_SIGNALS: Mapping[str, Mapping[str, Type]] = {
    "sig-billing-dispute": {
        "chargeType": STRING,
        "disputedAmount": NUMBER,
    },
    "sig-site-outage": {
        "cause": STRING,
        "outageMinutes": NUMBER,
        "sitesAffected": NUMBER,
    },
    "sig-sla-breach": {
        "availabilityPct": NUMBER,
        "cause": STRING,
        "downtimeMinutes": NUMBER,
    },
}

#: The ``observed`` scope: aggregates per observed tenant, never raw records.
_OBSERVED: Mapping[str, Type] = {
    "availability_90d_pct": NUMBER,
    "chronic_site_count": NUMBER,
    "monthly_recurring_charge": NUMBER,
    "tenancy_count": NUMBER,
}

#: Expose-column -> runtime-attribute aliases, step 2 of
#: :func:`flux_engine.seam.mapping.resolve_column_mapping`.
#:
#: The shipped ``tenant_renewal_risk_moment`` contract spells its columns the
#: way the tower-management contracts do (snake_case, ``country``,
#: ``detected_at``), and none of those names exists in a runtime scope, so the
#: by-name step cannot reach any of them. One row is one renewal-risk
#: detection: the moment's own coordinates, the event that completed it, and
#: the tenant it happened to.
TOWERS_EMIT_ALIASES: Mapping[str, str] = {
    "availability_pct": "event.data.availabilityPct",
    "breach_count": "moment.eventCount",
    "chronic_sla_breaches": "subject.chronicSlaBreaches",
    "country": "subject.market",
    "detected_at": "moment.lastTime",
    "downtime_minutes": "event.data.downtimeMinutes",
    "first_breach_at": "moment.firstTime",
    "lifecycle_stage": "subject.lifecycle",
    "monthly_recurring_charge": "subject.monthlyRecurringCharge",
    "months_to_lease_expiry": "subject.monthsToLeaseExpiry",
    "outage_cause": "event.data.cause",
    "sla_target_pct": "subject.slaTargetPct",
    "tenancy_count": "subject.tenancyCount",
    "tenant_id": "subject.id",
    "tenant_segment": "subject.persona",
}

TOWERS = VerticalPack(
    name="towers",
    subject_attributes=_SUBJECT,
    signal_attributes=_SIGNALS,
    observed_features=_OBSERVED,
)
