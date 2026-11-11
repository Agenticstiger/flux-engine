# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The result log: one JSON line per measured case, joined into the nightly artefact.

A test asserts its pass criterion *and* records what it measured, so the
report page can show the number behind a green result, not only the green.
Recording is off unless ``FLUX_ACCURACY_RESULTS_DIR`` names a directory; the
pull-request tier only asserts.

Each pytest-xdist worker writes its own file (``results-<worker>.jsonl``), so
no two processes ever append to one file. ``tests/accuracy/tools/artefact.py``
joins them.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Optional

from tests.accuracy.harness.tier import Tier

__all__ = ["RESULTS_ENV", "RESULT_SCHEMA", "ResultLog", "jsonable"]

RESULTS_ENV = "FLUX_ACCURACY_RESULTS_DIR"
RESULT_SCHEMA = "flux-accuracy-result/1"


def jsonable(value: Any) -> Any:
    """*value* in a form ``json.dumps`` accepts without losing what it says.

    Exact values stay exact: a ``Decimal`` or a ``Fraction`` becomes its
    string spelling rather than a rounded float. A non-finite float becomes
    a string too, because JSON has no spelling for it.
    """
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, (Decimal, Fraction)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if hasattr(value, "item"):  # a numpy scalar
        return jsonable(value.item())
    if hasattr(value, "tolist"):  # a numpy array
        return jsonable(value.tolist())
    return str(value)


@dataclass(frozen=True)
class ResultLog:
    """Writes one test's measured cases to the results directory, when there is one."""

    node_id: str
    tier: Tier
    directory: Optional[Path]
    #: The reason of the strict ``xfail`` the test carries, when it pins a
    #: known engine defect. Its cases are logged as known defects, not as
    #: failures, so a defect already pinned does not turn every night red;
    #: the day it is fixed the strict ``xfail`` passes, pytest fails the
    #: test, and the mark has to come off.
    known_defect: Optional[str] = None

    @classmethod
    def for_test(
        cls, node_id: str, tier: Tier, *, known_defect: Optional[str] = None
    ) -> "ResultLog":
        raw = os.environ.get(RESULTS_ENV, "").strip()
        return cls(
            node_id=node_id,
            tier=tier,
            directory=Path(raw) if raw else None,
            known_defect=known_defect,
        )

    @property
    def enabled(self) -> bool:
        """Whether this run keeps a log at all."""
        return self.directory is not None

    def record(
        self,
        *,
        family: str,
        case: str,
        criterion: str,
        passed: bool,
        metrics: Optional[Mapping[str, Any]] = None,
        seed: Optional[int] = None,
        n: Optional[int] = None,
    ) -> None:
        """Append one measured case. A no-op when logging is off."""
        if self.directory is None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        line = {
            "schema": RESULT_SCHEMA,
            "family": family,
            "case": case,
            "test": self.node_id,
            "tier": self.tier.name,
            "baseSeed": self.tier.base_seed,
            "seed": seed,
            "n": n,
            "criterion": criterion,
            "passed": bool(passed),
            "knownDefect": self.known_defect,
            "metrics": jsonable(dict(metrics or {})),
        }
        with (self.directory / f"results-{worker}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
