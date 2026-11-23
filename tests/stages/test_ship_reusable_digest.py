# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``ship`` checks ``G`` the way ``plan`` recorded it, whatever the bundle declares.

The reusable digest is blind to the ``facts``, ``links``, ``bridges``, ``counts``, ``copies`` and
``overlay`` members of the Flux extension. The compiler once stripped only the fidelity block before taking
``G``, so ship's recompile disagreed with plan.json for every bundle that declared one of them and
refused it with FLX-8001 (``contract.plannedG``). Each case here runs plan, apply, verify and ship.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify
from tests.golden.test_shared_copies_golden import _bundle as _copies_bundle
from tests.overlays import (
    DOUBLE_YAML,
    ORDERS_MEAN_PROFILE,
    cite_rows_from,
    declare_overlay,
    init_orders,
)

from .conftest import FIXTURE_BUNDLE, REPO_ROOT
from .test_bridges_run import _bundle as _bridges_bundle
from .test_conditional_links_run import _with_links
from .test_rules_run import _bundle as _rules_bundle

pytestmark = pytest.mark.integration

FIXTURES = REPO_ROOT / "tests" / "fixtures"


def _counts(root: Path) -> Path:
    target = root / "counts"
    shutil.copytree(FIXTURES / "counts-invoices", target)
    return target


def _copies(root: Path) -> Path:
    return _copies_bundle(root, copies=True)


def _links(root: Path) -> Path:
    return _with_links(root, "links")


def _bridges(root: Path) -> Path:
    return _bridges_bundle(root, "bridges")


def _overlay(root: Path) -> Path:
    bundle = declare_overlay(init_orders(root / "overlay", ORDERS_MEAN_PROFILE), DOUBLE_YAML)
    return cite_rows_from(bundle, "double-orders")


_BEFORE_2000 = '        - id: before_2000\n          check: "d_year < 2000"\n'
_FACTS = '    facts:\n      - { emit: date_dim, rule: year_of_date, tolerance: "0.03" }\n'
_NAMESPACE = "  com.agentics.flux/1:\n"


def _facts(root: Path) -> Path:
    bundle = _rules_bundle(root, name="facts", invariants=_BEFORE_2000)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    simulation.write_text(text.replace(_NAMESPACE, _NAMESPACE + _FACTS, 1), encoding="utf-8")
    return bundle


#: One bundle per extension member ``G`` is blind to, each declaring it.
CASES = {
    "counts": _counts,
    "copies": _copies,
    "links": _links,
    "bridges": _bridges,
    "facts": _facts,
    "overlay": _overlay,
}

#: The profile a member's bundle is read with, where it needs one.
PROFILES = {"overlay": ORDERS_MEAN_PROFILE}

#: Members whose bundle is shipped without a committed slice: committing the 600-row orders table
#: to a Merkle state takes minutes, and the plan's ``G`` is recompiled and checked either way.
UNSLICED = {"overlay"}


def _slices(layout: RunLayout, tmp_path: Path) -> Dict[str, str]:
    slices: Dict[str, str] = {}
    for expose_id in json.loads(layout.plan_path.read_text(encoding="utf-8"))["emits"]:
        attestation = json.loads(
            layout.seam_attestation_path(expose_id).read_text(encoding="utf-8")
        )
        rows = [
            dict(zip(attestation["columns"], json.loads(line), strict=True))
            for line in layout.emit_path(expose_id).read_text(encoding="utf-8").splitlines()
            if line
        ]
        path = tmp_path / f"{expose_id}.ndjson"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        slices[expose_id] = f"file://{path}"
    return slices


@pytest.mark.parametrize("member", sorted(CASES))
def test_ship_accepts_a_bundle_declaring_the_member(member: str, tmp_path: Path) -> None:
    bundle = CASES[member](tmp_path)
    state_dir = tmp_path / ".flux"
    profile = PROFILES.get(member)
    applied = apply(bundle, state_dir=state_dir, profile=profile)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state_dir, str(applied.artifacts["run_id"]))
    verified = verify(bundle, run_id=layout.run_id, state_dir=state_dir, profile=profile)
    assert verified.exit_code in (0, 1), verified.stderr

    result = ship(
        bundle,
        run_id=layout.run_id,
        out_dir=tmp_path / "dist",
        state_dir=state_dir,
        committed_slice=None if member in UNSLICED else _slices(layout, tmp_path),
        profile=profile,
    )
    assert result.success and result.error_code is None, result.stderr

    failed = [
        (expose_id, check["check"])
        for expose_id, decision in result.artifacts.get("decisions", {}).items()
        for check in decision["checks"]
        if not check["passed"] and check["check"] == "contract.plannedG"
    ]
    assert not failed, failed


