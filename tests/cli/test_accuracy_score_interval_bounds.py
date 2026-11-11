# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim accuracy score`` keeps every registered interval bound, zero included.

``accuracy register`` accepts a decimal field as a JSON number or as its
string spelling, and the store keeps the submitted document verbatim. The
score command used to rebuild the registered interval with a truthiness
test, ``... if document.get("interval_low") else None``, so a bound
registered as the JSON number ``0`` (or ``0.0``) came back as *no bound*:
the prediction was scored as having no interval and the verdict was
``not-scored`` instead of ``within-interval`` or ``outside-interval``. The
string ``"0"`` is truthy and was never affected, which is why the defect
hid behind the suite's string-spelled fixtures.

Found by the accuracy program's recovery tests (docs/accuracy/DESIGN.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from flux_engine.cli import main

pytestmark = pytest.mark.unit

_REGISTERED_AT = "2026-09-01T00:00:00Z"
_OBSERVED_AT = "2026-10-02T00:00:00Z"


def _score(tmp_path: Path, low: Any, high: Any, observed: Any) -> str:
    state = tmp_path / "state"
    prediction = tmp_path / "prediction.json"
    prediction.write_text(
        json.dumps(
            {
                "prediction_id": "p-zero",
                "run_digest": "sha256:" + "0" * 64,
                "metric_id": "recovery_rate",
                "statement": "the rate stays inside the interval",
                "predicted": "5",
                "horizon_end": "2026-10-01T00:00:00Z",
                "registered_at": _REGISTERED_AT,
                "interval_low": low,
                "interval_high": high,
            }
        ),
        encoding="utf-8",
    )
    assert main(["accuracy", "register", "--file", str(prediction), "--state-dir", str(state)]) == 0
    readout = tmp_path / "readout.json"
    readout.write_text(
        json.dumps(
            {
                "prediction_id": "p-zero",
                "metric_id": "recovery_rate",
                "observed": observed,
                "sample_size": 100,
                "basis": "historical-holdout",
                "observed_at": _OBSERVED_AT,
            }
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "accuracy",
                "score",
                "--prediction-id",
                "p-zero",
                "--readout",
                str(readout),
                "--state-dir",
                str(state),
            ]
        )
        == 0
    )
    verdict = json.loads(
        (state / "accuracy" / "verdicts" / "p-zero.json").read_text(encoding="utf-8")
    )
    return str(verdict["outcome"])


@pytest.mark.parametrize(
    ("low", "high", "observed", "expected"),
    [
        pytest.param(0, 10, 5, "within-interval", id="number-zero-low-inside"),
        pytest.param(0, 10, 11, "outside-interval", id="number-zero-low-outside"),
        pytest.param(0.0, 10, 0, "within-interval", id="float-zero-low-on-the-boundary"),
        pytest.param(-5, 0, -1, "within-interval", id="number-zero-high-inside"),
        pytest.param(-5, 0, 1, "outside-interval", id="number-zero-high-outside"),
        pytest.param("0", "10", 5, "within-interval", id="string-zero-low"),
    ],
)
def test_a_zero_bound_is_a_bound(
    tmp_path: Path, low: Any, high: Any, observed: Any, expected: str
) -> None:
    assert _score(tmp_path, low, high, observed) == expected
