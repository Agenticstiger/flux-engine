# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A profile released under differential privacy: its accounting, its noise and its point estimates.

``docs/spec/PROFILE-SPEC.md`` section 3a is the normative description. A profiler that adds noise
states one release-level account (:class:`Accounting`): the measure, the total privacy loss and
the unit of privacy. Each noisy statistic and each noisy clique states its mechanism, its scale
and its share of the total (:class:`Noise`). This module reads those blocks, checks that the
shares add up to no more than the total (ADR 0019), and turns a noisy vector of counts into a
valid distribution, deterministically and in exact arithmetic (:func:`point_estimate`).

Vocabulary, not dependency: the names measure, privacy unit, privacy loss and scale are
OpenDP's (https://docs.opendp.org, ``opendp.measures``). Zero-concentrated DP is
``zero_concentrated_divergence`` and approximate DP is ``fixed_smoothed_max_divergence`` there.
The projection onto the probability simplex is the sort-based algorithm of Duchi, Shalev-Shwartz,
Singer and Chandra, "Efficient projections onto the l1-ball for learning in high dimensions"
(ICML 2008), the "Norm-Sub" post-processing of Wang et al. for noisy frequencies, written in
:class:`fractions.Fraction` so every platform computes the same numbers.

Nothing here draws noise or reads a record: a number that arrives is a number the profiler
released, and everything computed from it is post-processing of that release.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_CEILING,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
)
from fractions import Fraction
from math import isqrt
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple

from flux_stats.spelling import read_decimal, refuse

__all__ = [
    "APPROXIMATE_DP",
    "Accounting",
    "MECHANISMS",
    "MEASURES",
    "Noise",
    "PointEstimate",
    "PrivacyLoss",
    "PrivacyShare",
    "Spent",
    "ZERO_CONCENTRATED_DP",
    "check_budget",
    "point_estimate",
    "read_accounting",
    "read_noise",
    "read_share",
    "sampling_widening",
    "variance_of",
    "widening",
]

#: Zero-concentrated DP: the loss is rho, and rho adds under composition. OpenDP's
#: ``zero_concentrated_divergence``.
ZERO_CONCENTRATED_DP = "zeroConcentratedDP"
#: Approximate DP: the loss is (epsilon, delta), and each adds under basic composition. Pure DP
#: is delta = 0. OpenDP's ``fixed_smoothed_max_divergence``.
APPROXIMATE_DP = "approximateDP"
MEASURES: Tuple[str, ...] = (ZERO_CONCENTRATED_DP, APPROXIMATE_DP)

#: The mechanisms a noisy statistic may name, with the variance of one noisy cell for a scale
#: ``s`` (:attr:`Noise.variance`): Gaussian ``s^2``; discrete Gaussian at most ``s^2`` (the engine
#: takes ``s^2``); Laplace ``2 s^2``.
MECHANISMS: Tuple[str, ...] = ("gaussian", "discreteGaussian", "laplace")


@dataclass(frozen=True)
class PrivacyLoss:
    """A privacy loss in one measure: ``rho``, or ``epsilon`` and ``delta``."""

    rho: Optional[Fraction] = None
    epsilon: Optional[Fraction] = None
    delta: Optional[Fraction] = None

    def as_document(self) -> Mapping[str, str]:
        """The loss as a document spells it, each number a decimal string."""
        out = {}
        for key in ("rho", "epsilon", "delta"):
            value = getattr(self, key)
            if value is not None:
                out[key] = _text(value)
        return out


def _text(value: Fraction) -> str:
    """An exact decimal text of *value* when it terminates, else ``numerator/denominator``."""
    denominator = value.denominator
    while denominator % 2 == 0:
        denominator //= 2
    while denominator % 5 == 0:
        denominator //= 5
    if denominator != 1:
        return f"{value.numerator}/{value.denominator}"
    text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True)
class Accounting:
    """The release-level account: the measure, the total privacy loss and the unit of privacy.

    ``unit`` is an expose id: neighbouring data sets differ by adding or removing one row of that
    expose (OpenDP's symmetric distance at distance one).
    """

    measure: str
    total: PrivacyLoss
    unit: str
    #: The profiler declares that the levels of its histograms, their bin edges, the
    #: ``sampleSize`` bands and every ``rowFloor`` of a rank correlation were chosen without the
    #: data, or are part of the release (PROFILE-SPEC.md section 3a). The engine cannot check it.
    public_structure: bool = False


