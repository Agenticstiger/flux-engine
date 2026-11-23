# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A date column's calendar against the profile's, on known values (ADR 0025 section 8).

The counts are by month, ISO day of week and hour of week in UTC; the comparison is the one a
``levelHistogram/1`` column statistic gets against a profile, over the published cells, with the
tolerance a distance is written with.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest
from jsonschema import Draft202012Validator

from flux_engine.author.columns import DEFAULT_TOLERANCES
from flux_engine.concordance.calendar_lines import (
    CALENDAR_TOLERANCE,
    NO_HOUR_REASON,
    calendar_counts,
    calendar_for_emit,
    calendar_rows,
    hidden_in_plan,
    hidden_not_scored,
)
from flux_engine.concordance.decoded import DecodedRows
from flux_engine.concordance.scorecard import (
    SCHEMA_URL,
    SCHEMA_URL_1_4_0,
    TableInput,
    build_scorecard,
)
from flux_engine.concordance.scorecard import (
    SCHEMA_URL_1_3_0 as SCORECARD_URL_1_3_0,
)
from flux_engine.core.errors import FluxError
from flux_engine.seam.scorecard import ScorecardDeclaration, Threshold
from flux_stats.profile import CALENDAR_LEVELS, CalendarEntry, parse_profile
from flux_stats.profile import SCHEMA_URL_1_3_0 as PROFILE_URL_1_3_0
from flux_stats.spelling import parse_exact, render_exact

pytestmark = pytest.mark.unit

Row = Dict[str, Any]

#: Every cell of a row's week of 2024-02-26 (a Monday) to 2024-03-03 (a Sunday), and a leap day.
LEAP_DAY = "2024-02-29T12:30:00.000Z"  # a Thursday
SUNDAY_LAST_MS = "2024-03-03T23:59:59.999Z"  # the last millisecond of the ISO week
MONDAY_FIRST = "2024-02-26T00:00:00.000Z"  # the first hour of the ISO week


def _rows(column: str, cells: Sequence[Any]) -> List[Row]:
    return [{column: cell} for cell in cells]


def _decoded(column: str, cells: Sequence[Any]) -> DecodedRows:
    return DecodedRows([column], [[cell] for cell in cells])


def _hist(
    key: str, counts: Sequence[Optional[int]], cell_status: Optional[List[str]] = None
) -> Any:
    group: Dict[str, Any] = {
        "group": "",
        "k": sum(count or 0 for count in counts),
        "levelCounts": list(counts),
        "status": "F",
    }
    if cell_status is not None:
        group["cellStatus"] = cell_status
    return {
        "definition": "levelHistogram/1",
        "levels": list(CALENDAR_LEVELS[key]),
        "sampleSize": {"min": 1, "max": 10**9},
        "disclosure": "none",
        "groups": [group],
    }


def _entry(column: str = "at", **histograms: Any) -> CalendarEntry:
    """A calendar entry read through the profile reader, so it is the object ``verify`` is handed."""
    document = {
        "_producer": "urn:example:test",
        "_schemaURL": PROFILE_URL_1_3_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "t": {
                "columns": {column: {"domain": "timestamp"}},
                "calendar": [{"column": column, **histograms}],
            }
        },
    }
    return parse_profile(document).tables["t"].calendar[0]


def _unit(counts: Sequence[int], at: int) -> List[int]:
    out = list(counts)
    out[at] += 1
    return out


def test_the_tolerance_is_the_one_a_distance_is_written_with() -> None:
    assert CALENDAR_TOLERANCE == parse_exact(DEFAULT_TOLERANCES["distance"])


