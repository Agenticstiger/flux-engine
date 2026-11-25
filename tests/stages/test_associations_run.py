# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An association table, end to end (ADR 0039): members, clubs and their memberships.

A membership holds a member and a club and nothing else. Each side's memberships per parent follow
its ``counts`` shape, the rows pair the two sides stub by stub, and no pair repeats. Each test holds
one promise:

* **The table.** Both sides hold exactly the rows the plan laid out for each parent, every pair
  once, and the plan records each side's distance from its shape and what the repair did.
* **One run however it is cut.** The column path's rows are the per-row path's, ``--shard`` and
  ``--jobs`` change no byte, though the repair reads every row.
* **The checks.** ``verify`` adds a line for the pairs and one for each side's rows per parent, and
  a repeated pair or a side outside its tolerance fails it.
* **Old bundles.** Without the association the table is refused as before; kernel 0.1.0 and what
  the pairing cannot hold are refused.
"""

from __future__ import annotations

import collections
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator import columns
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages._module_checks import module_checks
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import build_plan_document, plan
from flux_engine.stages.verify import verify as run_verify
from tests.bundles import repin_contract
from tests.generator.test_rules_over_links import _by_both_paths

from .conftest import REPO_ROOT
from .test_rules_over_links_run import _BINDING, _FIDELITY

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"
_CONTRACT = "store-star.fluid.yml"
_SIMULATION = "simulation.flux.yml"
MEMBERS, CLUBS, ROWS = 3000, 60, 9000
PAIR = "member,club"
MEMBER_SHAPE = '{ bins: ["1", "2", "3", "5", "9"], binWeights: ["0.2", "0.35", "0.3", "0.15"] }'
CLUB_SHAPE = (
    '{ bins: ["50", "100", "150", "200", "400"], binWeights: ["0.25", "0.35", "0.25", "0.15"] }'
)
COUNTS = (
    "    counts:\n"
    f"      - {{ emit: membership, relationship: member, children: {MEMBER_SHAPE} }}\n"
    f"      - {{ emit: membership, relationship: club, children: {CLUB_SHAPE} }}\n"
)
ASSOCIATION = "    associations:\n      - { emit: membership, relationships: [member, club] }\n"
BRIDGE = "    bridges:\n      - { emit: membership, relationships: [member, club] }\n"

_CONTRACT_TEXT = f"""\
fluidVersion: "0.7.7"
kind: DataProduct
id: retail.store_star
name: Memberships
metadata:
  owner:
    team: registry-team
exposes:
  - exposeId: person
    kind: table
{_BINDING.format(name="person")}    contract:
      schema:
        - {{ name: p_id, type: STRING, required: true }}
        - {{ name: p_age, type: INT, required: false }}
    semantics:
      entities:
        - {{ name: person, type: primary, expr: p_id }}
  - exposeId: club
    kind: table
{_BINDING.format(name="club")}    contract:
      schema:
        - {{ name: c_id, type: STRING, required: true }}
        - {{ name: c_size, type: INT, required: false }}
    semantics:
      entities:
        - {{ name: club, type: primary, expr: c_id }}
  - exposeId: membership
    kind: table
{_BINDING.format(name="membership")}    contract:
      schema:
        - {{ name: m_person, type: STRING, required: true }}
        - {{ name: m_club, type: STRING, required: true }}
    semantics:
      relationships:
        - {{ name: member, to: person, fromColumns: [m_person], toColumns: [p_id], cardinality: many_to_one }}
        - {{ name: club, to: club, fromColumns: [m_club], toColumns: [c_id], cardinality: many_to_one }}
"""

_EMPTY = """\
        rows: {rows}
        fidelity:
          encoding: {{ fractionalBits: 16, rounding: floor }}
          records: {{ min: 1, max: 1000000 }}
          columns: {{}}
          properties: []
          budget: {{ maxChecksPerState: 20, maxStateAge: P30D }}
          failureAction: withholdBundle
