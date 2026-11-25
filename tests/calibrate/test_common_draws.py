# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Calibration compares its candidates on the same draws (ADR 0028 section 8, common random numbers).

``H`` binds the fidelity block, so a candidate that moves one parameter would, left alone, seed every
row again and the fit would follow seed noise. The fit passes ``common_draws_of``, the unpatched bundle,
to every run it makes; nothing else does.

* **Same draws.** Two candidates that differ only in a column's ``nullFraction`` draw the same rows in
  every column but the one the value decides.
* **Every other run is unchanged.** Without the option, the same two bundles share almost no row.
* **The fit uses it, every time.** Each evaluation of the fit, the held-out check included, is run with the
  unpatched bundle named, and the fit is deterministic.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytest.importorskip("sqlglot")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("cma")

from flux_engine.calibrate.fit import FitRequest, run_fit  # noqa: E402
from flux_engine.stages.run import run, run_on_common_draws  # noqa: E402
from tests.bundles import edit_simulation  # noqa: E402
from tests.calibrate._fit_bundle import (  # noqa: E402
    NAMESPACE,
    copy_fit_bundle,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "consumers"
PROFILE = FIXTURES / "subscriptions.profile.json"
HELD_OUT = FIXTURES / "subscriptions.held-out-profile.json"
RENEWED = 4  # the position of `renewed` in a row of the `subscriptions` emit


def bundle_with_nulls(root: Path, nulls: str) -> Path:
    """The fit fixture with the share of ``renewed`` left NULL set to *nulls*."""

    def change(document: Dict[str, Any]) -> None:
        emit = document["extensions"][NAMESPACE]["emits"]["subscriptions"]
        emit["fidelity"]["columns"]["renewed"]["nullFraction"] = nulls

    return edit_simulation(copy_fit_bundle(root), change)


def rows_of(bundle: Path, state: Path, draws_of: Optional[Path] = None) -> List[List[Any]]:
    if draws_of is None:
        result = run(bundle, state_dir=state, profile=PROFILE, scale="0.25")
    else:
        result = run_on_common_draws(
            bundle, common_draws_of=draws_of, state_dir=state, profile=PROFILE, scale="0.25"
        )
    assert result.success, result.stderr
    run_id = result.artifacts["plan"]["run_id"]
    path = state / "runs" / run_id / "emits" / "subscriptions.ndjson"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def matches(first: List[List[Any]], second: List[List[Any]]) -> List[int]:
    """For each column, how many rows the two runs give the same value."""
    assert len(first) == len(second) > 0
    return [
        sum(a[column] == b[column] for a, b in zip(first, second, strict=True))
        for column in range(len(first[0]))
    ]


@pytest.fixture(scope="module")
def pair(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    root = tmp_path_factory.mktemp("pair")
    (root / "a").mkdir()
    (root / "b").mkdir()
    return {
        "root": root,
        "tenth": bundle_with_nulls(root / "a", "0.1"),
        "fifth": bundle_with_nulls(root / "b", "0.2"),
    }


def test_candidates_that_differ_in_one_value_draw_the_same_other_columns(
    pair: Dict[str, Path],
) -> None:
    root = pair["root"]
    base = rows_of(pair["tenth"], root / "s1")
    moved = rows_of(pair["fifth"], root / "s2", draws_of=pair["tenth"])
    counts = matches(base, moved)
    rows = len(base)
    assert [count for column, count in enumerate(counts) if column != RENEWED] == [rows] * (
        len(counts) - 1
    )
    # The value decides which cells of `renewed` are NULL, and only those: the rest agree.
    assert 0 < counts[RENEWED] < rows
    assert all(
        a[RENEWED] == b[RENEWED] or a[RENEWED] is None or b[RENEWED] is None
        for a, b in zip(base, moved, strict=True)
    )


def test_without_the_option_the_same_two_bundles_share_almost_no_row(pair: Dict[str, Path]) -> None:
    root = pair["root"]
    counts = matches(rows_of(pair["tenth"], root / "s3"), rows_of(pair["fifth"], root / "s4"))
    assert counts[0] < 5  # the ids are drawn again
    assert max(counts) < 300 - 50


def test_a_run_that_names_its_own_bundle_is_the_run_without_the_option(
    pair: Dict[str, Path],
) -> None:
    root = pair["root"]
    plain = rows_of(pair["tenth"], root / "s5")
    named = rows_of(pair["tenth"], root / "s6", draws_of=pair["tenth"])
    assert plain == named


def test_every_evaluation_of_the_fit_names_the_unpatched_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = copy_fit_bundle(tmp_path)
    asked: List[Any] = []
    real = run_on_common_draws

    def spy(*args: Any, **kwargs: Any) -> Any:
        asked.append(kwargs.get("common_draws_of"))
        return real(*args, **kwargs)

    monkeypatch.setattr(
        importlib.import_module("flux_engine.stages.run"), "run_on_common_draws", spy
    )
    outcome = run_fit(
        FitRequest(
            bundle=bundle,
            profile=PROFILE,
            out=tmp_path / "calibrated",
            held_out=HELD_OUT,
            state_dir=tmp_path / "state",
        )
    )
    assert outcome.gate is not None
    assert len(asked) > 2
    assert all(item == bundle for item in asked)
