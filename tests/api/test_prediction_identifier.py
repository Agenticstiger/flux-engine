# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""FLX-7003: a prediction id is one safe path component (the FLUX identifier grammar)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from flux_engine.api.accuracy import PredictionRecord, require_identifier
from flux_engine.core.digest import DigestDomain, DigestRef
from flux_engine.core.errors import FluxError


def _record(prediction_id: str) -> PredictionRecord:
    return PredictionRecord(
        prediction_id=prediction_id,
        run_digest=DigestRef(
            value="sha256:" + "0" * 64, domain=DigestDomain.JCS_DOCUMENT, subject="runDigest"
        ),
        metric_id="telco/saved_customers",
        statement="x",
        predicted=Decimal("1"),
        horizon_end="2026-10-01T00:00:00Z",
        registered_at="2026-09-01T00:00:00Z",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad", ["../escape", "a/b", "/abs", ".hidden", "", "trailing.", "sp ace", "a\\b"]
)
def test_unsafe_prediction_ids_are_refused_at_construction(bad: str) -> None:
    with pytest.raises(FluxError) as err:
        _record(bad)
    assert err.value.code.code == "FLX-7003"


@pytest.mark.unit
@pytest.mark.parametrize("good", ["p1", "pred-2026.09_a", "X", "a.b-c_d"])
def test_safe_prediction_ids_are_accepted(good: str) -> None:
    assert _record(good).prediction_id == good
    assert require_identifier(good, field_name="prediction_id") == good
