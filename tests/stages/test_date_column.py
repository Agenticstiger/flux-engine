# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A DATE column's bounds are spelled ``YYYY-MM-DD`` in the whole fidelity path.

The small telco bundle gains a DATE column, ``due_on``, whose range and bins are
spelled as calendar dates. Pinned here, each against that bundle:

* the block validates, the run generates, and a value outside the range is
  refused as it is for an INT (``FLX-5005``);
* a range spelled as integer days reads as the same range;
* a profile that holds the column as a timestamp in milliseconds (as the profile
  format does) scores a mean, a histogram and a quantile of the column, and a
  statistic that is not a whole number of days is refused (``FLX-5037``);
* the shape checks read the run's ``YYYY-MM-DD`` values against that profile.
"""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.concordance.boundary.statistic import compute_statistic
from flux_engine.concordance.compiler import CompiledFidelity, compile_fidelity
from flux_engine.concordance.predicate import ValueDomain, expose_from_contract
from flux_engine.concordance.shape import CHECKED, summarise_emit
from flux_engine.core.errors import FluxError
from flux_engine.stages.run import run
from flux_engine.stages.validate import validate
from flux_stats.profile import parse_profile
from tests.bundles import add_contract_columns
from tests.stats.profiles import profile_from_rows, statistic_of

from .conftest import FIXTURE_BUNDLE
from .test_scorecard_stages import EXPOSE, Pinned

DAY_MS = 86_400_000
EPOCH = date(1970, 1, 1).toordinal()
LOW, HIGH = "2026-01-01", "2026-12-31"
EDGES = ["2026-01-01", "2026-04-01", "2026-07-01", "2026-12-31"]


def _properties(edges: List[str]) -> str:
    spelled = ", ".join(edges)
    return (
        "            - property: mean\n"
        "              column: due_on\n"
        '              tolerance: { type: absolute, value: "30" }\n'
        "            - property: marginalHistogram\n"
        "              column: due_on\n"
        f"              bins: [{spelled}]\n"
        '              tolerance: { type: distance, measure: totalVariation, max: "0.5" }\n'
        "            - property: quantile\n"
        "              column: due_on\n"
        '              level: "0.5"\n'
        '              tolerance: { type: absolute, value: "30" }\n'
        "            - property: quantile\n"
        "              column: due_on\n"
        '              level: "0.5"\n'
        f"              bins: [{spelled}]\n"
        '              tolerance: { type: absolute, value: "30" }\n'
    )


def days(text: str) -> int:
    year, month, day = (int(part) for part in text.split("-"))
    return date(year, month, day).toordinal() - EPOCH


def _bundle(root: Path, low: str, high: str, edges: List[str]) -> Path:
    """The small telco bundle with a DATE column ``due_on`` ranged ``low`` to ``high``."""
    bundle = root / "bundle"
    shutil.copytree(FIXTURE_BUNDLE, bundle)
    (bundle / "flux-manifest.json").unlink(missing_ok=True)
    add_contract_columns(bundle, [("due_on", "DATE", False)])
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    column_anchor = "            aborted_at:\n"
    property_anchor = "          budget:"
    assert text.count(column_anchor) == 1 and text.count(property_anchor) == 1
    declared = (
        "            due_on:\n"
        f"              range: {{ min: {low}, max: {high} }}\n"
        '              binWeights: ["1", "2", "1"]\n'
        "              null: exclude\n"
    )
    simulation.write_text(
        text.replace(column_anchor, declared + column_anchor).replace(
            property_anchor, _properties(edges) + property_anchor
        ),
        encoding="utf-8",
    )
    return bundle


@pytest.fixture(scope="module")
def dated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(
        tmp_path_factory.mktemp("dated"), f'"{LOW}"', f'"{HIGH}"', [f'"{edge}"' for edge in EDGES]
    )


@pytest.fixture(scope="module")
def numbered(tmp_path_factory: pytest.TempPathFactory) -> Path:
    edges = [str(days(edge)) for edge in EDGES]
    return _bundle(tmp_path_factory.mktemp("numbered"), str(days(LOW)), str(days(HIGH)), edges)


def _compiled(run_dir: Path) -> Tuple[CompiledFidelity, List[Dict[str, Any]]]:
    fidelity_input = json.loads(
        (run_dir / f"fidelity-input-{EXPOSE}.json").read_text(encoding="utf-8")
    )
    contract = fidelity_input["contract"]
    compiled = compile_fidelity(
        contract=contract,
        emit=fidelity_input["emit"],
        expose=expose_from_contract(contract, EXPOSE),
    )
    attestation = json.loads(
        (run_dir / f"seam-attestation-{EXPOSE}.json").read_text(encoding="utf-8")
    )
    lines = (run_dir / "emits" / f"{EXPOSE}.ndjson").read_text(encoding="utf-8").splitlines()
    rows = [
        dict(zip(attestation["columns"], json.loads(line), strict=True)) for line in lines if line
    ]
    return compiled, rows


@pytest.fixture(scope="module")
def generated(dated: Path, tmp_path_factory: pytest.TempPathFactory) -> Tuple[Path, Any, Any]:
    state = tmp_path_factory.mktemp("state")
    result = run(dated, state_dir=state)
    assert result.success, result.stderr
    run_dir = state / "runs" / result.artifacts["plan"]["run_id"]
    compiled, rows = _compiled(run_dir)
    return run_dir, compiled, rows


def test_a_date_range_spelled_as_dates_validates_and_generates(
    dated: Path, generated: Tuple[Path, Any, Any]
) -> None:
    checked = validate(dated)
    assert checked.success, checked.stderr
    _, compiled, rows = generated
    column = compiled.block.columns["due_on"]
    assert column.domain is ValueDomain.DATE
    assert (column.lower, column.upper) == (days(LOW), days(HIGH))
    spelled = [row["due_on"] for row in rows if row["due_on"] is not None]
    assert spelled and all(LOW <= value <= HIGH for value in spelled)


def test_a_range_spelled_as_integer_days_is_the_same_range(numbered: Path, tmp_path: Path) -> None:
    result = run(numbered, state_dir=tmp_path / "state")
    assert result.success, result.stderr
    compiled, _ = _compiled(tmp_path / "state" / "runs" / result.artifacts["plan"]["run_id"])
    column = compiled.block.columns["due_on"]
    assert column.domain is ValueDomain.DATE
    assert (column.lower, column.upper) == (days(LOW), days(HIGH))
    assert compiled.block.columns["due_on"].interval.contains(days("2026-06-30"))


def test_a_value_outside_the_range_is_refused_as_for_an_int(
    generated: Tuple[Path, Any, Any],
) -> None:
    _, compiled, rows = generated
    compute_statistic(rows, compiled)
    for outside in ("2025-12-31", "2027-01-01"):
        moved = [dict(row) for row in rows]
        moved[0]["due_on"] = outside
        with pytest.raises(FluxError) as refused:
            compute_statistic(moved, compiled)
        assert refused.value.code.code == "FLX-5005"
        assert outside not in str(refused.value.detail), "a refusal never quotes the value"


def test_a_value_that_is_no_date_is_refused(generated: Tuple[Path, Any, Any]) -> None:
    _, compiled, rows = generated
    moved = [dict(row) for row in rows]
    moved[0]["due_on"] = "2026-02-30"
    with pytest.raises(FluxError) as refused:
        compute_statistic(moved, compiled)
    assert refused.value.code.code == "FLX-5005"


def _profiled(compiled: CompiledFidelity, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A profile of *rows* as a profiler writes a DATE column: a timestamp in milliseconds.

    The statistics are formed in days by the engine's own definitions, then
    written out as a profiler that reads the column as instants writes them:
    each sum and order statistic times a day's milliseconds, each edge in
    milliseconds. The profile format is unchanged; only its numbers differ.
    """
    document = profile_from_rows(rows, compiled)
    column = document["tables"][EXPOSE]["columns"]["due_on"]
    column["domain"] = "timestamp"
    for statistic in column["statistics"]:
        if "bins" in statistic:
            statistic["bins"] = [str(int(edge) * DAY_MS) for edge in statistic["bins"]]
        for group in statistic["groups"]:
            if "Sx" in group:
                group["Sx"] = str(int(group["Sx"]) * DAY_MS)
            if group.get("quantileWitness") is not None:
                group["quantileWitness"] = str(int(group["quantileWitness"]) * DAY_MS)
    held = sorted(row["due_on"] for row in rows if row["due_on"] is not None)
    column["metrics"] = {
        name: {
            "definition": f"{name}/1",
            "status": "F",
            "value": str(days(value) * DAY_MS),
            "sampleSize": {"min": len(rows), "max": len(rows)},
            "disclosure": "none",
        }
        for name, value in (("min", held[0]), ("max", held[-1]))
    }
    return document