@pytest.mark.parametrize("make", [_rows, _decoded], ids=["per-row", "decoded"])
def test_a_timestamp_is_counted_by_month_day_of_week_and_hour_of_week_in_utc(make: Any) -> None:
    counts = calendar_counts(
        make("at", [LEAP_DAY, SUNDAY_LAST_MS, MONDAY_FIRST, None]), "at", "TIMESTAMP"
    )
    month = [0] * 12
    month[1] += 2  # the leap day and the Monday, both February
    month[2] += 1  # the Sunday is March 3
    assert counts["month"] == tuple(month)
    weekday = [0] * 7
    weekday[3] += 1  # Thursday
    weekday[6] += 1  # Sunday
    weekday[0] += 1  # Monday
    assert counts["dayOfWeek"] == tuple(weekday)
    hour = [0] * 168
    hour[3 * 24 + 12] += 1  # Thursday 12:00
    hour[167] += 1  # Sunday 23:00 is the last hour of the week
    hour[0] += 1  # Monday 00:00 is hour 000
    assert counts["hourOfWeek"] == tuple(hour)
    assert sum(counts["hourOfWeek"]) == 3  # the NULL is not counted


@pytest.mark.parametrize("make", [_rows, _decoded], ids=["per-row", "decoded"])
def test_a_date_is_its_own_day_and_has_no_hour(make: Any) -> None:
    counts = calendar_counts(
        make("on", ["2024-02-29", "2024-03-03", "2024-02-26", "1969-12-31", None]), "on", "DATE"
    )
    assert "hourOfWeek" not in counts
    month = [0] * 12
    month[1] += 2
    month[2] += 1
    month[11] += 1  # 1969-12-31
    assert counts["month"] == tuple(month)
    weekday = [0] * 7
    weekday[3] += 1  # 2024-02-29, a Thursday
    weekday[6] += 1  # 2024-03-03, a Sunday
    weekday[0] += 1  # 2024-02-26, a Monday
    weekday[2] += 1  # 1969-12-31, a Wednesday
    assert counts["dayOfWeek"] == tuple(weekday)


def test_the_column_path_equals_the_per_row_path() -> None:
    cells = [
        f"2023-{1 + i % 12:02d}-{1 + i % 28:02d}T{i % 24:02d}:{i % 60:02d}:00.000Z"
        for i in range(500)
    ]
    assert calendar_counts(_decoded("at", cells), "at", "TIMESTAMP") == calendar_counts(
        _rows("at", cells), "at", "TIMESTAMP"
    )
    days = [f"2022-{1 + i % 12:02d}-{1 + i % 28:02d}" for i in range(500)]
    assert calendar_counts(_decoded("on", days), "on", "DATE") == calendar_counts(
        _rows("on", days), "on", "DATE"
    )


def test_a_cell_that_is_no_instant_is_refused_naming_its_row_and_not_the_value() -> None:
    with pytest.raises(FluxError) as refused:
        calendar_counts(_rows("at", [LEAP_DAY, "not-an-instant"]), "at", "TIMESTAMP")
    assert "row 1" in refused.value.detail
    assert "not-an-instant" not in refused.value.detail


def _run_of(cells: Sequence[str]) -> DecodedRows:
    return _decoded("at", cells)


def _matching() -> CalendarEntry:
    cells = [LEAP_DAY, SUNDAY_LAST_MS, MONDAY_FIRST]
    counts = calendar_counts(_rows("at", cells), "at", "TIMESTAMP")
    return _entry(
        month=_hist("month", counts["month"]),
        dayOfWeek=_hist("dayOfWeek", counts["dayOfWeek"]),
        hourOfWeek=_hist("hourOfWeek", counts["hourOfWeek"]),
    )


def test_a_matching_run_passes_with_each_distance_and_tolerance_printed() -> None:
    cells = [LEAP_DAY, SUNDAY_LAST_MS, MONDAY_FIRST]
    found = calendar_for_emit("t", [_matching()], _run_of(cells), {"at": "TIMESTAMP"})
    (line,) = found.lines
    assert line.within and not found.failures() and not found.not_scored
    assert line.line() == (
        "calendar:at (t): passed -- month distance 0 (tolerance 0.1); "
        "dayOfWeek distance 0 (tolerance 0.1); hourOfWeek distance 0 (tolerance 0.1)"
    )
    assert found.artifact()["calendar"]["met"] is True


