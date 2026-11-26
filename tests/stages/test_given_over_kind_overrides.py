# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column drawn ``given`` a column a kind of a periods entry overrides (ADR 0027, the amendment of 2026-11-26).

``vehicle_month`` of ``tests/fixtures/history-fleet`` gains kinds, each a row of a vehicle's month
that sets ``charge`` by an override (a ``fuel`` row and a ``toll`` row), and ``band`` is drawn
given ``charge``. What is pinned:

* **The row's level.** ``band`` follows the ``charge`` its row holds: the kind's override, the
  version's value in a kind that overrides no column of the version, and the row's own draw in a
  kind that overrides no column the emit draws. A column a kind overrides is still drawn in the
  rows of the other kinds, given the level those rows hold.
* **A value no level names.** Where a kind's override is no level of ``charge``, ``band`` is drawn
  from the marginal ``charge``'s level weights and ``band``'s table imply, else from the support
  ``band`` declares; with neither the plan is refused (``FLX-3403``), as is a column with
  ``allocation: exact`` drawn given an override.
* **One run.** One job and four, shares joined in order, and both kernel paths give the same bytes.
* **Recorded plans.** A plan whose draw holds an override says ``projectedGivens:
  "afterOverrides"``; a plan recorded without that value replays the draw it was made with.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
import yaml

from flux_engine.core.digest import build_plan_digest
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator import columns
from flux_engine.stages import pipeline
from flux_engine.stages.apply import load_recorded_plan
from flux_engine.stages.layout import RunLayout, run_id_for
from flux_engine.stages.replay import replay
from flux_engine.stages.verify import verify
from tests.generator.test_columns import _both

from . import _fleet
from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run
from .test_declared_range_replay import recorded_digest, run_as_before
from .test_given_after_projection import (
    LEVELS,
    TIER_OF,
    TIERS,
    _edited,
    _history_rule,
    _refusal,
)

pytestmark = pytest.mark.integration

KEY = "projectedGivens"
OVERRIDES = "afterOverrides"
CHARGES = {"fuel": "0.6", "toll": "0.4"}
#: ``band`` is ``x`` on a fuel row and ``y`` on a toll row: one weight of one per level.
BAND_OF = {"fuel": "x", "toll": "y"}
BANDS = {"fuel": {"x": "1", "y": "0"}, "toll": {"x": "0", "y": "1"}}
GIVEN_CHARGE = {"given": "charge", "levelWeightsGiven": BANDS}
#: Where ``charge`` holds no level, the marginal its weights and the table imply: ``x`` with 0.6.
IMPLIED_X = 0.6
#: Where ``model`` holds no level, ``tier`` is ``a`` with 0.5 + 0.3.
IMPLIED_A = 0.8
FUEL = {"count": 1, "overrides": {"charge": "fuel"}}
TOLL = {"count": 1, "overrides": {"charge": "toll"}}
#: Each kind overrides ``charge`` with one of its levels.
TWO = [FUEL, TOLL]
#: The first kind overrides ``charge`` with ``parking``, which no level names.
PARKING = [{"count": 1, "overrides": {"charge": "parking"}}, TOLL]
#: The second kind overrides nothing: its rows draw ``charge`` themselves.
PARTLY = [FUEL, {"count": 2}]
#: The first kind overrides ``band`` too; the second kind's rows draw it, given their toll.
SELF = [{"count": 1, "overrides": {"charge": "fuel", "band": "y"}}, TOLL]
#: A column of the version the first kind overrides: ``model`` is ``legacy`` there (no level),
#: and the version's ``model`` on the second kind's rows.
LEGACY = [
    {"count": 1, "when": "valid_from < DATE '2024-06-01'", "overrides": {"model": "legacy"}},
    {"count": 1, "when": "valid_from >= DATE '2024-06-01'"},
]
#: As ``LEGACY``, with an override that is a level of ``model``.
TRUCK = [{**LEGACY[0], "overrides": {"model": "truck"}}, LEGACY[1]]
#: Every kind overrides ``model``, a column of the version, with one of its levels.
EVERY = [
    {"count": 1, "overrides": {"model": "sedan"}},
    {"count": 1, "overrides": {"model": "truck"}},
]
#: Every kind overrides ``band`` as well as ``charge``, so no row draws ``band``.
BOTH = [
    {"count": 1, "overrides": {"charge": "fuel", "band": "x"}},
    {"count": 1, "overrides": {"charge": "toll", "band": "y"}},
]
#: The kinds set ``litres``, a number drawn from bins, to a value in each bin.
LITRES = [{"count": 1, "overrides": {"litres": "20"}}, {"count": 1, "overrides": {"litres": "80"}}]
#: The first kind overrides ``model``, a column of the version, as well as ``charge``.
TRUCK_FUEL = [{"count": 1, "overrides": {"charge": "fuel", "model": "truck"}}, TOLL]
#: A share of a fallback group is within this many standard errors of its implied share.
Z_LIMIT = 4.0
#: The fewest rows a group whose share is checked must hold.
MOST_FEW = 100