@dataclass(frozen=True)
class Noise:
    """One noisy statistic's mechanism, scale and share of the budget."""

    mechanism: str
    scale: Fraction
    loss: PrivacyLoss

    @property
    def variance(self) -> Fraction:
        """The variance of one noisy cell, exact. The discrete Gaussian's is not above ``scale^2``."""
        return variance_of(self.mechanism, self.scale)


@dataclass(frozen=True)
class PrivacyShare:
    """A share of the budget with no mechanism and no scale: a bare ``privacyLoss``.

    It is what choosing cliques or releasing a domain spent (``cliqueSelection``): the profiler
    states the loss, and there is no noise block to check it against. It adds to the account as a
    :class:`Noise` does, in the account's measure.
    """

    loss: PrivacyLoss


class Spent(Protocol):
    """A part of the account: anything that states the privacy loss it spends."""

    @property
    def loss(self) -> PrivacyLoss: ...


def variance_of(mechanism: str, scale: Fraction) -> Fraction:
    """The variance of one cell released by *mechanism* at *scale*: ``scale^2``, or twice it for Laplace."""
    square = scale * scale
    return 2 * square if mechanism == "laplace" else square


def _mapping(value: Any, where: str, keys: Sequence[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise refuse(where, f"expected an object, got {type(value).__name__}")
    found = {str(key) for key in value}
    unknown = sorted(found - set(keys))
    if unknown:
        raise refuse(where, f"unknown key {unknown[0]!r}")
    missing = [key for key in keys if key not in found]
    if missing:
        raise refuse(where, f"missing key {missing[0]!r}")
    return value


def _loss(value: Any, where: str, measure: str) -> PrivacyLoss:
    """A privacy loss in *measure*: positive rho, or positive epsilon and a delta in [0, 1)."""
    if measure == ZERO_CONCENTRATED_DP:
        raw = _mapping(value, where, ("rho",))
        rho = read_decimal(raw["rho"], f"{where}.rho")
        if rho <= 0 or str(raw["rho"]).startswith("-"):
            raise refuse(f"{where}.rho", "a privacy loss is above zero")
        return PrivacyLoss(rho=rho)
    raw = _mapping(value, where, ("epsilon", "delta"))
    epsilon = read_decimal(raw["epsilon"], f"{where}.epsilon")
    delta = read_decimal(raw["delta"], f"{where}.delta")
    if epsilon <= 0:
        raise refuse(f"{where}.epsilon", "a privacy loss is above zero")
    if not 0 <= delta < 1:
        raise refuse(f"{where}.delta", "delta lies in [0, 1)")
    return PrivacyLoss(epsilon=epsilon, delta=delta)


def read_accounting(value: Any, where: str) -> Accounting:
    """Read the ``privacy`` block: ``measure``, ``privacyLoss`` and ``privacyUnit``."""
    allowed = {"measure", "privacyLoss", "privacyUnit", "publicStructure"}
    unknown = (
        sorted(str(key) for key in value if key not in allowed)
        if isinstance(value, Mapping)
        else []
    )
    if unknown:
        raise refuse(where, f"unknown key {unknown[0]!r}")
    declared = value.get("publicStructure") if isinstance(value, Mapping) else None
    if isinstance(value, Mapping) and "publicStructure" in value and not isinstance(declared, bool):
        # A present null is not "undeclared": the schema holds a boolean, and so does this.
        raise refuse(f"{where}.publicStructure", "is true or false")
    raw = _mapping(
        (
            {key: item for key, item in value.items() if key != "publicStructure"}
            if isinstance(value, Mapping)
            else value
        ),
        where,
        ("measure", "privacyLoss", "privacyUnit"),
    )
    measure = raw["measure"]
    if measure not in MEASURES:
        raise refuse(f"{where}.measure", f"a measure is one of {list(MEASURES)}")
    unit = _mapping(raw["privacyUnit"], f"{where}.privacyUnit", ("expose",))
    expose = unit["expose"]
    if not isinstance(expose, str) or not expose:
        raise refuse(f"{where}.privacyUnit.expose", "the unit of privacy is an expose id")
    return Accounting(
        measure=measure,
        total=_loss(raw["privacyLoss"], f"{where}.privacyLoss", measure),
        unit=expose,
        public_structure=declared is True,
    )


def read_noise(
    value: Any,
    where: str,
    accounting: Accounting,
    *,
    l1: Fraction,
    l2_squared: Fraction,
) -> Noise:
    """Read a ``noise`` block: ``mechanism``, ``scale`` and the share ``privacyLoss``.

    ``l1`` and ``l2_squared`` are the L1 sensitivity and the squared L2 sensitivity of what the
    block noises, under adding or removing one row of the unit's expose. They have no default: a
    count table passes ``Fraction(1)`` for both, written out, so a caller that forgets them fails
    instead of charging a sensitivity of one by accident.
    """
    raw = _mapping(value, where, ("mechanism", "scale", "privacyLoss"))
    mechanism = raw["mechanism"]
    if mechanism not in MECHANISMS:
        raise refuse(f"{where}.mechanism", f"a mechanism is one of {list(MECHANISMS)}")
    scale = read_decimal(raw["scale"], f"{where}.scale")
    if scale <= 0 or str(raw["scale"]).startswith("-"):
        raise refuse(f"{where}.scale", "a noise scale is above zero")
    loss = _loss(raw["privacyLoss"], f"{where}.privacyLoss", accounting.measure)
    _require_spent(mechanism, scale, loss, f"{where}.privacyLoss", l1=l1, l2_squared=l2_squared)
    return Noise(mechanism=mechanism, scale=scale, loss=loss)


def read_share(value: Any, where: str, accounting: Accounting) -> PrivacyShare:
    """Read ``{"privacyLoss": ...}``, a share stated without a mechanism, in the account's measure."""
    raw = _mapping(value, where, ("privacyLoss",))
    return PrivacyShare(loss=_loss(raw["privacyLoss"], f"{where}.privacyLoss", accounting.measure))


#: The arithmetic that turns a rho and a delta into an epsilon: 60 significant digits and every
#: inexact result rounded up, so the same digits on every platform and never below the exact value.
#: Written out here, not taken from the thread's context, so no setting of the caller can move it.
_EPSILON_CONTEXT = Context(
    prec=60,
    rounding=ROUND_CEILING,
    Emin=-999_999_999,
    Emax=999_999_999,
    traps=[InvalidOperation, Overflow, DivisionByZero],
)


def _epsilon_at_delta(rho: Fraction, delta: Fraction) -> Fraction:
    """An upper bound on ``rho + 2 sqrt(rho ln(1 / delta))``, as an exact rational.

    Bun and Steinke (2016), Proposition 1.3: a mechanism that is rho-zero-concentrated DP is
    (epsilon, delta)-DP for every delta in (0, 1) with this epsilon. The value is computed in
    :class:`decimal.Decimal` at 60 digits with every inexact step rounded up. The logarithm and the
    square root are the two operations that round half to even whatever the context says, so each is
    moved to the next representable number above, which is never below the exact value. The result
    is a little above the true epsilon (about 1e-58 of it) and never below it, so a stated epsilon
    that reaches it is at least what the mechanism spends. ``rho`` is above zero and ``delta`` in
    ``(0, 1)``. A value the arithmetic cannot hold raises :class:`ArithmeticError`.
    """
    ctx = _EPSILON_CONTEXT
    rho_up = ctx.divide(Decimal(rho.numerator), Decimal(rho.denominator))
    inverse = ctx.divide(Decimal(delta.denominator), Decimal(delta.numerator))
    log_up = ctx.next_plus(ctx.ln(inverse))
    root_up = ctx.next_plus(ctx.sqrt(ctx.multiply(rho_up, log_up)))
    epsilon = ctx.add(rho_up, ctx.multiply(Decimal(2), root_up))
    return Fraction(epsilon)


def _require_spent(
    mechanism: str,
    scale: Fraction,
    loss: PrivacyLoss,
    where: str,
    *,
    l1: Fraction,
    l2_squared: Fraction,
) -> None:
    """Refuse a share below what the mechanism spends on a release of the stated sensitivity (ADR 0019).

    ``l1`` is the L1 sensitivity and ``l2_squared`` the squared L2 sensitivity of the released
    vector under adding or removing one row of the unit's expose. A count table has both equal to
    one: a row lands in one cell. Each pairing of a mechanism and a measure is checked, in exact
    arithmetic, and a stated share is at least what is spent (a larger one is slack):

    * A Gaussian or discrete Gaussian of scale ``s`` spends rho = ``l2_squared`` / (2 s^2)
      (Canonne, Kamath and Steinke 2020 for the discrete one). Under zero-concentrated DP the
      share is at least that rho.
    * A Gaussian or discrete Gaussian under approximate DP states a delta in (0, 1), for it never
      satisfies delta = 0, and an epsilon at least ``rho + 2 sqrt(rho ln(1 / delta))`` with that
      rho (Bun and Steinke 2016, Proposition 1.3). The bound is computed by
      :func:`_epsilon_at_delta`, rounded up, and compared with the stated epsilon exactly.
    * A Laplace of scale ``b`` spends epsilon = ``l1`` / b under approximate DP, with any delta.
    * A Laplace under zero-concentrated DP spends rho = (``l1`` / b)^2 / 2, since an epsilon-DP
      release is (epsilon^2 / 2)-zero-concentrated DP (Bun and Steinke 2016, Proposition 1.4). A
      tighter stated rho cannot be checked and is refused.

    A refusal names the formula and never a value of the document.
    """
    if l1 <= 0 or l2_squared <= 0:
        raise ValueError("a sensitivity is above zero")
    if mechanism == "laplace":
        _require_laplace(scale, loss, where, l1)
    else:
        _require_gaussian(scale, loss, where, l2_squared)


def _require_laplace(scale: Fraction, loss: PrivacyLoss, where: str, l1: Fraction) -> None:
    """A Laplace's share: epsilon at least ``l1 / scale``, or rho at least half its square."""
    epsilon = l1 / scale
    if loss.rho is not None:
        if loss.rho < epsilon * epsilon / 2:
            raise refuse(
                where,
                f"states a rho below ({_text(l1)} / scale)^2 / 2, "
                "what a Laplace spends under zero-concentrated DP",
            )
    elif loss.epsilon is not None and loss.epsilon < epsilon:
        raise refuse(
            where, f"states an epsilon below {_text(l1)} / scale, what a count table spends"
        )


def _require_gaussian(scale: Fraction, loss: PrivacyLoss, where: str, l2_squared: Fraction) -> None:
    """A (discrete) Gaussian's share: rho at least ``l2_squared / (2 scale^2)``, or its epsilon."""
    rho = l2_squared / (2 * scale * scale)
    if loss.rho is not None:
        if loss.rho < rho:
            raise refuse(
                where,
                f"states a rho below {_text(l2_squared)} / (2 scale^2), what a count table spends",
            )
        return
    if loss.epsilon is None or loss.delta is None:
        return
    if not 0 < loss.delta < 1:
        raise refuse(
            where, "states a delta outside (0, 1), and a Gaussian never satisfies delta = 0"
        )
    try:
        needed = _epsilon_at_delta(rho, loss.delta)
    except (ArithmeticError, ValueError):
        raise refuse(
            where, "states a delta or a scale too extreme for its epsilon to be checked"
        ) from None
    if loss.epsilon < needed:
        raise refuse(
            where,
            "states an epsilon below rho + 2 sqrt(rho ln(1 / delta)) with rho = "
            f"{_text(l2_squared)} / (2 scale^2), what a Gaussian spends at that delta",
        )


def check_budget(accounting: Accounting, parts: Sequence[Tuple[str, Spent]]) -> None:
    """Refuse a release whose parts add up to more than its total (ADR 0019).

    Under zero-concentrated DP the parts' rho adds; under approximate DP their epsilon adds and
    their delta adds (basic composition). Each is compared on its own, in exact arithmetic. A
    release with no noisy part is refused too: its account states a loss nothing spends.
    ``parts`` pairs each part's place in the document with its :class:`Noise` or
    :class:`PrivacyShare`.
    """
    if not parts:
        raise refuse(
            "profile.privacy", "states a privacy loss that no noisy statistic or clique spends"
        )
    total = accounting.total
    for key in ("rho", "epsilon", "delta"):
        limit = getattr(total, key)
        if limit is None:
            continue
        spent = sum((getattr(noise.loss, key) for _, noise in parts), Fraction(0))
        if spent > limit:
            names = ", ".join(where for where, _ in parts[:3])
            more = "" if len(parts) <= 3 else f" and {len(parts) - 3} more"
            raise refuse(
                "profile.privacy",
                f"the parts add up to more than the total {key} "
                f"({len(parts)} parts, among them {names}{more})",
            )


# ---------------------------------------------------------------------------
# A noisy vector of counts as a distribution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PointEstimate:
    """A noisy vector of cell counts read as a point estimate: a distribution and a size.

    ``total`` is the sum of the noisy values, the estimate of the records behind them (it can be
    zero or below when the noise swamps the counts). ``weights`` are the cells after the
    projection of :func:`point_estimate`: non-negative, adding up to ``total`` when ``total`` is
    above zero, and equal otherwise.
    """

    total: Fraction
    weights: Tuple[Fraction, ...]

    @property
    def shares(self) -> Tuple[Fraction, ...]:
        """The cells as a distribution: non-negative and adding up to exactly one."""
        if self.total <= 0:
            return tuple(Fraction(1, len(self.weights)) for _ in self.weights)
        return tuple(weight / self.total for weight in self.weights)

    @property
    def records(self) -> int:
        """The estimated record count: the total rounded half up, and zero when it is not above zero."""
        if self.total <= 0:
            return 0
        return int(self.total + Fraction(1, 2))

    @property
    def counts(self) -> Tuple[int, ...]:
        """Whole counts that add up to :attr:`records`: the shares by the largest remainder.

        Each cell takes the whole part of its share of the records, and the records that are left
        go one each to the cells with the largest remainders, the lowest position first among
        equals. All-integer, deterministic, and equal to the noisy values themselves when those
        are whole, non-negative and add up to a whole number.
        """
        size = self.records
        if size == 0:
            return tuple(0 for _ in self.weights)
        exact = [share * size for share in self.shares]
        counts = [int(value) for value in exact]
        order = sorted(range(len(exact)), key=lambda i: (-(exact[i] - counts[i]), i))
        for position in order[: size - sum(counts)]:
            counts[position] += 1
        return tuple(counts)


def point_estimate(noisy: Sequence[Fraction]) -> PointEstimate:
    """The noisy counts as a valid distribution, by one rule, in exact arithmetic.

    Let ``x`` be the noisy values and ``T`` their sum. When ``T`` is above zero the estimate is the
    Euclidean projection of ``x`` onto ``{p : p >= 0, sum p = T}``: each cell is ``max(x_i - t, 0)``
    for the one threshold ``t`` that makes them add up to ``T`` (found by sorting, exactly). A
    vector that is already non-negative is returned as it is (``t = 0``), and a negative cell
    takes the mass it lacked from the cells above it, evenly. The shares are the cells over
    ``T``. When ``T`` is not above zero the vector carries no information about the shares, and
    they are equal. The result depends on the values alone, never on their order of arrival.
    """
    if not noisy:
        raise ValueError("a vector of counts has at least one cell")
    values = [Fraction(value) for value in noisy]
    total = sum(values, Fraction(0))
    if total <= 0:
        return PointEstimate(total=total, weights=tuple(Fraction(0) for _ in values))
    ordered = sorted(values, reverse=True)
    running = Fraction(0)
    threshold = Fraction(0)
    for count, value in enumerate(ordered, start=1):
        running += value
        candidate = (running - total) / count
        if value - candidate > 0:
            threshold = candidate
        else:
            break
    weights = tuple(max(value - threshold, Fraction(0)) for value in values)
    return PointEstimate(total=total, weights=weights)


# ---------------------------------------------------------------------------
# How far noise can move a distribution
# ---------------------------------------------------------------------------

#: The exponent of the confidence the widening is taken at: the noise stays inside its bound
#: with probability at least ``1 - e^-5``, about 0.993, when it is Gaussian.
CONFIDENCE_EXPONENT = 5

_PLACES = 12


def _root_up(value: Fraction) -> Fraction:
    """A square root of a non-negative rational, rounded up to :data:`_PLACES` decimal places.

    Exact integer arithmetic, so the same number on every platform, and never below the root.
    """
    scale = 10 ** (2 * _PLACES)
    scaled = -((-value.numerator * scale) // value.denominator)  # the ceiling of value * scale
    root = isqrt(scaled)
    if root * root < scaled:
        root += 1
    return Fraction(root, 10**_PLACES)


def widening(cell_variance: Fraction, cells: int, total: Fraction, records: int) -> Fraction:
    """How much a noisy count table's tolerance widens: the noise's reach on a total variation.

    A reference of ``cells`` cells, each a count with independent noise of variance ``v``, whose
    noisy values add up to ``total`` (``T``, the exact estimated number of records) and whose
    point estimate is spelled as whole counts over ``records`` (``K``). The cells' shares err by
    ``eta_i / T``, and the total variation distance of the normalised estimate from the true
    distribution is at most ``|eta|_1 / T``. With ``m`` cells, ``|eta|_1 <= sqrt(m) |eta|_2``, and
    for Gaussian ``eta``, with probability at least ``1 - e^-t`` (Laurent and Massart 2000, Lemma
    1), ``|eta|_2 <= sqrt(m v) + sqrt(2 t v)``. Taking ``t = 5``, the bound is::

        w = sqrt(m) * (sqrt(m v) + sqrt(10 v)) / T  +  m / (2 K)

    The last term is the most the whole counts the estimate is spelled in (largest remainder) can
    differ from its shares. Each root is rounded up to 12 decimal places, in exact integers, and
    ``w`` is rounded up to 12 places, so it is a terminating decimal and never below the bound.
    ``total`` and ``records`` must be above zero. The bound is for the estimate before its
    projection onto the non-negative vectors, and needs the cells' noise to have a covariance at
    most ``v`` times the identity. A discrete Gaussian is sub-Gaussian with a variance proxy of
    ``scale^2`` (Canonne, Kamath and Steinke 2020), and the same tail holds for a sub-Gaussian
    vector (Hsu, Kakade and Zhang 2012), so it holds for it with ``v = scale^2``. A Laplace's tails
    are heavier, so its pass rate is the one the tests measure (SCORECARD-SPEC.md section 9).
    """
    if total <= 0 or records <= 0 or cells <= 0:
        raise ValueError("a widening needs cells, a total and a record count above zero")
    count = Fraction(cells)
    reach = _root_up(count) * (
        _root_up(count * cell_variance) + _root_up(2 * CONFIDENCE_EXPONENT * cell_variance)
    )
    return _round_up(reach / Fraction(total) + count / (2 * records))


def _round_up(value: Fraction) -> Fraction:
    """*value* rounded up to :data:`_PLACES` decimal places, in exact integers."""
    scale = 10**_PLACES
    return Fraction(-((-value.numerator * scale) // value.denominator), scale)


def sampling_widening(
    cells: int, rows: int, largest_share: Fraction, *, exact_allocation: bool = False
) -> Fraction:
    """How much a distance tolerance widens because a run generated ``rows`` rows by sampling.

    A reference of ``cells`` cells (``m``) is compared with the shares of ``rows`` rows (``n``),
    each drawn independently, so a cell's count has a variance of at most ``v = n p`` for the
    largest share ``p`` any cell has. This is the bound of :func:`widening` with the noise's
    variance replaced by ``v``, the total and the record count both ``n``::

        w = sqrt(m) * (sqrt(m v) + sqrt(10 v)) / n  +  m / (2 n)

    rounded as :func:`widening` rounds it: each root up to 12 decimal places and ``w`` up to 12
    places, so it is a terminating decimal and never below the bound. With ``exact_allocation``
    the run places whole counts by largest remainder, so the shares carry no sampling variance
    and only the rounding term ``m / (2 n)`` remains, rounded up to 12 places. ``cells`` and
    ``rows`` must be at least one and ``largest_share`` in ``(0, 1]``.
    """
    if rows < 1 or cells < 1:
        raise ValueError("a sampling widening needs at least one cell and one row")
    if not 0 < largest_share <= 1:
        raise ValueError("a sampling widening needs a largest share above zero and at most one")
    if exact_allocation:
        return _round_up(Fraction(cells, 2 * rows))
    return widening(rows * Fraction(largest_share), cells, Fraction(rows), rows)
