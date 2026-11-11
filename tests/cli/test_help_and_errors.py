# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a person reads: help that leads with examples, errors that say where to read more.

The conventions are clig.dev's: examples first, a link to the docs, a next step
after success, a suggestion after a typo, and an error that points at its own
explanation. Each is held here on the real parser and the real commands.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import List, Tuple

import pytest

import flux_engine.cli as cli_module
from flux_engine._links import DOCS_URL, ISSUES_URL, command_url, error_url
from flux_engine.cli import build_parser, main
from flux_engine.starter import scaffold

pytestmark = pytest.mark.unit


def _help(argv: List[str], capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as raised:
        main([*argv, "--help"])
    assert raised.value.code == 0
    return capsys.readouterr().out


# --- help ------------------------------------------------------------------------


def test_every_help_screen_puts_the_examples_before_the_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands = build_parser()._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    for name in commands:
        out = _help([name], capsys)
        assert "\nexamples:\n" in out, name
        assert out.index("\nexamples:\n") < out.index("\noptions:\n"), name
        assert (
            out.index("\nexamples:\n") < out.index("\narguments:\n")
            if "\narguments:" in out
            else True
        )


def test_an_example_is_printed_verbatim_and_never_wrapped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help(["replay"], capsys)
    digest = "sha256:42d923fd6f4b8c0a1e7d59b2c8a4f36e0d15927acb3e8f6410d2b7c95e8a3f01"
    assert f"      --assert-digest {digest}\n" in out
    assert "  fluxsim replay ./bundle --run-id de72fa4b22711c52 \\\n" in out


def test_a_long_example_continues_with_a_backslash_so_it_can_be_pasted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help(["ship"], capsys)
    assert (
        "  fluxsim ship ./bundle --run-id de72fa4b22711c52 -o ./out \\\n"
        "      --committed-slice payment_recovery_moment=file:///data/slice.ndjson\n"
    ) in out


def test_the_top_level_help_starts_with_the_first_run_and_ends_with_where_to_go(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help([], capsys)
    assert "fluxsim init demo\n  cd demo\n  fluxsim run . --verify --ship -o out\n" in out
    assert out.rstrip().endswith(f"docs: {command_url()}\nissues: {ISSUES_URL}")


def test_the_command_list_has_no_duplicate_metavar_header(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _help([], capsys)
    assert "\ncommands:\n  init " in out
    assert "<command>\n    init" not in out


# --- errors -------------------------------------------------------------------------


def _bundle_without_a_run(tmp_path: Path) -> Tuple[Path, Path]:
    bundle = tmp_path / "demo"
    scaffold(bundle)
    return bundle, tmp_path / "state"


def test_a_catalogued_failure_points_at_its_explanation_and_its_page(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, state = _bundle_without_a_run(tmp_path)
    code = main(["verify", str(bundle), "--run-id", "0123456789abcdef", "--state-dir", str(state)])
    err = capsys.readouterr().err
    assert code == 1
    assert "FLX-2109" in err
    assert f"more: fluxsim explain FLX-2109  {error_url('FLX-2109')}\n" in err


def test_the_json_envelope_is_left_alone_by_the_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, state = _bundle_without_a_run(tmp_path)
    main(
        ["--json", "verify", str(bundle), "--run-id", "0123456789abcdef", "--state-dir", str(state)]
    )
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["error"]["code"] == "FLX-2109"
    assert set(envelope["error"]) == {"code", "message"}
    assert "more:" not in captured.err


def test_a_failure_with_no_code_says_how_to_see_more_and_where_to_report_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(args: object) -> int:
        raise RuntimeError("something nobody catalogued")

    monkeypatch.delenv("FLUX_DEBUG", raising=False)
    monkeypatch.setattr(cli_module, "_cmd_plugins", boom)
    code = main(["plugins"])
    err = capsys.readouterr().err
    assert code == 1
    assert "RuntimeError: something nobody catalogued\n" in err
    assert (
        f"more: run again with FLUX_DEBUG=1 for the traceback, or report it at {ISSUES_URL}" in err
    )


def test_explain_ends_with_the_page_for_the_code(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["explain", "FLX-1001"]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith(error_url("FLX-1001"))
    assert out.startswith("FLX-1001  [canonicalisation]")


def test_explain_with_no_code_shows_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["explain"]) == 2
    assert "for example `fluxsim explain FLX-3105`" in capsys.readouterr().err


# --- a typo, a missing argument -----------------------------------------------------


def test_a_near_miss_on_a_command_name_names_the_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["validte", "."])
    assert raised.value.code == 2
    assert "did you mean `fluxsim validate`?" in capsys.readouterr().err


def test_a_nested_near_miss_names_the_nested_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["accuracy", "scor"])
    assert "did you mean `fluxsim accuracy score`?" in capsys.readouterr().err


def test_a_missing_argument_shows_a_worked_example(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["init"])
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "the following arguments are required: directory" in err
    assert err.rstrip().endswith("for example: fluxsim init demo")


def test_a_usage_error_under_json_is_still_one_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--json", "validte", "."])
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exitCode"] == 2
    assert "did you mean" not in envelope["error"]["message"]


def test_a_flag_typo_after_a_command_blames_that_command_not_the_program(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["validate", ".", "--bogus"])
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "usage: fluxsim validate" in err
    assert "fluxsim validate: error: unrecognized arguments: --bogus" in err
    assert err.rstrip().endswith("for example: fluxsim validate ./bundle")
    assert "fluxsim init demo" not in err


@pytest.mark.parametrize(
    "command,example",
    [
        ("gate", "fluxsim gate conform"),
        ("accuracy", "fluxsim accuracy register --file prediction.json"),
        ("expr", "fluxsim expr model --bundle ./bundle"),
        ("describe", "fluxsim describe --self --json"),
    ],
)
def test_a_command_used_without_what_it_needs_shows_its_own_example(
    command: str, example: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main([command])
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert f"usage: fluxsim {command}" in err
    assert err.rstrip().endswith(f"for example: {example}")


def test_leftover_arguments_under_json_name_the_command_as_the_stage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--json", "validate", ".", "--bogus"])
    assert raised.value.code == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["stage"] == "validate"
    assert envelope["error"]["message"] == "fluxsim validate: unrecognized arguments: --bogus"


# --- what to run next ------------------------------------------------------------------


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def _on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> _Terminal:
    stream = _Terminal()
    monkeypatch.delenv("FLUX_NO_PROGRESS", raising=False)
    monkeypatch.setattr("sys.stderr", stream)
    return stream


def test_validate_suggests_running_the_whole_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _state = _bundle_without_a_run(tmp_path)
    stream = _on_a_terminal(monkeypatch)
    assert main(["validate", str(bundle)]) == 0
    assert f"next: fluxsim run {bundle} --verify --ship -o out\n" in stream.getvalue()


def test_plan_and_apply_each_name_the_step_after_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, state = _bundle_without_a_run(tmp_path)
    stream = _on_a_terminal(monkeypatch)
    assert main(["plan", str(bundle), "--state-dir", str(state)]) == 0
    run_id = capsys.readouterr().out.split("runId=")[1].split(",")[0]
    assert (
        f"next: fluxsim apply {bundle} --run-id {run_id} --state-dir {state}\n" in stream.getvalue()
    )

    stream = _on_a_terminal(monkeypatch)
    assert main(["apply", str(bundle), "--run-id", run_id, "--state-dir", str(state)]) == 0
    assert (
        f"next: fluxsim verify {bundle} --run-id {run_id} --state-dir {state}\n"
        in stream.getvalue()
    )


def test_a_finished_run_points_at_the_report_that_says_what_it_does_not_prove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, state = _bundle_without_a_run(tmp_path)
    stream = _on_a_terminal(monkeypatch)
    out = tmp_path / "out"
    assert (
        main(["run", str(bundle), "--verify", "--ship", "-o", str(out), "--state-dir", str(state)])
        == 0
    )
    assert "EVIDENCE.md: it says what this run does not prove" in stream.getvalue()


def test_a_run_that_stopped_before_shipping_names_the_step_it_stopped_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, state = _bundle_without_a_run(tmp_path)
    stream = _on_a_terminal(monkeypatch)
    assert main(["run", str(bundle), "--state-dir", str(state)]) == 0
    assert " verify " in [line for line in stream.getvalue().splitlines() if "next:" in line][0]


def test_no_next_step_off_a_terminal_under_json_or_with_the_switch_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _state = _bundle_without_a_run(tmp_path)
    assert main(["validate", str(bundle)]) == 0
    assert "next:" not in capsys.readouterr().err  # not a terminal

    stream = _on_a_terminal(monkeypatch)
    assert main(["validate", str(bundle), "--json"]) == 0
    assert "next:" not in stream.getvalue()  # a terminal, but --json

    stream = _on_a_terminal(monkeypatch)
    monkeypatch.setenv("FLUX_NO_PROGRESS", "1")
    assert main(["validate", str(bundle)]) == 0
    assert "next:" not in stream.getvalue()  # a terminal, but switched off


def test_a_failed_command_suggests_nothing_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _on_a_terminal(monkeypatch)
    assert main(["validate", str(tmp_path)]) != 0
    assert "next:" not in stream.getvalue()


def test_the_next_step_quotes_a_bundle_path_the_shell_would_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "my bundle"
    scaffold(bundle)
    stream = _on_a_terminal(monkeypatch)
    assert main(["validate", str(bundle)]) == 0
    assert f"next: fluxsim run '{bundle}' --verify --ship -o out\n" in stream.getvalue()


def test_the_docs_address_is_one_constant() -> None:
    assert DOCS_URL.endswith("/") and DOCS_URL.startswith("https://")
    assert error_url("FLX-1001") == f"{DOCS_URL}reference/errors/FLX-1001.html"
    assert cli_module.command_url("run") == f"{DOCS_URL}reference/cli/run.html"
