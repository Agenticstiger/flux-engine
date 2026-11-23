# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A 26-row catalogue that passes its own profile, through the ``fluxsim`` command line.

``tests/fixtures/small-catalogue`` holds a contract with one ``catalogue`` expose and the profile
of those same 26 rows as a source would publish it: a categorical column, a column given it and a
binned column. Every run here is ``fluxsim init``, ``plan``, ``apply`` and ``verify`` in a
subprocess, and every share is recomputed from the rows the run wrote. What is pinned:

* **Exact shares.** Every column ``allocation`` can read takes ``allocation: exact``, the key does
  not, and each class holds exactly its share of the 26 rows: the levels, the levels of the given
  column within each level of its parent, and the bins.
* **Scored.** Verify passes, every distance tolerance carries ``sampling: true``, and its row in
  the scorecard is within with the rounding term alone as its widening.
* **Seeds.** The same holds for several seeds, since the counts never vary.
* **The defect.** The same bundle with ``allocation`` stripped draws each row by sampling, and at
  the fixed seed measured here it misses at least one of the profile's own shares.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest
import yaml

from flux_engine.stages.layout import RunLayout
from flux_stats.noise import sampling_widening

from .conftest import REPO_ROOT

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "small-catalogue"
CONTRACT = FIXTURE / "catalogue.fluid.yaml"
PROFILE = FIXTURE / "catalogue.profile.json"
NAMESPACE = "com.agentics.flux/1"
ROWS = 26
SEEDS = (42, 7, 99, 2026, 31337)
#: The seed at which the stripped bundle was measured to miss a share of its own profile.
MEASURED_SEED = 42
EXACT_COLUMNS = {"category", "tier", "price"}


def _run(args: Sequence[str], cwd: Path) -> Dict[str, Any]:
    """Run ``fluxsim -j <args>`` as a subprocess and return its JSON envelope."""
    argv = [sys.executable, "-m", "flux_engine.cli", "-j", *args]
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr or done.stdout
    envelope: Dict[str, Any] = json.loads(done.stdout)
    assert envelope["ok"], envelope
    return envelope


def _profile() -> Dict[str, Any]:
    document: Dict[str, Any] = json.loads(PROFILE.read_text("utf-8"))
    return document


def _statistics(column: str) -> List[Dict[str, Any]]:
    statistics: List[Dict[str, Any]] = _profile()["tables"]["catalogue"]["columns"][column][
        "statistics"
    ]
    return statistics


def _published() -> Dict[str, Dict[Tuple[str, ...], int]]:
    """What the profile publishes, as class counts: level, level within a parent level, and bin."""
    (category,) = _statistics("category")
    levels = category["levels"]
    published: Dict[str, Dict[Tuple[str, ...], int]] = {
        "category": {
            (level,): count
            for level, count in zip(levels, category["groups"][0]["levelCounts"], strict=True)
        }
    }
    (grouped,) = [found for found in _statistics("tier") if found.get("groupBy") == "category"]
    published["tier"] = {
        (group["group"], level): count
        for group in grouped["groups"]
        for level, count in zip(grouped["levels"], group["levelCounts"], strict=True)
    }
    (prices,) = _statistics("price")
    published["price"] = {
        (str(position),): count for position, count in enumerate(prices["groups"][0]["binCounts"])
    }
    return published


def _observed(rows: List[Dict[str, Any]]) -> Dict[str, Dict[Tuple[str, ...], int]]:
    """The same class counts, read from the rows a run wrote (the last bin holds its upper edge)."""
    observed: Dict[str, Dict[Tuple[str, ...], int]] = {"category": {}, "tier": {}, "price": {}}
    for row in rows:
        classes = {
            "category": (row["category"],),
            "tier": (row["category"], row["tier"]),
            "price": ("0",) if row["price"] < 50 else ("1",),
        }
        for column, key in classes.items():
            observed[column][key] = observed[column].get(key, 0) + 1
    return observed


def _bundle(tmp_path: Path, *, seed: int, allocation: bool = True) -> Path:
    """``fluxsim init`` from the fixture's contract and profile, then the seed (and no allocation)."""
    bundle = tmp_path / "bundle"
    done = _run_init(tmp_path, bundle)
    assert done.returncode == 0, done.stderr
    path = bundle / "simulation.flux.yml"
    text = path.read_text("utf-8")
    assert text.count("  seed: 42\n") == 1
    text = text.replace("  seed: 42\n", f"  seed: {seed}\n")
    if not allocation:
        text, stripped = re.subn(r"\n *allocation: exact(?=\n)", "", text)
        assert stripped == len(EXACT_COLUMNS)
    path.write_text(text, "utf-8")
    return bundle


def _run_init(tmp_path: Path, bundle: Path) -> "subprocess.CompletedProcess[str]":
    argv = [
        sys.executable,
        "-m",
        "flux_engine.cli",
        "init",
        str(bundle),
        "--contract",
        str(CONTRACT),
        "--profile",
        str(PROFILE),
    ]
    return subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=120)


