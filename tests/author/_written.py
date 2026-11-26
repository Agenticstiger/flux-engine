# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a test reads back from the bundle ``fluxsim init`` wrote under ``tmp_path / "bundle"``."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict

from flux_engine.seam.resolve import load_bundle, resolve_all_emits
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from flux_engine.stages.verify import verify
from tests.bundles import by_file


def bundle_digests(tmp_path: Path) -> Dict[str, str]:
    """The sha256 of each file the bundle holds, by file name."""
    return by_file(tmp_path / "bundle", lambda path: hashlib.sha256(path.read_bytes()).hexdigest())


def resolved_emits(tmp_path: Path) -> Dict[str, Any]:
    """The bundle's emits as the seam resolves them, once ``validate`` accepts the bundle."""
    bundle = tmp_path / "bundle"
    assert validate(bundle).exit_code == 0
    loaded = load_bundle(bundle)
    (simulation_id,) = loaded.simulation_ids()
    return {emit.expose_id: emit for emit in resolve_all_emits(loaded, simulation_id=simulation_id)}


def plan_written(tmp_path: Path, profile: str = "plans.profile.json") -> None:
    """The bundle plans under kernel 0.2.0 against the profile ``init`` wrote beside it."""
    outcome = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / profile,
        kernel="0.2.0",
        stage=False,
    )
    assert outcome.success, outcome.stderr


def applied_and_verified(tmp_path: Path, profile: str = "plans.profile.json") -> None:
    """The bundle validates, plans under kernel 0.2.0, applies and verifies."""
    bundle, state = tmp_path / "bundle", tmp_path / "state"
    assert validate(bundle).exit_code == 0
    planned = plan(bundle, state_dir=state, profile=tmp_path / profile, kernel="0.2.0", stage=False)
    assert planned.success, planned.stderr
    run_id = str(planned.artifacts["run_id"])
    applied = apply(bundle, state_dir=state, run_id=run_id)
    assert applied.success, applied.stderr
    checked = verify(bundle, state_dir=state, run_id=run_id)
    assert checked.success, checked.stdout + checked.stderr


def assert_state(result: Any, pointer: str, state: str, *notes: str) -> None:
    """The ledger holds ``pointer`` in ``state``, its note saying each of ``notes``."""
    entry = {item["pointer"]: item for item in result.artifacts["ledger"]["statistics"]}[pointer]
    assert entry["state"] == state
    for note in notes:
        assert note in entry["note"]
