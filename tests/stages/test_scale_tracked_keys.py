# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A tracked foreign key never leaves a child naming keys its parent lacks, at any ``--scale``.

The bundle is ``tests/fixtures/history-fleet`` with a catalogue, ``body_type`` (26 drawn rows keyed
101..126), and a history ``vehicle_version`` that tracks ``body_type_id``, a foreign key into it, over
26 levels, one for each key (ADR 0017 section 6). A tracked column writes its level as the cell and
takes no parent row, so a level is a value of the parent's key only while the parent holds that key.
``--scale`` below 1 cuts the catalogue's rows and its key range and leaves the levels where they are,
so the plan keeps the catalogue at the fewest rows whose keys hold every level. What is pinned:

* **The floor.** At ``--scale 0.25`` the catalogue keeps all 26 rows, ``plan`` prints one note naming
  it, the rows and the scale, and ``plan.json`` says ``scaleFloors``; the run holds every level as a
  key and ``verify`` scores the link at 100 %, at any ``--jobs``.
* **The refusal.** A level that is no key of the catalogue at any row count is ``FLX-3403`` at every
  scale, naming the expose, the column, the level and the keys the plan holds.
* **The run.** At scale 1 and above the bundle plans and applies as it did, writes no marker, and every
  cell is a key of the catalogue.
* **The replay.** A plan recorded without the marker replays as it was made, with no check and no
  floor; one that names another value is ``FLX-2102``.
* **The pick.** A foreign key that is not tracked takes the nearest key the catalogue holds, so the same
  levels at ``--scale 0.25`` keep the link whole, and no marker is written.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.core.digest import build_plan_digest
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator import relations
from flux_engine.stages.apply import load_recorded_plan, plan_scale_floors
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.replay import replay
from flux_engine.stages.verify import verify

from ._fleet import (
    CONTRACT,
    HISTORY,
    NAMESPACE,
    PRODUCT,
    apply_module,
    bundle,
    expose,
    plan_module,
    repin,
    rows,
    run_bundle,
)
from .test_declared_range_replay import recorded_digest

pytestmark = pytest.mark.integration

KEY = "scaleFloors"
CATALOGUE = "body_type"
COLUMN = "body_type_id"
KEYS = [str(key) for key in range(101, 127)]
#: Twenty-four levels at 4 % and two at 2 %: the weights total one exactly.
WEIGHTS = {level: ("0.04" if place < len(KEYS) - 2 else "0.02") for place, level in enumerate(KEYS)}


def _catalogue_bundle(
    tmp_path: Path,
    *,
    levels: Optional[List[str]] = None,
    weights: Optional[Dict[str, str]] = None,
    tracked: bool = True,
    name: str = "bundle",
) -> Path:
    """The fleet with a 26-row catalogue and a history column of *levels* pointing at it."""
    target = bundle(tmp_path, name=name)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    contract["exposes"].insert(
        1,
        {
            "exposeId": CATALOGUE,
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "glue_table",
                "location": {"database": "fleet", "table": CATALOGUE, "region": "eu-west-1"},
            },
            "contract": {
                "schema": [
                    {"name": COLUMN, "type": "INT", "required": True},
                    {"name": "label", "type": "STRING", "required": True},
                ]
            },
            "semantics": {"entities": [{"name": CATALOGUE, "type": "primary", "expr": COLUMN}]},
        },
    )
    history = expose(contract, HISTORY)
    history["contract"]["schema"].append({"name": COLUMN, "type": "INT", "required": True})
    history["semantics"]["relationships"].append(
        {
            "name": CATALOGUE,
            "to": CATALOGUE,
            "fromColumns": [COLUMN],
            "toColumns": [COLUMN],
            "cardinality": "many_to_one",
            "joinType": "inner",
        }
    )
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["spec"]["emits"].insert(
        1,
        {
            "productRef": PRODUCT,
            "exposeId": CATALOGUE,
            "fluidVersion": "0.7.7",
            "provenance": {"contractDigest": "sha256:" + "0" * 64},
        },
    )
    extension = document["extensions"][NAMESPACE]
    extension["emits"][CATALOGUE] = {
        "rows": len(KEYS),
        "fidelity": {
            "encoding": {"fractionalBits": 16, "rounding": "floor"},
            "records": {"min": 1, "max": 1000000},
            "columns": {
                COLUMN: {"range": {"min": KEYS[0], "max": KEYS[-1]}},
                "label": {"levels": {"a": "0.5", "b": "0.5"}},
            },
            "properties": [
                {
                    "property": "mean",
                    "column": COLUMN,
                    "tolerance": {"type": "absolute", "value": "1000"},
                }
            ],
            "budget": {"maxChecksPerState": 20, "maxStateAge": "P30D"},
            "failureAction": "withholdBundle",
        },
    }
    held = levels if levels is not None else KEYS
    weights = weights or {level: WEIGHTS.get(level, "0.02") for level in held}
    extension["emits"][HISTORY]["fidelity"]["columns"][COLUMN] = {"levels": weights}
    if tracked:
        # Each level moves to the next: a ring over the levels, so a run changes it on every version.
        extension["history"][0]["tracked"] = [
            {
                "column": COLUMN,
                "transitions": {
                    level: {held[(place + 1) % len(held)]: "1"} for place, level in enumerate(held)
                },
            }
        ]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    repin(target, contract)
    return target


