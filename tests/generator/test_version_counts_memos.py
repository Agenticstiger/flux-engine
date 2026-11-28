# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The memos keyed by a plan digest hold how a history gives its version counts.

A plan's digest holds ``versionCounts`` only for a history with a ``versions`` shape, since without
a shape both modes spread the rows evenly. A memo kept under that digest still holds the mode, so
the layout one plan built is never served to a plan that sets the other, in the plan's reach as for
the plan itself.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict

import pytest

from flux_engine.generator.relations import run_set_key, version_counts_of
from flux_engine.generator.rows import GeneratorPlan
from flux_engine.seam.history import VERSION_COUNTS_FIT, VERSION_COUNTS_RANK

from .test_event_projection import EMIT, events_plan
from .test_history_end import plans_for
from .test_history_tracked import BINS, tracked_entry
from .test_projection_plan import fresh_sources  # noqa: F401  (an autouse fixture)

pytestmark = pytest.mark.unit


def _plans(mode: str, **options: object) -> Dict[str, GeneratorPlan]:
    """The plans of a history under *mode*, with no ``versions`` shape unless *options* give one."""
    return plans_for(
        tracked_entry(**{"versions": None, "version_counts": mode, **options}),
        rows=40,
        entities=12,
        priced=True,
    )


def test_two_plans_of_one_digest_that_differ_only_in_the_mode_have_two_run_set_keys() -> None:
    fit, rank = (_plans(mode)[EMIT] for mode in (VERSION_COUNTS_FIT, VERSION_COUNTS_RANK))
    assert fit.digest() == rank.digest()
    assert run_set_key(fit) != run_set_key(rank)
    assert dict(version_counts_of(fit)) == {EMIT: VERSION_COUNTS_FIT}
    assert dict(version_counts_of(rank)) == {EMIT: VERSION_COUNTS_RANK}


def test_a_shaped_history_has_two_digests_and_two_run_set_keys() -> None:
    fit, rank = (
        _plans(mode, versions=BINS)[EMIT] for mode in (VERSION_COUNTS_FIT, VERSION_COUNTS_RANK)
    )
    assert fit.digest() != rank.digest()
    assert run_set_key(fit) != run_set_key(rank)


def test_a_plan_that_sums_a_history_holds_the_mode_of_the_history() -> None:
    """The mode is read through the child plans a plan sums, as through the plans it reads."""
    parents = []
    for mode in (VERSION_COUNTS_FIT, VERSION_COUNTS_RANK):
        plans = _plans(mode)
        parents.append(replace(plans["customer"], sums=((EMIT, "r", plans[EMIT]),)))
    first, second = parents
    assert first.digest() == second.digest()
    assert run_set_key(first) != run_set_key(second)
    assert dict(version_counts_of(first)) == {EMIT: VERSION_COUNTS_FIT}


def test_a_projection_holds_the_mode_of_the_history_it_reads() -> None:
    first, second = (_plans(mode) for mode in (VERSION_COUNTS_FIT, VERSION_COUNTS_RANK))
    fit, rank = events_plan(first), events_plan(second)
    assert fit.digest() == rank.digest()
    assert run_set_key(fit) != run_set_key(rank)
    assert dict(version_counts_of(fit)) == {EMIT: VERSION_COUNTS_FIT}
