# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` never writes an exposure its own run cannot meet (ADR 0025 sections 7 and 8).

The fixture is a customer, a subscription within the customer's lifetime, and an invoice within the
subscription's. The subscription's lifetime is cut to the customer's, so the days a subscription is
active are fewer than its own durations give, and the profile's days per subscription are not the
run's. A trial run of the bundle scores the line ``exposure:<relationship>`` as ``verify`` does:
the line the run cannot meet is not declared, its counts are recorded as unused with the numbers,
and every line the bundle does declare passes ``verify``.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

from flux_engine.api.stages import StageResult
from flux_engine.author import bundle as authoring
from flux_engine.cli._init import run_init
from flux_engine.core.errors import FluxError
from flux_engine.core.sizing import FLX_3405, parse_scale
from flux_engine.stages.run import run

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "exposure-chain"
NAMESPACE = "com.agentics.flux/1"


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("exposure-chain") / "bundle"
    result = run_init(
        str(directory),
        contract=str(FIXTURE / "contract.fluid.yaml"),
        profile=str(FIXTURE / "profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return directory


def _block(directory: Path) -> Dict[str, Any]:
    document = yaml.safe_load((directory / "simulation.flux.yml").read_text(encoding="utf-8"))
    return dict(document["extensions"][NAMESPACE])


def test_an_exposure_over_a_lifetime_cut_to_its_parents_is_not_declared(written: Path) -> None:
    within = {entry["relationship"]: entry for entry in _block(written)["within"]}
    assert "exposure" not in within["invoice_to_subscription"]
    # The exposure over a lifetime nothing cuts is declared as before.
    assert within["subscription_to_customer"]["exposure"] is True
    assert within["order_to_customer"]["exposure"] is True


def test_the_exposure_it_leaves_out_is_unused_with_the_numbers_of_the_trial(written: Path) -> None:
    # Both counts leave with the exposure, so the register lists the entry once.
    reasons = [
        item["reason"]
        for item in _block(written)["unused"]
        if item["statistic"]["pointer"] == "/relationships/1/exposure/0"
    ]
    assert len(reasons) == 1
    assert (
        "the run cannot reproduce this exposure's rate under the declared lifetimes" in reasons[0]
    )
    assert "whose parent's lifetime lies within its own parent's lifetime" in reasons[0]
    assert "children per active day 1751 over" in reasons[0]
    assert "against the profile's 1751 over 65452" in reasons[0]


def test_the_bundle_it_writes_passes_its_own_verify(written: Path, tmp_path: Path) -> None:
    result = run(
        written,
        state_dir=tmp_path / "state",
        profile=FIXTURE / "profile.json",
        verify=True,
    )
    assert result.exit_code == 0, result.stderr
    document = result.artifacts["verify"]["exposure"]
    assert document["met"] is True
    assert {line["line"] for line in document["lines"]} == {
        "exposure:subscription_to_customer",
        "exposure:order_to_customer",
    }
    assert all(line["outcome"] == "within" for line in document["lines"])


_COUNTS = ("rowCount", "k", "binCounts", "levelCounts", "value")


def _times(document: Any, factor: int, parent: Optional[str] = None) -> Any:
    """*document* with every count a profile publishes multiplied by *factor*."""
    if isinstance(document, dict):
        return {
            key: (
                value * factor
                if key in ("min", "max") and parent == "sampleSize"
                else _times(value, factor, key)
            )
            for key, value in document.items()
        }
    if isinstance(document, list):
        return [_times(item, factor, parent) for item in document]
    if isinstance(document, int) and not isinstance(document, bool) and parent in _COUNTS:
        return document * factor
    return document


@pytest.mark.parametrize("rows", [6001, 450_000, 45_000_000])
def test_the_trial_scale_is_a_plain_decimal_run_accepts_and_holds_the_cap(rows: int) -> None:
    simulation = {"extensions": {NAMESPACE: {"emits": {"a": {"rows": rows}}}}}
    scale = authoring._trial_scale(simulation)
    assert scale is not None
    chosen = parse_scale(scale)
    assert 0 < chosen * rows <= authoring._MOST_TRIAL_ROWS
    assert len(Decimal(scale).as_tuple().digits) <= 18
    assert chosen * rows > authoring._MOST_TRIAL_ROWS * Decimal("0.99")


def test_the_trial_scale_is_none_for_a_bundle_the_cap_holds() -> None:
    simulation = {"extensions": {NAMESPACE: {"emits": {"a": {"rows": 6000}}}}}
    assert authoring._trial_scale(simulation) is None


def test_a_bundle_over_the_trial_cap_leaves_out_the_exposure_its_trial_fails(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile.json"
    original = json.loads((FIXTURE / "profile.json").read_text(encoding="utf-8"))
    scaled = _times(original, 100)
    scaled["encoding"] = original["encoding"]
    profile.write_text(json.dumps(scaled), encoding="utf-8")
    directory = tmp_path / "bundle"
    result = run_init(
        str(directory),
        contract=str(FIXTURE / "contract.fluid.yaml"),
        profile=str(profile),
    )
    assert result.exit_code == 0, result.stderr
    block = _block(directory)
    within = {entry["relationship"]: entry for entry in block["within"]}
    assert "exposure" not in within["invoice_to_subscription"]
    reasons = [
        item["reason"]
        for item in block["unused"]
        if item["statistic"]["pointer"] == "/relationships/1/exposure/0"
    ]
    assert reasons
    assert "children per active day" in reasons[0]
    assert "at scale 0." in reasons[0]


def _declining(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> Dict[Any, str]:
    stage = sys.modules["flux_engine.stages.run"]

    def trial(*args: Any, **kwargs: Any) -> StageResult:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(stage, "run", trial)
    with pytest.raises(authoring._ExposuresDeclined) as declined:
        authoring._refuse_unless_reproduced(
            [], [{"emit": "invoice", "relationship": "rel", "exposure": True}], [], None, Path("p")
        )
    return dict(declined.value.reasons)


def test_a_trial_that_cannot_be_made_leaves_the_exposure_out_and_names_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = StageResult(
        stage="run",
        exit_code=1,
        stderr="FLX-3405: 'x' is not a plain decimal\nmore",
        error_code="FLX-3405",
    )
    reasons = _declining(monkeypatch, refused)
    assert "could not be made (FLX-3405: 'x' is not a plain decimal)" in reasons[("invoice", "rel")]
    assert "is not declared" in reasons[("invoice", "rel")]


def test_a_trial_that_raises_leaves_the_exposure_out_and_names_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasons = _declining(monkeypatch, FluxError(FLX_3405, "no good scale"))
    assert "could not be made (FLX-3405: " in reasons[("invoice", "rel")]


def test_a_trial_that_forms_no_line_for_the_exposure_leaves_it_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = StageResult(
        stage="run", exit_code=0, _artifacts={"verify": {"exposure": {"lines": []}}}
    )
    reasons = _declining(monkeypatch, empty)
    assert "formed no line for this exposure" in reasons[("invoice", "rel")]
