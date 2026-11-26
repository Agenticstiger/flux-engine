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
  rows lack still makes the two sides group differently. A level the profile holds no record of is
  left out whether the generated rows hold none of it or a few, in the full comparison and in a
  comparison on a sample, and an exact group of no record is still refused.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from flux_engine.cli._init import run_init
from flux_engine.concordance.sampling import SampleRequest
from flux_engine.stages.run import run
from flux_engine.stages.verify import verify

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


def _dated_by_branch(per_branch: Dict[str, List[str]], edges: List[int]) -> Dict[str, Any]:
    """``due_date`` over the year grid, grouped by ``branch``, each branch's noisy counts as given."""
    return {
        "definition": "marginalHistogram/1",
        "bins": [str(edge) for edge in edges],
        "groupBy": "branch",
        "sampleSize": {"min": 900, "max": 1100},
        "disclosure": "noisy",
        "noise": {"mechanism": "gaussian", "scale": "2.5", "privacyLoss": {"rho": "0.1"}},
        "groups": [
            {"group": branch, "status": "F", "noisyCounts": counts}
            for branch, counts in per_branch.items()
        ],
    }


def _profile(
    edges: List[int],
    per_branch: Dict[str, List[str]],
    branches: Tuple[str, ...] = ("260", "250", "240", "250"),
    dated: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    dates = [_date_histogram(edges)]
    if dated is not None:
        dates.append(_dated_by_branch(dated, edges))
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
                    "due_date": {"domain": "timestamp", "statistics": dates},
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


LEVELS_BY_BRANCH = """\
          - property: levelHistogram
            column: format
            groupBy: branch
"""
DATES_BY_BRANCH = """\
          - property: marginalHistogram
            column: due_date
            groupBy: branch
            bins: ['0001-01-01', '2000-01-01', '2020-01-01', '9999-12-31']
"""
TOLERANCE = """\
            tolerance:
              type: distance
              measure: totalVariation
              max: '0.10'
              sampling: true
"""


def _bundle(
    tmp_path: Path,
    profile: Dict[str, Any],
    declare: str = "",
    minimum_records: Optional[int] = None,
) -> None:
    """Write the contract and the profile, run ``init``, then declare by hand what it did not.

    ``init`` declares no grouped property over a grouping whose group has no record, so a
    property that is grouped over one is added to the bundle's simulation, as an author would.
    """
    (tmp_path / "loans.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "loans.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "loans.fluid.yaml"),
        profile=str(tmp_path / "loans.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    simulation = tmp_path / "bundle" / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    if declare:
        anchor = "          budget:\n"
        assert text.count(anchor) == 1
        text = text.replace(anchor, declare + TOLERANCE + anchor)
    if minimum_records is not None:
        assert text.count("            min: 1\n") == 1
        text = text.replace("            min: 1\n", f"            min: {minimum_records}\n")
    simulation.write_text(text, encoding="utf-8")


def _verify(
    tmp_path: Path,
    profile: Dict[str, Any],
    declare: str = "",
    minimum_records: Optional[int] = None,
) -> Any:
    _bundle(tmp_path, profile, declare, minimum_records)
    return run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "loans.profile.json",
        verify=True,
    )


def _sampled(
    tmp_path: Path,
    profile: Dict[str, Any],
    declare: str,
    minimum_records: Optional[int] = None,
    fraction: str = "1",
) -> Any:
    """``verify --sample`` of a run that holds no verdict of its own: the blocks are 100 records."""
    _bundle(tmp_path, profile, declare, minimum_records)
    applied = run(
        tmp_path / "bundle", state_dir=tmp_path / "state", profile=tmp_path / "loans.profile.json"
    )
    assert applied.success, applied.stderr
    return verify(
        tmp_path / "bundle",
        run_id=applied.artifacts["plan"]["run_id"],
        state_dir=tmp_path / "state",
        profile=tmp_path / "loans.profile.json",
        sample=SampleRequest(fraction=Fraction(fraction), block_records=100),
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


# ``mill`` holds a noisy count of one record, so a run draws it a single row; the profile's grouped
# statistics hold no record of it.
ONE_MILL = ("260", "250", "240", "1")
NO_MILL_GROUPED = dict(EVEN, mill=["0", "0"])
NO_MILL_DATED = dict({branch: ["10", "40", "50"] for branch in BRANCHES}, mill=["0", "0", "0"])


def test_a_grouped_property_leaves_out_a_level_the_profile_holds_no_record_of_and_the_rows_lack(
    tmp_path: Path,
) -> None:
    # ``init`` declares no grouped property over such a level, so the property is declared by hand.
    profile = _profile(YEAR_GRID, NO_MILL_GROUPED, NO_MILL)
    outcome = _verify(tmp_path, profile, LEVELS_BY_BRANCH)
    assert "FLX-5005" not in outcome.stdout, outcome.stdout
    assert "4 of 4 declared properties within the margin" in outcome.stdout, outcome.stdout
    assert outcome.success, outcome.stdout + outcome.stderr


def test_a_grouped_property_leaves_out_a_level_the_profile_holds_no_record_of_and_the_rows_hold_one(
    tmp_path: Path,
) -> None:
    profile = _profile(YEAR_GRID, NO_MILL_GROUPED, ONE_MILL)
    outcome = _verify(tmp_path, profile, LEVELS_BY_BRANCH)
    assert "FLX-5005" not in outcome.stdout, outcome.stdout
    assert "4 of 4 declared properties within the margin" in outcome.stdout, outcome.stdout
    assert outcome.success, outcome.stdout + outcome.stderr


def test_the_rows_of_a_level_the_profile_holds_no_record_of_are_still_held_to_the_floor(
    tmp_path: Path,
) -> None:
    profile = _profile(YEAR_GRID, NO_MILL_GROUPED, ONE_MILL)
    outcome = _verify(tmp_path, profile, LEVELS_BY_BRANCH, minimum_records=5)
    assert "FLX-5005" in outcome.stdout, outcome.stdout
    assert "1 records, minimum is 5" in outcome.stdout, outcome.stdout
    assert not outcome.success


def test_an_exact_group_of_no_record_is_still_a_group_the_rows_lack(tmp_path: Path) -> None:
    profile = _profile(YEAR_GRID, EVEN, NO_MILL)
    exact = {
        "definition": "levelHistogram/1",
        "groupBy": "branch",
        "sampleSize": {"min": 1000, "max": 1000},
        "disclosure": "none",
        "levels": ["audio", "book"],
        "groups": [
            {"group": branch, "k": 250, "levelCounts": [75, 175], "status": "F"}
            for branch in BRANCHES[:3]
        ]
        + [{"group": "mill", "k": 0, "levelCounts": [0, 0], "status": "F"}],
    }
    profile["tables"]["loan"]["columns"]["format"]["statistics"][1] = exact
    outcome = _verify(tmp_path, profile, LEVELS_BY_BRANCH)
    assert "the two sides group into different sets: 4 in the profile and 3 synthetic" in (
        outcome.stdout
    ), outcome.stdout
    assert not outcome.success


def test_a_sample_leaves_out_a_level_the_profile_holds_no_record_of_and_the_rows_lack(
    tmp_path: Path,
) -> None:
    profile = _profile(YEAR_GRID, EVEN, NO_MILL, NO_MILL_DATED)
    outcome = _sampled(tmp_path, profile, DATES_BY_BRANCH)
    assert outcome.exit_code == 0, outcome.stdout + outcome.stderr
    assert "FLX-5005" not in outcome.stdout + outcome.stderr
    assert "histograms 2 within, 0 outside, 0 undecided" in outcome.stdout, outcome.stdout


def test_a_sample_leaves_out_a_level_the_profile_holds_no_record_of_and_the_rows_hold_one(
    tmp_path: Path,
) -> None:
    profile = _profile(YEAR_GRID, EVEN, ONE_MILL, NO_MILL_DATED)
    outcome = _sampled(tmp_path, profile, DATES_BY_BRANCH)
    assert outcome.exit_code == 0, outcome.stdout + outcome.stderr
    assert "FLX-5005" not in outcome.stdout + outcome.stderr
    assert "histograms 2 within, 0 outside, 0 undecided" in outcome.stdout, outcome.stdout


def test_a_sample_holds_the_rows_of_such_a_level_to_the_floor_as_the_full_comparison_does(
    tmp_path: Path,
) -> None:
    profile = _profile(YEAR_GRID, EVEN, ONE_MILL, NO_MILL_DATED)
    outcome = _sampled(tmp_path, profile, DATES_BY_BRANCH, minimum_records=5)
    assert outcome.exit_code == 1, outcome.stdout + outcome.stderr
    assert "1 records, minimum is 5" in outcome.stdout + outcome.stderr
