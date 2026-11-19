# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a bundle declares and the run does not generate is named.

Three declarations are accepted and drawn as if they were absent: a relationship's
``sharedColumns``, a ``check`` that determines no column, and a ``check`` that reads
another row through a relationship. Each is reported by ``plan`` (on the console and in the
``declared_not_generated`` artifact), listed by the evidence report under "What Was Not
Proven" (worked out from the bundle, so nothing the plan binds changes), and refused under strict coverage
(``FLX-3305``). Every bundle, with or without them, keeps its plan digest.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.cli import main
from flux_engine.stages.plan import column_provenance_text, plan, plan_with_result
from flux_engine.starter import scaffold
from tests.bundles import repin_contract

from .conftest import REPO_ROOT

pytestmark = pytest.mark.integration

RELATED_STAR = REPO_ROOT / "tests" / "fixtures" / "related-star"
_CONTRACT = "store-star.fluid.yml"
_STARTER_CONTRACT = "payment-recovery.fluid.yml"


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text("utf-8")
    assert text.count(old) == 1, f"{path.name} changed shape: {old!r}"
    path.write_text(text.replace(old, new, 1), "utf-8")


def _related_star(target: Path, *, shared: bool = False, over_link: bool = False) -> Path:
    """The related-star bundle, with the declarations a case asks for added to ``store_sales``."""
    shutil.copytree(RELATED_STAR, target)
    contract, simulation = target / _CONTRACT, target / "simulation.flux.yml"
    if shared:
        _edit(
            contract,
            '        - { name: ss_net_paid, type: "DECIMAL(7,2)", required: true }\n',
            '        - { name: ss_net_paid, type: "DECIMAL(7,2)", required: true }\n'
            '        - { name: i_price, type: "DECIMAL(7,2)", required: false }\n',
        )
        _edit(
            contract,
            "          fromColumns: [ss_item_sk]\n          toColumns: [i_item_sk]\n"
            "          cardinality: many_to_one\n",
            "          fromColumns: [ss_item_sk]\n          toColumns: [i_item_sk]\n"
            "          cardinality: many_to_one\n          sharedColumns: [i_price]\n",
        )
        _edit(
            simulation,
            '            ss_net_paid: { range: { min: "1", max: "500" } }\n',
            '            ss_net_paid: { range: { min: "1", max: "500" } }\n'
            '            i_price: { range: { min: "1", max: "99" } }\n',
        )
    if over_link:
        _edit(
            contract,
            '        - { name: ss_net_paid, type: "DECIMAL(7,2)", required: true }\n',
            '        - { name: ss_net_paid, type: "DECIMAL(7,2)", required: true }\n'
            "      invariants:\n"
            "        - id: sold_in_range\n"
            '          check: "sold_date.d_year >= 1990"\n',
        )
    repin_contract(contract, simulation, "retail.store_star")
    return target


def _starter_with_check(target: Path) -> Path:
    """The starter bundle (a scenario-driven emit) with a ``check`` that generates nothing."""
    scaffold(target)
    # Invariants are a FLUID 0.7.7 field; the starter contract is 0.7.5.
    _edit(target / _STARTER_CONTRACT, 'fluidVersion: "0.7.5"', 'fluidVersion: "0.7.7"')
    _edit(target / "simulation.flux.yml", 'fluidVersion: "^0.7.3"', 'fluidVersion: "0.7.7"')
    _edit(
        target / _STARTER_CONTRACT,
        "    binding:\n",
        '      invariants:\n        - id: positive_amount\n          check: "amount > 0"\n'
        "    binding:\n",
    )
    repin_contract(
        target / _STARTER_CONTRACT,
        target / "simulation.flux.yml",
        "telco.gold.payment_recovery_moment",
    )
    return target


def _recorded(bundle: Path, state: Path) -> Dict[str, Any]:
    """The record ``apply`` would write for *bundle*: the text the plan's digest is taken over."""
    _, executed = plan_with_result(bundle, state_dir=state)
    assert executed is not None
    document: Dict[str, Any] = json.loads(column_provenance_text(executed))
    return document


def test_a_bundle_that_declares_none_says_nothing_and_keeps_its_record(tmp_path: Path) -> None:
    bundle = _related_star(tmp_path / "plain")
    result = plan(bundle, state_dir=tmp_path / "state")

    assert result.exit_code == 0, result.stderr
    assert "declared, not generated" not in result.stdout
    assert "declared_not_generated" not in result.artifacts
    assert "notGenerated" not in _recorded(bundle, tmp_path / "state")


def test_shared_columns_are_reported_by_plan(tmp_path: Path) -> None:
    bundle = _related_star(tmp_path / "shared", shared=True)
    result = plan(bundle, state_dir=tmp_path / "state")

    assert result.exit_code == 0, result.stderr
    assert "declared, not generated: store_sales: 1 declaration" in result.stdout
    assert "semantics.relationships.item.sharedColumns (i_price)" in result.stdout
    assert "fluxsim explain FLX-3305" in result.stdout
    (row,) = result.artifacts["declared_not_generated"]
    assert (row["exposeId"], row["kind"]) == ("store_sales", "shared-columns")
    assert row["locator"] == "semantics.relationships.item.sharedColumns"
    assert row["names"] == ["i_price"]
    assert "notGenerated" not in _recorded(bundle, tmp_path / "state")