def _refused(target: Path, state: Path, **options: Any) -> str:
    """The text a refused plan of *target* ends in, which is ``FLX-3403``."""
    planned = plan_module.plan(
        target, state_dir=state, kernel=KERNEL_V2.version, stage=False, **options
    )
    assert not planned.success
    assert "FLX-3403" in planned.stderr, planned.stderr
    return str(planned.stderr)


def _link(target: Path, state: Path, **options: Any) -> str:
    """Plan, apply and verify *target*; the verified link's line, after checking every cell is a key."""
    layout = run_bundle(target, state, **options)
    keys = {row[COLUMN] for row in rows(layout, CATALOGUE)}
    cells = {row[COLUMN] for row in rows(layout, HISTORY)}
    assert cells <= keys, sorted(cells - keys)
    verified = verify(target, run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stdout + verified.stderr
    found = re.search(
        rf"integrity {HISTORY}\.{CATALOGUE} -> {CATALOGUE}: found .*", verified.stdout
    )
    assert found is not None, verified.stdout
    return found.group(0)


def test_a_scale_that_cuts_the_keys_below_a_tracked_level_keeps_the_catalogue_whole(
    tmp_path: Path,
) -> None:
    target, state = _catalogue_bundle(tmp_path), tmp_path / "state"
    planned = plan_module.plan(
        target, state_dir=state, kernel=KERNEL_V2.version, stage=False, scale="0.25"
    )
    assert planned.success, planned.stderr
    note = (
        f"note: {CATALOGUE} keeps 26 rows at --scale 0.25, "
        f"the fewest that hold every level {HISTORY}.{COLUMN} tracks"
    )
    assert planned.stdout.splitlines().count(note) == 1, planned.stdout
    layout = RunLayout.for_state_dir(state, planned.artifacts["run_id"])
    assert load_recorded_plan(layout)[KEY] == "trackedLevels"
    line = _link(target, tmp_path / "again", scale="0.25")
    assert "found 50 of 50 (100.00%)" in line


def _only_run(state: Path) -> str:
    (run,) = (path.name for path in (state / "runs").iterdir())
    return run


def test_the_floored_run_is_the_same_bytes_at_any_job_count(tmp_path: Path) -> None:
    target = _catalogue_bundle(tmp_path)
    one = run_bundle(target, tmp_path / "one", scale="0.25", jobs=1)
    four = run_bundle(target, tmp_path / "four", scale="0.25", jobs=4)
    assert one.run_id == four.run_id
    for expose_id in (CATALOGUE, HISTORY):
        assert one.emit_path(expose_id).read_bytes() == four.emit_path(expose_id).read_bytes()
    assert len(rows(one, CATALOGUE)) == len(KEYS)
    # A plan that floored replays floored: the check and the floor are worked out again.
    digest = recorded_digest(tmp_path / "one", one.run_id)
    again = replay(
        target, run_id=one.run_id, assert_digest=digest, state_dir=tmp_path / "one", jobs=4
    )
    assert again.success, again.stderr


@pytest.mark.parametrize("scale", [None, "0.25", "1"])
@pytest.mark.parametrize("level", ["127", "100"])
def test_a_level_that_is_no_key_at_any_row_count_is_refused(
    tmp_path: Path, scale: Optional[str], level: str
) -> None:
    target = _catalogue_bundle(tmp_path, levels=[*KEYS[:-1], level])
    text = _refused(target, tmp_path / "state", **({} if scale is None else {"scale": scale}))
    assert f"expose '{HISTORY}' column '{COLUMN}'" in text and f"'{CATALOGUE}'" in text
    assert "plan without --scale" in text and "keys 101 to " in text
    if scale != "0.25":  # the cut catalogue refuses on the first level it lost
        assert f"level '{level}'" in text


def test_a_level_below_the_first_key_is_refused_at_twice_the_rows(tmp_path: Path) -> None:
    target = _catalogue_bundle(tmp_path, levels=[*KEYS[:-1], "100"])
    assert "level '100'" in _refused(target, tmp_path / "state", scale="2")


#: The versions a run writes at each scale: 200 declared, scaled and rounded half up.
VERSIONS = {None: 200, "1": 200, "1.5": 300, "2": 400}


@pytest.mark.parametrize("scale", sorted(VERSIONS, key=str))
def test_the_levels_at_scale_one_and_above_are_keys_and_the_link_is_whole(
    tmp_path: Path, scale: Optional[str]
) -> None:
    line = _link(_catalogue_bundle(tmp_path), tmp_path / "state", scale=scale)
    assert f"found {VERSIONS[scale]} of {VERSIONS[scale]} (100.00%)" in line


def test_an_untracked_foreign_key_takes_the_nearest_key_of_the_cut_catalogue(
    tmp_path: Path,
) -> None:
    target, state = _catalogue_bundle(tmp_path, tracked=False), tmp_path / "state"
    assert "found 50 of 50 (100.00%)" in _link(target, state, scale="0.25")
    assert KEY not in load_recorded_plan(RunLayout.for_state_dir(state, _only_run(state)))


def test_a_run_at_scale_one_and_above_writes_no_marker(tmp_path: Path) -> None:
    target = _catalogue_bundle(tmp_path)
    for scale in (None, "1", "2"):
        layout = run_bundle(target, tmp_path / f"state-{scale}", scale=scale)
        assert KEY not in load_recorded_plan(layout)
    layout = run_bundle(_catalogue_bundle(tmp_path, tracked=False, name="plain"), tmp_path / "s")
    assert KEY not in load_recorded_plan(layout)


def test_a_plan_recorded_without_the_marker_replays_as_it_was_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planned with neither the check nor the floor, as before they existed: the cut catalogue stands."""
    target, state = _catalogue_bundle(tmp_path), tmp_path / "state"
    with monkeypatch.context() as patched:
        patched.setattr(relations, "_check_tracked_keys", lambda *args: None)
        layout = run_bundle(target, state, scale="0.25")
    assert KEY not in load_recorded_plan(layout)
    assert len(rows(layout, CATALOGUE)) == 7
    recorded = layout.emit_path(HISTORY).read_bytes()
    digest = recorded_digest(state, layout.run_id)
    for jobs in (1, 4):
        replayed = replay(
            target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=jobs
        )
        assert replayed.success, replayed.stderr
        assert layout.emit_path(HISTORY).read_bytes() == recorded
    layout.emit_path(HISTORY).unlink()
    applied = apply_module.apply(target, run_id=layout.run_id, state_dir=state)
    assert applied.success, applied.stderr
    assert layout.emit_path(HISTORY).read_bytes() == recorded


def test_the_marker_moves_the_plan_digest_of_a_plan_that_floored_only(tmp_path: Path) -> None:
    layout = run_bundle(_catalogue_bundle(tmp_path), tmp_path / "state", scale="0.25")
    document = dict(load_recorded_plan(layout))
    del document[KEY]
    assert build_plan_digest(document).ref.value != document["planDigest"]


def test_the_marker_is_read_as_a_known_value_or_not_at_all() -> None:
    assert plan_scale_floors({}) is False
    assert plan_scale_floors({KEY: "trackedLevels"}) is True


@pytest.mark.parametrize("value", ["everything", ["trackedLevels"], True, None])
def test_a_floor_value_this_engine_does_not_know_is_refused(value: Any) -> None:
    with pytest.raises(FluxError, match=f"FLX-2102.*{KEY}"):
        plan_scale_floors({KEY: value})


def _conditioned(target: Path) -> None:
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["extensions"][NAMESPACE]["conditions"] = {
        CATALOGUE: [
            {"values": {"label": "a"}, "count": 10},
            {"values": {"label": "b"}, "count": 16},
        ]
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")


def test_ship_counts_a_floored_parents_condition_groups_as_the_run_holds_them(
    tmp_path: Path,
) -> None:
    from flux_engine.stages.ship import _declared_conditions

    target = _catalogue_bundle(
        tmp_path,
        levels=["101", "103", "110", "115"],
        weights=dict.fromkeys(["101", "103", "110", "115"], "0.25"),
    )
    _conditioned(target)
    layout = run_bundle(target, tmp_path / "state", scale="0.25")
    recorded = load_recorded_plan(layout)
    assert recorded[KEY] == "trackedLevels"
    held = rows(layout, CATALOGUE)
    labels = sorted(sum(1 for row in held if row["label"] == name) for name in ("a", "b"))
    assert sum(labels) == len(held)
    [(expose_id, groups)] = _declared_conditions(target, "sim-fleet", recorded)
    assert expose_id == CATALOGUE
    assert sorted(count for count, _ in groups) == labels


def test_ship_scales_a_plan_without_a_floor_as_it_did(tmp_path: Path) -> None:
    from flux_engine.stages.ship import _declared_conditions

    target = _catalogue_bundle(tmp_path)
    _conditioned(target)
    layout = run_bundle(target, tmp_path / "state", scale="1")
    recorded = load_recorded_plan(layout)
    assert KEY not in recorded
    [(_, groups)] = _declared_conditions(target, "sim-fleet", recorded)
    assert [count for count, _ in groups] == [10, 16]


def _small_catalogue(tmp_path: Path, rows: int, high: int, levels: List[str]) -> Path:
    """The catalogue with *rows* drawn rows keyed 1..*high*, and a history column of *levels*."""
    target = _catalogue_bundle(tmp_path, levels=levels, weights=_even(levels))
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    emit = document["extensions"][NAMESPACE]["emits"][CATALOGUE]
    emit["rows"] = rows
    emit["fidelity"]["columns"][COLUMN] = {"range": {"min": 1, "max": high}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def _even(levels: List[str]) -> Dict[str, str]:
    first = Decimal(1) - Decimal("0.01") * (100 // len(levels)) * (len(levels) - 1)
    share = Decimal("0.01") * (100 // len(levels))
    return {level: str(first if place == 0 else share) for place, level in enumerate(levels)}


@pytest.mark.parametrize(
    ("rows", "high", "levels", "scale"),
    [
        (13, 26, ["1", "3"], "0.5"),
        (13, 26, ["1", "26"], "0.5"),
        (20, 40, ["1", "5", "37"], "0.3"),
        (40, 80, ["2", "70"], "0.55"),
        (8, 24, ["1", "20"], "0.4"),
    ],
)
def test_a_floor_always_raises_the_rows_and_the_note_and_marker_appear_when_it_does(
    tmp_path: Path, rows: int, high: int, levels: List[str], scale: str
) -> None:
    from flux_engine.stages.scaling import scaled_rows

    target = _small_catalogue(tmp_path, rows, high, levels)
    planned = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False, scale=scale
    )
    if not planned.success:
        assert "FLX-3403" in planned.stderr
        return
    layout = RunLayout.for_state_dir(tmp_path / "state", planned.artifacts["run_id"])
    recorded = load_recorded_plan(layout)
    kept = recorded["emits"][CATALOGUE]["generator"]["rows"]
    scaled = scaled_rows(rows, Decimal(scale))
    noted = f"note: {CATALOGUE} keeps {kept} rows" in planned.stdout
    assert noted == (KEY in recorded) == (kept > scaled)
    assert kept >= scaled


def test_a_catalogue_whose_scaled_layout_misses_a_level_is_floored_to_more_rows(
    tmp_path: Path,
) -> None:
    target = _small_catalogue(tmp_path, 13, 26, ["1", "3"])
    layout = run_bundle(target, tmp_path / "state", scale="0.5")
    recorded = load_recorded_plan(layout)
    assert recorded[KEY] == "trackedLevels"
    assert recorded["emits"][CATALOGUE]["generator"]["rows"] > 7
    assert {row[COLUMN] for row in rows(layout, HISTORY)} <= {
        row[COLUMN] for row in rows(layout, CATALOGUE)
    }
