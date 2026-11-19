# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose contract carries rules: the calendar's date and year follow its key.

``tests/fixtures/related-star`` is copied, its ``date_dim`` gains a ``d_date`` column and two
invariants, and the Simulation names the column the difference-form ``check`` generates. The
tests run it through the pipeline every command uses and check what a run hands on: the plan
records the rules, coverage names them, every row holds them, and a setting that names no
invariant is refused before a row is drawn.
"""

from __future__ import annotations

import calendar
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.stages import pipeline
from flux_engine.stages.plan import build_plan_document
from tests.bundles import repin_contract

from .conftest import REPO_ROOT

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"
_CONTRACT = "store-star.fluid.yml"
_FIRST_DAY = 2415021  # the key whose date is 1900-01-01

_COLUMN = "        - { name: d_year, type: INT, required: true }\n"
_INVARIANTS = f"""        - {{ name: d_date, type: DATE, required: true }}
      invariants:
        - id: date_of_key
          check: "d_date_sk - {_FIRST_DAY} = DAYS_BETWEEN(DATE '1900-01-01', d_date)"
        - id: year_of_date
          derive: d_year
          expr: "YEARS_BETWEEN(DATE '1900-01-01', d_date) + 1900"
"""
_SETTING = "      date_dim:\n        rows: 365\n"
_GENERATES = (
    _SETTING + "        invariants:\n          date_of_key:\n            generates: d_date\n"
)


def _bundle(
    tmp_path: Path,
    *,
    setting: str = _GENERATES,
    name: str = "bundle",
    columns: str = "",
    invariants: str = "",
) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    contract = target / _CONTRACT
    text = contract.read_text("utf-8")
    assert text.count(_COLUMN) == 1
    contract.write_text(
        text.replace(_COLUMN, _COLUMN + columns + _INVARIANTS + invariants, 1), "utf-8"
    )
    simulation = target / "simulation.flux.yml"
    repin_contract(contract, simulation, "retail.store_star")
    text = simulation.read_text("utf-8")
    assert text.count(_SETTING) == 1
    simulation.write_text(text.replace(_SETTING, setting, 1), "utf-8")
    return target


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    return pipeline.execute(_bundle(tmp_path_factory.mktemp("rules-run")))


def _date_dim(result: pipeline.PipelineResult) -> pipeline.EmitOutcome:
    return next(e for e in result.emits if e.resolved.expose_id == "date_dim")


def calendar_rows(result: pipeline.PipelineResult) -> List[Tuple[Dict[str, Any], date]]:
    """Every row of the run's ``date_dim`` with the date its key names."""
    outcome = _date_dim(result)
    names = list(outcome.expose.column_names)
    rows = [dict(zip(names, row, strict=True)) for row in outcome.rows]
    return [(row, date(1900, 1, 1) + timedelta(days=row["d_date_sk"] - _FIRST_DAY)) for row in rows]


@pytest.mark.integration
def test_the_calendar_follows_its_key(result: pipeline.PipelineResult) -> None:
    rows = calendar_rows(result)
    assert len(rows) == 365
    for row, day in rows:
        assert row["d_date"] == day
        assert row["d_year"] == day.year
    assert len({row["d_date"] for row, _ in rows}) == 365


@pytest.mark.integration
def test_a_contract_derive_moves_a_date_by_days_months_and_years(tmp_path: Path) -> None:
    columns = (
        "        - { name: due_on, type: DATE, required: true }\n"
        "        - { name: renews_on, type: DATE, required: true }\n"
        "        - { name: expires_on, type: DATE, required: true }\n"
    )
    invariants = """        - id: due_of_date
          derive: due_on
          expr: "ADD_DAYS(d_date, 30)"
        - id: renewal_of_date
          derive: renews_on
          expr: "ADD_MONTHS(d_date, 12)"
        - id: expiry_of_date
          derive: expires_on
          expr: "ADD_YEARS(d_date, 1)"
"""
    outcome = _date_dim(pipeline.execute(_bundle(tmp_path, columns=columns, invariants=invariants)))
    names = list(outcome.expose.column_names)
    rows = [dict(zip(names, row, strict=True)) for row in outcome.rows]
    assert len(rows) == 365
    for row in rows:
        start = row["d_date"]
        assert row["due_on"] == start + timedelta(days=30)
        # The same day a year on, or the month's last day when the year has no such day.
        year = start.year + 1
        day = min(start.day, calendar.monthrange(year, start.month)[1])
        assert row["renews_on"] == date(year, start.month, day)
        assert row["expires_on"] == date(year, start.month, day)


