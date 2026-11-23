# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``verify`` reads each emit once, in named steps, and the pool changes no byte.

The small telco bundle is planned and applied with its own profile pinned
(:mod:`tests.stages.test_scorecard_stages`'s fixtures). Held here:

* every emit's file is decoded once, however many legs read its rows;
* the verdict, the artifacts and every line of output are the same for one job and for
  several;
* the pool is made in one place, :func:`flux_engine.stages.verify._map_in_pool`, keeps the
  order of its tasks, and is not made for one job.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List

import pytest

from . import test_scorecard_stages as _scorecard
from .test_scorecard_stages import Pinned

# The small telco bundle's fixtures, shared with the scorecard stage tests.
unpinned = _scorecard.unpinned
compiled_and_rows = _scorecard.compiled_and_rows
own_profile = _scorecard.own_profile
own_pinned = _scorecard.own_pinned

pytestmark = pytest.mark.unit

V = importlib.import_module("flux_engine.stages.verify")


def _everything(result: Any) -> Dict[str, Any]:
    return {
        "exit": result.exit_code,
        "artifacts": repr(result.artifacts),
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def test_each_emit_is_decoded_once_for_every_leg_that_reads_it(
    own_pinned: Pinned, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: List[str] = []
    real = V._decoded_rows

    def counted(path: Any, expose_id: str) -> Any:
        calls.append(expose_id)
        return real(path, expose_id)

    monkeypatch.setattr(V, "_decoded_rows", counted)
    result = own_pinned.verify(profile=own_pinned.profile)
    assert result.exit_code == 0, result.stderr
    assert result.artifacts["scorecard"] is not None
    assert sorted(calls) == sorted(set(calls)), "an emit was decoded more than once"
    assert calls, "the emit was never decoded"


def test_the_same_bytes_for_any_number_of_jobs(own_pinned: Pinned) -> None:
    one = _everything(own_pinned.verify(profile=own_pinned.profile))
    several = _everything(own_pinned.verify(profile=own_pinned.profile, jobs=3))
    assert one == several
    assert one["exit"] == 0


def _double(task: int) -> int:
    return task * 2


def test_the_pool_keeps_the_order_of_its_tasks() -> None:
    assert V._map_in_pool(_double, [3, 1, 2], 1) == [6, 2, 4]  # type: ignore[arg-type]
    assert V._map_in_pool(_double, [3, 1, 2, 5], 2) == [6, 2, 4, 10]  # type: ignore[arg-type]


def test_one_job_makes_no_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    import concurrent.futures

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a pool was made for one job")

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", refused)
    assert V._map_in_pool(_double, [1, 2], 1) == [2, 4]  # type: ignore[arg-type]
    assert V._map_in_pool(_double, [7], 4) == [14]  # type: ignore[arg-type]
