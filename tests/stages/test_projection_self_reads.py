# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history that reads a projection of itself is refused up front (ADR 0027, the amendment of 2026-11-26).

A snapshot, an events emit or a periodic emit takes its rows from the history it is projected
from. A history whose plan reads one of its own projections, by any path, is not generated: a link
to the snapshot, a link to a table that links to it, a ``within`` entry over it, or the snapshots
of two histories that read each other. The decision is made over the emits before any pass runs,
so every one of them is refused at once, naming the emits in order, and a history that reads a
projection of another history is planned as it was.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.generator import relations
from flux_engine.generator.projection_cycles import refuse_self_reads
from flux_engine.generator.rows import GeneratorPlan
from flux_engine.stages import pipeline
from flux_engine.stages.apply import load_recorded_plan
from flux_engine.stages.replay import replay
from flux_engine.stages.verify import verify

from . import _fleet
from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run
from .test_declared_range_replay import run_as_before
from .test_projection_reads_renewed import (
    DATES,
    SITE,
    SITE_HISTORY,
    ZONES,
    _add_emit,
    _column,
    _fidelity,
    _relationship,
    assert_whole,
    shaped_bundle,
)

pytestmark = pytest.mark.integration

KEY = "projectedReads"
#: A bound that only a run of passes would break: they took minutes for some shapes. That no
#: pass runs is shown by the passes raising when called; the clock only catches a hang, so it
#: allows for a slow machine.
QUICK = 10.0
MODELS = {"sedan": "0.5", "van": "0.3", "truck": "0.2"}
STATUSES = {"levels": {"active": "0.8", "retired": "0.2"}}
KINDS = {"k1": "0.5", "k2": "0.3", "k3": "0.2"}
#: The shapes of the self-reference: the column the history reads of its peer, its type and its
#: statistics, and whether each entity keeps one version or several.
SHAPES: Dict[str, Tuple[str, str, Dict[str, Any], bool]] = {
    "date": ("commissioned_on", "DATE", DATES, True),
    "status": ("status", "STRING", STATUSES, True),
    "multi": ("commissioned_on", "DATE", DATES, False),
}
Change = Callable[[Dict[str, Any], Dict[str, Any]], None]


def _link(name: str, to: str, source: str, target: str) -> Dict[str, Any]:
    return {
        "name": name,
        "to": to,
        "fromColumns": [source],
        "toColumns": [target],
        "cardinality": "many_to_one",
        "joinType": "inner",
    }


def _edit(target: Path, change: Change) -> Path:
    """Apply *change* to the Simulation and to the contract of the bundle, and pin it."""
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    change(document, contract)
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    _fleet.repin(target, contract)
    return target


def _flux(document: Dict[str, Any]) -> Dict[str, Any]:
    flux: Dict[str, Any] = document["extensions"][_fleet.NAMESPACE]
    return flux


def _one_version(flux: Dict[str, Any]) -> None:
    """Each vehicle keeps one version."""
    flux["history"][0]["versions"] = {"bins": ["1", "2"], "binWeights": ["1"]}
    flux["emits"][_fleet.HISTORY]["rows"] = 60
    flux["emits"][_fleet.HISTORY]["fidelity"]["columns"]["vehicle_sk"]["range"]["max"] = "60"


def _link_history(
    flux: Dict[str, Any], contract: Dict[str, Any], link: Dict[str, Any], key: str
) -> Dict[str, Any]:
    """Give the vehicle history a foreign key *key* and the relationship *link* on it."""
    history = _fleet.expose(contract, _fleet.HISTORY)
    history["contract"]["schema"].append(_column(key, "INT"))
    history["semantics"]["relationships"].append(link)
    flux["emits"][_fleet.HISTORY]["fidelity"]["columns"][key] = {
        "range": {"min": "1", "max": "200"}
    }
    return history


def _derive(
    history: Dict[str, Any],
    flux: Dict[str, Any],
    name: str,
    kind: str,
    statistics: Dict[str, Any],
    text: str,
) -> None:
    """A column of the vehicle history drawn from *statistics* and derived from *text*."""
    history["contract"]["schema"].append(_column(name, kind))
    history["contract"].setdefault("invariants", []).append(
        {"id": f"d_{name}", "derive": name, "expr": text}
    )
    flux["emits"][_fleet.HISTORY]["fidelity"]["columns"][name] = statistics


