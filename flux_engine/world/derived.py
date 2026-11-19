# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Vertical-pack derived subject attributes (KERNEL-SPEC §3, ``...,"products"``).

The seed-tree table gives one stream per subject to "vertical-pack derived
attributes", and EXPR-SPEC §3 gives the vertical pack the *types* of those
attributes -- telco's ``products: set<string>``, ``tenureMonths: number``,
``latePaymentCount: number``, ``arpu: number``. What the pack deliberately does
not carry is the shapes those values are drawn from: "a pack never carries a
value, a rate or a distribution, so nothing in this module can put a number
into a hashed document" (``flux_engine/verticals/telco.py``).

This module carries them, and is the **v0.1 stopgap** that the pack's own
docstring anticipates: "FLUX 0.6 RFC moves the declaration into the spec". Until
it does, the shapes are engine-owned, exactly as ``tickMs`` is engine-owned in
KERNEL-SPEC §7 -- declared in one table, written as exact decimal strings, and
therefore part of ``kernelVersion``: changing a number here moves the goldens on
purpose and needs a ``GOLDEN-CHANGELOG.md`` row like any other kernel change.

Two determinism rules the table must keep, both pinned by tests:

* every attribute is drawn on **one** stream, in NFC-sorted attribute order, so
  adding an attribute changes only the values after it in that order;
* the word cost of a subject's derivation does **not** depend on that subject's
  lifecycle, even though the ``latePaymentCount`` rate does -- a lifecycle-
  dependent cost would make one subject's stream position depend on another
  document's cohort allocation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Mapping, Tuple

from flux_engine.core.errors import FluxError
from flux_engine.expr.attrs import VerticalPack
from flux_engine.expr.types import Type, nfc
from flux_engine.world.errors import FLX_5040
from flux_engine.world.families import (
    BoundedNormal,
    Family,
    RateCount,
    SetOf,
    StreamFactory,
    drawn_type_satisfies,
)

__all__ = ["DERIVATIONS", "Derivation", "derivation_for", "TELCO_DERIVATION", "TOWERS_DERIVATION"]