@pytest.mark.integration
def test_the_plan_and_the_coverage_name_the_rules(result: pipeline.PipelineResult) -> None:
    outcome = _date_dim(result)
    assert outcome.plan is not None
    document = outcome.plan.as_document()["rules"]
    assert [rule["id"] for rule in document["rules"]] == ["date_of_key", "year_of_date"]
    assert [rule["order"] for rule in document["rules"]] == [0, 1]
    coverage = {row.column: row for row in outcome.coverage.columns}
    assert (coverage["d_date"].origin.value, coverage["d_date"].decided_by) == ("derived", "rule")
    assert coverage["d_date"].source == "contract.invariants.date_of_key"
    assert coverage["d_year"].source == "contract.invariants.year_of_date"
    assert coverage["d_date_sk"].decided_by == "key"
    plan_document = build_plan_document(result)
    assert plan_document  # the plan document renders with the new coverage pair
    other = next(e for e in result.emits if e.resolved.expose_id == "item")
    assert other.plan is not None and "rules" not in other.plan.as_document()


@pytest.mark.integration
def test_a_run_with_rules_is_the_same_run_twice_and_strict_coverage_passes(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    first = pipeline.execute(bundle, strict_coverage=True)
    second = pipeline.execute(bundle, strict_coverage=True)
    assert _date_dim(first).rows == _date_dim(second).rows
    assert _date_dim(first).result == _date_dim(second).result


@pytest.mark.integration
def test_a_setting_that_names_no_invariant_is_refused_before_any_row(tmp_path: Path) -> None:
    setting = _SETTING + "        invariants:\n          nope:\n            generates: d_date\n"
    with pytest.raises(FluxError, match="names no invariant") as caught:
        pipeline.execute(_bundle(tmp_path, setting=setting))
    assert caught.value.code.code == "FLX-3105"


@pytest.mark.integration
def test_without_the_setting_the_check_generates_nothing(tmp_path: Path) -> None:
    outcome = _date_dim(pipeline.execute(_bundle(tmp_path, setting=_SETTING)))
    coverage = {row.column: row for row in outcome.coverage.columns}
    assert coverage["d_date"].decided_by == "time-bounds"
    assert coverage["d_year"].decided_by == "rule"


@pytest.mark.integration
def test_a_setting_that_names_a_column_the_difference_does_not_has_no_rule(tmp_path: Path) -> None:
    setting = (
        _SETTING + "        invariants:\n          date_of_key:\n            generates: d_date_sk\n"
    )
    with pytest.raises(FluxError) as caught:
        pipeline.execute(_bundle(tmp_path, setting=setting))
    assert caught.value.code.code == "FLX-6202"


@pytest.mark.integration
def test_validate_refuses_a_rule_plan_refuses(tmp_path: Path) -> None:
    # `fluxsim validate` plans the contract's rules as `plan` does, so a bundle it passes
    # is one `plan` will not refuse for its rules.
    from flux_engine.stages.validate import validate

    setting = (
        _SETTING + "        invariants:\n          date_of_key:\n            generates: d_date_sk\n"
    )
    refused = validate(_bundle(tmp_path, setting=setting))
    assert refused.exit_code != 0
    assert "FLX-6202" in refused.stderr
    assert validate(_bundle(tmp_path, name="good")).exit_code == 0