def peering_bundle(
    tmp_path: Path,
    shape: str,
    *,
    copied: bool = False,
    crossed: bool = False,
    derived: bool = True,
) -> Path:
    """The fleet with each version linked to a vehicle (``peer``), and ``peer_x`` derived from it.

    ``copied`` has the snapshot hold ``peer_x`` as well, ``crossed`` grows the fleet with a second
    history first, whose snapshot the vehicle history reads too, and ``derived`` false links the
    versions to the vehicle and derives nothing from it.
    """
    column, kind, statistics, single = SHAPES[shape]
    if crossed:
        column, kind, statistics = "szone", "STRING", {"levels": ZONES}
    target = shaped_bundle(tmp_path, "site") if crossed else _fleet.bundle(tmp_path, name=shape)

    def change(document: Dict[str, Any], contract: Dict[str, Any]) -> None:
        flux = _flux(document)
        if single and not crossed:
            _one_version(flux)
        link = _link("peer", _fleet.SNAPSHOT, "peer_id", "vehicle_id")
        history = _link_history(flux, contract, link, "peer_id")
        if derived:
            history["contract"]["schema"].append(_column("peer_x", kind))
            flux["emits"][_fleet.HISTORY]["fidelity"]["columns"]["peer_x"] = statistics
            history["contract"].setdefault("invariants", []).append(
                {
                    "id": "d_peer_x",
                    "derive": "peer_x",
                    "expr": f"peer.{column}",
                }
            )
        if copied:
            _fleet.expose(contract, _fleet.SNAPSHOT)["contract"]["schema"].append(
                _column("peer_x", kind)
            )
            flux["emits"][_fleet.SNAPSHOT]["fidelity"]["columns"]["peer_x"] = statistics

    return _edit(target, change)


def within_bundle(tmp_path: Path, *, tainted: bool) -> Path:
    """A version that keeps a date within the lifetime of the snapshot row its peer names.

    The lifetime starts at ``commissioned_on``, or, ``tainted``, at a column the version derives
    from its peer and the snapshot holds as well.
    """
    target = peering_bundle(tmp_path, "date", copied=tainted, derived=tainted)

    def change(document: Dict[str, Any], contract: Dict[str, Any]) -> None:
        flux = _flux(document)
        history = _fleet.expose(contract, _fleet.HISTORY)
        history["contract"]["schema"].append(_column("kept_on", "DATE"))
        flux["emits"][_fleet.HISTORY]["fidelity"]["columns"]["kept_on"] = DATES
        flux["lifetimes"] = [
            {"emit": _fleet.SNAPSHOT, "start": "peer_x" if tainted else "commissioned_on"}
        ]
        flux["within"] = [{"emit": _fleet.HISTORY, "relationship": "peer", "date": "kept_on"}]

    return _edit(target, change)


def tagged_bundle(tmp_path: Path, tags: int, *, padding: int = 0) -> Path:
    """A history that reads its own snapshot through *tags* tables, each of which links to it."""
    target = _fleet.bundle(tmp_path, name=f"tagged{tags}_{padding}")

    def change(document: Dict[str, Any], contract: Dict[str, Any]) -> None:
        flux = _flux(document)
        _one_version(flux)
        for index in range(tags):
            name, key = f"tag{index}", f"tag{index}_id"
            tag = _add_emit(
                document,
                contract,
                name,
                [_column(key, "INT"), _column("vehicle_id", "INT"), _column("tag_on", "DATE")],
                [_link("vehicle", _fleet.SNAPSHOT, "vehicle_id", "vehicle_id")],
                key,
                _fidelity(
                    100,
                    {
                        key: {"range": {"min": "1", "max": "100"}},
                        "vehicle_id": {"range": {"min": "1", "max": "1000"}},
                        "tag_on": DATES,
                    },
                    key,
                ),
            )
            tag["contract"]["invariants"] = [
                {"id": f"tag_on{index}", "derive": "tag_on", "expr": "vehicle.commissioned_on"}
            ]
            history = _link_history(flux, contract, _link(name, name, key, key), key)
            _derive(history, flux, f"tagged{index}", "DATE", DATES, f"{name}.tag_on")
        for index in range(padding):
            _add_emit(
                document,
                contract,
                f"pad{index}",
                [_column(f"pad{index}_id", "INT")],
                [],
                f"pad{index}_id",
                _fidelity(
                    10,
                    {f"pad{index}_id": {"range": {"min": "1", "max": "10"}}},
                    f"pad{index}_id",
                ),
            )

    return _edit(target, change)