def test_the_profile_holds_the_column_as_milliseconds(
    generated: Tuple[Path, Any, Any],
) -> None:
    _, compiled, rows = generated
    document = _profiled(compiled, rows)
    kinds = {statistic["definition"] for statistic in statistic_of_all(document)}
    assert kinds == {"mean/1", "marginalHistogram/1", "quantile/1", "quantile/2"}
    assert statistic_of(document, EXPOSE, "due_on", "marginalHistogram/1")["bins"][0] == str(
        days(LOW) * DAY_MS
    )


def statistic_of_all(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    return document["tables"][EXPOSE]["columns"]["due_on"]["statistics"]


def test_a_profile_in_milliseconds_scores_a_date_column(
    tmp_path: Path, dated: Path, generated: Tuple[Path, Any, Any]
) -> None:
    _, compiled, rows = generated
    # The run is the rows the profile was formed over, so every property is within.
    pinned = Pinned(tmp_path, _profiled(compiled, rows), source=dated)
    verified = pinned.verify(profile=pinned.profile)
    assert verified.success, verified.stderr
    document = verified.artifacts["scorecard"]["document"]
    (table,) = [table for table in document["tables"] if table["exposeId"] == EXPOSE]
    scored = {
        entry["statistic"]: entry["outcome"]
        for entry in table["properties"]
        if "due_on" in entry["propertyId"]
    }
    assert scored == {
        "mean/1": "within",
        "marginalHistogram/1": "within",
        "quantile/1": "within",
        "quantile/2": "within",
    }
    assert "FLX-5037" not in verified.stdout + verified.stderr


def test_a_statistic_that_is_not_a_whole_day_is_refused(
    tmp_path: Path, dated: Path, generated: Tuple[Path, Any, Any]
) -> None:
    _, compiled, rows = generated
    document = _profiled(compiled, rows)
    (group,) = statistic_of(document, EXPOSE, "due_on", "mean/1")["groups"]
    group["Sx"] = str(int(group["Sx"]) + 1)
    pinned = Pinned(tmp_path, document, source=dated)
    verified = pinned.verify(profile=pinned.profile)
    assert verified.exit_code != 0
    refusals = verified.artifacts["fidelity"][EXPOSE]
    assert "FLX-5037" in json.dumps(refusals)
    assert "whole number of days" in json.dumps(refusals)


def test_the_shape_checks_read_dates_against_a_profile_in_milliseconds(
    generated: Tuple[Path, Any, Any],
) -> None:
    _, compiled, rows = generated
    profile = parse_profile(_profiled(compiled, rows))
    summary = summarise_emit(profile, EXPOSE, rows)
    (column,) = [column for column in summary.columns if column.column == "due_on"]
    assert column.status == CHECKED, (column.code, column.reason)
    assert column.outside == 0 and sum(column.cell_counts) == column.records
