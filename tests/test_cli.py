# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""flux_engine.cli -- argparse surface, exit codes, and delegation to the
governance gate / describe surfaces.
"""

from __future__ import annotations

import json

import pytest

from flux_engine import __version__
from flux_engine.cli import main

_FAILING_VECTORS = {
    "vectorsVersion": "test-fixture-1",
    "contract": "test fixture (not the real RFC-04 vectors)",
    "canonicalisation": "n/a",
    "description": "Minimal fixture for CLI failure-path coverage (fluxsim gate conform --vectors).",
    "vectors": [
        {
            "name": "deliberately-wrong-expectation",
            "policy": {
                "allowedModels": ["gpt-4"],
                "tokenBudget": 1000,
                "purposeLimitation": False,
            },
            "request": {"model": "gpt-4", "useCase": "test", "tokens": 10},
            "expect": {"allow": False, "reasonCode": "WRONG_ON_PURPOSE"},
        }
    ],
}


@pytest.mark.unit
def test_version_flag_prints_version_and_exits_zero(capsys) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


@pytest.mark.unit
def test_describe_self_prints_key_value_lines(capsys) -> None:
    assert main(["describe", "--self"]) == 0
    out = capsys.readouterr().out
    assert "component: FLUX Simulation Engine" in out


@pytest.mark.unit
def test_describe_self_json_is_valid_json(capsys) -> None:
    """``describe --self --json`` uses the same envelope every other command
    does (stage/exitCode/ok/artifacts/error, see ``_print_result``'s
    docstring) -- the describe document itself lives
    under ``artifacts``, not at the envelope's top level, so a consumer
    never has to special-case one command's JSON shape.
    """
    assert main(["describe", "--self", "--json"]) == 0
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert envelope["stage"] == "describe"
    assert envelope["exitCode"] == 0
    assert envelope["ok"] is True
    assert envelope["error"] is None
    document = envelope["artifacts"]
    assert document["component"] == "FLUX Simulation Engine"
    assert "commands" in document


@pytest.mark.unit
def test_describe_without_self_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["describe"])
    assert exc_info.value.code == 2


@pytest.mark.unit
def test_gate_conform_default_vectors_all_pass(capsys) -> None:
    assert main(["gate", "conform"]) == 0
    out = capsys.readouterr().out
    assert "34/34 RFC-04 conformance vectors passed" in out


@pytest.mark.unit
def test_gate_conform_explicit_vectors_path(capsys) -> None:
    from flux_engine.spec import default_vectors_path

    assert main(["gate", "conform", "--vectors", str(default_vectors_path())]) == 0


@pytest.mark.unit
def test_gate_conform_reports_failures_and_exits_one(tmp_path, capsys) -> None:
    vectors_path = tmp_path / "failing-vectors.json"
    vectors_path.write_text(json.dumps(_FAILING_VECTORS))

    exit_code = main(["gate", "conform", "--vectors", str(vectors_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "FAIL:" in captured.err
    assert "0/1 RFC-04 conformance vectors passed" in captured.err


@pytest.mark.unit
def test_gate_without_a_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["gate"])
    assert exc_info.value.code == 2


@pytest.mark.unit
def test_no_command_prints_help_and_returns_two(capsys) -> None:
    assert main([]) == 2
    out = capsys.readouterr().out
    assert "usage" in out.lower()
