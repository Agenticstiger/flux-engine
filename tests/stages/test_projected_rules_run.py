# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A projected row's rules run after the projection has set its cells (ADR 0027, the amendment of 2026-11-24).

``tests/fixtures/history-fleet`` is copied with contract derives on its periodic emit
``vehicle_month`` and its events emit ``vehicle_change``. Each derive reads a cell the
projection sets: the period column, a column of the version the row carries, a kind's override, and
an event's ``op``, ``at``, ``date`` and ``after`` image. What is pinned:

* **The rows hold their derives.** Every derive holds on every row, so ``verify`` passes every rule
  line, under each ``match``, with kinds, with an ended history and across ``--jobs``.
* **The order.** A derive reads the final value of the cell the projection sets, and a derive that
  reads another derive reads its final value too.
* **One source per column.** A rule that sets a column the projection sets is ``FLX-6203``: the
  period column, a column of the version, a kind's override column, and an event's ``op``. A
  derive on a foreign key stays ``FLX-6210``, as on any emit.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify

from ._fleet import (
    CONTRACT,
    EVENTS,
    FINAL,
    NAMESPACE,
    PERIODIC,
    bundle,
    expose,
    plan_module,
    repin,
    run_bundle,
)
from ._fleet import rows as read_rows
from ._same_run import assert_same_run_at_one_and_four_jobs

pytestmark = pytest.mark.integration

#: Derived columns of the periodic emit: one reads the period column, one a column of the version
#: and a kind's override, one another derive, one a derive and the period column.
PERIODIC_COLUMNS = [
    {"name": "due_on", "type": "DATE", "required": True},
    {"name": "is_legacy", "type": "INT", "required": True},
    {"name": "visit_key", "type": "INT", "required": True},
    {"name": "due_after", "type": "INT", "required": True},
]
PERIODIC_DERIVES = [
    {"id": "due_on", "derive": "due_on", "expr": "ADD_DAYS(month, 14)"},
    {
        "id": "is_legacy",
        "derive": "is_legacy",
        "expr": "CASE WHEN model = 'legacy' THEN 1 ELSE 0 END",
    },
    {"id": "visit_key", "derive": "visit_key", "expr": "vehicle_id * 10 + is_legacy"},
    {"id": "due_after", "derive": "due_after", "expr": "DAYS_BETWEEN(month, due_on)"},
]
#: Derived columns of the events emit: they read ``op``, ``at``, ``date`` and the after image.
EVENT_COLUMNS = [
    {"name": "is_create", "type": "BOOLEAN", "required": True},
    {"name": "lag_days", "type": "INT", "required": True},
    {"name": "after_van", "type": "INT", "required": True},
]
EVENT_DERIVES = [
    {"id": "is_create", "derive": "is_create", "expr": "op = 'c'"},
    {"id": "lag_days", "derive": "lag_days", "expr": "DAYS_BETWEEN(changed_at, recorded_at)"},
    {
        "id": "after_van",
        "derive": "after_van",
        "expr": "CASE WHEN after_model = 'van' THEN 1 ELSE 0 END",
    },
]
KINDS = [
    {"count": 1, "when": "valid_from < DATE '2024-06-01'", "overrides": {"model": "legacy"}},
    {"count": 2, "when": "valid_from >= DATE '2024-06-01'"},
]


def _derived(
    tmp_path: Path,
    name: str = "bundle",
    *,
    match: Optional[str] = None,
    kinds: Optional[Sequence[Dict[str, Any]]] = None,
    lag: bool = True,
    periodic: Sequence[Dict[str, Any]] = PERIODIC_DERIVES,
    events: Sequence[Dict[str, Any]] = EVENT_DERIVES,
    **options: Any,
) -> Path:
    """The fixture with the derives above on its periodic and events emits, the digest re-pinned."""
    target = bundle(tmp_path, name=name, **options)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    for expose_id, columns, derives in (
        (PERIODIC, PERIODIC_COLUMNS, periodic),
        (EVENTS, EVENT_COLUMNS, events),
    ):
        item = expose(contract, expose_id)
        item["contract"]["schema"].extend(dict(column) for column in columns)
        item["contract"]["invariants"] = [dict(derive) for derive in derives]
    repin(target, contract)
    path = target / "simulation.flux.yml"
    simulation = yaml.safe_load(path.read_text("utf-8"))
    flux = simulation["extensions"][NAMESPACE]
    entry = flux["periods"][0]
    if match is not None:
        entry["match"] = match
    if kinds is not None:
        entry["kinds"] = [dict(kind) for kind in kinds]
    if lag:
        flux["events"][0]["lag"] = {"bins": ["0", "3", "30"], "binWeights": ["0.6", "0.4"]}
    path.write_text(yaml.safe_dump(simulation, sort_keys=False), "utf-8")
    return target


def _day(text: str) -> date:
    return date.fromisoformat(text)


def _check_periodic(rows: List[Dict[str, Any]]) -> None:
    assert rows
    for row in rows:
        month = _day(row["month"])
        legacy = 1 if row["model"] == "legacy" else 0
        assert _day(row["due_on"]) == date.fromordinal(month.toordinal() + 14)
        assert row["is_legacy"] == legacy
        assert row["visit_key"] == row["vehicle_id"] * 10 + legacy
        assert row["due_after"] == 14


