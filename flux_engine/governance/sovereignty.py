# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""World/Simulation sovereignty enforcement (schema ``$defs/sovereignty``).

The FLUX schema declares sovereignty on both ``World.spec.sovereignty`` and
``Simulation.spec.sovereignty`` as ``{"allowedZones": [...], "enforcement":
"block" | "audit"}`` (default ``"block"``), with the schema's own
description of the two modes: *block* -- execution refuses to start outside
the allowed zones; *audit* -- a violation is recorded instead of stopping
the run.

This module is the boundary that turns that declaration into an action. It
resolves the run's own declared zone from ``--zone``/``FLUX_ZONE`` (whoever
invokes the CLI passes ``--zone``; :func:`declared_zone` is the same
precedence, callable directly for a caller that never goes through the
CLI), checks it against the policy, and either raises
:class:`SovereigntyViolation` (``FLX-4001``, block mode) or returns a
:class:`SovereigntyAuditRow` for the caller to hand to a lineage emitter
(audit mode).

**Ambiguity resolved.** The schema does not say what happens when no zone
is declared at all (no ``--zone``, no ``FLUX_ZONE``) against a document
that *does* declare ``allowedZones``. Read literally, KERNEL-SPEC's
governance band exists to stop a run executing somewhere it should not --
an *absent* declaration is not a claim about where the run is executing,
so :func:`enforce_sovereignty` does not refuse the run in that case, in
any enforcement mode, rather than treating it as a
zone-outside-allowedZones violation by default. A deployment that wants
"no zone declared" to be a hard failure enforces that separately, before
calling this module -- KERNEL-SPEC does not require every deployment to
declare one.

**But it is written down.** Not refusing is not the same as saying
nothing. The zone is resolved from a value the runner supplies about
itself, so "no zone declared" is the one case where an ``enforcement:
block`` policy did not actually run -- and it used to leave the receipt
and the evidence pack byte-identical to a run that had been checked and
passed. :func:`enforce_sovereignty` now returns a
:class:`SovereigntyAuditRow` with :attr:`~SovereigntyAuditRow.enforced`
false and no zone for that case, so a reader of the artifacts can tell
"checked, inside the allow-list" from "never checked".

No wall clock is read here: :attr:`SovereigntyAuditRow.event_time` is
caller-supplied, the same discipline
:class:`flux_engine.api.lineage.LineageEvent` uses and for the same
reason -- KERNEL-SPEC keeps the read at the facade, never in a governed
decision.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from flux_engine.core.errors import FluxError, register

__all__ = [
    "FLUX_ZONE_ENV_VAR",
    "FLX_4001",
    "SovereigntyAuditRow",
    "SovereigntyPolicy",
    "SovereigntyViolation",
    "declared_zone",
    "enforce_sovereignty",
]

FLX_4001 = register(
    "FLX-4001",
    "declared zone is outside the document's allowed zones",
    "World.spec.sovereignty / Simulation.spec.sovereignty (schema $defs/sovereignty) "
    "declares the zones a run may execute in. The run's own declared zone -- the "
    "--zone flag, falling back to FLUX_ZONE -- was resolved and it is not a member "
    "of allowedZones. Under enforcement: block (the default) the run refuses to "
    "start; under enforcement: audit it does not, and a lineage-style row is "
    "recorded instead.",
)

#: The one environment variable this module ever reads, and the fallback
#: named in the schema's own vocabulary alongside ``--zone``. A deployment
#: switch, not a value that reaches a digest.
FLUX_ZONE_ENV_VAR = "FLUX_ZONE"


class SovereigntyViolation(FluxError):
    """Raised in ``enforcement: block`` when the declared zone is not allowed.

    Carries the offending zone and the policy's allow-list so a caller can
    report both without re-deriving them from the document.
    """

    def __init__(self, zone: str, allowed_zones: Sequence[str]) -> None:
        allowed = tuple(allowed_zones)
        super().__init__(
            FLX_4001,
            f"zone {zone!r} is not one of the allowed zones {sorted(allowed)!r}",
        )
        self.zone = zone
        self.allowed_zones = allowed