def kinds_bundle(
    tmp_path: Path,
    kinds: Sequence[Dict[str, Any]],
    declare: Optional[Dict[str, Dict[str, Any]]] = None,
    tier: bool = False,
    size: Tuple[int, int] = (200, 60),
) -> Path:
    """The fleet bundle with *kinds* on ``vehicle_month``, whose ``band`` is drawn given ``charge``.

    *declare* replaces the declaration of each column it names (``charge``'s level weights,
    ``band``'s :data:`GIVEN_CHARGE`). With *tier*, ``tier`` is drawn given ``model``, a column of
    the version. *size* is the history's versions and entities.
    """
    target = _fleet.bundle(tmp_path, versions=size[0], entities=size[1])
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    schema = _fleet.expose(contract, _fleet.PERIODIC)["contract"]["schema"]
    added = ["charge", "band", *(["tier"] if tier else [])]
    schema.extend({"name": column, "type": "STRING", "required": True} for column in added)
    _fleet.repin(target, contract)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    flux = document["extensions"][_fleet.NAMESPACE]
    flux["periods"][0]["kinds"] = [json.loads(json.dumps(kind)) for kind in kinds]
    declared = flux["emits"][_fleet.PERIODIC]["fidelity"]["columns"]
    declared.update({"charge": {"levels": dict(CHARGES)}, "band": dict(GIVEN_CHARGE)})
    declared.update(declare or {})
    if tier:
        declared["tier"] = {"given": "model", "levelWeightsGiven": TIERS}
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def _run(target: Path, tmp_path: Path) -> Tuple[RunLayout, List[Dict[str, Any]]]:
    """Plan, apply and verify *target*; the run's layout and the periodic emit's rows."""
    state = tmp_path / "state"
    layout = _fleet.run_bundle(target, state)
    checked = verify(target, run_id=layout.run_id, state_dir=state)
    assert checked.success, checked.stdout + checked.stderr
    return layout, _fleet.rows(layout, _fleet.PERIODIC)


def _wrong_bands(rows: List[Dict[str, Any]]) -> List[Tuple[Any, Any]]:
    """The rows whose ``band`` is not the one the row's ``charge`` gives it."""
    return [(row["charge"], row["band"]) for row in rows if row["band"] != BAND_OF[row["charge"]]]


def _z(drawn: int, rows: int, share: float) -> float:
    """How many standard errors *drawn* of *rows* lies from *share* of them."""
    assert rows >= MOST_FEW, rows
    return (drawn - rows * share) / math.sqrt(rows * share * (1 - share))


# -- the row's level -------------------------------------------------------------------------------


def test_a_column_drawn_given_a_kind_s_override_follows_the_row(tmp_path: Path) -> None:
    layout, rows = _run(kinds_bundle(tmp_path, TWO), tmp_path)
    assert {row["charge"] for row in rows} == set(CHARGES)
    assert _wrong_bands(rows) == []
    assert load_recorded_plan(layout)[KEY] == OVERRIDES


