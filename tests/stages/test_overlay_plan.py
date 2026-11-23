# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation's profile overlay through ``plan`` (ADR 0030 section 7).

Pinned here, on a bundle ``fluxsim init`` wrote for the orders fixture:

* a declared overlay is recorded in ``plan.json`` as ``{name, patch}`` and moves the plan's digest,
  and a Simulation that declares none plans to the bytes it had;
* ``plan`` reads the overlaid profile with flux-stats' reader, so a patch that makes a document the
  reader refuses, or names a table the profile does not hold, is refused (``FLX-3105``) before
  anything is written;
* the privacy grade reads the pinned, unpatched profile: an overlay never raises or lowers it;
* kernel 0.1.0 refuses the key, naming the kernel, and ``validate`` reads the block's shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.cli._init import run_init
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from tests.overlays import (
    DOUBLE_ORDERS,
    DOUBLE_YAML,
    ORDERS_PROFILE,
    cite_rows_from,
    declare_overlay,
    init_orders,
)

pytestmark = pytest.mark.integration


def _plan(bundle: Path, state: Path, **options: Any) -> Any:
    return plan(bundle, state_dir=state, profile=ORDERS_PROFILE, **options)


def _document(state: Path, planned: Any) -> Dict[str, Any]:
    path = state / "runs" / planned.artifacts["run_id"] / "plan.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    root = tmp_path_factory.mktemp("plain")
    bundle = init_orders(root / "bundle")
    planned = _plan(bundle, root / "state")
    assert planned.success, planned.stderr
    return {"bundle": bundle, "document": _document(root / "state", planned)}


def test_a_simulation_that_declares_no_overlay_records_none(plain: Dict[str, Any]) -> None:
    assert "overlay" not in plain["document"]
    assert plain["document"]["privacyGrade"]


def test_a_declared_overlay_is_recorded_in_the_plan(tmp_path: Path, plain: Dict[str, Any]) -> None:
    # The number the overlay changes cites the overlay: a `profile` citation of it is refused.
    bundle = cite_rows_from(
        declare_overlay(init_orders(tmp_path / "bundle"), DOUBLE_YAML), "double-orders"
    )
    planned = _plan(bundle, tmp_path / "state")
    assert planned.success, planned.stderr
    document = _document(tmp_path / "state", planned)
    assert document["overlay"] == DOUBLE_ORDERS
    assert document["planDigest"] != plain["document"]["planDigest"]
    # The overlay changes nothing else the plan records, apart from the digests the Simulation moves.
    for key in ("profileDigest", "kernelVersion", "seedTreeVersion"):
        assert document[key] == plain["document"][key]


PRIVATE = Path(__file__).resolve().parents[1] / "fixtures" / "private-telco"


