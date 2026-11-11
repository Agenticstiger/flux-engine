# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Real-binary subprocess tests: ``python -m flux_engine.cli <command>``.

No in-process shortcuts -- every test here launches the actual
interpreter and reads argparse's own exit codes and stdout, exactly what
a shell user gets.
"""

from __future__ import annotations

import json
from pathlib import Path

from .conftest import parse_json_stdout, run_flux


def test_version_flag(tmp_path: Path) -> None:
    result = run_flux(["--version"], cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip()


def test_no_command_prints_help_and_exits_2(tmp_path: Path) -> None:
    result = run_flux([], cwd=tmp_path)
    assert result.returncode == 2
    assert "usage" in result.stdout.lower() or "usage" in result.stderr.lower()


def test_validate_the_fixture_bundle(bundle_dir: Path, tmp_path: Path) -> None:
    result = run_flux(["validate", str(bundle_dir)], cwd=tmp_path)
    assert result.returncode == 0, result.stderr


def test_validate_a_nonexistent_bundle_exits_1(tmp_path: Path) -> None:
    result = run_flux(["validate", str(tmp_path / "nope")], cwd=tmp_path)
    assert result.returncode == 1


def test_plan_apply_verify_ship_as_four_real_invocations(bundle_dir: Path, tmp_path: Path) -> None:
    state = tmp_path / ".flux"

    planned = run_flux(["--json", "plan", str(bundle_dir), "--state-dir", str(state)], cwd=tmp_path)
    assert planned.returncode == 0, planned.stderr
    plan_envelope = parse_json_stdout(planned)
    assert plan_envelope["stage"] == "plan"
    assert plan_envelope["ok"] is True
    assert plan_envelope["exitCode"] == 0
    assert plan_envelope["error"] is None
    run_id = plan_envelope["artifacts"]["run_id"]

    applied = run_flux(
        ["apply", str(bundle_dir), "--run-id", run_id, "--state-dir", str(state)], cwd=tmp_path
    )
    assert applied.returncode == 0, applied.stderr

    verified = run_flux(
        ["--json", "verify", str(bundle_dir), "--run-id", run_id, "--state-dir", str(state)],
        cwd=tmp_path,
    )
    # No <state_dir>/golden/ baseline exists yet on this fresh --state-dir,
    # so the honest answer is NOT_CHECKED (never a silent pass) -- and that
    # exits 0, since only a mismatch (NOT_REPRODUCIBLE) fails verify.
    assert verified.returncode == 0, verified.stderr
    verify_artifacts = parse_json_stdout(verified)["artifacts"]
    assert verify_artifacts["reproducibility"] == "not-checked"
    # H1: the JSON verdict always carries both halves of VerifyVerdict, not
    # just the one this build can answer.
    assert verify_artifacts["validity"] == "not-asserted"
    assert verify_artifacts["verdict"] == {
        "reproducibility": "not-checked",
        "validity": "not-asserted",
    }

    shipped = run_flux(
        [
            "--json",
            "ship",
            str(bundle_dir),
            "--run-id",
            run_id,
            "--state-dir",
            str(state),
            "-o",
            str(tmp_path / "dist"),
        ],
        cwd=tmp_path,
    )
    assert shipped.returncode == 1
    assert "FLX-8001" in shipped.stderr
    # B3/M11: a withheld ship's `--json` stdout is still parseable JSON --
    # the withheld CloudEvent(s) travel inside the envelope's artifacts,
    # never printed directly to stdout ahead of it.
    ship_envelope = parse_json_stdout(shipped)
    assert ship_envelope["ok"] is False
    assert ship_envelope["error"]["code"] == "FLX-8001"
    assert ship_envelope["artifacts"]["events"][0]["type"] == "com.agentics.flux.ship.withheld"


def test_json_flag_is_honoured_after_the_subcommand(bundle_dir: Path, tmp_path: Path) -> None:
    """B1/M1: ``--json`` is not global-position-only."""
    result = run_flux(["validate", str(bundle_dir), "--json"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    payload = parse_json_stdout(result)
    assert payload["stage"] == "validate"
    assert payload["ok"] is True


def test_apply_with_no_recorded_plan_raises_flx_2107_not_flx_2101(
    bundle_dir: Path, tmp_path: Path
) -> None:
    """H4: a missing ``plan.json`` is a distinct failure from a moved ``inputDigest``."""
    state = tmp_path / ".flux"
    result = run_flux(
        ["apply", str(bundle_dir), "--run-id", "0123456789abcdef", "--state-dir", str(state)],
        cwd=tmp_path,
    )
    assert result.returncode == 1
    assert "FLX-2107" in result.stderr
    assert "FLX-2101" not in result.stderr


def test_malformed_committed_slice_is_a_usage_error(bundle_dir: Path, tmp_path: Path) -> None:
    """M6: a malformed ``--committed-slice`` is argparse's own usage error (exit 2)."""
    state = tmp_path / ".flux"
    result = run_flux(
        [
            "run",
            str(bundle_dir),
            "--state-dir",
            str(state),
            "--committed-slice",
            "not-a-kv-pair",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 2
    assert "NAME=VALUE" in result.stderr


def test_explain_a_registered_code(tmp_path: Path) -> None:
    result = run_flux(["explain", "FLX-1001"], cwd=tmp_path)
    assert result.returncode == 0
    assert "FLX-1001" in result.stdout


def test_explain_an_unregistered_code_exits_1(tmp_path: Path) -> None:
    result = run_flux(["explain", "FLX-9999"], cwd=tmp_path)
    assert result.returncode == 1


def test_describe_self_json(tmp_path: Path) -> None:
    result = run_flux(["--json", "describe", "--self"], cwd=tmp_path)
    assert result.returncode == 0
    envelope = parse_json_stdout(result)
    payload = envelope["artifacts"]
    assert payload["component"] == "FLUX Simulation Engine"
    # M8: the command tree is present and not degraded to the error shape.
    commands = payload["commands"]
    assert "error" not in commands
    assert "verify" in commands["subcommands"]
    assert "gate" in commands["subcommands"]


def test_describe_self_human_output_has_no_python_repr(tmp_path: Path) -> None:
    """M7: no ``{'agentic': False, ...}`` leaking a dict's own ``repr``."""
    result = run_flux(["describe", "--self"], cwd=tmp_path)
    assert result.returncode == 0
    assert "{'" not in result.stdout
    assert "capabilities:" in result.stdout


def test_plugins_lists_vertical_packs(tmp_path: Path) -> None:
    result = run_flux(["--json", "plugins"], cwd=tmp_path)
    assert result.returncode == 0
    payload = parse_json_stdout(result)["artifacts"]
    assert "telco" in payload["verticalPacks"]


def test_gate_conform_runs_the_bundled_rfc04_vectors(tmp_path: Path) -> None:
    result = run_flux(["gate", "conform"], cwd=tmp_path)
    assert result.returncode == 0
    assert "RFC-04 conformance vectors passed" in result.stdout


def test_gate_decide_denial_exits_3_not_1(tmp_path: Path) -> None:
    """A policy denial is not a command failure -- distinct exit codes.

    Uses the bundled RFC-04 conformance vector "deny: purposeLimitation
    ABSENT defaults to true" verbatim, so the request is one this engine's
    own conformance suite already asserts denies.
    """
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "policy": {"allowedModels": ["m1"], "tokenBudget": 1000},
                "request": {"model": "m1", "useCase": "u", "tokens": 10},
            }
        ),
        encoding="utf-8",
    )
    result = run_flux(["--json", "gate", "decide", "--request", str(request)], cwd=tmp_path)
    payload = parse_json_stdout(result)
    assert payload["artifacts"]["allow"] is False
    assert payload["artifacts"]["reasonCode"] == "PURPOSE_REQUIRED"
    assert result.returncode == 3
    assert payload["exitCode"] == 3


def test_replay_assert_digest_against_a_real_run(bundle_dir: Path, tmp_path: Path) -> None:
    state = tmp_path / ".flux"
    applied = run_flux(
        ["--json", "apply", str(bundle_dir), "--state-dir", str(state)], cwd=tmp_path
    )
    assert applied.returncode == 0, applied.stderr
    payload = parse_json_stdout(applied)
    run_id = payload["artifacts"]["run_id"]
    run_digest = payload["artifacts"]["receipt"]["runDigest"]

    replayed = run_flux(
        [
            "replay",
            str(bundle_dir),
            "--run-id",
            run_id,
            "--assert-digest",
            run_digest,
            "--state-dir",
            str(state),
        ],
        cwd=tmp_path,
    )
    assert replayed.returncode == 0, replayed.stderr

    mismatched = run_flux(
        [
            "replay",
            str(bundle_dir),
            "--run-id",
            run_id,
            "--assert-digest",
            "sha256:" + "0" * 64,
            "--state-dir",
            str(state),
        ],
        cwd=tmp_path,
    )
    assert mismatched.returncode == 1
    assert "FLX-2106" in mismatched.stderr