#: Taken from the plan of ``telco-fidelity-small`` with the code before the compiler shared the
#: seam's exclusion, so a bundle without the members keeps the ``G`` it always had.
PLAIN_G = "sha256:eee5c8c8c595a15e70d72360de2f422c4490594c1de4e1aade2e9ccbaed65cfb"


def test_g_of_a_bundle_without_the_members_is_unchanged(tmp_path: Path) -> None:
    applied = apply(FIXTURE_BUNDLE, state_dir=tmp_path / ".flux")
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(tmp_path / ".flux", str(applied.artifacts["run_id"]))
    emits = json.loads(layout.plan_path.read_text(encoding="utf-8"))["emits"]
    assert emits["payment_recovery_moment"]["G"] == PLAIN_G


#: The plan digest the facts bundle has on origin/main and on 0.9.0 (whose run id is its prefix):
#: ``ship`` now compares the plan's ``G`` with the plan's own function, so nothing a plan records
#: moves for a bundle that declares ``facts``.
FACTS_PLAN_DIGEST = "sha256:23377bc023112320f9ab1cd4e6f59d1e35d1c5298982f8decdce2140e1f60c00"


def test_the_plan_of_a_facts_bundle_is_the_one_it_had(tmp_path: Path) -> None:
    applied = apply(_facts(tmp_path), state_dir=tmp_path / ".flux")
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(tmp_path / ".flux", str(applied.artifacts["run_id"]))
    plan = json.loads(layout.plan_path.read_text(encoding="utf-8"))
    assert plan["planDigest"] == FACTS_PLAN_DIGEST


#: A run of a bundle that declares ``facts``, planned and applied by flux-sim-engine 0.9.0 and kept
#: as it wrote it: its committed states are bound to the compiler's ``G``, which still covers
#: ``facts``, and its plan.json to the seam's, which does not.
RUN_0_9_0 = FIXTURES / "facts-run-0.9.0"


def test_a_run_0_9_0_planned_and_applied_verifies_and_ships(tmp_path: Path) -> None:
    shutil.copytree(RUN_0_9_0, tmp_path / "run")
    bundle, state_dir = tmp_path / "run" / "bundle", tmp_path / "run" / "state"
    (run_id,) = [entry.name for entry in (state_dir / "runs").iterdir()]
    layout = RunLayout.for_state_dir(state_dir, run_id)
    verified = verify(bundle, run_id=run_id, state_dir=state_dir)
    assert verified.exit_code == 0, verified.stderr

    result = ship(
        bundle,
        run_id=run_id,
        out_dir=tmp_path / "dist",
        state_dir=state_dir,
        committed_slice=_slices(layout, tmp_path),
    )

    assert result.exit_code == 0, result.stderr


def _fixture(name: str):
    def build(root: Path) -> Path:
        target = root / name
        shutil.copytree(FIXTURES / name, target)
        return target

    return build


#: The history keys and what each ships: a bundle and the emits its own key governs.
HISTORY_CASES = {
    "history": ("history-chain", ("subscription_version",)),
    "asOf": ("history-asof", ("invoice",)),
    "snapshots": ("history-projections", ("subscription_current",)),
    "events": ("history-projections", ("subscription_change",)),
    "periods": ("history-projections", ("subscription_month",)),
    "all": (
        "history-window",
        ("subscription_version", "invoice", "subscription_current", "subscription_change"),
    ),
}


#: A bundle applied, verified and shipped once, however many of the cases above name it: the three
#: projection cases ship the same ``history-projections`` bundle and differ only in which emit they check.
_SHIPPED: Dict[str, Tuple[RunLayout, Any]] = {}


def _shipped(name: str, factory: pytest.TempPathFactory) -> Tuple[RunLayout, Any]:
    if name not in _SHIPPED:
        root = factory.mktemp(name)
        bundle = _fixture(name)(root)
        state_dir = root / ".flux"
        applied = apply(bundle, state_dir=state_dir)
        assert applied.success, applied.stderr
        layout = RunLayout.for_state_dir(state_dir, str(applied.artifacts["run_id"]))
        verified = verify(bundle, run_id=layout.run_id, state_dir=state_dir)
        assert verified.exit_code in (0, 1), verified.stderr

        result = ship(
            bundle,
            run_id=layout.run_id,
            out_dir=root / "dist",
            state_dir=state_dir,
            committed_slice=_slices(layout, root),
        )
        _SHIPPED[name] = (layout, result)
    return _SHIPPED[name]


@pytest.mark.parametrize("key", sorted(HISTORY_CASES))
def test_ship_ships_a_history_and_its_projections(
    key: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    name, emits = HISTORY_CASES[key]
    layout, result = _shipped(name, tmp_path_factory)

    assert result.success, (result.stderr, result.artifacts)
    assert (layout.run_dir / "EVIDENCE.md").is_file()
    decisions = result.artifacts.get("decisions", {})
    for expose_id in emits:
        assert decisions[expose_id]["action"] == "emit", decisions[expose_id]