def _private_bundle(root: Path, overlay: str, *, row_count: bool) -> Path:
    """The private telco bundle: a fully noisy profile (P3), or one with an exact row count (P1)."""
    profile = json.loads((PRIVATE / "telco.profile.json").read_text("utf-8"))
    if row_count:
        profile["tables"]["subscriber"]["rowCount"] = 9195
    contract = (PRIVATE / "telco.fluid.yaml").read_text("utf-8")
    column = "- { name: tier, type: \"enum('gold','silver')\", required: true }"
    contract = contract.replace(column, column.replace(" }", ", sensitivity: pii }"))
    (root / "telco.fluid.yaml").write_text(contract, encoding="utf-8")
    (root / "telco.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(root / "bundle"),
        contract=str(root / "telco.fluid.yaml"),
        profile=str(root / "telco.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return declare_overlay(root / "bundle", overlay)


def test_an_overlay_that_adds_exact_numbers_does_not_lower_the_grade(tmp_path: Path) -> None:
    # The pinned profile is fully noisy, P3, and the tier column is pii. Merged with this patch the
    # document would hold an exact row count, which grades P1 and would refuse the run (FLX-4005).
    # The grade reads the pinned profile, so the run plans and is recorded P3.
    bundle = _private_bundle(
        tmp_path,
        "overlay:\n  name: exact-rows\n  patch:\n    tables:\n      subscriber: {rowCount: 9195}\n",
        row_count=False,
    )
    planned = plan(bundle, state_dir=tmp_path / "state", profile=tmp_path / "telco.profile.json")
    assert planned.exit_code == 0, planned.stderr
    assert planned.artifacts["plan_document"]["privacyGrade"] == "P3"


def test_an_overlay_that_removes_exact_numbers_does_not_raise_the_grade(tmp_path: Path) -> None:
    # The pinned profile holds an exact row count, P1, and the tier column is pii: refused. An
    # overlay that removes the row count would make a P3 document, and does not clear the floor.
    bundle = _private_bundle(
        tmp_path,
        "overlay:\n  name: no-rows\n  patch:\n    tables:\n      subscriber: {rowCount: null}\n",
        row_count=True,
    )
    # The row count is the number the overlay removes, so the register does not cite it as the
    # profile's.
    simulation = bundle / "simulation.flux.yml"
    cited = (
        "      source: profile\n      statistic:\n        pointer: /tables/subscriber/rowCount\n"
    )
    text = simulation.read_text(encoding="utf-8")
    assert text.count(cited) == 1
    simulation.write_text(
        text.replace(cited, "      source: assumption\n      reason: The size of the scenario.\n"),
        encoding="utf-8",
    )
    planned = plan(bundle, state_dir=tmp_path / "state", profile=tmp_path / "telco.profile.json")
    assert (planned.exit_code, planned.error_code) == (1, "FLX-4005")
    assert "the run's grade is P1" in planned.stderr
    assert not list((tmp_path / "state").glob("runs/*"))


@pytest.mark.parametrize(
    ("patch", "problem"),
    [
        ("tables: {orders: {rowCount: many}}", "makes a profile that is refused"),
        ("tables: {shipments: {rowCount: 1}}", "table 'shipments', which the pinned profile"),
        (
            "tables: {orders: {columns: {weight: {metrics: {}}}}}",
            "column 'weight' of table 'orders'",
        ),
        ("relationships: {parent: orders}", "makes a profile that is refused"),
    ],
)
def test_a_patch_that_makes_no_profile_is_refused_before_anything_is_written(
    tmp_path: Path, patch: str, problem: str
) -> None:
    bundle = declare_overlay(
        init_orders(tmp_path / "bundle"), f"overlay:\n  name: x\n  patch:\n    {patch}\n"
    )
    planned = _plan(bundle, tmp_path / "state")
    assert (planned.exit_code, planned.error_code) == (1, "FLX-3105")
    assert problem in planned.stderr
    assert not list((tmp_path / "state").glob("runs/*/plan.json"))


def test_a_metrics_key_of_a_patch_reaches_the_message_quoted(tmp_path: Path) -> None:
    key = "a\\n\\x1b[31mFLX-9999: forged\\x1b[0m"
    bundle = declare_overlay(
        init_orders(tmp_path / "bundle"),
        "overlay:\n  name: x\n  patch:\n    tables:\n      orders:\n        columns:\n"
        f'          amount: {{metrics: {{"{key}": 1}}}}\n',
    )
    planned = _plan(bundle, tmp_path / "state")
    assert (planned.exit_code, planned.error_code) == (1, "FLX-3105")
    assert "makes a profile that is refused" in planned.stderr
    assert "\x1b" not in planned.stderr
    assert "\nFLX-9999" not in planned.stderr
    assert "\\x1b[31mFLX-9999" in planned.stderr


def test_a_patch_that_touches_a_forbidden_member_is_refused_by_validate_and_plan(
    tmp_path: Path,
) -> None:
    bundle = declare_overlay(
        init_orders(tmp_path / "bundle"),
        "overlay:\n  name: x\n  patch:\n    encoding: {fractionalBits: 8}\n",
    )
    assert validate(bundle).exit_code != 0
    planned = _plan(bundle, tmp_path / "state")
    assert (planned.exit_code, planned.error_code) == (1, "FLX-3105")
    assert "touches 'encoding'" in planned.stderr


def test_kernel_0_1_0_refuses_the_key_and_names_the_kernel(tmp_path: Path) -> None:
    bundle = declare_overlay(init_orders(tmp_path / "bundle"), DOUBLE_YAML)
    planned = _plan(bundle, tmp_path / "state", kernel="0.1.0")
    assert (planned.exit_code, planned.error_code) == (1, "FLX-3105")
    assert "overlay is declared" in planned.stderr and "kernel 0.1.0" in planned.stderr
