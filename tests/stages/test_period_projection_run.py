# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Periodic rows generate: a run of ``tests/fixtures/history-projections`` through ``plan``, ``apply`` and ``verify``.

The bundle is a history of 60 subscriptions in 200 versions and ``subscription_month``, a periodic
emit with a row for each version and month it holds (ADR 0027 section 5), which declares no
``rows``. Every assertion reads the rows a run wrote or the plan it recorded. What is pinned:

* **The rows.** Period by period, the period column holding the period's first day; the plan
  records the derived count; kernel 0.1.0 refuses the key.
* **The version.** Each row's columns that name the history's columns are a version its period holds;
  ``verify`` holds ``period:within_validity`` and ``period:matches_valid_version`` under each match,
  with kinds, and with an ended history.
* **The kinds.** Each kind's rows follow its count, its condition and its overrides; the shares of a
  run with kinds joined are the whole run's bytes.
* **Size.** ``--jobs 1`` and ``--jobs 4`` make the same run, ``--scale`` below 1 follows the scaled
  history, and the shares of ``--shard I/N``, which cut across period boundaries, joined are the
  whole run's bytes.
* **The window.** A longer window with the same ``until`` begins with the shorter window's rows,
  the key included, which takes one step per row from the low end of its range, and every other
  emit is the same byte for byte.
* **The key.** A primary key that is no whole number, and a column under ``allocation: exact``,
  are refused; the key of a snapshot or an events emit is still spread over its range.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify

from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run
from .test_projection_plan_run import (
    HISTORY,
    NAMESPACE,
    _bundle,
    _plan_document,
    _rows,
    _run,
    plan_module,
)

pytestmark = pytest.mark.integration

PERIODIC = "subscription_month"
EVENTS = "subscription_change"
SNAPSHOT = "subscription_current"
FINAL = {"mode": "finalVersion", "share": "0.25", "column": "status", "value": "cancelled"}
CLOSE = {"mode": "closeLast", "share": "0.25", "column": "status", "value": "cancelled"}


def _periodic(tmp_path: Path, name: str = "bundle", **options: Any) -> Path:
    """The fixture, with *grain* and *match* set on its periods entry, and the bundle's own options."""
    grain = options.pop("grain", None)
    match = options.pop("match", None)
    bundle = _bundle(tmp_path, name=name, **options)
    if grain is not None or match is not None:
        path = bundle / "simulation.flux.yml"
        document = yaml.safe_load(path.read_text("utf-8"))
        entry = document["extensions"][NAMESPACE]["periods"][0]
        entry["grain"] = grain or entry["grain"]
        entry["match"] = match or entry["match"]
        path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return bundle


def _held(layout: RunLayout) -> int:
    """The number of periodic rows a run wrote."""
    return len(_rows(layout, PERIODIC))


KINDS = [
    {
        "count": {"levels": ["0", "1", "3"], "levelWeights": ["0.2", "0.5", "0.3"]},
        "when": "valid_from < DATE '2024-06-01'",
        "overrides": {"plan": "old"},
    },
    {
        "count": 1,
        "when": "valid_from >= DATE '2024-06-01' AND month >= DATE '2024-01-01'",
        "overrides": {"plan": "new"},
    },
]


def _with_kinds(bundle: Path) -> Path:
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["extensions"][NAMESPACE]["periods"][0]["kinds"] = KINDS
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return bundle


# --- the rows ---------------------------------------------------------------------------------


@pytest.mark.parametrize("match", ["overlap", "start", "end"])
def test_a_run_writes_the_periods_in_order_from_the_windows_start(
    tmp_path: Path, match: str
) -> None:
    layout = _run(_periodic(tmp_path, match=match), tmp_path / "state")
    rows = _rows(layout, PERIODIC)
    months = [row["month"] for row in rows]
    assert rows and months == sorted(months)
    assert all(month.endswith("-01") for month in months)
    assert "2023-01-01" <= months[0] and months[-1] <= "2025-12-01"
    assert _plan_document(layout)["emits"][PERIODIC]["rows"] == len(rows)
    assert _plan_document(layout)["emits"][PERIODIC]["rowsFrom"] == {
        "of": HISTORY,
        "projection": "periods",
    }


