# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The scorecard's ``queries`` dimension, format 1-4-0 (ADR 0028 section 7, SCORECARD-SPEC.md section 3a).

* A scorecard with the dimension names 1-4-0, validates against that schema, and scores the dimension the
  smaller of the two shares that were measured.
* A scorecard without it keeps the bytes it had: the digest below was computed with the code before the
  dimension existed.
* Without the extra the dimension says so and gives the install command, and a threshold declared on it is
  not met.
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from jsonschema import Draft202012Validator

from flux_engine.concordance.queries import (
    CALIBRATION,
    COUNTED,
    HELD_OUT,
    INSTALL_COMMAND,
    NOT_COUNTED,
    GroupRow,
    QueriesDimension,
    QueryRow,
    QueryVersions,
    RequirementRow,
)
from flux_engine.concordance.scorecard import SCHEMA_URL, SCHEMA_URL_1_4_0, build_scorecard
from flux_engine.core.errors import FluxError
from flux_engine.seam.resolve import FLX_3105
from flux_engine.seam.scorecard import (
    QUERY_SCORED_DIMENSIONS,
    SCORED_DIMENSIONS,
    DeclaredProperty,
    ScorecardDeclaration,
    Threshold,
    read_scorecard_block,
)

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
SCHEMA_1_4 = json.loads((SPEC / "scorecard-1-4.schema.json").read_text(encoding="utf-8"))
DIGEST = "sha256:" + "a" * 64

#: The sha256 of a scorecard of an empty run, formed by the code before the ``queries`` dimension
#: existed. A scorecard that holds no queries member must keep writing these bytes.
EMPTY_SCORECARD_SHA256 = "0301d065adf34907765f6f5fb003a28d0c93c2df016e2a36b32ab5da441d5677"


def _row(
    name: str,
    which: str,
    *,
    rate: Optional[Fraction] = Fraction(3, 20),
    target: Optional[Fraction] = Fraction(3, 20),
    tolerance: Optional[Fraction] = Fraction(1, 50),
    status: str = "measured",
    met: Optional[bool] = True,
    **more: Any,
) -> QueryRow:
    gap = rate - target if rate is not None and target is not None else None
    return QueryRow(
        name,
        which,
        DIGEST,
        more.pop("rows", 180),
        more.pop("unit_rows", 1200),
        rate,
        target,
        gap,
        tolerance,
        status,
        met,
        **more,
    )


def _dimension(**kwargs: Any) -> QueriesDimension:
    group = GroupRow(0, False, 5, 10, Fraction(1, 2), Fraction(1, 2), Fraction(0), "measured", True)
    rows = (
        _row("a", CALIBRATION),
        _row("b", CALIBRATION, rate=Fraction(1, 5), met=False),
        _row("c", CALIBRATION, target=None, status="noTarget", met=None, groups=(group,)),
        _row("d", HELD_OUT, met=True),
        _row(
            "e",
            CALIBRATION,
            rows=None,
            unit_rows=None,
            rate=None,
            target=None,
            tolerance=None,
            status="timedOut",
            met=None,
            reason="the query ran past its seconds",
        ),
    )
    base: Dict[str, Any] = {
        "queries": rows,
        "requirements": (
            RequirementRow("a", "subscriptions", "plan", "level", "the level 'premium'", "met"),
            RequirementRow("d", "subscriptions", "renewed_at", "range", "the range", "unmet"),
        ),
        "versions": QueryVersions("2.0.1", "30.33.0", "21.0.0"),
        "pinned_grade": "P1",
        "held_out_grade": "P1",
    }
    base.update(kwargs)
    return QueriesDimension(**base)


def _scorecard(queries: Optional[QueriesDimension], declaration: Any = None) -> Any:
    return build_scorecard(
        run_id="0123456789abcdef",
        plan_digest="sha256:" + "1" * 64,
        run_digest="sha256:" + "2" * 64,
        profile_digest="sha256:" + "3" * 64,
        tables=[],
        declaration=declaration,
        queries=queries,
    )


def test_a_scorecard_with_no_queries_keeps_its_bytes() -> None:
    result = _scorecard(None)
    assert hashlib.sha256(result.to_bytes()).hexdigest() == EMPTY_SCORECARD_SHA256
    assert result.to_document()["_schemaURL"] == SCHEMA_URL
    assert all("queries" not in entry for entry in result.to_document()["dimensions"])


def test_the_dimension_scores_the_smaller_of_the_shares_that_were_measured() -> None:
    dimension = _dimension()
    assert dimension.share_of(CALIBRATION).covered == 1
    assert (
        dimension.share_of(CALIBRATION).of == 2
    ), "a query with no target or no tolerance is in no share"
    assert dimension.share_of(HELD_OUT).share == 1
    assert dimension.score == Fraction(1, 2)
    only_held_out = _dimension(queries=(_row("d", HELD_OUT),))
    assert only_held_out.score == 1 and only_held_out.share_of(CALIBRATION).share is None
    none_compared = _dimension(queries=(_row("c", CALIBRATION, met=None, status="noTarget"),))
    assert none_compared.score is None