def test_a_column_the_second_kind_does_not_override_follows_its_own_draw(tmp_path: Path) -> None:
    layout, rows = _run(kinds_bundle(tmp_path, PARTLY), tmp_path)
    # The second kind's rows draw ``charge`` themselves, toll among them, and ``band`` follows.
    assert {row["charge"] for row in rows} == set(CHARGES)
    assert _wrong_bands(rows) == []
    assert load_recorded_plan(layout)[KEY] == OVERRIDES


def test_a_column_one_kind_overrides_is_drawn_given_the_other_kind_s_override(
    tmp_path: Path,
) -> None:
    # The first kind sets ``band`` itself; on the second kind's rows ``band`` is drawn given the
    # toll the row holds, not the level ``charge``'s own draw made.
    layout, rows = _run(kinds_bundle(tmp_path, SELF), tmp_path)
    assert {row["band"] for row in rows} == {"y"}
    assert load_recorded_plan(layout)[KEY] == OVERRIDES


@pytest.mark.parametrize("kinds", [TRUCK, LEGACY], ids=["level", "no-level"])
def test_a_column_of_the_version_a_kind_overrides_reads_the_override_or_the_version(
    tmp_path: Path, kinds: List[Dict[str, Any]]
) -> None:
    layout, rows = _run(kinds_bundle(tmp_path, kinds, tier=True, size=(600, 120)), tmp_path)
    named = [row for row in rows if row["model"] in LEVELS]
    assert named and all(row["tier"] == TIER_OF[row["model"]] for row in named)
    assert load_recorded_plan(layout)[KEY] == OVERRIDES
    unnamed = [row for row in rows if row["model"] not in LEVELS]
    if kinds is TRUCK:
        assert unnamed == []
        return
    assert {row["model"] for row in unnamed} == {"legacy"}
    drawn = sum(row["tier"] == "a" for row in unnamed)
    assert abs(_z(drawn, len(unnamed), IMPLIED_A)) < Z_LIMIT, (drawn, len(unnamed))


# -- a value no level names ------------------------------------------------------------------------


def test_a_column_of_the_version_every_kind_overrides_holds_no_value_of_the_version(
    tmp_path: Path,
) -> None:
    # The history's ``model`` may hold a value no level names, but every row holds an override.
    target = kinds_bundle(tmp_path, EVERY, tier=True)
    _edited(target, _history_rule)
    projection = pipeline.execute(target, kernel=KERNEL_V2).emit(_fleet.PERIODIC).plan.projection
    assert "model" in projection.held and projection.fallback == ()
    _, rows = _run(target, tmp_path)
    assert {row["model"] for row in rows} == {"sedan", "truck"}
    assert all(row["tier"] == TIER_OF[row["model"]] for row in rows)


def test_a_column_every_kind_overrides_is_drawn_on_no_row_and_holds_nothing_given(
    tmp_path: Path,
) -> None:
    # ``band`` is declared given ``charge``, but every kind sets it, so its own draw is never
    # kept: nothing is drawn given an override, and the plan keeps the bytes it had before.
    target = kinds_bundle(tmp_path, BOTH)
    projection = pipeline.execute(target, kernel=KERNEL_V2).emit(_fleet.PERIODIC).plan.projection
    assert projection.held == () and projection.fallback == () and not projection.overridden
    layout, rows = _run(target, tmp_path)
    assert KEY not in load_recorded_plan(layout)
    assert {(row["charge"], row["band"]) for row in rows} == {("fuel", "x"), ("toll", "y")}


def test_a_column_drawn_given_the_bin_of_an_override_follows_the_row(tmp_path: Path) -> None:
    target = kinds_bundle(tmp_path, LITRES)
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    schema = _fleet.expose(contract, _fleet.PERIODIC)["contract"]["schema"]
    schema.append({"name": "litres", "type": "INT", "required": True})
    _fleet.repin(target, contract)

    def binned(document: Dict[str, Any]) -> None:
        emit = document["extensions"][_fleet.NAMESPACE]["emits"][_fleet.PERIODIC]
        declared = emit["fidelity"]["columns"]
        declared["litres"] = {
            "range": {"min": "0", "max": "100"},
            "bins": ["0", "50", "100"],
            "binWeights": ["1", "1"],
        }
        table = {"0": {"x": "1", "y": "0"}, "50": {"x": "0", "y": "1"}}
        declared["band"] = {"given": "litres", "levelWeightsGiven": table}

    _edited(target, binned)
    layout, rows = _run(target, tmp_path)
    assert {row["litres"] for row in rows} == {20, 80}
    assert all(row["band"] == ("x" if row["litres"] == 20 else "y") for row in rows)
    assert load_recorded_plan(layout)[KEY] == OVERRIDES


