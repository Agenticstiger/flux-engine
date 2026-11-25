# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim calibrate --fit`` reads the noisy rates of a private release (profile format 1-5-0).

A noisy query rate is read through its point estimate. One whose unit's records do not stand clear
of the noise (:func:`flux_stats.noise.noise_floor` over its two cells) states no rate and is left
out, with a note; a release whose rates are all inside the noise gives ``FLX-3707`` naming them.
Each query's squared gaps weigh the inverse of its rate's variance, scaled so the least noisy
weighs 1, as an exact rate does.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.calibrate import fit as fit_module
from flux_engine.calibrate.fit import FitRequest, fit_ceilings
from flux_engine.calibrate.objective import inside_noise, query_weights, score
from flux_engine.concordance.queries import QueryRow
from flux_engine.consumers.outcome import profile_targets
from flux_engine.core.errors import FluxError
from flux_stats.noise import noise_floor
from flux_stats.profile import SCHEMA_URL_1_5_0, parse_profile
from tests.calibrate._fit_bundle import copy_fit_bundle

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPO_ROOT / "tests" / "fixtures" / "consumers" / "subscriptions.profile.json"


def _released(counts: Dict[str, Tuple[str, str]], scale: str = "10") -> Dict[str, Any]:
    """The fixture's profile released under privacy, each query's rate noisy with *counts*."""
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    profile["_schemaURL"] = SCHEMA_URL_1_5_0
    profile["disclosureRules"] = {**profile["disclosureRules"], "noisy": {}}
    profile["privacy"] = {
        "measure": "zeroConcentratedDP",
        "privacyLoss": {"rho": "2"},
        "privacyUnit": {"expose": "subscriptions"},
    }
    for query in profile["queries"]:
        (statistic,) = query["statistics"]
        noisy = copy.deepcopy(statistic)
        noisy["disclosure"] = "noisy"
        noisy["noise"] = {"mechanism": "gaussian", "scale": scale, "privacyLoss": {"rho": "0.5"}}
        noisy["groups"] = [{"group": "", "status": "F", "noisyCounts": list(counts[query["name"]])}]
        query["statistics"] = [noisy]
        query["unit"] = {"table": "subscriptions"}
    return profile


CLEAR = {"premium-renewed": ("1020", "180"), "renewed-offered": ("310", "590")}


def _rows(gaps: List[Tuple[str, Fraction]]) -> List[QueryRow]:
    return [
        QueryRow(
            name=name,
            set="calibration",
            digest="sha256:" + "0" * 64,
            rows=1,
            unit_rows=2,
            rate=Fraction(1, 2),
            target=Fraction(1, 2) + gap,
            gap=gap,
            tolerance=None,
            status="measured",
            met=None,
        )
        for name, gap in gaps
    ]


def test_a_noisy_rate_is_read_with_its_variance() -> None:
    targets = profile_targets(parse_profile(_released(CLEAR)))
    ((premium,),) = [targets["premium-renewed"]]
    target = premium.groups[""]
    assert target.rate == Fraction(180, 1200)
    # The delta method over the two cells: v (f^2 + t^2) / n^4, v = 100.
    assert target.variance == Fraction(100 * (1020**2 + 180**2), 1200**4)


def test_each_query_weighs_the_inverse_of_its_rate_s_variance() -> None:
    targets = profile_targets(parse_profile(_released(CLEAR)))
    weights = query_weights(targets)
    variances = {
        name: next(iter(statistics[0].groups.values())).variance
        for name, statistics in targets.items()
    }
    least = min(variances, key=lambda name: variances[name])
    assert weights[least] == 1
    other = next(name for name in weights if name != least)
    assert weights[other] == variances[least] / variances[other]
    # The squared gaps add as weighted.
    gap = Fraction(1, 10)
    evaluation = score(_rows([(least, gap), (other, gap)]), frozenset(weights), weights)
    assert evaluation.objective == gap * gap * (1 + weights[other])


def test_an_exact_rate_weighs_one() -> None:
    targets = profile_targets(parse_profile(json.loads(PROFILE.read_text(encoding="utf-8"))))
    assert query_weights(targets) == {}
    gap = Fraction(1, 5)
    names = list(targets)
    assert score(_rows([(names[0], gap)]), frozenset(names)).objective == gap * gap


def test_a_rate_inside_the_noise_states_none() -> None:
    floor = noise_floor("gaussian", Fraction(10), 2)
    assert 5 < floor
    counts = {"premium-renewed": ("3", "2"), "renewed-offered": ("310", "590")}
    targets = profile_targets(parse_profile(_released(counts)))
    premium = targets["premium-renewed"][0].groups[""]
    assert premium.rate is None and premium.inside_noise
    assert inside_noise(targets) == frozenset({"premium-renewed"})
    assert "premium-renewed" not in query_weights(targets)


def _prepare(tmp_path: Path, counts: Dict[str, Tuple[str, str]]) -> Any:
    profile = tmp_path / "released.profile.json"
    profile.write_text(json.dumps(_released(counts)), encoding="utf-8")
    bundle = copy_fit_bundle(tmp_path / "bundle-root")
    request = FitRequest(
        bundle=bundle, profile=profile, out=tmp_path / "calibrated", state_dir=tmp_path / "state"
    )
    return fit_module._prepare(request, fit_ceilings(None, None, None))


def test_the_fit_leaves_out_a_rate_inside_the_noise_and_says_so(tmp_path: Path) -> None:
    counts = {"premium-renewed": ("1020", "180"), "renewed-offered": ("3", "2")}
    prepared = _prepare(tmp_path, counts)
    assert prepared.inside == ("renewed-offered",)
    assert prepared.stated == frozenset({"premium-renewed"})
    assert prepared.weights == {"premium-renewed": 1}


def test_a_release_whose_rates_are_all_inside_the_noise_has_nothing_to_fit(tmp_path: Path) -> None:
    counts = {"premium-renewed": ("3", "2"), "renewed-offered": ("1", "4")}
    with pytest.raises(FluxError) as refused:
        _prepare(tmp_path, counts)
    message = str(refused.value)
    assert refused.value.code.code == "FLX-3707"
    assert "outside its noise" in message
    assert "'premium-renewed'" in message and "'renewed-offered'" in message
