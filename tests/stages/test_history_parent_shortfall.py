# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history that holds fewer rows than it declares says so in the plan and in verify's report.

When no entity has room left under its parent's lifetime, the history writes fewer rows than the
contract declares (ADR 0017, amendment of 2026-11-24). The shortfall is no refusal, so it has to be
visible to a reader of the run, not only to one who reads ``plan``'s stdout. What is pinned:

* ``plan.json`` holds ``declaredRows`` and ``shortfall`` on that emit's entry, and the plan
  digest covers them;
* ``fluxsim verify`` prints one line, declared against written, for that emit;
* a bundle whose history holds every declared row keeps its plan document and verify output.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from flux_engine.core.digest import build_plan_digest
from flux_engine.core.errors import FluxError
from flux_engine.stages.apply import load_recorded_plan
from flux_engine.stages.layout import RunLayout, run_id_for
from flux_engine.stages.verify import verify

from ._fleet import HISTORY as EMIT
from ._fleet import rows as read_rows
from ._fleet import run_bundle
from .test_history_parent_fit import SHORT, _bundle

pytestmark = pytest.mark.integration

#: Every depot lives three to five days, so the 60 vehicles hold fewer than the 200 declared rows.
SHORT_ALL = {"range": {"min": "2024-03-05", "max": "2024-03-06"}}


def _plan(layout: RunLayout) -> Dict[str, Any]:
    return json.loads(layout.plan_path.read_text("utf-8"))


def _verified(bundle: Path, layout: RunLayout, state: Path) -> Tuple[str, Dict[str, Any]]:
    verified = verify(bundle, run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stdout + verified.stderr
    return verified.stdout, verified.artifacts


def test_the_plan_records_the_declared_rows_and_the_shortfall(tmp_path: Path) -> None:
    layout = run_bundle(_bundle(tmp_path, closed=SHORT_ALL), tmp_path / "state")
    written = len(read_rows(layout, EMIT))
    assert written < 200
    plan = _plan(layout)
    entry = plan["emits"][EMIT]
    assert entry["generator"]["rows"] == written
    assert entry["declaredRows"] == 200
    assert entry["shortfall"] == 200 - written
    # Only the emit that fell short says so.
    assert [name for name, found in plan["emits"].items() if "shortfall" in found] == [EMIT]
    assert [name for name, found in plan["emits"].items() if "declaredRows" in found] == [EMIT]
    # The plan digest covers the two keys: it is the digest of the document that holds them.
    held = {**plan, "planDigest": None}
    assert plan["planDigest"] == build_plan_digest(held).ref.value
    bare = {
        name: {k: v for k, v in found.items() if k not in ("declaredRows", "shortfall")}
        for name, found in plan["emits"].items()
    }
    assert build_plan_digest({**held, "emits": bare}).ref.value != plan["planDigest"]


def test_verify_reports_the_declared_rows_against_the_written_ones(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, closed=SHORT_ALL)
    layout = run_bundle(bundle, tmp_path / "state")
    written = len(read_rows(layout, EMIT))
    stdout, artifacts = _verified(bundle, layout, tmp_path / "state")
    line = f"  shortfall {EMIT}: {written} rows written of 200 declared ({200 - written} short)"
    assert stdout.splitlines().count(line) == 1
    assert [each for each in stdout.splitlines() if "declared (" in each] == [line]
    assert artifacts["historyShortfalls"] == {
        EMIT: {"declaredRows": 200, "rows": written, "shortfall": 200 - written}
    }


def test_a_history_that_holds_every_declared_row_keeps_its_plan_and_verify_output(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, closed=SHORT)  # capped vehicles, but the versions move: 200 rows
    layout = run_bundle(bundle, tmp_path / "state")
    assert len(read_rows(layout, EMIT)) == 200
    plan = _plan(layout)
    for found in plan["emits"].values():
        assert "declaredRows" not in found and "shortfall" not in found
    stdout, artifacts = _verified(bundle, layout, tmp_path / "state")
    assert "shortfall" not in stdout and "declared" not in stdout.replace("declared, not", "")
    assert "historyShortfalls" not in artifacts


_GONE = object()


def _replanned(layout: RunLayout, state: Path, **entry: Any) -> RunLayout:
    """The run directory of a plan whose short emit's entry says *entry* instead, digest recomputed.

    A value of ``_GONE`` removes the key.
    """
    plan = _plan(layout)
    for key, value in entry.items():
        if value is _GONE:
            del plan["emits"][EMIT][key]
        else:
            plan["emits"][EMIT][key] = value
    plan["planDigest"] = None
    plan["planDigest"] = build_plan_digest(plan).ref.value
    layout.plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True), "utf-8")
    moved = state / "runs" / run_id_for(plan["planDigest"])
    shutil.move(str(layout.run_dir), str(moved))
    return RunLayout.for_state_dir(state, run_id_for(plan["planDigest"]))