def test_a_column_of_the_version_drawn_given_an_override_is_no_draw_to_hold(
    tmp_path: Path,
) -> None:
    # ``model`` holds the version's value or the first kind's override on every row, so its own
    # draw is never kept, and nothing it is drawn given is held.
    target = kinds_bundle(tmp_path, TRUCK_FUEL)

    def model_given_charge(document: Dict[str, Any]) -> None:
        emit = document["extensions"][_fleet.NAMESPACE]["emits"][_fleet.PERIODIC]
        declared = emit["fidelity"]["columns"]
        table = {
            "fuel": {"sedan": "1", "van": "0", "truck": "0"},
            "toll": {"sedan": "0", "van": "1", "truck": "0"},
        }
        declared["model"] = {"given": "charge", "levelWeightsGiven": table}
        declared["band"] = {"levels": {"x": "0.5", "y": "0.5"}}

    _edited(target, model_given_charge)
    result = pipeline.execute(target, kernel=KERNEL_V2)
    assert result.emit(_fleet.PERIODIC).plan.projection.held == ()
    layout, rows = _run(target, tmp_path)
    assert KEY not in load_recorded_plan(layout)
    assert {row["model"] for row in rows if row["charge"] == "fuel"} == {"truck"}


def test_an_override_no_level_names_draws_the_implied_marginal(tmp_path: Path) -> None:
    target = kinds_bundle(tmp_path, PARKING, size=(600, 120))
    layout, rows = _run(target, tmp_path)
    tolls = [row for row in rows if row["charge"] == "toll"]
    assert tolls and _wrong_bands(tolls) == []
    parked = [row for row in rows if row["charge"] == "parking"]
    assert {row["band"] for row in parked} == {"x", "y"}
    drawn = sum(row["band"] == "x" for row in parked)
    assert abs(_z(drawn, len(parked), IMPLIED_X)) < Z_LIMIT, (drawn, len(parked))
    assert load_recorded_plan(layout)[KEY] == OVERRIDES


def test_the_plan_records_and_notes_the_marginal_an_override_falls_back_to(
    tmp_path: Path,
) -> None:
    result = pipeline.execute(kinds_bundle(tmp_path, PARKING), kernel=KERNEL_V2)
    projection = result.emit(_fleet.PERIODIC).plan.projection
    assert projection.held == ("charge",) and projection.fallback == ("charge",)
    assert projection.marginals == (("band", (("x", "3/5"), ("y", "2/5"))),)
    member = projection.as_document()
    assert member["heldGivens"] == ["charge"] and member["fallbackGivens"] == ["charge"]
    assert member["fallbackMarginals"] == {"band": {"x": "3/5", "y": "2/5"}}
    notes = [note for note in pipeline.advice_notes(result) if "vehicle_month.band" in note]
    assert len(notes) == 1, notes
    for word in ("given charge", "kinds[0] overrides it with 'parking'", "marginal"):
        assert word in notes[0], notes[0]


def test_an_override_no_level_names_draws_the_support_where_charge_declares_no_weights(
    tmp_path: Path,
) -> None:
    bare = {"support": {"values": list(CHARGES)}, "maxCardinality": 2}
    support = {"support": {"values": ["x", "y"]}, "maxCardinality": 2}
    declare = {"charge": bare, "band": {**GIVEN_CHARGE, **support}}
    target = kinds_bundle(tmp_path, PARKING, declare, size=(600, 120))
    _, rows = _run(target, tmp_path)
    parked = [row for row in rows if row["charge"] == "parking"]
    drawn = sum(row["band"] == "x" for row in parked)
    assert abs(_z(drawn, len(parked), 0.5)) < Z_LIMIT, (drawn, len(parked))


