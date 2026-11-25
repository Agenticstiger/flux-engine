# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A windowed rule on a projected emit reads the whole emit, however the run is cut (ADR 0027).

A FLUX-side rule with ``ROW_NUMBER() OVER (...)`` on a periods or events emit of
``tests/fixtures/history-fleet`` reads the rows of every slice, so the emit is made whole and the
rule applied over all of it. What is pinned:

* **The rule holds.** Over the rows of one vehicle the number runs 1, 2, 3... in the order the
  window names, on the periodic and on the events emit.
* **One cut or four.** One job and four write the same lines, the numbers included.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2

from ._fleet import (
    CONTRACT,
    EVENTS,
    NAMESPACE,
    PERIODIC,
    bundle,
    expose,
    group_by,
    repin,
    run_bundle,
)
from ._fleet import rows as read_rows
from ._same_run import assert_same_run_at_one_and_four_jobs

pytestmark = pytest.mark.integration

RULES = {
    PERIODIC: {
        "id": "period_no",
        "derive": "period_no",
        "expr": "ROW_NUMBER() OVER (PARTITION BY vehicle ORDER BY month, period_id)",
    },
    EVENTS: {
        "id": "event_no",
        "derive": "event_no",
        "expr": "ROW_NUMBER() OVER (PARTITION BY vehicle ORDER BY event_id)",
    },
}
ORDER = {PERIODIC: "month", EVENTS: "event_id"}


def _windowed(tmp_path: Path) -> Path:
    target = bundle(tmp_path)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    for expose_id, rule in RULES.items():
        schema = expose(contract, expose_id)["contract"]["schema"]
        schema.append({"name": rule["derive"], "type": "INT", "required": True})
    repin(target, contract)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    for expose_id, rule in RULES.items():
        document["extensions"][NAMESPACE]["emits"][expose_id]["rules"] = [
            {"language": "flux-expr/2", **rule}
        ]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


@pytest.mark.parametrize("expose_id", [PERIODIC, EVENTS])
def test_a_windowed_rule_numbers_the_rows_of_each_entity_over_the_whole_emit(
    tmp_path: Path, expose_id: str
) -> None:
    layout = run_bundle(_windowed(tmp_path), tmp_path / "state")
    rows: List[Dict[str, Any]] = read_rows(layout, expose_id)
    assert rows
    column = RULES[expose_id]["derive"]
    for held in group_by(rows, "vehicle_id").values():
        ordered = sorted(held, key=lambda row: row[ORDER[expose_id]])
        assert [row[column] for row in ordered] == list(range(1, len(held) + 1))


def test_one_job_and_four_jobs_write_the_same_numbered_rows(tmp_path: Path) -> None:
    assert_same_run_at_one_and_four_jobs(_windowed(tmp_path), kernel=KERNEL_V2)
