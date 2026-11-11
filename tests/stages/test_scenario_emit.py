# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §1's scenario path, end to end on the shipped bundle.

The FLUX specification repository's ``telco-payment-recovery`` example
declares one emit and **no** fidelity block, so its rows can only come
from the run's own moments: the detector finds an aborted-payment burst,
and that firing for that subject is one ``payment_recovery_moment``.

No count in this module is typed. Every number is either read back from
the pipeline's own result or computed from the detections it produced --
a test that pre-stated a row count would pass on a run that emitted the
wrong rows for the right total. The exact count is pinned in one place
only, the golden vectors.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.seam.conform import render_rows
from flux_engine.stages.pipeline import execute

REPO_ROOT = Path(__file__).resolve().parents[2]
FLUX_REPO = REPO_ROOT.parent / "FLUX"
EXAMPLE = FLUX_REPO / "examples" / "telco-payment-recovery"
EXPOSE_ID = "payment_recovery_moment"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (EXAMPLE / "simulation.flux.yml").is_file(),
        reason=f"the FLUX example bundle is not checked out at {EXAMPLE}",
    ),
]


@pytest.fixture(scope="module")
def result():
    return execute(EXAMPLE)


@pytest.fixture(scope="module")
def outcome(result):
    return result.emit(EXPOSE_ID)


# ---------------------------------------------------------------------------
# The rows exist, and they are the run's moments
# ---------------------------------------------------------------------------


def test_the_shipped_example_declares_no_fidelity_block(outcome) -> None:
    """The premise: this emit has no declared statistics to draw from."""
    assert outcome.resolved.fidelity is None
    assert outcome.source == "scenario"
    assert outcome.plan is None


def test_the_shipped_example_emits_rows(outcome) -> None:
    """The defect this path exists to close: the example emitted nothing."""
    assert len(outcome.rows) > 0
    assert outcome.result.rows == len(outcome.rows)


def test_one_detector_firing_is_one_row(result, outcome) -> None:
    """§1: "one detector firing for one subject is one row"."""
    firings = [detection for detection in result.detections if detection.events]
    assert firings, "the shipped scenario produces at least one moment"
    assert len(outcome.rows) == len(firings)


def test_the_rows_are_in_scheduler_order(result, outcome) -> None:
    """Keyed on the instant of the event that completed each moment."""
    index = outcome.expose.index_of["aborted_at"]
    instants = [parse_rfc3339_ms(row[index]) for row in render_rows(outcome.rows, outcome.expose)]
    assert instants == sorted(instants)


# ---------------------------------------------------------------------------
# Every row conforms to the expose the emit references
# ---------------------------------------------------------------------------


def test_every_row_conforms_to_the_declared_expose(outcome) -> None:
    """Arrays aligned to the header, required columns present, no float."""
    expose = outcome.expose
    rendered = render_rows(outcome.rows, expose)
    assert len(rendered) == len(outcome.rows)
    for row in rendered:
        assert len(row) == len(expose.columns)
        for column, value in zip(expose.columns, row, strict=True):
            assert not isinstance(value, float)
            if column.required:
                assert value is not None, column.name


def test_the_subject_identifier_and_the_market_come_from_the_subject(result, outcome) -> None:
    subjects = {result.population.subject(i).id for i in range(len(result.population))}
    markets = {result.population.subject(i).market for i in range(len(result.population))}
    index = outcome.expose.index_of
    for row in render_rows(outcome.rows, outcome.expose):
        assert row[index["subscriber_id"]] in subjects
        assert row[index["market"]] in markets


def test_the_instant_is_the_last_matched_event_of_the_moment(result, outcome) -> None:
    index = outcome.expose.index_of["aborted_at"]
    by_subject: Dict[str, list] = {}
    for detection in result.detections:
        if detection.events:
            by_subject.setdefault(result.population.subject(detection.ordinal).id, []).append(
                detection.events[-1].t_ms
            )
    rendered = render_rows(outcome.rows, outcome.expose)
    subject_index = outcome.expose.index_of["subscriber_id"]
    for row in rendered:
        assert parse_rfc3339_ms(row[index]) in by_subject[row[subject_index]]


def test_the_amount_is_the_matched_payload_at_the_declared_scale(outcome) -> None:
    """``amount`` is ``DECIMAL(18,4)``; the row carries it at scale 4."""
    column = outcome.expose.column("amount")
    assert column.type_name.startswith("DECIMAL")
    index = outcome.expose.index_of["amount"]
    for row in render_rows(outcome.rows, outcome.expose):
        rendered = row[index]
        assert isinstance(rendered, str)
        assert -Decimal(rendered).as_tuple().exponent == column.logical_type.scale


# ---------------------------------------------------------------------------
# The declared row count caps, and the run repeats
# ---------------------------------------------------------------------------


def test_a_declared_row_count_caps_the_stream_rather_than_padding_it(
    outcome, monkeypatch: Any
) -> None:
    """§1: "a declared ``rows`` **caps** a scenario-driven stream"."""
    from flux_engine.seam import resolve as resolve_module

    full = len(outcome.rows)
    assert full > 1, "the cap is only observable on a stream with rows to drop"

    cap = full - 1
    original = resolve_module.ResolvedEmit.declared_row_count

    monkeypatch.setattr(
        resolve_module.ResolvedEmit,
        "declared_row_count",
        property(lambda self: cap),
    )
    capped = execute(EXAMPLE).emit(EXPOSE_ID)
    assert len(capped.rows) == cap
    assert capped.rows == outcome.rows[:cap]

    monkeypatch.setattr(resolve_module.ResolvedEmit, "declared_row_count", original)


def test_a_cap_above_the_moment_count_emits_what_the_run_has(outcome, monkeypatch: Any) -> None:
    """The paired case: nothing is invented to reach a declared number."""
    from flux_engine.seam import resolve as resolve_module

    beyond = len(outcome.rows) + 100
    original = resolve_module.ResolvedEmit.declared_row_count
    monkeypatch.setattr(
        resolve_module.ResolvedEmit,
        "declared_row_count",
        property(lambda self: beyond),
    )
    padded = execute(EXAMPLE).emit(EXPOSE_ID)
    assert len(padded.rows) == len(outcome.rows)
    monkeypatch.setattr(resolve_module.ResolvedEmit, "declared_row_count", original)


def test_the_scenario_stream_is_reproducible(outcome) -> None:
    again = execute(EXAMPLE).emit(EXPOSE_ID)
    assert again.rows == outcome.rows
    assert again.result.output_digest.value == outcome.result.output_digest.value
