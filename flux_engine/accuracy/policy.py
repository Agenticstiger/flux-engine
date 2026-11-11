# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The promotion policy: a customer-owned document, never an engine default.

A grade or a verdict does not become a validity claim on its own -- an
owner decides, in writing, which claim bases their organisation is willing
to promote (surface in a report, gate a release on, hand to a customer)
and what ceiling applies to each. :class:`PromotionPolicy` is that
decision, loaded from a document the customer authors; this module never
invents one. :func:`default_promotion_policy` exists only so a caller with
no document yet gets FLUX RFC-05's own baseline (the historical-holdout
grade ceiling from :mod:`flux_engine.accuracy.records`) rather than an
empty policy that promotes nothing -- it is a starting point, not a
recommendation the engine is making silently on the owner's behalf.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Mapping, Optional

from flux_engine.accuracy.records import GRADE_ORDER
from flux_engine.api.accuracy import AccuracyOutcome, AccuracyVerdict
from flux_engine.api.claims import ClaimBasis
from flux_engine.core.errors import FluxError, register

__all__ = [
    "FLX_7011",
    "PromotionPolicy",
    "default_promotion_policy",
    "load_promotion_policy",
    "promote",
]

FLX_7011 = register(
    "FLX-7011",
    "promotion policy document is malformed",
    "A promotion-policy document must declare policy_id (a non-empty "
    "string) and, when present, grade_ceiling as a mapping from a "
    "ClaimBasis value to one of the six FLUX letter grades. A malformed "
    "policy is refused rather than loaded with a field silently defaulted, "
    "because a promotion decision an owner did not actually author is not "
    "one this engine will honour.",
)

#: FLUX RFC-05's own ceiling, restated: a historical-holdout basis alone
#: never promotes past "C". Live-comparison bases (and "none", which
#: promotes nothing regardless of ceiling) carry no entry -- uncapped.
_DEFAULT_GRADE_CEILING: Mapping[str, str] = {ClaimBasis.HISTORICAL_HOLDOUT.value: "C"}


@dataclass(frozen=True)
class PromotionPolicy:
    """What an owner is willing to promote, and how far.

    Attributes
    ----------
    policy_id
        Identifies the document, for a receipt that names which policy
        governed a promotion decision.
    grade_ceiling
        Maps a :class:`~flux_engine.api.claims.ClaimBasis` value to the
        best letter grade that basis may report (see
        :func:`~flux_engine.accuracy.records.clamp_grade_for_basis`). A
        basis absent from the mapping is uncapped by this policy.
    allowed_bases
        The claim bases this policy will promote at all. A verdict whose
        basis is outside this set is never promoted, whatever its outcome.
        Defaults to every basis except
        :attr:`~flux_engine.api.claims.ClaimBasis.NONE` -- "nothing was
        measured" promotes nothing under any policy.
    """

    policy_id: str
    grade_ceiling: Mapping[str, str] = field(default_factory=lambda: dict(_DEFAULT_GRADE_CEILING))
    allowed_bases: FrozenSet[str] = field(
        default_factory=lambda: frozenset(
            {ClaimBasis.LIVE_AB.value, ClaimBasis.HISTORICAL_HOLDOUT.value}
        )
    )


def default_promotion_policy(policy_id: str = "flux.accuracy.default/1") -> PromotionPolicy:
    """Return FLUX RFC-05's own baseline policy: the historical-holdout ceiling, nothing more.

    A starting point for a caller with no customer-authored policy yet --
    not a recommendation this engine makes silently, and never substituted
    for a document an owner has actually authored (see the module
    docstring).
    """
    return PromotionPolicy(policy_id=policy_id)


def load_promotion_policy(document: Mapping[str, Any]) -> PromotionPolicy:
    """Parse a customer-authored policy document into a :class:`PromotionPolicy`.

    Raises ``FLX-7011`` on a document missing ``policy_id``, or whose
    ``grade_ceiling`` names a basis outside :class:`ClaimBasis` or a grade
    outside FLUX's six letter grades.
    """
    if not isinstance(document, Mapping):
        raise FluxError(
            FLX_7011, f"policy document must be a mapping, got {type(document).__name__}"
        )
    policy_id = document.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise FluxError(FLX_7011, "policy_id must be a non-empty string")

    known_bases = {basis.value for basis in ClaimBasis}
    raw_ceiling = document.get("grade_ceiling", _DEFAULT_GRADE_CEILING)
    if not isinstance(raw_ceiling, Mapping):
        raise FluxError(FLX_7011, "grade_ceiling must be a mapping")
    ceiling: Dict[str, str] = {}
    for basis, grade in raw_ceiling.items():
        if basis not in known_bases:
            raise FluxError(FLX_7011, f"grade_ceiling names unknown basis {basis!r}")
        if grade not in GRADE_ORDER:
            raise FluxError(
                FLX_7011, f"grade_ceiling[{basis!r}] must be one of {GRADE_ORDER}, got {grade!r}"
            )
        ceiling[basis] = grade

    raw_allowed = document.get(
        "allowed_bases", [ClaimBasis.LIVE_AB.value, ClaimBasis.HISTORICAL_HOLDOUT.value]
    )
    if not isinstance(raw_allowed, (list, tuple)):
        raise FluxError(FLX_7011, "allowed_bases must be a list of ClaimBasis values")
    for basis in raw_allowed:
        if basis not in known_bases:
            raise FluxError(FLX_7011, f"allowed_bases names unknown basis {basis!r}")

    return PromotionPolicy(
        policy_id=policy_id,
        grade_ceiling=ceiling,
        allowed_bases=frozenset(raw_allowed),
    )


def promote(verdict: AccuracyVerdict, policy: PromotionPolicy) -> Optional[AccuracyVerdict]:
    """Return *verdict* when *policy* allows it to support a validity claim, else ``None``.

    A verdict is never promoted when it was not actually scored
    (:attr:`~flux_engine.api.accuracy.AccuracyOutcome.NOT_SCORED`) or when
    its basis is outside :attr:`PromotionPolicy.allowed_bases`. The
    returned verdict is *unchanged* -- :class:`PromotionPolicy` decides
    whether a verdict counts, not what it says; a grade ceiling constrains
    a *letter grade* computed elsewhere (see
    :func:`~flux_engine.accuracy.records.clamp_grade_for_basis`), which
    this function does not compute because v0.1 has no grade to clamp
    (see :mod:`flux_engine.accuracy._scoring`).
    """
    if verdict.outcome is AccuracyOutcome.NOT_SCORED:
        return None
    if verdict.basis.value not in policy.allowed_bases:
        return None
    return verdict