def mutual_bundle(tmp_path: Path) -> Path:
    """Two histories, each of which reads the snapshot of the other."""
    target = shaped_bundle(tmp_path, "site")

    def change(document: Dict[str, Any], contract: Dict[str, Any]) -> None:
        flux = _flux(document)
        site = _fleet.expose(contract, SITE_HISTORY)
        site["contract"]["schema"] += [_column("vehicle_id", "INT"), _column("vmodel", "STRING")]
        columns = flux["emits"][SITE_HISTORY]["fidelity"]["columns"]
        columns["vehicle_id"] = {"range": {"min": "1", "max": "200"}}
        columns["vmodel"] = {"levels": MODELS}
        site["semantics"]["relationships"].append(
            _link("vehicle", _fleet.SNAPSHOT, "vehicle_id", "vehicle_id")
        )
        site["contract"]["invariants"] = [{"id": "vm", "derive": "vmodel", "expr": "vehicle.model"}]
        entry = next(held for held in flux["history"] if held["emit"] == SITE_HISTORY)
        entry["versions"] = {"bins": ["1", "2"], "binWeights": ["1"]}
        entry.pop("tracked", None)
        _fleet.expose(contract, SITE)["contract"]["schema"].append(_column("vmodel", "STRING"))
        flux["emits"][SITE]["fidelity"]["columns"]["vmodel"] = {"levels": MODELS}

    return _edit(target, change)


def two_sites_bundle(tmp_path: Path) -> Path:
    """The vehicle history reads the snapshots of two other histories, and none reads its own."""
    target = shaped_bundle(tmp_path, "site")

    def change(document: Dict[str, Any], contract: Dict[str, Any]) -> None:
        flux = _flux(document)
        area = _add_emit(
            document,
            contract,
            "area_version",
            [
                _column("area_sk", "INT"),
                _column("area_id", "INT"),
                _column("depot_id", "INT"),
                _column("valid_from", "DATE"),
                _column("valid_to", "DATE", False),
                _column("kind", "STRING"),
            ],
            [_relationship("depot", _fleet.DEPOT, "depot_id")],
            "area_sk",
            _fidelity(
                200,
                {
                    "area_sk": {"range": {"min": "1", "max": "200"}},
                    "area_id": {"range": {"min": "1", "max": "200"}},
                    "valid_from": DATES,
                    "valid_to": DATES,
                    "kind": {"levels": KINDS},
                },
                "area_sk",
            ),
        )
        area["semantics"]["entities"].append({"name": "area", "type": "natural", "expr": "area_id"})
        area["semantics"]["dimensions"] = [
            {
                "name": "valid_from",
                "type": "time",
                "typeParams": {"validityParams": {"isStart": True}},
            },
            {"name": "valid_to", "type": "time", "typeParams": {"validityParams": {"isEnd": True}}},
        ]
        current = _add_emit(
            document,
            contract,
            "area_current",
            [_column("area_id", "INT"), _column("kind", "STRING")],
            [],
            "area_id",
            _fidelity(
                None,
                {"area_id": {"range": {"min": "1", "max": "200"}}, "kind": {"levels": KINDS}},
                "area_id",
            ),
        )
        current["semantics"]["entities"] = [{"name": "area", "type": "primary", "expr": "area_id"}]
        entry = dict(flux["history"][0])
        entry.update(
            {
                "emit": "area_version",
                "entities": 60,
                "parent": {"relationship": "depot", "minChildren": 0},
                "tracked": [
                    {
                        "column": "kind",
                        "transitions": {"k1": {"k2": "1"}, "k2": {"k3": "1"}, "k3": {"k1": "1"}},
                    }
                ],
            }
        )
        flux["history"].append(entry)
        flux["snapshots"].append({"emit": "area_current", "of": "area_version"})
        history = _link_history(
            flux, contract, _relationship("area", "area_current", "area_id"), "area_id"
        )
        _derive(history, flux, "akind", "STRING", {"levels": KINDS}, "area.kind")

    return _edit(target, change)


