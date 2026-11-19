# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation pins a profile by digest under the namespace's top-level ``profile`` key.

The namespace is closed to the keys this engine reads, ``emits``, ``parameters``,
``profile``, ``scorecard`` and ``strictCoverage``; a pin is a mapping holding exactly
``digest``. The plan pairs
the pin with the profile it is handed: a pin with no profile, a profile with
no pin (``FLX-3109``) and a profile whose digest is not the pinned one
(``FLX-3110``) are refused before anything is written.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.cli import main
from flux_engine.core.errors import FluxError
from flux_engine.seam import resolve as resolve_module
from flux_engine.seam.profile import load_profile, require_profile
from flux_engine.seam.resolve import load_bundle, profile_reference, resolve_emit
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_stats.profile import SCHEMA_URL

from .conftest import minimal_simulation

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"
DIGEST = "sha256:" + "0" * 64


def _profile(path: Path, producer: str = "urn:example:pin-test") -> Path:
    document = {
        "_producer": producer,
        "_schemaURL": SCHEMA_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {},
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def _simulation(namespace: Any) -> Dict[str, Any]:
    return {"extensions": {resolve_module.EXTENSION_NAMESPACE: namespace}}


def test_a_simulation_pins_a_profile_by_its_digest() -> None:
    assert profile_reference(_simulation({"profile": {"digest": DIGEST}})) == DIGEST
    assert profile_reference(_simulation({"emits": {}})) is None
    assert profile_reference(_simulation({"profile": None})) is None
    assert profile_reference({}) is None


@pytest.mark.parametrize(
    "pin",
    [
        "sha256:" + "0" * 64,
        {"digest": "sha256:" + "0" * 63},
        {"digest": "sha256:" + "A" * 64},
        {"digest": "sha512:" + "0" * 64},
        {"digest": DIGEST, "path": "elsewhere.json"},
        {},
    ],
)
def test_a_malformed_pin_is_refused(pin: Any) -> None:
    with pytest.raises(FluxError) as raised:
        profile_reference(_simulation({"profile": pin}), where="sim-x")
    assert raised.value.code.code == "FLX-3105"
    assert raised.value.detail.startswith("sim-x: extensions['com.agentics.flux/1'].profile")


def test_the_namespace_carries_its_six_keys_and_nothing_else(write_bundle: Any) -> None:
    documents = minimal_simulation()
    documents[1]["extensions"] = {
        resolve_module.EXTENSION_NAMESPACE: {"profile": {"digest": DIGEST}, "profiles": {}}
    }
    root = write_bundle(flux_documents=documents)
    with pytest.raises(FluxError) as raised:
        resolve_emit(load_bundle(root), simulation_id="sim-widget")
    assert raised.value.code.code == "FLX-3105"
    assert (
        "declares ['profiles']; this namespace carries "
        "['emits', 'parameters', 'profile', 'scorecard', 'strictCoverage', 'unused']"
        in raised.value.detail
    )
    documents[1]["extensions"] = {
        resolve_module.EXTENSION_NAMESPACE: {"profile": {"digest": DIGEST}}
    }
    resolved = resolve_emit(
        load_bundle(write_bundle(flux_documents=documents)), simulation_id="sim-widget"
    )
    assert resolved.extension is None


def test_the_pin_and_the_profile_pair_or_are_refused(tmp_path: Path) -> None:
    pinned = load_profile(_profile(tmp_path / "pinned.json"))
    other = _profile(tmp_path / "other.json", producer="urn:example:another")
    assert require_profile(None, None, where="sim") is None
    paired = require_profile(pinned.digest.value, tmp_path / "pinned.json", where="sim")
    assert paired is not None and paired.digest == pinned.digest
    for pin, supplied, code in (
        (pinned.digest.value, None, "FLX-3109"),
        (None, tmp_path / "pinned.json", "FLX-3109"),
        (pinned.digest.value, other, "FLX-3110"),
        (pinned.digest.value, tmp_path / "absent.json", "FLX-3109"),
    ):
        with pytest.raises(FluxError) as raised:
            require_profile(pin, supplied, where="sim")
        assert raised.value.code.code == code


def test_reindenting_a_profile_keeps_its_digest(tmp_path: Path) -> None:
    pinned = load_profile(_profile(tmp_path / "pinned.json"))
    compact = tmp_path / "compact.json"
    compact.write_text(
        json.dumps(json.loads((tmp_path / "pinned.json").read_text()), separators=(",", ":")),
        encoding="utf-8",
    )
    assert load_profile(compact).digest == pinned.digest


def _pinned_bundle(tmp_path: Path, digest: str) -> Path:
    bundle = tmp_path / "bundle"
    shutil.copytree(SMALL, bundle)
    (bundle / "flux-manifest.json").unlink(missing_ok=True)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    anchor = "  com.agentics.flux/1:\n    emits:\n"
    assert text.count(anchor) == 1
    simulation.write_text(
        text.replace(
            anchor, f"  com.agentics.flux/1:\n    profile:\n      digest: {digest}\n    emits:\n"
        ),
        encoding="utf-8",
    )
    return bundle


def test_plan_refuses_a_missing_or_mismatched_profile_and_writes_nothing(tmp_path: Path) -> None:
    pinned = load_profile(_profile(tmp_path / "pinned.json"))
    other = _profile(tmp_path / "other.json", producer="urn:example:another")
    bundle = _pinned_bundle(tmp_path, pinned.digest.value)
    state = tmp_path / "state"
    for supplied, code in ((None, "FLX-3109"), (other, "FLX-3110")):
        refused = plan(bundle, state_dir=state, profile=supplied)
        assert refused.exit_code == 1 and refused.error_code == code, refused.stderr
    assert not (state / "runs").exists(), "a refused plan writes no run directory"
    planned = plan(bundle, state_dir=state, profile=tmp_path / "pinned.json")
    assert planned.success, planned.stderr
    assert planned.artifacts["profile_digest"] == pinned.digest.value
    unpinned = plan(SMALL, state_dir=tmp_path / "other-state", profile=tmp_path / "pinned.json")
    assert unpinned.error_code == "FLX-3109"


def test_apply_pairs_the_profile_when_it_takes_a_fresh_plan(tmp_path: Path) -> None:
    pinned = load_profile(_profile(tmp_path / "pinned.json"))
    bundle = _pinned_bundle(tmp_path, pinned.digest.value)
    refused = apply(bundle, state_dir=tmp_path / "state")
    assert refused.error_code == "FLX-3109", refused.stderr
    applied = apply(bundle, state_dir=tmp_path / "state", profile=tmp_path / "pinned.json")
    assert applied.success, applied.stderr


def test_apply_refuses_a_profile_with_a_recorded_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pinned = load_profile(_profile(tmp_path / "pinned.json"))
    bundle = _pinned_bundle(tmp_path, pinned.digest.value)
    state = tmp_path / "state"
    planned = plan(bundle, state_dir=state, profile=tmp_path / "pinned.json")
    assert planned.success, planned.stderr
    run_id = planned.artifacts["run_id"]
    before = sorted(path.name for path in (state / "runs" / run_id).iterdir())
    refused = apply(bundle, run_id=run_id, state_dir=state, profile=tmp_path / "pinned.json")
    assert refused.exit_code == 2 and refused.error_code is None
    assert "--profile" in refused.stderr and "--run-id" in refused.stderr
    assert sorted(path.name for path in (state / "runs" / run_id).iterdir()) == before
    command = ["apply", str(bundle), "--run-id", run_id, "--state-dir", str(state)]
    assert main([*command, "--profile", str(tmp_path / "pinned.json")]) == 2
    assert refused.stderr in capsys.readouterr().err
    applied = apply(bundle, run_id=run_id, state_dir=state)
    assert applied.success, applied.stderr


def test_a_bundle_that_pins_no_profile_plans_exactly_as_before(tmp_path: Path) -> None:
    result = plan(SMALL, state_dir=tmp_path / "state")
    assert result.success and "profile_digest" not in result.artifacts
