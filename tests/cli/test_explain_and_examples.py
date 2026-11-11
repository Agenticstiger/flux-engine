# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim explain`` as a discovery surface, and help examples that run.

Three defects, one theme: the CLI told the reader something it then would
not honour.

* ``fluxsim explain flx-3105`` answered "not a registered FLX code" for a
  code the catalog holds, and there was no way to see which codes exist
  short of grepping the source.
* ``fluxsim accuracy record --help`` prescribed RFC 3339 and then printed an
  example the command rejects.
* a missing input file was reported under ``FLX-7005`` ("does not have the
  required shape"), so the code and the detail contradicted each other in
  one line -- a file that does not exist has no shape.
"""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from flux_engine.cli import build_parser, main

pytestmark = pytest.mark.unit


# ── fluxsim explain ───────────────────────────────────────────────────────


def test_a_lowercase_code_explains_the_same_entry(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["explain", "flx-3105"]) == 0
    lowered = capsys.readouterr().out
    assert main(["explain", "FLX-3105"]) == 0
    assert capsys.readouterr().out == lowered


def test_surrounding_whitespace_is_not_a_different_code() -> None:
    assert main(["explain", "  FLX-3105 "]) == 0


def test_list_prints_every_registered_code(capsys: pytest.CaptureFixture[str]) -> None:
    from flux_engine.core.errors import catalog, ensure_core_codes_loaded

    assert main(["explain", "--list"]) == 0
    printed = capsys.readouterr().out
    ensure_core_codes_loaded()
    entries = catalog()
    assert len(entries) > 20
    for entry in entries:
        assert entry.code in printed
        assert entry.title in printed


def test_list_under_json_carries_code_family_and_title(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--json", "explain", "--list"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    codes = envelope["artifacts"]["codes"]
    assert {"code", "family", "title"} == set(codes[0])


def test_an_unregistered_code_points_at_the_listing(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["explain", "FLX-9999"]) == 1
    assert "fluxsim explain --list" in capsys.readouterr().err


def test_explain_with_neither_a_code_nor_list_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["explain"]) == 2
    assert "--list" in capsys.readouterr().err


# ── the missing-input code ─────────────────────────────────────────────


def test_a_missing_prediction_file_is_not_reported_as_a_shape_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["accuracy", "register", "--file", str(tmp_path / "nope.json")]) == 1
    error = capsys.readouterr().err
    assert "FLX-7006" in error
    assert "FLX-7005" not in error
    assert "--file" in error


def test_a_document_that_exists_and_is_wrong_still_reports_the_shape_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = tmp_path / "prediction.json"
    document.write_text(json.dumps({"prediction_id": "p-1"}), encoding="utf-8")
    assert main(["accuracy", "register", "--file", str(document)]) == 1
    assert "FLX-7005" in capsys.readouterr().err


# ── every example in the help actually parses ──────────────────────────


def _commands_of(text: str) -> List[str]:
    """The commands in an example's text: a trailing backslash joins the next line."""
    commands: List[str] = []
    pending = ""
    for line in text.split("\n"):
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1].strip() + " "
            continue
        commands.append((pending + stripped.strip()).strip())
        pending = ""
    return [command for command in commands if command]


def _examples(
    parser: argparse.ArgumentParser, prog: str
) -> Iterator[Tuple[str, argparse.ArgumentParser, List[str]]]:
    for example in getattr(parser, "examples", ()):
        for command in _commands_of(example.command):
            if command.startswith("fluxsim "):  # `cd demo` is a shell line, not ours
                yield prog, parser, shlex.split(command)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _examples(sub, f"{prog} {name}")


_EXAMPLES = list(_examples(build_parser(), "fluxsim"))


def test_there_are_examples_to_check() -> None:
    assert len(_EXAMPLES) > 10


@pytest.mark.parametrize("prog,parser,argv", _EXAMPLES, ids=[p for p, _, _ in _EXAMPLES])
def test_every_help_example_parses_against_its_own_parser(
    prog: str, parser: argparse.ArgumentParser, argv: List[str]
) -> None:
    """A worked example the command rejects is worse than none at all.

    Values are checked here too, because ``type=`` callables run at parse
    time: ``--registered-at 2026-01-01`` parsed fine and then failed at
    ``FLX-7005``, which is why this test asserts on the *documented* value
    rather than merely on the flag names.
    """
    words = argv[len(prog.split()) :]
    parser.parse_args(words)


def test_the_accuracy_record_example_is_an_instant_the_command_accepts() -> None:
    from flux_engine.cli import _require_rfc3339

    example = [example for prog, _, example in _EXAMPLES if prog == "fluxsim accuracy record"]
    assert example, "the accuracy record example moved"
    argv = example[0]
    value = argv[argv.index("--registered-at") + 1]
    assert _require_rfc3339(value, flag="--registered-at") == value
