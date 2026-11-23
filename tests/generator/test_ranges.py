# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A drawn value stays inside its declared range and inside the contract's integer width."""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SEED_TREE_VERSION, derive_root, digest_bytes
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_2,
    build_plan,
    declared_range_findings,
    generate_rows,
)

from .conftest import SIM_ID, H, expose_from_schema, fidelity

BOTH_KERNELS = [GENERATOR_VERSION, GENERATOR_VERSION_2]


def _plan(
    declared: Mapping[str, Any],
    *,
    column_type: str = "INT",
    version: str = GENERATOR_VERSION,
    rows: int = 4000,
) -> Any:
    return build_plan(
        expose_from_schema([{"name": "n", "type": column_type, "required": True}]),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=rows,
        fidelity=fidelity({"n": declared}),
        generator_version=version,
    )


def _drawn(plan: Any) -> List[int]:
    root = derive_root(
        digest_bytes(H),
        42,
        seed_tree_version=(
            kernel2.SEED_TREE_VERSION
            if plan.generator_version == GENERATOR_VERSION_2
            else SEED_TREE_VERSION
        ),
    )
    return [int(row[0]) for row in generate_rows(plan, root=root)]


@pytest.mark.parametrize("version", BOTH_KERNELS)
def test_bins_inside_the_range_never_draw_past_it(version: str) -> None:
    plan = _plan(
        {
            "range": {"min": "1", "max": "10"},
            "bins": ["1", "5", "10"],
            "binWeights": ["0.5", "0.5"],
        },
        version=version,
    )
    values = _drawn(plan)
    assert min(values) >= 1 and max(values) <= 10
    assert 10 in values  # the last bin is closed at its top


@pytest.mark.parametrize("version", BOTH_KERNELS)
def test_a_last_bin_past_the_range_is_refused(version: str) -> None:
    with pytest.raises(FluxError, match="leaves the declared range"):
        _plan(
            {
                "range": {"min": "1", "max": "10"},
                "bins": ["1", "5", "11"],
                "binWeights": ["0.5", "0.5"],
            },
            version=version,
        )


def test_a_first_bin_below_the_range_is_refused() -> None:
    with pytest.raises(FluxError, match="leaves the declared range"):
        _plan(
            {"range": {"min": "2", "max": "10"}, "bins": ["1", "5", "10"], "binWeights": ["1", "1"]}
        )


@pytest.mark.parametrize(
    ("column_type", "high"),
    [("SMALLINT", 32767), ("INTEGER", 2147483647), ("BIGINT", 9223372036854775807)],
)
def test_an_integer_range_may_reach_the_top_of_its_width_and_no_further(
    column_type: str, high: int
) -> None:
    plan = _plan({"range": {"min": str(high - 5), "max": str(high)}}, column_type=column_type)
    assert max(_drawn(plan)) <= high
    with pytest.raises(FluxError, match="is outside"):
        _plan({"range": {"min": "0", "max": str(high + 1)}}, column_type=column_type)


def test_a_bare_int_names_no_width() -> None:
    plan = _plan({"range": {"min": "0", "max": "8964128893"}}, column_type="INT")
    assert max(_drawn(plan)) <= 8964128893


def test_a_count_never_leaves_its_width() -> None:
    plan = _plan({"family": "rate", "mean": "500000"}, column_type="SMALLINT", rows=200)
    assert max(_drawn(plan)) <= 32767


def _findings(declared: Mapping[str, Any], bounds: Optional[Mapping[str, str]]) -> Any:
    expose = expose_from_schema([{"name": "d", "type": "DATE", "required": True}])
    return declared_range_findings(expose, fidelity({"d": declared}), bounds)


def test_a_date_range_outside_the_time_bounds_is_a_warning() -> None:
    window = {"start": "2026-01-01T00:00:00Z", "end": "2026-12-31T23:59:59Z"}
    outside = {"range": {"min": "2025-06-01", "max": "2026-06-01"}}
    inside = {"range": {"min": "2026-02-01", "max": "2026-06-01"}}
    assert len(_findings(outside, window)) == 1
    assert _findings(inside, window) == ()
    assert _findings(outside, None) == ()


def test_a_date_bin_outside_the_time_bounds_is_a_warning() -> None:
    window = {"start": "2026-01-01T00:00:00Z", "end": "2026-12-31T23:59:59Z"}
    declared = {"bins": ["2026-02-01", "2026-06-01", "2027-03-01"], "binWeights": ["1", "1"]}
    assert len(_findings(declared, window)) == 1


def test_decimal_bins_are_held_to_the_range_too() -> None:
    expose = expose_from_schema([{"name": "n", "type": "DECIMAL(7,2)", "required": True}])
    with pytest.raises(FluxError, match="leaves the declared range"):
        declared_range_findings(
            expose,
            fidelity(
                {
                    "n": {
                        "range": {"min": "0", "max": "1"},
                        "bins": ["0", "1.01"],
                        "binWeights": ["1"],
                    }
                }
            ),
            None,
        )