def test_a_skewed_run_fails_and_prints_the_distance_that_missed() -> None:
    # The run is all one month; the profile spread its mass over every month evenly.
    cells = [LEAP_DAY] * 12
    entry = _entry(month=_hist("month", [1] * 12))
    found = calendar_for_emit("t", [entry], _run_of(cells), {"at": "TIMESTAMP"})
    (line,) = found.lines
    assert not line.within
    # 11 of the 12 equal shares are missing from the run: half of 11/12 + 11/12 = 11/12.
    (part,) = line.parts
    assert part.distance == Fraction(11, 12)
    assert part.tolerance == CALENDAR_TOLERANCE
    assert found.failures() == [
        "calendar:at (t): failed -- month distance 0.9166 (tolerance 0.1) (outside)"
    ]
    document = found.artifact()["calendar"]
    assert document["met"] is False
    assert document["lines"][0]["histograms"][0]["distance"] == "11/12"


def test_a_line_gates_only_for_a_column_a_rates_entry_shapes() -> None:
    cells = [LEAP_DAY] * 12
    entry = _entry(month=_hist("month", [1] * 12))
    found = calendar_for_emit("t", [entry], _run_of(cells), {"at": "TIMESTAMP"})
    shaped = found.gating({"t": frozenset({"at"})})
    assert [line.gated for line in shaped.lines] == [True]
    assert shaped.failures() == found.failures() != []
    for unshaped in ({}, {"t": frozenset({"other"})}, {"u": frozenset({"at"})}):
        loose = found.gating(unshaped)
        (line,) = loose.lines
        assert not line.gated and not line.within and not line.failed
        assert loose.failures() == []
        # The distances are still computed and printed, and the line says why it does not gate.
        assert line.line() == (
            "calendar:at (t): outside, not gated: no rates entry shapes this column -- "
            "month distance 0.9166 (tolerance 0.1) (outside)"
        )
        document = loose.artifact()["calendar"]
        assert document["met"] is True
        assert document["lines"][0]["outcome"] == "outside"
        assert document["lines"][0]["gated"] is False
    # A passing line prints as passed whether or not it gates.
    matching = calendar_for_emit(
        "t", [_matching()], _run_of([LEAP_DAY, SUNDAY_LAST_MS, MONDAY_FIRST]), {"at": "TIMESTAMP"}
    ).gating({})
    assert matching.lines[0].line().startswith("calendar:at (t): passed -- ")


def test_a_distance_at_the_tolerance_passes() -> None:
    # 10 values: 9 in January, 1 in February, against a profile of 8 and 2: distance 1/10.
    cells = ["2024-01-15T00:00:00.000Z"] * 9 + ["2024-02-15T00:00:00.000Z"]
    entry = _entry(month=_hist("month", [8, 2] + [0] * 10))
    (line,) = calendar_for_emit("t", [entry], _run_of(cells), {"at": "TIMESTAMP"}).lines
    assert line.parts[0].distance == Fraction(1, 10)
    assert line.within


def test_a_suppressed_cell_is_left_out_on_both_sides_not_scored_as_zero() -> None:
    # The profile withheld February. The run holds nothing but February and January: over the
    # published cells the run is all January and so is the profile, so the distance is 0.
    cells = ["2024-01-15T00:00:00.000Z"] * 4 + ["2024-02-15T00:00:00.000Z"] * 6
    counts: List[Optional[int]] = [4, None] + [0] * 10
    statuses = ["F", "C"] + ["F"] * 10
    entry = _entry(month=_hist("month", counts, statuses))
    (line,) = calendar_for_emit("t", [entry], _run_of(cells), {"at": "TIMESTAMP"}).lines
    assert line.parts[0].distance == 0
    assert line.within