def test_an_override_no_level_names_is_refused_with_no_distribution_to_draw_from(
    tmp_path: Path,
) -> None:
    bare = {"support": {"values": list(CHARGES)}, "maxCardinality": 2}
    text = _refusal(kinds_bundle(tmp_path, PARKING, {"charge": bare}))
    for word in (
        f"({_fleet.PERIODIC})",
        "'band'",
        "'charge'",
        "an override of kinds[0]",
        "kinds[0] overrides it with 'parking'",
        "declares no support",
    ):
        assert word in text, text


def test_a_column_drawn_given_the_period_column_is_still_refused(tmp_path: Path) -> None:
    target = kinds_bundle(tmp_path, TWO)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    emit = document["extensions"][_fleet.NAMESPACE]["emits"][_fleet.PERIODIC]
    declared = emit["fidelity"]["columns"]
    declared["month"] = {
        "bins": ["2023-01-01", "2024-07-01", "2026-01-01"],
        "binWeights": ["1", "1"],
    }
    table = {"2023-01-01": {"x": "1", "y": "0"}, "2024-07-01": {"x": "0", "y": "1"}}
    declared["band"] = {"given": "month", "levelWeightsGiven": table}
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    text = _refusal(target)
    for word in ("'band'", "'month'", "its period column", "a kind's override", "on its own"):
        assert word in text, text


def test_a_column_drawn_given_an_override_cannot_allot_exact_classes(tmp_path: Path) -> None:
    # Its classes would be allotted by the draw of ``charge``, not by the override a row holds; a
    # periodic emit declares no allocation at all, since its rows grow with its window.
    exact = {"levels": dict(CHARGES), "allocation": "exact"}
    declare = {"charge": exact, "band": {**GIVEN_CHARGE, "allocation": "exact"}}
    target = kinds_bundle(tmp_path, TWO, declare)
    text = _refusal(target)
    for word in (f"'{_fleet.PERIODIC}' is a periodic emit", "allocation exact", "no allocation"):
        assert word in text, text


# -- one run ---------------------------------------------------------------------------------------

SHAPES = pytest.mark.parametrize(
    "kinds",
    [TWO, PARKING, PARTLY, SELF, LEGACY],
    ids=["levels", "no-level", "partly", "self", "version"],
)


@SHAPES
def test_the_draw_given_an_override_is_one_run_at_one_job_and_four(
    tmp_path: Path, kinds: List[Dict[str, Any]]
) -> None:
    assert_same_run_at_one_and_four_jobs(kinds_bundle(tmp_path, kinds, tier=True), kernel=KERNEL_V2)


@SHAPES
def test_the_draw_given_an_override_is_one_run_in_shares(
    tmp_path: Path, kinds: List[Dict[str, Any]]
) -> None:
    target = kinds_bundle(tmp_path, kinds, tier=True)
    assert_shares_join_to_the_run(target, tmp_path, _fleet.run_bundle, 3, [_fleet.PERIODIC])


@SHAPES
def test_both_kernel_paths_draw_the_same_rows_given_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kinds: List[Dict[str, Any]]
) -> None:
    target = kinds_bundle(tmp_path, kinds, tier=True)

    def lines() -> Any:
        return pipeline.execute(target, kernel=KERNEL_V2).emit(_fleet.PERIODIC).writer.lines()

    by_columns, by_rows, taken = _both(monkeypatch, lines)
    assert any(taken)
    assert by_columns == by_rows


@SHAPES
def test_the_column_path_draws_every_slice_of_the_periodic_emit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kinds: List[Dict[str, Any]]
) -> None:
    # A kind that holds no level for a column (``None``) is served by the column path too, so the
    # comparison above is one of the two paths, not of the per-row path with itself.
    taken: List[bool] = []
    column_rows = columns.column_rows

    def spy(plan: Any, *args: Any) -> Any:
        computed = column_rows(plan, *args)
        if plan.expose.expose_id == _fleet.PERIODIC:
            taken.append(computed is not None)
        return computed

    monkeypatch.setattr(columns, "column_rows", spy)
    pipeline.execute(kinds_bundle(tmp_path, kinds, tier=True), kernel=KERNEL_V2)
    assert taken and all(taken), taken


