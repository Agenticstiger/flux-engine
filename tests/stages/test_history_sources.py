# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's emit has one source for its validity and activation columns, and may hide columns.

A ``lifetimes`` or ``within`` entry on the start, the end or the activation column of a
history's emit is ``FLX-6203`` at ``validate`` and at ``plan`` (ADR 0017 section 2). A
``within`` entry on any other date column of the emit still plans. A history's emit that declares
a hidden column and its statistic validates, plans, applies and verifies, and the hidden column is
never written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import pipeline
from flux_engine.stages.validate import validate
from flux_engine.stages.verify import verify
from tests.bundles import edit_flux
from tests.stages.test_history_run import EMIT, NAMESPACE, _bundle, _rows, _run

pytestmark = pytest.mark.integration

LIFETIME = {"emit": "customer", "start": "signed_up_on"}
WITHIN = {"emit": EMIT, "relationship": "customer", "date": "started_on"}


def _declare(bundle: Path, **blocks: List[Dict[str, Any]]) -> Path:
    """*bundle* with each key of *blocks* set as an extension key of the Simulation."""
    return edit_flux(bundle, lambda flux: flux.update(blocks))


def _history(bundle: Path, **changes: Any) -> None:
    edit_flux(bundle, lambda flux: flux["history"][0].update(changes))


def _refused_twice(bundle: Path, *fragments: str) -> None:
    """``validate`` and ``plan`` both refuse *bundle* with ``FLX-6203``, naming *fragments*."""
    result = validate(bundle)
    assert result.exit_code == 1 and result.error_code == "FLX-6203"
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-6203"
    for text in (result.stderr, str(caught.value)):
        for fragment in ("history (" + EMIT + ")", *fragments):
            assert fragment in text


@pytest.mark.parametrize(
    ("start", "end", "activation", "column"),
    [
        ("valid_from", None, None, "valid_from"),
        ("started_on", "valid_to", None, "valid_to"),
        ("started_on", None, "started_on", "started_on"),
    ],
)
def test_a_lifetime_on_a_column_the_history_sets_is_refused(
    tmp_path: Path, start: str, end: Any, activation: Any, column: str
) -> None:
    entry: Dict[str, Any] = {"emit": EMIT, "start": start}
    if end is not None:
        entry["end"] = end
    bundle = _declare(_bundle(tmp_path), lifetimes=[LIFETIME, entry])
    if activation is not None:
        _history(bundle, activation=activation)
    _refused_twice(bundle, f"column '{column}' has more than one source", "lifetimes entry")


@pytest.mark.parametrize("date", ["valid_from", "valid_to", "started_on"])
def test_a_within_date_the_history_sets_is_refused(tmp_path: Path, date: str) -> None:
    bundle = _declare(
        _bundle(tmp_path), lifetimes=[LIFETIME], within=[{**WITHIN, "date": date, "target": "0.5"}]
    )
    _history(bundle, activation="started_on")
    _refused_twice(bundle, f"column '{date}' has more than one source", "within entry")


def test_a_within_on_a_column_the_history_does_not_set_still_plans(tmp_path: Path) -> None:
    bundle = _declare(_bundle(tmp_path), lifetimes=[LIFETIME], within=[WITHIN])
    assert validate(bundle).exit_code == 0
    pipeline.execute(bundle, kernel=KERNEL_V2)


# --- a hidden column on a history's emit ---------------------------------------------------------


def _hidden(tmp_path: Path, *, statistic: bool) -> Path:
    bundle = _bundle(
        tmp_path, columns={"secret": {"range": {"min": "1", "max": "9"}}} if statistic else None
    )
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["extensions"][NAMESPACE]["emits"][EMIT]["hiddenColumns"] = [
        {"name": "secret", "type": "INT"}
    ]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return bundle


def test_a_hidden_column_with_its_statistic_validates_runs_and_is_never_written(
    tmp_path: Path,
) -> None:
    bundle = _hidden(tmp_path, statistic=True)
    assert validate(bundle).exit_code == 0
    layout = _run(bundle, tmp_path / "state")
    rows = _rows(layout, EMIT)
    assert rows and all("secret" not in row for row in rows)
    assert verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state").success


def test_a_hidden_column_without_a_statistic_is_refused_naming_the_statistic_to_declare(
    tmp_path: Path,
) -> None:
    bundle = _hidden(tmp_path, statistic=False)
    with pytest.raises(FluxError, match="declares no range") as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3402"
    assert "fidelity.columns['secret']" in str(caught.value)
