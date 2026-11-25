# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's end column has one source: a rule that sets it is ``FLX-6203`` at plan (ADR 0017).

The history writes ``end.value`` into ``end.column`` on the ending version of an entity and a drawn
level on the others, and ``history:ended_is_last`` holds the rows to that. A contract rule that
also sets the column, with or without a ``when``, would write over it, and a ``when`` that reads
the column (a periods kind, an events entry) would read the draw, not the rule. What is pinned, on
``tests/fixtures/history-fleet``:

* **A rule on the end column is refused** at plan with ``FLX-6203``, naming the column, the
  history and the rule.
* **A rule on another column still plans**, and the rows of a run verify, ``history:ended_is_last``
  included.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.stages.verify import verify

from ._fleet import (
    CONTRACT,
    FINAL,
    HISTORY,
    NAMESPACE,
    bundle,
    expose,
    plan_refused,
    repin,
    run_bundle,
)

pytestmark = pytest.mark.integration

ON_STATUS = {
    "id": "status_rule",
    "derive": "status",
    "expr": "CASE WHEN model = 'van' THEN 'retired' ELSE 'active' END",
}
ON_STATUS_WHEN = {**ON_STATUS, "when": "model = 'van'"}
ON_MODEL = {"id": "model_rule", "derive": "model", "expr": "'van'"}


def _ruled(tmp_path: Path, rule: Dict[str, Any], name: str = "bundle") -> Path:
    target = bundle(tmp_path, name=name, end=FINAL)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    expose(contract, HISTORY)["contract"]["invariants"] = [dict(rule)]
    repin(target, contract)
    return target


@pytest.mark.parametrize("rule", [ON_STATUS, ON_STATUS_WHEN], ids=["plain", "with-when"])
def test_a_rule_on_the_end_column_is_refused_at_plan(tmp_path: Path, rule: Dict[str, Any]) -> None:
    planned = plan_refused(_ruled(tmp_path, rule), tmp_path / "state")
    assert planned.error_code == "FLX-6203"
    for text in (
        "column 'status' has more than one source",
        f"history ({HISTORY})",
        "end column",
        "'status_rule'",
    ):
        assert text in planned.stderr


def test_a_rule_on_another_column_still_plans_and_the_history_ends_where_it_says(
    tmp_path: Path,
) -> None:
    target = _ruled(tmp_path, ON_MODEL)
    layout = run_bundle(target, tmp_path / "state")
    assert verify(target, run_id=layout.run_id, state_dir=tmp_path / "state").success


def test_a_drawn_end_column_that_is_not_carried_is_refused_naming_the_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard behind the plan-time refusal: ``DrawnEnd`` reads only a column the history carries."""
    from flux_engine.generator import relations

    target = bundle(tmp_path, end=FINAL)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["extensions"][NAMESPACE]["periods"][0]["kinds"] = [
        {"count": 1, "when": "status <> 'retired'"}
    ]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    monkeypatch.setattr(relations, "carried_columns", lambda plan: frozenset())
    planned = plan_refused(target, tmp_path / "state")
    assert planned.error_code == "FLX-3403"
    assert "end.column 'status' is not carried" in planned.stderr
