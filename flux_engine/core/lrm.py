# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Largest Remainder Method -- KERNEL-SPEC §5.

The one place the engine turns declared *shares* into declared *counts*: a lifecycle mix
over a population, a temperature dial's frozen/stochastic split (§6), any cohort partition.

§5 states the algorithm and its three normative properties:

    Inputs: ``n: int >= 1``, ``weights: Mapping[str, Decimal]`` summing to 1 within 1e-9
    (validator already checks). Algorithm on exact rationals
    (``fractions.Fraction(str(weight))``): ``floor(n·w_i)`` each, residual units go to the
    largest fractional parts; **ties broken by NFC-sorted key** (never by seed). Output
    preserves the input key order. Property: sum equals ``n`` exactly; invariant to input
    order; changing the seed never changes cohort sizes.

Three things follow from "exact rationals", and each is a defect this module exists to
avoid:

* **No float accumulates.** Weights arrive as ``Decimal`` and become ``Fraction``
  immediately. A float-accumulating implementation gets the same answer on almost every
  input and a different one on the inputs that matter -- three tenths repeated, a weight
  whose binary expansion rounds the wrong way at the tenth subject -- and the difference
  lands in the population digest.
* **The tie-break carries no randomness.** Two equal fractional parts are separated by
  their NFC-sorted key, so a cohort split is a function of the declaration alone. A run
  under a different seed produces the same counts, which is what makes a seed sweep
  comparable across its runs.
* **The residual is exact.** ``n − Σ floor(n·w_i)`` is computed in integers, so the
  returned counts sum to ``n`` for every input rather than for almost every input.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Dict, Mapping, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError, register

__all__ = [
    "FLX_5020",
    "WEIGHT_SUM_TOLERANCE",
    "largest_remainder",
    "residual_order",
]

FLX_5020 = register(
    "FLX-5020",
    "declared weights do not sum to 1",
    "A set of declared shares -- a lifecycle mix, a categorical level set, any weighted "
    "partition -- must sum to 1 within 1e-9 (KERNEL-SPEC §5). The kernel refuses a set "
    "that does not rather than renormalising it: a renormalised weight is a number "
    "nobody declared, and it would be hashed into the run digest indistinguishably from "
    "an authored one.",
)

#: KERNEL-SPEC §5: weights "summing to 1 within 1e-9 (validator already checks)". The
#: kernel re-checks anyway -- a validator that ran on a different document, or did not run
#: at all, must not be able to turn into a silently rescaled population.
WEIGHT_SUM_TOLERANCE = Decimal("1e-9")


def _exact(weight: Decimal) -> Fraction:
    """The exact rational value of a declared weight.

    ``Fraction(Decimal)`` is exact: a ``Decimal`` is a rational by construction, so this
    conversion loses nothing. §5 spells it ``Fraction(str(weight))``, which is the same
    number by a longer road; the direct conversion is used so that a caller who hands in a
    ``Decimal`` carrying a declared scale (``0.150`` rather than ``0.15``) gets the value
    it spells, not a re-parse of its repr.
    """
    return Fraction(weight)


def _validate(n: int, weights: Mapping[str, Decimal]) -> Tuple[Tuple[str, Fraction], ...]:
    if n < 1:
        raise FluxError(FLX_5020, f"largest_remainder requires n >= 1, got {n}")
    if not weights:
        raise FluxError(FLX_5020, "largest_remainder requires at least one declared weight")

    exact: list[Tuple[str, Fraction]] = []
    total = Fraction(0)
    for key, weight in weights.items():
        value = _exact(weight)
        if value < 0:
            raise FluxError(FLX_5020, f"weight for {key!r} is negative")
        exact.append((key, value))
        total += value
    if abs(total - 1) > Fraction(WEIGHT_SUM_TOLERANCE):
        raise FluxError(
            FLX_5020,
            f"weights sum to {total.numerator}/{total.denominator}, "
            f"which is further than {WEIGHT_SUM_TOLERANCE} from 1",
        )
    return tuple(exact)


def residual_order(n: int, weights: Mapping[str, Decimal]) -> Tuple[str, ...]:
    """The order in which keys receive a residual unit (KERNEL-SPEC §5's tie-break).

    Largest fractional part first; ties broken by NFC-sorted key, and the raw key last so
    that two distinct keys sharing an NFC form still have a total order. Exposed because
    the order is the interesting half of the algorithm: a reader auditing a cohort split
    wants to see which key was next in line, not only which ones were served.
    """
    exact = _validate(n, weights)
    scaled = [(key, n * value) for key, value in exact]
    fractions = [(key, value - int(value)) for key, value in scaled]
    ordered = sorted(fractions, key=lambda item: (-item[1], nfc(item[0]), item[0]))
    return tuple(key for key, _ in ordered)


def largest_remainder(n: int, weights: Mapping[str, Decimal]) -> Dict[str, int]:
    """Split *n* units across *weights* by the Largest Remainder Method (KERNEL-SPEC §5).

    Returns a mapping from every declared key to its integer count, **in the input key
    order** -- the counts are what changed, not the shape of the declaration, so a caller
    can zip the result against the document it came from.

    The returned counts sum to *n* exactly, are invariant to the order the weights were
    declared in, and depend on no seed.
    """
    exact = _validate(n, weights)

    floors: Dict[str, int] = {}
    fractional: Dict[str, Fraction] = {}
    for key, value in exact:
        scaled = n * value
        whole = int(scaled)  # Fraction -> int truncates toward zero; scaled is >= 0.
        floors[key] = whole
        fractional[key] = scaled - whole

    residual = n - sum(floors.values())
    if residual < 0:  # pragma: no cover - only reachable if the sum check let through > 1
        raise FluxError(
            FLX_5020,
            f"floors already exceed n by {-residual}; the declared weights sum above 1",
        )

    ordered = sorted(
        fractional.items(),
        key=lambda item: (-item[1], nfc(item[0]), item[0]),
    )
    if residual > len(ordered):  # pragma: no cover - needs weights summing above 1 + 1e-9
        raise FluxError(
            FLX_5020,
            f"{residual} residual units for {len(ordered)} declared keys; "
            "the declared weights do not partition the whole",
        )
    for key, _ in ordered[:residual]:
        floors[key] += 1

    # Input key order, per §5's "Output preserves the input key order".
    return {key: floors[key] for key in weights}