def _check_events(rows: List[Dict[str, Any]]) -> None:
    assert rows
    for row in rows:
        assert row["is_create"] is (row["op"] == "c")
        lag = _day(row["recorded_at"]).toordinal() - _day(row["changed_at"]).toordinal()
        assert row["lag_days"] == lag
        assert row["after_van"] == (1 if row["after_model"] == "van" else 0)


def _verified(target: Path, layout: RunLayout) -> List[str]:
    result = verify(target, run_id=layout.run_id, state_dir=layout.state_dir)
    assert result.success, result.stderr
    return [line for line in result.stdout.splitlines() if line.strip().startswith("rules ")]


# --- the rows hold their derives -----------------------------------------------------------------


@pytest.mark.parametrize("match", ["overlap", "start", "end"])
def test_a_periodic_rows_derives_read_its_period_and_its_version(
    tmp_path: Path, match: str
) -> None:
    layout = run_bundle(_derived(tmp_path, match=match), tmp_path / "state")
    _check_periodic(read_rows(layout, PERIODIC))


def test_an_event_rows_derives_read_its_op_its_instants_and_its_images(tmp_path: Path) -> None:
    layout = run_bundle(_derived(tmp_path), tmp_path / "state")
    _check_events(read_rows(layout, EVENTS))


def test_a_derive_reads_a_kinds_override(tmp_path: Path) -> None:
    layout = run_bundle(_derived(tmp_path, kinds=KINDS), tmp_path / "state")
    rows = read_rows(layout, PERIODIC)
    assert {row["model"] for row in rows} >= {"legacy"}
    _check_periodic(rows)


def test_an_ended_history_keeps_the_derives(tmp_path: Path) -> None:
    layout = run_bundle(_derived(tmp_path, end=FINAL), tmp_path / "state")
    _check_periodic(read_rows(layout, PERIODIC))
    _check_events(read_rows(layout, EVENTS))


@pytest.mark.parametrize("kinds", [None, KINDS])
def test_verify_holds_every_rule_line_of_the_projected_emits(
    tmp_path: Path, kinds: Optional[Sequence[Dict[str, Any]]]
) -> None:
    target = _derived(tmp_path, kinds=kinds, end=FINAL)
    lines = _verified(target, run_bundle(target, tmp_path / "state"))
    for expose_id, derives in ((PERIODIC, PERIODIC_DERIVES), (EVENTS, EVENT_DERIVES)):
        line = next(line for line in lines if line.strip().startswith(f"rules {expose_id}:"))
        for derive in derives:
            assert f"{derive['id']} " in line and "(100.00%)" in line.split(derive["id"])[1]
        assert "violated" not in line


def test_one_job_and_four_jobs_write_the_same_derived_rows(tmp_path: Path) -> None:
    target = _derived(tmp_path, kinds=KINDS, end=FINAL)
    assert_same_run_at_one_and_four_jobs(target, kernel=KERNEL_V2)


# --- one source per column -----------------------------------------------------------------------


def _refused(target: Path, tmp_path: Path) -> Any:
    planned = plan_module.plan(
        target, state_dir=tmp_path / "refused", kernel=KERNEL_V2.version, stage=False
    )
    assert not planned.success
    return planned


@pytest.mark.parametrize(
    ("column", "expr", "kinds", "role"),
    [
        ("month", "ADD_DAYS(DATE '2023-01-01', 1)", None, "its period column"),
        ("model", "'sedan'", None, "a column of the version the row carries"),
        ("model", "'sedan'", KINDS, "an override of kinds[0]"),
    ],
)
def test_a_derive_on_a_column_the_periods_set_is_refused(
    tmp_path: Path,
    column: str,
    expr: str,
    kinds: Optional[Sequence[Dict[str, Any]]],
    role: str,
) -> None:
    periodic = [*PERIODIC_DERIVES, {"id": "second", "derive": column, "expr": expr}]
    planned = _refused(_derived(tmp_path, periodic=periodic, kinds=kinds), tmp_path)
    assert planned.error_code == "FLX-6203"
    assert f"column {column!r}" in planned.stderr and "periods entry" in planned.stderr
    assert role in planned.stderr and "'second'" in planned.stderr


def test_a_derive_on_an_events_op_is_refused(tmp_path: Path) -> None:
    events = [*EVENT_DERIVES, {"id": "second", "derive": "op", "expr": "'u'"}]
    planned = _refused(_derived(tmp_path, events=events), tmp_path)
    assert planned.error_code == "FLX-6203"
    assert "column 'op'" in planned.stderr and "events entry" in planned.stderr
    assert "its op column" in planned.stderr


# --- a read through a relationship whose key the entry sets -----------------------------------------


@pytest.mark.parametrize("emit", [EVENTS, PERIODIC])
def test_a_derive_through_a_relationship_whose_key_the_entry_sets_is_refused(
    tmp_path: Path, emit: str
) -> None:
    extra = {"id": "depot_of", "derive": "depot_of", "expr": "vehicle.depot_id"}
    target = _derived(tmp_path)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    item = expose(contract, emit)
    item["contract"]["schema"].append({"name": "depot_of", "type": "INT", "required": False})
    item["contract"]["invariants"].append(extra)
    repin(target, contract)
    planned = _refused(target, tmp_path)
    assert planned.error_code == "FLX-3403"
    assert "'depot_of'" in planned.stderr and "relationship 'vehicle'" in planned.stderr
    assert "'vehicle_id'" in planned.stderr
