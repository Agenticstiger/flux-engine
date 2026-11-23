# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose contract carries a dependency: a sale's ticket carries its date, customer and quantity.

``tests/fixtures/related-star`` is copied and its ``store_sales`` gains one invariant, a
dependency from ``ss_ticket_number`` to ``ss_sold_date_sk``, ``ss_customer_id`` and
``ss_quantity``. The tests run it through the pipeline every command uses, under kernel
``0.1.0`` and kernel ``0.2.0``, and check what a run hands on: every row of a ticket carries
one tuple, the two foreign keys still name rows their parents hold, the plan records the
dependency, coverage names it, and ``validate`` refuses what ``plan`` refuses.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.core import kernels
from flux_engine.core.errors import FluxError
from flux_engine.stages import pipeline
from tests.bundles import repin_contract

from .conftest import REPO_ROOT

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"
_CONTRACT = "store-star.fluid.yml"
_ANCHOR = '        - { name: ss_net_paid, type: "DECIMAL(7,2)", required: true }\n'
_DEPENDENCY = (
    "      invariants:\n"
    "        - id: ticket\n"
    "          dependency:\n"
    "            from: [ss_ticket_number]\n"
    "            to: [ss_sold_date_sk, ss_customer_id, ss_quantity]\n"
)
_TO = ("ss_sold_date_sk", "ss_customer_id", "ss_quantity")


def _bundle(tmp_path: Path, *, invariants: str = _DEPENDENCY, name: str = "bundle") -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    contract = target / _CONTRACT
    text = contract.read_text("utf-8")
    assert text.count(_ANCHOR) == 1
    contract.write_text(text.replace(_ANCHOR, _ANCHOR + invariants, 1), "utf-8")
    repin_contract(contract, target / "simulation.flux.yml", "retail.store_star")
    return target


def _outcome(result: pipeline.PipelineResult, expose_id: str) -> pipeline.EmitOutcome:
    return next(e for e in result.emits if e.resolved.expose_id == expose_id)


def _table(result: pipeline.PipelineResult, expose_id: str) -> List[Dict[str, Any]]:
    outcome = _outcome(result, expose_id)
    names = list(outcome.expose.column_names)
    return [dict(zip(names, row, strict=True)) for row in outcome.rows]