#: Each shape that reads a projection of its own history, and the emits the refusal names.
SELF_READS: Dict[str, Tuple[Callable[[Path], Path], str]] = {
    "date": (lambda tmp: peering_bundle(tmp, "date"), "vehicle_version -> vehicle_current"),
    "status": (lambda tmp: peering_bundle(tmp, "status"), "vehicle_version -> vehicle_current"),
    "multi": (lambda tmp: peering_bundle(tmp, "multi"), "vehicle_version -> vehicle_current"),
    "copied": (
        lambda tmp: peering_bundle(tmp, "date", copied=True),
        "vehicle_version -> vehicle_current",
    ),
    "crossed": (
        lambda tmp: peering_bundle(tmp, "date", copied=True, crossed=True),
        "vehicle_version -> vehicle_current",
    ),
    "within_tainted": (
        lambda tmp: within_bundle(tmp, tainted=True),
        "vehicle_version -> vehicle_current",
    ),
    "within_only": (
        lambda tmp: within_bundle(tmp, tainted=False),
        "vehicle_version -> vehicle_current",
    ),
    "middle": (lambda tmp: tagged_bundle(tmp, 1), "vehicle_version -> tag0 -> vehicle_current"),
    "fanned": (
        lambda tmp: tagged_bundle(tmp, 2, padding=4),
        "vehicle_version -> tag0 -> vehicle_current",
    ),
    "mutual": (
        mutual_bundle,
        "site_version -> vehicle_current -> vehicle_version -> site_current",
    ),
}
EMITS = [_fleet.HISTORY, _fleet.SNAPSHOT, _fleet.EVENTS, _fleet.PERIODIC]


@pytest.fixture
def no_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan that reaches a pass, or settles a history, fails the test."""

    def reached(*_args: Any, **_options: Any) -> Any:
        raise AssertionError("a pass ran before the refusal")

    for name in ("_settled_histories", "_reading_projections", "_renewed_plans"):
        monkeypatch.setattr(pipeline, name, reached)


@pytest.mark.parametrize("shape", SELF_READS)
@pytest.mark.usefixtures("no_passes")
def test_a_history_that_reads_a_projection_of_itself_is_refused_up_front(
    tmp_path: Path, shape: str
) -> None:
    build, path = SELF_READS[shape]
    target = build(tmp_path)
    started = time.perf_counter()
    refused = _fleet.plan_refused(target, tmp_path / "state")
    assert time.perf_counter() - started < QUICK
    assert refused.error_code == "FLX-3403"
    message = " ".join(refused.stderr.split())
    history = path.split(" -> ")[0]
    assert f"history {history!r} reads a projection of itself: {path}." in message
    assert "reads a projection of itself, directly or through other emits, is not generated" in (
        message
    )
    assert f"read a drawn column of {history!r} instead" in message
    assert "still changing" not in message


@pytest.mark.parametrize("shape", ["date", "multi", "within_only", "middle", "mutual"])
def test_a_plan_recorded_before_the_refusal_replays_its_own_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    target = SELF_READS[shape][0](tmp_path)
    state = tmp_path / "state"
    layout, digest = run_as_before(
        target,
        state,
        monkeypatch,
        "_planned_with_projections",
        era=pipeline.PlanEra(projected_reads=False),
    )
    assert KEY not in load_recorded_plan(layout)
    recorded = {path.name: path.read_bytes() for path in layout.emits_dir.glob("*.ndjson")}
    assert recorded
    for jobs in (1, 4):
        replayed = replay(
            target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=jobs
        )
        assert replayed.success, replayed.stderr
        for name, content in recorded.items():
            assert (layout.emits_dir / name).read_bytes() == content, name


def test_a_history_that_reads_a_projection_of_another_history_still_plans(tmp_path: Path) -> None:
    state = tmp_path / "state"
    target = shaped_bundle(tmp_path, "site")
    layout = _fleet.run_bundle(target, state)
    assert load_recorded_plan(layout)[KEY] == "final"
    verified = verify(target, run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stderr
    lines = {
        expose_id: {line["rule"]: line for line in found}
        for expose_id, found in verified.artifacts["rules"]["emits"].items()
    }
    assert_whole(lines[_fleet.HISTORY], "zone")
    assert_whole(lines[_fleet.SNAPSHOT], "snapshot:equals_last_version")
    assert_same_run_at_one_and_four_jobs(target)
    assert_shares_join_to_the_run(target, tmp_path, _fleet.run_bundle, 3, EMITS)


def test_a_history_that_reads_the_projections_of_two_other_histories_plans(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    target = two_sites_bundle(tmp_path)
    layout = _fleet.run_bundle(target, state)
    assert load_recorded_plan(layout)[KEY] == "final"
    zones = {row["site_id"]: row["zone"] for row in _fleet.rows(layout, SITE)}
    kinds = {row["area_id"]: row["kind"] for row in _fleet.rows(layout, "area_current")}
    versions = _fleet.rows(layout, _fleet.HISTORY)
    assert zones and kinds and versions
    assert all(row["szone"] == zones[row["site_id"]] for row in versions)
    assert all(row["akind"] == kinds[row["area_id"]] for row in versions)
    verified = verify(target, run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stderr
    assert_same_run_at_one_and_four_jobs(target)


def test_plans_that_do_not_settle_are_refused_with_the_emits_still_moving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No history reads its own projection, so the plans settle; the cap is a backstop."""
    target = shaped_bundle(tmp_path, "site")
    monkeypatch.setattr(pipeline, "_moved_history", lambda *_args: True)
    refused = _fleet.plan_refused(target, tmp_path / "state")
    assert refused.error_code == "FLX-3403"
    message = " ".join(refused.stderr.split())
    assert "are still changing after" in message
    assert "still moving:" in message
    assert _fleet.SNAPSHOT in message


