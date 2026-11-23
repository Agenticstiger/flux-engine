# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bridge table, end to end (ADR 0022 section 8): students, courses and their enrolments.

An enrolment holds a student and a course, and no pair repeats. ``fluxsim verify`` adds a line
for the bridge, which covers every row and holds where the row's pair occurs once; one repeated
pair in the rows fails the run. ``--jobs`` does not change the bytes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages._module_checks import module_checks
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from flux_engine.stages.verify import verify as run_verify
from tests.bundles import repin_contract

from .conftest import REPO_ROOT
from .test_rules_over_links_run import _BINDING, _FIDELITY

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"
_CONTRACT = "store-star.fluid.yml"
_SIMULATION = "simulation.flux.yml"
STUDENTS, COURSES = 120, 100
ENROLMENTS = 2 * parallel.MIN_ROWS_PER_JOB + 301
BRIDGES = "    bridges:\n      - { emit: enrolment, relationships: [student, course] }\n"
NAMESPACE = "  com.agentics.flux/1:\n    copies: true\n"

_CONTRACT_TEXT = f"""\
fluidVersion: "0.7.7"
kind: DataProduct
id: retail.store_star
name: Enrolment
metadata:
  owner:
    team: growth-lab
exposes:
  - exposeId: student
    kind: table
{_BINDING.format(name="student")}    contract:
      schema:
        - {{ name: s_id, type: STRING, required: true }}
        - {{ name: s_name, type: STRING, required: false }}
        - {{ name: s_age, type: INT, required: false }}
    semantics:
      entities:
        - {{ name: student, type: primary, expr: s_id }}
  - exposeId: course
    kind: table
{_BINDING.format(name="course")}    contract:
      schema:
        - {{ name: c_id, type: STRING, required: true }}
        - {{ name: c_title, type: STRING, required: false }}
        - {{ name: c_credits, type: INT, required: false }}
    semantics:
      entities:
        - {{ name: course, type: primary, expr: c_id }}
  - exposeId: enrolment
    kind: table
{_BINDING.format(name="enrolment")}    contract:
      schema:
        - {{ name: e_student, type: STRING, required: false }}
        - {{ name: e_course, type: STRING, required: false }}
        - {{ name: s_name, type: STRING, required: false }}
        - {{ name: c_title, type: STRING, required: false }}
        - {{ name: e_hours, type: INT, required: false }}
    semantics:
      relationships:
        - name: student
          to: student
          fromColumns: [e_student]
          toColumns: [s_id]
          cardinality: many_to_one
          sharedColumns: [s_name]
        - name: course
          to: course
          fromColumns: [e_course]
          toColumns: [c_id]
          cardinality: many_to_one
          sharedColumns: [c_title]
"""


def _extension(rows: int) -> str:
    student = _FIDELITY.format(
        rows=STUDENTS,
        columns="            s_name: { levels: { ann: '0.5', bo: '0.5' } }\n"
        "            s_age: { range: { min: '17', max: '60' } }\n",
        mean="s_age",
    )
    course = _FIDELITY.format(
        rows=COURSES,
        columns="            c_title: { levels: { art: '0.5', maths: '0.5' } }\n"
        "            c_credits: { range: { min: '1', max: '12' } }\n",
        mean="c_credits",
    )
    enrolment = _FIDELITY.format(
        rows=rows,
        columns="            e_hours: { range: { min: '1', max: '40' } }\n"
        "            s_name: { levels: { x: '0.5', y: '0.5' } }\n"
        "            c_title: { levels: { x: '0.5', y: '0.5' } }\n",
        mean="e_hours",
    )
    return f"      student:\n{student}      course:\n{course}      enrolment:\n{enrolment}"


def _bundle(tmp_path: Path, name: str, *, bridges: str = BRIDGES, rows: int = ENROLMENTS) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    (target / _CONTRACT).write_text(_CONTRACT_TEXT, "utf-8")
    text = (target / _SIMULATION).read_text("utf-8")
    head, _, _ = text.partition("    - productRef:")
    emits = "".join(
        f"    - productRef: retail.store_star\n      exposeId: {expose}\n"
        '      fluidVersion: "0.7.7"\n'
        '      provenance: { contractDigest: "sha256:' + "0" * 64 + '" }\n'
        for expose in ("student", "course", "enrolment")
    )
    extension = "extensions:\n" + NAMESPACE + bridges + "    emits:\n" + _extension(rows)
    (target / _SIMULATION).write_text(head + emits + extension, "utf-8")
    repin_contract(target / _CONTRACT, target / _SIMULATION, "retail.store_star")
    return target


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("bridge-run"), "bridge")


