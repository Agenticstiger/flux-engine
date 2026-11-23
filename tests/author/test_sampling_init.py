# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Init writes ``sampling: true`` on each distance tolerance (ADR 0016 sections 7 and 8).

Pinned here, through ``fluxsim init`` and a run:

* every distance tolerance in a written bundle declares ``sampling: true``, whatever property
  it belongs to, and a tolerance that is not a distance declares none;
* the bundle plans, applies and verifies against its own profile, the margins following the
  row count.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.stages.run import run

from .test_children import _mean
from .test_given import NAMESPACE, _init, _profile

pytestmark = pytest.mark.integration


def _tolerances(node: Any) -> List[Dict[str, Any]]:
    """Every ``tolerance`` mapping under *node*."""
    found: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "tolerance" and isinstance(value, dict):
                found.append(value)
            else:
                found.extend(_tolerances(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_tolerances(item))
    return found


def _written(tmp_path: Path) -> List[Dict[str, Any]]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    emits = yaml.safe_load(text)["extensions"][NAMESPACE]["emits"]
    return _tolerances(emits)


def _shop_profile() -> Dict[str, Any]:
    profile = _profile(region_tier=True, spend_by_segment=True, spend_by_region=True)
    spend = profile["tables"]["customer"]["columns"]["spend"]
    spend["statistics"].append(_mean(55000, 1000))
    return profile


def test_every_distance_tolerance_init_writes_follows_the_row_count(tmp_path: Path) -> None:
    _init(tmp_path, _shop_profile())
    tolerances = _written(tmp_path)
    distances = [found for found in tolerances if found["type"] == "distance"]
    assert len(distances) >= 5  # levels, grouped levels, bins and grouped bins
    assert all(found["sampling"] is True for found in distances)


def test_a_tolerance_that_is_not_a_distance_carries_no_sampling_key(tmp_path: Path) -> None:
    _init(tmp_path, _shop_profile())
    others = [found for found in _written(tmp_path) if found["type"] != "distance"]
    assert others  # the mean's relative tolerance
    assert all("sampling" not in found for found in others)


def test_init_plan_apply_verify_a_bundle_whose_tolerances_follow_the_row_count(
    tmp_path: Path,
) -> None:
    _init(tmp_path, _shop_profile())
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "shop.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stdout + outcome.stderr
