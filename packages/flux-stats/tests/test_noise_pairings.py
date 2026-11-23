# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Every pairing of a noise mechanism and a measure states a share at least what it spends (ADR 0019).

A Gaussian under approximate DP and a Laplace under zero-concentrated DP were accepted with any
stated share, so a profile could grade as differentially private with a loss far below the true
one. The reader now checks each pairing in exact arithmetic.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal, getcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from flux_stats.errors import FluxError
from flux_stats.noise import PrivacyLoss, _epsilon_at_delta, _require_spent
from flux_stats.profile import parse_profile

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


EXAMPLE: Dict[str, Any] = json.loads(
    (_spec() / "profile-1-2-private-example.json").read_text("utf-8")
)
PARTS = 3  # the example's two cliques and its one noisy histogram


def _refused(mechanism: str, scale: str, loss: PrivacyLoss, **sensitivity: Fraction) -> str:
    with pytest.raises(FluxError) as refused:
        _require_spent(mechanism, Fraction(scale), loss, "x", **sensitivity)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


def _release(measure: str, mechanism: str, scale: str, share: Dict[str, str]) -> Dict[str, Any]:
    """The example with every noisy part of *mechanism* and *scale*, each stating *share*."""
    document = copy.deepcopy(EXAMPLE)
    total = {key: format(Decimal(value) * PARTS, "f") for key, value in share.items()}
    document["privacy"] = {
        "measure": measure,
        "privacyLoss": total,
        "privacyUnit": {"expose": "subscriber"},
        "publicStructure": True,
    }
    table = document["tables"]["subscriber"]
    noisy = [*table["cliques"], table["columns"]["region"]["statistics"][0]]
    assert len(noisy) == PARTS
    for part in noisy:
        part["noise"] = {"mechanism": mechanism, "scale": scale, "privacyLoss": dict(share)}
    return document


def _reads(document: Dict[str, Any]) -> bool:
    try:
        parse_profile(document)
    except FluxError as error:
        assert error.code.code == "FLX-5036"
        return False
    return True


# ---------------------------------------------------------------------------
# The two profiles of the defect
# ---------------------------------------------------------------------------


def test_a_gaussian_under_approximate_dp_with_a_delta_of_zero_is_refused() -> None:
    # scale 0.001 spends rho = 500000: no epsilon of 0.0001 holds, and no Gaussian has delta = 0.
    document = _release("approximateDP", "gaussian", "0.001", {"epsilon": "0.0001", "delta": "0"})
    assert not _reads(document)


def test_a_laplace_under_zero_concentrated_dp_with_a_tiny_rho_is_refused() -> None:
    # scale 0.001 is epsilon 1000, so rho = 500000, not 1e-9.
    document = _release("zeroConcentratedDP", "laplace", "0.001", {"rho": "0.000000001"})
    assert not _reads(document)


@pytest.mark.parametrize("mechanism", ["gaussian", "discreteGaussian"])
def test_a_gaussian_under_approximate_dp_must_state_a_delta_above_zero(mechanism: str) -> None:
    loss = PrivacyLoss(epsilon=Fraction(10**9), delta=Fraction(0))
    assert "delta" in _refused(mechanism, "2", loss)
    assert "delta" in _refused(
        mechanism, "2", PrivacyLoss(epsilon=Fraction(10**9), delta=Fraction(1))
    )


# ---------------------------------------------------------------------------
# The boundary of each pairing
# ---------------------------------------------------------------------------


def test_a_laplace_under_zero_concentrated_dp_states_at_least_epsilon_squared_over_two() -> None:
    # scale 3 is epsilon 1/3, so rho = 1/18 exactly.
    exact = Fraction(1, 18)
    _require_spent("laplace", Fraction(3), PrivacyLoss(rho=exact), "x")
    _require_spent("laplace", Fraction(3), PrivacyLoss(rho=exact * 2), "x")
    assert "zero-concentrated" in _refused(
        "laplace", "3", PrivacyLoss(rho=exact - Fraction(1, 10**12))
    )
    # scale 2 is a terminating decimal, 0.125, and the document reader sees the same boundary
    assert _reads(_release("zeroConcentratedDP", "laplace", "2", {"rho": "0.125"}))
    assert not _reads(_release("zeroConcentratedDP", "laplace", "2", {"rho": "0.124999"}))


def _reference(rho: Fraction, delta: Fraction) -> Decimal:
    """``rho + 2 sqrt(rho ln(1 / delta))`` at 100 digits, the value the bound must not undercut."""
    context = getcontext().copy()
    context.prec = 100
    r = Decimal(rho.numerator) / Decimal(rho.denominator)
    inverse = Decimal(delta.denominator) / Decimal(delta.numerator)
    return r + 2 * (r * inverse.ln(context)).sqrt(context)


def test_the_epsilon_a_gaussian_needs_is_the_conversion_of_bun_and_steinke() -> None:
    needed = _epsilon_at_delta(Fraction(1, 2), Fraction(1, 10**6))
    assert str(Decimal(needed.numerator) / Decimal(needed.denominator))[:14] == "5.756521769756"


@pytest.mark.parametrize(
    ("rho", "delta"),
    [
        (Fraction(1, 2), Fraction(1, 10**6)),
        (Fraction(1, 8), Fraction(1, 10**5)),
        (Fraction(1, 1000), Fraction(1, 2)),
        (Fraction(500000), Fraction(1, 10**30)),
        (Fraction(1, 10**9), Fraction(999999, 10**6)),
        (Fraction(7, 3), Fraction(1, 7)),
    ],
)
def test_the_computed_epsilon_is_an_upper_bound_and_a_close_one(
    rho: Fraction, delta: Fraction
) -> None:
    bound = _epsilon_at_delta(rho, delta)
    reference = _reference(rho, delta)
    assert Decimal(bound.numerator) / Decimal(bound.denominator) >= reference
    assert Decimal(bound.numerator) / Decimal(bound.denominator) <= reference * Decimal(
        "1.000000000001"
    )


