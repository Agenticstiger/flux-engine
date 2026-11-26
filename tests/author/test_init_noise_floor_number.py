# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` reads a released number histogram only where its counts stand clear of the noise.

ADR 0019, amendment of 2026-11-25, extended to number columns: a bin of a noisy histogram whose
count is at most :func:`flux_stats.noise.noise_floor` (``sqrt(2 v (5 + ceil(ln m)))`` for Gaussian
noise of variance ``v`` over ``m`` bins) is read as noise. It carries no weight in the draw, and
the outer bins inside the noise do not set where the column's values lie for a rule that reads
them. When no bin clears the floor, the column is drawn evenly over its declared range as a noted
assumption. The declared range, the histogram's whole grid, and the histogram property stay, so
``verify`` still scores the column. An exact histogram keeps its weights.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.run import run
from flux_stats.noise import noise_floor
from tests.author.test_init_noise_floor import _branches, _joined, _noise

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
SAMPLE = {"min": 900, "max": 1100}
SCHEMA = "https://agenticstiger.github.io/flux/engine/spec/profile/1-2-0/FluxProfile.json"

#: A lending library's loans, and the days each was kept. A late fee is two a day.
CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: library.loans
name: Loans
metadata:
  owner:
    team: library-team
exposes:
  - exposeId: loan
    kind: table
    binding:
      platform: local
      format: parquet
      location: {{ path: ./out/loan.parquet }}
    contract:
      schema:
        - {{ name: loan_id, type: INT, required: true }}
        - {{ name: branch, type: "enum('east','north','west')", required: true }}
        - {{ name: days_kept, type: "{kept}", required: true }}
{extra_column}{invariants}    semantics:
      entities:
        - {{ name: loan, type: primary, expr: loan_id }}