def _short_run(tmp_path: Path) -> Tuple[Path, Path, RunLayout]:
    bundle, state = _bundle(tmp_path, closed=SHORT_ALL), tmp_path / "state"
    layout = run_bundle(bundle, state)
    assert "shortfall" in _plan(layout)["emits"][EMIT]
    return bundle, state, layout


def test_the_plan_a_run_recorded_is_read_back(tmp_path: Path) -> None:
    _, _, layout = _short_run(tmp_path)
    assert load_recorded_plan(layout)["emits"][EMIT]["shortfall"] > 0


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ({"declaredRows": _GONE}, "without the other"),
        ({"shortfall": _GONE}, "without the other"),
        ({"shortfall": True}, "whole numbers"),
        ({"declaredRows": True}, "whole numbers"),
        ({"declaredRows": "200"}, "whole numbers"),
        ({"shortfall": "x"}, "whole numbers"),
        ({"shortfall": [1]}, "whole numbers"),
        ({"shortfall": 0, "declaredRows": 200}, "above zero"),
        ({"shortfall": -5, "declaredRows": 200}, "above zero"),
        ({"shortfall": 1}, "is not the shortfall"),
        ({"declaredRows": 10**9}, "is not the shortfall"),
    ],
)
def test_a_plan_whose_shortfall_does_not_hold_together_is_flx_2102(
    tmp_path: Path, entry: Dict[str, Any], why: str
) -> None:
    bundle, state, layout = _short_run(tmp_path)
    edited = _replanned(layout, state, **entry)
    with pytest.raises(FluxError) as refused:
        load_recorded_plan(edited)
    assert refused.value.code.code == "FLX-2102"
    assert EMIT in str(refused.value) and why in str(refused.value)
    verified = verify(bundle, run_id=edited.run_id, state_dir=state)
    assert not verified.success
    assert "FLX-2102" in verified.stderr and EMIT in verified.stderr
    assert "Traceback" not in verified.stderr


def test_a_plan_whose_short_emit_has_no_generator_rows_is_flx_2102(tmp_path: Path) -> None:
    _, state, layout = _short_run(tmp_path)
    edited = _replanned(layout, state, generator={})
    with pytest.raises(FluxError, match="does not state the rows it writes"):
        load_recorded_plan(edited)


def test_a_plan_that_gives_an_emit_a_shortfall_it_never_had_is_flx_2102(tmp_path: Path) -> None:
    layout = run_bundle(_bundle(tmp_path, closed=SHORT), tmp_path / "state")
    plan = _plan(layout)
    assert "shortfall" not in plan["emits"][EMIT]
    edited = _replanned(layout, tmp_path / "state", shortfall=4)
    with pytest.raises(FluxError, match="without the other"):
        load_recorded_plan(edited)


def test_a_long_shortfall_value_is_quoted_short(tmp_path: Path) -> None:
    _, state, layout = _short_run(tmp_path)
    edited = _replanned(layout, state, shortfall="x" * 100_000)
    with pytest.raises(FluxError) as refused:
        load_recorded_plan(edited)
    assert len(str(refused.value)) < 600
