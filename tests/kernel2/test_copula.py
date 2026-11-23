# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Gaussian copula's plan (ADR 0035 decision 5): latent correlations, shrinkage, the factor."""

from __future__ import annotations

import math
from decimal import Decimal, getcontext

import pytest

from flux_engine.core import copula

# Pinned once, computed by this module; a change is a change of the plan document's digest.
SHRUNK_DIGEST = "62fae3adcabb315f5d820da46d9465e5224a53409aab9a558469e397a14bdae3"
PASSING_DIGEST = "631f3e6cb31ec412e799ae720e37125e626adb78ad6e975aeff6579bf7207cbd"

TOLERANCE = Decimal(10) ** -49


def _equicorrelated(size: int, r: str) -> list[list[Decimal]]:
    return [[Decimal(1) if i == j else Decimal(r) for j in range(size)] for i in range(size)]


def test_latent_known_values() -> None:
    assert copula.latent("0", "kendall") == 0
    assert copula.latent("0", "spearman") == 0
    assert abs(copula.latent("1", "kendall") - 1) < TOLERANCE
    assert abs(copula.latent("-1", "kendall") + 1) < TOLERANCE
    assert abs(copula.latent("1", "spearman") - 1) < TOLERANCE
    # sin(pi / 6) = 1/2 and 2 sin(pi / 6) = 1.
    assert (
        abs(copula.latent("0.3333333333333333333333333333333333", "kendall") - Decimal("0.5"))
        < Decimal(10) ** -30
    )
    assert abs(copula.latent("0.5", "spearman") - Decimal("0.5176380902050415246977976752")) < (
        Decimal(10) ** -27
    )


def test_latent_is_odd_and_has_fifty_digits() -> None:
    value = copula.latent("0.3", "kendall")
    assert copula.latent("-0.3", "kendall") == value.copy_negate()
    assert len(value.as_tuple().digits) <= 50


def test_latent_refuses_what_is_not_a_correlation() -> None:
    with pytest.raises(ValueError):
        copula.latent("1.5", "kendall")
    with pytest.raises(ValueError):
        copula.latent("0.5", "pearson")  # type: ignore[arg-type]


def test_a_matrix_that_passes_has_alpha_zero() -> None:
    matrix = _equicorrelated(3, "0.3")
    assert copula.passes(matrix)
    assert copula.shrink(matrix) == 0
    plan = copula.plan_copula(["a", "b", "c"], "spearman", {("a", "b"): "0.5", ("b", "c"): "0.25"})
    assert plan.alpha == "0"
    assert plan.digest == PASSING_DIGEST
    assert plan.factor[0] == (1.0,)
    assert plan.factor[2][0] == 0.0  # a and c are independent given b only through b


def test_an_infeasible_matrix_is_shrunk_to_the_edge_of_passing() -> None:
    matrix = _equicorrelated(3, "-0.9")
    assert not copula.passes(matrix)
    alpha = copula.shrink(matrix)
    assert 0 < alpha < 1
    assert copula.passes(copula._shrunk(matrix, alpha))
    assert not copula.passes(copula._shrunk(matrix, alpha - Decimal(2) ** -40))


def test_a_singular_matrix_is_shrunk() -> None:
    matrix = _equicorrelated(2, "1")
    assert not copula.passes(matrix)
    alpha = copula.shrink(matrix)
    assert 0 < alpha < Decimal("0.001")
    assert copula.passes(copula._shrunk(matrix, alpha))


def test_the_factor_reproduces_the_matrix() -> None:
    plan = copula.plan_copula(
        ["a", "b", "c"], "kendall", {("a", "b"): "-0.9", ("a", "c"): "-0.9", ("b", "c"): "-0.9"}
    )
    alpha = Decimal(plan.alpha)
    rows = plan.factor
    for i in range(3):
        for j in range(i + 1):
            product = math.fsum(rows[i][k] * rows[j][k] for k in range(j + 1))
            expected = 1.0 if i == j else float((1 - alpha) * copula.latent("-0.9", "kendall"))
            assert product == pytest.approx(expected, abs=1e-12)


def test_the_plan_digest_is_pinned_and_stable() -> None:
    pairs = {("a", "b"): "-0.9", ("a", "c"): "-0.9", ("b", "c"): "-0.9"}
    first = copula.plan_copula(["a", "b", "c"], "kendall", pairs)
    second = copula.plan_copula(["a", "b", "c"], "kendall", dict(reversed(list(pairs.items()))))
    assert first == second
    assert first.digest == SHRUNK_DIGEST
    assert first.alpha == "0.4937676058507349807769060135"
    assert copula.factor_digest(first.factor) == first.digest


def test_pairs_name_either_order_and_alpha_is_canonical_text() -> None:
    a = copula.plan_copula(["x", "y"], "kendall", {("x", "y"): "0.5"})
    b = copula.plan_copula(["x", "y"], "kendall", {("y", "x"): Decimal("0.5")})
    assert a == b
    assert copula.plan_copula(["x", "y"], "kendall", {}).factor == ((1.0,), (0.0, 1.0))
    assert copula._alpha_text(Decimal("0.500")) == "0.5"
    assert copula._alpha_text(Decimal(0)) == "0"
    assert copula._alpha_text(Decimal(1)) == "1"


def test_bad_pairs_are_refused() -> None:
    with pytest.raises(ValueError):
        copula.plan_copula(["x", "y"], "kendall", {("x", "z"): "0.5"})
    with pytest.raises(ValueError):
        copula.plan_copula(["x", "y"], "kendall", {("x", "x"): "0.5"})
    with pytest.raises(ValueError):
        copula.plan_copula(["x", "y"], "kendall", {("x", "y"): "0.5", ("y", "x"): "0.5"})
    with pytest.raises(ValueError):
        copula.plan_copula(["x", "x"], "kendall", {})


def test_a_tiny_factor_entry_is_zero() -> None:
    tiny = Decimal(10) ** -300
    matrix = [[Decimal(1), tiny], [tiny, Decimal(1)]]
    assert copula.factor(matrix, Decimal(0))[1][0] == 0.0


def test_the_global_decimal_context_is_untouched() -> None:
    before = getcontext().copy()
    copula.plan_copula(["a", "b", "c"], "kendall", {("a", "b"): "-0.9", ("b", "c"): "-0.9"})
    after = getcontext()
    assert (after.prec, after.rounding, after.Emin, after.Emax) == (
        before.prec,
        before.rounding,
        before.Emin,
        before.Emax,
    )
    assert after.traps == before.traps
