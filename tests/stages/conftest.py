# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Fixtures shared by the stages test suite.

Uses this repo's own ``tests/fixtures/telco-fidelity`` copy of the FLUX
spec repo's telco example (with a SEAM-FIDELITY-SPEC §2 fidelity block
added -- see that directory's own comments) so the whole suite runs
without the sibling ``../FLUX`` checkout the *other* work items' tests
need. Every count these tests assert on is read back from the pipeline's
own output, never typed in twice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

pytestmark = pytest.mark.integration


@pytest.fixture()
def bundle_dir() -> Path:
    assert FIXTURE_BUNDLE.is_dir(), FIXTURE_BUNDLE
    return FIXTURE_BUNDLE


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / ".flux"


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_committed_slice(path: Path, *, amount_offset: str = "0") -> str:
    """A synthetic (never real-data) committed slice for the fidelity check."""
    from decimal import Decimal

    markets = ("IE", "GB", "FR", "DE")
    rows = [
        {
            "subscriber_id": f"sub-{index:05d}",
            "aborted_at": f"2026-07-{(index % 28) + 1:02d}T09:00:00.000Z",
            "amount": str(Decimal(amount_offset) + Decimal(25 * (index % 400) + 10)),
            "market": markets[index % len(markets)],
        }
        for index in range(200)
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return f"file://{path}"
