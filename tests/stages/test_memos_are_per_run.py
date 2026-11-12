# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A run keeps none of its declarations in the draws' memos once it is over.

The memos hold values worked out from a bundle's declarations (level sets,
grids, bound probabilities). A long-lived process that runs many bundles
must not keep one bundle's declarations after its run, whether the run
succeeded or was refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flux_engine.core import rng
from flux_engine.core.errors import FluxError
from flux_engine.generator import rows
from flux_engine.stages import pipeline

_MEMOS = (
    rng._probability_threshold,
    rng._categorical_table,
    rng._bound_cdf_of,
    rows._seam_bytes,
    rows._grid_of,
    rows._inner_bin_top,
    rows._declared_levels,
    rows._declared_support,
    rows._bin_levels,
)


def test_a_run_clears_the_memos_it_filled(bundle_dir: Path) -> None:
    result = pipeline.execute(bundle_dir)
    assert result.emits
    assert [memo.cache_info().currsize for memo in _MEMOS] == [0] * len(_MEMOS)


def test_a_refused_run_clears_them_too(tmp_path: Path) -> None:
    rng.categorical(rng.DrawStream.from_entropy(1), {"a": rng._ONE})
    with pytest.raises(FluxError):
        pipeline.execute(tmp_path / "no-bundle-here")
    assert rng._categorical_table.cache_info().currsize == 0


def test_a_second_run_is_the_first_run(bundle_dir: Path) -> None:
    first = pipeline.execute(bundle_dir)
    second = pipeline.execute(bundle_dir)
    assert [e.result for e in second.emits] == [e.result for e in first.emits]
