# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Settings for this package's tests, when they run without the engine's.

The engine's ``tests/conftest.py`` turns Hypothesis's per-example deadline
off for the same reason given there: under ``-n auto`` the deadline reports
scheduler contention as a property violation. These tests also run where
that file is never loaded (``make test-stats-alone``), so the profile is
registered here as well. The example budget and shrinking are untouched.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings

settings.register_profile(
    "flux-stats",
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("flux-stats")
