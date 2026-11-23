# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's emit takes no exact allocation, since its values follow the entity (ADR 0017 section 6).

Pinned here:

* ``init`` on a contract whose small history no other expose reads writes no ``allocation: exact``
  on it, says why as a note, and the bundle plans;
* a column of a history's emit under ``allocation: exact`` is ``FLX-3403`` at plan time;
* an expose named as one whose values follow the entity is given no allocation, and is noted.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator

import pytest
import yaml

from flux_engine.author.columns import ColumnPlan
from flux_engine.author.exact import allot_small_tables
from flux_engine.stages.plan import plan

from .test_history_init import _init, _profile

pytestmark = pytest.mark.integration


def _planned(tmp_path: Path) -> Any:
    return plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "plans.profile.json",
        kernel="0.2.0",
        stage=False,
    )


def _column_maps(node: Any) -> Iterator[Dict[str, Any]]:
    if isinstance(node, dict):
        if "plan_id" in node and "tier" in node:
            yield node
        for value in node.values():
            yield from _column_maps(value)
    elif isinstance(node, list):
        for value in node:
            yield from _column_maps(value)


def test_init_on_a_small_history_no_other_expose_reads_writes_no_exact_and_plans(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _profile())
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    assert "allocation: exact" not in text
    assert "plan_version: allocation: a history's columns follow the entity" in result.stdout
    outcome = _planned(tmp_path)
    assert outcome.success, outcome.stderr


def test_a_column_of_a_history_emit_under_exact_allocation_is_refused(tmp_path: Path) -> None:
    _init(tmp_path, _profile())
    path = tmp_path / "bundle" / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    (columns,) = list(_column_maps(document))
    columns["tier"]["allocation"] = "exact"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    outcome = _planned(tmp_path)
    assert not outcome.success
    assert "FLX-3403" in outcome.stderr
    assert "plan_version" in outcome.stderr and "'tier'" in outcome.stderr


def _small_expose(expose_id: str) -> Any:
    column = ColumnPlan(
        name="tier",
        declaration={"levels": ["a", "b"], "levelWeights": [1, 1]},
        properties=[],
        how="",
    )
    return SimpleNamespace(
        expose_id=expose_id,
        rows=4,
        columns=[column],
        kinds={},
        derives={},
        dependents=set(),
        copies={},
    )


def test_an_expose_that_follows_its_entity_is_given_no_allocation_and_is_noted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import flux_engine.author.exact as exact

    monkeypatch.setattr(exact, "is_small", lambda *args, **kwargs: True)
    monkeypatch.setattr(exact, "_exact_names", lambda expose, by_name: {"tier"})
    followed, plain = _small_expose("followed"), _small_expose("plain")
    notes = allot_small_tables([followed, plain], {"followed": "its values follow the entity"})
    assert "allocation" not in followed.columns[0].declaration
    assert plain.columns[0].declaration["allocation"] == "exact"
    assert notes == [("followed: allocation", "its values follow the entity")]
