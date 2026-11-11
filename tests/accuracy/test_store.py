# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The accuracy state layout, containment and seal -- in the package that owns them.

All three lived inside the CLI's command handlers: the directory names,
the "this path must stay inside the state directory" check and the bytes
of the sealed record were built by presentation-layer code, so the seal --
the integrity anchor of the whole outcome-accuracy claim -- was unreachable
to any library consumer, and a second consumer would have had to re-derive
the layout from argparse handlers.

These tests exercise the store directly. ``tests/cli`` still covers the
same behaviour through the real binary, which is what pins the move as
behaviour-preserving.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from flux_engine.accuracy.records import seal_prediction
from flux_engine.accuracy.store import AccuracyStore
from flux_engine.api.accuracy import AccuracyOutcome, AccuracyVerdict, PredictionRecord
from flux_engine.api.claims import ClaimBasis
from flux_engine.core.digest import DigestDomain, DigestRef
from flux_engine.core.errors import FluxError

pytestmark = pytest.mark.unit


def _prediction(prediction_id: str = "p-1", registered_at: str = "2026-09-01T00:00:00Z"):
    return PredictionRecord(
        prediction_id=prediction_id,
        run_digest=DigestRef(
            value="sha256:" + "0" * 64, domain=DigestDomain.JCS_DOCUMENT, subject="runDigest"
        ),
        metric_id="telco/saved_customers",
        statement="saved customers rise",
        predicted=Decimal("12"),
        horizon_end="2026-10-01T00:00:00Z",
        registered_at=registered_at,
    )


def _document(prediction: PredictionRecord) -> dict:
    return {
        "prediction_id": prediction.prediction_id,
        "run_digest": prediction.run_digest.value,
        "metric_id": prediction.metric_id,
        "statement": prediction.statement,
        "predicted": str(prediction.predicted),
        "horizon_end": prediction.horizon_end,
        "registered_at": prediction.registered_at,
    }


def test_the_layout_is_state_dir_accuracy(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    assert store.predictions_dir == tmp_path / "accuracy" / "predictions"
    assert store.verdicts_dir == tmp_path / "accuracy" / "verdicts"


def test_the_default_state_dir_is_dot_flux() -> None:
    assert AccuracyStore.for_state_dir(None).root == Path(".flux") / "accuracy"


def test_register_writes_the_document_with_its_seal(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    prediction = _prediction()
    path, digest = store.register(prediction, _document(prediction))
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["sealDigest"] == digest.value == seal_prediction(prediction).value
    assert written["statement"] == "saved customers rise"


def test_the_seal_covers_the_registration_instant(tmp_path: Path) -> None:
    """An edited registration time is a different digest, not an updated one."""
    store = AccuracyStore.for_state_dir(tmp_path)
    first = seal_prediction(_prediction(registered_at="2026-09-01T00:00:00Z"))
    second = seal_prediction(_prediction(registered_at="2026-09-02T00:00:00Z"))
    assert first.value != second.value
    del store


def test_a_traversing_prediction_id_is_refused_before_anything_is_created(
    tmp_path: Path,
) -> None:
    """Defence in depth: the record type refuses this id too, and this
    refuses it again at the moment a path is built from it."""
    store = AccuracyStore.for_state_dir(tmp_path)
    with pytest.raises(FluxError) as raised:
        store.prediction_path("../../escaped")
    assert raised.value.code.code == "FLX-7003"
    assert not store.predictions_dir.exists()


def test_a_path_that_resolves_outside_the_store_is_refused(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    with pytest.raises(FluxError) as raised:
        store.contained(store.predictions_dir / ".." / ".." / ".." / "escaped.json")
    assert raised.value.code.code == "FLX-7003"


def test_read_prediction_returns_none_when_nothing_is_registered(tmp_path: Path) -> None:
    assert AccuracyStore.for_state_dir(tmp_path).read_prediction("p-1") is None


def test_a_verdict_is_written_beside_its_prediction(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path)
    verdict = AccuracyVerdict(
        prediction_id="p-1",
        metric_id="telco/saved_customers",
        outcome=AccuracyOutcome.WITHIN_INTERVAL,
        basis=ClaimBasis.HISTORICAL_HOLDOUT,
    )
    path = store.write_verdict(verdict)
    assert path.parent == store.verdicts_dir.resolve()
    assert store.read_verdict("p-1") == json.loads(path.read_text(encoding="utf-8"))


def test_documents_registered_at_matches_the_exact_spelling(tmp_path: Path) -> None:
    """Two spellings of one instant are two registrations, to the seal."""
    store = AccuracyStore.for_state_dir(tmp_path)
    for index, moment in enumerate(("2026-09-01T00:00:00Z", "2026-09-01T00:00:00.000Z")):
        prediction = _prediction(f"p-{index}", registered_at=moment)
        store.register(prediction, _document(prediction))
    matched = store.documents_registered_at("2026-09-01T00:00:00Z")
    assert [document["prediction_id"] for document in matched] == ["p-0"]


def test_documents_registered_at_is_empty_before_anything_is_registered(
    tmp_path: Path,
) -> None:
    assert (
        AccuracyStore.for_state_dir(tmp_path).documents_registered_at("2026-09-01T00:00:00Z") == []
    )
