# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's emit refuses a date outside ``timeBounds`` (ADR 0017 section 11); other emits warn."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pytest
import yaml

from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.seam.resolve import load_documents_exact
from flux_engine.stages import pipeline
from flux_engine.stages.validate import validate
from tests.seam.test_history import PRODUCT, bundle_with, entry_with

pytestmark = pytest.mark.unit

VERSIONED = Path(__file__).resolve().parents[1] / "fixtures" / "versioned"
OUTSIDE = {"range": {"min": "2026-01-01", "max": "2026-09-30"}}
BINNED = {
    "range": {"min": "2026-07-01", "max": "2026-12-31"},
    "bins": ["2026-07-01", "2026-10-01", "2026-12-31"],
    "binWeights": ["0.5", "0.5"],
}


def _both(bundle: Path) -> Dict[str, Any]:
    """What ``validate`` and ``plan`` each say of *bundle*."""
    result = validate(bundle)
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    return {"validate": result, "plan": caught.value}


def _refused(
    tmp_path: Path, columns: Mapping[str, Any], entry: Dict[str, Any], fragment: str
) -> None:
    said = _both(bundle_with(tmp_path, "refused", [entry], columns=columns))
    assert said["validate"].exit_code == 1
    assert said["validate"].error_code == "FLX-3403"
    assert said["plan"].code.code == "FLX-3403"
    for text in (said["validate"].stderr, str(said["plan"])):
        assert fragment in text and "history (subscription_version)" in text


def test_a_declared_range_outside_the_window_is_refused(tmp_path: Path) -> None:
    _refused(tmp_path, {"started_on": OUTSIDE}, entry_with(activation=None), "'started_on' leave")


def test_declared_bins_outside_the_window_are_refused(tmp_path: Path) -> None:
    _refused(tmp_path, {"started_on": BINNED}, entry_with(activation=None), "'started_on' leave")


def test_a_timestamp_range_outside_the_window_is_refused(tmp_path: Path) -> None:
    columns = {
        "started_on": {"range": {"min": "2026-07-01T00:00:00Z", "max": "2026-10-02T00:00:00Z"}}
    }
    bundle = bundle_with(tmp_path, "stamp", [_stamped_entry()], columns=columns, stamp="TIMESTAMP")
    result = validate(bundle)
    assert result.error_code == "FLX-3403" and "'started_on' leave" in result.stderr


def _stamped_entry() -> Dict[str, Any]:
    return entry_with(
        activation=None,
        until="2026-09-30T00:00:00Z",
        start={"bins": ["2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z"], "binWeights": ["1"]},
    )


def test_a_range_the_history_sets_is_still_refused_outside_the_window(tmp_path: Path) -> None:
    _refused(tmp_path, {"valid_to": OUTSIDE}, entry_with(), "'valid_to' leave")


def test_a_date_column_with_no_range_is_refused_unless_the_history_sets_it(tmp_path: Path) -> None:
    bare = _bare(tmp_path, "undeclared", entry_with(activation=None))
    result = validate(bare)
    assert result.error_code == "FLX-3403"
    assert "'started_on' declares no range and no bins" in result.stderr
    with pytest.raises(FluxError, match="declares no range and no bins"):
        pipeline.execute(bare, kernel=KERNEL_V2)


def _bare(tmp_path: Path, name: str, entry: Optional[Dict[str, Any]]) -> Path:
    """A bundle whose ``started_on`` declares no range, so the window would fill it."""
    bundle = bundle_with(tmp_path, name, None if entry is None else [entry])
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    emits = document["extensions"]["com.agentics.flux/1"]["emits"]
    del emits["subscription_version"]["fidelity"]["columns"]["started_on"]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return bundle


def _derived(tmp_path: Path, name: str, invariant: Optional[Dict[str, Any]]) -> Path:
    """The bare bundle, its contract declaring *invariant* (re-pinned), when there is one."""
    bundle = _bare(tmp_path, name, entry_with(activation=None))
    if invariant is None:
        return bundle
    contract_path = bundle / "subscriptions.fluid.yml"
    document = yaml.safe_load(contract_path.read_text("utf-8"))
    document["exposes"][1]["contract"]["invariants"] = [invariant]
    contract_path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    (loaded,) = load_documents_exact(contract_path)
    digest = digest_of_document(loaded, subject=f"contractDigest:{PRODUCT}").value
    path = bundle / "simulation.flux.yml"
    simulation = yaml.safe_load(path.read_text("utf-8"))
    for emit in simulation["spec"]["emits"]:
        emit["provenance"]["contractDigest"] = digest
    path.write_text(yaml.safe_dump(simulation, sort_keys=False), "utf-8")
    return bundle


def test_a_derived_date_column_with_no_range_is_accepted(tmp_path: Path) -> None:
    derive = {"id": "started_from_valid", "derive": "started_on", "expr": "valid_from"}
    result = validate(_derived(tmp_path, "derived", derive))
    assert result.error_code != "FLX-3403", result.stderr


def test_a_drawn_date_column_with_no_range_is_still_refused(tmp_path: Path) -> None:
    other = {"id": "seats_are_positive", "check": "seats > 0"}
    result = validate(_derived(tmp_path, "drawn", other))
    assert result.error_code == "FLX-3403"
    assert "'started_on' declares no range and no bins" in result.stderr


# A history that passes these checks still stops at the generator's not-yet-generated refusal
# (FLX-6210), so the two tests below look only for the absence of FLX-3403.


def test_a_date_column_the_history_sets_may_declare_no_range(tmp_path: Path) -> None:
    result = validate(_bare(tmp_path, "activation", entry_with(activation="started_on")))
    assert result.error_code != "FLX-3403", result.stderr


def test_the_open_end_may_lie_outside_the_window(tmp_path: Path) -> None:
    result = validate(bundle_with(tmp_path, "open", [entry_with(openEnd="9999-12-31")]))
    assert result.error_code != "FLX-3403", result.stderr


def test_another_emit_keeps_the_warning(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, "warned", None, columns={"started_on": OUTSIDE})
    result = validate(bundle)
    assert result.exit_code == 0
    warning = (
        "subscription_version.started_on: the declared range or bins leave the run's "
        "timeBounds, so rows can carry dates outside the window the run covers"
    )
    assert result._artifacts["warnings"] == [warning]
    assert f"\nwarning: {warning}" in result.stdout


def test_an_emit_without_a_history_needs_no_declared_date(tmp_path: Path) -> None:
    result = validate(_bare(tmp_path, "plain", None))
    assert result.exit_code == 0, result.stderr
    assert "warnings" not in result._artifacts


def test_a_versioned_fixture_with_no_history_is_unaffected() -> None:
    result = validate(VERSIONED)
    assert result.exit_code == 0, result.stderr
    assert "warnings" not in result._artifacts