"""


def _extension(rows: int) -> str:
    person = _FIDELITY.format(
        rows=MEMBERS,
        columns="            p_age: { range: { min: '17', max: '90' } }\n",
        mean="p_age",
    )
    club = _FIDELITY.format(
        rows=CLUBS, columns="            c_size: { range: { min: '1', max: '9' } }\n", mean="c_size"
    )
    return (
        f"      person:\n{person}      club:\n{club}      membership:\n{_EMPTY.format(rows=rows)}"
    )


def _bundle(
    tmp_path: Path, name: str, declared: str = ASSOCIATION + COUNTS, rows: int = ROWS
) -> Path:
    """The membership bundle, its namespace holding *declared* before its emits."""
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    (target / _CONTRACT).write_text(_CONTRACT_TEXT, "utf-8")
    text = (target / _SIMULATION).read_text("utf-8")
    head, _, _ = text.partition("    - productRef:")
    emits = "".join(
        f"    - productRef: retail.store_star\n      exposeId: {expose}\n"
        '      fluidVersion: "0.7.7"\n'
        '      provenance: { contractDigest: "sha256:' + "0" * 64 + '" }\n'
        for expose in ("person", "club", "membership")
    )
    namespace = "  com.agentics.flux/1:\n" + declared
    extension = "extensions:\n" + namespace + "    emits:\n" + _extension(rows)
    (target / _SIMULATION).write_text(head + emits + extension, "utf-8")
    repin_contract(target / _CONTRACT, target / _SIMULATION, "retail.store_star")
    return target


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("association-run"), "association")


@pytest.fixture(scope="module")
def outcome(bundle: Path) -> Any:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def _pairs(outcome: Any) -> List[Tuple[str, str]]:
    emit = outcome.emit("membership")
    return [(row[0], row[1]) for row in emit.rows]


def _report(outcome: Any) -> Dict[str, Any]:
    found: Dict[str, Any] = build_plan_document(outcome)["emits"]["membership"]["association"]
    return found


def test_every_pair_is_held_once_and_each_side_holds_its_laid_out_rows(outcome: Any) -> None:
    pairs = _pairs(outcome)
    assert len(pairs) == ROWS and len(set(pairs)) == ROWS
    people = {row[0] for row in outcome.emit("person").rows}
    clubs = {row[0] for row in outcome.emit("club").rows}
    assert {member for member, _ in pairs} <= people and {club for _, club in pairs} <= clubs
    per_club = collections.Counter(club for _, club in pairs)
    assert max(per_club.values()) <= 400 and len(per_club) == CLUBS
    per_member = collections.Counter(member for member, _ in pairs)
    assert max(per_member.values()) <= 9


def test_the_plan_records_the_distance_of_each_side_and_the_repair(outcome: Any) -> None:
    report = _report(outcome)
    assert report["relationships"] == ["member", "club"] and report["distinct"] is True
    assert report["rows"] == ROWS and report["moved"] == {"caps": [0, 0], "galeRyser": 0}
    assert report["repair"]["switches"] > 0 and report["repair"]["rebuilt"] is False
    assert all(float(distance) < 0.05 for distance in report["distance"])


def test_the_rows_follow_the_first_side_in_order(outcome: Any) -> None:
    order = [row[0] for row in outcome.emit("person").rows]
    position = {key: index for index, key in enumerate(order)}
    ranks = [position[member] for member, _ in _pairs(outcome)]
    assert ranks == sorted(ranks)


@pytest.mark.parametrize("chunk", [4096, 100])
def test_the_column_paths_rows_are_the_per_row_paths_rows(
    monkeypatch: pytest.MonkeyPatch, bundle: Path, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(bundle, kernel=KERNEL_V2)
    )
    assert by_columns.emit("membership").rows == by_rows.emit("membership").rows
    assert taken and all(taken)


@pytest.mark.parametrize("count", [3, 7])
def test_the_shares_joined_are_the_whole_runs_bytes(bundle: Path, outcome: Any, count: int) -> None:
    parts = [
        pipeline.execute(bundle, kernel=KERNEL_V2, shard=(i, count)) for i in range(1, count + 1)
    ]
    joined = b"".join(part.emit("membership").writer.stream_bytes() for part in parts)
    assert joined == outcome.emit("membership").writer.stream_bytes()


def _run(bundle: Path, state: Path, jobs: int) -> Tuple[RunLayout, Any, Any, Any]:
    planned = plan(bundle, state_dir=state, jobs=jobs, kernel="0.2.0")
    assert planned.success, planned.stderr
    applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    verified = run_verify(
        bundle, run_id=layout.run_id, state_dir=state, allow_declared_baseline_drift=True, jobs=jobs
    )
    return layout, planned, applied, verified


def _lines(verified: Any) -> Dict[str, Dict[str, Any]]:
    return {line["rule"]: line for line in verified.artifacts["rules"]["emits"]["membership"]}


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_jobs_change_no_byte_and_verify_holds_every_line(bundle: Path, tmp_path: Path) -> None:
    runs = [pipeline.execute(bundle, kernel=KERNEL_V2, jobs=jobs) for jobs in (1, 4)]
    assert runs[1].emit("membership").processes > 1
    assert (
        runs[0].emit("membership").writer.stream_bytes()
        == runs[1].emit("membership").writer.stream_bytes()
    )
    found = []
    for jobs in (1, 4):
        layout, planned, applied, verified = _run(bundle, tmp_path / f"state-{jobs}", jobs)
        assert verified.success, verified.stderr
        lines = _lines(verified)
        assert lines[f"association:{PAIR}.pairs"]["satisfied"] == ROWS
        for side in ("member", "club"):
            line = lines[f"association:{PAIR}.rows:{side}"]
            assert line["met"] is True and line["allowed"] == "0.1"
        found.append(
            (
                planned.artifacts["plan_digest"],
                {k: v for k, v in applied.artifacts.items() if "digest" in k.lower()},
            )
        )
    assert found[0] == found[1]


def test_a_repeated_pair_and_a_side_outside_its_tolerance_fail_verify(tmp_path: Path) -> None:
    # 7,000 rows are fewer than the shapes' means give: the member side is 0.15 from its shape,
    # above the default tolerance, and the club side 0.2, within the 0.25 its entry declares.
    loose = COUNTS.replace(
        "relationship: club, children:", "relationship: club, tolerance: '0.25', children:"
    )
    short = _bundle(tmp_path, "short", ASSOCIATION + loose, rows=7000)
    layout, _, _, verified = _run(short, tmp_path / "state", 1)
    lines = _lines(verified)
    assert lines[f"association:{PAIR}.rows:member"]["met"] is False and not verified.success
    assert lines[f"association:{PAIR}.rows:club"]["met"] is True
    assert lines[f"association:{PAIR}.pairs"]["met"] is True
    path = layout.emit_path("membership")
    rows: List[List[Any]] = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    rows[1][0], rows[1][1] = rows[0][0], rows[0][1]
    path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n", "utf-8")
    receipt = json.loads(layout.receipt_path.read_text("utf-8"))
    document = json.loads(layout.plan_path.read_text("utf-8"))
    broken = module_checks(short, layout, receipt, document, artifacts_ok=True)
    (line,) = (s for s in broken.rules if s.rule_id == f"association:{PAIR}.pairs")
    assert line.holds.satisfied == line.holds.total - 2 and line.met is False


def test_repeats_allowed_skip_the_repair_and_keep_the_degrees(tmp_path: Path) -> None:
    declared = ASSOCIATION.replace("[member, club] }", "[member, club], distinct: false }")
    repeated = pipeline.execute(_bundle(tmp_path, "repeats", declared + COUNTS), kernel=KERNEL_V2)
    report = _report(repeated)
    assert report["distinct"] is False and report["repair"]["switches"] == 0
    pairs = _pairs(repeated)
    assert len(pairs) == ROWS and len(set(pairs)) < ROWS


def test_without_the_association_the_table_of_two_keys_is_refused_as_before(
    tmp_path: Path,
) -> None:
    # The two counts entries alone leave a fidelity block that declares no column, which only an
    # association (or a snapshot) may: a bundle that declares none is refused as it was.
    with pytest.raises(FluxError) as caught:
        pipeline.execute(_bundle(tmp_path, "plain", COUNTS), kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-5034"


def test_kernel_0_1_0_refuses_the_association(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "0.2.0" in str(caught.value)


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"rows": MEMBERS * CLUBS + 1}, "pairs"),
        (
            {"declared": ASSOCIATION + COUNTS + BRIDGE},
            "bridge",
        ),
        (
            {
                "declared": ASSOCIATION
                + COUNTS.replace(
                    "relationship: member,", 'relationship: member, parentWhen: "p_age > 20",'
                )
            },
            "parentWhen",
        ),
    ],
)
def test_what_the_pairing_cannot_hold_is_refused_before_any_row(
    tmp_path: Path, change: Dict[str, Any], needle: str
) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(_bundle(tmp_path, "refused", **change), kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3403" and needle in str(caught.value)
