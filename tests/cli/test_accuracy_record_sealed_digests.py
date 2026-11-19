# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim accuracy record`` publishes the digests ``register`` sealed.

``accuracy register`` seals each prediction, interval included, and returns
its ``sealDigest``. The published accuracy record lists one digest per
covered prediction (``predictionDigests``) so a reader can match the record
to the seals. ``accuracy record`` used to rebuild each prediction *without*
its interval before digesting it, so for every prediction that declared an
interval the published digest was not the sealed one, and the record could
not be matched to anything that was registered.

Found by the accuracy program's recovery tests (docs/accuracy/DESIGN.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.cli import main

pytestmark = pytest.mark.unit

_REGISTERED_AT = "2026-09-01T00:00:00Z"


def _prediction(prediction_id: str, **interval: Any) -> Dict[str, Any]:
    return {
        "prediction_id": prediction_id,
        "run_digest": "sha256:" + "0" * 64,
        "metric_id": "recovery_rate",
        "statement": "the rate lands in the interval",
        "predicted": "12",
        "horizon_end": "2026-10-01T00:00:00Z",
        "registered_at": _REGISTERED_AT,
        **interval,
    }


def _register(tmp_path: Path, state: Path, document: Dict[str, Any]) -> str:
    path = tmp_path / f"{document['prediction_id']}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert main(["accuracy", "register", "--file", str(path), "--state-dir", str(state)]) == 0
    stored = json.loads(
        (state / "accuracy" / "predictions" / f"{document['prediction_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    return str(stored["sealDigest"])


def test_the_record_lists_exactly_the_sealed_digests(tmp_path: Path) -> None:
    state = tmp_path / "state"
    sealed: List[str] = [
        _register(tmp_path, state, _prediction("p-strings", interval_low="10", interval_high="14")),
        _register(tmp_path, state, _prediction("p-numbers", interval_low=10, interval_high=14.5)),
        _register(tmp_path, state, _prediction("p-point")),
    ]
    out = tmp_path / "accuracy.json"
    assert (
        main(
            [
                "accuracy",
                "record",
                "--registered-at",
                _REGISTERED_AT,
                "--out",
                str(out),
                "--state-dir",
                str(state),
            ]
        )
        == 0
    )
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["predictionDigests"] == sorted(sealed)
