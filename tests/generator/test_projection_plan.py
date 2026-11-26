# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A projected emit's row count is derived from its history's layouts (ADR 0027 section 2).

Built through :func:`flux_engine.generator.projections.project_plan`, as the pipeline builds
them. What is pinned, on kernel 0.2.0: the count of a snapshot, ``E`` or ``E`` less the ended
entities, follows from the plan alone and is the plan's ``rows``; the plan's document records the
projection and no other plan's does; the dispatch has a kind for each generated projection; and a
column the history lacks is refused.
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator import projections
from flux_engine.generator.projections import KINDS, derived_rows, project_plan, sources_of
from flux_engine.generator.rows import GeneratorPlan, build_plan
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.history import HistoryEnd
from flux_engine.seam.projections import GENERATED_PROJECTIONS

from .test_history_end import (
    CLOSE_WITH,
    EMIT,
    FINAL,
    entry,
    fresh_layouts,  # noqa: F401  (an autouse fixture)
    history_plan,
    plans_for,
)
from .test_history_layout import SIM_ID, root_for
from .test_relations import _column, _expose, _h, _primary

pytestmark = pytest.mark.unit

SNAPSHOT = "sub_current"
COLUMNS = ("sub_id", "plan", "tier", "status", "region", "price")


@pytest.fixture(autouse=True)
def fresh_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test works its sources out afresh: they are kept per process."""
    monkeypatch.setattr(projections, "_SOURCES", {})


def snapshot_plan(
    plans: Mapping[str, GeneratorPlan], *, ended: str = "keep", seed: int = 5
) -> GeneratorPlan:
    """The plan of a snapshot of the versioned emit, as the pipeline makes it."""
    types = {"sub_id": "STRING", "region": "INT", "price": "INT"}
    expose = _expose(
        SNAPSHOT,
        [_column(name, types.get(name, "STRING"), name == "sub_id") for name in COLUMNS],
        _primary("sub", "sub_id"),
    )
    plan = build_plan(
        compile_expose(expose),
        sim_id=SIM_ID,
        seam_digest=_h(SNAPSHOT),
        rows=1,
        fidelity={"columns": STATISTICS},
        time_bounds=None,
        related={"sub_id"},
        generator_version=plans[EMIT].generator_version,
    )
    return project_plan(plan, ("snapshots", EMIT, ended), plans[EMIT], root_for(seed))


def held(
    end: Optional[HistoryEnd] = None, *, rows: int = 80, entities: int = 24, priced: bool = True
) -> Dict[str, GeneratorPlan]:
    return plans_for(entry(end=end), rows=rows, entities=entities, priced=priced)


#: What the snapshot declares for its columns: never drawn, so any statistics will do.
STATISTICS = {name: {"levels": {"a": "1"}} for name in ("plan", "tier", "status")} | {
    name: {"range": {"min": "1", "max": "9"}} for name in ("region", "price")
}


# -- the count -------------------------------------------------------------------------------------


@pytest.mark.parametrize("end", [FINAL, CLOSE_WITH])
def test_ended_drop_counts_the_entities_less_the_ended_ones(end: HistoryEnd) -> None:
    plans = held(end)
    chain = history_plan(entry(end=end), rows=80, entities=24)
    assert chain.ended is not None
    ended = int(chain.ended.sum())
    assert 0 < ended < 24
    assert snapshot_plan(plans, ended="keep").rows == 24
    assert snapshot_plan(plans, ended="drop").rows == 24 - ended


def test_ended_drop_without_an_end_keeps_every_entity() -> None:
    plans = held(None)
    assert snapshot_plan(plans, ended="drop").rows == 24


def test_the_count_follows_from_the_plan_alone() -> None:
    plans = held(FINAL)
    plan = snapshot_plan(plans, ended="drop")
    counted = derived_rows(("snapshots", EMIT, "drop"), plans[EMIT], plan, root_for())
    assert counted == plan.rows
    # No cell was drawn for it: the history's rows were never generated here.
    assert sources_of(plan, root_for()).shape == (plan.rows,)


def test_the_snapshot_s_plan_document_holds_the_projection_and_no_other_plan_does() -> None:
    plans = held()
    plan = snapshot_plan(plans)
    document = plan.as_document()
    assert document["rows"] == 24
    assert document["projection"]["kind"] == "snapshots" and document["projection"]["of"] == EMIT
    assert document["projection"]["history"] == plans[EMIT].digest().value
    assert "projection" not in plans[EMIT].as_document()


def test_the_dispatch_has_a_kind_for_each_generated_projection() -> None:
    assert set(KINDS) == set(GENERATED_PROJECTIONS)


def test_a_projection_of_a_column_the_history_lacks_is_refused() -> None:
    plans = held()
    expose = _expose(
        SNAPSHOT,
        [_column("sub_id", "STRING"), _column("nothing", "STRING", False)],
        _primary("sub", "sub_id"),
    )
    plan = build_plan(
        compile_expose(expose),
        sim_id=SIM_ID,
        seam_digest=_h(SNAPSHOT),
        rows=1,
        fidelity={"columns": {"nothing": {"levels": {"a": "1"}}}},
        time_bounds=None,
        related={"sub_id"},
        generator_version=plans[EMIT].generator_version,
    )
    with pytest.raises(FluxError) as caught:
        project_plan(plan, ("snapshots", EMIT, "keep"), plans[EMIT], root_for())
    assert caught.value.code.code == "FLX-3403" and "'nothing'" in str(caught.value)