@dataclass(frozen=True)
class Derivation:
    """How one vertical's derived subject attributes are drawn.

    ``attributes`` is the base family per attribute name. ``by_lifecycle``
    overrides a family for subjects in one lifecycle cohort; an override must
    keep the base family's :attr:`~flux_engine.world.families.Family.word_cost`,
    which :meth:`__post_init__` checks, so the cohort a subject landed in can
    change the *value* it draws but never the *position* the stream is left in.
    """

    vertical: str
    attributes: Mapping[str, Family]
    by_lifecycle: Mapping[str, Mapping[str, Family]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for lifecycle, overrides in self.by_lifecycle.items():
            for name, family in overrides.items():
                base = self.attributes.get(name)
                if base is None:
                    raise ValueError(
                        f"{self.vertical}: lifecycle {lifecycle!r} overrides unknown "
                        f"attribute {name!r}"
                    )
                if base.word_cost != family.word_cost:
                    raise ValueError(
                        f"{self.vertical}: the {lifecycle!r} override of {name!r} costs "
                        f"{family.word_cost} words, the base family costs {base.word_cost}; "
                        "a lifecycle must not move the stream position"
                    )

    @property
    def names(self) -> Tuple[str, ...]:
        """The derived attribute names in the NFC-sorted order :meth:`draw` uses."""
        return tuple(sorted(self.attributes, key=nfc))

    def families_for(self, lifecycle: str) -> Mapping[str, Family]:
        """The family per attribute for a subject in *lifecycle*."""
        overrides = self.by_lifecycle.get(nfc(lifecycle), {})
        return {name: overrides.get(name, self.attributes[name]) for name in self.names}

    def word_cost(self) -> int:
        """Raw words one subject's derivation consumes. Lifecycle-independent."""
        return sum(self.attributes[name].word_cost for name in self.names)

    def draw(self, open_stream: StreamFactory, *, lifecycle: str) -> Dict[str, Any]:
        """Draw every derived attribute for one subject, in NFC-sorted name order.

        Every attribute draws on the one stream *open_stream* yields, so the
        factory is expected to be memoised by path -- which
        :meth:`flux_engine.core.seedtree.SeedTree.stream` is.
        """
        families = self.families_for(lifecycle)
        return {name: families[name].draw(open_stream) for name in self.names}

    def types(self) -> Mapping[str, Type]:
        """The ``flux-expr/1`` type of each derived attribute, for cross-checking a pack."""
        return {name: self.attributes[name].expr_type for name in self.names}

    def integral_names(self) -> Tuple[str, ...]:
        """The attributes every subject carries as a whole number, in :attr:`names` order.

        An attribute qualifies only when its base family and every lifecycle
        override of it are :attr:`~flux_engine.world.families.Family.integral`:
        the cohort a subject lands in picks the family, and an ``INT`` column
        fed by the attribute must hold in every cohort, not only in the ones
        one run happened to draw.
        """
        return tuple(
            name
            for name in self.names
            if self.attributes[name].integral
            and all(
                overrides[name].integral
                for overrides in self.by_lifecycle.values()
                if name in overrides
            )
        )

    def check_against(self, pack: VerticalPack) -> None:
        """Fail (``FLX-5040``) unless this table draws exactly what *pack* declares.

        The pack owns the names and types the expression language type-checks
        against; this table owns the shapes. If the two disagree, a segment
        query would type-check against an attribute no subject carries (or a
        subject would carry an attribute typed as something else), so the two
        are compared once, at plan time, rather than diverging silently.
        """
        declared = {nfc(name): t for name, t in pack.subject_attributes.items()}
        drawn = self.types()
        missing = sorted(set(declared) - set(drawn))
        extra = sorted(set(drawn) - set(declared))
        if missing or extra:
            raise FluxError(
                FLX_5040,
                f"vertical {pack.name!r}: the pack declares {missing or []} that no "
                f"derivation draws, and the derivation draws {extra or []} the pack does "
                "not declare",
            )
        for name in sorted(declared):
            if not drawn_type_satisfies(declared[name], drawn[name]):
                raise FluxError(
                    FLX_5040,
                    f"vertical {pack.name!r}: attribute {name!r} is declared "
                    f"{declared[name].name} by the pack and drawn as {drawn[name].name}",
                )


def _d(literal: str) -> Decimal:
    """An engine-owned constant, written as an exact decimal string (never a float)."""
    return Decimal(literal)


#: The telco table. Every number is a string literal; every family is one of
#: SEAM-FIDELITY-SPEC §1's four. ``latePaymentCount`` is the only
#: lifecycle-conditioned attribute: an at-risk subscriber is at risk *because*
#: of missed payments, and a table that drew the same rate for every cohort
#: would make ``seg-atrisk-tv`` and ``pb-billing-history``'s
#: ``late_payment_count >= 2`` describe unrelated populations.
TELCO_DERIVATION = Derivation(
    vertical="telco",
    attributes={
        "arpu": BoundedNormal(
            mean=_d("42.00"), stddev=_d("12.00"), lo=_d("5.00"), hi=_d("250.00"), scale=2
        ),
        "latePaymentCount": RateCount(mean=_d("0.30")),
        "products": SetOf(
            {"broadband": _d("0.70"), "mobile": _d("0.50"), "tv": _d("0.35")},
        ),
        "tenureMonths": BoundedNormal(
            mean=_d("36"), stddev=_d("18"), lo=_d("0"), hi=_d("240"), scale=0
        ),
    },
    by_lifecycle={
        "new": {"latePaymentCount": RateCount(mean=_d("0.10"))},
        "active": {"latePaymentCount": RateCount(mean=_d("0.20"))},
        "atrisk": {"latePaymentCount": RateCount(mean=_d("2.50"))},
        "churned": {"latePaymentCount": RateCount(mean=_d("1.50"))},
    },
)

#: The towers table: one subject is one tenant in one market
#: (:mod:`flux_engine.verticals.towers`). Two attributes are
#: lifecycle-conditioned, because they are what makes a tenant at risk at
#: renewal: an at-risk or notice-giving tenant has more chronic SLA breaches
#: and fewer months left on its lease than an active one. A tenant still
#: onboarding has a long lease ahead and almost no breach history; a churned
#: tenant's lease has run out. ``tenancyCount``, ``monthlyRecurringCharge`` and
#: ``slaTargetPct`` describe the contract, not the relationship's health, so
#: they draw the same way in every cohort. Every override keeps its base
#: family's word cost, which :class:`Derivation` checks.
TOWERS_DERIVATION = Derivation(
    vertical="towers",
    attributes={
        "chronicSlaBreaches": RateCount(mean=_d("0.40")),
        "monthlyRecurringCharge": BoundedNormal(
            mean=_d("1450.00"), stddev=_d("420.00"), lo=_d("300.00"), hi=_d("6000.00"), scale=2
        ),
        "monthsToLeaseExpiry": BoundedNormal(
            mean=_d("54"), stddev=_d("30"), lo=_d("0"), hi=_d("180"), scale=0
        ),
        "slaTargetPct": BoundedNormal(
            mean=_d("99.80"), stddev=_d("0.12"), lo=_d("99.00"), hi=_d("99.99"), scale=2
        ),
        "tenancyCount": BoundedNormal(
            mean=_d("220"), stddev=_d("240"), lo=_d("1"), hi=_d("4000"), scale=0
        ),
    },
    by_lifecycle={
        "onboarding": {
            "chronicSlaBreaches": RateCount(mean=_d("0.05")),
            "monthsToLeaseExpiry": BoundedNormal(
                mean=_d("118"), stddev=_d("12"), lo=_d("60"), hi=_d("180"), scale=0
            ),
        },
        "active": {"chronicSlaBreaches": RateCount(mean=_d("0.30"))},
        "atrisk": {
            "chronicSlaBreaches": RateCount(mean=_d("2.80")),
            "monthsToLeaseExpiry": BoundedNormal(
                mean=_d("14"), stddev=_d("8"), lo=_d("0"), hi=_d("36"), scale=0
            ),
        },
        "notice": {
            "chronicSlaBreaches": RateCount(mean=_d("3.60")),
            "monthsToLeaseExpiry": BoundedNormal(
                mean=_d("5"), stddev=_d("3"), lo=_d("0"), hi=_d("12"), scale=0
            ),
        },
        "churned": {
            "chronicSlaBreaches": RateCount(mean=_d("2.20")),
            "monthsToLeaseExpiry": BoundedNormal(
                mean=_d("0"), stddev=_d("1"), lo=_d("0"), hi=_d("2"), scale=0
            ),
        },
    },
)

#: Every vertical this engine can materialise, keyed the way
#: :data:`flux_engine.verticals.PACKS` is keyed.
DERIVATIONS: Mapping[str, Derivation] = {
    TELCO_DERIVATION.vertical: TELCO_DERIVATION,
    TOWERS_DERIVATION.vertical: TOWERS_DERIVATION,
}


def derivation_for(vertical: str) -> Derivation:
    """Return the derivation table for *vertical*; ``FLX-5040`` when there is none.

    A missing table is a hard failure rather than an empty derivation: an empty
    one would leave every vertical attribute null, which makes
    ``products.has('tv')`` quietly false for the whole population instead of
    naming the vertical the engine cannot materialise.
    """
    key = nfc(vertical)
    if key not in DERIVATIONS:
        raise FluxError(
            FLX_5040,
            f"no derivation table for vertical {vertical!r}; this engine materialises "
            f"{', '.join(sorted(DERIVATIONS))}",
        )
    return DERIVATIONS[key]
