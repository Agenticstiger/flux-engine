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
#: ``tier`` is ``a`` on a sedan's or a van's row and ``b`` on a truck's: one weight of one per level.
TIER_OF = {"sedan": "a", "van": "a", "truck": "b"}
TIERS = {
    level: {"a": "1", "b": "0"} if TIER_OF[level] == "a" else {"a": "0", "b": "1"}
    for level in LEVELS
}
#: Where ``model`` holds no level, the marginal its weights and the table imply: ``a`` with 0.5 + 0.3.
IMPLIED_A = 0.8
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
    support: bool = False,
    bare: bool = False,
    size: Tuple[int, int] = (200, 60),
    tiers: Optional[Dict[str, Dict[str, str]]] = None,
) -> Path:
    """A fleet bundle whose *projection* draws ``tier`` given *parent* (and ``band`` given ``tier``).

    The events emit gains a ``model`` column, which the entry sets from the version a change is
    about; the periodic emit holds one already. *history* replaces the history's ``model``, which
    the contract then leaves optional, and so does the projection's. With *support*, ``tier``
    declares the distribution it is drawn from where ``model`` holds no level a table row names.
    With *bare*, ``model`` declares no level weights (a support of its levels), in the emit and in
    the history unless *history* replaces it.
    *size* is the history's versions and entities. *tiers* replaces the table ``tier`` is drawn by.
    """
    target = _fleet.bundle(tmp_path, versions=size[0], entities=size[1])
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    added = ["tier", "band"] if chain else ["tier"]
    if projection == _fleet.EVENTS:
        added.insert(0, "model")
    schema = _fleet.expose(contract, projection)["contract"]["schema"]
    schema.extend(_schema_column(column) for column in added)
    if history is not None or bare:
        for exposed in contract["exposes"]:
            for column in exposed["contract"]["schema"]:
                if column["name"] == "model":
                    column["required"] = False
    _fleet.repin(target, contract)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    emits = document["extensions"][_fleet.NAMESPACE]["emits"]
    declared = emits[projection]["fidelity"]["columns"]
    if bare:
        declared["model"] = _bare_model()
    declared.setdefault("model", {"levels": dict(LEVELS)})
    declared["tier"] = {"given": parent, "levelWeightsGiven": tiers or TIERS}
    if support:
        declared["tier"].update({"support": {"values": list(SUPPORT)}, "maxCardinality": 2})
    if chain:
        declared["band"] = {"given": "tier", "levelWeightsGiven": BANDS}
    if history is not None or bare:
        emits[_fleet.HISTORY]["fidelity"]["columns"]["model"] = history or _bare_model()
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def _bare_model() -> Dict[str, Any]:
    """A ``model`` that declares its levels and no weights."""
    return {"support": {"values": list(LEVELS)}, "maxCardinality": 3}


def same_run_bundle(tmp_path: Path, projection: str, source: str) -> Path:
    """A bundle with a chain below ``tier``, whose ``model`` is a level on each row or, as a fallback, not.

    *source* is ``held`` (a level on every row), ``implied`` (the marginal the weights imply) or
    ``support`` (``model`` declares no weights, and ``tier`` a support).
    """
    if source == "held":
        return given_bundle(tmp_path, projection, chain=True)
    bare = source == "support"
    target = given_bundle(tmp_path, projection, chain=True, support=bare, bare=bare, size=(300, 80))
    _edited(target, _history_rule)
    return target


def _wrong_tiers(rows: List[Dict[str, Any]]) -> List[Tuple[Any, Any]]:
    """The rows whose ``tier`` is not the one the row's ``model`` gives it."""
    return [(row["model"], row["tier"]) for row in rows if row["tier"] != TIER_OF[row["model"]]]


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


FALLBACK = pytest.mark.parametrize("source", ["held", "implied", "support"])


@FALLBACK
@pytest.mark.parametrize("projection", PROJECTIONS)
def test_the_draw_after_the_projection_is_one_run_at_one_job_and_four(
    projection: str, source: str, tmp_path: Path
) -> None:
    target = same_run_bundle(tmp_path, projection, source)
    assert_same_run_at_one_and_four_jobs(target, kernel=KERNEL_V2)


@FALLBACK
@pytest.mark.parametrize("projection", PROJECTIONS)
def test_the_draw_after_the_projection_is_one_run_in_shares(
    projection: str, source: str, tmp_path: Path
) -> None:
    target = same_run_bundle(tmp_path, projection, source)
    assert_shares_join_to_the_run(target, tmp_path, _fleet.run_bundle, 3, [projection])


