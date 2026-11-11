# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Fixtures shared by the accuracy program (docs/accuracy/DESIGN.md).

Standard library and pytest only: the outcome-accuracy record's own tests
live in this directory too and must keep running in an environment without
the test-only ``accuracy`` dependency group. Each program module guards its
own scipy import with :func:`tests.accuracy.harness.deps.require`.
"""

from __future__ import annotations

import pytest

from tests.accuracy.harness.record import ResultLog
from tests.accuracy.harness.tier import Tier, current_tier


@pytest.fixture(scope="session")
def tier() -> Tier:
    """The tier this run was asked for (``FLUX_ACCURACY_TIER``)."""
    return current_tier()


@pytest.fixture()
def accuracy_log(request: pytest.FixtureRequest, tier: Tier) -> ResultLog:
    """The result log for the requesting test.

    A test marked ``xfail(strict=True)`` pins a known engine defect; its
    cases are logged with that reason as a known defect.
    """
    marker = request.node.get_closest_marker("xfail")
    known = None
    if marker is not None and marker.kwargs.get("strict", False):
        known = str(marker.kwargs.get("reason", "")) or "strict xfail"
    return ResultLog.for_test(request.node.nodeid, tier, known_defect=known)


def pytest_report_header(config: pytest.Config) -> str:
    """Name the tier and its base seed at the top of every run, so a log is reproducible."""
    selected = current_tier()
    return f"accuracy program: tier={selected.name} baseSeed={selected.base_seed}"
