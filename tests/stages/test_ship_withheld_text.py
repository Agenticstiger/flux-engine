# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a person reads when a ship is withheld: a refusal is a code and a reason, and is called refused."""

from __future__ import annotations

import pytest

from flux_engine.concordance.gate import GateAction, GateCheck, GateDecision
from flux_engine.concordance.predicate import PropertyKind
from flux_engine.concordance.verifier import (
    OUTSIDE,
    FidelityEvaluation,
    PropertyOutcome,
    Refusal,
)
from flux_engine.stages.ship import _withheld_summary

pytestmark = pytest.mark.unit

PROPERTY = "000:marginalHistogram:amount"


def _decision(refusals: tuple[Refusal, ...]) -> GateDecision:
    outcome = PropertyOutcome(
        property_id=PROPERTY,
        kind=PropertyKind.MARGINAL_HISTOGRAM,
        columns=("amount",),
        group_by=None,
        tolerance={"max": "0.10"},
        margin="0.0999908447265625",
        epsilon="0",
        outcome=OUTSIDE,
    )
    return GateDecision(
        action=GateAction.WITHHOLD,
        expose_id="orders",
        checks=(GateCheck(name="fidelity.properties", passed=False, detail="1 outside"),),
        evaluation=FidelityEvaluation(
            expose_id="orders", outcomes=(outcome,), refusals=refusals, synthetic_rows=10
        ),
        code="FLX-8001",
    )


def test_a_refusal_reads_as_its_code_and_reason_never_as_a_dict() -> None:
    reason = "the profile holds no marginalHistogram/1 statistic over this property's columns"
    text = _withheld_summary(
        {"orders": _decision((Refusal(code="FLX-5037", reason=reason, property_id=PROPERTY),))}
    )
    assert f"  - FLX-5037: {reason}" in text.splitlines()
    assert "{" not in text and "refusal:" not in text


def test_a_refused_property_is_called_refused_and_names_no_margin() -> None:
    text = _withheld_summary(
        {"orders": _decision((Refusal(code="FLX-5037", reason="r", property_id=PROPERTY),))}
    )
    line = next(line for line in text.splitlines() if "property " in line)
    assert "refused, not compared (FLX-5037; declared max=0.10)" in line
    assert "outside the declared margin" not in line and "within" not in line
    assert "0.0999908447265625" not in line and "declared max=0.10" in line


def test_a_property_outside_its_margin_with_no_refusal_still_says_outside() -> None:
    text = _withheld_summary({"orders": _decision(())})
    line = next(line for line in text.splitlines() if "property " in line)
    assert "outside the declared margin 0.0999908447265625 (declared max=0.10)" in line


def test_the_last_line_carries_the_first_failing_check_its_remedy_and_the_count() -> None:
    from flux_engine.stages.ship import _withheld_line

    decision = _decision(())
    line = _withheld_line({"orders": decision})
    assert line.startswith("FLX-8001: withheld orders: fidelity.properties: 1 outside; to fix: ")
    assert "more" not in line

    two = GateDecision(
        action=GateAction.WITHHOLD,
        expose_id="orders",
        checks=(
            GateCheck(name="profile.properties", passed=False, detail="1 outside"),
            GateCheck(name="scorecard.threshold", passed=False, detail="too low"),
            GateCheck(name="record.shape", passed=True, detail="fine"),
        ),
        code="FLX-8001",
    )
    line = _withheld_line({"orders": two})
    assert "profile.properties: 1 outside; to fix: change the numbers" in line
    assert line.endswith("; and 1 more")