@FALLBACK
@pytest.mark.parametrize("projection", PROJECTIONS)
def test_both_kernel_paths_draw_the_same_rows_after_the_projection(
    projection: str, source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = same_run_bundle(tmp_path, projection, source)

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
    history = {"support": {"values": [*LEVELS, "bus"]}, "maxCardinality": 4}
    text = _refusal(given_bundle(tmp_path, projection, history=history, bare=True))
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


# -- a value no level names: the column is drawn from the marginal the declarations imply ------------

#: A share of a fallback group is within this many standard errors of its implied share.
Z_LIMIT = 4.0
#: The fewest rows each of the two groups (a value a level names, and one none names) must hold.
MOST_FEW = 100


def _own_share(
    rows: List[Dict[str, Any]], *, with_named: bool = True, expected: float = IMPLIED_A
) -> Tuple[int, float]:
    """How many rows hold a value no level of ``model`` names, and z of their share of ``tier`` ``a``.

    Under the marginal the weights of ``model`` and the table imply, ``a`` has the share *expected*
    (0.8: a half and three tenths of the rows); an even draw over the support would give a half,
    and under the table alone a row that holds no level would draw ``tier`` from no row of it.
    """
    unnamed = [row for row in rows if row["model"] not in LEVELS]
    named = [row for row in rows if row["model"] in LEVELS]
    assert len(unnamed) >= MOST_FEW and (not with_named or len(named) >= MOST_FEW)
    # Rows that hold a level still follow the table, level for level.
    assert _wrong_tiers(named) == []
    drawn = sum(row["tier"] == "a" for row in unnamed)
    z = (drawn - len(unnamed) * expected) / math.sqrt(len(unnamed) * expected * (1 - expected))
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
def test_a_version_column_a_rule_of_the_history_sets_draws_the_implied_marginal(
    tmp_path: Path, projection: str
) -> None:
    target = given_bundle(tmp_path, projection, size=(600, 120))
    _edited(target, _history_rule)
    rows = _fallback_run(target, tmp_path, projection)
    assert _own_share(rows)[0] >= MOST_FEW


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_version_column_with_a_level_the_emit_does_not_name_draws_the_implied_marginal(
    tmp_path: Path, projection: str
) -> None:
    # The emit's weights are read, not the history's: the table is keyed by the emit's levels.
    history = {"levels": {"sedan": "0.35", "van": "0.25", "bus": "0.4"}}
    target = given_bundle(tmp_path, projection, history=history, size=(600, 120))
    rows = _fallback_run(target, tmp_path, projection)
    assert {row["model"] for row in rows} >= {"sedan", "van", "bus"}
    _own_share(rows)


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_version_column_the_history_may_leave_null_draws_the_implied_marginal(
    tmp_path: Path, projection: str
) -> None:
    history = {"levels": dict(LEVELS), "nullFraction": "0.4"}
    target = given_bundle(tmp_path, projection, history=history, size=(600, 120))
    rows = _fallback_run(target, tmp_path, projection)
    assert any(row["model"] is None for row in rows)
    _own_share(rows)


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_version_column_a_relationship_of_the_history_sets_draws_the_implied_marginal(
    tmp_path: Path, projection: str
) -> None:
    target = given_bundle(tmp_path, projection, size=(600, 120))
    _with_model_catalogue(target)
    rows = _fallback_run(target, tmp_path, projection)
    # The key of a catalogue is an identifier, which no level names: every row holds such a value.
    _own_share(rows, with_named=False)


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_column_with_no_level_weights_for_its_parent_draws_evenly_over_its_support(
    tmp_path: Path, projection: str
) -> None:
    target = given_bundle(tmp_path, projection, support=True, bare=True, size=(600, 120))
    _edited(target, _history_rule)
    rows = _fallback_run(target, tmp_path, projection)
    # Named rows are drawn from the table whatever ``model`` declares; the rest draw a half each.
    assert _own_share(rows, expected=0.5)[0] >= MOST_FEW


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_the_implied_marginal_is_drawn_where_the_column_also_declares_a_support(
    tmp_path: Path, projection: str
) -> None:
    target = given_bundle(tmp_path, projection, support=True, size=(600, 120))
    _edited(target, _history_rule)
    assert _own_share(_fallback_run(target, tmp_path, projection))[0] >= MOST_FEW


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


def test_a_version_column_the_history_ends_in_a_value_outside_the_levels_has_the_implied_marginal(
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
    target = given_bundle(tmp_path, _fleet.PERIODIC, history=history)
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
    assert note.endswith(
        "tier is drawn from the marginal model's level weights and tier's table imply"
    )
    assert dict(plan.projection.marginals) == {
        "tier": (("a", "4/5"), ("b", "1/5")),
    }


def test_a_column_with_neither_level_weights_for_its_parent_nor_a_support_is_refused(
    tmp_path: Path,
) -> None:
    history = {**_bare_model(), "nullFraction": "0.1"}
    text = _refusal(given_bundle(tmp_path, _fleet.PERIODIC, history=history, bare=True))
    for word in (
        "'tier'",
        "'model'",
        "NULL",
        "declare level weights for 'model'",
        "declare a support for 'tier'",
    ):
        assert word in text, text


def test_a_support_that_leaves_out_a_level_of_the_table_is_no_distribution_of_its_own(
    tmp_path: Path,
) -> None:
    history = {**_bare_model(), "nullFraction": "0.1"}
    target = given_bundle(tmp_path, _fleet.PERIODIC, history=history, support=True, bare=True)
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
def test_a_plan_says_which_distribution_a_fallback_column_is_drawn_from(
    tmp_path: Path, projection: str
) -> None:
    said = (
        f"note: {projection}.tier is drawn given model; where model holds a value no level names "
    )
    said += "(the rule 'model_is_set' of the history sets it), "
    for bare, tail in (
        (False, "tier is drawn from the marginal model's level weights and tier's table imply"),
        (True, "tier is drawn from tier's support, evenly"),
    ):
        target = given_bundle(tmp_path / str(bare), projection, support=bare, bare=bare)
        _edited(target, _history_rule)
        result = pipeline.execute(target, kernel=KERNEL_V2)
        notes = [note for note in pipeline.advice_notes(result) if f"{projection}.tier" in note]
        assert notes == [said + tail]
        assert result.emit(projection).plan.projection.fallback == ("model",)  # type: ignore[union-attr]


#: A table of counts, not of shares: the rows are read as shares, each over its own total.
COUNT_TIERS = {
    "sedan": {"a": "3", "b": "1"},
    "van": {"a": "1", "b": "1"},
    "truck": {"a": "1", "b": "9"},
}


@pytest.mark.parametrize("projection", PROJECTIONS)
def test_a_given_table_of_counts_is_read_as_shares_in_the_implied_marginal(
    tmp_path: Path, projection: str
) -> None:
    # 0.5 * 3/4 + 0.3 * 1/2 + 0.2 * 1/10 = 109/200 of the unnamed rows are ``a``.
    target = given_bundle(tmp_path, projection, size=(3000, 300), tiers=COUNT_TIERS)
    _edited(target, _history_rule)
    plan = pipeline.execute(target, kernel=KERNEL_V2).emit(projection).plan
    assert plan.as_document()["projection"]["fallbackMarginals"] == {
        "tier": {"a": "109/200", "b": "91/200"}
    }
    rows = _fallback_run(target, tmp_path, projection)
    unnamed = [row for row in rows if row["model"] not in LEVELS]
    assert len(unnamed) >= 500
    expected = 109 / 200
    drawn = sum(row["tier"] == "a" for row in unnamed)
    z = (drawn - len(unnamed) * expected) / math.sqrt(len(unnamed) * expected * (1 - expected))
    assert abs(z) < Z_LIMIT, (drawn, len(unnamed), z)


def test_a_fallback_column_changes_the_generator_digest(tmp_path: Path) -> None:
    target = given_bundle(tmp_path, _fleet.PERIODIC)
    _edited(target, _history_rule)
    plan = pipeline.execute(target, kernel=KERNEL_V2).emit(_fleet.PERIODIC).plan
    assert plan.projection is not None
    assert plan.as_document()["projection"]["fallbackGivens"] == ["model"]
    other = replace(plan, projection=replace(plan.projection, fallback=()))
    assert "fallbackGivens" not in other.as_document()["projection"]
    assert other.digest().value != plan.digest().value
    # The marginal is in the document, in exact shares, so the digest covers it.
    assert plan.as_document()["projection"]["fallbackMarginals"] == {
        "tier": {"a": "4/5", "b": "1/5"}
    }
    bare = replace(plan, projection=replace(plan.projection, marginals=()))
    assert "fallbackMarginals" not in bare.as_document()["projection"]
    assert bare.digest().value != plan.digest().value


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
