# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bundle that declares one of the namespace keys ``consumers``, ``conditions``, ``responses`` or ``forecasts`` ships.

``ship`` takes the reusable digest (``G``) again, with the function ``plan`` took it with, and
compares it with the one ``plan`` recorded; a key that enters ``G`` one way at plan and another at
ship withholds the bundle with ``FLX-8001`` naming ``contract.plannedG``. Each test is the
smallest bundle that declares its key, run through the command line as ``plan``, ``apply``,
``verify`` and ``ship``, and asserts that ship exits 0, writes ``EVIDENCE.md``, and that the
``G`` the plan recorded is the ``G`` the ship took again.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from flux_engine.cli import main
from flux_engine.stages.layout import RunLayout
from tests.outcomes.support import PROFILE as OUTCOMES_PROFILE
from tests.outcomes.support import make_bundle, namespace
from tests.seam.test_conditions_block import TWO_GROUPS, bundle_with

pytestmark = pytest.mark.integration

ship_module = importlib.import_module("flux_engine.stages.ship")

CONSUMERS_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "consumers"


def _cli(*arguments: str) -> Dict[str, Any]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([*arguments, "--json"])
    assert code == 0, (arguments[:2], err.getvalue(), out.getvalue())
    document: Dict[str, Any] = json.loads(out.getvalue())
    return document


def _slice_of(layout: RunLayout, expose: str, target: Path) -> str:
    columns = json.loads(layout.seam_attestation_path(expose).read_text("utf-8"))["columns"]
    rows = [
        dict(zip(columns, json.loads(line), strict=True))
        for line in layout.emit_path(expose).read_text("utf-8").splitlines()
        if line
    ]
    target.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return target.as_uri()


def _shipped_with_the_planned_g(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bundle: Path,
    *,
    profile: Optional[Path] = None,
    slice_of: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """Plan, apply, verify and ship *bundle*; the ``(planned G, G at ship)`` of every emit."""
    seen: List[Tuple[str, str]] = []
    real = ship_module._binding_checks

    def watching(
        directory: Path,
        compiled: Any,
        contract_document: Any,
        attestation: Dict[str, Any],
        planned: Dict[str, Any],
        planned_g_now: str,
    ) -> Any:
        seen.append((str(planned.get("G")), planned_g_now))
        return real(directory, compiled, contract_document, attestation, planned, planned_g_now)

    monkeypatch.setattr(ship_module, "_binding_checks", watching)
    state = ["--state-dir", str(tmp_path / ".flux")]
    pinned = ["--profile", str(profile)] if profile else []
    planned = _cli("plan", str(bundle), *state, *pinned)
    run_id = str(planned["artifacts"]["run_id"])
    _cli("apply", str(bundle), "--run-id", run_id, *state)
    layout = RunLayout.for_state_dir(tmp_path / ".flux", run_id)
    committed: List[str] = []
    if slice_of:
        uri = _slice_of(layout, slice_of, tmp_path / "slice.ndjson")
        committed = ["--committed-slice", f"{slice_of}={uri}"]
    _cli("verify", str(bundle), "--run-id", run_id, *state, *pinned, *committed)
    shipped = _cli(
        "ship",
        str(bundle),
        "--run-id",
        run_id,
        *state,
        *pinned,
        *committed,
        "-o",
        str(tmp_path / "out"),
    )
    assert shipped["ok"]
    assert (layout.run_dir / "EVIDENCE.md").is_file()
    return seen


def _assert_g_is_the_one_planned(seen: List[Tuple[str, str]]) -> None:
    assert seen, "ship checked no emit"
    for planned_g, shipped_g in seen:
        assert planned_g.startswith("sha256:")
        assert shipped_g == planned_g


def _declared(bundle: Path) -> Dict[str, Any]:
    document = yaml.safe_load((bundle / "simulation.flux.yml").read_text(encoding="utf-8"))
    return namespace(document)


def test_a_bundle_with_consumers_ships_with_the_g_it_planned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    shutil.copytree(CONSUMERS_FIXTURE / "bundle", bundle)
    assert "consumers" in _declared(bundle)
    seen = _shipped_with_the_planned_g(
        tmp_path, monkeypatch, bundle, profile=CONSUMERS_FIXTURE / "subscriptions.profile.json"
    )
    _assert_g_is_the_one_planned(seen)


def test_a_bundle_with_conditions_ships_with_the_g_it_planned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = bundle_with(tmp_path, TWO_GROUPS)
    assert "conditions" in _declared(bundle)
    seen = _shipped_with_the_planned_g(tmp_path, monkeypatch, bundle, slice_of="subscriptions")
    _assert_g_is_the_one_planned(seen)


def test_a_bundle_with_responses_ships_with_the_g_it_planned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``responses`` alone: the fixture's ``forecasts``, which read them, are dropped."""
    bundle = make_bundle(
        tmp_path / "responses", "bundle", simulation=lambda d: namespace(d).pop("forecasts")
    )
    declared = _declared(bundle)
    assert "responses" in declared and "forecasts" not in declared
    seen = _shipped_with_the_planned_g(tmp_path, monkeypatch, bundle, profile=OUTCOMES_PROFILE)
    _assert_g_is_the_one_planned(seen)


def test_a_bundle_with_forecasts_ships_with_the_g_it_planned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``forecasts`` forecast the families ``responses`` declares, so the bundle declares both."""
    bundle = make_bundle(tmp_path / "forecasts", "bundle")
    declared = _declared(bundle)
    assert "forecasts" in declared and "responses" in declared
    seen = _shipped_with_the_planned_g(tmp_path, monkeypatch, bundle, profile=OUTCOMES_PROFILE)
    _assert_g_is_the_one_planned(seen)
