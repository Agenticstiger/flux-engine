# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The on-disk layout of the outcome-accuracy state, and the writes into it.

The seal is the integrity anchor of the whole outcome-accuracy claim: a
prediction is digested with its ``registered_at`` and written down before
the outcome is known, so any later edit produces a different digest rather
than a quietly updated one. That write used to live inside the CLI's own
command handlers -- presentation-layer code -- so the layout, the
containment check and the sealed-record bytes were unreachable to a
library host, and a second consumer had to re-derive all three by reading
argparse handlers.

This module owns them instead. It is deliberately small and total:

* :class:`AccuracyStore` knows the two directories and nothing about
  argparse, exit codes or output rendering;
* every path it returns has been checked to resolve inside the state
  directory (``FLX-7003``), so a prediction id that tried to climb out of
  it is refused before anything is written;
* the bytes it writes are the same sorted-key JSON the CLI wrote, so a
  state directory written by an earlier build reads back unchanged.

The CLI now parses arguments and prints results; the layout lives here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from flux_engine.accuracy.records import seal_prediction
from flux_engine.api.accuracy import (
    FLX_7003,
    AccuracyVerdict,
    PredictionRecord,
    require_identifier,
)
from flux_engine.core.digest import DigestRef
from flux_engine.core.errors import FluxError

__all__ = ["DEFAULT_STATE_DIR", "AccuracyStore"]

#: Where the state lives when a caller names no directory -- the same
#: default every other stage uses.
DEFAULT_STATE_DIR = ".flux"


def _write_json(path: Path, document: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(document), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


@dataclass(frozen=True)
class AccuracyStore:
    """``<state_dir>/accuracy/``: sealed predictions and the verdicts on them.

    ``predictions/<predictionId>.json`` holds the caller's own document
    plus the ``sealDigest`` taken over it; ``verdicts/<predictionId>.json``
    holds the outcome of scoring it. One directory per kind, one file per
    prediction id, so a prediction and its verdict are found by the same
    name.
    """

    root: Path

    @classmethod
    def for_state_dir(cls, state_dir: Union[str, Path, None]) -> "AccuracyStore":
        """The store under *state_dir* (default ``./.flux``)."""
        return cls(root=Path(state_dir or DEFAULT_STATE_DIR) / "accuracy")

    @property
    def predictions_dir(self) -> Path:
        return self.root / "predictions"

    @property
    def verdicts_dir(self) -> Path:
        return self.root / "verdicts"

    def contained(self, path: Path) -> Path:
        """Refuse (``FLX-7003``) a path that resolves outside this store.

        Defence in depth behind :func:`~flux_engine.api.accuracy.require_identifier`:
        the identifier grammar already refuses a separator, and this refuses
        anything that nevertheless resolves out of the tree.
        """
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise FluxError(FLX_7003, f"{path} escapes {self.root}")
        return resolved

    def prediction_path(self, prediction_id: str) -> Path:
        """The sealed-record path for *prediction_id*, checked for containment."""
        require_identifier(prediction_id, field_name="prediction_id")
        return self.contained(self.predictions_dir / f"{prediction_id}.json")

    def verdict_path(self, prediction_id: str) -> Path:
        """The verdict path for *prediction_id*, checked for containment."""
        require_identifier(prediction_id, field_name="prediction_id")
        return self.contained(self.verdicts_dir / f"{prediction_id}.json")

    def register(
        self, prediction: PredictionRecord, document: Mapping[str, Any]
    ) -> Tuple[Path, DigestRef]:
        """Seal *prediction* and write *document* plus its ``sealDigest``.

        *document* is the caller's own record as it was read, so the file on
        disk is what was submitted rather than a re-rendering of it; the
        digest is taken over the model, not over the file.
        """
        # The path -- identifier grammar and containment -- before anything
        # is created, so a refused prediction id leaves no directory behind.
        path = self.prediction_path(prediction.prediction_id)
        digest = seal_prediction(prediction)
        self.predictions_dir.mkdir(parents=True, exist_ok=True)
        _write_json(path, {**document, "sealDigest": digest.value})
        return path, digest

    def read_prediction(self, prediction_id: str) -> Optional[Dict[str, Any]]:
        """The sealed record for *prediction_id*, or ``None`` when none is registered."""
        path = self.prediction_path(prediction_id)
        if not path.is_file():
            return None
        document: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return document

    def write_verdict(self, verdict: AccuracyVerdict) -> Path:
        """Write the outcome of scoring one prediction, and return its path."""
        self.verdicts_dir.mkdir(parents=True, exist_ok=True)
        path = self.verdict_path(verdict.prediction_id)
        return _write_json(
            path,
            {
                "prediction_id": verdict.prediction_id,
                "metric_id": verdict.metric_id,
                "outcome": verdict.outcome.value,
                "basis": verdict.basis.value,
            },
        )

    def read_verdict(self, prediction_id: str) -> Optional[Dict[str, Any]]:
        """The recorded verdict for *prediction_id*, or ``None``."""
        path = self.verdict_path(prediction_id)
        if not path.is_file():
            return None
        document: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return document

    def documents_registered_at(self, registered_at: str) -> List[Dict[str, Any]]:
        """Every sealed record whose ``registered_at`` is exactly *registered_at*.

        Exact string equality, not an instant comparison: ``registered_at``
        is the sealed prediction's own spelling of the moment, and two
        spellings of one instant are two registrations as far as the seal is
        concerned.
        """
        if not self.predictions_dir.is_dir():
            return []
        matched: List[Dict[str, Any]] = []
        for path in sorted(self.predictions_dir.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("registered_at") == registered_at:
                matched.append(document)
        return matched