# -- recorded plans --------------------------------------------------------------------------------


def _before(target: Path, state: Path, monkeypatch: pytest.MonkeyPatch) -> Tuple[RunLayout, str]:
    """A run as engine 0.12.0 made it: no override is held."""
    era = pipeline.PlanEra(override_givens=False)
    return run_as_before(target, state, monkeypatch, "_planned_with_projections", era=era)


@pytest.mark.parametrize("tier", [False, True], ids=["no-key", "after-projection"])
def test_a_plan_recorded_before_overrides_were_held_replays_its_own_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tier: bool
) -> None:
    target = kinds_bundle(tmp_path, SELF, tier=tier)
    state = tmp_path / "state"
    layout, digest = _before(target, state, monkeypatch)
    plan = load_recorded_plan(layout)
    assert plan.get(KEY) == ("afterProjection" if tier else None)
    recorded = layout.emit_path(_fleet.PERIODIC).read_bytes()
    # Before: the second kind's ``band`` was drawn given the level ``charge``'s own draw made.
    assert {row["band"] for row in _fleet.rows(layout, _fleet.PERIODIC)} == {"x", "y"}
    for jobs in (1, 4):
        replayed = replay(
            target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=jobs
        )
        assert replayed.success, replayed.stderr
        assert layout.emit_path(_fleet.PERIODIC).read_bytes() == recorded


def test_a_plan_recorded_before_overrides_were_held_still_refuses_a_given_over_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = pipeline._planned_with_projections

    def before(*args: Any, **options: Any) -> Any:
        return called(*args, **{**options, "era": pipeline.PlanEra(override_givens=False)})

    monkeypatch.setattr(pipeline, "_planned_with_projections", before)
    text = _refusal(kinds_bundle(tmp_path, TWO))
    for word in (f"({_fleet.PERIODIC})", "'band'", "'charge'", "an override of kinds[0]"):
        assert word in text, text


def test_a_new_plan_replays_the_draw_given_an_override(tmp_path: Path) -> None:
    target = kinds_bundle(tmp_path, PARKING)
    state = tmp_path / "state"
    layout = _fleet.run_bundle(target, state)
    recorded = layout.emit_path(_fleet.PERIODIC).read_bytes()
    digest = recorded_digest(state, layout.run_id)
    replayed = replay(target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=4)
    assert replayed.success, replayed.stderr
    assert layout.emit_path(_fleet.PERIODIC).read_bytes() == recorded


def test_kinds_that_override_no_column_drawn_given_keep_the_key_of_before(tmp_path: Path) -> None:
    # ``tier`` is drawn given ``model``, which no kind overrides, and ``band`` given ``charge``,
    # which the rows draw themselves: the draw engine 0.12.0 made.
    target = kinds_bundle(tmp_path, [{"count": 1}, {"count": 2}], tier=True)
    layout, rows = _run(target, tmp_path)
    assert _wrong_bands(rows) == []
    assert load_recorded_plan(layout)[KEY] == "afterProjection"


def test_a_plan_with_a_value_this_engine_does_not_know_names_both_it_knows(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    layout = _fleet.run_bundle(kinds_bundle(tmp_path, TWO), state)
    forged = {**load_recorded_plan(layout), KEY: "afterKinds"}
    forged["planDigest"] = build_plan_digest(forged).ref.value
    moved = RunLayout.for_state_dir(state, run_id_for(forged["planDigest"]))
    moved.run_dir.mkdir(parents=True)
    moved.plan_path.write_text(json.dumps(forged, indent=2, sort_keys=True), "utf-8")
    with pytest.raises(FluxError, match=f"FLX-2102.*{KEY}.*afterProjection.*{OVERRIDES}"):
        load_recorded_plan(moved)
