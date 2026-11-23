# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` points at the dates how-to when it writes ``lifetimes`` or ``within`` entries."""

from __future__ import annotations

from typing import Any, Dict

import pytest
import yaml

NAMESPACE = "com.agentics.flux/1"


def _bundle_with(block: Dict[str, Any]) -> Any:
    from types import SimpleNamespace

    data = yaml.safe_dump({"extensions": {NAMESPACE: block}}).encode("utf-8")
    return SimpleNamespace(files=[SimpleNamespace(name="simulation.flux.yml", data=data)])


@pytest.mark.parametrize("key", ["lifetimes", "within"])
def test_init_points_at_the_dates_page_when_it_writes_lifetimes_or_within(key: str) -> None:
    from flux_engine.cli._init import _dates_pointer

    (line,) = _dates_pointer(_bundle_with({key: [{"emit": "a"}]}))
    assert "docs/how-to/keep-dates-inside-their-parents-lifetime.md" in line


def test_init_prints_no_pointer_for_a_bundle_with_no_dates() -> None:
    from flux_engine.cli._init import _dates_pointer

    assert _dates_pointer(_bundle_with({"emits": {}})) == []
