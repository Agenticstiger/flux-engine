# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A quantile that declares bins goes through every stage (ADR 0012).

The small telco bundle gains one property, a median of ``amount`` with bins,
which is measured and compared by ``quantile/2``. The run is planned and
applied against a pinned profile (format 1-1-0) that holds the statistic, then
verified and shipped. One case is scored within (the profile of the run's own
rows) and one outside (a profile that puts the median in the top bin), and the
scorecard lists the property's statistic as ``quantile/2`` in both.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.concordance.compiler import compile_fidelity
from flux_engine.concordance.predicate import expose_from_contract
from flux_engine.concordance.scorecard import SCORECARD_FILE
from flux_stats.profile import SCHEMA_URL_1_1_0
from tests.stats.profiles import edited, profile_from_rows, statistic_of

from .conftest import FIXTURE_BUNDLE
from .test_scorecard_stages import EXPOSE, Pinned, Unpinned

BINNED = "001:quantile:amount"
BINS = ["1", "2500", "5000", "7500", "10000"]
ANCHOR = "            - property: support\n"
QUANTILE = (
    "            - property: quantile\n"
    "              column: amount\n"
    '              level: "0.5"\n'
    f"              bins: [{', '.join(chr(34) + edge + chr(34) for edge in BINS)}]\n"
    '              tolerance: { type: absolute, value: "50" }\n'
)


@pytest.fixture(scope="module")
def binned_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The small telco bundle with a binned median of ``amount`` declared before ``support``."""
    bundle = tmp_path_factory.mktemp("binned") / "bundle"
    shutil.copytree(FIXTURE_BUNDLE, bundle)
    (bundle / "flux-manifest.json").unlink(missing_ok=True)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    assert text.count(ANCHOR) == 1
    simulation.write_text(text.replace(ANCHOR, QUANTILE + ANCHOR), encoding="utf-8")
    return bundle


@pytest.fixture(scope="module")
def own_profile(binned_bundle: Path, tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    """A profile of the rows the bundle's own run emits: what the run must match exactly."""
    unpinned = Unpinned(tmp_path_factory.mktemp("unpinned"), binned_bundle)
    fidelity_input = json.loads(
        (unpinned.run_dir / f"fidelity-input-{EXPOSE}.json").read_text(encoding="utf-8")
    )
    contract = fidelity_input["contract"]
    compiled = compile_fidelity(
        contract=contract,
        emit=fidelity_input["emit"],
        expose=expose_from_contract(contract, EXPOSE),
    )
    attestation = json.loads(
        (unpinned.run_dir / f"seam-attestation-{EXPOSE}.json").read_text(encoding="utf-8")
    )
    lines = (unpinned.run_dir / "emits" / f"{EXPOSE}.ndjson").read_text().splitlines()
    rows = [
        dict(zip(attestation["columns"], json.loads(line), strict=True)) for line in lines if line
    ]
    return profile_from_rows(rows, compiled)


def _top_bin(profile: Dict[str, Any]) -> Dict[str, Any]:
    """*profile* claiming its median sits in the last bin, [7500, 10000]: far from the run's."""
    document = edited(profile)
    (group,) = statistic_of(document, EXPOSE, "amount", "quantile/2")["groups"]
    # The median is the 32nd of 64 records: 31 below the bin and all of them through it.
    group.update(quantileBin=3, binBelow=31, binThrough=group["k"])
    return document


def _scorecard_entry(document: Dict[str, Any]) -> Dict[str, Any]:
    (table,) = [table for table in document["tables"] if table["exposeId"] == EXPOSE]
    return next(entry for entry in table["properties"] if entry["propertyId"] == BINNED)


def test_the_profile_of_the_run_holds_the_bin_in_format_1_1_0(own_profile: Dict[str, Any]) -> None:
    assert own_profile["_schemaURL"] == SCHEMA_URL_1_1_0
    statistic = statistic_of(own_profile, EXPOSE, "amount", "quantile/2")
    assert statistic["bins"] == BINS and statistic["level"] == "0.5"
    assert "quantileWitness" not in json.dumps(statistic)


def test_a_binned_quantile_within_its_bin_is_scored_by_quantile_2_and_ships(
    tmp_path: Path, binned_bundle: Path, own_profile: Dict[str, Any]
) -> None:
    pinned = Pinned(tmp_path, own_profile, source=binned_bundle)
    verified = pinned.verify(profile=pinned.profile)
    assert verified.success, verified.stderr
    entry = _scorecard_entry(verified.artifacts["scorecard"]["document"])
    assert (entry["statistic"], entry["outcome"], entry["score"]) == ("quantile/2", "within", "1")
    assert verified.artifacts["fidelity"][EXPOSE]["status"] == "passed"

    shipped = pinned.ship(tmp_path / "out", profile=pinned.profile)
    assert shipped.success, shipped.stderr
    on_disk = json.loads((pinned.run_dir / SCORECARD_FILE).read_text(encoding="utf-8"))
    assert _scorecard_entry(on_disk)["statistic"] == "quantile/2"
    assert on_disk == verified.artifacts["scorecard"]["document"]


def test_a_binned_quantile_in_another_bin_is_outside_and_withholds(
    tmp_path: Path, binned_bundle: Path, own_profile: Dict[str, Any]
) -> None:
    pinned = Pinned(tmp_path, _top_bin(own_profile), source=binned_bundle)
    out = tmp_path / "out"
    shipped = pinned.ship(out, profile=pinned.profile)
    assert shipped.exit_code == 1 and shipped.error_code == "FLX-8001", shipped.stderr
    assert not out.exists() or not any(out.iterdir()), "a withheld ship writes nothing"
    entry = _scorecard_entry(shipped.artifacts["scorecard"])
    assert (entry["statistic"], entry["outcome"]) == ("quantile/2", "outside")
    assert "property 001:quantile:amount (" in shipped.stdout

    verified = pinned.verify(profile=pinned.profile)
    assert verified.exit_code == 1
    assert _scorecard_entry(verified.artifacts["scorecard"]["document"])["outcome"] == "outside"
