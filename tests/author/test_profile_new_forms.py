# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A statistic of profile format 1-3-0 that no rule reads yet changes nothing but the ledger.

A statistic grouped by a linked row or by a number column, or counted per entity
(PROFILE-SPEC.md section 3), answers another question from an ungrouped statistic of the same
column. ``fluxsim init`` passes it over, lists it among the statistics it does not use with the
reason, and writes the bundle it would write without it. A run and ``verify`` against the profile
are unaffected.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.author.forms import BY_LINK, BY_NUMBER, PER_ENTITY
from flux_engine.cli._init import run_init
from flux_engine.stages.run import run
from flux_engine.stages.verify import verify
from flux_stats.profile import SCHEMA_URL, SCHEMA_URL_1_3_0, parse_profile

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "tests" / "fixtures" / "author" / "subscriber-payments.fluid.yaml"
BASE: Dict[str, Any] = json.loads(
    (REPO_ROOT / "docs" / "spec" / "profile-example.json").read_text(encoding="utf-8")
)
NAMESPACE = "com.agentics.flux/1"
BAND = {"min": 1000, "max": 9999}


def _with_the_new_forms() -> Dict[str, Any]:
    document = copy.deepcopy(BASE)
    assert document["_schemaURL"] == SCHEMA_URL
    document["_schemaURL"] = SCHEMA_URL_1_3_0
    rule = next(iter(document["disclosureRules"]))
    columns = document["tables"]["payment"]["columns"]
    columns["amount"]["statistics"].append(
        {
            "definition": "mean/1",
            "sampleSize": BAND,
            "disclosure": rule,
            "groupBy": "attempts",
            "groups": [{"group": "1", "k": 100, "Sx": "1000", "status": "F"}],
        }
    )
    columns["attempts"]["statistics"] = [
        {
            "definition": "mean/1",
            "sampleSize": BAND,
            "disclosure": rule,
            "groupByLink": {
                "parent": {"table": "subscriber", "columns": ["subscriber_id"]},
                "columns": ["subscriber_id"],
                "column": "market",
            },
            "groups": [{"group": "DE", "k": 100, "Sx": "1000", "status": "F"}],
        }
    ]
    columns["subscriber_id"]["statistics"] = [
        {
            "definition": "levelHistogram/1",
            "levels": ["a", "b"],
            "sampleSize": BAND,
            "disclosure": rule,
            "per": ["paid_at"],
            "groups": [{"group": "", "k": 100, "levelCounts": [40, 60], "status": "F"}],
        }
    ]
    parse_profile(document)  # the reader takes it
    return document


class Authored:
    def __init__(self, root: Path, document: Dict[str, Any]) -> None:
        self.profile = root / "profile.json"
        self.profile.write_text(json.dumps(document), encoding="utf-8")
        self.bundle = root / "bundle"
        result = run_init(str(self.bundle), contract=str(CONTRACT), profile=str(self.profile))
        assert result.exit_code == 0, result.stderr
        self.state = root / "state"
        outcome = run(self.bundle, state_dir=self.state, profile=self.profile)
        assert outcome.success, outcome.stderr
        self.run_id: str = outcome.artifacts["plan"]["run_id"]

    def files(self) -> Dict[str, str]:
        return {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(self.bundle.iterdir())
            if path.is_file()
        }


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> Authored:
    return Authored(tmp_path_factory.mktemp("plain"), copy.deepcopy(BASE))


@pytest.fixture(scope="module")
def formed(tmp_path_factory: pytest.TempPathFactory) -> Authored:
    return Authored(tmp_path_factory.mktemp("formed"), _with_the_new_forms())


def _comparable(authored: Authored) -> Dict[str, Any]:
    """The bundle with what must differ taken out: the profile's name and digest."""
    found: Dict[str, Any] = {}
    for name, text in authored.files().items():
        text = re.sub(r"^# Written by .*\n", "", text)
        if not name.endswith(".yml"):
            found[name] = text
            continue
        document = yaml.safe_load(text)
        block = document.get("extensions", {}).get(NAMESPACE, {})
        block.pop("profile", None)
        found[name] = document
    return found


def _unused(authored: Authored) -> List[Dict[str, Any]]:
    document = yaml.safe_load((authored.bundle / "simulation.flux.yml").read_text("utf-8"))
    found = document["extensions"][NAMESPACE]["unused"]
    assert isinstance(found, list)
    return found


def test_the_bundle_is_the_one_written_without_the_forms_but_for_the_ledger(
    plain: Authored, formed: Authored
) -> None:
    without, with_forms = _comparable(plain), _comparable(formed)
    new = [item for item in _unused(formed) if item not in _unused(plain)]
    assert len(new) == 3
    kept = copy.deepcopy(with_forms)
    block = kept["simulation.flux.yml"]["extensions"][NAMESPACE]
    block["unused"] = [item for item in block["unused"] if item not in new]
    assert kept == without


def test_the_ledger_lists_each_form_with_its_reason(formed: Authored) -> None:
    reasons = {
        item["statistic"]["pointer"]: item["reason"]
        for item in _unused(formed)
        if item["reason"] in (BY_LINK, BY_NUMBER, PER_ENTITY)
    }
    assert reasons == {
        "/tables/payment/columns/amount/statistics/1": BY_NUMBER,
        "/tables/payment/columns/attempts/statistics/0": BY_LINK,
        "/tables/payment/columns/subscriber_id/statistics/0": PER_ENTITY,
    }


def test_verify_against_such_a_profile_gives_what_it_gives_without_the_forms(
    plain: Authored, formed: Authored
) -> None:
    outcomes = []
    for authored in (plain, formed):
        outcome = verify(
            authored.bundle,
            run_id=authored.run_id,
            state_dir=authored.state,
            profile=authored.profile,
        )
        checked = [
            line.strip()
            for line in (outcome.stdout + outcome.stderr).splitlines()
            if "fidelity" in line or "property " in line or "refusal" in line
        ]
        outcomes.append((outcome.exit_code, checked))
    assert any("fidelity subscriber: passed" in line for line in outcomes[1][1])
    assert outcomes[1] == outcomes[0]
