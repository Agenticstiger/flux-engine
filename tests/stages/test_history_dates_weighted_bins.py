# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's dates, and the validate warning, read the edges the draw can reach (ADR 0017 section 11).

A column that declares bins is drawn from the bins that carry weight, so a declared range that is
wider than that draw, such as the full year grid a noisy profile gives it, places no date outside
``spec.timeBounds``. What is pinned:

* **A history's emit** is accepted when the weighted bins lie inside the window, whatever range
  the column declares, and every date it writes lies inside the window; a bin outside the window
  that carries weight is still refused (``FLX-3403``) naming the column.
* **The warning** of ``fluxsim validate`` for another emit reads the same edges.
* **Replay.** A plan the change accepts replays to its recorded digest, and a bundle both
  versions accept keeps the run digest it had.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.digest import digest_of_document
from flux_engine.generator.statistics import ColumnStatistics, drawn_edges
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.replay import replay
from flux_engine.stages.validate import validate
from tests.seam.test_history import bundle_with

from ._fleet import plan_refused, rows, run_bundle
from .test_history_run import EMIT, _bundle

pytestmark = pytest.mark.integration

WINDOW_LOW, WINDOW_HIGH = "2023-01-01", "2026-01-01"
GRID = {"min": "0001-01-01", "max": "9999-12-31"}
WARNING_TEXT = "the declared range or bins leave the run's timeBounds"


def _column(weights: List[str]) -> Dict[str, Any]:
    """``started_on`` over the full date grid, with a bin each side of the window."""
    return {
        "range": dict(GRID),
        "bins": ["0001-01-01", WINDOW_LOW, "2025-01-01", WINDOW_HIGH, "9999-12-31"],
        "binWeights": weights,
    }


INSIDE = ["0", "0.4", "0.6", "0"]


def _digest(state: Path, run_id: str) -> str:
    document = json.loads((state / "runs" / run_id / "run-digest.json").read_text("utf-8"))
    return digest_of_document(document, subject="runDigest").value


def test_a_full_grid_range_with_weight_only_inside_the_window_is_accepted(tmp_path: Path) -> None:
    target = _bundle(tmp_path, columns={"started_on": _column(INSIDE)})
    layout = run_bundle(target, tmp_path / "state")
    dates = [row["started_on"] for row in rows(layout, EMIT)]
    assert dates and all(WINDOW_LOW <= value <= WINDOW_HIGH for value in dates)


def test_weight_in_a_bin_outside_the_window_is_still_refused(tmp_path: Path) -> None:
    for name, weights in (
        ("low", ["0.1", "0.4", "0.5", "0"]),
        ("high", ["0", "0.4", "0.5", "0.1"]),
    ):
        target = _bundle(tmp_path, name=name, columns={"started_on": _column(weights)})
        refused = plan_refused(target, tmp_path / f"state-{name}")
        assert refused.error_code == "FLX-3403"
        assert "'started_on' leave" in refused.stderr


def _plain(tmp_path: Path, name: str, weights: List[str]) -> Any:
    column = {
        "range": dict(GRID),
        "bins": ["0001-01-01", "2026-07-01", "2026-09-30", "9999-12-31"],
        "binWeights": weights,
    }
    return validate(bundle_with(tmp_path, name, None, columns={"started_on": column}))


def test_an_emit_without_a_history_warns_only_of_weighted_bins_outside(tmp_path: Path) -> None:
    quiet = _plain(tmp_path, "quiet", ["0", "1", "0"])
    assert quiet.exit_code == 0, quiet.stderr
    assert WARNING_TEXT not in quiet.stdout and "warnings" not in quiet._artifacts
    for name, weights in (("low", ["0.2", "0.8", "0"]), ("high", ["0", "0.8", "0.2"])):
        loud = _plain(tmp_path, name, weights)
        assert loud.exit_code == 0, loud.stderr
        assert any(WARNING_TEXT in text for text in loud._artifacts["warnings"])


@pytest.mark.unit
def test_a_weightless_bin_between_weighted_ones_lies_inside_the_hull() -> None:
    from decimal import Decimal as D

    statistics = ColumnStatistics(
        column="d",
        range_min=D(0),
        range_max=D(100),
        bins=(D(0), D(10), D(20), D(30), D(40)),
        bin_weights=(D(0), D(1), D(0), D(1)),
    )
    assert drawn_edges(statistics) == (D(10), D(40))
    assert drawn_edges(ColumnStatistics(column="d", range_min=D(1), range_max=D(2))) == (D(1), D(2))
    assert drawn_edges(ColumnStatistics(column="d")) == ()


def test_the_accepted_plan_replays_to_its_digest(tmp_path: Path) -> None:
    target = _bundle(tmp_path, columns={"started_on": _column(INSIDE)})
    state = tmp_path / "state"
    layout: RunLayout = run_bundle(target, state)
    digest = _digest(state, layout.run_id)
    recorded = layout.emit_path(EMIT).read_bytes()
    for jobs in (1, 4):
        replayed = replay(
            target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=jobs
        )
        assert replayed.success, replayed.stderr
        assert layout.emit_path(EMIT).read_bytes() == recorded
    again = run_bundle(target, tmp_path / "again")
    assert again.run_id == layout.run_id and _digest(tmp_path / "again", again.run_id) == digest


# The run digest of the default history-chain bundle, as the engine makes it with the counts each entity's room fits
# (it was sha256:1cd3043f7030f4a28a71812a7b6344f3e63d2b046f524092e8bf0910a262e83b under the rank coupling).
BASELINE_RUN_DIGEST = "sha256:47f361cb1f7fee8d36f8afb75bf0850bb65e62d178635da2f697f4c83636c67c"


def test_a_bundle_both_versions_accept_keeps_its_run_digest(tmp_path: Path) -> None:
    state = tmp_path / "state"
    layout = run_bundle(_bundle(tmp_path), state)
    digest = _digest(state, layout.run_id)
    assert digest == BASELINE_RUN_DIGEST
    replayed = replay(
        _bundle(tmp_path, name="again"),
        run_id=layout.run_id,
        assert_digest=BASELINE_RUN_DIGEST,
        state_dir=state,
        jobs=1,
    )
    assert replayed.success, replayed.stderr
