# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copy into a foreign key sets that relationship's pick (ADR 0022 section 4 amendment).

A trip has two relationships: ``vehicle``, which lists ``sharedColumns:
[depot_id]``, and ``depot``, whose foreign key is that very column. The vehicle holds
``depot_id`` as its own foreign key to the depot, so the trip's depot is the vehicle's
depot on every row. The tests run the bundle through the pipeline every command uses, and then
through ``plan``, ``apply`` and ``verify``:

* every trip holds its vehicle's depot, a NULL source gives a NULL key, and a rule that
  reads ``depot.<column>`` reads the copied depot's row;
* ``--jobs 1`` and ``--jobs 4`` give the same digests, the column path and the per-row path give the
  same rows, and a slice of rows is the rows of the whole;
* a bundle that does not ask for ``copies: true`` keeps its rows, and each copy the key cannot
  carry is refused before a row exists.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator import columns
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from tests.bundles import repin_contract
from tests.generator.test_rules_over_links import _by_both_paths

from ._same_run import rules_alike_at_one_and_four_jobs
from .conftest import REPO_ROOT
from .test_rules_over_links_run import _BINDING, _FIDELITY

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"
_CONTRACT = "store-star.fluid.yml"
_SIMULATION = "simulation.flux.yml"
TRIPS = 2 * parallel.MIN_ROWS_PER_JOB + 301

_CONTRACT_TEXT = f"""\
fluidVersion: "0.7.7"
kind: DataProduct
id: fleet.trips
name: Trips
metadata:
  owner:
    team: fleet
exposes:
  - exposeId: depot
    kind: table
{_BINDING.format(name="depot")}    contract:
      schema:
        - {{ name: d_id, type: STRING, required: true }}
        - {{ name: d_tier, type: STRING, required: false }}
        - {{ name: d_score, type: INT, required: false }}
    semantics:
      entities:
        - {{ name: depot, type: primary, expr: d_id }}
  - exposeId: vehicle
    kind: table
{_BINDING.format(name="vehicle")}    contract:
      schema:
        - {{ name: v_id, type: STRING, required: true }}
        - {{ name: depot_id, type: STRING, required: false }}
        - {{ name: v_model, type: STRING, required: false }}
        - {{ name: v_seats, type: INT, required: false }}
    semantics:
      entities:
        - {{ name: vehicle, type: primary, expr: v_id }}
      relationships:
        - name: depot
          to: depot
          fromColumns: [depot_id]
          toColumns: [d_id]
          cardinality: many_to_one
  - exposeId: trip
    kind: table
{_BINDING.format(name="trip")}    contract:
      schema:
        - {{ name: t_id, type: STRING, required: true }}
        - {{ name: vehicle_id, type: STRING, required: false }}
        - {{ name: depot_id, type: STRING, required: false }}
        - {{ name: t_tier, type: STRING, required: false }}
        - {{ name: t_km, type: INT, required: false }}
      invariants:
        - id: tier_copy
          derive: t_tier
          expr: "depot.d_tier"
    semantics:
      entities:
        - {{ name: trip, type: primary, expr: t_id }}
      relationships:
        - name: vehicle
          to: vehicle
          fromColumns: [vehicle_id]
          toColumns: [v_id]
          cardinality: many_to_one
          sharedColumns: [depot_id]
        - name: depot
          to: depot
          fromColumns: [depot_id]
          toColumns: [d_id]
          cardinality: many_to_one
"""


def _extension(rows: int, *, extra_trip_columns: str = "", vehicle_columns: str = "") -> str:
    depot = _FIDELITY.format(
        rows=120,
        columns="            d_tier: { levels: { gold: '0.3', silver: '0.7' } }\n"
        "            d_score: { range: { min: '0', max: '100' } }\n",
        mean="d_score",
    )
    vehicle = _FIDELITY.format(
        rows=900,
        columns=(vehicle_columns or "            depot_id: { nullFraction: '0.1' }\n")
        + "            v_model: { levels: { sedan: '0.5', van: '0.5' } }\n"
        "            v_seats: { range: { min: '1', max: '50' } }\n",
        mean="v_seats",
    )
    trip = _FIDELITY.format(
        rows=rows,
        columns="            vehicle_id: { nullFraction: '0.1' }\n"
        "            t_tier: { levels: { x: '0.5', y: '0.5' } }\n"
        "            t_km: { range: { min: '1', max: '500' } }\n" + extra_trip_columns,
        mean="t_km",
    )
    return f"      depot:\n{depot}      vehicle:\n{vehicle}" f"      trip:\n{trip}"


