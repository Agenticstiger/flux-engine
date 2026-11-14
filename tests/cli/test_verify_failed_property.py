# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim verify`` names the property a failed fidelity check found outside its margin.

The failed leg used to print the expose id and nothing else, on stdout and on
stderr, so a reader had to run it again with ``--json`` to learn which declared
property to open, and ``--json`` did not name it either: a property outside
its margin was only a ``failed`` status. The leg now carries one line per
property: its id, kind and columns, the column a grouped property groups by,
and the groups that landed outside.

A group is named by its ordinal in the sorted group order, never by its key.
The key is a value read out of the committed slice, and a failure names fields
and counts, not values (ADR 0003; the ``FLX-5005`` refusal for a group below
``records.min`` says ``group #N`` for the same reason).

Each case runs the real ``verify`` command over a copy of the small telco
bundle whose properties it sets, against a slice built from the run's own
emitted rows (synthetic, never real data): the slice agrees with the run
everywhere except the one place a case moves it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pytest
import yaml

from flux_engine.cli import main
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import _failure_lines
from tests.concordance.conftest import write_slice

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration

EXPOSE_ID = "payment_recovery_moment"
EXTENSION = "com.agentics.flux/1"
MEAN_TOLERANCE = {"type": "absolute", "value": "50"}
SUPPORT = {"property": "support", "column": "market", "tolerance": {"type": "count", "max": 0}}
UNGROUPED_MEAN = {"property": "mean", "column": "amount", "tolerance": MEAN_TOLERANCE}
GROUPED_MEAN = {**UNGROUPED_MEAN, "groupBy": "market"}
#: Far from every mean the generator draws (the declared range is 1 to 10000).
FAR = "100.0000"

Rows = List[Dict[str, Any]]


def _applied(tmp_path: Path, properties: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Apply a copy of the small bundle declaring *properties*; read back the emitted rows."""
    bundle = tmp_path / "bundle"
    shutil.copytree(FIXTURE_BUNDLE, bundle)
    simulation_path = bundle / "simulation.flux.yml"
    simulation = yaml.safe_load(simulation_path.read_text(encoding="utf-8"))
    entry = simulation["extensions"][EXTENSION]["emits"][EXPOSE_ID]
    entry["fidelity"]["properties"] = [dict(declared) for declared in properties]
    simulation_path.write_text(yaml.safe_dump(simulation, sort_keys=False), encoding="utf-8")

    state_dir = tmp_path / ".flux"
    applied = apply(bundle, state_dir=state_dir)
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])
    layout = RunLayout.for_state_dir(state_dir, run_id)
    columns = json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))[
        "columns"
    ]
    rows: Rows = [
        dict(zip(columns, json.loads(line), strict=True))
        for line in layout.emit_path(EXPOSE_ID).read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert rows, "the fixture declares a fidelity block, so its rows are generated"
    return {"bundle": bundle, "state_dir": state_dir, "run_id": run_id, "rows": rows}


def _verify(
    applied: Mapping[str, Any],
    rows: Rows,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
) -> Tuple[int, str, str]:
    """Run ``verify`` over a slice of *rows*; return the exit code, stdout and stderr."""
    uri = write_slice(tmp_path / "slice.ndjson", rows)
    capsys.readouterr()
    exit_code = main(
        [
            "verify",
            str(applied["bundle"]),
            "--run-id",
            applied["run_id"],
            "--state-dir",
            str(applied["state_dir"]),
            "--committed-slice",
            f"{EXPOSE_ID}={uri}",
            *extra,
        ]
    )
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def _fidelity_json(stdout: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    envelope = json.loads(stdout)
    return envelope["artifacts"]["fidelity"][EXPOSE_ID], envelope["error"]


def test_an_ungrouped_property_outside_its_margin_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    applied = _applied(tmp_path, [UNGROUPED_MEAN, SUPPORT])
    slice_rows = [{**row, "amount": FAR} for row in applied["rows"]]

    exit_code, stdout, stderr = _verify(applied, slice_rows, tmp_path, capsys)

    line = "property 000:mean:amount (mean of amount): outside the declared margin"
    assert exit_code == 1
    assert stderr.splitlines() == [f"{EXPOSE_ID}: fidelity check failed", f"  {line}"]
    leg = stdout.splitlines().index(f"  fidelity {EXPOSE_ID}: failed")
    assert stdout.splitlines()[leg + 1 :] == [f"    {line}"]

    exit_code, stdout, _ = _verify(applied, slice_rows, tmp_path, capsys, "--json")
    outcome, error = _fidelity_json(stdout)
    assert exit_code == 1
    assert outcome["status"] == "failed"
    assert outcome["refusals"] == []
    assert outcome["outside"] == [
        {"propertyId": "000:mean:amount", "property": "mean", "columns": ["amount"]}
    ]
    assert error["message"] == stderr.rstrip("\n")


def test_a_grouped_property_names_its_column_and_the_failing_group_by_ordinal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    applied = _applied(tmp_path, [GROUPED_MEAN, SUPPORT])
    rows: Rows = applied["rows"]
    markets = sorted({row["market"] for row in rows})
    assert len(markets) > 1, "a grouped case needs more than one group"
    moved = markets[1]
    slice_rows = [{**row, "amount": FAR} if row["market"] == moved else row for row in rows]

    exit_code, stdout, stderr = _verify(applied, slice_rows, tmp_path, capsys)

    line = (
        "property 000:mean:amount@market (mean of amount, grouped by market): "
        "outside the declared margin in group #1"
    )
    assert exit_code == 1
    assert stderr.splitlines() == [f"{EXPOSE_ID}: fidelity check failed", f"  {line}"]
    assert f"    {line}" in stdout.splitlines()

    _, stdout, _ = _verify(applied, slice_rows, tmp_path, capsys, "--json")
    outcome, _ = _fidelity_json(stdout)
    assert outcome["refusals"] == []
    assert outcome["outside"] == [
        {
            "propertyId": "000:mean:amount@market",
            "property": "mean",
            "columns": ["amount"],
            "groupBy": "market",
            "groupOrdinals": [1],
        }
    ]

    # Fields and ordinals only: not the key of the group that moved, not one
    # of the slice's other keys, and not an amount.
    named = [text for text in (stderr + stdout).splitlines() if "property " in text]
    assert named
    for text in named:
        for value in (*markets, FAR, str(rows[0]["amount"])):
            assert value not in text, (value, text)


def test_the_update_golden_refusal_names_the_property_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    applied = _applied(tmp_path, [UNGROUPED_MEAN, SUPPORT])
    slice_rows = [{**row, "amount": FAR} for row in applied["rows"]]
    refused = f"{EXPOSE_ID}: fidelity check failed: property 000:mean:amount (mean of amount)"

    exit_code, _, stderr = _verify(
        applied, slice_rows, tmp_path, capsys, "--update-golden", "--reason", "pin it"
    )
    assert exit_code == 1
    assert f"a leg failed: {refused}: outside the declared margin" in stderr

    _, stdout, _ = _verify(
        applied, slice_rows, tmp_path, capsys, "--update-golden", "--reason", "pin it", "--json"
    )
    golden = json.loads(stdout)["artifacts"]["golden"]
    assert golden["updated"] is False
    assert golden["problems"] == [f"{refused}: outside the declared margin"]


def test_a_check_that_passes_names_no_property(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    applied = _applied(tmp_path, [UNGROUPED_MEAN, GROUPED_MEAN, SUPPORT])

    exit_code, stdout, stderr = _verify(applied, applied["rows"], tmp_path, capsys)

    assert exit_code == 0, stderr
    assert f"  fidelity {EXPOSE_ID}: passed" in stdout.splitlines()
    assert "property " not in stdout + stderr

    _, stdout, _ = _verify(applied, applied["rows"], tmp_path, capsys, "--json")
    outcome, _ = _fidelity_json(stdout)
    assert (outcome["status"], outcome["refusals"], outcome["outside"]) == ("passed", [], [])


def test_every_property_outside_is_named_with_every_group_that_failed() -> None:
    """Both kinds on one leg, a refusal after the properties, and a plural for groups."""
    outcome: Dict[str, Any] = {
        "status": "failed",
        "outside": [
            {"propertyId": "001:mean:amount", "property": "mean", "columns": ["amount"]},
            {
                "propertyId": "002:mean:amount@market",
                "property": "mean",
                "columns": ["amount"],
                "groupBy": "market",
                "groupOrdinals": [0, 2],
            },
            {
                "propertyId": "003:correlation:amount+attempts@market",
                "property": "correlation",
                "columns": ["amount", "attempts"],
                "groupBy": "market",
                "groupOrdinals": [],
            },
        ],
        "refusals": [
            {
                "code": "FLX-5005",
                "reason": "column 'amount' row 3: value outside the declared range",
                "propertyId": "003:correlation:amount+attempts@market",
            },
            {"code": "FLX-5011", "reason": "the committed state was formed under others"},
        ],
    }
    assert _failure_lines(outcome) == [
        "property 001:mean:amount (mean of amount): outside the declared margin",
        "property 002:mean:amount@market (mean of amount, grouped by market): "
        "outside the declared margin in groups #0, #2",
        "property 003:correlation:amount+attempts@market "
        "(correlation of amount, attempts, grouped by market): outside the declared margin",
        "refusal FLX-5005 on property 003:correlation:amount+attempts@market: "
        "column 'amount' row 3: value outside the declared range",
        "refusal FLX-5011: the committed state was formed under others",
    ]
    assert _failure_lines({"status": "failed", "detail": "no readable seam attestation"}) == []
