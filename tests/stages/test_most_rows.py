# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``most_rows`` bounds the rows a plan holds before any row is drawn.

A caller that tries a bundle (the trial ``fluxsim init`` makes) asks for a ceiling on the rows the
plans of every emit hold together. A plan above it raises :class:`PlannedRowsAboveCeiling`, which is
not a ``FluxError``, and no row is generated. With no ceiling nothing changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.stages import pipeline
from flux_engine.stages.pipeline import PlannedRowsAboveCeiling, execute
from flux_engine.stages.run import run

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration


def _planned_total() -> int:
    """The rows the small fixture's plans hold, read from the ceiling's own refusal at 0."""
    with pytest.raises(PlannedRowsAboveCeiling) as above:
        execute(FIXTURE_BUNDLE, most_rows=0)
    return above.value.rows


def test_a_plan_above_the_ceiling_raises_before_any_row_is_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    total = _planned_total()
    assert total > 1

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a row was generated")

    monkeypatch.setattr(pipeline, "_generate_emits", refuse)
    with pytest.raises(PlannedRowsAboveCeiling) as above:
        execute(FIXTURE_BUNDLE, most_rows=total - 1)
    assert (above.value.rows, above.value.ceiling) == (total, total - 1)
    assert not isinstance(above.value, FluxError)


def test_a_plan_at_the_ceiling_runs_and_gives_the_rows_it_gave_without_one() -> None:
    total = _planned_total()
    bounded = execute(FIXTURE_BUNDLE, most_rows=total)
    free = execute(FIXTURE_BUNDLE)
    assert bounded.input_digest == free.input_digest
    assert bounded.emits == free.emits


def test_the_ceiling_passes_through_run_and_is_not_turned_into_a_result(tmp_path: Path) -> None:
    with pytest.raises(PlannedRowsAboveCeiling):
        run(FIXTURE_BUNDLE, state_dir=tmp_path / "state", most_rows=0)
