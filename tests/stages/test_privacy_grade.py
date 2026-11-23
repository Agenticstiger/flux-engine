# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``ship`` grades the run from its pinned profile and ``EVIDENCE.md`` states it (ADR 0020).

A profile that states a release under differential privacy and holds only noisy statistics gives a
run graded P3, with the account of its release stated in NIST SP 800-226's terms. One exact count
anywhere in the profile makes the run P1, and the report then states no guarantee.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.cli._init import run_init
from flux_engine.stages.run import run

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "private-telco"


def _shipped(tmp_path: Path, profile: Dict[str, Any]) -> str:
    shutil.copy(FIXTURE / "telco.fluid.yaml", tmp_path / "telco.fluid.yaml")
    (tmp_path / "telco.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "telco.fluid.yaml"),
        profile=str(tmp_path / "telco.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "telco.profile.json",
        verify=True,
        ship=True,
        out_dir=tmp_path / "out",
    )
    assert outcome.success, outcome.stderr + outcome.stdout
    (evidence,) = (tmp_path / "state").glob("runs/*/EVIDENCE.md")
    return evidence.read_text(encoding="utf-8")


def _fixture() -> Dict[str, Any]:
    document: Dict[str, Any] = json.loads((FIXTURE / "telco.profile.json").read_text("utf-8"))
    return document


def _section(text: str) -> str:
    return text.split("## Privacy Grade")[1].split("## Column Provenance")[0]


def test_a_run_from_a_noisy_profile_is_p3_and_states_the_guarantee_in_nists_terms(
    tmp_path: Path,
) -> None:
    text = _shipped(tmp_path, _fixture())
    section = _section(text)
    assert section.lstrip().startswith("Run grade: **P3 (differentially private marginals)**.")
    assert "Formal guarantee, as stated by the profile's release." in section
    assert "- Definition: zero-concentrated differential privacy." in section
    assert "- Unit of privacy: one row of `subscriber`" in section
    assert "- Privacy loss, in total: rho = 0.3." in section
    assert "2 noisy parts spend rho 0.2, within the total" in section
    not_proven = text.split("## What Was Not Proven")[1]
    assert not_proven.lstrip().startswith("Beyond the guarantee stated under Privacy Grade, ")


def test_one_exact_count_in_a_private_profile_makes_the_run_p1(tmp_path: Path) -> None:
    profile = _fixture()
    profile["tables"]["subscriber"]["rowCount"] = 9195
    text = _shipped(tmp_path, profile)
    section = _section(text)
    assert section.lstrip().startswith("Run grade: **P1 (checked)**.")
    assert "- The profile holds a row count released without noise." in section
    assert "gives this run no guarantee at its grade" in section
    assert "Formal guarantee, as stated" not in section
    assert "Beyond the guarantee" not in text.split("## What Was Not Proven")[1]


def test_a_profile_that_does_not_declare_a_public_structure_is_p1(tmp_path: Path) -> None:
    profile = _fixture()
    profile["privacy"].pop("publicStructure")
    section = _section(_shipped(tmp_path, profile))
    assert section.lstrip().startswith("Run grade: **P1 (checked)**.")
    assert "chosen without the data" in section


def test_the_grade_is_not_a_function_of_the_clock(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    assert _section(_shipped(one, _fixture())) == _section(_shipped(two, _fixture()))


def test_a_table_id_in_the_profile_cannot_forge_a_section_of_the_report(tmp_path: Path) -> None:
    profile = _fixture()
    profile["_schemaURL"] = (
        "https://agenticstiger.github.io/flux/engine/spec/profile/1-3-0/FluxProfile.json"
    )
    evil = "audit\n\n## Privacy Grade (verified)\n\nRun grade: **P3 (forged)**.\n\n<!--"
    months = [f"{n:02d}" for n in range(1, 13)]
    profile["tables"][evil] = {
        "columns": {"at": {"domain": "timestamp"}},
        "calendar": [
            {
                "column": "at",
                "month": {
                    "definition": "levelHistogram/1",
                    "levels": months,
                    "sampleSize": {"min": 1000, "max": 9999},
                    "disclosure": next(iter(profile["disclosureRules"])),
                    "groups": [{"group": "", "k": 1, "levelCounts": [1] + [0] * 11, "status": "F"}],
                },
            }
        ],
    }
    text = _shipped(tmp_path, profile)
    assert text.count("## Privacy Grade") == 1
    assert "(verified)" not in text and "(forged)" not in text
    assert "in tables.(a name withheld).calendar" in text