def test_the_end_match_holds_fewer_rows_than_overlap(tmp_path: Path) -> None:
    overlap = _run(_periodic(tmp_path, "o", match="overlap"), tmp_path / "o")
    closing = _run(_periodic(tmp_path, "e", match="end"), tmp_path / "e")
    assert _held(closing) <= _held(overlap)


def test_a_run_is_the_same_run_again(tmp_path: Path) -> None:
    bundle = _periodic(tmp_path)
    first = _run(bundle, tmp_path / "a")
    second = _run(bundle, tmp_path / "b")
    assert first.emit_path(PERIODIC).read_bytes() == second.emit_path(PERIODIC).read_bytes()


def test_a_declared_rows_on_the_periodic_emit_is_refused(tmp_path: Path) -> None:
    bundle = _periodic(tmp_path)
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["extensions"][NAMESPACE]["emits"][PERIODIC]["rows"] = 10
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    planned = plan_module.plan(
        bundle, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert not planned.success
    assert planned.error_code == "FLX-3403" and PERIODIC in planned.stderr


def test_kernel_010_refuses_the_periods(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(_periodic(tmp_path), kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "kernel 0.2.0" in str(caught.value)


# --- verify -----------------------------------------------------------------------------------


@pytest.mark.parametrize("match", ["overlap", "start", "end"])
@pytest.mark.parametrize("end", [None, CLOSE, FINAL])
def test_verify_holds_the_period_lines_under_each_match(
    tmp_path: Path, match: str, end: Optional[Dict[str, Any]]
) -> None:
    bundle = _periodic(tmp_path, match=match, end=end)
    layout = _run(bundle, tmp_path / "state")
    versions = _rows(layout, HISTORY)
    rows = _rows(layout, PERIODIC)
    held = {(v["subscription_id"], v["plan"]) for v in versions}
    assert rows and all((row["subscription_id"], row["plan"]) in held for row in rows)
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr
    text = verified.stdout + verified.stderr
    assert "period:within_validity" in text and "period:matches_valid_version" in text


def test_verify_holds_the_period_lines_with_kinds_and_a_quarterly_grain(tmp_path: Path) -> None:
    bundle = _with_kinds(_periodic(tmp_path, grain="quarter", end=CLOSE))
    layout = _run(bundle, tmp_path / "state")
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr
    assert "period:within_validity" in verified.stdout + verified.stderr


# --- from and through -------------------------------------------------------------------------


def _ranged(tmp_path: Path, name: str, **keys: str) -> Path:
    """The fixture with *keys* (``from``, ``through``) added to its periods entry."""
    bundle = _periodic(tmp_path, name)
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["extensions"][NAMESPACE]["periods"][0].update(keys)
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return bundle


def test_a_range_equal_to_the_window_leaves_the_rows_unchanged(tmp_path: Path) -> None:
    plain = _run(_periodic(tmp_path, "plain"), tmp_path / "a")
    ranged = _run(
        _ranged(tmp_path, "ranged", **{"from": "2023-01-01", "through": "2025-12-01"}),
        tmp_path / "b",
    )
    # The entry is part of the bundle's digest, so the run's seeds differ and the cells with them;
    # the periods the rows lie on do not.
    assert {r["month"] for r in _rows(plain, PERIODIC)} == {
        r["month"] for r in _rows(ranged, PERIODIC)
    }


def test_a_narrower_through_cuts_the_rows_and_verify_passes(tmp_path: Path) -> None:
    bundle = _ranged(tmp_path, "cut", **{"from": "2023-04-01", "through": "2024-06-01"})
    layout = _run(bundle, tmp_path / "state")
    rows = _rows(layout, PERIODIC)
    months = {row["month"] for row in rows}
    assert months and min(months) >= "2023-04-01" and max(months) <= "2024-06-01"
    whole = _held(_run(_periodic(tmp_path, "whole"), tmp_path / "w"))
    assert 0 < len(rows) < whole
    assert _plan_document(layout)["emits"][PERIODIC]["rows"] == len(rows)
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr
    assert "period:within_validity" in verified.stdout + verified.stderr


def test_a_longer_window_appends_no_period_past_through(tmp_path: Path) -> None:
    keys = {"through": "2025-06-01"}
    narrow = _run(_ranged(tmp_path, "n", **keys), tmp_path / "a")
    wide_bundle = _ranged(tmp_path, "w", **keys)
    path = wide_bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["spec"]["timeBounds"]["end"] = "2027-06-30T00:00:00Z"
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    wide = _run(wide_bundle, tmp_path / "b")
    assert max(row["month"] for row in _rows(wide, PERIODIC)) <= "2025-06-01"
    assert _held(wide) == _held(narrow)


@pytest.mark.parametrize(
    ("keys", "needle"),
    [
        ({"from": "2022-12-31"}, "before timeBounds.start"),
        ({"from": "2026-01-01"}, "not before timeBounds.end"),
        ({"through": "2026-01-01"}, "not before timeBounds.end"),
        ({"from": "2024-01-01", "through": "2023-06-01"}, "before its from"),
        ({"from": "2024-1-1"}, "YYYY-MM-DD"),
    ],
)
def test_a_range_outside_the_window_is_refused_at_plan(
    tmp_path: Path, keys: Dict[str, str], needle: str
) -> None:
    planned = plan_module.plan(
        _ranged(tmp_path, "bad", **keys),
        state_dir=tmp_path / "state",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert not planned.success and planned.error_code == "FLX-3403"
    assert needle in planned.stderr and PERIODIC in planned.stderr


# --- the kinds --------------------------------------------------------------------------------


def test_each_kind_holds_its_pairs_and_writes_its_overrides(tmp_path: Path) -> None:
    layout = _run(_with_kinds(_periodic(tmp_path, end=CLOSE)), tmp_path / "state")
    rows = _rows(layout, PERIODIC)
    assert rows and {row["plan"] for row in rows} == {"old", "new"}
    assert all(row["month"] >= "2024-01-01" for row in rows if row["plan"] == "new")
    assert _plan_document(layout)["emits"][PERIODIC]["rows"] == len(rows)


@pytest.mark.parametrize("count", [3, 7])
def test_the_shares_of_a_run_with_kinds_joined_are_the_bytes_of_one_run(
    tmp_path: Path, count: int
) -> None:
    bundle = _with_kinds(_periodic(tmp_path, end=CLOSE))
    whole = _run(bundle, tmp_path / "whole")
    assert_shares_join_to_the_run(bundle, tmp_path, whole, count, (PERIODIC,))


def test_one_job_and_four_jobs_make_the_same_run_with_kinds(tmp_path: Path) -> None:
    bundle = _with_kinds(_periodic(tmp_path))
    assert_same_run_at_one_and_four_jobs(bundle, kernel=KERNEL_V2)


# --- jobs, scale, shard -----------------------------------------------------------------------


def test_one_job_and_four_jobs_make_the_same_run(tmp_path: Path) -> None:
    bundle = _periodic(tmp_path, end=CLOSE)
    assert_same_run_at_one_and_four_jobs(bundle, kernel=KERNEL_V2)


@pytest.mark.parametrize(("scale", "versions"), [("0.5", 100), ("0.25", 50)])
def test_a_scale_below_one_follows_the_scaled_history(
    tmp_path: Path, scale: str, versions: int
) -> None:
    full = _run(_periodic(tmp_path, "full"), tmp_path / "full")
    layout = _run(_periodic(tmp_path, "scaled"), tmp_path / "scaled", scale=scale)
    assert len(_rows(layout, HISTORY)) == versions
    count = len(_rows(layout, PERIODIC))
    assert 0 < count < _held(full)
    assert _plan_document(layout)["emits"][PERIODIC]["rows"] == count  # not scaled again


@pytest.mark.parametrize("count", [1, 3, 7])
def test_the_shares_joined_in_order_are_the_bytes_of_one_run(tmp_path: Path, count: int) -> None:
    bundle = _periodic(tmp_path, end=CLOSE)
    whole = _run(bundle, tmp_path / "whole")
    assert_shares_join_to_the_run(bundle, tmp_path, whole, count, (PERIODIC,))


# --- the window -------------------------------------------------------------------------------


@pytest.mark.parametrize("end", [None, CLOSE])
@pytest.mark.parametrize("match", ["overlap", "start"])
def test_a_longer_window_begins_with_the_shorter_windows_rows(
    tmp_path: Path, end: Optional[Dict[str, Any]], match: str
) -> None:
    narrow = _run(_periodic(tmp_path, "narrow", end=end, match=match), tmp_path / "a")
    wide = _run(
        _periodic(tmp_path, "wide", end=end, match=match, window_end="2027-06-30T00:00:00Z"),
        tmp_path / "b",
    )
    old = narrow.emit_path(PERIODIC).read_bytes().splitlines()
    new = wide.emit_path(PERIODIC).read_bytes().splitlines()
    assert len(new) > len(old) and new[: len(old)] == old
    for expose_id in (HISTORY, EVENTS, SNAPSHOT):
        assert narrow.emit_path(expose_id).read_bytes() == wide.emit_path(expose_id).read_bytes()
    assert narrow.run_id != wide.run_id


def test_a_periodic_key_takes_one_step_per_row_from_the_low_end(tmp_path: Path) -> None:
    layout = _run(_periodic(tmp_path), tmp_path / "state")
    keys = [row["period_id"] for row in _rows(layout, PERIODIC)]
    assert keys == list(range(1, len(keys) + 1))


def test_the_key_of_a_snapshot_or_an_events_emit_is_still_spread_over_its_range(
    tmp_path: Path,
) -> None:
    layout = _run(_periodic(tmp_path), tmp_path / "state")
    snapshot = [row["subscription_id"] for row in _rows(layout, SNAPSHOT)]
    assert len(snapshot) == 60 and snapshot != list(range(1, 61)) and max(snapshot) > 60
    events = [row["event_id"] for row in _rows(layout, EVENTS)]
    assert events != list(range(1, len(events) + 1))


def _refused(tmp_path: Path, edit: Any) -> Any:
    bundle = _periodic(tmp_path)
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    edit(document["extensions"][NAMESPACE]["emits"][PERIODIC]["fidelity"]["columns"])
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return plan_module.plan(
        bundle, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )


def test_a_periodic_primary_key_that_is_no_whole_number_is_refused(tmp_path: Path) -> None:
    bundle = _periodic(tmp_path)
    contract = bundle / "subscriptions.fluid.yml"
    text = contract.read_text("utf-8")
    old = "{ name: period_id, type: INT, required: true }"
    assert old in text
    changed = text.replace(old, "{ name: period_id, type: DATE, required: true }")
    contract.write_text(changed, "utf-8")
    digest = digest_of_document(
        yaml.safe_load(changed), subject="contractDigest:billing.subscriptions"
    )
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    for emit in document["spec"]["emits"]:
        emit["provenance"]["contractDigest"] = digest.value
    columns = document["extensions"][NAMESPACE]["emits"][PERIODIC]["fidelity"]["columns"]
    columns["period_id"] = {"range": {"min": "2000-01-01", "max": "2090-01-01"}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    planned = plan_module.plan(
        bundle, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert not planned.success
    assert planned.error_code == "FLX-3403" and PERIODIC in planned.stderr


def test_a_column_of_a_periodic_emit_under_allocation_exact_is_refused(tmp_path: Path) -> None:
    def edit(columns: Dict[str, Any]) -> None:
        columns["plan"]["allocation"] = "exact"

    planned = _refused(tmp_path, edit)
    assert not planned.success
    assert planned.error_code == "FLX-3403" and PERIODIC in planned.stderr


# --- the command line -------------------------------------------------------------------------


def test_the_command_line_plans_and_applies_the_periodic_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from flux_engine.cli import main

    bundle = _periodic(tmp_path, grain="quarter")
    state = str(tmp_path / "state")
    assert main(["plan", str(bundle), "--kernel", "0.2.0", "--state-dir", state, "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    run_id = envelope["artifacts"]["run_id"]
    assert main(["apply", str(bundle), "--run-id", run_id, "--state-dir", state, "--json"]) == 0
    layout = RunLayout.for_state_dir(Path(state), run_id)
    rows: List[Dict[str, Any]] = _rows(layout, PERIODIC)
    assert {row["month"] for row in rows} <= {
        f"{year}-{month}-01" for year in (2023, 2024, 2025) for month in ("01", "04", "07", "10")
    }
