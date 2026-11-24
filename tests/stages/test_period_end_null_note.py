# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``plan`` and ``run`` say that a period end's ``nullFraction`` is met only under open parents.

ADR 0024 section 5 cuts a period's end at a closed parent's end, so an end is NULL only under a
parent that is open and the share generated can be lower than the share declared. When a period
end declares a ``nullFraction`` and its parent's lifetime has an end, the stage prints one note on
stderr. The note is advice only: it is in no plan document, digest or receipt, and a bundle with
the same keys plans and runs to the same digests with or without it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import pytest

from flux_engine.cli import main
from tests.fixtures.temporal.bundle import copy_uncut

pytestmark = pytest.mark.integration

#: Accounts are periods within their customers, and customers can end.
CLOSING_PARENT = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end }
  - { emit: account, start: a_open, end: a_close, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
within:
  - { emit: account, relationship: customer, lifetime: true }
"""
#: The same periods under customers whose lifetime has a start and no end: they never close.
OPEN_PARENT = """\
lifetimes:
  - { emit: customer, start: cu_start }
  - { emit: account, start: a_open, end: a_close, duration: { bins: ['0', '300', '900'], binWeights: ['2', '1'] } }
within:
  - { emit: account, relationship: customer, lifetime: true }
"""
#: Accounts that are not periods: their opening date is within the customer's lifetime.
NO_PERIOD = """\
lifetimes:
  - { emit: customer, start: cu_start, end: cu_end }
  - { emit: account, start: a_open, end: a_close }
within:
  - { emit: account, relationship: customer, date: a_open }
"""
NOTE = "note: account.a_close declares nullFraction 0.7"


def _plan(bundle: Path, state: Path, capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["plan", str(bundle), "--state-dir", str(state), "--kernel", "0.2.0"]) == 0
    return capsys.readouterr().err


def _end_null_fraction(bundle: Path, replacement: str) -> None:
    path = bundle / "simulation.flux.yml"
    text = path.read_text("utf-8")
    old = "a_close: { nullFraction: '0.7', "
    assert text.count(old) == 1
    path.write_text(text.replace(old, replacement, 1), "utf-8")


def test_plan_notes_a_period_end_nullfraction_under_a_parent_that_can_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = copy_uncut(tmp_path, "closing", CLOSING_PARENT)
    err = _plan(bundle, tmp_path / ".flux", capsys)
    assert err.count(NOTE) == 1
    assert "within customer" in err and "NULL only under a parent that is open" in err
    assert "lower than 0.7" in err


def test_plan_json_carries_the_note_in_its_artifacts_and_stays_out_of_plan_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = copy_uncut(tmp_path, "json", CLOSING_PARENT)
    state = tmp_path / ".flux"
    assert main(["-j", "plan", str(bundle), "--state-dir", str(state), "--kernel", "0.2.0"]) == 0
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert len(envelope["artifacts"]["period_end_notes"]) == 1
    assert NOTE in captured.err
    plan_path = Path(envelope["artifacts"]["plan_path"])
    assert "nullFraction" not in plan_path.read_text("utf-8")


@pytest.mark.parametrize(
    "keys, replacement",
    [
        (OPEN_PARENT, None),
        (NO_PERIOD, None),
        (CLOSING_PARENT, "a_close: { "),
        (CLOSING_PARENT, "a_close: { nullFraction: '0', "),
    ],
    ids=["parent-cannot-close", "no-period", "no-nullfraction", "zero-nullfraction"],
)
def test_plan_gives_no_note_without_the_three_conditions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], keys: str, replacement: Optional[str]
) -> None:
    bundle = copy_uncut(tmp_path, "control", keys)
    if replacement is not None:
        _end_null_fraction(bundle, replacement)
    assert "declares nullFraction" not in _plan(bundle, tmp_path / ".flux", capsys)


def test_run_gives_the_note_and_the_note_moves_no_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    closing = copy_uncut(tmp_path, "closing", CLOSING_PARENT)
    args: List[str] = ["-j", "run", str(closing), "--state-dir"]
    assert main([*args, str(tmp_path / ".flux")]) == 0
    captured = capsys.readouterr()
    assert NOTE in captured.err
    digests = json.loads(captured.out)["artifacts"]["plan"]
    plan_document = json.loads(Path(digests["plan_path"]).read_text("utf-8"))
    assert plan_document["planDigest"] == digests["plan_digest"]
    assert "ADR 0024" not in json.dumps(plan_document)
