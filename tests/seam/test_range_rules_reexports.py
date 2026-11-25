# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The authoring modules re-export the range grammar the kernel's ``seam`` package owns."""

from __future__ import annotations

from flux_engine.author import ranges, window
from flux_engine.seam import range_rules


def test_window_reexports_the_seam_grammar() -> None:
    assert window.declared_instants is range_rules.declared_instants
    assert window.instant_ms is range_rules.instant_ms
    assert window.WINDOW_END_LIMIT_MS == range_rules.WINDOW_END_LIMIT_MS
    assert {"declared_instants", "instant_ms", "WINDOW_END_LIMIT_MS"} <= set(window.__all__)


def test_ranges_splitter_is_the_seam_splitter() -> None:
    assert ranges._and_parts is range_rules.and_parts
