# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The evidence report names the bundle, whichever way the bundle was spelled.

``fluxsim run . --verify --ship -o out`` is the first command most people run
after ``fluxsim init``, and ``Path(".").name`` is the empty string. The first
evidence report they open began ``# Evidence: `` with a blank where the name
goes, and said ``Bundle: ``````.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.cli import main
from flux_engine.starter import scaffold

pytestmark = pytest.mark.integration


def test_ship_names_the_bundle_when_it_is_given_as_dot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    demo = tmp_path / "demo"
    scaffold(demo)
    monkeypatch.chdir(demo)
    code = main(
        ["run", ".", "--ship", "-o", str(tmp_path / "out"), "--json"]
        + ["--state-dir", str(tmp_path / "state")]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0, envelope
    report = next((tmp_path / "state" / "runs").glob("*/EVIDENCE.md")).read_text(encoding="utf-8")
    assert report.splitlines()[0] == "# Evidence: demo"
    assert "- Bundle: `demo`" in report
