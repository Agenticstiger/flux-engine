# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` reads a released date histogram only where its counts stand clear of the noise.

ADR 0019, amendment of 2026-11-25: a bin of a noisy histogram whose count is at most
:func:`flux_stats.noise.noise_floor` (``sqrt(2 v (5 + ceil(ln m)))`` for Gaussian noise of variance
``v`` over ``m`` bins) is read as noise. It carries no weight in the draw and gives the run's
window nothing. When no bin clears the floor, the column is drawn evenly within the run's window,
inside its range, as a noted assumption. The declared range, the histogram's whole grid, and the
histogram property stay, so ``verify`` still scores the column. An exact histogram keeps its
weights.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.run import run
from flux_stats.noise import noise_floor

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
SAMPLE = {"min": 900, "max": 1100}
SCHEMA = "https://agenticstiger.github.io/flux/engine/spec/profile/1-2-0/FluxProfile.json"

#: A lending library's members, and the day each joined.
CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: library.members
name: Members
metadata:
  owner:
    team: library-team
exposes:
  - exposeId: member
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/member.parquet }
    contract:
      schema:
        - { name: member_id, type: INT, required: true }
        - { name: branch, type: "enum('east','north','west')", required: true }
        - { name: joined_on, type: DATE, required: true }
    semantics:
      entities:
        - { name: member, type: primary, expr: member_id }
