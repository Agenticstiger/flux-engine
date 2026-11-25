# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``plan`` notes the share an ``end`` yields when too few entities can end (ADR 0017, amendment of 2026-11-24).

``tests/fixtures/history-fleet`` is copied with 70 versions over 60 entities: ten entities hold two
versions and fifty hold one. An ``end`` with a column ends entities of two versions or more, so a
share of every entity that asks for more than the ten ends only the ten, and ``plan`` says so on
its text output. The note is advice: no plan document carries it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.core.kernels import KERNEL_V2

from ._fleet import FINAL, HISTORY, bundle, plan_module

pytestmark = pytest.mark.integration


def _planned(tmp_path: Path, end: Dict[str, Any], **options: Any) -> Any:
    target = bundle(tmp_path, history={"activation": "commissioned_on"}, end=end, **options)
    planned = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert planned.success, planned.stderr
    return planned


def test_plan_notes_the_share_an_end_yields_when_too_few_entities_can_end(tmp_path: Path) -> None:
    # A share of 0.9 of every entity asks for 54 endings and only the ten can end.
    planned = _planned(tmp_path, {**FINAL, "share": "0.9"}, versions=70)
    note = (
        f"note: {HISTORY} declares a history end share of 0.9, a share of every "
        "entity, and only 10 of 60 entities can end, so every eligible entity ends and the "
        "share yielded is 0.1667"
    )
    assert note in planned.stderr
    plan_text = Path(planned.artifacts["plan_path"]).read_text("utf-8")
    assert "yielded" not in plan_text


def test_plan_has_no_end_share_note_when_every_declared_ending_is_eligible(tmp_path: Path) -> None:
    planned = _planned(tmp_path, FINAL)
    assert "share yielded" not in planned.stderr