def _bundle(
    tmp_path: Path,
    *,
    rows: int = TRIPS,
    name: str = "bundle",
    contract: str = _CONTRACT_TEXT,
    copies: bool = True,
    links: str = "",
    extra_trip_columns: str = "",
    vehicle_columns: str = "",
) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    (target / _CONTRACT).write_text(contract, "utf-8")
    text = (target / _SIMULATION).read_text("utf-8")
    head, _, _ = text.partition("    - productRef:")
    emits = "".join(
        f"    - productRef: fleet.trips\n      exposeId: {expose}\n"
        '      fluidVersion: "0.7.7"\n'
        '      provenance: { contractDigest: "sha256:' + "0" * 64 + '" }\n'
        for expose in ("depot", "vehicle", "trip")
    )
    asked = "    copies: true\n" if copies else ""
    extension = (
        "extensions:\n  com.agentics.flux/1:\n"
        + asked
        + links
        + "    emits:\n"
        + _extension(
            rows,
            extra_trip_columns=extra_trip_columns,
            vehicle_columns=vehicle_columns,
        )
    )
    (target / _SIMULATION).write_text(head + emits + extension, "utf-8")
    repin_contract(target / _CONTRACT, target / _SIMULATION, "fleet.trips")
    return target


def _tables(outcome: pipeline.PipelineResult) -> Dict[str, List[Dict[str, Any]]]:
    return {
        e.resolved.expose_id: [
            dict(zip(list(e.expose.column_names), row, strict=True)) for row in e.rows
        ]
        for e in outcome.emits
    }


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("fk-copies"))


@pytest.fixture(scope="module")
def result(bundle: Path) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


@pytest.mark.integration
def test_a_line_holds_its_vehicles_depot_and_a_null_source_gives_a_null_key(
    result: pipeline.PipelineResult,
) -> None:
    tables = _tables(result)
    vehicles = {row["v_id"]: row for row in tables["vehicle"]}
    depots = {row["d_id"]: row for row in tables["depot"]}
    same = nulls = unlinked = 0
    for row in tables["trip"]:
        if row["vehicle_id"] is None:
            assert row["depot_id"] is None and row["t_tier"] is None
            unlinked += 1
            continue
        source = vehicles[row["vehicle_id"]]["depot_id"]
        assert row["depot_id"] == source
        if source is None:
            assert row["t_tier"] is None
            nulls += 1
        else:
            assert row["t_tier"] == depots[source]["d_tier"]
            same += 1
    assert same > 0.7 * TRIPS and nulls > 0 and unlinked > 0


@pytest.mark.integration
def test_the_depots_and_the_keys_hold_and_the_copy_is_scored(
    result: pipeline.PipelineResult,
) -> None:
    tables = _tables(result)
    held = {row["d_id"] for row in tables["depot"]}
    assert all(row["depot_id"] in held or row["depot_id"] is None for row in tables["trip"])
    trip = next(e for e in result.emits if e.resolved.expose_id == "trip")
    assert not trip.not_generated
    assert trip.coverage is not None
    coverage = {c.column: (c.origin.value, c.source) for c in trip.coverage.columns}
    assert coverage["depot_id"] == ("derived", "semantics.relationships.depot")


@pytest.mark.integration
def test_without_copies_true_the_two_keys_are_drawn_apart_and_the_declaration_is_listed(
    tmp_path: Path,
) -> None:
    bare = pipeline.execute(_bundle(tmp_path, copies=False), kernel=KERNEL_V2)
    tables = _tables(bare)
    vehicles = {row["v_id"]: row for row in tables["vehicle"]}
    agree = sum(
        row["depot_id"] == vehicles[row["vehicle_id"]]["depot_id"]
        for row in tables["trip"]
        if row["vehicle_id"] is not None
    )
    assert agree < 0.1 * TRIPS
    trip = next(e for e in bare.emits if e.resolved.expose_id == "trip")
    assert [row.names for row in trip.not_generated] == [("depot_id",)]


@pytest.mark.integration
def test_kernel_0_1_0_lists_the_declaration_and_copies_nothing(tmp_path: Path) -> None:
    plain = _CONTRACT_TEXT.replace(
        "      invariants:\n        - id: tier_copy\n          derive: t_tier\n"
        '          expr: "depot.d_tier"\n',
        "",
    )
    old = pipeline.execute(_bundle(tmp_path, copies=False, contract=plain), kernel=KERNEL_V1)
    trip = next(e for e in old.emits if e.resolved.expose_id == "trip")
    assert [row.names for row in trip.not_generated] == [("depot_id",)]


