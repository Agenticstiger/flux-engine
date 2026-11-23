# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Guards against a pinned golden artifact silently ballooning back up.

Before the plan-slimming fix, ``tests/golden/telco/plan.json`` alone was
16 700 065 bytes (the full per-subject seed tree inlined into the plan
document) -- a single checked-in file most of a git checkout's size. This
test is the regression guard: every JSON file pinned under
``tests/golden/`` must stay small, because a golden artifact is meant to
be a *pointer* (a digest, a masked summary document) into a run, never a
copy of the run's own bulk data.
"""

from __future__ import annotations

from pathlib import Path

GOLDEN_ROOT = Path(__file__).resolve().parent
MAX_BYTES = 200 * 1024  # 200 KB


def _pinned_json_files() -> list[Path]:
    return sorted(GOLDEN_ROOT.glob("**/*.json"))


def test_at_least_one_pinned_json_file_exists() -> None:
    assert _pinned_json_files(), f"no pinned *.json files found under {GOLDEN_ROOT}"


def test_no_pinned_golden_file_exceeds_200kb() -> None:
    oversized = {
        str(path.relative_to(GOLDEN_ROOT)): path.stat().st_size
        for path in _pinned_json_files()
        if path.stat().st_size > MAX_BYTES
    }
    assert not oversized, (
        f"pinned golden file(s) exceed the {MAX_BYTES} byte budget: {oversized} -- "
        "a golden artifact should be a digest/summary, not a copy of run data"
    )
