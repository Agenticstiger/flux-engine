# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's weight and bin-edge texts are bounded: a long text is refused, never a raw error."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.core.errors import FluxError
from tests.seam.test_history import ENTRY, bundle_with, entry_with, resolved

pytestmark = pytest.mark.unit

#: Long enough to pass the 64-character bound's neighbour and to meet Python's int digit limit.
_LONG = "1" * 4301
_JUST_OVER = "1" * 65


def _refused(tmp_path: Path, entry: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as caught:
        resolved(bundle_with(tmp_path, "long", [entry]))
    assert caught.value.code.code == "FLX-3403", str(caught.value)
    return str(caught.value)


@pytest.mark.parametrize("text", [_LONG, _JUST_OVER])
def test_a_long_weight_text_is_refused(tmp_path: Path, text: str) -> None:
    versions = {"bins": ["1", "2", "4"], "binWeights": [text, "0.4"]}
    message = _refused(tmp_path, entry_with(versions=versions))
    assert "history[0]" in message and "weight" in message
    assert len(message) < 400


@pytest.mark.parametrize("text", [_LONG, _JUST_OVER])
def test_a_long_bin_edge_text_is_refused(tmp_path: Path, text: str) -> None:
    length = {**ENTRY["length"], "bins": ["30", text]}
    message = _refused(tmp_path, entry_with(length=length))
    assert "history[0]" in message and "bins" in message
    assert len(message) < 400


def test_a_weight_of_sixty_four_characters_still_reads(tmp_path: Path) -> None:
    text = "0." + "5" * 62
    versions = {"bins": ["1", "2", "4"], "binWeights": [text, "0.4"]}
    resolved(bundle_with(tmp_path, "edge", [entry_with(versions=versions)]))
