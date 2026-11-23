# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run of five related tables: keys unique, foreign keys whole, any job count.

``tests/fixtures/related-star`` is a FLUID 0.7.7 store star (dates, items,
customers, sales and returns) and a Simulation that generates every table from
statistics. These tests run it through the pipeline every command uses and
check the rows a run hands on, not the plan:

* every key the contract declares is unique, and every foreign key value is a
  key its parent's rows hold, for all six relationships;
* the plan records how each foreign key is produced, and ``generatorDigest``
  covers it;
* three jobs make the run one job makes, byte for byte, with the sales table
  large enough to be cut into slices.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.seam.keys import compile_keys
from flux_engine.spec.validate import load_yaml_docs
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.plan import build_plan_document

from .conftest import REPO_ROOT

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"


def _rows(result: pipeline.PipelineResult) -> Dict[str, Tuple[List[str], Any]]:
    return {
        outcome.resolved.expose_id: (list(outcome.expose.column_names), outcome.rows)
        for outcome in result.emits
    }


def _values(tables: Dict[str, Tuple[List[str], Any]], expose_id: str, columns: Any) -> List[Any]:
    names, rows = tables[expose_id]
    indexes = [names.index(column) for column in columns]
    return [tuple(row[index] for index in indexes) for row in rows]


def _contract_keys() -> Any:
    documents, error = load_yaml_docs(FIXTURE / "store-star.fluid.yml")
    assert not error
    keys = compile_keys(documents[0])
    assert keys is not None
    return keys


@pytest.fixture(scope="module")
def result() -> pipeline.PipelineResult:
    return pipeline.execute(FIXTURE)


@pytest.fixture(scope="module")
def result_v2() -> pipeline.PipelineResult:
    """The same bundle under kernel 0.2.0 (KERNEL-SPEC-2), whose run tests/golden pins too."""
    return pipeline.execute(FIXTURE, kernel=KERNEL_V2)


#: The run's integrity holds under both kernels.
BOTH_KERNELS = pytest.mark.parametrize("run", ["result", "result_v2"])


@pytest.mark.integration
@BOTH_KERNELS
def test_every_declared_key_of_the_run_is_unique(run: str, request: pytest.FixtureRequest) -> None:
    tables = _rows(request.getfixturevalue(run))
    checked = 0
    for expose_id, keys in _contract_keys().exposes.items():
        for key in (*keys.keys, *keys.instant_keys):
            rows = _values(tables, expose_id, key)
            assert rows and len(set(rows)) == len(rows), (expose_id, key)
            checked += 1
    assert checked == 5


@pytest.mark.integration
@BOTH_KERNELS
def test_every_foreign_key_of_the_run_names_a_parent_row(
    run: str, request: pytest.FixtureRequest
) -> None:
    tables = _rows(request.getfixturevalue(run))
    checked = 0
    for expose_id, keys in _contract_keys().exposes.items():
        for relationship in keys.relationships:
            parent = set(_values(tables, relationship.target, relationship.to_columns))
            references = [
                row
                for row in _values(tables, expose_id, relationship.from_columns)
                if None not in row
            ]
            assert references and all(row in parent for row in references), relationship.name
            checked += 1
    assert checked == 6


@pytest.mark.integration
def test_the_plan_records_how_each_foreign_key_is_produced(
    result: pipeline.PipelineResult,
) -> None:
    sales = next(e for e in result.emits if e.resolved.expose_id == "store_sales")
    assert sales.plan is not None and sales.plan.relations is not None
    document = sales.plan.as_document()["relations"]
    assert document["ss_sold_date_sk"]["pick"] == "declared"
    assert document["ss_customer_id"]["pick"] == "uniform"
    assert document["ss_ticket_number"]["layout"] == "composite"
    returns = next(e for e in result.emits if e.resolved.expose_id == "store_returns")
    assert returns.plan is not None
    assert returns.plan.as_document()["relations"]["sr_item_sk"]["pick"] == "distinct"
    coverage = {row.column: row.decided_by for row in sales.coverage.columns}
    assert coverage["ss_item_sk"] == coverage["ss_customer_id"] == "relationship"