@pytest.mark.parametrize("chunk", [4096, 100])
def test_the_column_paths_rows_are_the_per_row_paths_rows(
    monkeypatch: pytest.MonkeyPatch, bundle: Path, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(bundle, kernel=KERNEL_V2)
    )
    for expose_id in ("depot", "vehicle", "trip"):
        assert by_columns.emit(expose_id).rows == by_rows.emit(expose_id).rows, expose_id
    assert taken and all(taken)


@pytest.mark.parametrize("count", [3, 7])
def test_the_shares_joined_are_the_whole_runs_bytes(bundle: Path, count: int) -> None:
    whole = pipeline.execute(bundle, kernel=KERNEL_V2)
    parts = [
        pipeline.execute(bundle, kernel=KERNEL_V2, shard=(i, count)) for i in range(1, count + 1)
    ]
    joined = b"".join(part.emit("trip").writer.stream_bytes() for part in parts)
    assert joined == whole.emit("trip").writer.stream_bytes()


@pytest.mark.slow
@pytest.mark.integration
def test_plan_apply_and_verify_hold_the_copy_and_give_the_same_digests_for_any_jobs(
    bundle: Path, tmp_path: Path
) -> None:
    emits = rules_alike_at_one_and_four_jobs(bundle, tmp_path)
    by_rule = {entry["rule"]: entry for entry in emits["trip"]}
    copy = by_rule["vehicle.depot_id"]
    assert copy["origin"] == "shared" and copy["enforced"] is True
    assert copy["satisfied"] == copy["total"] and copy["total"] > 0
    tier = by_rule["tier_copy"]
    assert tier["satisfied"] == tier["total"] and tier["total"] > 0


def _refused(bundle: Path, code: str, *words: str) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == code, str(caught.value)
    for word in words:
        assert word in str(caught.value), str(caught.value)


@pytest.mark.integration
def test_a_column_the_vehicle_does_not_hold_as_a_foreign_key_is_refused(
    tmp_path: Path,
) -> None:
    contract = _CONTRACT_TEXT.replace(
        """      relationships:
        - name: depot
          to: depot
          fromColumns: [depot_id]
          toColumns: [d_id]
          cardinality: many_to_one
  - exposeId: trip""",
        "  - exposeId: trip",
        1,
    )
    # Without that relationship the trip's own relationship cannot be checked against a source.
    bundle = _bundle(
        tmp_path,
        contract=contract,
        vehicle_columns="            depot_id: { levels: { a: '0.5', b: '0.5' } }\n",
    )
    _refused(bundle, "FLX-6210", "depot_id", "not known to be a key")


@pytest.mark.integration
def test_an_inner_relationship_fed_by_a_left_one_is_refused(tmp_path: Path) -> None:
    contract = _CONTRACT_TEXT.replace(
        "          sharedColumns: [depot_id]\n        - name: depot\n"
        "          to: depot\n          fromColumns: [depot_id]\n          toColumns: [d_id]\n"
        "          cardinality: many_to_one\n",
        "          sharedColumns: [depot_id]\n        - name: depot\n"
        "          to: depot\n          fromColumns: [depot_id]\n          toColumns: [d_id]\n"
        "          cardinality: many_to_one\n          joinType: inner\n",
        1,
    )
    assert "joinType: inner" in contract
    _refused(_bundle(tmp_path, contract=contract), "FLX-6210", "inner", "NULL")


@pytest.mark.integration
@pytest.mark.parametrize(
    "extra, links, words",
    [
        ("            depot_id: { nullFraction: '0.2' }\n", "", ("nullFraction",)),
        (
            "",
            "    links:\n      - { emit: trip, relationship: depot, weight: d_score }\n",
            ("weights it",),
        ),
    ],
)
def test_a_relationship_that_picks_on_its_own_is_refused(
    tmp_path: Path, extra: str, links: str, words: tuple[str, ...]
) -> None:
    bundle = _bundle(tmp_path, links=links, extra_trip_columns=extra)
    _refused(bundle, "FLX-3403", "copies 'depot_id' through relationship 'vehicle'", *words)


@pytest.mark.integration
def test_a_one_to_one_relationship_is_refused(tmp_path: Path) -> None:
    head, _, last = _CONTRACT_TEXT.rpartition("          cardinality: many_to_one\n")
    contract = head + "          cardinality: one_to_one\n" + last
    _refused(_bundle(tmp_path, contract=contract, rows=100), "FLX-3403", "at most once")


# -- a pick that is read from a column drawn given, or is not its own ----------------------------

