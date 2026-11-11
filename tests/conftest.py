# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Suite-wide collection rules.

The pull-request gate runs ``pytest -m "not slow"``; one full-suite CI job and
``make ci-local`` run everything. A test is *slow* when it materialises the
25,000-subject worlds (``tests/fixtures/telco-fidelity`` or the public FLUX
example): every golden and acceptance test does, so those directories are
marked wholesale. Keep fast tests on ``tests/fixtures/telco-fidelity-small``.

Two suites outside those directories materialised a big world as well, and
were costing the gate about 69% of its CPU on every one of the six
OS x Python matrix cells -- roughly 285 of 410 seconds, in six tests. One
of the two is now on the small bundle and is gone from the list; the other
is named in :data:`SLOW_MODULES` until its fixture follows.
``tests/stages/test_slow_marking.py`` asserts both that every entry still
exists and that the marker really lands on it, so the list cannot rot into
a lie.

A derived rule was tried first -- mark any module that names a big-world
bundle in code -- and it is recorded here because the next reader will
have the same idea: it marks five suites that only *parse* those bundles
(``tests/seam/test_resolve.py`` and friends, all fast), because no static
reading distinguishes loading a bundle's YAML from executing the world
inside it. Naming the modules is the honest version.

This file also holds the sibling-FLUX reporting boundary
(:func:`pytest_runtest_makereport`): with ``FLUX_EXAMPLES_REQUIRED`` set, a
skip whose reason names the absent ``../FLUX`` checkout is a failure,
whichever gate produced it. See ``tests/flux_examples.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pytest
from hypothesis import HealthCheck, settings

# Hypothesis measures a per-example deadline (200ms by default) and fails a
# test that exceeds it. That is a latency assertion, not a correctness one,
# and under `-n auto` it reports contention as a property violation: a whole
# local gate run failed on three property tests, one of them reporting
# "292.35ms on an initial run, 0.44ms on a subsequent one", which is the
# scheduler and not the code. Hypothesis's own message on that failure
# recommends turning the deadline off, and running property tests in parallel
# is what makes it necessary here.
#
# The example budget and shrinking are untouched: this changes when a test is
# called slow, never what it checks or how hard it looks. `too_slow` is
# suppressed for the same reason, since it is the same clock.
settings.register_profile(
    "flux",
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("flux")

#: Directories whose every test materialises a big world by construction.
SLOW_DIRS: Tuple[str, ...] = ("golden", "acceptance")

#: Modules outside those directories that materialise one, relative to
#: ``tests/``. Each entry is a module whose fixtures resolve a
#: 25,000-subject bundle:
#:
#: * ``stages/test_scenario_emit.py`` -- points at the public FLUX example.
#:
#: ``engine/test_engine_facade.py`` was the other entry until its
#: ``bundle_dir`` fixture was pointed at ``telco-fidelity-small``: what
#: that module proves is ``asyncio.to_thread`` dispatch and soft-timeout
#: semantics, neither of which needs 25 000 subjects, so the fix was the
#: fixture rather than the marker.
#:
#: One entry is slow for a different reason, and is here for the same one:
#:
#: * ``docs/test_run_pages.py`` -- runs every documentation page, and a page
#:   starts ``fluxsim`` once per command it shows. Most pages take more than the
#:   five seconds the marker is defined by, about ninety seconds in all, and the
#:   fast suite runs on every cell of the matrix, so the same pages would run six
#:   times for one answer. A pull request runs them once, in the slow shard; the
#:   scheduled and release runs, which run every slow test on each of their
#:   cells, run them there too.
SLOW_MODULES: Tuple[str, ...] = ("stages/test_scenario_emit.py", "docs/test_run_pages.py")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    tests_root = Path(__file__).resolve().parent
    for item in items:
        rel = Path(str(item.fspath)).resolve().relative_to(tests_root)
        if (rel.parts and rel.parts[0] in SLOW_DIRS) or rel.as_posix() in SLOW_MODULES:
            item.add_marker(pytest.mark.slow)


def _skip_reason(report: pytest.TestReport) -> str:
    """The reason text out of a skip report's ``(path, lineno, reason)`` triple."""
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ")
    return str(longrepr or "")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):  # type: ignore[no-untyped-def]
    """Turn a sibling-FLUX skip into a failure while ``FLUX_EXAMPLES_REQUIRED`` is set.

    ``tests/flux_examples.py`` describes this hook as the single place the
    guarantee is enforced, and for a while it described a hook that was
    not here: eight gates hand-roll their own ``pytest.skip`` instead of
    calling the helpers, so under CI and no sibling checkout they still
    skipped -- about 40 tests, the real-binary six-step acceptance success
    path among them -- and the suite still reported green.

    Enforced at the reporting boundary rather than at each call site on
    purpose: rewriting eight gates closes eight instances of the hole, not
    the hole, because a ninth hand-rolled gate reopens it. Whatever
    produced the skip, if the reason names the sibling checkout
    (:func:`~tests.flux_examples.is_flux_examples_skip_reason`) and the
    flag is set, it is a failure.
    """
    report = yield
    from tests.flux_examples import (
        REQUIRED_ENV_VAR,
        examples_are_required,
        is_flux_examples_skip_reason,
    )

    # `wasxfail` rides on a skipped report too, and an expected failure is
    # not a gate that vanished.
    if report.skipped and not hasattr(report, "wasxfail") and examples_are_required():
        reason = _skip_reason(report)
        if is_flux_examples_skip_reason(reason):
            report.outcome = "failed"
            report.longrepr = f"{REQUIRED_ENV_VAR} is set but {reason}"
    return report