def test_a_check_that_determines_no_column_is_reported(tmp_path: Path) -> None:
    result = plan(_starter_with_check(tmp_path / "check"), state_dir=tmp_path / "state")

    assert result.exit_code == 0, result.stderr
    assert "contract.invariants.positive_amount" in result.stdout
    (row,) = result.artifacts["declared_not_generated"]
    assert (row["exposeId"], row["kind"]) == ("payment_recovery_moment", "check")
    assert row["names"] == []


def test_a_check_over_a_link_names_the_link_it_reads(tmp_path: Path) -> None:
    result = plan(_related_star(tmp_path / "link", over_link=True), state_dir=tmp_path / "state")

    assert result.exit_code == 0, result.stderr
    (row,) = result.artifacts["declared_not_generated"]
    assert (row["kind"], row["locator"]) == ("check-over-link", "contract.invariants.sold_in_range")
    assert row["names"] == ["sold_date.d_year"]
    assert "(sold_date.d_year)" in result.stdout


@pytest.mark.parametrize(
    ("make", "locator"),
    [
        (lambda p: _related_star(p, shared=True), "semantics.relationships.item.sharedColumns"),
        (_starter_with_check, "contract.invariants.positive_amount"),
        (lambda p: _related_star(p, over_link=True), "contract.invariants.sold_in_range"),
    ],
    ids=["shared-columns", "check", "check-over-link"],
)
def test_strict_coverage_refuses_each_before_anything_is_written(
    tmp_path: Path, make: Any, locator: str
) -> None:
    result = plan(make(tmp_path / "bundle"), state_dir=tmp_path / "state", strict_coverage=True)

    assert (result.exit_code, result.error_code) == (1, "FLX-3305"), result.stderr
    assert locator in result.stderr
    assert "remove" in result.stderr
    assert "without --strict-coverage this is a report and the run goes on" in result.stderr
    assert not (tmp_path / "state").exists()


def test_the_bundle_can_ask_for_the_refusal_itself(tmp_path: Path) -> None:
    bundle = _starter_with_check(tmp_path / "declared")
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert "extensions:" not in text
    simulation.write_text(
        text + "extensions:\n  com.agentics.flux/1:\n    strictCoverage: true\n", "utf-8"
    )

    result = plan(bundle, state_dir=tmp_path / "state")

    assert (result.exit_code, result.error_code) == (1, "FLX-3305"), result.stderr
    assert "the strictCoverage declaration of the Simulation" in result.stderr


def test_the_evidence_report_lists_the_declaration_under_what_was_not_proven(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _starter_with_check(tmp_path / "shipped")
    code = main(
        ["run", str(bundle), "--ship", "-o", str(tmp_path / "out"), "--json"]
        + ["--state-dir", str(tmp_path / "state")]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0, envelope

    report = next((tmp_path / "state" / "runs").glob("*/EVIDENCE.md")).read_text("utf-8")
    not_proven = report.split("## What Was Not Proven", 1)[1]
    assert "accepted and not generated" in not_proven
    assert "`contract.invariants.positive_amount` (check)" in not_proven
    assert "accepted and not generated" not in report.split("## What Was Not Proven", 1)[0]


def test_a_flux_side_check_is_reported_with_its_own_locator(tmp_path: Path) -> None:
    bundle = tmp_path / "flux-side"
    scaffold(bundle)
    simulation = bundle / "simulation.flux.yml"
    simulation.write_text(
        simulation.read_text("utf-8")
        + "extensions:\n  com.agentics.flux/1:\n    emits:\n      payment_recovery_moment:\n"
        "        rules:\n          - id: amount_is_positive\n            language: flux-expr/3\n"
        "            check: \"subscriber_id || market <> ''\"\n",
        "utf-8",
    )

    result = plan(bundle, state_dir=tmp_path / "state")

    assert result.exit_code == 0, result.stderr
    (row,) = result.artifacts["declared_not_generated"]
    assert (row["kind"], row["locator"]) == ("check", "bundle.rules.amount_is_positive")


#: The ``planDigest`` the engine at 4d43786, before declarations were reported, gives each
#: bundle below. Reporting them changes nothing the plan binds, so every plan keeps its digest.
_DIGESTS_BEFORE_THE_REPORT = {
    "shared": "sha256:8e51e9b14cca51ba8c36cb54d0732b96fd3d2d4a728ac433ac239ce2f40e2b2c",
    "link": "sha256:7257252a66c7f3b1a95749ddf30dd78a35b242112486e43ebe9f5e5d8fadb323",
    "check": "sha256:8c9c06d2c7586925d5acb5d498650e04e0c9db26e48be1c715e4970338c8d130",
}


@pytest.mark.parametrize("name", sorted(_DIGESTS_BEFORE_THE_REPORT))
def test_a_bundle_that_declares_one_keeps_its_plan_digest_and_record(
    tmp_path: Path, name: str
) -> None:
    bundle = {
        "shared": lambda: _related_star(tmp_path / "b", shared=True),
        "link": lambda: _related_star(tmp_path / "b", over_link=True),
        "check": lambda: _starter_with_check(tmp_path / "b"),
    }[name]()
    result = plan(bundle, state_dir=tmp_path / "state")

    assert result.exit_code == 0, result.stderr
    assert result.artifacts["plan_digest"] == _DIGESTS_BEFORE_THE_REPORT[name]
    assert "notGenerated" not in json.dumps(result.artifacts["plan_document"])
    assert "notGenerated" not in _recorded(bundle, tmp_path / "state2")
