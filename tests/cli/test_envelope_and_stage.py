# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``--json`` envelope on the three paths that did not print one.

* a classified ``FLX-6nnn`` expression failure reported ``code: null``,
  because ``FluxExprError`` is not a ``FluxError`` and fell into the
  generic arm;
* ``fluxsim --version --json`` printed a bare version string, and every
  usage error (exit 2) printed argparse's usage block with **zero bytes**
  on stdout, so a wrapper that always parses stdout crashed on a flag
  typo;
* a malformed ``--signing-key`` raised ``SystemExit`` -- a
  ``BaseException`` -- from inside a command function, which sailed past
  the CLI's own safety net entirely: exit 1 (a run failure) for what is a
  usage error, and nothing at all on stdout.

Plus the discriminator: ``stage`` must not change depending on whether
the command worked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine import __version__
from flux_engine.cli import main

pytestmark = pytest.mark.unit

FIXTURE_BUNDLE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "telco-fidelity-small"


def _envelope(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.out.strip(), "no envelope was printed to stdout"
    return json.loads(captured.out)


# --- a classified error carries its code -------------------------------------


def test_an_expression_error_reports_its_flx_code(capsys) -> None:
    exit_code = main(
        [
            "expr",
            "eval",
            "tenure_months > 12",
            "--bundle",
            str(FIXTURE_BUNDLE),
            "--values",
            '{"tenure_months": 24}',
            "--json",
        ]
    )
    envelope = _envelope(capsys)
    assert exit_code == 1
    assert envelope["error"]["code"] == "FLX-6004"
    assert not envelope["error"]["message"].startswith("UnknownPathError")


def test_an_unclassified_failure_still_reports_null(tmp_path: Path, capsys) -> None:
    """The control: a code-less failure must not borrow a code from anywhere."""
    request = tmp_path / "bad.json"
    request.write_text("not json {", encoding="utf-8")
    assert main(["gate", "decide", "--request", str(request), "--json"]) == 1
    assert _envelope(capsys)["error"]["code"] is None


# --- --version and usage errors print the envelope too -----------------------


def test_version_json_is_an_envelope(capsys) -> None:
    assert main(["--version", "--json"]) == 0
    envelope = _envelope(capsys)
    assert envelope["stage"] == "version"
    assert envelope["ok"] is True
    assert envelope["artifacts"]["version"] == __version__
    assert envelope["error"] is None


def test_version_without_json_is_still_the_bare_string(capsys) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_a_usage_error_prints_an_envelope_with_exit_code_2(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["expr", "eval", "1 + 1", "--json"])
    assert excinfo.value.code == 2
    envelope = _envelope(capsys)
    assert envelope["exitCode"] == 2
    assert envelope["ok"] is False
    assert envelope["stage"] == "expr-eval"
    assert "--bundle" in envelope["error"]["message"]


def test_a_usage_error_without_json_stays_argparses_own_output(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["expr", "eval", "1 + 1"])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == "", "the human path must not gain a JSON blob"
    assert "usage:" in captured.err


def test_a_malformed_signing_key_is_a_usage_error_with_an_envelope(tmp_path: Path, capsys) -> None:
    key = tmp_path / "badkey"
    key.write_bytes(b"not-a-hex-key-at-all")
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "ship",
                str(FIXTURE_BUNDLE),
                "--run-id",
                "nope",
                "-o",
                str(tmp_path / "out"),
                "--signing-key",
                str(key),
                "--json",
            ]
        )
    assert excinfo.value.code == 2, "a bad key file is a usage error, not a failed ship"
    envelope = _envelope(capsys)
    assert envelope["exitCode"] == 2
    assert "--signing-key" in envelope["error"]["message"]


def test_a_short_signing_key_is_refused_by_length(tmp_path: Path, capsys) -> None:
    key = tmp_path / "shortkey"
    key.write_bytes(b"ab" * 4)  # valid hex, 4 bytes decoded
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "ship",
                str(FIXTURE_BUNDLE),
                "--run-id",
                "nope",
                "-o",
                str(tmp_path / "out"),
                "--signing-key",
                str(key),
                "--json",
            ]
        )
    assert excinfo.value.code == 2
    assert "32 bytes" in _envelope(capsys)["error"]["message"]


# --- the discriminator does not move -----------------------------------------


def test_the_stage_is_the_same_whether_the_command_worked(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"
    good = tmp_path / "p.json"
    good.write_text(
        json.dumps(
            {
                "prediction_id": "p-1",
                "run_digest": "sha256:" + "0" * 64,
                "metric_id": "recovery_rate",
                "statement": "rate rises",
                "predicted": "12",
                "horizon_end": "2026-10-01T00:00:00Z",
                "registered_at": "2026-09-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    assert (
        main(["accuracy", "register", "--file", str(good), "--state-dir", str(state), "--json"])
        == 0
    )
    success_stage = _envelope(capsys)["stage"]

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"prediction_id": "p-2"}), encoding="utf-8")
    assert (
        main(["accuracy", "register", "--file", str(bad), "--state-dir", str(state), "--json"]) == 1
    )
    failure_stage = _envelope(capsys)["stage"]

    assert success_stage == failure_stage == "accuracy-register"


def test_a_failing_score_is_not_confusable_with_a_failing_register(tmp_path: Path, capsys) -> None:
    """Both failures used to report `stage: "accuracy"` -- the top-level verb."""
    state = tmp_path / "state"
    prediction = tmp_path / "p.json"
    prediction.write_text(
        json.dumps(
            {
                "prediction_id": "p-1",
                "run_digest": "sha256:" + "0" * 64,
                "metric_id": "recovery_rate",
                "statement": "rate rises",
                "predicted": "12",
                "horizon_end": "2026-10-01T00:00:00Z",
                "registered_at": "2026-09-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    assert main(["accuracy", "register", "--file", str(prediction), "--state-dir", str(state)]) == 0
    capsys.readouterr()
    readout = tmp_path / "r.json"
    readout.write_text(json.dumps({"prediction_id": "p-1"}), encoding="utf-8")
    assert (
        main(
            [
                "accuracy",
                "score",
                "--prediction-id",
                "p-1",
                "--readout",
                str(readout),
                "--state-dir",
                str(state),
                "--json",
            ]
        )
        == 1
    )
    assert _envelope(capsys)["stage"] == "accuracy-score"


# --- one flag, one spelling, either position ---------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "describe", "--self"],
        ["describe", "--self", "--json"],
        ["-j", "describe", "--self"],
        ["describe", "--self", "-j"],
    ],
    ids=["--json before", "--json after", "-j before", "-j after"],
)
def test_the_json_flag_is_accepted_in_either_position_in_either_spelling(argv, capsys) -> None:
    """``-j`` was accepted after the verb and rejected before it.

    The long form already worked either side of the subcommand, so the
    short form being a usage error in one of the two positions was a
    difference between two spellings of one flag, with nothing behind it.
    """
    assert main(argv) == 0
    assert _envelope(capsys)["stage"] == "describe"
