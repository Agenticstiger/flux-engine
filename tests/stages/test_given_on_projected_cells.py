# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column drawn ``given`` a column an events or periods entry sets is refused (ADR 0027).

An events or periods emit draws its own columns, and the entry then sets some of their cells from
the version a row reads (``model`` on a month, ``after_model`` on a change). A column drawn given
one of them reads the cell the draw made, not the cell the entry set, so the two would disagree
on most rows. The plan refuses it (``FLX-3403``), naming both columns, before a row exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import pipeline

from . import _fleet

pytestmark = pytest.mark.integration

#: Each projection with the column its entry sets that a new column is drawn given.
CASES = [(_fleet.PERIODIC, "model"), (_fleet.EVENTS, "after_model")]
LEVELS = ("sedan", "van", "truck")


def _given_bundle(tmp_path: Path, projection: str, parent: str, *, via: bool = False) -> Path:
    """A bundle whose *projection* declares ``tier`` drawn given *parent* (or given ``mid``)."""
    target = _fleet.bundle(tmp_path)
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    held = _fleet.expose(contract, projection)
    names = ("mid", "tier") if via else ("tier",)
    for name in names:
        held["contract"]["schema"].append({"name": name, "type": "STRING", "required": True})
    _fleet.repin(target, contract)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    columns = document["extensions"][_fleet.NAMESPACE]["emits"][projection]["fidelity"]["columns"]
    weights: Dict[str, Any] = {
        level: {"a": "1", "b": "0"} if level == "sedan" else {"a": "0", "b": "1"}
        for level in LEVELS
    }
    columns["tier" if not via else "mid"] = {"given": parent, "levelWeightsGiven": weights}
    if via:
        columns["tier"] = {
            "given": "mid",
            "levelWeightsGiven": {"a": {"x": "1", "y": "0"}, "b": {"x": "0", "y": "1"}},
        }
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


@pytest.mark.parametrize(("projection", "parent"), CASES)
def test_a_column_drawn_given_a_column_the_entry_sets_is_refused(
    tmp_path: Path, projection: str, parent: str
) -> None:
    target = _given_bundle(tmp_path, projection, parent)
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2)
    text = str(caught.value)
    assert caught.value.code.code == "FLX-3403", text
    for word in (f"({projection})", "'tier'", f"'{parent}'"):
        assert word in text, text


def test_a_column_given_a_column_that_is_given_a_set_column_is_refused(tmp_path: Path) -> None:
    target = _given_bundle(tmp_path, _fleet.PERIODIC, "model", via=True)
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2)
    text = str(caught.value)
    assert caught.value.code.code == "FLX-3403", text
    for word in ("'mid'", "'model'"):
        assert word in text, text