@pytest.fixture(scope="module", params=["0.1.0", "0.2.0"])
def result(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> pipeline.PipelineResult:
    bundle = _bundle(tmp_path_factory.mktemp("deps-run"))
    if request.param == "0.1.0":
        return pipeline.execute(bundle)
    return pipeline.execute(bundle, kernel=kernels.kernel_named("0.2.0"))


@pytest.mark.integration
def test_every_row_of_a_ticket_carries_one_tuple(result: pipeline.PipelineResult) -> None:
    sales = _table(result, "store_sales")
    assert len(sales) == 4000
    tuples: Dict[Any, set] = {}
    for row in sales:
        tuples.setdefault(row["ss_ticket_number"], set()).add(tuple(row[c] for c in _TO))
    assert len(tuples) > 300
    assert all(len(found) == 1 for found in tuples.values())
    assert len({next(iter(found)) for found in tuples.values()}) > 200


@pytest.mark.integration
def test_the_foreign_keys_still_name_rows_their_parents_hold(
    result: pipeline.PipelineResult,
) -> None:
    sales = _table(result, "store_sales")
    dates = {row["d_date_sk"] for row in _table(result, "date_dim")}
    customers = {row["c_customer_id"] for row in _table(result, "customer")}
    assert any(row["ss_sold_date_sk"] is None for row in sales)
    assert any(row["ss_customer_id"] is None for row in sales)
    for row in sales:
        assert row["ss_sold_date_sk"] is None or row["ss_sold_date_sk"] in dates
        assert row["ss_customer_id"] is None or row["ss_customer_id"] in customers
        assert 1 <= row["ss_quantity"] <= 10


@pytest.mark.integration
def test_the_plan_and_the_coverage_name_the_dependency(result: pipeline.PipelineResult) -> None:
    outcome = _outcome(result, "store_sales")
    assert outcome.plan is not None
    (rule,) = outcome.plan.as_document()["rules"]["rules"]
    assert rule["id"] == "ticket" and rule["from"] == ["ss_ticket_number"]
    assert rule["to"] == list(_TO) and rule["origin"] == "contract"
    coverage = {row.column: row for row in outcome.coverage.columns}
    for column in _TO:
        assert (coverage[column].origin.value, coverage[column].decided_by) == (
            "derived",
            "dependency",
        )
        assert coverage[column].source == "contract.invariants.ticket"
    assert coverage["ss_item_sk"].decided_by == "relationship"
    other = _outcome(result, "item")
    assert other.plan is not None and "rules" not in other.plan.as_document()


@pytest.mark.integration
def test_a_run_with_a_dependency_is_the_same_run_twice(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    first = pipeline.execute(bundle, strict_coverage=True)
    second = pipeline.execute(bundle, strict_coverage=True)
    assert _outcome(first, "store_sales").rows == _outcome(second, "store_sales").rows


@pytest.mark.integration
def test_jobs_change_nothing(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    one = pipeline.execute(bundle, jobs=1)
    many = pipeline.execute(bundle, jobs=2)
    assert _outcome(one, "store_sales").rows == _outcome(many, "store_sales").rows


def _refusal(tmp_path: Path, invariants: str, name: str) -> Optional[FluxError]:
    try:
        pipeline.execute(_bundle(tmp_path, invariants=invariants, name=name))
    except FluxError as error:
        return error
    return None


@pytest.mark.integration
def test_plan_and_validate_refuse_what_each_other_refuses(tmp_path: Path) -> None:
    from flux_engine.stages.validate import validate

    both_ways = _DEPENDENCY.replace(
        "            to:", "            bothWays: true\n            to:"
    )
    error = _refusal(tmp_path, both_ways, "plan")
    assert error is not None and error.code.code == "FLX-6210"
    refused = validate(_bundle(tmp_path, invariants=both_ways, name="validate"))
    assert refused.exit_code != 0 and "FLX-6210" in refused.stderr
    key = (
        "      invariants:\n"
        "        - id: ticket\n"
        "          dependency:\n"
        "            from: [ss_quantity]\n"
        "            to: [ss_ticket_number]\n"
    )
    error = _refusal(tmp_path, key, "plan-key")
    assert error is not None and error.code.code == "FLX-6210" and "key" in str(error)
    assert validate(_bundle(tmp_path, invariants=key, name="validate-key")).exit_code != 0
    assert validate(_bundle(tmp_path, name="good")).exit_code == 0


_BANDED = _DEPENDENCY + "          mustBeLessOrEqualTo: 20\n          unit: percent\n"


@pytest.mark.integration
@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
def test_an_upper_bound_band_puts_the_columns_own_nulls_on_rows_not_keys(
    tmp_path: Path, version: str
) -> None:
    bundle = _bundle(tmp_path, invariants=_BANDED)
    kernel = None if version == "0.1.0" else kernels.kernel_named(version)
    banded = _table(pipeline.execute(bundle, kernel=kernel), "store_sales")
    for column in _TO:
        found: Dict[Any, set] = {}
        for row in banded:
            if row[column] is not None:
                found.setdefault(row["ss_ticket_number"], set()).add(row[column])
        assert all(len(values) == 1 for values in found.values()), column
    # A NULL is a row's own, so a ticket can hold both a NULL and the ticket's value.
    nulls = {r["ss_ticket_number"] for r in banded if r["ss_customer_id"] is None}
    values = {r["ss_ticket_number"] for r in banded if r["ss_customer_id"] is not None}
    assert nulls & values


@pytest.mark.integration
def test_a_lower_bound_above_the_nulls_is_refused_by_plan_and_validate(tmp_path: Path) -> None:
    from flux_engine.stages.validate import validate

    lower = _DEPENDENCY + "          mustBeGreaterOrEqualTo: 40\n          unit: percent\n"
    error = _refusal(tmp_path, lower, "plan")
    assert error is not None and error.code.code == "FLX-6210"
    assert validate(_bundle(tmp_path, invariants=lower, name="validate")).exit_code != 0


@pytest.mark.integration
def test_a_band_the_nulls_exceed_is_refused_by_plan_and_validate_without_strict(
    tmp_path: Path,
) -> None:
    from flux_engine.stages.validate import validate

    tight = _DEPENDENCY + "          mustBeLessOrEqualTo: 1\n          unit: percent\n"
    error = _refusal(tmp_path, tight, "plan")
    assert error is not None and error.code.code == "FLX-6204"
    assert validate(_bundle(tmp_path, invariants=tight, name="validate")).exit_code != 0


_IDENTIFIER = (
    "        - { name: ss_ref, type: UUID, required: false }\n"
    "      invariants:\n"
    "        - id: ticket\n"
    "          dependency:\n"
    "            from: [ss_ticket_number]\n"
    "            to: [ss_ref]\n"
)


@pytest.mark.integration
def test_validate_refuses_a_to_column_with_nothing_to_draw_as_plan_does(tmp_path: Path) -> None:
    from flux_engine.stages.validate import validate

    # An identifier is a function of the row index, so it cannot be drawn once per key.
    error = _refusal(tmp_path, _IDENTIFIER, "plan-identifier")
    assert error is not None and error.code.code == "FLX-6210" and "identifier" in str(error)
    refused = validate(_bundle(tmp_path, invariants=_IDENTIFIER, name="validate-identifier"))
    assert refused.exit_code != 0 and "identifier" in refused.stderr