@pytest.fixture(scope="module")
def tables(bundle: Path) -> Dict[str, List[Dict[str, Any]]]:
    outcome = pipeline.execute(bundle, kernel=KERNEL_V2)
    return {
        e.resolved.expose_id: [
            dict(zip(list(e.expose.column_names), row, strict=True)) for row in e.rows
        ]
        for e in outcome.emits
    }


def test_no_pair_repeats_and_both_sides_name_a_row_their_parent_holds(
    tables: Dict[str, List[Dict[str, Any]]],
) -> None:
    students = {row["s_id"]: row for row in tables["student"]}
    courses = {row["c_id"]: row for row in tables["course"]}
    pairs = [(row["e_student"], row["e_course"]) for row in tables["enrolment"]]
    assert len(pairs) == ENROLMENTS and len(set(pairs)) == ENROLMENTS
    for row in tables["enrolment"]:
        assert row["s_name"] == students[row["e_student"]]["s_name"]
        assert row["c_title"] == courses[row["e_course"]]["c_title"]


def test_kernel_0_1_0_refuses_the_bridges_key_and_names_the_kernel(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "0.2.0" in str(caught.value)


def test_more_rows_than_pairs_is_refused_before_any_row(tmp_path: Path) -> None:
    refused = _bundle(tmp_path, "toomany", rows=STUDENTS * COURSES + 1)
    with pytest.raises(FluxError) as caught:
        pipeline.execute(refused, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3403" and "pairs" in str(caught.value)


def test_validate_reads_the_declaration(bundle: Path) -> None:
    assert validate(bundle).success


def _run(bundle: Path, state: Path, jobs: int) -> "tuple[RunLayout, Any, Any, Any]":
    planned = plan(bundle, state_dir=state, jobs=jobs)
    assert planned.success, planned.stderr
    applied = apply(bundle, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    verified = run_verify(
        bundle,
        run_id=layout.run_id,
        state_dir=state,
        allow_declared_baseline_drift=True,
        jobs=jobs,
    )
    return layout, planned, applied, verified


@pytest.mark.slow
def test_plan_apply_and_verify_give_the_same_bytes_for_any_jobs_and_a_line_for_the_bridge(
    bundle: Path, tmp_path: Path
) -> None:
    digests = []
    for jobs in (1, 4):
        layout, planned, applied, verified = _run(bundle, tmp_path / f"state-{jobs}", jobs)
        assert verified.success, verified.stderr
        lines = {e["rule"]: e for e in verified.artifacts["rules"]["emits"]["enrolment"]}
        line = lines["bridge:student,course"]
        assert (line["form"], line["origin"], line["enforced"]) == ("check", "bundle", True)
        assert line["satisfied"] == line["total"] == ENROLMENTS and line["met"] is True
        digests.append(
            (
                planned.artifacts.get("plan_digest"),
                {
                    k: v
                    for k, v in applied.artifacts.items()
                    if "digest" in k.lower() and k != "run_id"
                },
            )
        )
    assert digests[0] == digests[1]


def test_a_repeated_pair_in_the_rows_breaks_the_bridge_line(bundle: Path, tmp_path: Path) -> None:
    layout, _, _, verified = _run(bundle, tmp_path / ".flux", 1)
    assert verified.success, verified.stderr
    path = layout.emit_path("enrolment")
    names = json.loads(layout.seam_attestation_path("enrolment").read_text("utf-8"))["columns"]
    rows: List[List[Any]] = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    student, course = names.index("e_student"), names.index("e_course")
    rows[1][student], rows[1][course] = rows[0][student], rows[0][course]
    path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n", "utf-8")
    receipt = json.loads(layout.receipt_path.read_text("utf-8"))
    document = json.loads(layout.plan_path.read_text("utf-8"))
    broken = module_checks(bundle, layout, receipt, document, artifacts_ok=True)
    (line,) = (s for s in broken.rules if s.rule_id == "bridge:student,course")
    assert line.holds.satisfied == line.holds.total - 2 and line.enforced and line.met is False
    assert any(t.startswith("rule enrolment.bridge:student,course:") for t in broken.failures())