def test_a_histogram_the_profile_withheld_is_censored_and_a_line_with_nothing_scored_fails() -> (
    None
):
    document = _hist("month", [None] * 12, ["C"] * 12)
    document["groups"][0]["status"] = "C"
    document["groups"][0] = {"group": "", "status": "C"}
    entry = _entry(month=document)
    (line,) = calendar_for_emit("t", [entry], _run_of([LEAP_DAY]), {"at": "TIMESTAMP"}).lines
    assert line.parts[0].censored
    assert not line.within
    assert "withheld by the profile" in line.line()


def test_a_date_with_a_published_hour_of_week_lists_it_as_not_scored() -> None:
    cells = ["2024-02-29", "2024-03-03"]
    counts = calendar_counts(_rows("on", cells), "on", "DATE")
    hours = [0] * 168
    entry = _entry(
        "on",
        month=_hist("month", counts["month"]),
        hourOfWeek=_hist("hourOfWeek", hours),
    )
    found = calendar_for_emit("t", [entry], _run_of_dates(cells), {"on": "DATE"})
    (line,) = found.lines
    assert [part.key for part in line.parts] == ["month"]
    assert line.within
    assert found.not_scored == (("calendar:on.hourOfWeek", NO_HOUR_REASON),)
    assert NO_HOUR_REASON == "a DATE has no hour"
    assert "  facts: not scored -- calendar:on.hourOfWeek: a DATE has no hour" in found.text()


def _run_of_dates(cells: Sequence[str]) -> DecodedRows:
    return _decoded("on", cells)


def test_a_hidden_column_is_not_scored_with_the_reason_the_date_facts_give() -> None:
    found = calendar_for_emit("t", [_matching()], _run_of([LEAP_DAY]), {}, hidden_columns=("at",))
    assert found.lines == ()
    assert found.not_scored == (
        (
            "calendar:at",
            "it reads the hidden column t.at; the run's streams hold no hidden column and no "
            "hidden table",
        ),
    )
    assert found.artifact()["calendar"]["notScored"][0]["fact"] == "calendar:at"


def test_a_hidden_table_is_read_from_the_recorded_plan_and_not_scored() -> None:
    document = {
        "_producer": "urn:example:test",
        "_schemaURL": PROFILE_URL_1_3_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "t": {
                "columns": {"at": {"domain": "timestamp"}},
                "calendar": [{"column": "at", "month": _hist("month", [1] * 12)}],
            }
        },
    }
    profile = parse_profile(document)
    plan = {"emits": {"t": {"hidden": True}, "u": {"generator": {"hiddenColumns": ["x"]}}}}
    assert hidden_in_plan(plan) == (frozenset({"t"}), {"u": ("x",)})
    found = hidden_not_scored(profile, plan)
    assert found.not_scored == (
        (
            "calendar:at",
            "it reads the hidden table t; the run's streams hold no hidden column and no hidden table",
        ),
    )
    assert hidden_not_scored(profile, {"emits": {"t": {}}}).empty


def test_a_column_the_run_does_not_hold_or_that_is_no_date_is_not_scored() -> None:
    entry = _matching()
    absent = calendar_for_emit("t", [entry], _run_of([LEAP_DAY]), {"other": "TIMESTAMP"})
    assert absent.not_scored == (("calendar:at", "the run's emit t holds no column at"),)
    text = calendar_for_emit("t", [entry], _run_of([LEAP_DAY]), {"at": "STRING"})
    assert text.not_scored == (
        ("calendar:at", "the column is a STRING, not a DATE or a TIMESTAMP"),
    )
    unreadable = calendar_for_emit(
        "t", [entry], None, {"at": "TIMESTAMP"}, unreadable="no emit file"
    )
    assert unreadable.not_scored[0][1] == "the emit's rows cannot be read: no emit file"


# ---------------------------------------------------------------------------
# The scorecard's rows (SCORECARD-SPEC.md section 3, format 1-4-0)
# ---------------------------------------------------------------------------

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"


