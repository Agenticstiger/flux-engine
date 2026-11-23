# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An overlay's restated ``mean/1`` the column cannot reach is refused, at ``init`` and at ``plan``.

The orders profile publishes ``amount`` between 10 and 100, with quartiles at 20, 35 and 50 for
its first three quarters. The highest mean those allow puts each quarter at its upper edge:
(20 + 35 + 50 + 100) / 4 = 51.25, and the lowest at its lower edge: (10 + 20 + 35 + 50) / 4 = 28.75.
A mean of 69 is no column of that profile's, so a run drawn to it fails its own ``mean`` check.
Pinned here: the refusal names a code of the 3400 band and the bound it breaks, a mean inside the
bounds is read as before, and the two ends of a profile that bounds a mean on one side only are
not refused on the other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flux_engine.cli._init import run_init
from flux_engine.stages.plan import plan
from tests.overlays import (
    ORDERS_CONTRACT,
    ORDERS_MEAN_PROFILE,
    declare_overlay,
    init_orders,
    mean_overlay_yaml,
)

pytestmark = pytest.mark.integration

#: ``Sx`` of 600 rows on the 16-bit grid: ``mean * 600 * 65536``.
SUM_OF_69 = str(69 * 600 * 65536)
SUM_OF_5 = str(5 * 600 * 65536)
SUM_OF_48 = str(48 * 600 * 65536)


def _overlay_file(root: Path, name: str, mean_sum: str) -> str:
    path = root / f"{name}.yml"
    block = mean_overlay_yaml(name, mean_sum).removeprefix("overlay:\n")
    path.write_text(
        "\n".join(line[2:] if line.startswith("  ") else line for line in block.splitlines())
        + "\n",
        encoding="utf-8",
    )
    return str(path)


def _init(root: Path, *overlays: str) -> Any:
    return run_init(
        str(root / "bundles"),
        contract=str(ORDERS_CONTRACT),
        profile=str(ORDERS_MEAN_PROFILE),
        overlays=overlays,
    )


def _plan(bundle: Path, state: Path) -> Any:
    return plan(bundle, state_dir=state, profile=ORDERS_MEAN_PROFILE)


@pytest.mark.parametrize(
    ("name", "mean_sum", "relation", "bound"),
    [
        ("mean-69", SUM_OF_69, "above the highest", "51.25"),
        ("mean-5", SUM_OF_5, "below the lowest", "28.75"),
    ],
)
def test_init_refuses_a_restated_mean_the_column_cannot_reach(
    tmp_path: Path, name: str, mean_sum: str, relation: str, bound: str
) -> None:
    result = _init(tmp_path, _overlay_file(tmp_path, name, mean_sum))
    assert result.exit_code == 1
    assert "FLX-3402" in result.stderr
    assert relation in result.stderr and bound in result.stderr
    assert f"overlay '{name}' restates the mean of orders.amount" in result.stderr
    assert not (tmp_path / "bundles").exists()


@pytest.mark.parametrize(
    ("name", "mean_sum", "relation", "bound"),
    [
        ("mean-69", SUM_OF_69, "above the highest", "51.25"),
        ("mean-5", SUM_OF_5, "below the lowest", "28.75"),
    ],
)
def test_plan_refuses_the_same_mean_before_anything_is_written(
    tmp_path: Path, name: str, mean_sum: str, relation: str, bound: str
) -> None:
    bundle = declare_overlay(
        init_orders(tmp_path / "bundle", ORDERS_MEAN_PROFILE), mean_overlay_yaml(name, mean_sum)
    )
    planned = _plan(bundle, tmp_path / "state")
    assert (planned.exit_code, planned.error_code) == (1, "FLX-3402")
    assert relation in planned.stderr and bound in planned.stderr
    assert not list((tmp_path / "state").glob("runs/*/plan.json"))


def test_a_restated_mean_inside_the_bounds_is_read_as_before(tmp_path: Path) -> None:
    result = _init(tmp_path, _overlay_file(tmp_path, "mean-48", SUM_OF_48))
    assert result.exit_code == 0, result.stderr
    bundle = tmp_path / "bundles" / "mean-48"
    planned = _plan(bundle, tmp_path / "state")
    assert planned.exit_code == 0, planned.stderr


def test_an_overlay_that_does_not_patch_a_column_is_never_refused_for_its_mean(
    tmp_path: Path,
) -> None:
    path = tmp_path / "double.yml"
    path.write_text(
        "name: double-orders\npatch:\n  tables:\n    orders:\n      rowCount: 1200\n",
        encoding="utf-8",
    )
    assert _init(tmp_path, str(path)).exit_code == 0