"""

#: Twelve yearly bins, 2015 to 2026, each edge a whole day.
YEARS = list(range(2015, 2028))


def _ms(year: int) -> str:
    return str(int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000))


def _noise(scale: str) -> Dict[str, Any]:
    return {"mechanism": "gaussian", "scale": scale, "privacyLoss": {"rho": "0.1"}}


def _branches() -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "sampleSize": SAMPLE,
        "disclosure": "noisy",
        "levels": ["east", "north", "west"],
        "noise": _noise("2.5"),
        "groups": [{"group": "", "status": "F", "noisyCounts": ["400", "350", "250"]}],
    }


def _joined(counts: List[str], scale: Optional[str]) -> Dict[str, Any]:
    """The histogram of ``joined_on`` over the yearly bins: noisy at *scale*, or exact for ``None``."""
    statistic: Dict[str, Any] = {
        "definition": "marginalHistogram/1",
        "sampleSize": SAMPLE,
        "bins": [_ms(year) for year in YEARS],
    }
    if scale is None:
        statistic["disclosure"] = "acro-default"
        cells = [int(count) for count in counts]
        statistic["groups"] = [{"group": "", "k": sum(cells), "binCounts": cells, "status": "F"}]
    else:
        statistic["disclosure"] = "noisy"
        statistic["noise"] = _noise(scale)
        statistic["groups"] = [{"group": "", "status": "F", "noisyCounts": counts}]
    return statistic


def _profile(counts: List[str], scale: Optional[str]) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": SCHEMA,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {
            "noisy": {},
            "acro-default": {
                "dominance": {"k": "0.90", "n": 2},
                "minimumCell": 1,
                "pRatio": "0.10",
                "roundingBase": 1,
                "zerosDisclosive": True,
            },
        },
        "privacy": {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "0.5"},
            "privacyUnit": {"expose": "member"},
        },
        "tables": {
            "member": {
                "columns": {
                    "member_id": {"domain": "number"},
                    "branch": {"domain": "category", "statistics": [_branches()]},
                    "joined_on": {"domain": "timestamp", "statistics": [_joined(counts, scale)]},
                }
            }
        },
    }
    return document


def _init(tmp_path: Path, profile: Dict[str, Any]) -> Any:
    (tmp_path / "library.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "library.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "library.fluid.yaml"),
        profile=str(tmp_path / "library.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _simulation(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    document: Dict[str, Any] = yaml.safe_load(text)
    return document


def _joined_on(tmp_path: Path) -> Dict[str, Any]:
    block = _simulation(tmp_path)["extensions"][NAMESPACE]
    column: Dict[str, Any] = block["emits"]["member"]["fidelity"]["columns"]["joined_on"]
    return column


def _histogram_property(tmp_path: Path) -> Dict[str, Any]:
    block = _simulation(tmp_path)["extensions"][NAMESPACE]
    properties = block["emits"]["member"]["fidelity"].get("properties", [])
    (found,) = [
        item
        for item in properties
        if item.get("property") == "marginalHistogram" and item.get("column") == "joined_on"
    ]
    return dict(found)


def _verified(tmp_path: Path) -> Any:
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "library.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr + outcome.stdout
    assert "fidelity member" in outcome.stdout
    return outcome


def _days(tmp_path: Path) -> List[str]:
    (stream,) = (tmp_path / "state").glob("runs/*/emits/member.ndjson")
    return [
        json.loads(line)[2]  # a row is its columns in order: member_id, branch, joined_on
        for line in stream.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


GRID = [f"{year}-01-01" for year in YEARS]


def test_the_floor_is_the_union_bound_over_the_bins() -> None:
    # Twelve bins: ceil(ln 12) = 3, so the floor is sqrt(2 * 50^2 * (5 + 3)) = 200 at scale 50
    # and sqrt(2 * 2.5^2 * 8) = 10 at scale 2.5.
    assert noise_floor("gaussian", Fraction(50), 12) == 200
    assert noise_floor("gaussian", Fraction("2.5"), 12) == 10


def test_a_histogram_within_the_noise_is_drawn_evenly_inside_the_window(tmp_path: Path) -> None:
    # Every count lies below the floor of 200: the histogram says nothing of when members joined.
    counts = ["80", "-40", "120", "60", "-90", "30", "150", "-20", "100", "40", "-60", "90"]
    result = _init(tmp_path, _profile(counts, "50"))
    column = _joined_on(tmp_path)
    window = _simulation(tmp_path)["spec"]["timeBounds"]
    # No span: the window is the assumed year, and its note says why.
    notes = {item["subject"]: item["assumption"] for item in result.artifacts["notes"]}
    assert "a released histogram with a bin that stands clear of the noise" in notes["time window"]
    assert column["bins"] == [window["start"][:10], window["end"][:10]]
    assert column["binWeights"] == ["1"]
    # The range stays the histogram's grid, and its property still compares the grid's bins.
    assert column["range"] == {"min": GRID[0], "max": GRID[-1]}
    assert _histogram_property(tmp_path)["bins"] == GRID
    assert "No bin of the column's released histogram stands clear of the noise" in result.stdout
    _verified(tmp_path)
    days = _days(tmp_path)
    assert days and all(window["start"][:10] <= day <= window["end"][:10] for day in days)


def test_a_strong_signal_keeps_the_bins_that_stand_clear(tmp_path: Path) -> None:
    # The floor is 10: only 2019 and 2020 stand clear, and every other bin is noise.
    counts = ["3", "-2", "4", "1", "500", "300", "2", "-1", "5", "0", "1", "-3"]
    _init(tmp_path, _profile(counts, "2.5"))
    column = _joined_on(tmp_path)
    weights = [Fraction(weight) for weight in column["binWeights"]]
    assert [position for position, weight in enumerate(weights) if weight > 0] == [4, 5]
    assert column["bins"] == GRID and column["range"] == {"min": GRID[0], "max": GRID[-1]}
    window = _simulation(tmp_path)["spec"]["timeBounds"]
    assert window["start"].startswith("2019-01-01") and window["end"].startswith("2021-01-01")
    register = _simulation(tmp_path)["extensions"][NAMESPACE]["parameters"]
    zeroed = [
        entry
        for entry in register
        if entry["pointer"].endswith(("joined_on/binWeights/0", "joined_on/binWeights/11"))
    ]
    assert len(zeroed) == 2
    assert all("does not stand clear of the noise" in entry["reason"] for entry in zeroed)
    _verified(tmp_path)
    assert all("2019-01-01" <= day <= "2021-01-01" for day in _days(tmp_path))


def test_an_exact_histogram_keeps_every_weight(tmp_path: Path) -> None:
    # The same small outer counts, published exactly: they are the data, so they keep their weight.
    counts = ["3", "2", "4", "1", "500", "300", "2", "1", "5", "1", "1", "3"]
    profile = _profile(counts, None)
    _init(tmp_path, profile)
    weights = [Fraction(weight) for weight in _joined_on(tmp_path)["binWeights"]]
    assert all(weight > 0 for weight in weights)
    window = _simulation(tmp_path)["spec"]["timeBounds"]
    assert window["start"].startswith("2015-01-01") and window["end"].startswith("2027-01-01")


# -- a history's emit with a released date column ---------------------------------------------------


def _tiers_with_released_renewals(tmp_path: Path) -> Path:
    """The gym's tiers, whose versions' renewal dates are released with noise on the year grid.

    The renewals sit in 2024 and 2025, well clear of the noise; every other year holds noise. The
    declared range stays the grid, 2015 to 2027, while the draw keeps to the two clear years.
    """
    from tests.author.test_history_date_pairs import TIERS
    from tests.author.test_history_date_pairs import _profile as _tiers

    document = _tiers()
    counts = ["2", "-1", "3", "0", "1", "-2", "4", "1", "-3", "900", "950", "2"]
    renewed = _joined(counts, "2.5")
    document["tables"]["tier_version"]["columns"]["renewed_on"] = {
        "domain": "timestamp",
        "statistics": [renewed],
    }
    document["disclosureRules"]["noisy"] = {}
    document["privacy"] = {
        "measure": "zeroConcentratedDP",
        "privacyLoss": {"rho": "0.5"},
        "privacyUnit": {"expose": "tier_version"},
    }
    (tmp_path / "tiers.fluid.yml").write_text(TIERS, encoding="utf-8")
    profile = tmp_path / "tiers.profile.json"
    profile.write_text(json.dumps(document), encoding="utf-8")
    return profile


def test_a_history_emit_keeps_a_released_column_drawn_inside_the_window(tmp_path: Path) -> None:
    # The renewals' range is the whole grid, past the window, while their draw keeps inside it, so
    # plan accepts the history (ADR 0017, amendment of 2026-11-25) and verify scores the column.
    profile = _tiers_with_released_renewals(tmp_path)
    result = run_init(
        str(tmp_path / "bundle"), contract=str(tmp_path / "tiers.fluid.yml"), profile=str(profile)
    )
    assert result.exit_code == 0, result.stderr
    block = _simulation(tmp_path)["extensions"][NAMESPACE]
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    renewed = block["emits"]["tier_version"]["fidelity"]["columns"]["renewed_on"]
    assert renewed["range"] == {"min": GRID[0], "max": GRID[-1]}
    weights = [Fraction(weight) for weight in renewed["binWeights"]]
    assert [position for position, weight in enumerate(weights) if weight > 0] == [9, 10]
    window = _simulation(tmp_path)["spec"]["timeBounds"]
    assert window["start"][:10] <= "2024-01-01" and window["end"][:10] >= "2026-01-01"
    outcome = run(tmp_path / "bundle", state_dir=tmp_path / "state", profile=profile, verify=True)
    assert outcome.success, outcome.stderr + outcome.stdout
    assert "fidelity tier_version" in outcome.stdout
