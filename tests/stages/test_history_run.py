# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history generates: a run of ``tests/fixtures/history-chain`` through ``plan``, ``apply`` and ``verify``.

The bundle is ``tests/fixtures/versioned`` with a ``history`` entry on ``subscription_version``:
60 subscriptions of 200 versions under 50 customers, chained by ``nextStart`` before 2025-12-31.
Every assertion reads the rows a run wrote. What is pinned:

* **The chain.** Per subscription no gap and no overlap, one open end, every start before
  ``until`` and every closed end before it; the customer of a subscription on every version.
* **Size.** ``--jobs 1`` and ``--jobs 4`` make the same run, ``--scale`` below 1 scales the
  entities as the rows, a scale that leaves the parents too few for the entities is refused naming
  ``--scale``, and the shares of ``--shard I/N`` joined are the whole run's bytes.
* **The window.** Two windows with one ``until`` give the history's emit byte for byte.
* **The kernel.** Kernel 0.1.0 refuses the history.
"""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator.history import ended_count
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import build_plan_document
from flux_engine.stages.verify import verify

apply_module = import_module("flux_engine.stages.apply")
plan_module = import_module("flux_engine.stages.plan")

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "history-chain"
NAMESPACE = "com.agentics.flux/1"
EMIT = "subscription_version"
UNTIL = "2025-12-31"


def _bundle(
    tmp_path: Path,
    *,
    name: str = "bundle",
    versions: int = 200,
    entities: int = 60,
    customers: int = 50,
    window_end: Optional[str] = None,
    minimum: Optional[int] = None,
    history: Optional[Dict[str, Any]] = None,
    columns: Optional[Dict[str, Any]] = None,
) -> Path:
    """A copy of the fixture, with its sizes (and, when asked, its window end) changed."""
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    flux = document["extensions"][NAMESPACE]
    emits = flux["emits"]
    emits["customer"]["rows"] = customers
    emits["customer"]["fidelity"]["columns"]["customer_id"]["range"]["max"] = str(customers)
    emits[EMIT]["rows"] = versions
    declared = emits[EMIT]["fidelity"]["columns"]
    declared["subscription_sk"]["range"]["max"] = str(versions)
    declared["subscription_id"]["range"]["max"] = str(max(entities, 200))
    flux["history"][0]["entities"] = entities
    if minimum is not None:
        flux["history"][0]["parent"]["minChildren"] = minimum
    if window_end is not None:
        document["spec"]["timeBounds"]["end"] = window_end
    flux["history"][0].update(history or {})
    declared.update(columns or {})
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def _rows(layout: RunLayout, expose_id: str) -> List[Dict[str, Any]]:
    names = json.loads(layout.seam_attestation_path(expose_id).read_text("utf-8"))["columns"]
    return [
        dict(zip(names, json.loads(line), strict=True))
        for line in layout.emit_path(expose_id).read_text("utf-8").splitlines()
    ]


def _run(bundle: Path, state: Path, **options: Any) -> RunLayout:
    planned = plan_module.plan(
        bundle, state_dir=state, kernel=KERNEL_V2.version, stage=False, **options
    )
    assert planned.success, planned.stderr
    run_id = planned.artifacts["run_id"]
    applied = apply_module.apply(bundle, run_id=run_id, state_dir=state)
    assert applied.success, applied.stderr
    return RunLayout.for_state_dir(state, run_id)


def _subscriptions(rows: List[Dict[str, Any]]) -> Dict[Any, List[Dict[str, Any]]]:
    found: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        found[row["subscription_id"]].append(row)
    return found


def _check_chains(rows: List[Dict[str, Any]], *, entities: int, customers: int) -> None:
    held = _subscriptions(rows)
    assert len(held) == entities
    assert {row["customer_id"] for row in rows} <= set(range(1, customers + 1))
    for versions in held.values():
        assert len({row["customer_id"] for row in versions}) == 1
        starts = [row["valid_from"] for row in versions]
        assert starts == sorted(set(starts)) and all(start < UNTIL for start in starts)
        ends = [row["valid_to"] for row in versions]
        assert [end is None for end in ends] == [False] * (len(ends) - 1) + [True]
        for index, end in enumerate(ends[:-1]):
            assert end < UNTIL and end == starts[index + 1]
        # The activation and the rest keep drawing as cells until later records set them.
        assert all(row["plan"] in ("basic", "pro", "team") for row in versions)


# --- plan, apply, verify ----------------------------------------------------------------------


def test_a_run_writes_chains_and_verify_passes(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    layout = _run(bundle, tmp_path / "state")
    rows = _rows(layout, EMIT)
    assert len(rows) == 200
    _check_chains(rows, entities=60, customers=50)
    assert sum(1 for row in rows if row["valid_to"] is None) == 60
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr
    # Every customer holds a subscription: minChildren is 1.
    assert {row["customer_id"] for row in rows} == set(range(1, 51))


def test_the_exposes_that_read_the_natural_key_name_a_subscription_that_exists(
    tmp_path: Path,
) -> None:
    layout = _run(_bundle(tmp_path), tmp_path / "state")
    held = {row["subscription_id"] for row in _rows(layout, EMIT)}
    assert {row["subscription_id"] for row in _rows(layout, "invoice")} <= held


def test_a_run_is_the_same_run_again(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    first = _run(bundle, tmp_path / "a")
    second = _run(bundle, tmp_path / "b")
    assert first.emit_path(EMIT).read_bytes() == second.emit_path(EMIT).read_bytes()


def test_kernel_010_refuses_the_history(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(_bundle(tmp_path), kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "kernel 0.2.0" in str(caught.value)


# --- jobs -------------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_four_jobs_make_the_run_one_job_makes(tmp_path: Path) -> None:
    versions = 3 * parallel.MIN_ROWS_PER_JOB + 11
    bundle = _bundle(tmp_path, versions=versions, entities=2500, customers=1200)
    one = pipeline.execute(bundle, jobs=1, kernel=KERNEL_V2)
    four = pipeline.execute(bundle, jobs=4, kernel=KERNEL_V2)
    assert build_plan_document(four) == build_plan_document(one)
    for mine, theirs in zip(four.emits, one.emits, strict=True):
        assert mine.result == theirs.result
        assert mine.writer.lines() == theirs.writer.lines()
    generated = [e for e in four.emits if e.plan is not None and e.plan.rows == versions]
    assert generated and all(e.processes > 1 for e in generated)


# --- scale ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scale", "entities", "rows", "customers"), [("0.5", 30, 100, 25), ("0.25", 15, 50, 13)]
)
def test_a_scale_below_one_scales_the_entities_as_the_rows(
    tmp_path: Path, scale: str, entities: int, rows: int, customers: int
) -> None:
    bundle = _bundle(tmp_path)
    layout = _run(bundle, tmp_path / "state", scale=scale)
    held = _rows(layout, EMIT)
    assert len(held) == rows
    _check_chains(held, entities=entities, customers=customers)


def test_a_scale_above_one_scales_the_entities_too(tmp_path: Path) -> None:
    layout = _run(_bundle(tmp_path), tmp_path / "state", scale="2")
    held = _rows(layout, EMIT)
    assert len(held) == 400
    _check_chains(held, entities=120, customers=100)


def test_a_scale_that_leaves_the_parents_too_few_for_the_entities_names_the_scale(
    tmp_path: Path,
) -> None:
    # Six customers and 12 entities with two entities each at the least: a quarter of them rounds
    # to two customers and three entities, and two customers cannot hold two each.
    bundle = _bundle(tmp_path, entities=12, customers=6, minimum=2)
    plain = plan_module.plan(
        bundle, state_dir=tmp_path / "plain", kernel=KERNEL_V2.version, stage=False
    )
    assert plain.success, plain.stderr
    refused = plan_module.plan(
        bundle, state_dir=tmp_path / "scaled", kernel=KERNEL_V2.version, stage=False, scale="0.25"
    )
    assert not refused.success
    assert refused.error_code == "FLX-3403" and "--scale 0.25" in refused.stderr


# --- shard ------------------------------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 3, 5])
def test_the_shares_joined_in_order_are_the_bytes_of_one_run(tmp_path: Path, count: int) -> None:
    bundle = _bundle(tmp_path)
    whole = _run(bundle, tmp_path / "whole")
    state = tmp_path / "shards"
    planned = plan_module.plan(bundle, state_dir=state, kernel=KERNEL_V2.version, stage=False)
    run_id = planned.artifacts["run_id"]
    assert run_id == whole.run_id
    for index in range(1, count + 1):
        part = apply_module.apply(bundle, run_id=run_id, state_dir=state, shard=(index, count))
        assert part.success, part.stderr
    shards = RunLayout.for_state_dir(state, run_id)
    for expose_id in ("customer", EMIT, "invoice"):
        joined = b"".join(
            shards.shard_emit_path(expose_id, index, count).read_bytes()
            for index in range(1, count + 1)
        )
        assert joined == whole.emit_path(expose_id).read_bytes(), expose_id


# --- the window -------------------------------------------------------------------------------


def test_two_windows_with_one_until_give_the_histories_emit_byte_for_byte(tmp_path: Path) -> None:
    narrow = _run(_bundle(tmp_path, name="narrow"), tmp_path / "a")
    wide = _run(_bundle(tmp_path, name="wide", window_end="2030-06-30T00:00:00Z"), tmp_path / "b")
    assert narrow.emit_path(EMIT).read_bytes() == wide.emit_path(EMIT).read_bytes()
    assert narrow.run_id != wide.run_id  # the plan holds the window, the history's rows do not


# --- end modes and activation -----------------------------------------------------------------

#: The statuses of a run that ends: ``cancelled`` is the end value, so no draw may give it.
STATUSES = {"status": {"levels": {"active": "0.7", "paused": "0.3", "cancelled": "0"}}}


def _end(mode: str, *, column: bool = True, share: str = "0.25") -> Dict[str, Any]:
    end: Dict[str, Any] = {"mode": mode, "share": share}
    if column:
        end.update(column="status", value="cancelled")
    return {"history": {"end": end, "activation": "started_on"}, "columns": STATUSES}


def _history_lines(rows: List[Dict[str, Any]], mode: str, *, column: bool) -> Dict[str, Any]:
    """The three ``history:*`` lines of the written rows, as verify reads them."""
    from flux_engine.concordance.history_checks import history_shares
    from flux_engine.expr2.scope import Versions
    from flux_engine.seam.history import Histogram, HistoryEnd, HistoryEntry

    held = HistoryEntry(
        emit=EMIT,
        entities=60,
        length=Histogram(("30", "1096"), ("1",)),
        until=UNTIL,
        end=HistoryEnd(mode, "0.25", "status" if column else None, "cancelled" if column else None),
    )
    found = history_shares(held, rows, Versions(("subscription_id",), "valid_from", "valid_to"))
    return {rule: (holds.satisfied, holds.total) for rule, holds in found}


@pytest.mark.parametrize("column", [True, False])
@pytest.mark.parametrize("mode", ["closeLast", "finalVersion"])
def test_a_run_with_an_end_mode_holds_the_history_lines_and_verify_passes(
    tmp_path: Path, mode: str, column: bool
) -> None:
    if mode == "finalVersion" and not column:
        pytest.skip("finalVersion needs a column")
    bundle = _bundle(tmp_path, **_end(mode, column=column))
    layout = _run(bundle, tmp_path / "state")
    rows = _rows(layout, EMIT)
    assert len(rows) == 200
    lines = _history_lines(rows, mode, column=column)
    assert lines and all(satisfied == total for satisfied, total in lines.values()), lines
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr
    held = _subscriptions(rows)
    opened = sum(1 for row in rows if row["valid_to"] is None)
    closed_last = sum(1 for versions in held.values() if versions[-1]["valid_to"] is not None)
    if mode == "finalVersion":
        assert opened == 60 and closed_last == 0
    else:
        assert opened + closed_last == 60 and closed_last > 0
    if column:
        eligible = sum(1 for versions in held.values() if len(versions) >= 2)
        ending = [row for row in rows if row["status"] == "cancelled"]
        assert len(ending) == ended_count(len(held), eligible, "0.25")
        for versions in held.values():
            assert [row["status"] for row in versions[:-1]].count("cancelled") == 0


def test_the_activation_holds_every_entitys_first_start(tmp_path: Path) -> None:
    layout = _run(_bundle(tmp_path, **_end("closeLast")), tmp_path / "state")
    for versions in _subscriptions(_rows(layout, EMIT)).values():
        assert {row["started_on"] for row in versions} == {versions[0]["valid_from"]}


@pytest.mark.parametrize("mode", ["closeLast", "finalVersion"])
def test_a_run_with_an_end_mode_is_the_same_run_at_any_jobs_and_shard(
    tmp_path: Path, mode: str
) -> None:
    bundle = _bundle(tmp_path, **_end(mode))
    whole = _run(bundle, tmp_path / "whole")
    state = tmp_path / "shards"
    planned = plan_module.plan(bundle, state_dir=state, kernel=KERNEL_V2.version, stage=False)
    run_id = planned.artifacts["run_id"]
    for index in range(1, 4):
        part = apply_module.apply(bundle, run_id=run_id, state_dir=state, shard=(index, 3))
        assert part.success, part.stderr
    shards = RunLayout.for_state_dir(state, run_id)
    joined = b"".join(shards.shard_emit_path(EMIT, index, 3).read_bytes() for index in range(1, 4))
    assert joined == whole.emit_path(EMIT).read_bytes()


def test_kernel_010_refuses_an_end_with_the_history(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(_bundle(tmp_path, **_end("closeLast")), kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "kernel 0.2.0" in str(caught.value)


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
@pytest.mark.parametrize("mode", ["closeLast", "finalVersion"])
def test_four_jobs_make_the_run_one_job_makes_under_an_end_mode(tmp_path: Path, mode: str) -> None:
    versions = 3 * parallel.MIN_ROWS_PER_JOB + 11
    bundle = _bundle(tmp_path, versions=versions, entities=2500, customers=1200, **_end(mode))
    one = pipeline.execute(bundle, jobs=1, kernel=KERNEL_V2)
    four = pipeline.execute(bundle, jobs=4, kernel=KERNEL_V2)
    for mine, theirs in zip(four.emits, one.emits, strict=True):
        assert mine.result == theirs.result
        assert mine.writer.lines() == theirs.writer.lines()


# --- tracked columns --------------------------------------------------------------------------

TRACKED = {
    "tracked": [
        {
            "column": "plan",
            "transitions": {
                "basic": {"pro": "0.8", "team": "0.2"},
                "pro": {"basic": "0.5", "team": "0.5"},
                "team": {"pro": "1"},
            },
        }
    ]
}


def _tracked(**more: Any) -> Dict[str, Any]:
    return {"history": {**TRACKED, **more.pop("history", {})}, **more}


def test_a_run_with_a_tracked_column_changes_it_once_a_version_and_verify_passes(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, **_tracked())
    layout = _run(bundle, tmp_path / "state")
    rows = _rows(layout, EMIT)
    moves = {"basic": {"pro", "team"}, "pro": {"basic", "team"}, "team": {"pro"}}
    changes = 0
    for versions in _subscriptions(rows).values():
        for before, after in zip(versions, versions[1:], strict=False):
            changes += 1
            assert after["plan"] in moves[before["plan"]]
        # Every column the history does not set is the first version's cell, on every version.
        for column in ("customer_id", "status", "started_on"):
            assert len({row[column] for row in versions}) == 1, column
    assert changes == 200 - 60
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr


def test_a_tracked_column_with_an_end_value_and_the_activation_passes_verify(
    tmp_path: Path,
) -> None:
    options = _tracked(history={**_end("finalVersion")["history"]}, columns=STATUSES)
    bundle = _bundle(tmp_path, **options)
    layout = _run(bundle, tmp_path / "state")
    rows = _rows(layout, EMIT)
    ending = sum(1 for row in rows if row["status"] == "cancelled")
    assert ending > 0
    held = _subscriptions(rows)
    # An ending version is no change: the plan carries over it.
    for versions in held.values():
        if versions[-1]["status"] == "cancelled":
            assert versions[-1]["plan"] == versions[-2]["plan"]
    ordinary = 200 - 60 - ending
    moved = sum(
        1
        for versions in held.values()
        for before, after in zip(versions, versions[1:], strict=False)
        if before["plan"] != after["plan"]
    )
    assert moved == ordinary
    assert verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state").success


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_four_jobs_make_the_run_one_job_makes_with_tracked_columns(tmp_path: Path) -> None:
    versions = 3 * parallel.MIN_ROWS_PER_JOB + 11
    bundle = _bundle(
        tmp_path, versions=versions, entities=2500, customers=1200, **_tracked(**_end("closeLast"))
    )
    one = pipeline.execute(bundle, jobs=1, kernel=KERNEL_V2)
    four = pipeline.execute(bundle, jobs=4, kernel=KERNEL_V2)
    assert build_plan_document(four) == build_plan_document(one)
    for mine, theirs in zip(four.emits, one.emits, strict=True):
        assert mine.result == theirs.result
        assert mine.writer.lines() == theirs.writer.lines()
    generated = [e for e in four.emits if e.plan is not None and e.plan.rows == versions]
    assert generated and all(e.processes > 1 for e in generated)


def test_the_shares_of_a_tracked_run_joined_are_the_bytes_of_one_run(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, **_tracked())
    whole = _run(bundle, tmp_path / "whole")
    state = tmp_path / "shards"
    planned = plan_module.plan(bundle, state_dir=state, kernel=KERNEL_V2.version, stage=False)
    run_id = planned.artifacts["run_id"]
    for index in range(1, 6):
        part = apply_module.apply(bundle, run_id=run_id, state_dir=state, shard=(index, 5))
        assert part.success, part.stderr
    shards = RunLayout.for_state_dir(state, run_id)
    joined = b"".join(shards.shard_emit_path(EMIT, index, 5).read_bytes() for index in range(1, 6))
    assert joined == whole.emit_path(EMIT).read_bytes()


def test_a_scale_below_one_keeps_every_tracked_change_exact(tmp_path: Path) -> None:
    layout = _run(_bundle(tmp_path, **_tracked()), tmp_path / "state", scale="0.5")
    rows = _rows(layout, EMIT)
    assert len(rows) == 100
    held = _subscriptions(rows)
    assert len(held) == 30
    changed = sum(
        1
        for versions in held.values()
        for before, after in zip(versions, versions[1:], strict=False)
        if before["plan"] != after["plan"]
    )
    assert changed == 100 - 30


def test_kernel_010_refuses_a_tracked_history(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(_bundle(tmp_path, **_tracked()), kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "kernel 0.2.0" in str(caught.value)