def _scorecard(
    rows: Any, declaration: Optional[ScorecardDeclaration] = None, expose_id: str = "t"
) -> Any:
    return build_scorecard(
        run_id="0123456789abcdef",
        plan_digest="sha256:" + "0" * 64,
        run_digest="sha256:" + "1" * 64,
        profile_digest="sha256:" + "2" * 64,
        tables=[TableInput(expose_id=expose_id)],
        module_rows=rows,
        declaration=declaration,
    )


def _floor(value: str) -> ScorecardDeclaration:
    threshold = Threshold(
        scope="dimension",
        name="temporal",
        operator="mustBeGreaterOrEqualTo",
        value=Fraction(value),
        declared=value,
    )
    return ScorecardDeclaration(thresholds=(threshold,))


def _skewed_checks() -> Any:
    """A month histogram off by 1/12 + ..., a matching day of week and an hour of week it withheld."""
    cells = [LEAP_DAY, SUNDAY_LAST_MS, MONDAY_FIRST]
    counts = calendar_counts(_rows("at", cells), "at", "TIMESTAMP")
    # The run holds February twice and March once; the profile says February once, March twice.
    skewed = [0, 1, 2] + [0] * 9
    withheld = {"group": "", "status": "C"}
    hour = _hist("hourOfWeek", [0] * 168)
    hour["groups"][0] = withheld
    entry = _entry(
        month=_hist("month", skewed),
        dayOfWeek=_hist("dayOfWeek", counts["dayOfWeek"]),
        hourOfWeek=hour,
    )
    return calendar_for_emit("t", [entry], _run_of(cells), {"at": "TIMESTAMP"})


def test_each_published_histogram_is_a_row_of_the_temporal_dimension() -> None:
    checks = _skewed_checks()
    rows = calendar_rows(checks)["t"]
    assert [row.property_id for row in rows] == [
        "calendar:at.month",
        "calendar:at.dayOfWeek",
        "calendar:at.hourOfWeek",
    ]
    month, weekday, hour = rows
    assert {row.dimension for row in rows} == {"temporal"} and {row.kind for row in rows} == {
        "calendar"
    }
    # The run is 2/3 February and 1/3 March; the profile is 1/3 and 2/3: distance 1/3.
    assert month.outcome == "outside" and month.score == Fraction(2, 3)
    assert month.document is not None
    assert month.document["distance"] == "1/3" and month.document["tolerance"] == "0.1"
    assert month.document["records"] == 3 and month.document["histogram"] == "month"
    assert weekday.outcome == "within" and weekday.score == 1
    assert hour.outcome == "censored" and hour.score is None
    assert hour.document is not None
    assert hour.document["score"] is None and hour.document["distance"] is None
    assert hour.document["tolerance"] is None


def test_a_row_reads_the_comparison_of_its_line_and_forms_no_second_one() -> None:
    checks = _skewed_checks()
    (line,) = checks.lines
    rows = calendar_rows(checks)["t"]
    for part, row in zip(line.parts, rows, strict=True):
        assert row.outcome == part.outcome
        assert row.document is not None
        if part.distance is None or part.tolerance is None:
            continue
        assert row.score == 1 - part.distance
        assert row.document["distance"] == render_exact(part.distance)
        assert row.document["tolerance"] == render_exact(part.tolerance)


def test_a_histogram_the_comparison_refused_is_a_row_with_no_score() -> None:
    entry = _entry(month=_hist("month", [1] * 12))
    found = calendar_for_emit("t", [entry], _run_of(["not an instant"]), {"at": "TIMESTAMP"})
    (row,) = calendar_rows(found)["t"]
    assert row.outcome == "refused" and row.score is None
    assert row.document is not None
    assert [item["code"] for item in row.document["refusals"]] == ["FLX-5005"]
    assert "not an instant" not in json.dumps(row.document)


