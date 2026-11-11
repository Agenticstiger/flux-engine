# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The kernel sampler cases: what is drawn, against which reference, at which size.

One row per (sampler, parameter set). Each row draws through
:func:`tests.accuracy.harness.draws.draw_many` -- one path-derived stream per
value, the way a run draws -- and names the reference distribution the draws
must fit. The parameter sets cover each sampler's branches, not only its
common case: ``truncated_normal``'s mirrored branch (an interval wholly above
the mean) and its far tails, ``exponential`` at a scale of zero digits,
``poisson`` small and large, ``bounded_uint`` at a bound where an undebiased
multiply-shift would be visibly wrong.

Sizes are per tier. The nightly sizes are the ones the power claim in
``test_kernel_power.py`` is made at; the pull-request sizes are smaller
wherever the sampler is slow (``truncated_normal`` evaluates Φ and Φ⁻¹ in
50-digit decimal arithmetic, about 0.2 ms a draw).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Optional, Tuple

from flux_engine.core.rng import (
    DrawStream,
    bernoulli,
    bounded_uint,
    categorical,
    exponential,
    permutation,
    poisson,
    poisson_cap,
    truncated_normal,
    uniform53,
)

__all__ = ["CONTINUOUS", "DISCRETE", "ContinuousCase", "DiscreteCase", "by_name"]

D = Decimal


@dataclass(frozen=True)
class ContinuousCase:
    """A sampler whose output is continuous at the resolution the tests see."""

    name: str
    draw: Callable[[DrawStream], Any]
    #: The fully specified reference: ``("truncnorm", mean, sd, lo, hi)``,
    #: ``("expon", rate)`` or ``("uniform",)``.
    reference: Tuple[Any, ...]
    n_pr: int
    n_nightly: int
    #: The family scipy's composite Anderson-Darling test applies to, if any.
    anderson: Optional[str] = None
    #: The same sampler with its mean moved by 2%, for the power check.
    shifted: Optional[Callable[[DrawStream], Any]] = None


@dataclass(frozen=True)
class DiscreteCase:
    """A sampler with a finite (or capped) support, tested by counting."""

    name: str
    draw: Callable[[DrawStream], Any]
    #: ``("grid-truncnorm", mean, sd, lo, hi, step)``, ``("poisson", mean)``,
    #: ``("bernoulli", p)``, ``("categorical", {level: p})``,
    #: ``("uniform-int", n)``, ``("residue", n, modulus)`` or
    #: ``("permutation", n)``.
    reference: Tuple[Any, ...]
    n_pr: int
    n_nightly: int
    shifted: Optional[Callable[[DrawStream], Any]] = None


def _tn(mean: str, sd: str, lo: str, hi: str, scale: int) -> Callable[[DrawStream], Any]:
    m, s, a, b = D(mean), D(sd), D(lo), D(hi)

    def draw(stream: DrawStream) -> Any:
        return truncated_normal(stream, mean=m, stddev=s, lo=a, hi=b, scale=scale)

    return draw


def _exp(rate: Decimal, scale: int) -> Callable[[DrawStream], Any]:
    def draw(stream: DrawStream) -> Any:
        return exponential(stream, rate=rate, scale=scale)

    return draw


def _poisson(mean: str) -> Callable[[DrawStream], Any]:
    value = D(mean)

    def draw(stream: DrawStream) -> Any:
        return poisson(stream, mean=value)

    return draw


def _bernoulli(p: str) -> Callable[[DrawStream], Any]:
    value = D(p)

    def draw(stream: DrawStream) -> Any:
        return bernoulli(stream, value)

    return draw


#: Levels written out of NFC order on purpose: the sampler must sort them.
_THREE_LEVELS = {"retail": D("0.5"), "enterprise": D("0.2"), "smb": D("0.3")}
_TEN_LEVELS = {f"l{index:02d}": D("0.111") for index in range(9)} | {"l09": D("0.001")}

#: ``3 * 2**61``: Lemire's threshold is ``2**64 mod n = 2**62``, a quarter of
#: the words, so an undebiased multiply-shift would put three preimages on
#: some results and two on others -- a 3:2 imbalance in ``k mod 3`` that the
#: residue test below sees at any size.
_HUGE_BOUND = 3 * (1 << 61)

CONTINUOUS: Tuple[ContinuousCase, ...] = (
    ContinuousCase(
        name="uniform53",
        draw=lambda stream: uniform53(stream) / float(1 << 53),
        reference=("uniform",),
        n_pr=50_000,
        n_nightly=250_000,
    ),
    ContinuousCase(
        name="truncated_normal-wide",
        draw=_tn("0", "1", "-8", "8", 6),
        reference=("truncnorm", 0.0, 1.0, -8.0, 8.0),
        n_pr=10_000,
        n_nightly=50_000,
        anderson="norm",
    ),
    ContinuousCase(
        name="truncated_normal-two-sided",
        draw=_tn("50", "10", "30", "90", 6),
        reference=("truncnorm", 50.0, 10.0, 30.0, 90.0),
        n_pr=10_000,
        n_nightly=50_000,
        shifted=_tn("51", "10", "30", "90", 6),
    ),
    ContinuousCase(
        name="truncated_normal-above-mean",
        draw=_tn("0", "1", "0.5", "3", 6),
        reference=("truncnorm", 0.0, 1.0, 0.5, 3.0),
        n_pr=10_000,
        n_nightly=50_000,
    ),
    ContinuousCase(
        name="truncated_normal-upper-tail",
        draw=_tn("0", "1", "4", "7", 8),
        reference=("truncnorm", 0.0, 1.0, 4.0, 7.0),
        n_pr=5_000,
        n_nightly=50_000,
    ),
    ContinuousCase(
        name="truncated_normal-lower-tail",
        draw=_tn("0", "1", "-7", "-4", 8),
        reference=("truncnorm", 0.0, 1.0, -7.0, -4.0),
        n_pr=5_000,
        n_nightly=50_000,
    ),
    ContinuousCase(
        name="exponential-unit",
        draw=_exp(D(1), 6),
        reference=("expon", 1.0),
        n_pr=20_000,
        n_nightly=250_000,
        anderson="expon",
        shifted=_exp(D(1) / D("1.02"), 6),
    ),
    ContinuousCase(
        name="exponential-gap-ms",
        draw=_exp(D(1) / D(300_000), 0),
        reference=("expon", 1.0 / 300_000),
        n_pr=20_000,
        n_nightly=100_000,
    ),
)