# The reachability itself, over plans that hold nothing but the emits they read and sum.


def _plan(*, reads: Tuple[str, ...] = (), sums: Tuple[str, ...] = ()) -> GeneratorPlan:
    return cast(
        GeneratorPlan,
        SimpleNamespace(
            reads=tuple((name, None) for name in reads),
            sums=tuple((name, "via", None) for name in sums),
        ),
    )


def _refused(plans: Dict[str, GeneratorPlan], projections: Dict[str, str]) -> Optional[List[str]]:
    """The emits a refusal names, in order, or ``None`` when *plans* are not refused."""
    try:
        refuse_self_reads(plans, projections)
    except FluxError as error:
        assert error.code is relations.FLX_3403
        return str(error.detail).split("itself: ")[1].split(". ")[0].split(" -> ")
    return None


def test_the_emits_a_history_reaches_decide_it() -> None:
    projections = {"snap": "hist"}
    plain = {"hist": _plan(), "snap": _plan()}
    assert _refused(plain, projections) is None
    assert _refused({**plain, "hist": _plan(reads=("snap",))}, projections) == ["hist", "snap"]
    assert _refused(
        {"hist": _plan(reads=("mid",)), "mid": _plan(reads=("snap",)), "snap": _plan()},
        projections,
    ) == ["hist", "mid", "snap"]
    # A summary is a path too: the history sums children that read its snapshot.
    assert _refused(
        {"hist": _plan(sums=("child",)), "child": _plan(reads=("snap",)), "snap": _plan()},
        projections,
    ) == ["hist", "child", "snap"]


def test_a_cycle_of_reads_and_sums_that_holds_no_projection_is_not_refused() -> None:
    plans = {
        "parent": _plan(sums=("hist",)),
        "hist": _plan(reads=("parent",)),
        "snap": _plan(),
    }
    assert _refused(plans, {"snap": "hist"}) is None


def test_histories_that_read_each_others_projections_are_refused_and_one_that_reads_is_not() -> (
    None
):
    projections = {"snap": "hist", "other_snap": "other"}
    plans = {
        "hist": _plan(reads=("other_snap",)),
        "snap": _plan(),
        "other": _plan(),
        "other_snap": _plan(),
    }
    assert _refused(plans, projections) is None
    plans["other"] = _plan(reads=("snap",))
    assert _refused(plans, projections) == ["hist", "other_snap", "other", "snap"]
