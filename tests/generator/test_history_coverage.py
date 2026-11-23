# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How the columns of a history's emit, and of the emits projected from it, were produced.

A history's validity and tracked columns are drawn by its chain, and its natural key repeats on
every version, so none of them is "a key the contract declares, laid out from the row index so
that no value repeats". The projected emits take their columns from the history's versions. An emit
with no history keeps every label it had.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Optional, Tuple

import pytest

from flux_engine.evidence.provenance import render_provenance_section
from flux_engine.generator.coverage import coverage_of_plan
from flux_engine.generator.rows import GeneratorPlan
from flux_engine.seam.coverage import ExposeCoverage, Origin
from flux_engine.seam.projections import PeriodKind

from . import test_event_projection as events
from . import test_period_projection as periods
from . import test_projection_plan as snapshots

pytestmark = pytest.mark.unit

Label = Tuple[Origin, str, Optional[str]]
KEY_TEXT = "a key the contract declares, laid out from the row index"


def labels(plan: GeneratorPlan) -> Dict[str, Label]:
    """``{column: (origin, decided by, source)}`` of *plan*'s coverage."""
    return {
        row.column: (row.origin, row.decided_by, row.source)
        for row in coverage_of_plan(plan).columns
    }


def history_labels() -> Dict[str, Label]:
    return labels(events.held(None)[events.EMIT])


def section_of(plan: GeneratorPlan) -> str:
    return render_provenance_section([coverage_of_plan(plan)])


CHAINED = [
    ("valid_from", (Origin.DECLARED, "history", "history")),
    ("valid_to", (Origin.DECLARED, "history", "history")),
    ("activated", (Origin.DECLARED, "history", "history")),
    ("plan", (Origin.DECLARED, "tracked", "history.tracked.plan")),
    ("tier", (Origin.DECLARED, "tracked", "history.tracked.tier")),
]


@pytest.mark.parametrize(("column", "expected"), CHAINED)
def test_a_validity_or_tracked_column_is_named_for_what_lays_it_out(
    column: str, expected: Label
) -> None:
    assert history_labels()[column] == expected


def assert_labels(found: Dict[str, Label], expected: Dict[str, Tuple[Any, ...]]) -> None:
    """Each column's label starts with what *expected* says of it (the whole label, or less)."""
    for column, want in expected.items():
        assert found[column][: len(want)] == want, column


def test_the_natural_key_repeats_across_versions_and_is_not_a_row_key() -> None:
    found = history_labels()
    assert found["sub_id"][:2] == (Origin.DERIVED, "entity")
    row = next(
        r for r in coverage_of_plan(events.held(None)[events.EMIT]).columns if r.column == "sub_id"
    )
    assert "repeated on every version" in row.detail
    assert KEY_TEXT not in row.detail


def test_the_version_tables_own_key_is_laid_out_from_the_row_index() -> None:
    assert history_labels()["version_id"][:2] == (Origin.DERIVED, "key")


def test_a_column_the_history_does_not_set_keeps_its_declaration() -> None:
    found = history_labels()
    assert found["status"][:2] == (Origin.DECLARED, "fidelity")
    assert found["region"][:2] == (Origin.DECLARED, "fidelity")
    assert found["customer_id"][:2] == (Origin.DERIVED, "relationship")


def test_the_evidence_text_of_a_history_names_no_validity_or_tracked_column_a_key() -> None:
    text = section_of(events.held(None)[events.EMIT])
    lines = {line.split("`")[1]: line for line in text.splitlines() if line.startswith("- `")}
    for column in ("valid_from", "valid_to", "activated", "plan", "tier", "sub_id"):
        assert KEY_TEXT not in lines[column], lines[column]
    assert "declared in `history`" in lines["valid_from"]
    assert "declared in `history.tracked.tier`" in lines["tier"]
    assert KEY_TEXT in lines["version_id"]


def test_an_emit_without_a_history_keeps_its_labels() -> None:
    plans = events.held(None)
    assert labels(plans["customer"]) == {
        "customer_id": (Origin.DERIVED, "key", "semantics+rowIndex"),
        "c_name": (Origin.DECLARED, "fidelity", "fidelity.columns.c_name"),
    }
    assert labels(plans["store"]) == {
        "store_id": (Origin.DERIVED, "key", "semantics+rowIndex"),
        "s_name": (Origin.DECLARED, "fidelity", "fidelity.columns.s_name"),
    }
    text = section_of(plans["customer"])
    assert (
        f"- `customer_id` (INT): derived: {KEY_TEXT} so that no value repeats, never drawn" in text
    )


