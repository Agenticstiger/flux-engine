# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``verify`` and ``run --verify`` fail a run whose children per parent are outside a class's margin.

A bundle whose ``counts`` entry lays the children out given a parent column is scored per class
against the profile's grouped histogram. One class the profile holds otherwise is an ``outside``
row whose OpenLineage assertion says ``success: false`` with ``severity: error``; the run used to
print that next to a passing summary and exit 0. The exit code now agrees with the assertions of the
rows a ``counts`` entry declares, and the text names the failing group by its ordinal.

The bundle is the one ``fluxsim init`` writes from the profile of
``tests/author/test_init_children_given`` (the layout of ``docs/how-to/give-every-parent-its-children.md``),
verified against the same profile and against a copy with one group's histogram swapped.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.cli import main
from flux_engine.core.digest import digest_of_document
from tests.author.test_init_children_given import _by_segment, _init, _profile, _ungrouped

pytestmark = pytest.mark.integration


def _bundle(tmp_path: Path, *, swap: bool) -> Tuple[Path, Path]:
    """The bundle ``init`` writes, pinned to the profile it is verified against; with *swap*, one class's cells reversed."""
    profile = _profile([_ungrouped(), _by_segment()])
    written = _init(tmp_path, profile)
    assert written.exit_code == 0, written.stderr
    held = copy.deepcopy(profile)
    if swap:
        group = held["relationships"][0]["statistics"][1]["groups"][0]
        group["binCounts"] = group["binCounts"][::-1]
    path = tmp_path / "held.profile.json"
    path.write_text(json.dumps(held), encoding="utf-8")
    simulation = tmp_path / "bundle" / "simulation.flux.yml"
    pinned = digest_of_document(profile, subject="p").value
    moved = digest_of_document(held, subject="p").value
    simulation.write_text(
        simulation.read_text(encoding="utf-8").replace(pinned, moved), encoding="utf-8"
    )
    return tmp_path / "bundle", path


def _verify(
    command: List[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> Tuple[int, str, str]:
    capsys.readouterr()
    code = main([*command, "--state-dir", str(tmp_path / "state")])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _failed_assertions(envelope: Dict[str, Any], verify: Dict[str, Any]) -> List[Dict[str, Any]]:
    tables = verify["scorecard"]["document"]["tables"]
    return [
        entry
        for table in tables
        for entry in table["dataQualityAssertions"]["assertions"]
        if not entry["success"]
    ]


def test_run_verify_exits_1_and_names_the_class_when_one_group_is_swapped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, profile = _bundle(tmp_path, swap=True)
    code, stdout, stderr = _verify(
        ["run", str(bundle), "--verify", "--profile", str(profile)], tmp_path, capsys
    )
    assert code == 1
    assert "children orders.order_to_customer, given segment: group #0 " in stderr
    assert "outside the tolerance 0.1" in stderr
    assert "scorecard: overall" in stdout


def test_verify_exits_1_exactly_when_the_json_says_an_assertion_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, profile = _bundle(tmp_path, swap=True)
    code, out, _ = _verify(
        ["run", str(bundle), "--verify", "--profile", str(profile), "-j"], tmp_path, capsys
    )
    envelope = json.loads(out)
    verify = envelope["artifacts"]["verify"]
    failed = _failed_assertions(envelope, verify)
    assert [entry["name"] for entry in failed] == ["children:orders.order_to_customer"]
    assert failed[0]["severity"] == "error" and failed[0]["params"]["outcome"] == "outside"
    assert verify["scorecard"]["met"] is False
    assert code == 1 and envelope["ok"] is False and envelope["exitCode"] == 1

    state = json.loads(out)["artifacts"]["apply"]
    run_id = state.get("run_id") or state["run_id"]
    code, out, stderr = _verify(
        ["verify", str(bundle), "--run-id", str(run_id), "--profile", str(profile), "-j"],
        tmp_path,
        capsys,
    )
    again = json.loads(out)
    assert code == 1 and again["ok"] is False
    assert again["artifacts"]["scorecard"]["met"] is False
    assert [entry["name"] for entry in _failed_assertions(again, again["artifacts"])] == [
        "children:orders.order_to_customer"
    ]


def test_a_profile_that_matches_every_class_still_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, profile = _bundle(tmp_path, swap=False)
    code, out, stderr = _verify(
        ["run", str(bundle), "--verify", "--profile", str(profile), "-j"], tmp_path, capsys
    )
    envelope = json.loads(out)
    verify = envelope["artifacts"]["verify"]
    assert code == 0, stderr
    assert not _failed_assertions(envelope, verify)
    assert verify["scorecard"]["met"] is True
