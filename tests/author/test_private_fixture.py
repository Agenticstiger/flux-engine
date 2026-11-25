# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A profile released under differential privacy, from a profile to a verified run.

``tests/fixtures/private-telco`` holds a contract and a profile of format 1-2-0: a ``privacy``
block, and two noisy cliques that agree on the column they share, one of three columns with a
negative noisy count among them. ``fluxsim init`` compiles it into a junction tree, the run draws
it, and ``verify`` scores it within the tolerances the noise sets. The rows are pinned by their
sha256, so every platform of the CI matrix has to draw the same bytes, and they are the same for any
``--jobs``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.run import run

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "private-telco"
NAMESPACE = "com.agentics.flux/1"

#: sha256 of the rows ``fluxsim run`` emits for the fixture, whatever the platform or ``--jobs``.
EMITTED_SHA256 = "d4b6902a0531324bae190bb36f75a6d23a3f79bf3b750a4c4a72484bbd609a95"


def _bundle(tmp_path: Path) -> Any:
    for name in ("telco.fluid.yaml", "telco.profile.json"):
        shutil.copy(FIXTURE / name, tmp_path / name)
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "telco.fluid.yaml"),
        profile=str(tmp_path / "telco.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def test_the_fixture_compiles_generates_and_verifies(tmp_path: Path) -> None:
    result = _bundle(tmp_path)
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    columns = block["emits"]["subscriber"]["fidelity"]["columns"]
    assert columns["channel"]["given"] == ["region", "plan"]
    assert block["emits"]["subscriber"]["rows"] == 9195  # both cliques total 6100 + 3095 records
    assert [
        e["noise"]["mechanism"] for e in result.artifacts["ledger"]["statistics"] if "noise" in e
    ] == [
        "gaussian",
        "gaussian",
    ]
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "telco.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr + outcome.stdout
    assert "fidelity subscriber" in outcome.stdout


def test_init_notes_a_stated_loss_above_what_the_noise_blocks_spend(tmp_path: Path) -> None:
    # The fixture states a total rho of 0.3 and its two noise blocks spend 0.2. Both are values the
    # profile states, and the note gives both.
    result = _bundle(tmp_path)
    assert result.artifacts["privacyLoss"] == {
        "measure": "zeroConcentratedDP",
        "stated": {"rho": "0.3"},
        "spent": {"rho": "0.2"},
        "parts": 2,
    }
    assert "privacy: the release's loss is the stated rho 0.3;" in result.stdout
    assert "still holds spend rho 0.2 of it" in result.stdout


def test_init_notes_nothing_when_the_stated_loss_is_spent_in_full(tmp_path: Path) -> None:
    shutil.copy(FIXTURE / "telco.fluid.yaml", tmp_path / "telco.fluid.yaml")
    document = json.loads((FIXTURE / "telco.profile.json").read_text(encoding="utf-8"))
    document["privacy"]["privacyLoss"] = {"rho": "0.2"}
    (tmp_path / "telco.profile.json").write_text(json.dumps(document), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "telco.fluid.yaml"),
        profile=str(tmp_path / "telco.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    assert "privacyLoss" not in result.artifacts
    assert "privacy: the release's loss" not in result.stdout


@pytest.mark.parametrize("jobs", [1, 3])
def test_the_rows_are_the_same_bytes_on_every_platform_and_for_any_jobs(
    tmp_path: Path, jobs: int
) -> None:
    _bundle(tmp_path)
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "telco.profile.json",
        jobs=jobs,
    )
    assert outcome.success, outcome.stderr
    (stream,) = (tmp_path / "state").glob("runs/*/emits/subscriber.ndjson")
    assert hashlib.sha256(stream.read_bytes()).hexdigest() == EMITTED_SHA256


def test_the_profile_is_a_valid_profile_of_format_1_2_0() -> None:
    from flux_stats.profile import SCHEMA_URL_1_2_0, parse_profile

    document = json.loads((FIXTURE / "telco.profile.json").read_text(encoding="utf-8"))
    assert document["_schemaURL"] == SCHEMA_URL_1_2_0
    profile = parse_profile(document)
    assert profile.accounting is not None and len(profile.tables["subscriber"].cliques) == 2