def test_what_a_line_lists_as_not_scored_is_not_a_row() -> None:
    cells = ["2024-02-29", "2024-03-03"]
    counts = calendar_counts(_rows("on", cells), "on", "DATE")
    entry = _entry(
        "on", month=_hist("month", counts["month"]), hourOfWeek=_hist("hourOfWeek", [0] * 168)
    )
    found = calendar_for_emit("t", [entry], _run_of_dates(cells), {"on": "DATE"})
    assert found.not_scored
    assert [row.property_id for row in calendar_rows(found)["t"]] == ["calendar:on.month"]
    hidden = calendar_for_emit("t", [entry], _run_of_dates(cells), {}, hidden_columns=("on",))
    assert calendar_rows(hidden) == {}


def test_a_run_with_no_calendar_has_no_rows_and_a_scorecard_that_keeps_its_bytes() -> None:
    assert calendar_rows(None) == {}
    assert calendar_rows(calendar_for_emit("t", [], None, {})) == {}
    plain = _scorecard(None)
    assert _scorecard({}).to_bytes() == plain.to_bytes()
    assert plain.to_document()["_schemaURL"] == SCHEMA_URL


def test_the_temporal_dimension_scores_its_rows_and_the_scorecard_names_format_1_4_0() -> None:
    scorecard = _scorecard(calendar_rows(_skewed_checks()))
    document = scorecard.to_document()
    assert document["_schemaURL"] == SCHEMA_URL_1_4_0
    temporal = next(d for d in document["dimensions"] if d["dimension"] == "temporal")
    # (2/3 + 1) / 2: the censored hour of week is left out of the mean.
    assert temporal["status"] == "measured" and temporal["score"] == "5/6"
    assert temporal["counts"] == {"properties": 3, "scored": 2, "censored": 1, "refused": 0}
    (table,) = document["tables"]
    assert table["score"] == "5/6" and len(table["properties"]) == 3


def test_a_threshold_on_temporal_is_met_by_a_calendar_and_not_met_by_none() -> None:
    rows = calendar_rows(_skewed_checks())
    met = _scorecard(rows, _floor("0.8"))
    assert [result.met for result in met.thresholds] == [True] and met.met
    missed = _scorecard(rows, _floor("0.9"))
    assert [result.met for result in missed.thresholds] == [False] and not missed.met
    nothing = _scorecard(None, _floor("0"))
    assert [result.met for result in nothing.thresholds] == [False], "nothing was scored"
    assert nothing.failure_lines()[0].endswith("no property under it was scored")


def test_the_schema_accepts_calendar_rows_and_refuses_a_malformed_one() -> None:
    schema = json.loads((SPEC / "scorecard-1-4.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    document = _scorecard(calendar_rows(_skewed_checks())).to_document()
    validator.validate(document)
    for damage in (
        lambda row: row.update(histogram="week"),
        lambda row: row.update(dimension="marginals"),
        lambda row: row.pop("records"),
        lambda row: row.update(score="1.5"),
        lambda row: row.update(extra=1),
    ):
        broken = json.loads(json.dumps(document))
        damage(broken["tables"][0]["properties"][0])
        assert list(validator.iter_errors(broken)), "a malformed calendar row is refused"


def test_the_format_is_1_4_0_only_with_a_calendar_row_or_another_1_4_0_row() -> None:
    from flux_engine.concordance.module_scores import module_rows
    from flux_engine.concordance.rule_shares import RuleShare
    from flux_engine.concordance.shares import Holds

    share = RuleShare(
        expose_id="t",
        rule_id="r",
        form="expression",
        origin="contract",
        holds=Holds(1, 1),
        enforced=True,
    )
    facts = module_rows([share], [], [], None)
    assert _scorecard(None).to_document()["_schemaURL"] == SCHEMA_URL
    assert _scorecard(facts).to_document()["_schemaURL"] == SCORECARD_URL_1_3_0
    both = {"t": list(facts["t"]) + list(calendar_rows(_skewed_checks())["t"])}
    assert _scorecard(both).to_document()["_schemaURL"] == SCHEMA_URL_1_4_0
