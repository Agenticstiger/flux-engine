# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The shape flags, on profiles written out by hand (SCORECARD-SPEC.md §6).

Each flag is pinned on a run that raises it and on one that does not: the
acceptance case (a column drawn as three uniform blocks is flagged for its
empty bins), a point mass inside and outside the profile's cells, a constant
column, a distinct count above twice and below half the prediction. The
level is pinned as a split across every test of the run (Bonferroni), and
the profile's withheld cells and rounded counts are pinned as read against
a flag. The document is pinned valid against the schema, the same bytes on
every call, and free of the data's values.
"""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest
from jsonschema import Draft202012Validator

from flux_engine.concordance.shape import (
    ABSENT,
    CHECKED,
    CONSTANT,
    EMPTY_BINS,
    HIGH_CARDINALITY,
    IMBALANCE,
    LOW_CARDINALITY,
    NOT_PROFILED,
    REFUSED,
    ShapeReport,
    decide_shape,
    refused_emit,
    summarise_emit,
)
from flux_stats.profile import SCHEMA_URL, parse_profile

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
SCHEMA = json.loads((SPEC / "scorecard-1.schema.json").read_text(encoding="utf-8"))
SHAPE_SCHEMA = {"$defs": SCHEMA["$defs"], "$ref": "#/$defs/ShapeFlags"}

TABLE = "orders"
EDGES = list(range(0, 101, 10))


def _histogram(
    counts: Sequence[Optional[int]],
    *,
    edges: Sequence[int] = EDGES,
    cell_status: Optional[Sequence[str]] = None,
    disclosure: str = "none",
) -> Dict[str, Any]:
    group: Dict[str, Any] = {
        "group": "",
        "k": sum(count or 0 for count in counts),
        "binCounts": list(counts),
        "status": "F",
    }
    if cell_status is not None:
        group["cellStatus"] = list(cell_status)
    return {
        "definition": "marginalHistogram/1",
        "sampleSize": {"min": 1},
        "disclosure": disclosure,
        "groups": [group],
        "bins": [str(edge) for edge in edges],
    }


def _levels(levels: Sequence[str], counts: Sequence[int]) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "sampleSize": {"min": 1},
        "disclosure": "none",
        "groups": [{"group": "", "k": sum(counts), "levelCounts": list(counts), "status": "F"}],
        "levels": list(levels),
    }


def _metric(name: str, value: int, disclosure: str = "none") -> Dict[str, Any]:
    return {
        name: {
            "definition": f"{name}/1",
            "status": "F",
            "sampleSize": {"min": 1},
            "disclosure": disclosure,
            "value": value,
        }
    }


def _distinct(
    value: int,
    disclosure: str = "none",
    *,
    count: Optional[int] = None,
    count_disclosure: str = "none",
) -> Dict[str, Any]:
    metrics = _metric("distinctCount", value, disclosure)
    if count is not None:
        metrics.update(_metric("count", count, count_disclosure))
    return metrics


def _column(
    domain: str = "number",
    statistics: Sequence[Mapping[str, Any]] = (),
    metrics: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    column: Dict[str, Any] = {"domain": domain, "statistics": list(statistics)}
    if metrics:
        column["metrics"] = dict(metrics)
    return column


def _profile(columns: Mapping[str, Mapping[str, Any]], *, tables: Optional[Dict] = None) -> Any:
    document = {
        "_producer": "urn:example:flux-test-profiler",
        "_schemaURL": SCHEMA_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {
            "none": {},
            "base5": {"roundingBase": 5},
            "base100": {"roundingBase": 100},
        },
        "tables": tables
        or {TABLE: {"rowCount": 1000, "columns": dict(columns), "columnPairs": []}},
    }
    return parse_profile(document)


def _uniform(blocks: Sequence[int], per_block: int = 1000) -> List[Dict[str, Any]]:
    """Rows of ``amount``: *per_block* values spread over each 10-wide block."""
    return [
        {"amount": str(Decimal(block * 10 + index % 100) / 10)}
        for block in blocks
        for index in range(per_block)
    ]


def _report(profile: Any, rows: Sequence[Mapping[str, Any]], expose_id: str = TABLE) -> ShapeReport:
    return decide_shape([summarise_emit(profile, expose_id, rows)])


def _all_flags(report: ShapeReport, column: str = "amount") -> List[Any]:
    table = report.tables[0]
    found = next(entry for entry in table.columns if entry.summary.column == column)
    return list(found.flags)


def _flags(report: ShapeReport, column: str = "amount") -> Dict[str, Any]:
    return {flag.alert: flag for flag in _all_flags(report, column)}


UNIFORM_PROFILE = {"amount": _column(statistics=[_histogram([100] * 10)])}


# ---------------------------------------------------------------------------
# EMPTY_BINS
# ---------------------------------------------------------------------------


def test_a_column_drawn_as_three_uniform_blocks_is_flagged_for_its_empty_bins() -> None:
    report = _report(_profile(UNIFORM_PROFILE), _uniform([0, 40, 90]))
    flags = _flags(report)
    assert flags[EMPTY_BINS].cells == (1, 2, 3, 5, 6, 7, 8)


def test_a_run_drawn_over_the_whole_range_is_not_flagged() -> None:
    report = _report(_profile(UNIFORM_PROFILE), _uniform(range(0, 100, 10), per_block=300))
    assert _flags(report) == {}
    assert report.summary_lines()[0].startswith("shape: no flags in 1 profiled column")


def test_an_empty_bin_the_profile_makes_likely_enough_is_not_flagged() -> None:
    # Two bins of 500: 12 records leave one empty with chance 1/4096, above
    # the 1/5000 each of the run's five tests (two empty cells, two point
    # masses, one constant column) is taken at; 13, with 1/8192, are below.
    profile = _profile({"amount": _column(statistics=[_histogram([500, 500], edges=[0, 50, 100])])})
    twelve = [{"amount": "10"}] * 12
    thirteen = [{"amount": "10"}] * 13
    assert EMPTY_BINS not in _flags(_report(profile, twelve))
    # The thirteen all sit in the other cell: a mass the profile makes as unlikely.
    assert set(_flags(_report(profile, thirteen))) == {EMPTY_BINS, IMBALANCE}
    assert _flags(_report(profile, thirteen))[EMPTY_BINS].cells == (1,)
    assert _report(profile, thirteen).tests == 5


def test_the_level_is_split_across_every_test_of_the_run() -> None:
    # The same twelve records are flagged alone, and not once a second
    # profiled column brings twenty more tests into the run.
    two_bins = _histogram([500, 500], edges=[0, 50, 100])
    profile = _profile(
        {
            "amount": _column(statistics=[two_bins]),
            "other": _column(statistics=[_histogram([100] * 10)]),
        }
    )
    rows = [{"amount": "10", "other": str(5 + 10 * (index % 10))} for index in range(12)]
    report = _report(profile, rows)
    assert report.tests == (2 + 2 + 1) + (10 + 10 + 1)
    assert report.test_level == Fraction(1, 1000 * 26)
    assert EMPTY_BINS not in _flags(report)


def test_a_withheld_cell_is_never_tested_and_its_records_are_left_out() -> None:
    counts: List[Optional[int]] = [100] * 10
    counts[4] = None
    status = ["F"] * 10
    status[4] = "A"
    profile = _profile({"amount": _column(statistics=[_histogram(counts, cell_status=status)])})
    report = _report(profile, _uniform([0, 10, 20, 30, 50, 60, 70, 80, 90]))
    assert _flags(report) == {}
    assert report.tests == 9 + 9 + 1


def test_a_rounded_count_is_read_as_the_smallest_share_it_allows() -> None:
    # 3000 records leave the first cell empty. Read as published, 5 of 1000,
    # that is a chance of about 3e-7 and a flag; ACRO's base 5 lets the 5 be
    # as little as 1, a chance of about 0.05, and no flag.
    edges = [0, 50, 100]
    rows = [{"amount": "75"}] * 3000
    plain = _profile({"amount": _column(statistics=[_histogram([5, 995], edges=edges)])})
    rounded = _profile(
        {"amount": _column(statistics=[_histogram([5, 995], edges=edges, disclosure="base5")])}
    )
    assert _flags(_report(plain, rows))[EMPTY_BINS].cells == (0,)
    assert EMPTY_BINS not in _flags(_report(rounded, rows))


# ---------------------------------------------------------------------------
# IMBALANCE
# ---------------------------------------------------------------------------


def test_a_point_mass_the_profile_does_not_have_is_flagged() -> None:
    # 55.05 is not among the uniform values (55.0 is, and 55.0 is 55 exactly).
    rows = _uniform(range(0, 100, 10), per_block=60) + [{"amount": "55.05"}] * 400
    flags = _flags(_report(_profile(UNIFORM_PROFILE), rows))
    assert flags[IMBALANCE].cell == 5
    assert flags[IMBALANCE].count == 400


def test_a_point_mass_outside_every_cell_is_flagged_without_a_cell() -> None:
    rows = _uniform(range(0, 100, 10), per_block=80) + [{"amount": "150"}] * 200
    flag = _flags(_report(_profile(UNIFORM_PROFILE), rows))[IMBALANCE]
    assert flag.cell is None
    assert flag.to_document() == {"alert": IMBALANCE, "cell": None, "count": 200}


def test_a_level_holding_the_share_the_profile_gives_it_is_not_a_point_mass() -> None:
    profile = _profile(
        {"market": _column("category", statistics=[_levels(["DE", "FR", "GB"], [500, 300, 200])])}
    )
    rows = [{"market": "DE"}] * 500 + [{"market": "FR"}] * 300 + [{"market": "GB"}] * 200
    assert _flags(_report(profile, rows), "market") == {}
    skewed = [{"market": "GB"}] * 700 + [{"market": "DE"}] * 200 + [{"market": "FR"}] * 100
    assert _flags(_report(profile, skewed), "market")[IMBALANCE].cell == 2


# ---------------------------------------------------------------------------
# CONSTANT and the distinct count
# ---------------------------------------------------------------------------


def test_a_constant_column_is_flagged_where_the_profile_shows_more_than_one_value() -> None:
    # Fifty values among a thousand records: even the most lopsided such
    # column leaves 200 records on one value with a chance of about 4e-5.
    profile = _profile({"amount": _column(metrics=_distinct(50, count=1000))})
    report = _report(profile, [{"amount": "42"}] * 200)
    assert set(_flags(report)) == {CONSTANT}
    assert report.tests == 1
    assert report.summary_lines() == [
        "shape: 1 flag in 1 of 1 profiled column (1 test at 1/1000 per run); they inform and "
        "withhold nothing",
        "shape of orders.amount: CONSTANT",
    ]
    # A hundred such records happen about once in 150 runs: not flagged.
    assert _flags(_report(profile, [{"amount": "42"}] * 100)) == {}


def test_one_record_is_never_constant_and_a_rounded_two_may_be_one() -> None:
    profile = _profile({"amount": _column(metrics=_distinct(50))})
    assert CONSTANT not in _flags(_report(profile, [{"amount": "42"}]))
    rounded = _profile({"amount": _column(metrics=_distinct(5, disclosure="base5"))})
    assert CONSTANT not in _flags(_report(rounded, [{"amount": "42"}] * 100))


def test_a_distinct_count_above_twice_the_prediction_is_high_cardinality() -> None:
    profile = _profile(
        {"market": _column("category", statistics=[_levels(["DE", "FR", "GB"], [500, 300, 200])])}
    )
    rows = [{"market": code} for code in ("DE", "FR", "GB", "A", "B", "C", "D")] * 50
    # Seven distinct values where the three levels predict just under 3.
    flag = _flags(_report(profile, rows), "market")[HIGH_CARDINALITY]
    assert flag.to_document() == {"alert": HIGH_CARDINALITY}


def test_a_distinct_count_below_half_the_prediction_is_low_cardinality() -> None:
    # A thousand values of one record each, a thousand draws: about 632
    # distinct values are expected, and five is far below half of that.
    profile = _profile({"amount": _column(metrics=_distinct(1000, count=1000))})
    rows = [{"amount": str(index % 5)} for index in range(1000)]
    flag = _flags(_report(profile, rows))[LOW_CARDINALITY]
    assert flag.text(5) == "LOW_CARDINALITY (5 distinct, below half the fewest the profile allows)"


def test_no_count_of_the_profile_reaches_the_document() -> None:
    # Two profiles that differ only in their distinctCount flag the same run
    # alike, and the documents are the same bytes: the prediction, which with
    # the run's records would give the distinctCount back, is not in them.
    rows = [{"amount": str(index % 5)} for index in range(1000)]
    documents = [
        json.dumps(
            _report(
                _profile({"amount": _column(metrics=_distinct(count, count=2000))}), rows
            ).to_document(),
            sort_keys=True,
        )
        for count in (1000, 2000)
    ]
    assert documents[0] == documents[1]
    assert "2000" not in documents[1]


# ---------------------------------------------------------------------------
# What is not read, and the document
# ---------------------------------------------------------------------------


def test_a_value_that_cannot_be_read_refuses_its_column_and_no_other() -> None:
    profile = _profile(
        {
            "amount": _column(statistics=[_histogram([100] * 10)]),
            "count": _column(metrics=_distinct(9)),
        }
    )
    rows = [{"amount": "secret-text", "count": 3}, {"amount": "5", "count": 3}]
    table = _report(profile, rows).tables[0]
    by_name = {column.summary.column: column.summary for column in table.columns}
    assert by_name["amount"].status == REFUSED
    assert by_name["amount"].code == "FLX-5005"
    assert "secret-text" not in (by_name["amount"].reason or "")
    assert by_name["count"].status == CHECKED


def test_a_column_the_rows_do_not_carry_is_absent_and_a_table_the_profile_lacks_is_not_profiled() -> (
    None
):
    profile = _profile(
        {"amount": _column(metrics=_distinct(9)), "missing": _column(metrics=_distinct(9))}
    )
    report = decide_shape(
        [
            summarise_emit(profile, TABLE, [{"amount": "1"}, {"amount": "2"}]),
            summarise_emit(profile, "elsewhere", [{"amount": "1"}]),
            refused_emit("broken", "FLX-8001", "no emit file broken.jsonl for this run"),
        ]
    )
    statuses = {table.summary.expose_id: table.summary.status for table in report.tables}
    assert statuses == {TABLE: CHECKED, "elsewhere": NOT_PROFILED, "broken": REFUSED}
    columns = {column.summary.column: column.summary.status for column in report.tables[2].columns}
    assert columns == {"amount": CHECKED, "missing": ABSENT}


def test_the_document_is_valid_the_same_bytes_and_free_of_values() -> None:
    profile = _profile(
        {
            "amount": _column(statistics=[_histogram([100] * 10)], metrics=_distinct(1000)),
            "market": _column(
                "category", statistics=[_levels(["DE", "FR", "GB"], [500, 300, 200])]
            ),
        }
    )
    rows = [
        {"amount": str(Decimal(block * 10 + index % 100) / 10), "market": "zebra-crossing"}
        for block in (0, 40, 90)
        for index in range(1000)
    ]
    document = _report(profile, rows).to_document()
    Draft202012Validator(SHAPE_SCHEMA).validate(document)
    text = json.dumps(document, sort_keys=True)
    assert text == json.dumps(_report(profile, rows).to_document(), sort_keys=True)
    assert "zebra" not in text
    assert "40.5" not in text
    flags = {
        column["column"]: [flag["alert"] for flag in column["flags"]]
        for column in document["tables"][0]["columns"]
    }
    assert flags == {
        "amount": [EMPTY_BINS],
        "market": [EMPTY_BINS, IMBALANCE, CONSTANT],
    }


# ---------------------------------------------------------------------------
# Regressions found in review
# ---------------------------------------------------------------------------


def test_a_column_outside_the_profiled_range_is_flagged_for_every_bin() -> None:
    # Every record lies above the profile's last edge (100 itself is in the
    # last bin, which is closed at the top): each is a record the profile
    # gives no chance, so it counts against every cell.
    profile = _profile({"amount": _column(statistics=[_histogram([100] * 10)])})
    rows = [{"amount": str(101 + index % 100)} for index in range(5000)]
    assert _flags(_report(profile, rows))[EMPTY_BINS].cells == tuple(range(10))


def test_a_second_point_mass_behind_a_larger_one_is_flagged() -> None:
    # The profile holds a real mass in the first bin; the run's 40 % there is
    # within it, while its 20 % on one value in the last bin is not.
    profile = _profile({"amount": _column(statistics=[_histogram([451] + [61] * 9)])})
    rows = (
        [{"amount": "5"}] * 2000
        + [{"amount": "95"}] * 1000
        + [{"amount": f"{index % 100}.25"} for index in range(2000)]
    )
    masses = [flag for flag in _all_flags(_report(profile, rows)) if flag.alert == IMBALANCE]
    assert [(flag.cell, flag.count) for flag in masses] == [(9, 1000)]


def test_a_run_larger_than_the_profiles_records_is_not_checked_for_its_distinct_count() -> None:
    # A near-unique column profiled on a thousand records: a faithful run of
    # five thousand shows some five thousand values, which the profile, never
    # having seen more than a thousand records, cannot speak to.
    profile = _profile({"amount": _column(metrics=_distinct(1000, count=1000))})
    rows = [{"amount": f"{index}.5"} for index in range(5000)]
    assert _flags(_report(profile, rows)) == {}
    smaller = [{"amount": f"{index}.5"} for index in range(1000)]
    assert _flags(_report(profile, smaller)) == {}


def test_a_skewed_column_is_read_against_the_fewest_values_its_counts_allow() -> None:
    # 10,000 records holding 1,010 values, heavily skewed: a run of a thousand
    # with about a hundred distinct values is what such a column shows, and is
    # not flagged; one with ten is below half of even the fewest it allows.
    profile = _profile({"amount": _column(metrics=_distinct(1010, count=10_000))})
    faithful = [{"amount": str(index % 105)} for index in range(1000)]
    assert _flags(_report(profile, faithful)) == {}
    narrow = [{"amount": str(index % 10)} for index in range(1000)]
    assert set(_flags(_report(profile, narrow))) == {LOW_CARDINALITY}


def test_a_tiny_constant_run_the_profile_makes_likely_is_not_constant() -> None:
    profile = _profile({"m": _column("category", statistics=[_levels(["A", "B"], [500, 500])])})
    assert CONSTANT not in _flags(_report(profile, [{"m": "A"}] * 2), "m")
    assert CONSTANT in _flags(_report(profile, [{"m": "A"}] * 30), "m")
    assert CONSTANT in _flags(_report(profile, [{"m": "ZZ"}] * 2), "m"), "outside every cell"


def test_the_bin_found_by_bisection_is_the_one_bin_index_gives() -> None:
    import random

    from flux_engine.concordance.shape import _cells
    from flux_stats import bin_index
    from flux_stats.fixedpoint import Grid

    generator = random.Random(7)
    for _ in range(200):
        edges = sorted(generator.sample(range(-500, 500), generator.randint(2, 12)))
        statistic = _histogram([1] * (len(edges) - 1), edges=edges)
        profile = _profile({"amount": _column(statistics=[statistic])})
        grid = Grid(fractional_bits=profile.fractional_bits)
        entry = profile.column(TABLE, "amount")
        assert entry is not None
        cells = _cells(profile, entry, grid)
        assert cells is not None
        cell_of = cells.locator(grid)
        for units in range(edges[0] * 4, edges[-1] * 4 + 1):
            value = Fraction(units, 4)
            expected = bin_index(grid.quantise(value), cells.edges)
            assert cell_of(value) == expected


def test_a_value_above_the_top_edge_by_less_than_a_grid_step_is_outside() -> None:
    profile = _profile({"amount": _column(statistics=[_histogram([500, 500], edges=[0, 50, 100])])})
    rows = [{"amount": value} for value in ("50", "100", "100.00001", "0", "-0.00001")] * 10
    (summary,) = summarise_emit(profile, TABLE, rows).columns
    assert summary.cell_counts == (10, 20)
    assert summary.outside == 20


def test_a_timestamp_the_calendar_cannot_hold_refuses_its_column() -> None:
    edges = [0, 10**12, 2 * 10**12]
    profile = _profile({"at": _column("timestamp", statistics=[_histogram([5, 5], edges=edges)])})
    rows = [{"at": "0001-01-01T00:00:00+01:00"}]
    (column,) = summarise_emit(profile, TABLE, rows).columns
    assert (column.status, column.code) == (REFUSED, "FLX-5022")


def test_a_column_with_no_value_takes_no_test_and_says_so() -> None:
    profile = _profile({"amount": _column(statistics=[_histogram([500, 500], edges=[0, 50, 100])])})
    report = _report(profile, [{"amount": None}] * 5)
    assert report.tests == 0
    assert "shape of orders.amount: no non-null record to test" in report.summary_lines()


def test_a_rounded_distinct_count_is_read_at_both_ends_of_what_it_allows() -> None:
    # Published as 5, base 5: anything from 1 to 9 values. Twelve distinct
    # values in a hundred records are above twice five but not twice nine.
    rows = [{"amount": str(index % 12)} for index in range(100)]
    plain = _profile({"amount": _column(metrics=_distinct(5, count=1000))})
    rounded = _profile({"amount": _column(metrics=_distinct(5, "base5", count=1000))})
    assert set(_flags(_report(plain, rows))) == {HIGH_CARDINALITY}
    assert _flags(_report(rounded, rows)) == {}


def test_a_rounded_count_is_read_where_it_makes_a_constant_column_likeliest() -> None:
    # Two values among 1000 records, so one value can hold 999 of them: 6910
    # records of one value then have a chance of about 9.9e-4, a flag at the
    # run's one test. Rounded to base 100 the 1000 may be 1099, and the chance
    # about 1.85e-3: no flag.
    rows = [{"amount": "7"}] * 6910
    plain = _profile({"amount": _column(metrics=_distinct(2, count=1000))})
    rounded = _profile(
        {"amount": _column(metrics=_distinct(2, count=1000, count_disclosure="base100"))}
    )
    assert set(_flags(_report(plain, rows))) == {CONSTANT}
    assert _flags(_report(rounded, rows)) == {}
