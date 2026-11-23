# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Column coverage through the real stages: reported by ``plan``, refused when strict, recorded.

Two bundles have a column that falls back to a type default, one for each way
rows are produced:

* a scenario-driven emit (the starter bundle) with an optional ``note`` column
  that no override, alias or attribute reaches, so it renders ``null``;
* a generator-driven emit (the small fidelity fixture) whose ``aborted_at``
  TIMESTAMP column has had its declaration taken out, so it is drawn uniformly
  within the simulation's time bounds.

What these cases hold in place is the contract around the report: it is
always made, a strict run refuses before anything is written, and neither
the setting nor the report moves a byte of what an accepted run produces.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

import pytest

from flux_engine.api.stages import StageResult
from flux_engine.cli import main
from flux_engine.core.digest import build_plan_digest
from flux_engine.core.errors import FluxError
from flux_engine.evidence.provenance import PROVENANCE_DIGEST_KEY, provenance_digest
from flux_engine.stages.apply import apply
from flux_engine.stages.errors import ColumnProvenanceMoved
from flux_engine.stages.layout import RunLayout, run_id_for
from flux_engine.stages.plan import plan, read_column_provenance
from flux_engine.stages.run import run
from flux_engine.stages.ship import ship
from flux_engine.stages.verify import verify
from flux_engine.starter import scaffold
from tests.archives import packed_file
from tests.bundles import add_contract_columns, add_second_expose, bundle_with_text

from .conftest import FIXTURE_BUNDLE, read_json

pytestmark = pytest.mark.integration

EXPOSE_ID = "payment_recovery_moment"

#: The small fixture's declaration of ``aborted_at``, the one column the
#: generator-driven case leaves undeclared.
_ABORTED_AT = """            aborted_at:
              range: { min: "2026-07-01T00:00:00Z", max: "2026-09-30T23:59:59Z" }
              null: fail
"""

#: ``(bundle fixture, the defaulted column, its type, what it fell back to)``.
DEFAULTED = [
    pytest.param(
        "defaulted_scenario", "note", "STRING", "null: no override, alias", id="scenario-emit"
    ),
    pytest.param(
        "defaulted_generator",
        "aborted_at",
        "TIMESTAMP",
        "uniform within the simulation's time bounds",
        id="generator-emit",
    ),
]


