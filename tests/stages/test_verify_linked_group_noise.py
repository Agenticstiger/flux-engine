# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A noisy reference grouped by a link widens each group's margin by its own reach (ADR 0037 decision 7).

The world is ``test_verify_linked_group``'s: the invoices' ``i_channel`` grouped by the region of the
account each names, held by the profile as a ``groupByLink`` statistic. Here the statistic is released
under Gaussian noise, so every group's margin is the declared tolerance plus the widening of
SCORECARD-SPEC.md section 9 for that group's own cells and total. A region with fewer invoices has the
wider margin: the same edit that leaves it within leaves a larger region outside.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import pytest

from flux_stats.noise import widening as noise_widening
from flux_stats.profile import SCHEMA_URL_1_4_0, parse_profile

from .test_verify_linked_group import _evaluate, world  # noqa: F401  (the module's fixture)

pytestmark = pytest.mark.integration


def _noisy(profile: Dict[str, Any]) -> Dict[str, Any]:
    """The same profile with the invoices' link grouping released as a noisy count table."""
    document = copy.deepcopy(profile)
    document["_schemaURL"] = SCHEMA_URL_1_4_0
    document["disclosureRules"] = {"none": {}, "noisy": {}}
    document["privacy"] = {
        "measure": "zeroConcentratedDP",
        "privacyLoss": {"rho": "1000"},
        "privacyUnit": {"expose": "invoice"},
        "publicStructure": True,
    }
    (statistic,) = document["tables"]["invoice"]["columns"]["i_channel"]["statistics"]
    statistic["disclosure"] = "noisy"
    statistic["noise"] = {"mechanism": "gaussian", "scale": "1", "privacyLoss": {"rho": "0.5"}}
    for group in statistic["groups"]:
        group["noisyCounts"] = [str(count) for count in group.pop("levelCounts")]
        group.pop("k")
    return document


def _flip(held: Dict[str, Any], region: str, share: float) -> List[Dict[str, Any]]:
    """The invoices with *share* of the ones of *region* moved to the other channel."""
    in_region = {row["a_id"] for row in held["accounts"] if row["a_region"] == region}
    invoices = copy.deepcopy(held["invoices"])
    mine = [row for row in invoices if row["i_account"] in in_region]
    for row in mine[: round(len(mine) * share)]:
        row["i_channel"] = "shop" if row["i_channel"] == "web" else "web"
    return invoices


def test_the_same_edit_is_outside_an_exact_reference_and_within_a_noisy_one_in_a_small_group(
    world: Dict[str, Any],  # noqa: F811
) -> None:
    invoices = _flip(world, "west", 0.3)
    exact = _evaluate(world, invoices, world["accounts"], world["profile"])
    (exact_outcome,) = exact.outcomes
    assert {group.group: group.outcome for group in exact_outcome.groups}["west"] == "outside"
    assert all(group.widening is None for group in exact_outcome.groups)

    noisy = _evaluate(world, invoices, world["accounts"], _noisy(world["profile"]))
    assert not noisy.refusals
    (outcome,) = noisy.outcomes
    held = {group.group: group for group in outcome.groups}
    assert held["west"].outcome == "within" and held["west"].widening is not None
    assert outcome.cell_variance == 1 and outcome.noise_mechanism == "gaussian"


def test_each_groups_margin_is_the_widening_of_its_own_cells_and_total(
    world: Dict[str, Any],  # noqa: F811
) -> None:
    noisy = _evaluate(world, world["invoices"], world["accounts"], _noisy(world["profile"]))
    (outcome,) = noisy.outcomes
    held = {group.group: group for group in outcome.groups}
    profile = parse_profile(_noisy(world["profile"]))
    (statistic,) = profile.tables["invoice"].columns["i_channel"].statistics
    for group in statistic.groups:
        assert group.statistic is not None
        total = sum(group.noisy)
        assert held[group.key].widening == noise_widening(
            statistic.cell_variance, 2, total, group.statistic.k
        )
    assert held["west"].widening > held["north"].widening


def test_the_same_edit_is_outside_a_noisy_reference_in_a_larger_group(
    world: Dict[str, Any],  # noqa: F811
) -> None:
    invoices = _flip(world, "north", 0.3)
    noisy = _evaluate(world, invoices, world["accounts"], _noisy(world["profile"]))
    (outcome,) = noisy.outcomes
    held = {group.group: group.outcome for group in outcome.groups}
    assert held["north"] == "outside" and held["west"] == "within"
