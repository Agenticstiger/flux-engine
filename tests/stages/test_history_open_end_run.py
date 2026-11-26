# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's open end, planned and verified (ADR 0017, the amendment of 2026-11-25).

A run whose history declares ``openEnd`` writes it on every open last version and nowhere else,
and ``verify`` holds every entity to ``history:open_end`` (``tests/stages/test_history_verify.py``
fails the line on rows that mix NULL and the sentinel). ``plan`` refuses an ``openEnd`` a closed
version could hold: one before ``until``, and, under ``nextStart``, one inside ``timeBounds``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flux_engine.stages.verify import verify

from . import _fleet

pytestmark = pytest.mark.integration

SENTINEL = "9999-12-31"


def test_a_run_writes_the_sentinel_on_every_open_version_and_verifies(tmp_path: Path) -> None:
    target = _fleet.bundle(tmp_path, history={"openEnd": SENTINEL})
    state = tmp_path / "state"
    layout = _fleet.run_bundle(target, state)
    rows = _fleet.rows(layout, _fleet.HISTORY)
    assert any(row["valid_to"] == SENTINEL for row in rows)
    assert all(row["valid_to"] is not None for row in rows)
    verified = verify(target, run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stdout + verified.stderr
    assert "history:open_end" in verified.stdout


@pytest.mark.parametrize(
    ("open_end", "words"),
    [
        ("2024-06-01", "is before until '2025-12-31'"),
        ("2025-12-31", "is inside timeBounds"),
    ],
)
def test_an_open_end_a_closed_version_could_hold_is_refused(
    tmp_path: Path, open_end: str, words: str
) -> None:
    target = _fleet.bundle(tmp_path, history={"openEnd": open_end})
    refused = _fleet.plan_refused(target, tmp_path / "state")
    assert "FLX-3403" in refused.stderr
    assert f"openEnd '{open_end}' {words}" in refused.stderr
