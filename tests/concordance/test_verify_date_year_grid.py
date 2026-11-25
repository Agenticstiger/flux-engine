# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``verify`` against a profile that holds a DATE column on a year grid, and a grouping with an empty level.

A lending library's loans: ``due_date`` is a DATE column and ``branch`` a category. Pinned, through
``init``, ``run`` and ``verify``:

* **The calendar's last instant is the day 9999-12-31.** A profiler's year grid ends at
  253402300799999 milliseconds, which a declaration spells as the whole day ``9999-12-31``. The
  histogram is compared, and one whose edge is not a whole day still matches no declared edge.
* **A level the profile holds no record of is no group.** A noisy release lists every level of a
  grouping column; a level whose noisy counts project to nothing is left out, with or without a
  generated row of it, and the others are compared. A level the profile holds records of that the
  rows lack still makes the two sides group differently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.cli._init import run_init
from flux_engine.stages.run import run

pytestmark = pytest.mark.integration

SCHEMA = "https://agenticstiger.github.io/flux/engine/spec/profile/1-4-0/FluxProfile.json"
FIRST_MS = -62_135_596_800_000  # 0001-01-01
Y2000_MS = 946_684_800_000
Y2020_MS = 1_577_836_800_000
LAST_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: library.loans
name: Library loans
metadata:
  owner:
    team: library-team
exposes:
  - exposeId: loan
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/loan.parquet }
    contract:
      schema:
        - { name: loan_id, type: INT, required: true }
        - { name: due_date, type: DATE, required: true }
        - { name: branch, type: "enum('central','harbour','hill','mill')", required: true }
        - { name: format, type: "enum('book','audio')", required: true }
    semantics:
      entities:
        - { name: loan, type: primary, expr: loan_id }
"""

BRANCHES = ["central", "harbour", "hill", "mill"]


def _date_histogram(edges: List[int]) -> Dict[str, Any]:
    return {
        "definition": "marginalHistogram/1",
        "bins": [str(edge) for edge in edges],
        "sampleSize": {"min": 100, "max": 100},
        "disclosure": "none",
        "groups": [{"group": "", "k": 100, "binCounts": [10, 40, 50], "status": "F"}],
    }


def _noisy_level_histogram(levels: List[str], counts: List[str]) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "sampleSize": {"min": 900, "max": 1100},
        "disclosure": "noisy",
        "levels": levels,
        "noise": {"mechanism": "gaussian", "scale": "2.5", "privacyLoss": {"rho": "0.1"}},
        "groups": [{"group": "", "status": "F", "noisyCounts": counts}],
    }


def _grouped_by_branch(per_branch: Dict[str, List[str]]) -> Dict[str, Any]:
    """``format`` grouped by ``branch``, each branch's two noisy counts as given."""
    statistic = _noisy_level_histogram(["audio", "book"], ["0", "0"])
    statistic["groupBy"] = "branch"
    statistic["groups"] = [
        {"group": branch, "status": "F", "noisyCounts": counts}
        for branch, counts in per_branch.items()
    ]
    return statistic


def _profile(
    edges: List[int],
    per_branch: Dict[str, List[str]],
    branches: Tuple[str, ...] = ("260", "250", "240", "250"),
) -> Dict[str, Any]:
    return {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": SCHEMA,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}, "noisy": {}},
        "privacy": {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "0.5"},
            "privacyUnit": {"expose": "loan"},
        },
        "tables": {
            "loan": {
                "columns": {
                    "loan_id": {"domain": "number"},
                    "due_date": {"domain": "timestamp", "statistics": [_date_histogram(edges)]},
                    "branch": {
                        "domain": "category",
                        "statistics": [_noisy_level_histogram(BRANCHES, list(branches))],
                    },
                    "format": {
                        "domain": "category",
                        "statistics": [
                            _noisy_level_histogram(["audio", "book"], ["300", "700"]),
                            _grouped_by_branch(per_branch),
                        ],
                    },
                }
            }
        },
    }


def _verify(tmp_path: Path, profile: Dict[str, Any]) -> Any:
    (tmp_path / "loans.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "loans.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "loans.fluid.yaml"),
        profile=str(tmp_path / "loans.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "loans.profile.json",
        verify=True,
    )


EVEN = {branch: ["75", "175"] for branch in BRANCHES}
YEAR_GRID = [FIRST_MS, Y2000_MS, Y2020_MS, LAST_MS]


def test_a_date_histogram_on_a_grid_that_ends_at_the_last_instant_is_compared(
    tmp_path: Path,
) -> None:
    outcome = _verify(tmp_path, _profile(YEAR_GRID, EVEN))
    assert "FLX-5037" not in outcome.stdout, outcome.stdout
    assert "refused" not in outcome.stdout, outcome.stdout
    assert "4 of 4 declared properties within the margin" in outcome.stdout, outcome.stdout
    assert outcome.success, outcome.stdout + outcome.stderr


def test_an_edge_that_is_not_a_whole_day_still_matches_no_declared_edge(tmp_path: Path) -> None:
    outcome = _verify(tmp_path, _profile([FIRST_MS, Y2000_MS, Y2020_MS, LAST_MS - 1], EVEN))
    assert "FLX-5037" in outcome.stdout, outcome.stdout
    assert "not with this property's bins" in outcome.stdout, outcome.stdout
    assert not outcome.success


# The branch ``mill`` has a noisy count of nothing: the generated loans hold none of it.
NO_MILL = ("260", "250", "240", "0")


def test_a_level_the_profile_holds_no_record_of_is_left_out_with_no_generated_row_of_it(
    tmp_path: Path,
) -> None:
    per_branch = dict(EVEN, mill=["0", "0"])
    outcome = _verify(tmp_path, _profile(YEAR_GRID, per_branch, NO_MILL))
    assert "FLX-5005" not in outcome.stdout, outcome.stdout
    assert "refused" not in outcome.stdout, outcome.stdout
    assert outcome.success, outcome.stdout + outcome.stderr


def test_a_level_the_profile_holds_records_of_and_the_rows_lack_is_still_refused(
    tmp_path: Path,
) -> None:
    outcome = _verify(tmp_path, _profile(YEAR_GRID, EVEN, NO_MILL))
    assert "the two sides group into different sets: 4 in the profile and 3 synthetic" in (
        outcome.stdout
    ), outcome.stdout
    assert not outcome.success
