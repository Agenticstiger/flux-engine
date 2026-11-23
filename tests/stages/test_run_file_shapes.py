# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``plan.json``, ``receipt.json`` and ``run-digest.json`` that are not the documents written.

A truncated file, a file that is not UTF-8 and a file holding a list are refused by code, naming
the file and the remedy, by every stage that reads them. None is a ``JSONDecodeError`` or an
``AttributeError`` out of a function whose contract is to return a result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Tuple

import pytest

from flux_engine.api.stages import StageResult
from flux_engine.stages.apply import apply
from flux_engine.stages.run import run
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify

pytestmark = pytest.mark.integration

BAD = {
    "a list": b"[]\n",
    "truncated": b'{"planDigest": "sha256:ab',
    "not UTF-8": b"\xff\xfe\x00",
    "a scalar": b"7\n",
}


def _applied(bundle_dir: Path, state_dir: Path) -> Tuple[str, Path]:
    ran = run(bundle_dir, state_dir=state_dir)
    assert ran.success, ran.stderr
    run_id = ran.artifacts["plan"]["run_id"]
    return run_id, state_dir / "runs" / run_id


def _stages(
    bundle_dir: Path, state_dir: Path, run_id: str, tmp: Path
) -> Dict[str, Callable[[], StageResult]]:
    return {
        "apply": lambda: apply(bundle_dir, run_id=run_id, state_dir=state_dir, auto_plan=False),
        "verify": lambda: verify(bundle_dir, run_id=run_id, state_dir=state_dir),
        "ship": lambda: ship(bundle_dir, run_id=run_id, state_dir=state_dir, out_dir=tmp / "out"),
    }


def _refused(result: StageResult, code: str, name: str) -> None:
    assert not result.success
    assert result.error_code == code, (result.error_code, result.stderr)
    assert name in result.stderr
    assert "again" in result.stderr  # the remedy


@pytest.mark.parametrize("kind", sorted(BAD))
@pytest.mark.parametrize("stage", ["apply", "verify", "ship"])
def test_a_plan_that_is_not_a_json_object_is_refused_by_every_stage(
    bundle_dir: Path, state_dir: Path, tmp_path: Path, stage: str, kind: str
) -> None:
    run_id, run_dir = _applied(bundle_dir, state_dir)
    (run_dir / "plan.json").write_bytes(BAD[kind])
    _refused(_stages(bundle_dir, state_dir, run_id, tmp_path)[stage](), "FLX-2102", "plan.json")


@pytest.mark.parametrize("kind", sorted(BAD))
@pytest.mark.parametrize("stage", ["verify", "ship"])
def test_a_receipt_that_is_not_a_json_object_is_refused(
    bundle_dir: Path, state_dir: Path, tmp_path: Path, stage: str, kind: str
) -> None:
    run_id, run_dir = _applied(bundle_dir, state_dir)
    (run_dir / "receipt.json").write_bytes(BAD[kind])
    _refused(_stages(bundle_dir, state_dir, run_id, tmp_path)[stage](), "FLX-2109", "receipt.json")


@pytest.mark.parametrize("kind", sorted(BAD))
@pytest.mark.parametrize("stage", ["verify", "ship"])
def test_a_run_digest_that_is_not_a_json_object_is_refused(
    bundle_dir: Path, state_dir: Path, tmp_path: Path, stage: str, kind: str
) -> None:
    run_id, run_dir = _applied(bundle_dir, state_dir)
    (run_dir / "run-digest.json").write_bytes(BAD[kind])
    _refused(
        _stages(bundle_dir, state_dir, run_id, tmp_path)[stage](), "FLX-2110", "run-digest.json"
    )


def test_a_valid_run_still_reads(bundle_dir: Path, state_dir: Path, tmp_path: Path) -> None:
    run_id, _ = _applied(bundle_dir, state_dir)
    result: Callable[[], StageResult] = _stages(bundle_dir, state_dir, run_id, tmp_path)["verify"]
    assert result().success
