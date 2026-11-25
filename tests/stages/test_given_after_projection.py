# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column drawn ``given`` a column of the version is drawn after the projection (ADR 0027).

An events or periods entry sets every column of its emit that names a column of the history's
emit from the version the row is about (``model`` on a vehicle's month, or on a vehicle's
change). A column the emit draws given such a column (``tier`` given ``model``) reads the level
the version holds, so on every row ``tier`` follows the ``model`` the row carries. The run is the
same at one job and at four, by either kernel path, and in shares joined in order. A recorded
plan without ``projectedGivens`` replays the draw it was made with.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
import yaml

from flux_engine.core.digest import build_plan_digest
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import pipeline
from flux_engine.stages.apply import load_recorded_plan
from flux_engine.stages.layout import RunLayout, run_id_for
from flux_engine.stages.replay import replay
from flux_engine.stages.verify import verify
from tests.generator.test_columns import _both

from . import _fleet
from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run
from .test_declared_range_replay import recorded_digest, run_as_before

pytestmark = pytest.mark.integration

KEY = "projectedGivens"
PROJECTIONS = (_fleet.PERIODIC, _fleet.EVENTS)
LEVELS = {"sedan": "0.5", "van": "0.3", "truck": "0.2"}
#: ``tier`` is ``a`` on a sedan's row and ``b`` on any other: one weight of one per level.
TIERS = {
    level: {"a": "1", "b": "0"} if level == "sedan" else {"a": "0", "b": "1"} for level in LEVELS
}
#: ``band`` is drawn given ``tier``: ``x`` on an ``a`` row, ``y`` on a ``b`` row.
SUPPORT = ("a", "b")
BANDS = {"a": {"x": "1", "y": "0"}, "b": {"x": "0", "y": "1"}}


def _schema_column(name: str) -> Dict[str, Any]:
    return {"name": name, "type": "STRING", "required": True}