def _verified(tmp_path: Path, bundle: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    state = tmp_path / ".flux"
    planned = _run(
        ["plan", str(bundle), "--state-dir", str(state), "--profile", str(PROFILE)], tmp_path
    )
    run_id = str(planned["artifacts"]["run_id"])
    _run(["apply", str(bundle), "--state-dir", str(state), "--run-id", run_id], tmp_path)
    verified = _run(
        [
            "verify",
            str(bundle),
            "--state-dir",
            str(state),
            "--run-id",
            run_id,
            "--profile",
            str(PROFILE),
        ],
        tmp_path,
    )
    layout = RunLayout.for_state_dir(state, run_id)
    names = json.loads(layout.seam_attestation_path("catalogue").read_text("utf-8"))["columns"]
    rows = [
        dict(zip(names, json.loads(line), strict=True))
        for line in layout.emit_path("catalogue").read_text("utf-8").splitlines()
    ]
    return verified, rows


def _fidelity(bundle: Path) -> Dict[str, Any]:
    document = yaml.safe_load((bundle / "simulation.flux.yml").read_text("utf-8"))
    fidelity: Dict[str, Any] = document["extensions"][NAMESPACE]["emits"]["catalogue"]["fidelity"]
    return fidelity


def _check_exact_run(tmp_path: Path, seed: int) -> None:
    bundle = _bundle(tmp_path, seed=seed)
    fidelity = _fidelity(bundle)
    exact = {
        name
        for name, declared in fidelity["columns"].items()
        if declared.get("allocation") == "exact"
    }
    assert exact == EXACT_COLUMNS and "allocation" not in fidelity["columns"]["sku"]

    verified, rows = _verified(tmp_path, bundle)
    assert len(rows) == ROWS
    assert _observed(rows) == _published()

    for declared in fidelity["properties"]:
        tolerance = declared["tolerance"]
        assert tolerance["type"] == "distance" and tolerance["sampling"] is True
    document = verified["artifacts"]["scorecard"]["document"]
    (table,) = document["tables"]
    assert len(table["properties"]) == len(fidelity["properties"]) == 4
    # The rounding term alone: m / (2 n) for the m cells of a group of n rows (the widest group).
    cells_and_rows = {
        "000:levelHistogram:category": (4, ROWS),
        "001:levelHistogram:tier": (2, ROWS),
        "002:levelHistogram:tier@category": (2, 3),
        "003:marginalHistogram:price": (2, ROWS),
    }
    assert {row["propertyId"] for row in table["properties"]} == set(cells_and_rows)
    for row in table["properties"]:
        cells, rows_of = cells_and_rows[row["propertyId"]]
        rounding = sampling_widening(cells, rows_of, Fraction(1), exact_allocation=True)
        assert row["outcome"] == "within" and row["score"] == "1", row
        assert Fraction(row["widening"]["sampling"]) == rounding, row
        assert set(row["widening"]) == {"sampling"}


def test_the_catalogue_passes_its_own_profile_with_every_share_exact(tmp_path: Path) -> None:
    _check_exact_run(tmp_path, seed=42)


@pytest.mark.parametrize("seed", SEEDS[1:])
def test_the_same_holds_for_other_seeds_since_the_counts_never_vary(
    tmp_path: Path, seed: int
) -> None:
    _check_exact_run(tmp_path, seed=seed)


def test_without_allocation_the_run_misses_a_share_of_its_own_profile(tmp_path: Path) -> None:
    """The defect exact allocation fixes, as a measured fact at one fixed seed."""
    bundle = _bundle(tmp_path, seed=MEASURED_SEED, allocation=False)
    columns = _fidelity(bundle)["columns"]
    assert all("allocation" not in declared for declared in columns.values())
    state = tmp_path / ".flux"
    planned = _run(
        ["plan", str(bundle), "--state-dir", str(state), "--profile", str(PROFILE)], tmp_path
    )
    run_id = str(planned["artifacts"]["run_id"])
    _run(["apply", str(bundle), "--state-dir", str(state), "--run-id", run_id], tmp_path)
    layout = RunLayout.for_state_dir(state, run_id)
    names = json.loads(layout.seam_attestation_path("catalogue").read_text("utf-8"))["columns"]
    rows = [
        dict(zip(names, json.loads(line), strict=True))
        for line in layout.emit_path("catalogue").read_text("utf-8").splitlines()
    ]
    assert len(rows) == ROWS
    observed, published = _observed(rows), _published()
    missed = {
        (column, key): (observed[column].get(key, 0), count)
        for column, shares in published.items()
        for key, count in shares.items()
        if observed[column].get(key, 0) != count
    }
    assert missed, "sampling happened to hit every share of the profile at this seed"