@dataclass(frozen=True)
class SovereigntyPolicy:
    """One document's resolved ``sovereignty`` block.

    ``enforcement`` mirrors the schema's own default: absent means
    ``"block"``, never an unenforced policy by omission.
    """

    allowed_zones: Tuple[str, ...]
    enforcement: str = "block"

    def __post_init__(self) -> None:
        if not self.allowed_zones:
            raise ValueError("sovereignty.allowedZones must not be empty")
        if self.enforcement not in ("block", "audit"):
            raise ValueError(
                f"sovereignty.enforcement must be 'block' or 'audit', got {self.enforcement!r}"
            )

    @classmethod
    def from_document(
        cls, sovereignty: Optional[Mapping[str, Any]]
    ) -> Optional["SovereigntyPolicy"]:
        """Build a policy from a document's ``spec.sovereignty`` block, or ``None``.

        Returns ``None`` for a document that declares no sovereignty block at
        all -- there is nothing to enforce, which is different from a
        declared-but-empty ``allowedZones`` (schema-invalid; the dataclass
        itself refuses that).
        """
        if sovereignty is None:
            return None
        zones = tuple(str(zone) for zone in sovereignty["allowedZones"])
        enforcement = sovereignty.get("enforcement", "block")
        return cls(allowed_zones=zones, enforcement=enforcement)


@dataclass(frozen=True)
class SovereigntyAuditRow:
    """A lineage-style record of a sovereignty check that did not come out clean.

    Two cases produce one: a zone that fell outside the declared allow-list
    under ``enforcement: audit`` (:attr:`enforced` true, :attr:`zone` set),
    and a policy that was never checked because the run declared no zone at
    all (:attr:`enforced` false, :attr:`zone` ``None``). Deliberately
    shaped close to :class:`flux_engine.api.lineage.LineageEvent` (job
    identity, a facets mapping) so :meth:`as_lineage_facets` slots straight
    into one without this module importing the port itself -- sovereignty
    is a governance decision; where the row travels afterward is
    deployment plumbing.
    """

    zone: Optional[str]
    allowed_zones: Tuple[str, ...]
    document_id: str
    document_kind: str
    event_time: str
    run_id: Optional[str] = None
    #: False only for the "no zone was declared, so nothing was checked"
    #: row. A consumer that treats every row as a violation would be
    #: reading the second case wrongly, which is why the facet it renders
    #: under is a different name too.
    enforced: bool = True

    def as_lineage_facets(self) -> Dict[str, Any]:
        """This row's content as a ``facets`` mapping for a lineage event.

        ``sovereigntyViolation`` for a zone outside the allow-list;
        ``sovereigntyNotEnforced`` for a policy that never ran. Two names,
        because "this run was somewhere it should not have been" and "we do
        not know where this run was" are different facts.
        """
        facet = "sovereigntyViolation" if self.enforced else "sovereigntyNotEnforced"
        return {
            facet: {
                "zone": self.zone,
                "allowedZones": list(self.allowed_zones),
                "documentId": self.document_id,
                "documentKind": self.document_kind,
                "runId": self.run_id,
            }
        }


def declared_zone(
    *, cli_zone: Optional[str] = None, env: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """Resolve the run's declared zone: ``--zone`` first, then ``FLUX_ZONE``.

    ``env`` defaults to ``os.environ`` -- the one environment read in this
    module, isolated to this function so a caller (or a test) can supply
    an explicit mapping instead. Returns ``None`` when neither is set (or
    both are blank), which :func:`enforce_sovereignty` treats as nothing to
    check.
    """
    if cli_zone is not None and cli_zone.strip():
        return cli_zone.strip()
    source: Mapping[str, str] = os.environ if env is None else env
    value = source.get(FLUX_ZONE_ENV_VAR)
    if value is not None and value.strip():
        return value.strip()
    return None


def enforce_sovereignty(
    policy: Optional[SovereigntyPolicy],
    *,
    zone: Optional[str],
    document_id: str,
    document_kind: str,
    event_time: str,
    run_id: Optional[str] = None,
) -> Optional[SovereigntyAuditRow]:
    """Apply *policy* to *zone*.

    Returns ``None`` when there is nothing to report: no policy declared,
    or the declared zone is allowed. Returns a
    :class:`SovereigntyAuditRow` in ``enforcement: audit`` when the zone is
    not allowed, and one with :attr:`SovereigntyAuditRow.enforced` false
    when a policy exists but the run declared no zone -- the case where the
    control did not run at all, in any enforcement mode. Raises
    :class:`SovereigntyViolation` in ``enforcement: block`` (the default)
    when the zone is not allowed.
    """
    if policy is None:
        return None
    if zone is None:
        return SovereigntyAuditRow(
            zone=None,
            allowed_zones=policy.allowed_zones,
            document_id=document_id,
            document_kind=document_kind,
            event_time=event_time,
            run_id=run_id,
            enforced=False,
        )
    if zone in policy.allowed_zones:
        return None
    if policy.enforcement == "block":
        raise SovereigntyViolation(zone, policy.allowed_zones)
    return SovereigntyAuditRow(
        zone=zone,
        allowed_zones=policy.allowed_zones,
        document_id=document_id,
        document_kind=document_kind,
        event_time=event_time,
        run_id=run_id,
    )
