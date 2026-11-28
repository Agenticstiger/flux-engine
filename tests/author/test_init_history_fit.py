# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A gym's membership table whose version counts do not follow the first start verifies (ADR 0017, the amendment of 2026-11-28).

``tests/fixtures/gym-membership-history`` is a contract and the release of a table of 5000 members. A member
joins every three days from 2019-01-01 and holds one to ten versions, each lasting 30 to 120 days, so the count of
versions a member holds says nothing of the day the member joined: the rank correlation of the first start and
the count is about 0. Under the rank coupling the members who joined first took nearly all the long chains, and
the histogram of the validity start over versions leaned toward the early years, past the margin the release
declares. Each member now takes a count its room fits, so the start's histogram is the release's and the
correlation stays near the source's.

A table whose counts do follow the room, long chains cut at ``until``, keeps a strongly negative correlation.
"""

from __future__ import annotations

import json
import random
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pytest

from flux_engine.cli._init import run_init

from ._written import emitted_rows

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "gym-membership-history"
TABLE = "membership_version"
MEMBERS = 5000


def _ranks(values: Sequence[float]) -> "np.ndarray":
    """The average rank of each value, ties sharing the mean of their places."""
    array = np.asarray(values, dtype=float)
    _, inverse, counts = np.unique(array, return_inverse=True, return_counts=True)
    ends = np.cumsum(counts)
    return (ends - (counts - 1) / 2.0)[inverse]


def spearman(first: Sequence[float], second: Sequence[float]) -> float:
    one, two = _ranks(first), _ranks(second)
    one, two = one - one.mean(), two - two.mean()
    return float((one * two).sum() / np.sqrt((one * one).sum() * (two * two).sum()))


def _poisson(draw: random.Random, mean: float) -> int:
    limit, found, product = np.exp(-mean), 0, draw.random()
    while product > limit:
        found += 1
        product *= draw.random()
    return found


def source_counts(seed: int = 7) -> Tuple[List[int], List[int]]:
    """Each member's first start (a day number) and count of versions, as the release was made from."""
    draw = random.Random(seed)
    starts = [
        (date(2019, 1, 1) + timedelta(days=3 * member)).toordinal() for member in range(MEMBERS)
    ]
    return starts, [min(1 + _poisson(draw, 2.0), 10) for _ in range(MEMBERS)]


def _generated(tmp_path: Path) -> Tuple[List[int], List[int]]:
    shutil.copy(FIXTURE / "contract.fluid.yaml", tmp_path / "membership.fluid.yaml")
    shutil.copy(FIXTURE / "profile.json", tmp_path / "membership.profile.json")
    initialised = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "membership.fluid.yaml"),
        profile=str(tmp_path / "membership.profile.json"),
    )
    assert initialised.exit_code == 0, initialised.stderr
    # Names the columns in the contract's order: member_id, valid_from, valid_to, ...
    held = emitted_rows(tmp_path, TABLE, "membership.profile.json", verify=True)
    by_member: Dict[Any, List[str]] = {}
    for row in held:
        by_member.setdefault(row[0], []).append(row[1])
    firsts = [date.fromisoformat(min(starts)).toordinal() for starts in by_member.values()]
    return firsts, [len(starts) for starts in by_member.values()]


def test_counts_that_do_not_follow_the_start_verify_and_keep_the_sources_correlation(
    tmp_path: Path,
) -> None:
    starts, counts = _generated(tmp_path)
    assert len(counts) == MEMBERS
    source_starts, source_versions = source_counts()
    assert abs(spearman(source_starts, source_versions)) < 0.05
    assert abs(spearman(starts, counts) - spearman(source_starts, source_versions)) < 0.1


def test_the_fixture_is_a_release_of_a_table_with_a_history() -> None:
    document = json.loads((FIXTURE / "profile.json").read_text("utf-8"))
    table = document["tables"][TABLE]
    assert table["rowCount"] > MEMBERS and "history" in table
