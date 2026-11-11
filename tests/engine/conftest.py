# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The population-500 copy. What this suite proves is `asyncio.to_thread`
#: dispatch and soft-timeout semantics; neither needs 25 000 subjects, and
#: two of its tests cost 83.54s + 41.60s against the full-size bundle.
FIXTURE_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

pytestmark = pytest.mark.integration


@pytest.fixture()
def bundle_dir() -> Path:
    assert FIXTURE_BUNDLE.is_dir(), FIXTURE_BUNDLE
    return FIXTURE_BUNDLE


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / ".flux"
