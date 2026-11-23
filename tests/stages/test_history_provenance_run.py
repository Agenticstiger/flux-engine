# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A real plan of ``tests/fixtures/history-projections`` records true column provenance.

A history's validity columns and natural key, and the snapshot, events and periods emits projected
from it, each say how they were produced in the record `plan` binds and in the Column Provenance
section of ``EVIDENCE.md``. A bundle with no history keeps the labels it always had.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

import pytest

from flux_engine.api.lineage import DatasetRef
from flux_engine.evidence.provenance import (
    build_provenance_document,
    parse_provenance,
    render_provenance_section,
)
from flux_engine.seam.coverage import ColumnCoverage, ExposeCoverage, Origin
from flux_engine.starter import scaffold

from .test_projection_plan_run import FIXTURE, _run

pytestmark = pytest.mark.integration

KEY_TEXT = "a key the contract declares, laid out from the row index"
#: The ways a column is produced that only a history and its projections have.
HISTORY_DECIDED_BY = {"entity", "history", "tracked", "version", "projection", "period-kind"}


def _lines(bundle: Path, state: Path) -> Dict[str, Dict[str, str]]:
    layout = _run(bundle, state)
    coverages = parse_provenance(layout.column_provenance_path.read_text("utf-8"))
    found: Dict[str, Dict[str, str]] = {}
    for coverage in coverages:
        section = render_provenance_section([coverage])
        found[coverage.expose_id] = {
            line.split("`")[1]: line for line in section.splitlines() if line.startswith("- `")
        }
    return found


def test_a_history_and_its_projections_say_how_each_column_was_produced(tmp_path: Path) -> None:
    found = _lines(FIXTURE, tmp_path / "state")
    version = found["subscription_version"]
    assert KEY_TEXT in version["subscription_sk"]
    assert KEY_TEXT not in version["subscription_id"]
    assert "repeated on every version" in version["subscription_id"]
    snapshot = found["subscription_current"]
    assert all("copied from `versions.subscription_version." in line for line in snapshot.values())
    events = found["subscription_change"]
    assert "worked out from the history" in events["op"]
    assert "copied from `versions.subscription_version.plan`" in events["before_plan"]
    assert "copied from `versions.subscription_version.plan`" in events["after_plan"]
    periods = found["subscription_month"]
    assert "worked out from the history" in periods["month"]
    assert any("copied from `versions.subscription_version." in line for line in periods.values())


def test_a_bundle_without_a_history_keeps_its_labels(tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    scaffold(target)
    layout = _run(target, tmp_path / "state")
    text = layout.column_provenance_path.read_text("utf-8")
    for coverage in parse_provenance(text):
        for row in coverage.columns:
            assert row.decided_by not in HISTORY_DECIDED_BY, row
    assert "versions." not in text and '"history"' not in text


def test_the_lineage_of_a_projected_column_names_the_history_emit_as_its_dataset(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    layout = _run(FIXTURE, state)
    record = json.loads(layout.column_provenance_path.read_text("utf-8"))
    inputs = []
    for expose in record["exposes"]:
        for name, entry in expose["facets"]["columnLineage"]["fields"].items():
            for field in entry["inputFields"]:
                inputs.append((expose["exposeId"], name, field["name"], field["field"]))
    assert inputs, "a projection copies columns of its history"
    assert not [item for item in inputs if item[2] == "versions"], inputs
    snapshot = [item for item in inputs if item[0] == "subscription_current"]
    assert ("subscription_current", "plan", "subscription_version", "plan") in snapshot


@pytest.mark.parametrize(
    ("history", "column"),
    [("subscription_version", "plan"), ("billing.versions", "plan"), ("billing.versions", "a.b")],
)
def test_the_lineage_dataset_is_the_history_emit_whatever_dots_its_id_and_column_hold(
    history: str, column: str
) -> None:
    held = ExposeCoverage(
        "billing.current",
        (
            ColumnCoverage(
                column, "STRING", True, Origin.COPIED, f"versions.{history}.{column}", "version"
            ),
        ),
    )
    source = ExposeCoverage(history, ())
    record = build_provenance_document(
        simulation_id="sim",
        exposes=[(source, DatasetRef("ns", history)), (held, DatasetRef("ns", held.expose_id))],
    )
    fields = record["exposes"][1]["facets"]["columnLineage"]["fields"]
    (found,) = fields[column]["inputFields"]
    assert (found["name"], found["field"]) == (history, column)
