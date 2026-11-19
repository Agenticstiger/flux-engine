# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Replay every committed counterexample on every pull request.

A case is a JSON file under ``regressions/``::

    {"property": "<name in checks.CHECKS>", "input": {...}, "tier": "...", "baseSeed": 0,
     "note": "why it is here", "xfail": {"reason": "...", "raises": "decimal.InvalidOperation"}}

``tier`` and ``baseSeed`` are the provenance a counterexample file already carries (a
hand-written case says ``"hand-written"`` and ``0``); the case runs the same way at
both tiers, because everything the check needs is in ``input``. A case with ``xfail``
is a *reproduced engine defect*: it is ``xfail(strict=True, raises=<that exception>)``
(``raises`` is a name from the ``RAISES`` allow-list), so any other failure, a broken setup
included (``HarnessError``), fails the test instead of passing as the defect, and the
day the engine is fixed the replay fails and says to drop the key.

Counterexamples reach this directory from ``FLUX_ACCURACY_COUNTEREXAMPLES`` (see
``counterexamples.py``): copy the file in, and add a ``note``.
"""

from __future__ import annotations

import decimal
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from tests.accuracy.harness.record import ResultLog
from tests.accuracy.harness.tier import Tier
from tests.accuracy.properties._support import HarnessError
from tests.accuracy.properties.checks import CHECKS

FAMILY = "properties"
DIRECTORY = Path(__file__).with_name("regressions")
CASES: List[Path] = sorted(DIRECTORY.glob("*.json"))


#: The exceptions a known defect may be pinned by. A name in a case file is looked up here,
#: never evaluated.
RAISES: Dict[str, type] = {
    "AssertionError": AssertionError,
    "decimal.InvalidOperation": decimal.InvalidOperation,
}


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _params() -> List[Any]:
    if not CASES:
        return [pytest.param(None, id="none-committed-yet")]
    params = []
    for path in CASES:
        marks = []
        pinned = _load(path).get("xfail")
        if pinned:
            marks.append(
                pytest.mark.xfail(
                    strict=True, reason=pinned["reason"], raises=RAISES[pinned["raises"]]
                )
            )
        params.append(pytest.param(path, id=path.stem, marks=marks))
    return params


@pytest.mark.parametrize("path", _params())
def test_regression_case_replays(path: Path, tier: Tier, accuracy_log: ResultLog) -> None:
    if path is None:
        pytest.skip("no regression cases are committed under tests/accuracy/properties/regressions")
    case = _load(path)
    if case["property"] not in CHECKS:
        raise HarnessError(f"{path.name}: unknown property {case['property']!r}")
    result = None
    try:
        result = CHECKS[case["property"]](case["input"])
        if result is False:
            raise HarnessError(f"{path.name}: the case is vacuous (nothing was compared)")
    finally:
        accuracy_log.record(
            family=FAMILY,
            case=f"regression-{path.stem}",
            criterion=f"the committed counterexample for {case['property']} passes",
            passed=result is not None and result is not False,
            metrics={"property": case["property"], "knownDefect": bool(case.get("xfail"))},
        )


def test_every_property_has_a_regression_case() -> None:
    covered = {_load(path)["property"] for path in CASES}
    missing = sorted(set(CHECKS) - covered)
    assert not missing, f"no committed regression case for: {', '.join(missing)}"
