# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A prediction document cannot write outside the state directory (security review finding)."""

from __future__ import annotations

import json
from pathlib import Path

from tests.cli.conftest import parse_json_stdout, run_flux


def _document(prediction_id: str) -> dict:
    return {
        "prediction_id": prediction_id,
        "run_digest": "sha256:" + "0" * 64,
        "metric_id": "telco/saved_customers",
        "statement": "saved customers rise",
        "predicted": "12",
        "horizon_end": "2026-10-01T00:00:00Z",
        "registered_at": "2026-09-01T00:00:00Z",
    }


def test_traversal_prediction_id_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    state = tmp_path / "state"
    escape = "../../outside/escaped"
    doc = tmp_path / "pred.json"
    doc.write_text(json.dumps(_document(escape)), encoding="utf-8")
    result = run_flux(
        ["--json", "accuracy", "register", "--file", str(doc), "--state-dir", str(state)],
        cwd=tmp_path,
    )
    assert result.returncode != 0
    envelope = parse_json_stdout(result)
    assert envelope["error"]["code"] == "FLX-7003"
    assert not list(outside.iterdir()), "nothing may be written outside the state directory"
    assert not (state / "accuracy" / "predictions").exists() or not list(
        (state / "accuracy" / "predictions").iterdir()
    )


def test_safe_prediction_id_registers_inside_the_state_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    doc = tmp_path / "pred.json"
    doc.write_text(json.dumps(_document("pred-1")), encoding="utf-8")
    result = run_flux(
        ["--json", "accuracy", "register", "--file", str(doc), "--state-dir", str(state)],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    written = Path(parse_json_stdout(result)["artifacts"]["path"]).resolve()
    assert written.is_relative_to((state / "accuracy" / "predictions").resolve())