def _gaussian_share(scale: str, delta: str) -> Tuple[Fraction, Fraction]:
    return _epsilon_at_delta(1 / (2 * Fraction(scale) ** 2), Fraction(delta)), Fraction(delta)


@pytest.mark.parametrize("mechanism", ["gaussian", "discreteGaussian"])
def test_a_gaussian_under_approximate_dp_states_the_computed_epsilon_or_more(
    mechanism: str,
) -> None:
    needed, delta = _gaussian_share("1", "0.000001")
    # the exact bound, and the bound rounded up to a few places, are accepted
    _require_spent(mechanism, Fraction(1), PrivacyLoss(epsilon=needed, delta=delta), "x")
    _require_spent(
        mechanism, Fraction(1), PrivacyLoss(epsilon=Fraction("5.756522"), delta=delta), "x"
    )
    _require_spent(mechanism, Fraction(1), PrivacyLoss(epsilon=Fraction(6), delta=delta), "x")
    # anything clearly below is refused, the rounded down figure and a far smaller one alike
    for below in ("5.756521", "5.7", "1", "0.0001"):
        message = _refused(mechanism, "1", PrivacyLoss(epsilon=Fraction(below), delta=delta))
        assert "ln(1 / delta)" in message
    # a smaller delta needs a larger epsilon, so the same epsilon under a smaller delta is refused
    assert _refused(
        mechanism, "1", PrivacyLoss(epsilon=Fraction("5.756522"), delta=Fraction(1, 10**9))
    )


def test_a_gaussian_under_approximate_dp_is_checked_in_a_profile_document() -> None:
    # scale 1, delta 1e-6: epsilon 5.756522 per part holds, 5.7565 does not
    ok = {"epsilon": "5.756522", "delta": "0.000001"}
    assert _reads(_release("approximateDP", "gaussian", "1", ok))
    assert _reads(_release("approximateDP", "discreteGaussian", "1", ok))
    low = {"epsilon": "5.7565", "delta": "0.000001"}
    assert not _reads(_release("approximateDP", "gaussian", "1", low))


# ---------------------------------------------------------------------------
# What stays accepted
# ---------------------------------------------------------------------------


def test_the_pairings_that_were_checked_still_are_and_valid_profiles_stay_accepted() -> None:
    # discrete Gaussian under zero-concentrated DP: the forge-cli pairing, rho = 1 / (2 s^2)
    assert _reads(_release("zeroConcentratedDP", "discreteGaussian", "2", {"rho": "0.125"}))
    assert not _reads(_release("zeroConcentratedDP", "discreteGaussian", "2", {"rho": "0.1249"}))
    assert _reads(_release("zeroConcentratedDP", "gaussian", "2", {"rho": "0.125"}))
    assert not _reads(_release("zeroConcentratedDP", "gaussian", "2", {"rho": "0.1249"}))
    # Laplace under approximate DP: epsilon 1 / b, with a delta of zero
    assert _reads(_release("approximateDP", "laplace", "4", {"epsilon": "0.25", "delta": "0"}))
    assert not _reads(
        _release("approximateDP", "laplace", "4", {"epsilon": "0.2499", "delta": "0"})
    )
    # the unchanged example
    assert _reads(copy.deepcopy(EXAMPLE))


# ---------------------------------------------------------------------------
# A sensitivity above one
# ---------------------------------------------------------------------------


def test_a_larger_sensitivity_raises_what_each_pairing_needs() -> None:
    two = Fraction(2)
    # Laplace, scale 4: epsilon l1 / b = 1/2 at l1 = 2, and rho = (1/2)^2 / 2 = 1/8
    _require_spent(
        "laplace", Fraction(4), PrivacyLoss(epsilon=Fraction(1, 2), delta=0), "x", l1=two
    )
    assert _refused("laplace", "4", PrivacyLoss(epsilon=Fraction(1, 4), delta=Fraction(0)), l1=two)
    _require_spent("laplace", Fraction(4), PrivacyLoss(rho=Fraction(1, 8)), "x", l1=two)
    assert _refused("laplace", "4", PrivacyLoss(rho=Fraction(1, 9)), l1=two)
    # Gaussian, scale 2: rho = l2^2 / (2 s^2) = 4 / 8 = 1/2 at l2^2 = 4
    four = Fraction(4)
    _require_spent("gaussian", Fraction(2), PrivacyLoss(rho=Fraction(1, 2)), "x", l2_squared=four)
    assert _refused("gaussian", "2", PrivacyLoss(rho=Fraction(1, 3)), l2_squared=four)
    # the same scale at sensitivity one needs an eighth, so what holds there is refused at four
    assert _refused("gaussian", "2", PrivacyLoss(rho=Fraction(1, 8)), l2_squared=four)
    delta = Fraction(1, 10**6)
    needed = _epsilon_at_delta(Fraction(1, 2), delta)
    _require_spent(
        "gaussian", Fraction(2), PrivacyLoss(epsilon=needed, delta=delta), "x", l2_squared=four
    )
    assert _refused("gaussian", "2", PrivacyLoss(epsilon=needed - 1, delta=delta), l2_squared=four)


def test_a_sensitivity_that_is_not_above_zero_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        _require_spent("laplace", Fraction(1), PrivacyLoss(rho=Fraction(1)), "x", l1=Fraction(0))
    with pytest.raises(ValueError):
        _require_spent(
            "gaussian", Fraction(1), PrivacyLoss(rho=Fraction(1)), "x", l2_squared=Fraction(-1)
        )
