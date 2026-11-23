# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``--scale``, ``--shard`` and ``--timings`` are checked where the command line is read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.cli import main

BUNDLE = Path(__file__).resolve().parents[1] / "fixtures" / "related-star"

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("command", ["plan", "apply", "run"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "1e3", "1000001", "abc", ""])
def test_a_scale_that_is_not_usable_is_a_usage_error_with_its_code(
    command: str, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main([command, str(BUNDLE), f"--scale={value}"])
    assert stopped.value.code == 2
    assert "FLX-3405" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0/4", "5/4", "1/0", "x/y", "2", "1.5/2", "-1/3", ""])
def test_a_shard_that_is_not_a_share_is_a_usage_error_with_its_code(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["apply", str(BUNDLE), f"--shard={value}"])
    assert stopped.value.code == 2
    assert "FLX-3404" in capsys.readouterr().err


def test_a_run_with_timings_prints_them_and_the_json_envelope_carries_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", str(BUNDLE), "--state-dir", str(tmp_path / "a"), "--timings"]) == 0
    out = capsys.readouterr().out
    assert "timings (seconds" in out and "attestation" in out
    assert main(["-j", "run", str(BUNDLE), "--state-dir", str(tmp_path / "b"), "--timings"]) == 0
    spans = json.loads(capsys.readouterr().out)["artifacts"]["timings"]["spans"]
    assert {"load", "compile", "simulate", "attestation"} <= {s["name"] for s in spans}


def test_a_scale_on_kernel_0_1_0_is_refused_with_what_to_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["plan", str(BUNDLE), "--state-dir", str(tmp_path), "--scale", "2", "--kernel", "0.1.0"]
    )
    assert code == 1
    assert "kernel 0.2.0 only" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["apply", "--run-id", "0123456789abcdef", "--shard", "3/2"], "FLX-3404"),
        (["apply", "--run-id", "0123456789abcdef", "--shard", "0/4"], "FLX-3404"),
        (["plan", str(BUNDLE), "--scale", "0"], "FLX-3405"),
        (["run", str(BUNDLE), "--scale", "1000001"], "FLX-3405"),
    ],
)
def test_an_argument_refusal_carries_its_code_into_the_json_envelope(
    argv: list[str], code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main([*argv, "--json"])
    assert stopped.value.code == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exitCode"] == 2
    assert envelope["error"]["code"] == code
    assert code in envelope["error"]["message"]


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["apply", "--run-id", "0123456789abcdef", "--shard", "3/2"], "FLX-3404"),
        (["plan", str(BUNDLE), "--scale", "0"], "FLX-3405"),
    ],
)
def test_an_argument_refusal_points_to_its_catalog_entry(
    argv: list[str], code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(argv)
    err = capsys.readouterr().err.splitlines()
    assert any(line.startswith(f"more: fluxsim explain {code}") for line in err), err


def test_a_usage_error_with_no_code_carries_none(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["plan", "--no-such-flag", "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exitCode"] == 2 and envelope["error"]["code"] is None
    with pytest.raises(SystemExit):
        main(["plan", "--no-such-flag"])
    assert "more:" not in capsys.readouterr().err


def test_a_scale_says_it_leaves_a_table_built_from_events_as_it_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from flux_engine.starter import scaffold

    starter = tmp_path / "demo"
    scaffold(starter)
    note = "(scenario; --scale leaves a table built from events as it is)"

    def out(*argv: str) -> str:
        assert main([*argv]) == 0
        return capsys.readouterr().out

    # run: the plan is staged and apply commits the stage
    assert note in out("run", str(starter), "--state-dir", str(tmp_path / "a"), "--scale", "10")
    plain = out("run", str(starter), "--state-dir", str(tmp_path / "b"))
    assert "(scenario)" in plain and "--scale" not in plain
    # apply --run-id on a plan made without a stage generates the run itself
    out("plan", str(starter), "--state-dir", str(tmp_path / "c"), "--scale", "10", "--no-stage")
    (plan_file,) = (tmp_path / "c").glob("runs/*/plan.json")
    applied = out(
        "apply", str(starter), "--state-dir", str(tmp_path / "c"), "--run-id", plan_file.parent.name
    )
    assert note in applied


def test_a_shard_with_a_sink_says_to_apply_the_run_whole(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "apply",
            str(BUNDLE),
            "--state-dir",
            str(tmp_path),
            "--shard",
            "1/2",
            "--sink",
            "parquet:x",
        ]
    )
    err = capsys.readouterr().err
    assert code == 2
    assert "shares joined are not a run" in err
    assert "fluxsim apply --run-id RUN_ID --sink" in err and "without --shard" in err
    assert "after the shares are joined" not in err
    assert "FLX-" not in err  # a usage refusal is uncatalogued