def test_the_dimension_names_format_1_4_0_and_validates_against_its_schema() -> None:
    result = _scorecard(_dimension())
    document = result.to_document()
    assert document["_schemaURL"] == SCHEMA_URL_1_4_0 == SCHEMA_1_4["$id"]
    Draft202012Validator(SCHEMA_1_4).validate(document)
    entry = {item["dimension"]: item for item in document["dimensions"]}["queries"]
    assert (entry["status"], entry["score"], entry["weight"]) == ("measured", "0.5", "1")
    assert entry["counts"] == {"properties": 5, "scored": 3, "censored": 1, "refused": 1}
    member = entry["queries"]
    assert member["status"] == COUNTED
    assert member["shares"] == {
        "calibration": {"covered": 1, "of": 2, "share": "0.5"},
        "heldOut": {"covered": 1, "of": 1, "share": "1"},
    }
    assert member["grades"] == {"pinned": "P1", "heldOut": "P1"}
    assert member["versions"] == {"duckdb": "2.0.1", "sqlglot": "30.33.0", "pyarrow": "21.0.0"}
    by_name = {row["name"]: row for row in member["queries"]}
    assert by_name["b"]["gap"] == "0.05" and by_name["b"]["met"] is False
    assert (
        by_name["e"]["reason"] == "the query ran past its seconds" and by_name["e"]["met"] is None
    )
    assert by_name["c"]["groups"][0]["nullGroup"] is False
    assert "groups" not in by_name["a"]
    # The overall score takes the dimension as it takes any measured one.
    assert document["overall"]["score"] == "0.5"
    # Every other dimension is untouched.
    assert all(
        "queries" not in item for item in document["dimensions"] if item["dimension"] != "queries"
    )


def test_a_negative_gap_is_written_signed_and_exact() -> None:
    row = _row("a", CALIBRATION, rate=Fraction(1, 10), target=Fraction(3, 20), met=False)
    result = _scorecard(_dimension(queries=(row,)))
    Draft202012Validator(SCHEMA_1_4).validate(result.to_document())
    assert row.to_document()["gap"] == "-0.05"


def test_without_the_extra_the_dimension_says_so_and_names_the_install_command() -> None:
    dimension = QueriesDimension(status=NOT_COUNTED, detail="consumer queries need duckdb")
    result = _scorecard(dimension)
    document = result.to_document()
    Draft202012Validator(SCHEMA_1_4).validate(document)
    entry = {item["dimension"]: item for item in document["dimensions"]}["queries"]
    assert (entry["status"], entry["score"]) == ("not measured", None)
    assert entry["queries"] == {
        "status": "not counted",
        "detail": "consumer queries need duckdb",
        "install": INSTALL_COMMAND,
    }
    assert INSTALL_COMMAND == "pip install 'flux-sim-engine[sql]'"
    assert any(INSTALL_COMMAND in line for line in result.detail_lines())


def test_a_threshold_declared_on_the_dimension_is_not_met_when_nothing_was_counted() -> None:
    threshold = Threshold("dimension", "queries", "mustBeGreaterOrEqualTo", Fraction(1, 2), "0.5")
    declaration = ScorecardDeclaration(thresholds=(threshold,))
    unmeasured = _scorecard(QueriesDimension(status=NOT_COUNTED, detail="x"), declaration)
    assert unmeasured.thresholds[0].met is False and not unmeasured.met
    assert unmeasured.failure_lines(), "the stage fails, as for any unmet threshold"
    held = _scorecard(_dimension(), declaration)
    assert held.thresholds[0].score == Fraction(1, 2) and held.thresholds[0].met and held.met
    missed = _scorecard(
        _dimension(),
        ScorecardDeclaration(
            (Threshold("dimension", "queries", "mustBeGreaterThan", Fraction(1, 2), "0.5"),)
        ),
    )
    assert not missed.met


def test_a_simulation_may_declare_a_threshold_on_the_queries_dimension() -> None:
    assert "queries" in QUERY_SCORED_DIMENSIONS and "queries" in SCORED_DIMENSIONS
    block = {"thresholds": {"dimensions": {"queries": {"mustBeGreaterOrEqualTo": "0.9"}}}}
    emits: Dict[str, Optional[Any]] = {"plain": None}
    declared = read_scorecard_block(block, pinned=True, emits=emits, where="sim", code=FLX_3105)
    assert [t.target for t in declared.thresholds] == ["dimensions.queries"]
    # The dimension no property kind scores is still refused, as it was. ``temporal`` is scored
    # from the calendars a profile publishes, so a threshold on it is accepted too.
    for dimension in ("defects",):
        with pytest.raises(FluxError, match="no property kind of this engine"):
            read_scorecard_block(
                {"thresholds": {"dimensions": {dimension: {"mustBeGreaterOrEqualTo": "0.9"}}}},
                pinned=True,
                emits={"plain": (DeclaredProperty(kind="mean", grouped=False),)},
                where="sim",
                code=FLX_3105,
            )


def test_an_unmet_requirement_is_printed_without_a_control_character() -> None:
    hostile = RequirementRow(
        "d\n", "subscriptions", "renewed_at", "range", "the range '>=5\r\x1b[2J'", "unmet"
    )
    lines = _dimension(requirements=(hostile,)).lines()
    line = next(each for each in lines if "requirement of" in each)
    assert line.isprintable() and "\r" not in line and "\x1b" not in line and "\n" not in line
    assert "'d\\n'" in line


def test_the_lines_say_the_shares_the_grades_and_each_query() -> None:
    lines = _dimension().lines()
    assert lines[0].startswith(
        "queries: calibration 1 of 2 within tolerance, held-out 1 of 1 within tolerance; "
        "requirements 1 of 2 met; profile grade P1, held-out profile grade P1"
    )
    assert any(
        line.strip().startswith("query b (calibration): rate 0.2") and line.endswith("outside")
        for line in lines
    )
    assert any(
        line.strip() == "query e (calibration): timedOut (the query ran past its seconds)"
        for line in lines
    )
    assert any(
        "requirement of d: subscriptions.renewed_at needs the range: unmet" in line
        for line in lines
    )
