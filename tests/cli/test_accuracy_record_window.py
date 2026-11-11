# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim accuracy record`` covers the registration it names, and only that.

The published AccuracyRecord is an outcomes-only attestation whose whole
point is that a third party can read it. It carried a ``registered_at``
that was stamped from the flag verbatim while the predictions inside it
came from an unfiltered glob of the state directory -- so a record could
say "registered at 2024-03-03" over predictions sealed years apart, and
``--registered-at 2026-01-01`` (matching nothing, and not even a
well-formed instant) still wrote a record and exited 0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.cli import main

pytestmark = pytest.mark.unit

_EARLY = "2024-03-03T00:00:00Z"
_LATE = "2026-09-07T00:00:00Z"


def _prediction(prediction_id: str, *, metric_id: str, registered_at: str) -> dict:
    return {
        "prediction_id": prediction_id,
        "run_digest": "sha256:" + "0" * 64,
        "metric_id": metric_id,
        "statement": f"{metric_id} moves",
        "predicted": "12",
        "horizon_end": "2026-10-01T00:00:00Z",
        "registered_at": registered_at,
    }


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """A state directory holding two predictions registered at different instants."""
    state = tmp_path / "state"
    for prediction_id, metric_id, registered_at in (
        ("p-1", "recovery_rate", _LATE),
        ("p-2", "churn_rate", _EARLY),
    ):
        document = tmp_path / f"{prediction_id}.json"
        document.write_text(
            json.dumps(
                _prediction(prediction_id, metric_id=metric_id, registered_at=registered_at)
            ),
            encoding="utf-8",
        )
        assert (
            main(["accuracy", "register", "--file", str(document), "--state-dir", str(state)]) == 0
        )
    return state


def _record(out: Path) -> dict:
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["kind"] == "AccuracyRecord"
    return document


def test_only_the_predictions_registered_at_that_instant_are_covered(
    state_dir: Path, tmp_path: Path, capsys
) -> None:
    out = tmp_path / "record.json"
    assert (
        main(
            [
                "accuracy",
                "record",
                "--registered-at",
                _EARLY,
                "--out",
                str(out),
                "--state-dir",
                str(state_dir),
            ]
        )
        == 0
    )
    capsys.readouterr()
    record = _record(out)
    assert record["registered_at"] == _EARLY
    assert record["predictions_registered"] == 1, "the other registration must not travel"
    assert record["targets"] == ["churn_rate"]
    assert len(record["predictionDigests"]) == 1


def test_the_other_registration_gets_its_own_record(
    state_dir: Path, tmp_path: Path, capsys
) -> None:
    out = tmp_path / "record.json"
    assert (
        main(
            [
                "accuracy",
                "record",
                "--registered-at",
                _LATE,
                "--out",
                str(out),
                "--state-dir",
                str(state_dir),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert _record(out)["targets"] == ["recovery_rate"]


def test_a_registration_no_prediction_carries_is_refused_and_writes_nothing(
    state_dir: Path, tmp_path: Path, capsys
) -> None:
    out = tmp_path / "record.json"
    exit_code = main(
        [
            "accuracy",
            "record",
            "--registered-at",
            "2030-01-01T00:00:00Z",
            "--out",
            str(out),
            "--state-dir",
            str(state_dir),
            "--json",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert envelope["error"]["code"] == "FLX-7004"
    assert not out.exists(), "an empty-but-stamped record must never be published"


def test_a_registered_at_that_is_not_rfc_3339_is_refused(
    state_dir: Path, tmp_path: Path, capsys
) -> None:
    out = tmp_path / "record.json"
    exit_code = main(
        [
            "accuracy",
            "record",
            "--registered-at",
            "2026-01-01",
            "--out",
            str(out),
            "--state-dir",
            str(state_dir),
            "--json",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert envelope["error"]["code"] == "FLX-7005"
    assert not out.exists()
