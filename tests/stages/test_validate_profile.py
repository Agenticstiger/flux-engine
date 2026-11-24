# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``validate --profile`` refuses what ``plan`` refuses about the profile a Simulation pins.

The same bundle goes through both stages with the same profile, and they have to agree: refused
by both with the same code and naming the same place, or accepted by both. The bundle is the
orders fixture ``fluxsim init`` writes; its profile is then taken apart (a table gone, a column
gone) and the pin is moved to the changed profile, so the only difference is what the profile
describes. Without ``--profile`` nothing is read, and ``validate`` says what it always said.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

import pytest

from flux_engine.seam.profile import load_profile
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from tests.overlays import ORDERS_PROFILE, init_orders

pytestmark = pytest.mark.integration


def _drop_table(document: Dict[str, Any]) -> None:
    del document["tables"]["orders"]


def _drop_column(document: Dict[str, Any]) -> None:
    del document["tables"]["orders"]["columns"]["country"]


def _keep(document: Dict[str, Any]) -> None:
    del document


def _bundle_pinning(root: Path, edit: Callable[[Dict[str, Any]], None]) -> Tuple[Path, Path]:
    """The orders bundle, pinned to the orders profile after *edit*, and that profile's path."""
    original = init_orders(root / "original")
    document = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    edited = copy.deepcopy(document)
    edit(edited)
    profile = root / "profile.json"
    profile.write_text(json.dumps(edited), encoding="utf-8")
    bundle = root / "bundle"
    shutil.copytree(original, bundle)
    simulation = bundle / "simulation.flux.yml"
    old = load_profile(ORDERS_PROFILE).digest.value
    new = load_profile(profile).digest.value
    text = simulation.read_text(encoding="utf-8")
    assert text.count(old) >= 1
    simulation.write_text(text.replace(old, new), encoding="utf-8")
    return bundle, profile


@pytest.mark.parametrize(
    ("edit", "refused", "names"),
    [
        (_drop_table, True, "/tables"),
        (_drop_column, True, "/tables/orders/columns"),
        (_keep, False, ""),
    ],
    ids=["missing-table", "missing-column", "complete-profile"],
)
def test_validate_and_plan_agree_on_a_profile(
    tmp_path: Path, edit: Callable[[Dict[str, Any]], None], refused: bool, names: str
) -> None:
    bundle, profile = _bundle_pinning(tmp_path, edit)
    planned = plan(bundle, state_dir=tmp_path / "state", profile=profile)
    validated = validate(bundle, profile=profile)
    assert planned.exit_code == validated.exit_code, (planned.stderr, validated.stderr)
    assert planned.error_code == validated.error_code
    if refused:
        assert validated.error_code == "FLX-3113"
        assert names in validated.stderr
    else:
        assert validated.exit_code == 0, validated.stderr


@pytest.mark.parametrize(
    "edit", [_drop_table, _drop_column], ids=["missing-table", "missing-column"]
)
def test_validate_reads_no_profile_unless_it_is_given_one(
    tmp_path: Path, edit: Callable[[Dict[str, Any]], None]
) -> None:
    bundle, _ = _bundle_pinning(tmp_path, edit)
    assert validate(bundle).exit_code == 0


def test_validate_refuses_a_profile_that_is_not_the_pinned_one_as_plan_does(tmp_path: Path) -> None:
    bundle, _ = _bundle_pinning(tmp_path, _keep)
    other = tmp_path / "other.json"
    document = json.loads(ORDERS_PROFILE.read_text(encoding="utf-8"))
    del document["tables"]["orders"]["columns"]["country"]
    other.write_text(json.dumps(document), encoding="utf-8")
    planned = plan(bundle, state_dir=tmp_path / "state", profile=other)
    validated = validate(bundle, profile=other)
    assert (validated.exit_code, validated.error_code) == (planned.exit_code, planned.error_code)
    assert validated.error_code == "FLX-3110"
