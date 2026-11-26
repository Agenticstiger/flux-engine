# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``validate``'s plan pre-flights read a contract's date range as ``plan`` does (ADR 0024, amendment of 2026-11-25).

``validate`` has no plan, so the pre-flights of an emit that declares ``conditions`` or a
``fidelity.copula`` build one with the cut on, and a recorded plan without ``dateRanges`` replays
with the cut off. Pinned on the fleet's ``service_visit``, which gets a categorical column and a
contract range on ``visited_on``:

* ``validate`` accepts what ``plan`` accepts, and a copula ``plan`` refuses is refused with its code;
* a plan recorded without the cut applies and replays byte for byte, for either declaration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages.apply import apply, load_recorded_plan
from flux_engine.stages.replay import replay
from flux_engine.stages.validate import validate

from ._fleet import CONTRACT, NAMESPACE, VISIT, expose, plan_module, repin, run_bundle
from .test_declared_range_replay import _run_without_the_cut, recorded_digest
from .test_declared_range_run import _visits

pytestmark = pytest.mark.integration

#: Bins that run past both ends of the range: a draw is cut, and the run is not a plain one.
RANGE = "between '2001-01-01' and '2005-12-31'"
INSIDE_RANGE = "between '2000-01-01' and '2009-01-01'"
NO_BINS_INSIDE = "between '2012-01-01' and '2013-12-31'"
CONDITIONS = [
    {"values": {"kind": "a"}, "count": 200},
    {"values": {"kind": "b"}, "count": 400},
]
COPULA = {
    "columns": ["visited_on", "cost"],
    "rankCorrelations": {
        "measure": "kendall",
        "pairs": [{"columns": ["visited_on", "cost"], "value": "0.5"}],
    },
}


def _declared(tmp_path: Path, *, rule: str, conditions: bool = False, copula: bool = False) -> Path:
    """The fleet with ``kind`` on ``service_visit``, a range on ``visited_on`` and the declaration asked for."""
    target = _visits(tmp_path, rule=rule)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    schema = expose(contract, VISIT)["contract"]["schema"]
    schema.append({"name": "kind", "type": "STRING", "required": False})
    repin(target, contract)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    namespace = document["extensions"][NAMESPACE]
    fidelity = namespace["emits"][VISIT]["fidelity"]
    fidelity["columns"]["kind"] = {"levels": {"a": "0.5", "b": "0.5"}}
    if conditions:
        namespace["conditions"] = {VISIT: CONDITIONS}
    if copula:
        fidelity["columns"]["cost"].update(bins=["5", "100", "500"], binWeights=["0.5", "0.5"])
        fidelity["copula"] = COPULA
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def _codes(target: Path, tmp_path: Path) -> List[Optional[str]]:
    """The error code ``validate`` and ``plan`` give the bundle, in that order (``None`` for success)."""
    checked = validate(target)
    planned = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    return [
        None if checked.exit_code == 0 else checked.error_code,
        None if planned.success else planned.error_code,
    ]


@pytest.mark.parametrize("declaration", ["conditions", "copula"])
@pytest.mark.parametrize("rule", [RANGE, INSIDE_RANGE])
def test_validate_accepts_what_plan_accepts(tmp_path: Path, declaration: str, rule: str) -> None:
    target = _declared(tmp_path, rule=rule, **{declaration: True})
    assert _codes(target, tmp_path) == [None, None]


@pytest.mark.parametrize("declaration", ["conditions", "copula"])
def test_a_range_that_holds_none_of_the_bins_is_refused_by_both_alike(
    tmp_path: Path, declaration: str
) -> None:
    target = _declared(tmp_path, rule=NO_BINS_INSIDE, **{declaration: True})
    assert _codes(target, tmp_path) == ["FLX-3403", "FLX-3403"]


EMPTY = [
    "between '2005-12-31' and '2001-01-01'",
    "> '2005-12-31' and < '2005-12-31'",
    "> '2005-12-31' and <= '2001-01-01'",
]


@pytest.mark.parametrize("rule", EMPTY)
@pytest.mark.parametrize("rows", [True, False])
def test_an_empty_range_is_refused_by_validate_as_plan_refuses_it(
    tmp_path: Path, rule: str, rows: bool
) -> None:
    """With or without a row count: an emit that takes its rows from the run is planned by ``plan`` alone."""
    target = _visits(tmp_path, rule=rule)
    if not rows:
        path = target / "simulation.flux.yml"
        document = yaml.safe_load(path.read_text("utf-8"))
        del document["extensions"][NAMESPACE]["emits"][VISIT]["rows"]
        path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    assert _codes(target, tmp_path) == ["FLX-3403", "FLX-3403"]


def test_a_range_that_holds_one_day_is_accepted_by_both_with_no_row_count(tmp_path: Path) -> None:
    target = _visits(tmp_path, rule="between '2005-12-31' and '2005-12-31'")
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    del document["extensions"][NAMESPACE]["emits"][VISIT]["rows"]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    assert _codes(target, tmp_path) == [None, None]


@pytest.mark.parametrize("declaration", ["conditions", "copula"])
def test_a_plan_recorded_without_the_cut_applies_and_replays_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declaration: str
) -> None:
    target = _declared(tmp_path, rule=RANGE, **{declaration: True})
    state = tmp_path / "state"
    layout, digest = _run_without_the_cut(target, state, monkeypatch)
    plan_document: Dict[str, Any] = dict(load_recorded_plan(layout))
    assert "dateRanges" not in plan_document
    recorded = layout.emit_path(VISIT).read_bytes()
    layout.emit_path(VISIT).unlink()
    applied = apply(target, run_id=layout.run_id, state_dir=state)
    assert applied.success, applied.stderr
    assert layout.emit_path(VISIT).read_bytes() == recorded
    for jobs in (1, 4):
        replayed = replay(
            target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=jobs
        )
        assert replayed.success, replayed.stderr
        assert layout.emit_path(VISIT).read_bytes() == recorded
        assert recorded_digest(state, layout.run_id) == digest


@pytest.mark.parametrize("declaration", ["conditions", "copula"])
def test_the_same_bundle_planned_fresh_cuts_and_says_so(tmp_path: Path, declaration: str) -> None:
    target = _declared(tmp_path, rule=RANGE, **{declaration: True})
    layout = run_bundle(target, tmp_path / "state")
    assert load_recorded_plan(layout)["dateRanges"] == "cut"
