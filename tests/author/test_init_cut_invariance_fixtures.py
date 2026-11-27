# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A release cut by the unused list gives the whole profile's bundle, for every fixture pair.

``fluxsim init`` lists the statistics no rule reads. Dropping them from the profile is meant to
give the same bundle as the whole profile. This runs that over every contract and every profile
under ``tests/fixtures`` that ``init`` accepts together, cut two ways: by the list the bundle
names, and by the ledger's statistics in the state unused as a release drops them
(:func:`tests.author._cut.release_units`).

It checks the pairs the repository holds, not every profile: a statistic read by a shape no fixture
holds is not found here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest

from ._cut import broken_cuts

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CONTRACTS = sorted(
    {path for pattern in ("*.fluid.yml", "*.fluid.yaml") for path in FIXTURES.rglob(pattern)}
)
PROFILES = sorted(path for path in FIXTURES.rglob("*profile*.json") if path.is_file())


def _document(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def test_the_fixtures_hold_pairs_to_cut() -> None:
    assert len(CONTRACTS) >= 10 and len(PROFILES) >= 10


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda path: str(path.relative_to(FIXTURES)))
def test_a_release_cut_by_the_unused_list_gives_the_bundle_of_the_whole_profile(
    contract: Path, tmp_path: Path
) -> None:
    broken: List[str] = []
    for number, profile in enumerate(PROFILES):
        document = _document(profile)
        if not isinstance(document, dict):
            continue
        found = broken_cuts(tmp_path / str(number), contract, document)
        broken += [f"{profile.relative_to(FIXTURES)}: {cut}" for cut in found]
    assert broken == []