def given_bundle(
    tmp_path: Path,
    projection: str,
    *,
    parent: str = "model",
    chain: bool = False,
    history: Optional[Dict[str, Any]] = None,
    name: str = "bundle",
    support: bool = False,
    versions: int = 200,
    entities: int = 60,
) -> Path:
    """A fleet bundle whose *projection* draws ``tier`` given *parent* (and ``band`` given ``tier``).

    The events emit gains a ``model`` column, which the entry sets from the version a change is
    about; the periodic emit holds one already. *history* replaces the history's ``model``, which
    the contract then leaves optional, and so does the projection's. With *support*, ``tier``
    declares the distribution it is drawn from where ``model`` holds no level a table row names.
    """
    target = _fleet.bundle(tmp_path, name=name, versions=versions, entities=entities)
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    if history is not None:
        kept = _fleet.expose(contract, _fleet.HISTORY)["contract"]["schema"]
        next(column for column in kept if column["name"] == "model")["required"] = False
    schema = _fleet.expose(contract, projection)["contract"]["schema"]
    added = ["tier", "band"] if chain else ["tier"]
    if projection == _fleet.EVENTS:
        added.insert(0, "model")
    schema.extend(_schema_column(column) for column in added)
    if history is not None:
        for exposed in contract["exposes"]:
            for column in exposed["contract"]["schema"]:
                if column["name"] == "model":
                    column["required"] = False
    _fleet.repin(target, contract)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    emits = document["extensions"][_fleet.NAMESPACE]["emits"]
    declared = emits[projection]["fidelity"]["columns"]
    declared.setdefault("model", {"levels": dict(LEVELS)})
    declared["tier"] = {"given": parent, "levelWeightsGiven": TIERS}
    if support:
        declared["tier"].update({"support": {"values": list(SUPPORT)}, "maxCardinality": 2})
    if chain:
        declared["band"] = {"given": "tier", "levelWeightsGiven": BANDS}
    if history is not None:
        emits[_fleet.HISTORY]["fidelity"]["columns"]["model"] = history
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def _wrong_tiers(rows: List[Dict[str, Any]]) -> List[Tuple[Any, Any]]:
    """The rows whose ``tier`` is not the one the row's ``model`` gives it."""
    return [
        (row["model"], row["tier"])
        for row in rows
        if row["tier"] != ("a" if row["model"] == "sedan" else "b")
    ]


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_column_drawn_given_a_column_of_the_version_follows_the_row(
    tmp_path: Path, projection: str
) -> None:
    target = given_bundle(tmp_path, projection)
    layout = _fleet.run_bundle(target, tmp_path / "state")
    rows = _fleet.rows(layout, projection)
    assert {row["model"] for row in rows} == set(LEVELS)
    assert _wrong_tiers(rows) == []
    assert load_recorded_plan(layout)[KEY] == "afterProjection"
    checked = verify(target, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert checked.success, checked.stdout + checked.stderr


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_chain_below_a_column_of_the_version_follows_the_row(
    tmp_path: Path, projection: str
) -> None:
    target = given_bundle(tmp_path, projection, chain=True)
    rows = _fleet.rows(_fleet.run_bundle(target, tmp_path / "state"), projection)
    assert _wrong_tiers(rows) == []
    assert all(row["band"] == ("x" if row["tier"] == "a" else "y") for row in rows)


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_the_draw_after_the_projection_is_one_run_at_one_job_and_four(
    projection: str, tmp_path: Path
) -> None:
    assert_same_run_at_one_and_four_jobs(given_bundle(tmp_path, projection), kernel=KERNEL_V2)


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_the_draw_after_the_projection_is_one_run_in_shares(
    projection: str, tmp_path: Path
) -> None:
    target = given_bundle(tmp_path, projection, chain=True)
    assert_shares_join_to_the_run(target, tmp_path, _fleet.run_bundle, 3, [projection])


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_both_kernel_paths_draw_the_same_rows_after_the_projection(
    projection: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = given_bundle(tmp_path, projection, chain=True)

    def lines() -> Any:
        return pipeline.execute(target, kernel=KERNEL_V2).emit(projection).writer.lines()

    by_columns, by_rows, taken = _both(monkeypatch, lines)
    assert any(taken)
    assert by_columns == by_rows


# -- what the draw after the projection cannot serve ---------------------------------------------


def _refusal(target: Path) -> str:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3403", str(caught.value)
    return str(caught.value)


@pytest.mark.parametrize(
    ("parent", "role"), [("before_model", "a before image"), ("after_model", "an after image")]
)
def test_a_column_drawn_given_an_image_is_still_refused(
    tmp_path: Path, parent: str, role: str
) -> None:
    # A before image is NULL on a vehicle's first event, an after image on its delete.
    text = _refusal(given_bundle(tmp_path, _fleet.EVENTS, parent=parent))
    for word in (f"({_fleet.EVENTS})", "'tier'", f"'{parent}'", role, "column of the version"):
        assert word in text, text


def test_a_column_drawn_given_the_op_is_still_refused(tmp_path: Path) -> None:
    target = given_bundle(tmp_path, _fleet.EVENTS, parent="op")
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    columns_ = document["extensions"][_fleet.NAMESPACE]["emits"][_fleet.EVENTS]["fidelity"]
    columns_["columns"]["tier"]["levelWeightsGiven"] = {"c": {"a": "1", "b": "0"}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    text = _refusal(target)
    for word in ("'tier'", "'op'", "its op column"):
        assert word in text, text


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_version_column_the_history_draws_outside_the_levels_is_refused_without_a_support(
    tmp_path: Path, projection: str
) -> None:
    history = {"levels": {"sedan": "0.5", "van": "0.3", "bus": "0.2"}}
    text = _refusal(given_bundle(tmp_path, projection, history=history))
    for word in ("'tier'", "'model'", "'bus'", "declares no support", "no distribution of its own"):
        assert word in text, text


# -- a recorded plan replays the draw it was made with ---------------------------------------------


def test_a_plan_recorded_before_the_reorder_replays_its_own_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = given_bundle(tmp_path, _fleet.PERIODIC)
    state = tmp_path / "state"
    # As engine 0.11.1 made it: ``tier`` drawn given the level ``model``'s own draw made.
    layout, digest = run_as_before(
        target,
        state,
        monkeypatch,
        "_planned_with_projections",
        era=pipeline.PlanEra(projected_givens=False),
    )
    assert KEY not in load_recorded_plan(layout)
    recorded = layout.emit_path(_fleet.PERIODIC).read_bytes()
    assert _wrong_tiers(_fleet.rows(layout, _fleet.PERIODIC))
    for jobs in (1, 4):
        replayed = replay(
            target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=jobs
        )
        assert replayed.success, replayed.stderr
        assert layout.emit_path(_fleet.PERIODIC).read_bytes() == recorded


@pytest.mark.parametrize(
    ("parent", "role"), [("op", "its op column"), ("before_model", "a before image")]
)
def test_a_plan_recorded_without_the_marker_still_refuses_a_given_over_an_op_or_an_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent: str, role: str
) -> None:
    target = given_bundle(tmp_path, _fleet.EVENTS, parent=parent)
    if parent == "op":
        path, document = _simulation(target)
        emit = document["extensions"][_fleet.NAMESPACE]["emits"][_fleet.EVENTS]
        emit["fidelity"]["columns"]["tier"]["levelWeightsGiven"] = {"c": {"a": "1", "b": "0"}}
        path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    called = pipeline._planned_with_projections

    def before(*args: Any, **options: Any) -> Any:
        return called(*args, **{**options, "era": pipeline.PlanEra(projected_givens=False)})

    monkeypatch.setattr(pipeline, "_planned_with_projections", before)
    text = _refusal(target)
    for word in (f"({_fleet.EVENTS})", "'tier'", f"'{parent}'", role):
        assert word in text, text


def test_a_new_plan_replays_the_draw_after_the_projection(tmp_path: Path) -> None:
    target = given_bundle(tmp_path, _fleet.EVENTS)
    state = tmp_path / "state"
    layout = _fleet.run_bundle(target, state)
    recorded = layout.emit_path(_fleet.EVENTS).read_bytes()
    digest = recorded_digest(state, layout.run_id)
    replayed = replay(target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=4)
    assert replayed.success, replayed.stderr
    assert layout.emit_path(_fleet.EVENTS).read_bytes() == recorded


def test_a_plan_with_no_column_drawn_given_a_projected_one_writes_no_key(tmp_path: Path) -> None:
    layout = _fleet.run_bundle(_fleet.bundle(tmp_path), tmp_path / "state")
    assert KEY not in load_recorded_plan(layout)


def test_a_plan_with_a_value_this_engine_does_not_know_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    layout = _fleet.run_bundle(given_bundle(tmp_path, _fleet.PERIODIC), state)
    forged = {**load_recorded_plan(layout), KEY: "beforeProjection"}
    forged["planDigest"] = build_plan_digest(forged).ref.value
    moved = RunLayout.for_state_dir(state, run_id_for(forged["planDigest"]))
    moved.run_dir.mkdir(parents=True)
    moved.plan_path.write_text(json.dumps(forged, indent=2, sort_keys=True), "utf-8")
    with pytest.raises(FluxError, match=f"FLX-2102.*{KEY}"):
        load_recorded_plan(moved)


def _simulation(target: Path) -> Tuple[Path, Dict[str, Any]]:
    path = target / "simulation.flux.yml"
    return path, yaml.safe_load(path.read_text("utf-8"))


def test_a_column_drawn_given_a_column_of_the_version_cannot_allot_exact_classes(
    tmp_path: Path,
) -> None:
    # Without the guard the classes are allotted by the parent's draw while the row holds the
    # version's level: wrong data, no error.
    target = given_bundle(tmp_path, _fleet.EVENTS)
    path, document = _simulation(target)
    emit = document["extensions"][_fleet.NAMESPACE]["emits"][_fleet.EVENTS]
    columns = emit["fidelity"]["columns"]
    columns["model"]["allocation"] = "exact"
    columns["tier"]["allocation"] = "exact"
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    text = _refusal(target)
    for word in (f"({_fleet.EVENTS})", "'tier'", "'model'", "allocation: exact"):
        assert word in text, text


# -- a value no level names: the column is drawn from its own distribution ---------------------------

#: A share of a fallback group is within this many standard errors of its declared share.
Z_LIMIT = 4.0
#: The fewest rows each of the two groups (a value a level names, and one none names) must hold.
MOST_FEW = 100


def _own_share(rows: List[Dict[str, Any]], *, with_named: bool = True) -> Tuple[int, float]:
    """How many rows hold a value no level of ``model`` names, and z of their share of ``tier`` ``a``.

    ``tier`` declares the support ``a``, ``b``: equal weights, so a share of one half. Under the
    table a row that holds no level would draw ``tier`` from no row of it at all.
    """
    unnamed = [row for row in rows if row["model"] not in LEVELS]
    named = [row for row in rows if row["model"] in LEVELS]
    assert len(unnamed) >= MOST_FEW and (not with_named or len(named) >= MOST_FEW)
    # Rows that hold a level still follow the table, level for level.
    assert _wrong_tiers(named) == []
    drawn = sum(row["tier"] == "a" for row in unnamed)
    z = (drawn - len(unnamed) / 2) / math.sqrt(len(unnamed) / 4)
    assert {row["tier"] for row in unnamed} == set(SUPPORT)
    assert abs(z) < Z_LIMIT, (drawn, len(unnamed), z)
    return len(unnamed), z


def _fallback_run(target: Path, tmp_path: Path, projection: str) -> List[Dict[str, Any]]:
    """Run *target*, check its plan and verify, and return *projection*'s rows."""
    state = tmp_path / "state"
    layout = _fleet.run_bundle(target, state)
    assert load_recorded_plan(layout)[KEY] == "afterProjection"
    checked = verify(target, run_id=layout.run_id, state_dir=state)
    assert checked.success, checked.stdout + checked.stderr
    return _fleet.rows(layout, projection)


def _edited(target: Path, change: Callable[[Dict[str, Any]], None]) -> None:
    path, document = _simulation(target)
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")


def _history_rule(document: Dict[str, Any]) -> None:
    """The history's ``model`` is ``sedan`` on an active row and no level on the others."""
    emits = document["extensions"][_fleet.NAMESPACE]["emits"]
    emits[_fleet.HISTORY]["rules"] = [
        {
            "id": "model_is_set",
            "language": "flux-expr/3",
            "derive": "model",
            "expr": "CASE WHEN status = 'active' THEN 'sedan' ELSE status || 'bus' END",
        }
    ]
    del emits[_fleet.HISTORY]["fidelity"]["columns"]["model"]


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_version_column_a_rule_of_the_history_sets_draws_from_the_columns_own_distribution(
    tmp_path: Path, projection: str
) -> None:
    target = given_bundle(tmp_path, projection, support=True, versions=600, entities=120)
    _edited(target, _history_rule)
    rows = _fallback_run(target, tmp_path, projection)
    assert _own_share(rows)[0] >= MOST_FEW


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_version_column_with_a_level_the_emit_does_not_name_draws_from_the_columns_own_distribution(
    tmp_path: Path, projection: str
) -> None:
    history = {"levels": {"sedan": "0.35", "van": "0.25", "bus": "0.4"}}
    target = given_bundle(
        tmp_path, projection, history=history, support=True, versions=600, entities=120
    )
    rows = _fallback_run(target, tmp_path, projection)
    assert {row["model"] for row in rows} >= {"sedan", "van", "bus"}
    _own_share(rows)


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_version_column_the_history_may_leave_null_draws_from_the_columns_own_distribution(
    tmp_path: Path, projection: str
) -> None:
    history = {"levels": dict(LEVELS), "nullFraction": "0.4"}
    target = given_bundle(
        tmp_path, projection, history=history, support=True, versions=600, entities=120
    )
    rows = _fallback_run(target, tmp_path, projection)
    assert any(row["model"] is None for row in rows)
    _own_share(rows)


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_version_column_a_relationship_of_the_history_sets_draws_from_the_columns_own_distribution(
    tmp_path: Path, projection: str
) -> None:
    target = given_bundle(tmp_path, projection, support=True, versions=600, entities=120)
    _with_model_catalogue(target)
    rows = _fallback_run(target, tmp_path, projection)
    # The key of a catalogue is an identifier, which no level names: every row holds such a value.
    _own_share(rows, with_named=False)


def _with_model_catalogue(target: Path) -> None:
    """The history's ``model`` follows a relationship to a catalogue that holds ``bus`` as well."""
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    catalogue = yaml.safe_load(yaml.safe_dump(_fleet.expose(contract, _fleet.DEPOT)))
    catalogue["exposeId"] = "model_type"
    catalogue["binding"]["location"]["table"] = "model_type"
    catalogue["contract"]["schema"] = [{"name": "model", "type": "STRING", "required": True}]
    catalogue["semantics"] = {
        "entities": [{"name": "model_type", "type": "primary", "expr": "model"}]
    }
    contract["exposes"].insert(1, catalogue)
    history = _fleet.expose(contract, _fleet.HISTORY)
    history["semantics"]["relationships"].append(
        {
            "name": "model_type",
            "to": "model_type",
            "fromColumns": ["model"],
            "toColumns": ["model"],
            "cardinality": "many_to_one",
            "joinType": "inner",
        }
    )
    _fleet.repin(target, contract)

    def declare(document: Dict[str, Any]) -> None:
        flux = document["extensions"][_fleet.NAMESPACE]
        flux["emits"]["model_type"] = {
            "rows": 30,
            "fidelity": {
                "encoding": {"fractionalBits": 16, "rounding": "floor"},
                "records": {"min": 1, "max": 1000000},
                "columns": {"model": {"levels": {"sedan": "0.5", "van": "0.2", "bus": "0.3"}}},
                "properties": [],
                "budget": {"maxChecksPerState": 20, "maxStateAge": "P30D"},
                "failureAction": "withholdBundle",
            },
        }
        del flux["emits"][_fleet.HISTORY]["fidelity"]["columns"]["model"]
        emits = document["spec"]["emits"]
        emits.append({**emits[0], "exposeId": "model_type"})

    _edited(target, declare)


def test_a_version_column_the_history_ends_in_a_value_outside_the_levels_has_its_own_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The history's end value is one of its levels, and every level of the history is a level of
    # the emit's column, so no declaration reaches this source end to end. It is pinned at the plan
    # with the emit's levels read without ``truck``, which the history ends in and draws with no
    # weight: the column falls back, and the plan says where the unnamed value comes from.
    from flux_engine.generator import projections

    real = projections.level_index

    def without_truck(column_plan: Any) -> Any:
        return {key: level for key, level in real(column_plan).items() if level != "truck"}

    monkeypatch.setattr(projections, "level_index", without_truck)
    history = {"levels": {"sedan": "0.5", "van": "0.5", "truck": "0"}}
    target = given_bundle(tmp_path, _fleet.PERIODIC, history=history, support=True)
    _edited(
        target,
        lambda document: document["extensions"][_fleet.NAMESPACE]["history"][0].update(
            {
                "end": {
                    "mode": "finalVersion",
                    "share": "0.25",
                    "column": "model",
                    "value": "truck",
                }
            }
        ),
    )
    plan = pipeline.execute(target, kernel=KERNEL_V2).emit(_fleet.PERIODIC).plan
    assert plan.projection is not None
    assert plan.projection.fallback == ("model",)
    (note,) = projections.fallback_notes(plan)
    assert "end writes 'truck'" in note, note


def test_a_column_with_no_distribution_of_its_own_is_refused_naming_what_to_declare(
    tmp_path: Path,
) -> None:
    history = {"levels": dict(LEVELS), "nullFraction": "0.1"}
    text = _refusal(given_bundle(tmp_path, _fleet.PERIODIC, history=history))
    for word in ("'tier'", "'model'", "NULL", "declare a support for 'tier'"):
        assert word in text, text


def test_a_support_that_leaves_out_a_level_of_the_table_is_no_distribution_of_its_own(
    tmp_path: Path,
) -> None:
    history = {"levels": dict(LEVELS), "nullFraction": "0.1"}
    target = given_bundle(tmp_path, _fleet.PERIODIC, history=history, support=True)
    _edited(
        target,
        lambda document: document["extensions"][_fleet.NAMESPACE]["emits"][_fleet.PERIODIC][
            "fidelity"
        ]["columns"]["tier"].update({"support": {"values": ["a", "b", "c"]}, "maxCardinality": 3}),
    )
    text = _refusal(target)
    for word in (
        "'tier'",
        "'model'",
        "its support names ['a', 'b', 'c'] and its table ['a', 'b']",
    ):
        assert word in text, text


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_fallback_run_is_one_run_at_one_job_and_four(projection: str, tmp_path: Path) -> None:
    target = given_bundle(tmp_path, projection, support=True, versions=300, entities=80)
    _edited(target, _history_rule)
    assert_same_run_at_one_and_four_jobs(target, kernel=KERNEL_V2)


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_fallback_run_is_one_run_in_shares(projection: str, tmp_path: Path) -> None:
    target = given_bundle(tmp_path, projection, support=True, chain=True, versions=300, entities=80)
    _edited(target, _history_rule)
    assert_shares_join_to_the_run(target, tmp_path, _fleet.run_bundle, 3, [projection])


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_both_kernel_paths_draw_the_same_fallback_rows(
    projection: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = given_bundle(tmp_path, projection, support=True, chain=True, versions=300, entities=80)
    _edited(target, _history_rule)

    def lines() -> Any:
        return pipeline.execute(target, kernel=KERNEL_V2).emit(projection).writer.lines()

    by_columns, by_rows, taken = _both(monkeypatch, lines)
    assert any(taken)
    assert by_columns == by_rows


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_plan_says_where_a_column_is_drawn_from_its_own_distribution(
    tmp_path: Path, projection: str
) -> None:
    target = given_bundle(tmp_path, projection, support=True)
    _edited(target, _history_rule)
    result = pipeline.execute(target, kernel=KERNEL_V2)
    notes = [note for note in pipeline.advice_notes(result) if f"{projection}.tier" in note]
    assert notes == [
        f"note: {projection}.tier is drawn given model; where model holds a value no level names "
        "(the rule 'model_is_set' of the history sets it), tier is drawn from its own distribution"
    ]
    assert result.emit(projection).plan.projection.fallback == ("model",)  # type: ignore[union-attr]


def test_a_fallback_column_changes_the_generator_digest(tmp_path: Path) -> None:
    target = given_bundle(tmp_path, _fleet.PERIODIC, support=True)
    _edited(target, _history_rule)
    plan = pipeline.execute(target, kernel=KERNEL_V2).emit(_fleet.PERIODIC).plan
    assert plan.projection is not None
    assert plan.as_document()["projection"]["fallbackGivens"] == ["model"]
    other = replace(plan, projection=replace(plan.projection, fallback=()))
    assert "fallbackGivens" not in other.as_document()["projection"]
    assert other.digest().value != plan.digest().value


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_the_generator_document_names_the_held_columns_and_its_digest_covers_them(
    tmp_path: Path, projection: str
) -> None:
    # plan.json states only ``projectedGivens``; the held columns are in the generator document the
    # plan's ``generatorDigest`` covers.
    plan = (
        pipeline.execute(given_bundle(tmp_path, projection), kernel=KERNEL_V2).emit(projection).plan
    )
    assert plan.projection is not None
    assert plan.as_document()["projection"]["heldGivens"] == ["model"]
    other = replace(plan, projection=replace(plan.projection, held=("model", "status")))
    assert other.as_document()["projection"]["heldGivens"] == ["model", "status"]
    assert other.digest().value != plan.digest().value
    none = replace(plan, projection=replace(plan.projection, held=()))
    assert "heldGivens" not in none.as_document()["projection"]
    assert none.digest().value != plan.digest().value
