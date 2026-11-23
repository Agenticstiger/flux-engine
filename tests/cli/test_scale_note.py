# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim plan --scale`` below 1: when the refusal for too few child rows names the scale.

The note ("raise the scale") is given only when the same layout at scale 1 would not be refused
the same way, and under a ``parentWhen`` it reads the eligible parents.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from flux_engine.cli import main
from tests.stages.test_counts_run import _without_shape, bundle_with

pytestmark = pytest.mark.unit

NOTE = "too few child rows"


def _plan(
    bundle: Path, tmp_path: Path, scale: Optional[str], capsys: pytest.CaptureFixture[str]
) -> Dict[str, Any]:
    argv = ["-j", "plan", str(bundle), "--state-dir", str(tmp_path / "state"), "--kernel", "0.2.0"]
    status = main([*argv, *(["--scale", scale] if scale else [])])
    envelope: Dict[str, Any] = json.loads(capsys.readouterr().out)
    assert (status == 0) == envelope["ok"]
    return envelope


def test_the_note_is_given_when_only_the_scale_leaves_too_few_child_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = bundle_with(tmp_path, "only-scale", minimum=None, account_minimum=4)
    assert _plan(bundle, tmp_path, None, capsys)["ok"]
    # 300 accounts at 0.335 are 101 (100.5 rounds up) and 1200 invoices are 402: 404 are needed.
    refused = _plan(bundle, tmp_path, "0.335", capsys)
    assert not refused["ok"] and refused["error"]["code"] == "FLX-3403"
    assert "--scale 0.335" in refused["error"]["message"] and NOTE in refused["error"]["message"]


def test_the_note_is_left_out_when_scale_one_fails_the_same_way(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = bundle_with(
        tmp_path, "both", account_minimum=5
    )  # 1200 invoices cannot give 300 accounts 5
    for scale in (None, "0.5", "0.335"):
        refused = _plan(bundle, tmp_path, scale, capsys)
        message = refused["error"]["message"]
        assert refused["error"]["code"] == "FLX-3403" and "N - m * e < 0" in message
        assert "--scale" not in message and NOTE not in message


def test_a_parent_when_reads_the_eligible_parents_for_the_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    everyone = "account_id >= 1"
    # Every account is eligible, so the answer is the one without a condition.
    passing = bundle_with(tmp_path, "when-note", account_when=everyone, account_minimum=4)
    _without_shape(passing)
    assert _plan(passing, tmp_path, None, capsys)["ok"]
    refused = _plan(passing, tmp_path, "0.335", capsys)
    assert "--scale 0.335" in refused["error"]["message"] and NOTE in refused["error"]["message"]
    failing = bundle_with(tmp_path, "when-both", account_when=everyone, account_minimum=5)
    _without_shape(failing)
    refused = _plan(failing, tmp_path, "0.335", capsys)
    assert "N - m * e < 0" in refused["error"]["message"]
    assert "--scale" not in refused["error"]["message"] and NOTE not in refused["error"]["message"]
