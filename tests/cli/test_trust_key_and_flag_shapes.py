# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``--trust-key``, ``--assert-digest`` and ``--committed-slice``: the flags themselves.

``--trust-key`` did not exist. ``ship`` and ``run --ship`` nevertheless
told the operator to pass it -- in the terminal and, verbatim, in the
``EVIDENCE.md`` that ships inside the evidence archive -- so the remedy
the tool printed could not be executed, and the one signature state that
is a statement about who signed, rather than about the bytes, was
unreachable from every shipped interface.

It then existed on ``ship`` and ``run`` and not on ``verify`` -- the
command ``ship`` itself names as the one a reader outside the process
runs -- so the anchor was reachable only from the two commands that
write. All three carry it now, and this module parametrises over all
three so a fourth writer cannot be added without one.

The other two cases here are the same class of defect one step smaller: a
flag whose value is checked too late, so a typo comes back as a verdict
about the run rather than as a usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

import flux_engine.stages.replay  # noqa: F401 - imported so sys.modules holds the module
import flux_engine.stages.run  # noqa: F401
import flux_engine.stages.ship  # noqa: F401
import flux_engine.stages.verify  # noqa: F401
from flux_engine.api.stages import StageResult
from flux_engine.cli import build_parser, main

pytestmark = pytest.mark.unit


def _flags(parser: argparse.ArgumentParser, command: str) -> Dict[str, argparse.Action]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            sub = action.choices[command]
            return {name: a for a in sub._actions for name in a.option_strings}
    raise AssertionError("no subparsers")


#: Every command that can be given a trust anchor. ``verify`` was the
#: omission: ``ship`` prints ``fluxsim verify`` as the command a reader runs
#: to reach the same conclusion from the pack, and ``fluxsim verify --help``
#: listed no ``--trust-key`` at all, so the one signature state that says
#: *who* signed was reachable from ``ship`` and from nowhere else.
_TRUST_KEY_COMMANDS = ["ship", "run", "verify"]


@pytest.mark.parametrize("command", _TRUST_KEY_COMMANDS)
def test_the_command_that_names_trust_key_also_accepts_it(command: str) -> None:
    assert "--trust-key" in _flags(build_parser(), command)


@pytest.mark.parametrize("command", _TRUST_KEY_COMMANDS)
def test_the_trust_key_carrier_also_accepts_the_signing_key_it_derives_from(
    command: str,
) -> None:
    """``--signing-key`` alone is a trust anchor too: the public half of the seed."""
    assert "--signing-key" in _flags(build_parser(), command)


@pytest.mark.parametrize("command", _TRUST_KEY_COMMANDS)
def test_trust_key_reads_raw_bytes_and_hex_alike(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value a caller has is the ``publicKey`` hex a record prints."""
    raw_file = tmp_path / "trust.raw"
    raw_file.write_bytes(bytes(range(32)))
    hex_file = tmp_path / "trust.hex"
    hex_file.write_text(bytes(range(32)).hex(), encoding="utf-8")

    seen: Dict[str, Any] = {}

    def recorder(*_args: object, **kwargs: object) -> StageResult:
        seen.update(kwargs)
        return StageResult(stage=command, exit_code=0, stdout="ok")

    # `import flux_engine.stages.ship as m` binds the *function* the
    # package re-exports, not the module; go through sys.modules.
    monkeypatch.setattr(sys.modules["flux_engine.stages.ship"], "ship", recorder)
    monkeypatch.setattr(sys.modules["flux_engine.stages.run"], "run", recorder)
    monkeypatch.setattr(sys.modules["flux_engine.stages.verify"], "verify", recorder)

    for key_file in (raw_file, hex_file):
        seen.clear()
        argv = [command, "tests/fixtures/telco-fidelity-small", "--trust-key", str(key_file)]
        if command == "ship":
            argv += ["--run-id", "0123456789abcdef", "-o", str(tmp_path / "out")]
        if command == "verify":
            argv += ["--run-id", "0123456789abcdef"]
        assert main(argv) == 0
        assert seen["trust_key"] == bytes(range(32))


def test_a_trust_key_of_the_wrong_length_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    short = tmp_path / "short.hex"
    short.write_text("00ff", encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        main(["ship", "b", "--run-id", "r", "-o", "o", "--trust-key", str(short)])
    assert exit_info.value.code == 2
    assert "32 bytes" in capsys.readouterr().err


# ── --assert-digest ────────────────────────────────────────────────────


def test_a_bare_hex_digest_is_normalised_rather_than_reported_as_a_regression(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Losing the ``sha256:`` prefix is a copy-paste slip, not a moved digest."""
    digest = "9" * 64
    seen: Dict[str, Any] = {}

    def recorder(*_args: object, **kwargs: object) -> StageResult:
        seen.update(kwargs)
        return StageResult(stage="replay", exit_code=0, stdout="ok")

    monkeypatch.setattr(sys.modules["flux_engine.stages.replay"], "replay", recorder)
    assert main(["replay", str(tmp_path), "--run-id", "r", "--assert-digest", digest]) == 0
    assert seen["assert_digest"] == f"sha256:{digest}"


def test_a_value_that_is_not_a_digest_is_a_usage_error_not_a_verdict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["replay", "b", "--run-id", "r", "--assert-digest", "sha256:deadbeef"])
    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert "--assert-digest" in error
    assert "FLX-2106" not in error


def test_an_unknown_digest_algorithm_is_refused() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["replay", "b", "--run-id", "r", "--assert-digest", f"md5:{'a' * 64}"])
    assert exit_info.value.code == 2


# ── --committed-slice ──────────────────────────────────────────────────


def test_a_bare_path_committed_slice_is_the_same_usage_error_as_a_missing_equals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One flag, one exit code for a malformed value.

    A mistyped expose id exited 2 and a bare path exited 1, for two
    spellings of one mistake in one flag.
    """
    for value in ("payment_recovery_moment", "payment_recovery_moment=/tmp/slice.ndjson"):
        with pytest.raises(SystemExit) as exit_info:
            main(["ship", "b", "--run-id", "r", "-o", "o", "--committed-slice", value])
        assert exit_info.value.code == 2, value
    assert "file://" in capsys.readouterr().err


def test_a_file_url_committed_slice_still_parses() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "ship",
            "b",
            "--run-id",
            "r",
            "-o",
            "o",
            "--committed-slice",
            "payment_recovery_moment=file:///tmp/slice.ndjson",
        ]
    )
    assert args.committed_slice == ["payment_recovery_moment=file:///tmp/slice.ndjson"]
