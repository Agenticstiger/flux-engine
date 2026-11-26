# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` declares no exposure over a relationship whose pick a copy sets (ADR 0022 section 4).

An exposure makes the relationship counted (ADR 0025 section 4), and a copy sets the pick alone:
the plan refuses both for one relationship (``FLX-3403``). The fixture is the exposure chain, its
order also pointing at the subscription and copying the customer from it, so the order's
relationship to the customer is the copy's, and the exposure the profile publishes for it is not
declared. The subscription's exposure is, as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.cli._init import run_init

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "exposure-chain"
NAMESPACE = "com.agentics.flux/1"

_ORDER_KEY = """\
    - name: customer_id
      type: STRING
      required: true
    - name: ordered_at"""
_ORDER_RELATIONSHIP = """\
    - name: order_to_customer
      to: customer
      fromColumns:
      - customer_id
      toColumns:
      - customer_id
      cardinality: many_to_one
"""


def _contract() -> str:
    text = (FIXTURE / "contract.fluid.yaml").read_text(encoding="utf-8")
    assert _ORDER_KEY in text and _ORDER_RELATIONSHIP in text
    text = text.replace(
        _ORDER_KEY,
        "    - name: customer_id\n      type: STRING\n      required: true\n"
        "    - name: subscription_id\n      type: STRING\n      required: true\n"
        "    - name: ordered_at",
    )
    return text.replace(
        _ORDER_RELATIONSHIP,
        _ORDER_RELATIONSHIP
        + "    - name: order_to_subscription\n      to: subscription\n      fromColumns:\n"
        "      - subscription_id\n      toColumns:\n      - subscription_id\n"
        "      cardinality: many_to_one\n      sharedColumns:\n      - customer_id\n",
    )


@pytest.fixture(scope="module")
def block(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    directory = tmp_path_factory.mktemp("exposure-copied-pick")
    document = json.loads((FIXTURE / "profile.json").read_text(encoding="utf-8"))
    document["tables"]["shop_order"]["columns"]["subscription_id"] = {"domain": "category"}
    (directory / "profile.json").write_text(json.dumps(document), encoding="utf-8")
    (directory / "contract.fluid.yaml").write_text(_contract(), encoding="utf-8")
    result = run_init(
        str(directory / "bundle"),
        contract=str(directory / "contract.fluid.yaml"),
        profile=str(directory / "profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    text = (directory / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    found: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return found


def test_the_exposure_over_a_relationship_a_copy_sets_is_not_declared(
    block: Dict[str, Any],
) -> None:
    within = {entry["relationship"]: entry for entry in block["within"]}
    assert block["copies"] is True
    assert "exposure" not in within["order_to_customer"]
    # An exposure over a relationship no copy sets is declared as before.
    assert within["subscription_to_customer"]["exposure"] is True


def test_the_exposure_it_leaves_out_is_unused_with_a_reason_that_names_no_value(
    block: Dict[str, Any],
) -> None:
    reasons = [
        item["reason"]
        for item in block["unused"]
        if item["statistic"]["pointer"] == "/relationships/2/exposure/0"
    ]
    assert len(reasons) == 1
    assert "through the relationship order_to_subscription sets the pick" in reasons[0]
    assert "so no exposure counts its children per parent" in reasons[0]
    assert not any(character.isdigit() for character in reasons[0])