def test_a_snapshot_takes_every_column_from_the_last_version() -> None:
    plan = snapshots.snapshot_plan(snapshots.held(None))
    for column, (origin, decided_by, source) in labels(plan).items():
        assert (origin, decided_by) == (Origin.COPIED, "version"), column
        assert source == f"versions.{events.EMIT}.{column}"


def test_an_event_reads_its_columns_from_the_history() -> None:
    version = f"versions.{events.EMIT}"
    assert_labels(
        labels(events.events_plan(events.held(None))),
        {
            "op": (Origin.DERIVED, "projection"),
            "changed_at": (Origin.DERIVED, "projection"),
            "recorded_at": (Origin.DERIVED, "projection"),
            "sub_id": (Origin.COPIED, "version", f"{version}.sub_id"),
            "before_plan": (Origin.COPIED, "version", f"{version}.plan"),
            "after_price": (Origin.COPIED, "version", f"{version}.price"),
            "channel": (Origin.DECLARED, "fidelity"),
        },
    )


def test_a_periodic_row_reads_its_period_and_versions() -> None:
    assert_labels(
        labels(periods.periods_plan(periods.held(None), named=True)),
        {
            "month": (Origin.DERIVED, "projection"),
            "plan": (Origin.COPIED, "version", f"versions.{periods.EMIT}.plan"),
            "price": (Origin.COPIED, "version"),
            "channel": (Origin.DECLARED, "fidelity"),
        },
    )


def test_a_column_a_periods_kind_overrides_is_declared_there() -> None:
    kinds: Dict[str, Any] = {"kinds": (PeriodKind(overrides=(("channel", "web"),)),)}
    found = labels(periods.periods_plan(periods.held(None), named=True, **kinds))
    assert found["channel"] == (Origin.DECLARED, "period-kind", "periods.kinds")
    assert found["plan"][:2] == (Origin.COPIED, "version")


def test_a_projected_row_survives_a_round_trip() -> None:
    coverage = coverage_of_plan(events.events_plan(events.held(None)))
    assert ExposeCoverage.from_document(coverage.as_document()) == coverage


def test_a_recorded_date_with_a_lag_is_declared_there_and_one_without_is_worked_out() -> None:
    plans = events.held(None)
    plain = labels(events.events_plan(plans))
    lagged = labels(events.events_plan(plans, lag=events.LAG))
    assert plain["recorded_at"][:2] == (Origin.DERIVED, "projection")
    assert lagged["recorded_at"] == (Origin.DECLARED, "lag", "events.lag")
    for column in ("op", "changed_at"):
        assert lagged[column][:2] == (Origin.DERIVED, "projection"), column


def test_the_evidence_text_of_a_lagged_date_does_not_say_never_drawn() -> None:
    plans = events.held(None)
    for lag, expected in ((None, "never drawn"), (events.LAG, "declared in `events.lag`")):
        options: Dict[str, Any] = {} if lag is None else {"lag": lag}
        text = section_of(events.events_plan(plans, **options))
        line = next(row for row in text.splitlines() if row.startswith("- `recorded_at`"))
        assert expected in line, line
        assert ("never drawn" in line) == (lag is None), line


def _hiding(plans: Dict[str, GeneratorPlan], column: str) -> Dict[str, GeneratorPlan]:
    """*plans* with the history's *column* hidden, as a history declares it with ``hiddenColumns``."""
    history = plans[events.EMIT]
    columns = tuple(
        (
            replace(held, column=replace(held.column, hidden=True))
            if held.column.name == column
            else held
        )
        for held in history.columns
    )
    return {**plans, events.EMIT: replace(history, columns=columns)}


def test_no_label_of_a_projection_names_a_hidden_column_of_the_history() -> None:
    plans = _hiding(events.held(None), "plan")
    for plan in (
        events.events_plan(plans),
        snapshots.snapshot_plan(_hiding(snapshots.held(None), "plan")),
        periods.periods_plan(_hiding(periods.held(None), "plan"), named=True),
    ):
        for column, (_, _, source) in labels(plan).items():
            assert source is None or not source.endswith(f"{events.EMIT}.plan"), column
