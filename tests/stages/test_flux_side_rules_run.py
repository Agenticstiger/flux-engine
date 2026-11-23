# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose Simulation carries FLUX-side rules: the calendar's day, first day and quarter name follow its date.

The bundle of ``test_rules_run`` gains a day of the month and a quarter's name as FLUX-side rules
in flux-expr/3, and a month, a quarter and a first day of the month as contract rules, one of
which reads a FLUX-side column. The tests run it through the pipeline every command uses: every row
holds every rule, the plan and the coverage name where each rule came from, an id repeated across
two emits is refused, and `validate` and the init pre-flight refuse a FLUX-side rule `plan` refuses.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.stages import pipeline
from flux_engine.stages.validate import validate

from .test_rules_run import _SETTING, _bundle, calendar_rows

_COLUMNS = """        - { name: d_moy, type: INT, required: false }
        - { name: d_dom, type: INT, required: false }
        - { name: d_qoy, type: INT, required: false }
        - { name: d_first_dom, type: INT, required: false }
        - { name: d_quarter_name, type: STRING, required: false }
"""
_INVARIANTS = """        - id: month_of_date
          derive: d_moy
          expr: "MONTHS_BETWEEN(DATE '1900-01-01', d_date) - 12 * YEARS_BETWEEN(DATE '1900-01-01', d_date) + 1"
        - id: quarter
          derive: d_qoy
          expr: "ROUND((d_moy + 1) / 3, 0)"
        - id: first_day_of_month
          derive: d_first_dom
          expr: "d_date_sk - d_dom + 1"
"""
_GENERATES = _SETTING + """        invariants:
          date_of_key:
            generates: d_date
        rules:
          - id: day_of_month
            language: flux-expr/3
            derive: d_dom
            expr: "EXTRACT(DAY FROM d_date)"
          - id: quarter_name
            language: flux-expr/3
            derive: d_quarter_name
            expr: "d_year || 'Q' || d_qoy"
"""


def _calendar(tmp_path: Path, *, setting: str = _GENERATES, name: str = "bundle") -> Path:
    return _bundle(tmp_path, setting=setting, name=name, columns=_COLUMNS, invariants=_INVARIANTS)


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory) -> pipeline.PipelineResult:
    return pipeline.execute(_calendar(tmp_path_factory.mktemp("flux-side-run")))


def _date_dim(result: pipeline.PipelineResult) -> pipeline.EmitOutcome:
    return next(e for e in result.emits if e.resolved.expose_id == "date_dim")


@pytest.mark.integration
def test_the_rows_hold_the_flux_side_and_the_contract_rules(
    result: pipeline.PipelineResult,
) -> None:
    for row, day in calendar_rows(result):
        assert row["d_dom"] == day.day
        assert row["d_first_dom"] == row["d_date_sk"] - day.day + 1
        assert row["d_quarter_name"] == f"{day.year}Q{(day.month + 2) // 3}"


@pytest.mark.integration
def test_the_plan_and_the_coverage_say_where_each_rule_came_from(
    result: pipeline.PipelineResult,
) -> None:
    outcome = _date_dim(result)
    assert outcome.plan is not None
    rules = {rule["id"]: rule for rule in outcome.plan.as_document()["rules"]["rules"]}
    assert rules["day_of_month"]["origin"] == "bundle"
    assert rules["day_of_month"]["language"] == "flux-expr/3"
    assert "origin" not in rules["first_day_of_month"]
    coverage = {row.column: row for row in outcome.coverage.columns}
    assert coverage["d_dom"].source == "bundle.rules.day_of_month"
    assert coverage["d_quarter_name"].source == "bundle.rules.quarter_name"
    assert coverage["d_first_dom"].source == "contract.invariants.first_day_of_month"
    assert (coverage["d_dom"].origin.value, coverage["d_dom"].decided_by) == ("derived", "rule")


@pytest.mark.integration
def test_a_run_with_flux_side_rules_is_the_same_run_twice(tmp_path: Path) -> None:
    bundle = _calendar(tmp_path)
    first = pipeline.execute(bundle, strict_coverage=True)
    second = pipeline.execute(bundle, strict_coverage=True)
    assert _date_dim(first).rows == _date_dim(second).rows
    assert _date_dim(first).result == _date_dim(second).result


def _with_rules(rules: str) -> str:
    return (
        _SETTING
        + "        invariants:\n          date_of_key:\n            generates: d_date\n"
        + rules
    )


_ITEM_SETTING = "      item:\n"


@pytest.mark.integration
def test_an_id_repeated_across_two_emits_is_refused_before_any_row(tmp_path: Path) -> None:
    bundle = _calendar(tmp_path)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert text.count("      date_dim:\n") == 1
    # Another emit declares a rule under an id the date emit already uses.
    assert text.count(_ITEM_SETTING) == 1
    text = text.replace(
        _ITEM_SETTING,
        _ITEM_SETTING
        + "        rules:\n          - id: day_of_month\n            language: flux-expr/3\n"
        '            derive: i_item_sk\n            expr: "EXTRACT(DAY FROM i_item_sk)"\n',
        1,
    )
    simulation.write_text(text, "utf-8")
    with pytest.raises(FluxError, match="repeats") as caught:
        pipeline.execute(bundle)
    assert caught.value.code.code == "FLX-6201"
    refused = validate(bundle)
    assert refused.exit_code != 0 and "FLX-6201" in refused.stderr


@pytest.mark.integration
def test_validate_refuses_a_flux_side_rule_plan_refuses(tmp_path: Path) -> None:
    broken = _GENERATES.replace("language: flux-expr/3\n            derive: d_dom", "derive: d_dom")
    assert broken != _GENERATES
    refused = validate(_calendar(tmp_path, setting=broken))
    assert refused.exit_code != 0
    assert "FLX-6101" in refused.stderr
    assert validate(_calendar(tmp_path, name="good")).exit_code == 0


@pytest.mark.integration
def test_the_init_pre_flight_refuses_a_bad_flux_side_rule(tmp_path: Path) -> None:
    from flux_engine.author.bundle import AuthorError, _check
    from flux_engine.starter import StarterFile

    def files_of(bundle: Path) -> List[StarterFile]:
        return [
            StarterFile(path.name, "", path.read_bytes())
            for path in sorted(bundle.iterdir())
            if path.is_file()
        ]

    _check(files_of(_calendar(tmp_path, name="good")), {})
    broken = _GENERATES.replace("flux-expr/3", "flux-expr/9", 1)
    with pytest.raises(AuthorError, match="FLX-6102"):
        _check(files_of(_calendar(tmp_path, setting=broken, name="bad")), {})