def _bundle_with_sales_rows(tmp_path: Path, rows: int) -> Path:
    target = tmp_path / "bundle"
    target.mkdir()
    for source in FIXTURE.iterdir():
        text = source.read_text(encoding="utf-8")
        if source.name == "simulation.flux.yml":
            text, count = re.subn(r"(      store_sales:\n        rows: )\d+", rf"\g<1>{rows}", text)
            assert count == 1
        (target / source.name).write_text(text, encoding="utf-8")
    return target


def _bundle_with(tmp_path: Path, rewrite: Any) -> Path:
    target = tmp_path / "edited"
    target.mkdir()
    for source in FIXTURE.iterdir():
        text = source.read_text(encoding="utf-8")
        if source.name == "simulation.flux.yml":
            text = rewrite(text)
        (target / source.name).write_text(text, encoding="utf-8")
    return target


def test_an_expose_planned_twice_from_statistics_is_refused(
    result: pipeline.PipelineResult,
) -> None:
    """The emits schema refuses an identical emit; the planner refuses any other repeat."""
    sales = next(e for e in result.emits if e.resolved.expose_id == "store_sales")
    planned = pipeline._PlannedEmit(
        expose=sales.expose, plan=sales.plan, mapping=None, coverage=sales.coverage
    )
    with pytest.raises(FluxError, match="more than once") as caught:
        pipeline._plans_by_expose([planned, planned])
    assert caught.value.code.code == "FLX-3403"


@pytest.mark.integration
def test_a_relationship_to_an_expose_the_run_does_not_generate_is_refused(tmp_path: Path) -> None:
    def without_items(text: str) -> str:
        emit = text[text.index("    - productRef: retail.store_star\n      exposeId: item\n") :]
        emit = emit[: emit.index("    - productRef", 1)]
        block = text[
            text.index("      item:\n        rows: 200\n") : text.index("      customer:\n")
        ]
        return text.replace(emit, "", 1).replace(block, "", 1)

    with pytest.raises(FluxError, match="does not generate from statistics") as caught:
        pipeline.execute(_bundle_with(tmp_path, without_items))
    assert caught.value.code.code == "FLX-3403"


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
@pytest.mark.parametrize("kernel", [None, KERNEL_V2], ids=["kernel-0.1.0", "kernel-0.2.0"])
def test_three_jobs_make_the_related_run_one_job_makes(tmp_path: Path, kernel: Any) -> None:
    rows = 3 * parallel.MIN_ROWS_PER_JOB + 11
    bundle = _bundle_with_sales_rows(tmp_path, rows)

    one = pipeline.execute(bundle, jobs=1, kernel=kernel)
    three = pipeline.execute(bundle, jobs=3, kernel=kernel)

    assert build_plan_document(three) == build_plan_document(one)
    for mine, theirs in zip(three.emits, one.emits, strict=True):
        assert mine.result == theirs.result
        # Workers keep the rows and hand back the lines every digest is over.
        assert mine.rows == theirs.rows or mine.processes > 1
        assert mine.writer.lines() == theirs.writer.lines()
    sales = [e for e in three.emits if e.resolved.expose_id == "store_sales"]
    assert sales and sales[0].processes == min(3, parallel.cpu_jobs())
    tables = _rows(one)
    returned = _values(tables, "store_returns", ("sr_item_sk", "sr_ticket_number"))
    sold = set(_values(tables, "store_sales", ("ss_item_sk", "ss_ticket_number")))
    assert len(sold) == rows
    assert all(row in sold for row in returned)


def test_a_table_of_no_rows_that_a_relationship_reads_verifies_cleanly(
    tmp_path: Path,
) -> None:
    """``store_returns`` declares no rows: the run verifies, as the row path reads it."""
    from flux_engine.stages.run import run

    bundle = tmp_path / "bundle"
    shutil.copytree(FIXTURE, bundle)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    assert text.count("store_returns:\n        rows: 300\n") == 1
    simulation.write_text(
        text.replace("store_returns:\n        rows: 300\n", "store_returns:\n        rows: 0\n"),
        encoding="utf-8",
    )
    result = run(bundle, state_dir=tmp_path / "state", verify=True)
    assert result.success, result.stderr
