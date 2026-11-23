# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Caller-supplied JSON documents fail with a field list, not a ``KeyError``.

``accuracy register --file``, ``accuracy score --readout`` and ``gate
decide --request`` indexed the parsed document with bare
``document["key"]`` lookups and built their enums with bare
``Enum(value)``, so a user filling in a prediction document learned one
missing field per run-and-crash cycle -- with a Python ``KeyError`` as
the whole diagnostic -- and ``ValueError: 'holdout' is not a valid
ClaimBasis`` withheld the three legal values it had just checked against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.cli import main
from flux_engine.cli._documents import (
    GATE_REQUEST_FIELDS,
    PREDICTION_FIELDS,
    field_list_help,
    problems_in,
    readout_fields,
)

pytestmark = pytest.mark.unit


def _write(path: Path, document: object) -> str:
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def _envelope(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.out.strip(), "no envelope was printed to stdout"
    return json.loads(captured.out)


# --- every problem at once, never one per run --------------------------------


def test_every_missing_field_is_reported_in_one_message() -> None:
    problems = problems_in({"prediction_id": "p-1"}, PREDICTION_FIELDS)
    missing = [p for p in problems if "missing required field" in p]
    assert len(missing) == 6, problems
    assert not any("interval_low" in p for p in problems), "optional fields are not required"


def test_a_closed_set_reports_its_legal_values() -> None:
    document = {
        "prediction_id": "p-1",
        "metric_id": "m",
        "observed": "1",
        "sample_size": 10,
        "basis": "holdout",
        "observed_at": "2026-10-02T00:00:00Z",
    }
    problems = problems_in(document, readout_fields())
    assert len(problems) == 1
    assert (
        "live-ab" in problems[0] and "historical-holdout" in problems[0] and "none" in problems[0]
    )


def test_a_wrong_type_is_named_with_the_type_it_got() -> None:
    problems = problems_in({"policy": [], "request": {}}, GATE_REQUEST_FIELDS)
    assert problems == ("policy must be a JSON object, got list",)


# --- through the real CLI ----------------------------------------------------


def test_register_reports_flx_7005_and_the_missing_fields(tmp_path: Path, capsys) -> None:
    document = _write(tmp_path / "bad.json", {"prediction_id": "p-1"})
    exit_code = main(
        ["accuracy", "register", "--file", document, "--state-dir", str(tmp_path / "s"), "--json"]
    )
    envelope = _envelope(capsys)
    assert exit_code == 1
    assert envelope["error"]["code"] == "FLX-7005"
    assert "run_digest" in envelope["error"]["message"]
    assert "KeyError" not in envelope["error"]["message"]


def test_a_missing_file_is_a_sentence_not_an_errno(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "accuracy",
            "register",
            "--file",
            str(tmp_path / "absent.json"),
            "--state-dir",
            str(tmp_path / "s"),
            "--json",
        ]
    )
    envelope = _envelope(capsys)
    assert exit_code == 1
    # FLX-7006, not FLX-7005: a file that does not exist has no shape, so
    # the code that says "does not have the required shape" would
    # contradict its own detail line and send the reader to check fields of
    # a document they have not written.
    assert envelope["error"]["code"] == "FLX-7006"
    assert "no PredictionRecord at" in envelope["error"]["message"]
    assert "--file" in envelope["error"]["message"]
    assert "FileNotFoundError" not in envelope["error"]["message"]


def test_score_reports_the_legal_basis_values(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"
    prediction = _write(
        tmp_path / "p.json",
        {
            "prediction_id": "p-1",
            "run_digest": "sha256:" + "0" * 64,
            "metric_id": "recovery_rate",
            "statement": "rate rises",
            "predicted": "12",
            "horizon_end": "2026-10-01T00:00:00Z",
            "registered_at": "2026-09-01T00:00:00Z",
        },
    )
    assert main(["accuracy", "register", "--file", prediction, "--state-dir", str(state)]) == 0
    capsys.readouterr()
    readout = _write(
        tmp_path / "r.json",
        {
            "prediction_id": "p-1",
            "metric_id": "recovery_rate",
            "observed": "11",
            "sample_size": 10,
            "basis": "holdout",
            "observed_at": "2026-10-02T00:00:00Z",
        },
    )
    exit_code = main(
        [
            "accuracy",
            "score",
            "--prediction-id",
            "p-1",
            "--readout",
            readout,
            "--state-dir",
            str(state),
            "--json",
        ]
    )
    envelope = _envelope(capsys)
    assert exit_code == 1
    assert envelope["error"]["code"] == "FLX-7005"
    assert "historical-holdout" in envelope["error"]["message"]


def test_gate_decide_names_the_fields_it_wanted(tmp_path: Path, capsys) -> None:
    request = _write(tmp_path / "req.json", {})
    exit_code = main(["gate", "decide", "--request", request, "--json"])
    envelope = _envelope(capsys)
    assert exit_code == 1
    assert envelope["stage"] == "gate-decide"
    assert "policy" in envelope["error"]["message"]
    assert "request" in envelope["error"]["message"]
    assert "KeyError" not in envelope["error"]["message"]


# --- the shape is discoverable from --help -----------------------------------


def test_the_field_list_reaches_the_flags_help(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["accuracy", "register", "--help"])
    out = capsys.readouterr().out
    for field in PREDICTION_FIELDS:
        assert field.name in out, field.name


def test_field_list_help_marks_optional_fields_and_closed_sets() -> None:
    text = field_list_help(readout_fields())
    assert "live-ab|historical-holdout|none" in text
    assert "[optional]" in field_list_help(PREDICTION_FIELDS)
