# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A profile overlay through a real run: plan, apply, verify and ship (ADR 0030 sections 7 and 8).

The bundle is the one ``fluxsim init`` writes for the orders fixture with its mean statistic, with
an overlay declared in its Simulation, and the commands are the CLI's own. Pinned here:

* a number the overlay changed cites the overlay (register format 1-3-0, five counts), and a
  ``profile`` citation of it is refused at ``plan`` (``FLX-3113``) before anything is written;
* ``verify`` scores against the overlaid profile and the scorecard (1-4-0) names the overlay,
  its name and digest, while a run with no overlay writes the scorecard it wrote before;
* ``EVIDENCE.md`` names the overlay and says its numbers are the maintainers' scenario;
* a Simulation that declares an overlay and cites it in no entry keeps its register format.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from jsonschema import Draft202012Validator

from flux_engine.cli import main
from flux_engine.stages.ship import ship
from tests.overlays import (
    ORDERS_MEAN_PROFILE,
    cite_rows_from,
    declare_overlay,
    init_orders,
    mean_overlay_yaml,
    without_replacements,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SCORECARD_SCHEMA = json.loads(
    (REPO_ROOT / "docs" / "spec" / "scorecard-1-4.schema.json").read_text(encoding="utf-8")
)
REGISTER_SCHEMA = json.loads(
    (REPO_ROOT / "docs" / "spec" / "parameter-register-1-3.schema.json").read_text(encoding="utf-8")
)
#: ``Sx = mean * 600 * 65536``: the means 48 and 30 of ``amount``. The run holds the published 46.25.
NEAR = "1887436800"
FAR = "1179648000"


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> Tuple[int, str]:
    capsys.readouterr()
    code = main(list(argv))
    return code, capsys.readouterr().out


class Cli:
    """One bundle, taken through ``plan``, ``apply`` and ``verify`` by the command line."""

    def __init__(self, bundle: Path, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.bundle, self.state, self.capsys = bundle, state, capsys
        self.profile = str(ORDERS_MEAN_PROFILE)

    def plan(self) -> Tuple[int, Dict[str, Any]]:
        code, out = _cli(
            self.capsys,
            "plan",
            str(self.bundle),
            "--profile",
            self.profile,
            "--state-dir",
            str(self.state),
            "--json",
        )
        return code, json.loads(out)

    def apply(self, run_id: str) -> int:
        return _cli(
            self.capsys,
            "apply",
            str(self.bundle),
            "--run-id",
            run_id,
            "--state-dir",
            str(self.state),
        )[0]

    def verify(self, run_id: str) -> Tuple[int, Dict[str, Any]]:
        code, out = _cli(
            self.capsys,
            "verify",
            str(self.bundle),
            "--run-id",
            run_id,
            "--profile",
            self.profile,
            "--state-dir",
            str(self.state),
            "--json",
        )
        return code, json.loads(out)

    def run(self) -> Tuple[str, int, Dict[str, Any]]:
        code, planned = self.plan()
        assert code == 0, planned
        run_id = planned["artifacts"]["run_id"]
        assert self.apply(run_id) == 0
        verified_code, verified = self.verify(run_id)
        return run_id, verified_code, verified

    def run_dir(self, run_id: str) -> Path:
        return self.state / "runs" / run_id


def _overlay_bundle(root: Path, mean_sum: str, *, cite: bool = True) -> Path:
    bundle = init_orders(root / "bundle", ORDERS_MEAN_PROFILE)
    declare_overlay(bundle, mean_overlay_yaml("scenario", mean_sum))
    return cite_rows_from(bundle, "scenario") if cite else bundle


@pytest.fixture(scope="module")
def scenario(
    tmp_path_factory: pytest.TempPathFactory,
) -> Tuple[Cli, str, int, Dict[str, Any]]:
    """The overlay bundle, planned, applied and verified, once."""
    root = tmp_path_factory.mktemp("overlay")
    # capsys is function scoped: the module fixture reads the commands' output from a file instead.
    import contextlib
    import io

    buffer = io.StringIO()

    class Capture:
        def readouterr(self) -> Any:
            class Out:
                out = buffer.getvalue()

            buffer.seek(0)
            buffer.truncate()
            return Out()

    cli = Cli(_overlay_bundle(root, NEAR), root / "state", Capture())  # type: ignore[arg-type]
    with contextlib.redirect_stdout(buffer):
        run_id, code, verified = cli.run()
    return cli, run_id, code, verified


def test_the_register_cites_the_overlay_and_the_record_is_format_1_3_0(
    scenario: Tuple[Cli, str, int, Dict[str, Any]],
) -> None:
    cli, run_id, _, _ = scenario
    record = json.loads((cli.run_dir(run_id) / "parameter-register.json").read_text("utf-8"))
    assert record["_schemaURL"].endswith("/parameter-register/1-3-0/FluxParameterRegister.json")
    assert sorted(record["counts"]) == [
        "assumption",
        "calibrated",
        "contract",
        "default",
        "overlay",
        "profile",
    ]
    assert record["counts"]["overlay"] == 1 and sum(record["counts"].values()) == record["declared"]
    (cited,) = [entry for entry in record["entries"] if entry["source"] == "overlay"]
    assert cited["overlay"] == "scenario"
    assert cited["statistic"] == {"pointer": "/tables/orders/rowCount"}
    Draft202012Validator(REGISTER_SCHEMA).validate(record)
    plan_document = json.loads((cli.run_dir(run_id) / "plan.json").read_text("utf-8"))
    assert plan_document["overlay"]["name"] == "scenario"


def test_verify_scores_against_the_overlaid_profile_and_the_scorecard_names_it(
    scenario: Tuple[Cli, str, int, Dict[str, Any]],
) -> None:
    _, _, code, verified = scenario
    assert code == 0, verified
    document = verified["artifacts"]["scorecard"]["document"]
    assert document["_schemaURL"].endswith("/scorecard/1-4-0/FluxScorecard.json")
    assert (
        set(document["overlay"]) == {"name", "digest"} and document["overlay"]["name"] == "scenario"
    )
    assert document["overlay"]["digest"].startswith("sha256:")
    assert document["profile"]["digest"] != document["overlay"]["digest"]
    Draft202012Validator(SCORECARD_SCHEMA).validate(document)
    assert verified["artifacts"]["parameterRegister"]["counts"]["overlay"] == 1


def test_the_run_is_scored_against_the_overlaid_numbers_not_the_pinned_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same rows, same seed, same declared check: the pinned profile's mean (46.25) is within the
    # margin; the overlay's mean of 30 is far outside it.
    near = Cli(_overlay_bundle(tmp_path / "near", NEAR), tmp_path / "near-state", capsys)
    # The far bundle is drawn to the near mean and then restates it: the run is scored against the
    # overlay the Simulation declares now, which the draw does not follow.
    far_bundle = _overlay_bundle(tmp_path / "far", NEAR)
    simulation = far_bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    assert text.count(NEAR) == 1
    simulation.write_text(text.replace(NEAR, FAR), encoding="utf-8")
    far = Cli(far_bundle, tmp_path / "far-state", capsys)
    _, near_code, near_out = near.run()
    _, far_code, far_out = far.run()
    assert near_code == 0 and far_code == 1
    assert (
        far_out["artifacts"]["fidelity"]["orders"]["status"]
        != near_out["artifacts"]["fidelity"]["orders"]["status"]
        or far_out["artifacts"]["scorecard"]["met"] is not None
    )
    near_score = near_out["artifacts"]["scorecard"]["document"]["overall"]["score"]
    far_score = far_out["artifacts"]["scorecard"]["document"]["overall"]["score"]
    assert far_score != near_score


def test_a_run_with_no_overlay_writes_the_scorecard_it_wrote_before(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = Cli(
        without_replacements(init_orders(tmp_path / "bundle", ORDERS_MEAN_PROFILE)),
        tmp_path / "state",
        capsys,
    )
    run_id, code, verified = plain.run()
    assert code == 0, verified
    document = verified["artifacts"]["scorecard"]["document"]
    assert "overlay" not in document and not document["_schemaURL"].endswith(
        "/1-4-0/FluxScorecard.json"
    )
    plan_document = json.loads((plain.run_dir(run_id) / "plan.json").read_text("utf-8"))
    assert "overlay" not in plan_document
    record = json.loads((plain.run_dir(run_id) / "parameter-register.json").read_text("utf-8"))
    assert record["_schemaURL"].endswith("/parameter-register/1-2-0/FluxParameterRegister.json")


def test_a_register_that_cites_the_overlay_in_no_entry_keeps_its_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The overlay restates the mean and leaves the row count alone, so the entry that cites the
    # row count as the profile's still holds, and no entry cites the overlay: the register is
    # recorded as it was, though the run is scored with the overlay.
    # (`init` writes `replacedBy`, which is format 1-3-0 too, so the bundle is without it.)
    bundle = without_replacements(init_orders(tmp_path / "bundle", ORDERS_MEAN_PROFILE))
    declare_overlay(bundle, mean_overlay_yaml("quiet", NEAR, rows=600))
    cli = Cli(bundle, tmp_path / "state", capsys)
    run_id, code, verified = cli.run()
    assert code == 0, verified
    record = json.loads((cli.run_dir(run_id) / "parameter-register.json").read_text("utf-8"))
    assert record["_schemaURL"].endswith("/parameter-register/1-2-0/FluxParameterRegister.json")
    assert "overlay" not in record["counts"]
    assert verified["artifacts"]["scorecard"]["document"]["overlay"]["name"] == "quiet"


def test_a_profile_citation_of_a_number_the_overlay_changed_is_refused_at_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = Cli(_overlay_bundle(tmp_path, NEAR, cite=False), tmp_path / "state", capsys)
    code, planned = cli.plan()
    assert code == 1 and planned["error"]["code"] == "FLX-3113"
    assert "number the overlay 'scenario' changed" in planned["error"]["message"]
    assert not list((tmp_path / "state").glob("runs/*"))


def test_an_overlay_citation_of_an_overlay_the_bundle_does_not_declare_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = init_orders(tmp_path / "bundle", ORDERS_MEAN_PROFILE)
    cite_rows_from(bundle, "scenario")
    code, planned = Cli(bundle, tmp_path / "state", capsys).plan()
    assert code == 1 and planned["error"]["code"] == "FLX-3105"
    assert "the Simulation declares none" in planned["error"]["message"]


def test_evidence_names_the_overlay_and_says_its_numbers_are_a_scenario(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = Cli(_overlay_bundle(tmp_path / "b", NEAR), tmp_path / "state", capsys)
    run_id, code, _ = cli.run()
    assert code == 0
    shipped = ship(
        cli.bundle,
        run_id=run_id,
        out_dir=tmp_path / "out",
        state_dir=cli.state,
        profile=ORDERS_MEAN_PROFILE,
    )
    assert shipped.success, shipped.stderr
    assert "from the overlay" in shipped.stdout
    with tarfile.open(shipped.artifacts["archive"]) as packed:
        names: List[str] = packed.getnames()
        report_member = next(name for name in names if name.endswith("/run/EVIDENCE.md"))
        extracted = packed.extractfile(report_member)
        assert extracted is not None
        report = extracted.read().decode("utf-8")
        scorecard_member = next(name for name in names if name.endswith("/run/scorecard.json"))
        scorecard = packed.extractfile(scorecard_member)
        assert scorecard is not None
        assert json.loads(scorecard.read())["overlay"]["name"] == "scenario"
    assert "and its overlay `scenario`" in report
    assert (
        "come from the overlay `scenario`, a scenario the maintainers wrote and not a measurement"
        in report
    )
    assert "the maintainers' scenario, not measured" in report
