# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim accuracy`` keeps the seal: register once, refuse an edited record.

Before, a second ``accuracy register`` of an id replaced the sealed
prediction and exited 0, ``accuracy score`` scored a record edited after it
was sealed, ``accuracy record`` published it with a digest recomputed from
the edit, and ``register`` wrote through a link planted at the record's
name. The store's own tests are ``tests/accuracy/test_store_integrity.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.cli import main

pytestmark = pytest.mark.unit

_REGISTERED_AT = "2026-09-01T00:00:00Z"


def _prediction(**fields: Any) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "prediction_id": "p-1",
        "run_digest": "sha256:" + "0" * 64,
        "metric_id": "recovery_rate",
        "statement": "the rate lands in the interval",
        "predicted": "12",
        "interval_low": "10",
        "interval_high": "14",
        "horizon_end": "2026-10-01T00:00:00Z",
        "registered_at": _REGISTERED_AT,
    }
    document.update(fields)
    return document


def _write(path: Path, document: Dict[str, Any]) -> str:
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def _envelope(capsys: pytest.CaptureFixture[str]) -> Dict[str, Any]:
    out = capsys.readouterr().out
    return dict(json.loads(out))


def _register(file: str, state: Path) -> int:
    return main(["accuracy", "register", "--file", file, "--state-dir", str(state), "--json"])


def _sealed(state: Path) -> Path:
    return state / "accuracy" / "predictions" / "p-1.json"


def test_registering_the_same_file_twice_succeeds_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    file = _write(tmp_path / "p.json", _prediction())
    assert _register(file, state) == 0
    first = _envelope(capsys)
    sealed_bytes = _sealed(state).read_bytes()
    assert _register(file, state) == 0
    again = _envelope(capsys)
    assert first["artifacts"]["created"] is True
    assert again["artifacts"]["created"] is False
    assert again["artifacts"]["sealDigest"] == first["artifacts"]["sealDigest"]
    assert _sealed(state).read_bytes() == sealed_bytes

    assert main(["accuracy", "register", "--file", file, "--state-dir", str(state)]) == 0
    assert "is already registered with this seal" in capsys.readouterr().out


def test_registering_another_claim_under_the_same_id_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    assert _register(_write(tmp_path / "p.json", _prediction()), state) == 0
    sealed = _envelope(capsys)["artifacts"]["sealDigest"]
    sealed_bytes = _sealed(state).read_bytes()

    assert _register(_write(tmp_path / "p2.json", _prediction(predicted="13")), state) == 1
    envelope = _envelope(capsys)
    assert envelope["error"]["code"] == "FLX-7007"
    message = envelope["error"]["message"]
    assert "'p-1'" in message and sealed in message
    assert message.count("sha256:") == 2
    assert _sealed(state).read_bytes() == sealed_bytes


def test_score_refuses_a_prediction_edited_after_it_was_sealed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    assert _register(_write(tmp_path / "p.json", _prediction()), state) == 0
    capsys.readouterr()
    record = json.loads(_sealed(state).read_text(encoding="utf-8"))
    record["predicted"] = "19"
    _sealed(state).write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    readout = _write(
        tmp_path / "r.json",
        {
            "prediction_id": "p-1",
            "metric_id": "recovery_rate",
            "observed": "12",
            "sample_size": 40,
            "basis": "historical-holdout",
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
    assert envelope["error"]["code"] == "FLX-7008"
    assert record["sealDigest"] in envelope["error"]["message"]
    assert not (state / "accuracy" / "verdicts").exists()


def test_record_refuses_a_store_holding_an_edited_prediction(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    assert _register(_write(tmp_path / "p.json", _prediction()), state) == 0
    capsys.readouterr()
    record = json.loads(_sealed(state).read_text(encoding="utf-8"))
    record["interval_high"] = "99"
    _sealed(state).write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    out = tmp_path / "accuracy.json"
    exit_code = main(
        [
            "accuracy",
            "record",
            "--registered-at",
            _REGISTERED_AT,
            "--out",
            str(out),
            "--state-dir",
            str(state),
            "--json",
        ]
    )
    assert exit_code == 1
    assert _envelope(capsys)["error"]["code"] == "FLX-7008"
    assert not out.exists()


@pytest.mark.parametrize("planted", ["accuracy", "accuracy/predictions"])
def test_record_refuses_a_dangling_link_in_place_of_a_store_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], planted: str
) -> None:
    """Not "no registered predictions": a link there is refused, not read as empty."""
    state = tmp_path / "state"
    link = state / planted
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "absent")
    exit_code = main(
        [
            "accuracy",
            "record",
            "--registered-at",
            _REGISTERED_AT,
            "--out",
            str(tmp_path / "accuracy.json"),
            "--state-dir",
            str(state),
            "--json",
        ]
    )
    envelope = _envelope(capsys)
    assert exit_code == 1
    assert envelope["error"]["code"] == "FLX-7009"
    assert f"{planted.split('/')[-1]} is a symbolic link" in envelope["error"]["message"]
    assert not (tmp_path / "absent").exists()


def test_a_link_planted_at_the_prediction_path_is_refused_and_its_target_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A link to another prediction's sealed record, inside the store, so the
    containment check (FLX-7003) passes it: register used to write through it
    and replace that record."""
    state = tmp_path / "state"
    assert _register(_write(tmp_path / "p2.json", _prediction(prediction_id="p-2")), state) == 0
    capsys.readouterr()
    victim = state / "accuracy" / "predictions" / "p-2.json"
    victim_bytes = victim.read_bytes()
    _sealed(state).symlink_to("p-2.json")
    assert _register(_write(tmp_path / "p.json", _prediction()), state) == 1
    envelope = _envelope(capsys)
    assert envelope["error"]["code"] == "FLX-7009"
    assert "is a symbolic link" in envelope["error"]["message"]
    assert victim.read_bytes() == victim_bytes
    assert _sealed(state).is_symlink()