_INT_CONTRACT = (
    _CONTRACT_TEXT.replace("{ name: d_id, type: STRING", "{ name: d_id, type: INT")
    .replace("{ name: v_id, type: STRING", "{ name: v_id, type: INT")
    .replace("{ name: depot_id, type: STRING", "{ name: depot_id, type: INT")
    .replace("{ name: vehicle_id, type: STRING", "{ name: vehicle_id, type: INT")
)


def _given_bundle(tmp_path: Path, *, on: str) -> Path:
    """The bundle with its keys as integers, and ``on`` drawn given (or declaring levels for) a key.

    ``on`` is ``"vehicle.depot_id"``: the vehicle's depot takes two levels and its model is drawn
    given it; or ``"trip.vehicle_id"``: the trip's vehicle takes two levels and its tier is drawn
    given it.
    """
    base = pipeline.execute(
        _bundle(tmp_path, rows=50, name="base", contract=_INT_CONTRACT), kernel=KERNEL_V2
    )
    depots = sorted({row["d_id"] for row in _tables(base)["depot"]})
    vehicles = sorted({row["v_id"] for row in _tables(base)["vehicle"]})
    if on == "vehicle.depot_id":
        a, b = depots[1], depots[5]
        found = _bundle(
            tmp_path,
            rows=50,
            name="given",
            contract=_INT_CONTRACT,
            vehicle_columns=f"            depot_id: {{ levels: {{ '{a}': '0.5', '{b}': '0.5' }} }}\n",
        )
        old = "            v_model: { levels: { sedan: '0.5', van: '0.5' } }\n"
        new = (
            "            v_model:\n              given: depot_id\n              levelWeightsGiven:\n"
            f"                '{a}': {{ sedan: '0.9', van: '0.1' }}\n"
            f"                '{b}': {{ sedan: '0.1', van: '0.9' }}\n"
        )
    else:
        a, b = vehicles[1], vehicles[5]
        found = _bundle(
            tmp_path,
            rows=50,
            name="given",
            contract=_INT_CONTRACT,
            extra_trip_columns="",
        )
        old = "            t_tier: { levels: { x: '0.5', y: '0.5' } }\n"
        new = (
            "            vehicle_id: { levels: { '"
            + str(a)
            + "': '0.5', '"
            + str(b)
            + "': '0.5' } }\n"
            "            t_tier:\n              given: vehicle_id\n              levelWeightsGiven:\n"
            f"                '{a}': {{ x: '0.9', y: '0.1' }}\n"
            f"                '{b}': {{ x: '0.1', y: '0.9' }}\n"
        )
    simulation = found / _SIMULATION
    text = simulation.read_text("utf-8")
    assert old in text
    simulation.write_text(text.replace(old, new, 1), "utf-8")
    repin_contract(found / _CONTRACT, simulation, "fleet.trips")
    return found


@pytest.mark.integration
@pytest.mark.parametrize("on", ["vehicle.depot_id", "trip.vehicle_id"])
def test_a_copy_through_a_key_that_is_drawn_given_or_read_by_a_given_is_refused(
    tmp_path: Path, on: str
) -> None:
    bundle = _given_bundle(tmp_path, on=on)
    column = on.split(".")[1]
    _refused(bundle, "FLX-3403", "copies 'depot_id' through relationship 'vehicle'", repr(column))


@pytest.mark.integration
def test_a_copy_through_a_relationship_into_a_history_is_refused(tmp_path: Path) -> None:
    import yaml

    from . import _fleet

    target = _fleet.bundle(tmp_path)
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    visit = _fleet.expose(contract, _fleet.VISIT)
    visit["contract"]["schema"].append({"name": "depot_id", "type": "INT", "required": True})
    relationships = visit["semantics"]["relationships"]
    relationships[0]["sharedColumns"] = ["depot_id"]
    relationships.append(
        {
            "name": "depot",
            "to": _fleet.DEPOT,
            "fromColumns": ["depot_id"],
            "toColumns": ["depot_id"],
            "cardinality": "many_to_one",
            "joinType": "inner",
        }
    )
    _fleet.repin(target, contract)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["extensions"][_fleet.NAMESPACE]["copies"] = True
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    _refused(target, "FLX-3403", "'depot_id'", "relationship 'vehicle'", "declares a history")


@pytest.mark.integration
def test_a_source_relationship_with_no_foreign_key_of_its_own_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flux_engine.generator import relations

    real = relations._check_copy_source

    def check(held: Any, expose_id: str, copied: Any) -> None:
        # The source relationship's column is laid out another way: no foreign key driver.
        real(None, expose_id, copied)

    monkeypatch.setattr(relations, "_check_copy_source", check)
    _refused(
        _bundle(tmp_path, rows=50),
        "FLX-3403",
        "relationship 'depot' of 'vehicle' has no foreign key of its own",
    )