DISCRETE: Tuple[DiscreteCase, ...] = (
    DiscreteCase(
        name="truncated_normal-integer",
        draw=_tn("10", "3", "0", "20", 0),
        reference=("grid-truncnorm", 10.0, 3.0, 0.0, 20.0, 1.0),
        n_pr=10_000,
        n_nightly=50_000,
    ),
    DiscreteCase(
        name="truncated_normal-one-digit",
        draw=_tn("100", "1", "95", "105", 1),
        reference=("grid-truncnorm", 100.0, 1.0, 95.0, 105.0, 0.1),
        n_pr=10_000,
        n_nightly=50_000,
    ),
    DiscreteCase(
        name="poisson-0.5",
        draw=_poisson("0.5"),
        reference=("poisson", 0.5),
        n_pr=20_000,
        n_nightly=100_000,
    ),
    DiscreteCase(
        name="poisson-1.5",
        draw=_poisson("1.5"),
        reference=("poisson", 1.5),
        n_pr=20_000,
        n_nightly=100_000,
    ),
    DiscreteCase(
        name="poisson-20",
        draw=_poisson("20"),
        reference=("poisson", 20.0),
        n_pr=20_000,
        n_nightly=100_000,
        shifted=_poisson("20.4"),
    ),
    DiscreteCase(
        name="poisson-250",
        draw=_poisson("250"),
        reference=("poisson", 250.0),
        n_pr=10_000,
        n_nightly=50_000,
    ),
    DiscreteCase(
        name="bernoulli-0.001",
        draw=_bernoulli("0.001"),
        reference=("bernoulli", 0.001),
        n_pr=50_000,
        n_nightly=250_000,
    ),
    DiscreteCase(
        name="bernoulli-0.35",
        draw=_bernoulli("0.35"),
        reference=("bernoulli", 0.35),
        n_pr=50_000,
        n_nightly=250_000,
        shifted=_bernoulli("0.357"),
    ),
    DiscreteCase(
        name="bernoulli-0.5",
        draw=_bernoulli("0.5"),
        reference=("bernoulli", 0.5),
        n_pr=50_000,
        n_nightly=250_000,
    ),
    DiscreteCase(
        name="categorical-three",
        draw=lambda stream: categorical(stream, _THREE_LEVELS),
        reference=("categorical", {key: float(value) for key, value in _THREE_LEVELS.items()}),
        n_pr=20_000,
        n_nightly=100_000,
    ),
    DiscreteCase(
        name="categorical-ten-with-a-rare-level",
        draw=lambda stream: categorical(stream, _TEN_LEVELS),
        reference=("categorical", {key: float(value) for key, value in _TEN_LEVELS.items()}),
        n_pr=20_000,
        n_nightly=100_000,
    ),
    DiscreteCase(
        name="bounded_uint-2",
        draw=lambda stream: bounded_uint(stream, 2),
        reference=("uniform-int", 2),
        n_pr=20_000,
        n_nightly=100_000,
    ),
    DiscreteCase(
        name="bounded_uint-7",
        draw=lambda stream: bounded_uint(stream, 7),
        reference=("uniform-int", 7),
        n_pr=20_000,
        n_nightly=100_000,
    ),
    DiscreteCase(
        name="bounded_uint-1000",
        draw=lambda stream: bounded_uint(stream, 1000),
        reference=("uniform-int", 1000),
        n_pr=20_000,
        n_nightly=100_000,
    ),
    DiscreteCase(
        name="bounded_uint-3x2^61-residue",
        draw=lambda stream: bounded_uint(stream, _HUGE_BOUND),
        reference=("residue", _HUGE_BOUND, 3),
        n_pr=20_000,
        n_nightly=100_000,
    ),
    DiscreteCase(
        name="permutation-4",
        draw=lambda stream: permutation(stream, 4),
        reference=("permutation", 4),
        n_pr=20_000,
        n_nightly=100_000,
    ),
    DiscreteCase(
        name="permutation-6",
        draw=lambda stream: permutation(stream, 6),
        reference=("permutation", 6),
        n_pr=10_000,
        n_nightly=50_000,
    ),
)

#: Bound checks of the case table itself, so a typo cannot hide a sampler.
assert len({case.name for case in CONTINUOUS + DISCRETE}) == len(CONTINUOUS) + len(DISCRETE)
assert poisson_cap(D(250)) > 250


def by_name(name: str) -> Any:
    """The case called *name*, continuous or discrete."""
    for case in CONTINUOUS + DISCRETE:
        if case.name == name:
            return case
    raise KeyError(name)