@pytest.fixture(scope="module")
def starter(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("covered") / "bundle"
    scaffold(target)
    return target


@pytest.fixture(scope="module")
def defaulted_scenario(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("defaulted-scenario") / "bundle"
    scaffold(target)
    return add_contract_columns(target, [("note", "STRING", False)])


@pytest.fixture(scope="module")
def defaulted_generator(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("defaulted-generator") / "bundle"
    return bundle_with_text(FIXTURE_BUNDLE, target, "simulation.flux.yml", _ABORTED_AT, "")


@pytest.fixture(scope="module")
def two_emits(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The starter bundle with an optional gap in each of two scenario-driven exposes."""
    target = tmp_path_factory.mktemp("two-emits") / "bundle"
    scaffold(target)
    add_contract_columns(target, [("note", "STRING", False)])
    return add_second_expose(
        target,
        "second_moment",
        [
            ("subscriber_id", "STRING", True),
            ("aborted_at", "TIMESTAMP", True),
            ("memo", "STRING", False),
        ],
    )


@dataclass(frozen=True)
class Applied:
    """A finished run, made once for the module: a test copies it before it damages anything."""

    state: Path
    run_id: str

    def copy_to(self, destination: Path) -> Path:
        shutil.copytree(self.state, destination)
        return destination

    @property
    def record(self) -> Path:
        return self.state / "runs" / self.run_id / "column-provenance.json"


def _applied(bundle: Path, factory: pytest.TempPathFactory) -> Applied:
    state = factory.mktemp("applied") / "state"
    result = run(bundle, state_dir=state)
    assert result.exit_code == 0, result.stderr
    return Applied(state, result.artifacts["plan"]["run_id"])


@pytest.fixture(scope="module")
def applied_starter(starter: Path, tmp_path_factory: pytest.TempPathFactory) -> Applied:
    return _applied(starter, tmp_path_factory)


@pytest.fixture(scope="module")
def applied_defaulted_scenario(
    defaulted_scenario: Path, tmp_path_factory: pytest.TempPathFactory
) -> Applied:
    return _applied(defaulted_scenario, tmp_path_factory)


@pytest.fixture(scope="module")
def applied_defaulted_generator(
    defaulted_generator: Path, tmp_path_factory: pytest.TempPathFactory
) -> Applied:
    return _applied(defaulted_generator, tmp_path_factory)


def _provenance(state: Path) -> Dict[str, Any]:
    (path,) = state.glob("runs/*/column-provenance.json")
    return read_json(path)


# ---------------------------------------------------------------------------
# Reported, always
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fixture", "column", "type_name", "fallback"), DEFAULTED)
def test_a_plan_names_the_column_that_fell_back_and_goes_on(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    fixture: str,
    column: str,
    type_name: str,
    fallback: str,
) -> None:
    result = plan(request.getfixturevalue(fixture), state_dir=tmp_path / "state")
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert any(line.startswith(f"coverage: {EXPOSE_ID}: 1 of ") for line in lines)
    assert any(line.startswith(f"  {column} ({type_name}): {fallback}") for line in lines)
    assert any("--strict-coverage" in line for line in lines)

    rows = {row["column"]: row for row in result.artifacts["coverage"][EXPOSE_ID]["columns"]}
    assert [name for name, row in rows.items() if row["origin"] == "default"] == [column]


def test_a_plan_of_a_bundle_where_every_column_is_covered_says_nothing_about_coverage(
    starter: Path, tmp_path: Path
) -> None:
    result = plan(starter, state_dir=tmp_path / "state")
    assert result.exit_code == 0, result.stderr
    assert "coverage" not in result.stdout
    rows = result.artifacts["coverage"][EXPOSE_ID]["columns"]
    assert {row["origin"] for row in rows} == {"copied"}


# ---------------------------------------------------------------------------
# Refused, when strict, before anything is written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fixture", "column", "type_name", "fallback"), DEFAULTED)
def test_a_strict_plan_refuses_with_the_column_and_writes_nothing(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    fixture: str,
    column: str,
    type_name: str,
    fallback: str,
) -> None:
    state = tmp_path / "state"
    result = plan(request.getfixturevalue(fixture), state_dir=state, strict_coverage=True)
    assert result.exit_code == 1
    assert result.error_code == "FLX-3304"
    assert f"expose '{EXPOSE_ID}' (1 of " in result.stderr
    assert f"{column} " in result.stderr and type_name in result.stderr
    assert fallback in result.stderr
    assert not list(state.glob("runs/*/plan.json")), "a refused plan leaves no plan behind"


def test_run_and_apply_refuse_a_strict_column_too_and_name_it(
    defaulted_scenario: Path, tmp_path: Path
) -> None:
    ran = run(defaulted_scenario, state_dir=tmp_path / "run-state", strict_coverage=True)
    assert (ran.exit_code, ran.error_code) == (1, "FLX-3304")
    assert "note" in ran.stderr
    assert ran.artifacts["apply"] == {"ran": False, "reason": "plan exited 1"}

    applied = apply(defaulted_scenario, state_dir=tmp_path / "apply-state", strict_coverage=True)
    assert (applied.exit_code, applied.error_code) == (1, "FLX-3304")
    assert not list((tmp_path / "apply-state").glob("runs/*/receipt.json"))


def test_a_strict_run_of_a_covered_bundle_is_accepted(starter: Path, tmp_path: Path) -> None:
    result = run(starter, state_dir=tmp_path / "state", strict_coverage=True)
    assert result.exit_code == 0, result.stderr


# ---------------------------------------------------------------------------
# No byte moves
# ---------------------------------------------------------------------------


def test_the_setting_changes_whether_a_run_is_accepted_and_never_what_it_produces(
    starter: Path, tmp_path: Path
) -> None:
    loose = run(starter, state_dir=tmp_path / "loose")
    strict = run(starter, state_dir=tmp_path / "strict", strict_coverage=True)
    assert loose.exit_code == strict.exit_code == 0

    assert loose.artifacts["plan"]["plan_digest"] == strict.artifacts["plan"]["plan_digest"]
    assert loose.artifacts["apply"]["run_digest"] == strict.artifacts["apply"]["run_digest"]
    assert _provenance(tmp_path / "loose") == _provenance(tmp_path / "strict")


def test_plan_json_holds_the_digest_of_the_record_and_nothing_else_about_coverage(
    defaulted_scenario: Path, tmp_path: Path
) -> None:
    """Rows are in the record; the plan carries one digest, as it does for seed-paths.ndjson."""
    result = plan(defaulted_scenario, state_dir=tmp_path / "state")
    document = result.artifacts["plan_document"]
    assert document["columnProvenanceDigest"].startswith("sha256:")
    assert "origin" not in json.dumps(document) and "coverage" not in json.dumps(document)


@pytest.mark.parametrize("fixture", ["defaulted_scenario", "defaulted_generator"])
def test_the_digest_in_the_plan_is_the_digest_of_the_record_apply_writes(
    request: pytest.FixtureRequest, tmp_path: Path, fixture: str
) -> None:
    state = tmp_path / "state"
    result = run(request.getfixturevalue(fixture), state_dir=state)
    assert result.exit_code == 0, result.stderr
    recorded = result.artifacts["plan"]["plan_document"]["columnProvenanceDigest"]
    (path,) = state.glob("runs/*/column-provenance.json")
    assert provenance_digest(path.read_bytes()).value == recorded
    assert read_json(next(state.glob("runs/*/plan.json")))["columnProvenanceDigest"] == recorded


def test_the_digest_in_the_plan_is_the_same_at_any_jobs(
    defaulted_generator: Path, tmp_path: Path
) -> None:
    one = plan(defaulted_generator, state_dir=tmp_path / "one", jobs=1)
    two = plan(defaulted_generator, state_dir=tmp_path / "two", jobs=2)
    assert one.artifacts["plan_digest"] == two.artifacts["plan_digest"]
    assert (
        one.artifacts["plan_document"]["columnProvenanceDigest"]
        == two.artifacts["plan_document"]["columnProvenanceDigest"]
    )


def test_a_column_that_moves_from_default_to_covered_moves_the_plan_digest(
    tmp_path: Path,
) -> None:
    """Coverage is a statement the plan makes, so changing it changes the plan's identity."""
    bundle = tmp_path / "bundle"
    scaffold(bundle)
    before = plan(
        add_contract_columns(bundle, [("note", "STRING", False)]), state_dir=tmp_path / "a"
    )
    other = tmp_path / "other"
    scaffold(other)
    after = plan(
        add_contract_columns(other, [("market_note", "STRING", False)]), state_dir=tmp_path / "b"
    )
    assert (
        before.artifacts["plan_document"]["columnProvenanceDigest"]
        != after.artifacts["plan_document"]["columnProvenanceDigest"]
    )


def test_the_record_is_the_same_bytes_at_any_jobs(
    defaulted_generator: Path, tmp_path: Path
) -> None:
    one = run(defaulted_generator, state_dir=tmp_path / "one", jobs=1)
    two = run(defaulted_generator, state_dir=tmp_path / "two", jobs=2)
    assert one.exit_code == two.exit_code == 0
    (first,) = (tmp_path / "one").glob("runs/*/column-provenance.json")
    (second,) = (tmp_path / "two").glob("runs/*/column-provenance.json")
    assert first.read_bytes() == second.read_bytes()


# ---------------------------------------------------------------------------
# Recorded by apply
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fixture", "column", "type_name", "fallback"), DEFAULTED)
def test_apply_records_how_each_column_was_produced(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    fixture: str,
    column: str,
    type_name: str,
    fallback: str,
) -> None:
    state = tmp_path / "state"
    assert run(request.getfixturevalue(fixture), state_dir=state).exit_code == 0
    record = _provenance(state)

    assert record["format"] == "flux-column-provenance/1"
    (entry,) = record["exposes"]
    assert entry["exposeId"] == EXPOSE_ID
    rows = {row["column"]: row for row in entry["columns"]}
    assert rows[column]["origin"] == "default"
    assert rows[column]["type"] == type_name
    assert rows[column]["detail"].startswith(fallback)
    assert entry["counts"]["default"] == 1
    assert entry["facets"]["columnLineage"]["fields"][column]["inputFields"] == []


def test_a_copied_column_carries_its_source_attribute_into_the_lineage_facet(
    defaulted_scenario: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    assert run(defaulted_scenario, state_dir=state).exit_code == 0
    (entry,) = _provenance(state)["exposes"]
    (source,) = entry["facets"]["columnLineage"]["fields"]["aborted_at"]["inputFields"]
    assert (source["name"], source["field"]) == ("event", "time")
    assert source["namespace"].startswith("flux://")
    assert source["transformations"][0]["subtype"] == "IDENTITY"


# ---------------------------------------------------------------------------
# Shipped
# ---------------------------------------------------------------------------


def _ship_report(bundle: Path, tmp_path: Path) -> str:
    result = run(
        bundle,
        state_dir=tmp_path / "state",
        ship=True,
        out_dir=tmp_path / "out",
    )
    assert result.exit_code == 0, result.stderr
    (report,) = (tmp_path / "state").glob("runs/*/EVIDENCE.md")
    return report.read_text(encoding="utf-8")


def test_ship_lists_every_column_and_names_the_defaulted_one_as_not_proven(
    defaulted_scenario: Path, tmp_path: Path
) -> None:
    report = _ship_report(defaulted_scenario, tmp_path)
    provenance, not_proven = report.split("## Column Provenance")[1].split("## What Was Not Proven")

    assert f"### `{EXPOSE_ID}`" in provenance
    assert "5 columns: 4 copied, 1 default." in provenance
    assert "- `subscriber_id` (STRING): copied from `subject.id`" in provenance
    assert "- `note` (STRING): default: null: no override, alias or attribute" in provenance
    assert (
        f"These columns fell back to a type default, so no declaration or source describes "
        f"their values: `{EXPOSE_ID}`: `note`."
    ) in not_proven


def test_ship_packs_the_record_with_the_run(defaulted_scenario: Path, tmp_path: Path) -> None:
    _ship_report(defaulted_scenario, tmp_path)
    (archive,) = (tmp_path / "out").glob("*.flux.tgz")
    assert packed_file(archive, "/run/column-provenance.json")


def test_ship_of_a_covered_bundle_says_that_no_column_fell_back(
    starter: Path, tmp_path: Path
) -> None:
    report = _ship_report(starter, tmp_path)
    assert "4 columns: 4 copied." in report
    assert "No column fell back to a type default" in report.split("## What Was Not Proven")[1]


def _default_row(record: Dict[str, Any]) -> Dict[str, Any]:
    return next(row for row in record["exposes"][0]["columns"] if row["origin"] == "default")


def _first_column(record: Dict[str, Any]) -> Dict[str, Any]:
    return record["exposes"][0]["columns"][0]


def _edit(change: Callable[[Dict[str, Any]], None]) -> Callable[[Path, Dict[str, Any], Path], None]:
    """A damage that changes the record's content and writes it back as ordinary JSON."""

    def damage(path: Path, record: Dict[str, Any], other_record: Path) -> None:
        change(record)
        path.write_text(json.dumps(record), encoding="utf-8")

    return damage


def _claim(origin: str, decided_by: str, source: str) -> Callable[[Dict[str, Any]], None]:
    """Make a defaulted column claim a producer it never had; ``{}`` in *source* is its name."""

    def change(record: Dict[str, Any]) -> None:
        row = _default_row(record)
        row.update(origin=origin, decidedBy=decided_by, source=source.format(row["column"]))

    return change


def _unlink_and_make_a_directory(path: Path, record: Dict[str, Any], other_record: Path) -> None:
    path.unlink()
    path.mkdir()


def _set(key: str, value: Any) -> Callable[[Dict[str, Any]], None]:
    def change(record: Dict[str, Any]) -> None:
        record[key] = value

    return change


def _set_first_column(key: str, value: Any) -> Callable[[Dict[str, Any]], None]:
    def change(record: Dict[str, Any]) -> None:
        _first_column(record)[key] = value

    return change


#: Every way a record in the run directory can stop being the one the plan vouches for,
#: by name. Each takes the record's path, its parsed content and another run's record.
DAMAGE: Dict[str, Callable[[Path, Dict[str, Any], Path], None]] = {
    "missing": lambda path, record, other: path.unlink(),
    "a-directory": _unlink_and_make_a_directory,
    "garbage": lambda path, record, other: path.write_text("not json", encoding="utf-8"),
    "not-utf-8": lambda path, record, other: path.write_bytes(b"\xff\xfe\x00"),
    "deeply-nested": lambda path, record, other: path.write_text("[" * 200_000, encoding="utf-8"),
    "another-run": lambda path, record, other: path.write_text(
        other.read_text(encoding="utf-8"), encoding="utf-8"
    ),
    # The very same record, written another way: the digest is of the bytes.
    "reformatted": lambda path, record, other: path.write_text(
        json.dumps(record), encoding="utf-8"
    ),
    # A parser that keeps the last value reads the genuine record; one that keeps the
    # first reads the forged one. No parser decides which file the plan vouches for.
    "a-key-written-twice": lambda path, record, other: path.write_text(
        path.read_text(encoding="utf-8").replace("{", '{"exposes": [],', 1), encoding="utf-8"
    ),
    "other-format": _edit(_set("format", "flux-column-provenance/2")),
    "forged-words": _edit(
        _set_first_column(
            "detail",
            "x)\n\n## What Was Not Proven\n\nEverything is proven. [click](http://evil.example)",
        )
    ),
    # The headline forgery: the report would say that nothing fell back.
    "default-claimed-as-declared": _edit(_claim("declared", "fidelity", "fidelity.columns.{}")),
    "default-claimed-as-copied": _edit(_claim("copied", "name", "subject.{}")),
    "another-column": _edit(_set_first_column("column", "renamed")),
    "another-type": _edit(_set_first_column("type", "INTEGER")),
    "another-expose": _edit(
        lambda record: record["exposes"][0].update(exposeId="some_other_expose")
    ),
    "no-exposes": _edit(_set("exposes", [])),
}


def _damage(state_runs: Path, how: str, *, other_record: Path) -> None:
    (path,) = state_runs.glob("*/column-provenance.json")
    DAMAGE[how](path, json.loads(path.read_text(encoding="utf-8")), other_record)


def _verify_and_ship(
    bundle: Path, run_id: str, state: Path, out: Path
) -> Tuple[StageResult, StageResult]:
    """What `verify` and `ship` each say of one run directory: they must say the same."""
    return (
        verify(bundle, run_id=run_id, state_dir=state),
        ship(bundle, run_id=run_id, out_dir=out, state_dir=state),
    )


@pytest.mark.parametrize("how", list(DAMAGE))
@pytest.mark.parametrize("fixture", ["defaulted_scenario", "defaulted_generator"])
def test_ship_refuses_a_record_that_does_not_hash_to_the_digest_in_the_plan(
    request: pytest.FixtureRequest,
    applied_starter: Applied,
    tmp_path: Path,
    fixture: str,
    how: str,
) -> None:
    """Absent, damaged, forged or someone else's: nothing is packed, and the code says why.

    The two bundles are the two ways an emit's rows are produced. The generator
    one is the case the receipt could not settle, because it holds nothing per
    column for such an emit: the plan's digest does. `verify` says the same as `ship`.
    """
    bundle = request.getfixturevalue(fixture)
    applied: Applied = request.getfixturevalue(f"applied_{fixture}")
    state = applied.copy_to(tmp_path / "state")
    _damage(state / "runs", how, other_record=applied_starter.record)

    verified, shipped = _verify_and_ship(bundle, applied.run_id, state, tmp_path / "out")

    assert (verified.exit_code, verified.error_code) == (1, "FLX-2115"), verified.stderr
    assert (shipped.exit_code, shipped.error_code) == (1, "FLX-2115"), shipped.stderr
    assert "column-provenance.json" in shipped.stderr
    assert not (tmp_path / "out").exists(), "a refused ship packs nothing"
    assert not list(state.glob("runs/*/EVIDENCE.md")), "and writes no report"


def test_the_refusal_names_the_file_and_both_digests(
    defaulted_generator: Path, applied_defaulted_generator: Applied, tmp_path: Path
) -> None:
    applied = applied_defaulted_generator
    state = applied.copy_to(tmp_path / "state")
    recorded = read_json(state / "runs" / applied.run_id / "plan.json")[PROVENANCE_DIGEST_KEY]
    _damage(state / "runs", "default-claimed-as-declared", other_record=Path("unused"))
    result = ship(
        defaulted_generator, run_id=applied.run_id, out_dir=tmp_path / "out", state_dir=state
    )
    assert recorded in result.stderr
    assert "declares" in result.stderr and "recomputes as" in result.stderr
    assert "evil.example" not in result.stderr


def test_apply_again_rewrites_the_record_and_the_ship_that_was_refused_goes_through(
    defaulted_scenario: Path, applied_defaulted_scenario: Applied, tmp_path: Path
) -> None:
    """The remedy FLX-2115 names works: the record is rewritten whole, never edited."""
    state = applied_defaulted_scenario.copy_to(tmp_path / "state")
    run_id = applied_defaulted_scenario.run_id
    _damage(state / "runs", "default-claimed-as-declared", other_record=Path("unused"))
    refused = ship(defaulted_scenario, run_id=run_id, out_dir=tmp_path / "out", state_dir=state)
    assert refused.error_code == "FLX-2115"

    assert apply(defaulted_scenario, run_id=run_id, state_dir=state).exit_code == 0

    shipped = ship(defaulted_scenario, run_id=run_id, out_dir=tmp_path / "out", state_dir=state)
    assert shipped.exit_code == 0, shipped.stderr
    (report_path,) = state.glob("runs/*/EVIDENCE.md")
    assert "`note`" in report_path.read_text(encoding="utf-8").split("## What Was Not Proven")[1]


def test_a_record_that_hashes_to_the_digest_but_this_build_cannot_read_is_refused(
    tmp_path: Path,
) -> None:
    """The file is the one the plan vouches for, and it is a record of some other format.

    That is a plan written by a different engine than the one reading it, so
    nothing here is damaged and the remedy is the one FLX-2115 names: plan again.
    """
    layout = RunLayout.for_state_dir(tmp_path, "0123456789abcdef").ensure()
    text = json.dumps({"format": "flux-column-provenance/9", "exposes": []})
    layout.column_provenance_path.write_text(text, encoding="utf-8")
    bound = {PROVENANCE_DIGEST_KEY: provenance_digest(text.encode("utf-8")).value}

    with pytest.raises(ColumnProvenanceMoved) as error:
        read_column_provenance(layout, bound)

    assert error.value.code.code == "FLX-2115"
    assert error.value.recomputed is None
    assert "this build can read" in str(error.value)
    assert read_column_provenance(layout, {}) is None, "a plan that binds no record believes none"


def _re_identified(state: Path, run_id: str, change: Callable[[Dict[str, Any]], None]) -> str:
    """Rewrite run *run_id* as the run its plan would be after *change*; return the new run id.

    The plan is changed and given the digest of its new contents, so it is a
    well-formed plan of another run: a new run id, with the run digest and the
    receipt (when the run was applied) naming it, and the directory renamed to match.
    """
    old = state / "runs" / run_id
    plan_document = json.loads((old / "plan.json").read_text(encoding="utf-8"))
    change(plan_document)
    digested = build_plan_digest(plan_document)
    plan_document["planDigest"] = digested.ref.value
    new_id = run_id_for(digested.ref.value)
    (old / "plan.json").write_text(json.dumps(plan_document, indent=2, sort_keys=True) + "\n")
    for name in ("run-digest.json", "receipt.json"):
        if not (old / name).is_file():
            continue
        document = json.loads((old / name).read_text(encoding="utf-8"))
        document["planDigest"] = digested.ref.value
        if name == "receipt.json":
            document["runId"] = new_id
        (old / name).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return old.rename(state / "runs" / new_id).name


def _as_an_older_engine_left_it(state: Path, run_id: str) -> str:
    """Rewrite run *run_id* as an engine that did not yet put the digest in the plan left it.

    Its plan has no ``columnProvenanceDigest`` (so a different ``planDigest``
    and a different run id, with the run digest and the receipt naming it) and
    there is no record. Returns the new run id.
    """
    new_id = _re_identified(
        state, run_id, lambda plan_document: plan_document.pop(PROVENANCE_DIGEST_KEY)
    )
    (state / "runs" / new_id / "column-provenance.json").unlink()
    return new_id


def test_apply_refuses_a_plan_whose_digest_this_engine_does_not_reproduce_and_writes_nothing(
    defaulted_scenario: Path, tmp_path: Path
) -> None:
    """A plan written by an engine that classified a column differently, applied by this one.

    The record this apply would write hashes to something the plan does not name. It
    is refused here, before a row is drawn onto disk, instead of leaving a run that
    `verify` and `ship` can only refuse at the end, and that applying again cannot fix.
    """
    state = tmp_path / "state"
    planned = plan(defaulted_scenario, state_dir=state)
    assert planned.exit_code == 0, planned.stderr
    drifted = _re_identified(
        state,
        planned.artifacts["run_id"],
        lambda document: document.update({PROVENANCE_DIGEST_KEY: "sha256:" + "cd" * 32}),
    )

    result = apply(defaulted_scenario, run_id=drifted, state_dir=state)

    assert (result.exit_code, result.error_code) == (1, "FLX-2115"), result.stderr
    assert "cd" * 8 in result.stderr and "would write" in result.stderr
    run_dir = state / "runs" / drifted
    assert not (run_dir / "receipt.json").exists(), "a refused apply leaves no finished run"
    assert not (run_dir / "column-provenance.json").exists()
    assert not list((run_dir / "emits").glob("*")), "and no rows"


def test_a_run_from_before_the_plan_carried_a_digest_still_ships_and_says_nothing_is_bound(
    defaulted_scenario: Path, applied_defaulted_scenario: Applied, tmp_path: Path
) -> None:
    state = applied_defaulted_scenario.copy_to(tmp_path / "state")
    run_id = _as_an_older_engine_left_it(state, applied_defaulted_scenario.run_id)

    shipped = ship(defaulted_scenario, run_id=run_id, out_dir=tmp_path / "out", state_dir=state)

    assert shipped.exit_code == 0, shipped.stderr
    (report_path,) = state.glob("runs/*/EVIDENCE.md")
    report = report_path.read_text(encoding="utf-8")
    assert "No column provenance is bound to this run" in report
    assert "Column provenance is not bound to this run" in report
    assert "No column fell back" not in report


def test_a_record_the_older_plan_never_named_is_not_believed_even_when_it_is_there(
    defaulted_scenario: Path, applied_defaulted_scenario: Applied, tmp_path: Path
) -> None:
    """With no digest in the plan there is nothing to check a file against."""
    state = applied_defaulted_scenario.copy_to(tmp_path / "state")
    run_id = _as_an_older_engine_left_it(state, applied_defaulted_scenario.run_id)
    record = state / "runs" / run_id / "column-provenance.json"
    record.write_text('{"format": "flux-column-provenance/1", "exposes": []}', encoding="utf-8")

    shipped = ship(defaulted_scenario, run_id=run_id, out_dir=tmp_path / "out", state_dir=state)

    assert shipped.exit_code == 0, shipped.stderr
    (report_path,) = state.glob("runs/*/EVIDENCE.md")
    assert "No column provenance is bound to this run" in report_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The chain the statement stands on
# ---------------------------------------------------------------------------


def _name_another_plan(state: Path) -> str:
    """Make run-digest.json name a plan other than the one beside it; return the run id."""
    (path,) = state.glob("runs/*/run-digest.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["planDigest"] = "sha256:" + "ab" * 32
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path.parent.name


def test_ship_refuses_a_run_digest_that_names_another_plan(
    defaulted_scenario: Path, applied_defaulted_scenario: Applied, tmp_path: Path
) -> None:
    state = applied_defaulted_scenario.copy_to(tmp_path / "state")
    run_id = _name_another_plan(state)

    result = ship(defaulted_scenario, run_id=run_id, out_dir=tmp_path / "out", state_dir=state)

    assert (result.exit_code, result.error_code) == (1, "FLX-2113"), result.stderr
    assert "run-digest.json" in result.stderr and "ab" * 8 in result.stderr
    assert not (tmp_path / "out").exists()


def test_verify_refuses_it_too_so_the_two_stages_agree(
    defaulted_scenario: Path, applied_defaulted_scenario: Applied, tmp_path: Path
) -> None:
    state = applied_defaulted_scenario.copy_to(tmp_path / "state")
    run_id = _name_another_plan(state)

    result = verify(defaulted_scenario, run_id=run_id, state_dir=state)

    assert (result.exit_code, result.error_code) == (1, "FLX-2113"), result.stderr


def _remove_the_plan(plan_path: Path) -> None:
    plan_path.unlink()


def _put_a_directory_where_the_plan_was(plan_path: Path) -> None:
    plan_path.unlink()
    plan_path.mkdir()


@pytest.mark.parametrize(
    "how", [_remove_the_plan, _put_a_directory_where_the_plan_was], ids=["missing", "a-directory"]
)
def test_a_run_directory_with_no_plan_is_refused_by_verify_and_ship_alike(
    defaulted_scenario: Path,
    applied_defaulted_scenario: Applied,
    tmp_path: Path,
    how: Callable[[Path], None],
) -> None:
    """Without a plan every check that runs through it is skipped, so the run is refused.

    The record is edited too, so a verify that went on without the plan would
    accept a record `apply` did not write while the run digest stood still.
    """
    state = applied_defaulted_scenario.copy_to(tmp_path / "state")
    run_id = applied_defaulted_scenario.run_id
    record = state / "runs" / run_id / "column-provenance.json"
    record.write_text(record.read_text(encoding="utf-8").replace("copied", "declared"))
    how(state / "runs" / run_id / "plan.json")

    verified, shipped = _verify_and_ship(defaulted_scenario, run_id, state, tmp_path / "out")

    assert (verified.exit_code, verified.error_code) == (1, "FLX-2107"), verified.stderr
    assert (shipped.exit_code, shipped.error_code) == (1, "FLX-2107"), shipped.stderr
    assert not (tmp_path / "out").exists()


def test_verify_refuses_a_renamed_run_directory_whatever_run_digest_holds(
    defaulted_scenario: Path, applied_defaulted_scenario: Applied, tmp_path: Path
) -> None:
    """The directory and its plan disagree, and that is what is said (FLX-2113), not a parse error.

    The plan is read first, so a run directory that was copied or renamed is named
    as such even when the file after it is truncated.
    """
    state = applied_defaulted_scenario.copy_to(tmp_path / "state")
    original = state / "runs" / applied_defaulted_scenario.run_id
    renamed = original.rename(original.with_name("aaaaaaaaaaaaaaaa"))
    (renamed / "run-digest.json").write_text("{trunc", encoding="utf-8")

    result = verify(defaulted_scenario, run_id=renamed.name, state_dir=state)

    assert (result.exit_code, result.error_code) == (1, "FLX-2113"), result.stderr


@pytest.mark.parametrize("record", ["intact", "bound-by-no-plan"])
def test_verify_passes_a_run_whose_record_is_intact_or_was_never_bound(
    defaulted_scenario: Path,
    applied_defaulted_scenario: Applied,
    tmp_path: Path,
    record: str,
) -> None:
    """Refusing a damaged record must not refuse a run that never had a digest to check."""
    state = applied_defaulted_scenario.copy_to(tmp_path / "state")
    run_id = applied_defaulted_scenario.run_id
    if record == "bound-by-no-plan":
        run_id = _as_an_older_engine_left_it(state, run_id)

    assert verify(defaulted_scenario, run_id=run_id, state_dir=state).exit_code == 0


def test_apply_refuses_a_link_planted_in_the_run_directory_and_writes_nothing_through_it(
    starter: Path, applied_starter: Applied, tmp_path: Path
) -> None:
    state = applied_starter.copy_to(tmp_path / "state")
    record = state / "runs" / applied_starter.run_id / "column-provenance.json"
    victim = tmp_path / "victim.txt"
    victim.write_text("original", encoding="utf-8")
    record.unlink()
    record.symlink_to(victim)

    with pytest.raises(FluxError) as refused:
        apply(starter, run_id=applied_starter.run_id, state_dir=state)

    assert refused.value.code.code == "FLX-2118"
    assert victim.read_text(encoding="utf-8") == "original"
    assert record.is_symlink()


# ---------------------------------------------------------------------------
# A name that is not a file, and a file that changes after it was checked
# ---------------------------------------------------------------------------


def _a_pipe(record: Path) -> None:
    record.unlink()
    os.mkfifo(record)


def _a_link_to_a_device(record: Path) -> None:
    record.unlink()
    record.symlink_to("/dev/zero")


def _a_link_to_a_copy(record: Path) -> None:
    copy = record.parents[2] / "copy-of-the-record.json"
    copy.write_bytes(record.read_bytes())
    record.unlink()
    record.symlink_to(copy)


#: Not a regular file, so not the file `apply` wrote. A pipe waits for a writer and a
#: link to a device never ends: the stages must refuse them without reading either.
NOT_A_FILE: Dict[str, Callable[[Path], None]] = {
    "a-pipe": _a_pipe,
    "a-link-to-a-device": _a_link_to_a_device,
    "a-link-to-a-copy-with-the-same-bytes": _a_link_to_a_copy,
}


@pytest.mark.timeout(60)
@pytest.mark.parametrize("stage", ["ship", "verify"])
@pytest.mark.parametrize("plant", list(NOT_A_FILE))
def test_a_name_that_is_not_a_regular_file_is_refused_and_never_read(
    defaulted_scenario: Path,
    applied_defaulted_scenario: Applied,
    tmp_path: Path,
    plant: str,
    stage: str,
) -> None:
    state = applied_defaulted_scenario.copy_to(tmp_path / "state")
    run_id = applied_defaulted_scenario.run_id
    NOT_A_FILE[plant](state / "runs" / run_id / "column-provenance.json")

    if stage == "ship":
        result = ship(defaulted_scenario, run_id=run_id, out_dir=tmp_path / "out", state_dir=state)
    else:
        result = verify(defaulted_scenario, run_id=run_id, state_dir=state)

    assert (result.exit_code, result.error_code) == (1, "FLX-2115"), result.stderr
    assert not (tmp_path / "out").exists()


def _forged_after_the_check(record: Path) -> None:
    record.write_text('{"format": "flux-column-provenance/1", "exposes": []}', encoding="utf-8")


#: What a writer racing `ship` could do to the record once it has been checked.
SWAPPED_AFTER_THE_CHECK: Dict[str, Callable[[Path], None]] = {
    "forged": _forged_after_the_check,
    "a-pipe": _a_pipe,
}


def _after_the_report(monkeypatch: pytest.MonkeyPatch, act: Callable[[Path], None]) -> None:
    """Run *act* on the record once `ship` has checked it and written the report.

    That is the last moment before the pack opens the run directory.
    """
    ship_module = importlib.import_module("flux_engine.stages.ship")
    write_report = ship_module._write_evidence_report

    def report_then_act(layout: RunLayout, **kwargs: Any) -> None:
        write_report(layout, **kwargs)
        act(layout.column_provenance_path)

    monkeypatch.setattr(ship_module, "_write_evidence_report", report_then_act)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("swap", list(SWAPPED_AFTER_THE_CHECK))
def test_ship_packs_the_record_it_checked_even_when_the_file_changes_after_the_check(
    defaulted_scenario: Path,
    applied_defaulted_scenario: Applied,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap: str,
) -> None:
    """What the report was written from is what is packed, byte for byte."""
    state = applied_defaulted_scenario.copy_to(tmp_path / "state")
    run_id = applied_defaulted_scenario.run_id
    checked = (state / "runs" / run_id / "column-provenance.json").read_bytes()
    _after_the_report(monkeypatch, SWAPPED_AFTER_THE_CHECK[swap])

    shipped = ship(defaulted_scenario, run_id=run_id, out_dir=tmp_path / "out", state_dir=state)

    assert shipped.exit_code == 0, shipped.stderr
    (archive,) = (tmp_path / "out").glob("*.flux.tgz")
    assert packed_file(archive, "/run/column-provenance.json") == checked


def test_a_link_planted_after_the_check_stops_the_pack_and_the_file_it_names_is_untouched(
    defaulted_scenario: Path,
    applied_defaulted_scenario: Applied,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = applied_defaulted_scenario.copy_to(tmp_path / "state")
    victim = tmp_path / "victim.txt"
    victim.write_text("original", encoding="utf-8")

    def plant(record: Path) -> None:
        record.unlink()
        record.symlink_to(victim)

    _after_the_report(monkeypatch, plant)

    shipped = ship(
        defaulted_scenario,
        run_id=applied_defaulted_scenario.run_id,
        out_dir=tmp_path / "out",
        state_dir=state,
    )

    assert shipped.exit_code == 1
    assert "symlink" in shipped.stderr
    assert victim.read_text(encoding="utf-8") == "original"
    assert not list((tmp_path / "out").glob("*.flux.tgz")) if (tmp_path / "out").exists() else True


# ---------------------------------------------------------------------------
# Several emits, and the order things happen in
# ---------------------------------------------------------------------------


def test_a_strict_plan_names_the_gaps_of_every_emit_in_one_refusal(
    two_emits: Path, tmp_path: Path
) -> None:
    result = plan(two_emits, state_dir=tmp_path / "state", strict_coverage=True)
    assert (result.exit_code, result.error_code) == (1, "FLX-3304")
    assert "-- 2 columns fell back to a type default" in result.stderr
    assert f"expose '{EXPOSE_ID}' (1 of 5 columns):" in result.stderr
    assert "expose 'second_moment' (1 of 3 columns):" in result.stderr
    assert "note" in result.stderr and "memo" in result.stderr


def test_a_strict_run_is_refused_before_any_rows_are_drawn(
    defaulted_scenario: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal comes from planning, so no emit's rows exist when it is raised."""
    from flux_engine.core.errors import FluxError
    from flux_engine.stages import pipeline

    def drawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("rows were drawn")

    monkeypatch.setattr(pipeline, "_generate_emit", drawn)
    with pytest.raises(FluxError) as error:
        pipeline.execute(defaulted_scenario, strict_coverage=True)
    assert error.value.code.code == "FLX-3304"
    with pytest.raises(AssertionError, match="rows were drawn"):
        pipeline.execute(defaulted_scenario)


def test_the_plan_prints_its_strict_hint_once_however_many_exposes_have_a_gap(
    two_emits: Path, tmp_path: Path
) -> None:
    result = plan(two_emits, state_dir=tmp_path / "state")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.count("coverage: ") == 2
    assert result.stdout.count("--strict-coverage refuses the run") == 1


def test_ship_reports_every_emit_in_order_and_names_each_gap(
    two_emits: Path, tmp_path: Path
) -> None:
    report = _ship_report(two_emits, tmp_path)
    assert report.index(f"### `{EXPOSE_ID}`") < report.index("### `second_moment`")
    assert "- `memo` (STRING): default: null:" in report
    assert (
        f"`{EXPOSE_ID}`: `note`; `second_moment`: `memo`."
        in report.split("## What Was Not Proven")[1]
    )


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def test_the_flag_reaches_the_stage_from_the_command_line(
    defaulted_scenario: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["plan", str(defaulted_scenario), "--strict-coverage", "--json"]
        + ["--state-dir", str(tmp_path / "state")]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert code == 1
    assert envelope["error"]["code"] == "FLX-3304"
    assert "note" in envelope["error"]["message"]


def test_only_the_commands_that_execute_a_run_take_the_flag() -> None:
    from flux_engine.cli import _children, build_parser

    takers = {
        name
        for name, parser in _children(build_parser()).items()
        if "--strict-coverage" in parser._option_string_actions
    }
    assert takers == {"plan", "apply", "run"}


def test_the_plan_prints_a_column_name_that_is_not_plain_text_quoted(tmp_path: Path) -> None:
    """A FLUID column name is any string, so the plan must not print one as it is."""
    from flux_engine.seam.coverage import ColumnCoverage, ExposeCoverage, Origin
    from flux_engine.stages.layout import RunLayout
    from flux_engine.stages.plan import _plan_stdout

    row = ColumnCoverage("evil\x1b[2Jname", "STRING", False, Origin.DEFAULT, None, "none")
    layout = RunLayout.for_state_dir(tmp_path, "0123456789abcdef")
    text = _plan_stdout(
        layout, "0123456789abcdef", "sha256:" + "0" * 64, (), [ExposeCoverage("e", (row,))]
    )
    assert "\x1b" not in text
    assert repr("evil\x1b[2Jname") in text