"""
FEE_COLUMN = "        - { name: fee, type: INT, required: true }\n"
FEE_RULE = (
    "      invariants:\n" '        - { id: fee_of_days, derive: fee, expr: "days_kept * 2" }\n'
)


def _contract(kept: str = "INT", *, fee: bool = False) -> str:
    text = CONTRACT.format(
        kept=kept,
        extra_column=FEE_COLUMN if fee else "",
        invariants=FEE_RULE if fee else "",
    )
    return text


#: Twelve bins of ten days each, 0 to 120.
GRID = [str(edge) for edge in range(0, 130, 10)]

#: Every count lies below the floor of 10 that twelve bins of scale 2.5 give.
INSIDE = ["3", "-2", "4", "1", "5", "-1", "2", "-1", "5", "0", "1", "-3"]
#: Only the fifth and sixth bins, 40 to 60 days, stand clear; the outer two are noise.
SIGNAL = ["3", "-2", "4", "1", "500", "300", "2", "-1", "5", "0", "1", "-3"]


def _kept(counts: List[str], scale: Optional[str]) -> Dict[str, Any]:
    """The histogram of ``days_kept`` over the grid: noisy at *scale*, or exact for ``None``."""
    statistic = _joined(counts, scale)  # the date column's, over a grid of days
    statistic["bins"] = GRID
    return statistic


def _profile(counts: List[str], scale: Optional[str], *, fee: bool = False) -> Dict[str, Any]:
    columns: Dict[str, Any] = {
        "loan_id": {"domain": "number"},
        "branch": {"domain": "category", "statistics": [_branches()]},
        "days_kept": {"domain": "number", "statistics": [_kept(counts, scale)]},
    }
    if fee:
        columns["fee"] = {"domain": "number"}
    return {
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
            "privacyUnit": {"expose": "loan"},
        },
        "tables": {"loan": {"columns": columns}},
    }


def _init(tmp_path: Path, profile: Dict[str, Any], contract: Optional[str] = None) -> Any:
    (tmp_path / "library.fluid.yaml").write_text(contract or _contract(), encoding="utf-8")
    (tmp_path / "library.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "library.fluid.yaml"),
        profile=str(tmp_path / "library.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _block(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block


def _column(tmp_path: Path, name: str = "days_kept") -> Dict[str, Any]:
    column: Dict[str, Any] = _block(tmp_path)["emits"]["loan"]["fidelity"]["columns"][name]
    return column


def _histogram_property(tmp_path: Path) -> Dict[str, Any]:
    properties = _block(tmp_path)["emits"]["loan"]["fidelity"].get("properties", [])
    (found,) = [
        item
        for item in properties
        if item.get("property") == "marginalHistogram" and item.get("column") == "days_kept"
    ]
    return dict(found)


def _verified(tmp_path: Path) -> None:
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "library.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr + outcome.stdout
    assert "fidelity loan" in outcome.stdout


def _days(tmp_path: Path) -> List[Fraction]:
    (stream,) = (tmp_path / "state").glob("runs/*/emits/loan.ndjson")
    return [
        Fraction(
            str(json.loads(line)[2])
        )  # a row is its columns in order: loan_id, branch, days_kept
        for line in stream.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_the_floor_is_the_union_bound_over_the_bins() -> None:
    # Twelve bins: ceil(ln 12) = 3, so the floor is sqrt(2 * 2.5^2 * (5 + 3)) = 10 at scale 2.5.
    assert noise_floor("gaussian", Fraction("2.5"), 12) == 10


@pytest.mark.parametrize("kept", ["INT", "DECIMAL(10,2)"])
def test_a_strong_signal_keeps_the_bins_that_stand_clear(tmp_path: Path, kept: str) -> None:
    # The floor is 10: only the bins of 40 to 60 days stand clear, and every other bin is noise.
    result = _init(tmp_path, _profile(SIGNAL, "2.5"), _contract(kept))
    column = _column(tmp_path)
    weights = [Fraction(weight) for weight in column["binWeights"]]
    assert [position for position, weight in enumerate(weights) if weight > 0] == [4, 5]
    assert [Fraction(edge) for edge in column["bins"]] == [Fraction(edge) for edge in GRID]
    assert {key: Fraction(value) for key, value in column["range"].items()} == {
        "min": 0,
        "max": 120,
    }
    register = _block(tmp_path)["parameters"]
    zeroed = [
        entry
        for entry in register
        if entry["pointer"].endswith(("days_kept/binWeights/0", "days_kept/binWeights/11"))
    ]
    assert len(zeroed) == 2
    assert all("does not stand clear of the noise" in entry["reason"] for entry in zeroed)
    assert "No bin of the column's released histogram stands clear" not in result.stdout
    _verified(tmp_path)
    days = _days(tmp_path)
    assert days and all(40 <= day <= 60 for day in days)


def test_a_histogram_within_the_noise_is_drawn_evenly_over_its_range(tmp_path: Path) -> None:
    # Every count lies below the floor: the histogram says nothing of how long loans are kept.
    result = _init(tmp_path, _profile(INSIDE, "2.5"))
    column = _column(tmp_path)
    assert column["bins"] == ["0", "120"] and column["binWeights"] == ["1"]
    # The range stays the histogram's grid, and its property still compares the grid's bins.
    assert column["range"] == {"min": "0", "max": "120"}
    assert _histogram_property(tmp_path)["bins"] == GRID
    assert "No bin of the column's released histogram stands clear of the noise" in result.stdout
    states = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}
    note = states["/tables/loan/columns/days_kept/statistics/0"]["note"]
    assert "no bin's count stands clear of the noise" in note
    _verified(tmp_path)
    days = _days(tmp_path)
    assert days and all(0 <= day <= 120 for day in days)


def test_a_published_min_and_max_bound_the_even_draw(tmp_path: Path) -> None:
    # The draw keeps to the published ends: the loans were kept at least 5 and at most 115 days.
    profile = _profile(INSIDE, "2.5")
    metrics = {
        name: {
            "definition": f"{name}/1",
            "status": "F",
            "sampleSize": SAMPLE,
            "disclosure": "acro-default",
            "value": value,
        }
        for name, value in (("min", "5"), ("max", "115"))
    }
    profile["tables"]["loan"]["columns"]["days_kept"]["metrics"] = metrics
    _init(tmp_path, profile)
    column = _column(tmp_path)
    assert column["bins"] == ["5", "115"] and column["binWeights"] == ["1"]
    assert column["range"] == {"min": "0", "max": "120"}


def test_an_exact_histogram_keeps_every_weight(tmp_path: Path) -> None:
    # The same small outer counts, published exactly: they are the data, so they keep their weight.
    counts = ["3", "2", "4", "1", "500", "300", "2", "1", "5", "1", "1", "3"]
    _init(tmp_path, _profile(counts, None))
    column = _column(tmp_path)
    assert all(Fraction(weight) > 0 for weight in column["binWeights"])
    assert len(column["binWeights"]) == 12


def test_a_release_whose_bins_all_stand_clear_is_read_as_before(tmp_path: Path) -> None:
    # Every count is far above the floor: nothing is zeroed, and nothing says a bin was noise.
    counts = ["120", "150", "200", "310", "500", "300", "260", "190", "150", "120", "90", "70"]
    result = _init(tmp_path, _profile(counts, "2.5"))
    column = _column(tmp_path)
    assert len(column["binWeights"]) == 12
    assert all(Fraction(weight) > 0 for weight in column["binWeights"])
    assert "stands clear of the noise" not in result.stdout
    assert not any(
        "does not stand clear" in entry.get("reason", "")
        for entry in _block(tmp_path)["parameters"]
    )


@pytest.mark.parametrize(
    ("first", "clears"), [("10", False), ("10.000001", True)], ids=["at the floor", "above it"]
)
def test_a_count_at_the_floor_is_noise_and_one_above_it_stands_clear(
    tmp_path: Path, first: str, clears: bool
) -> None:
    # Twelve bins of Gaussian noise of scale 2.5 have a floor of exactly 10: only a count above it
    # stands clear (strictly greater).
    assert noise_floor("gaussian", Fraction("2.5"), 12) == 10
    _init(tmp_path, _profile([first] + ["0"] * 11, "2.5"))
    column = _column(tmp_path)
    if clears:
        assert len(column["binWeights"]) == 12 and column["binWeights"][0] != "0"
        assert all(weight == "0" for weight in column["binWeights"][1:])
    else:
        assert column["binWeights"] == ["1"] and len(column["bins"]) == 2


def test_the_outer_bins_inside_the_noise_do_not_set_what_a_rule_reads(tmp_path: Path) -> None:
    # A late fee is two a day. The days kept are drawn between 40 and 60, so the fee lies between
    # 80 and 120; the noise in the outer bins, whose edges span 0 to 120, does not stretch it.
    _init(tmp_path, _profile(SIGNAL, "2.5", fee=True), _contract(fee=True))
    assert _column(tmp_path, "fee") == {"range": {"min": "80", "max": "120"}}
    # The days' own range is still the histogram's whole grid.
    assert _column(tmp_path)["range"] == {"min": "0", "max": "120"}
    _verified(tmp_path)


def test_a_rule_reads_the_whole_grid_when_no_bin_stands_clear(tmp_path: Path) -> None:
    # Drawn evenly over 0 to 120, the days reach the whole grid, and so does the fee's range.
    _init(tmp_path, _profile(INSIDE, "2.5", fee=True), _contract(fee=True))
    assert _column(tmp_path, "fee") == {"range": {"min": "0", "max": "240"}}


def test_an_exact_histogram_sets_what_a_rule_reads_with_its_whole_range(tmp_path: Path) -> None:
    # Exact counts of 3 in the outer bins are the data: the fee's range reaches them.
    counts = ["3", "2", "4", "1", "500", "300", "2", "1", "5", "1", "1", "3"]
    _init(tmp_path, _profile(counts, None, fee=True), _contract(fee=True))
    assert _column(tmp_path, "fee") == {"range": {"min": "0", "max": "240"}}


def test_a_key_keeps_its_bins_as_they_stand(tmp_path: Path) -> None:
    # The last 3 loan ids sit inside the noise (the floor is 10), but a key's rows hold distinct
    # values, so each bin keeps the room its count asks for: zeroing the last would leave the key
    # 3 values short of its 1103 rows, and the bundle would be refused (FLX-3403).
    ids = {
        "definition": "marginalHistogram/1",
        "sampleSize": SAMPLE,
        "disclosure": "noisy",
        "noise": _noise("2.5"),
        "bins": [str(edge) for edge in range(1, 1202, 100)],
        "groups": [{"group": "", "status": "F", "noisyCounts": ["100"] * 11 + ["3"]}],
    }
    profile = _profile(SIGNAL, "2.5")
    columns = profile["tables"]["loan"]["columns"]
    columns["loan_id"] = {"domain": "number", "statistics": [ids]}
    columns["branch"]["statistics"][0]["groups"][0]["noisyCounts"] = ["400", "403", "300"]
    _init(tmp_path, profile)
    assert _column(tmp_path, "loan_id")["binWeights"] == ["100"] * 11 + ["3"]


def test_a_published_mean_shapes_a_column_with_no_bin_that_stands_clear(tmp_path: Path) -> None:
    # The histogram says nothing, but the mean of 30 days is published: the even draw over 0 to 120
    # would have a mean of 60, so it is shaped to the mean as any bins are, and verify holds it.
    profile = _profile(INSIDE, "2.5")
    profile["tables"]["loan"]["columns"]["days_kept"]["statistics"].append(
        {
            "definition": "mean/1",
            "sampleSize": SAMPLE,
            "disclosure": "acro-default",
            "groups": [{"group": "", "k": 1000, "Sx": str(30 * 1000 * 2**16), "status": "F"}],
        }
    )
    _init(tmp_path, profile)
    column = _column(tmp_path)
    assert column["bins"][0] == "0" and column["bins"][-1] == "120"
    assert len(column["bins"]) > 2  # cut by the mean
    _verified(tmp_path)
    days = _days(tmp_path)
    assert days and all(0 <= day <= 120 for day in days)
    assert abs(sum(days) / len(days) - 30) <= 3


def test_a_column_a_rule_sets_is_not_noted_as_drawn_evenly(tmp_path: Path) -> None:
    # The fee is set by a rule, so it is not drawn: its own released histogram, all noise, must not
    # be noted as the draw, which would also say the column has a histogram property.
    profile = _profile(SIGNAL, "2.5", fee=True)
    profile["tables"]["loan"]["columns"]["fee"]["statistics"] = [_kept(INSIDE, "2.5")]
    result = _init(tmp_path, profile, _contract(fee=True))
    assert "bins" not in _column(tmp_path, "fee")
    states = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}
    assert "drawn evenly" not in states["/tables/loan/columns/fee/statistics/0"]["note"]
