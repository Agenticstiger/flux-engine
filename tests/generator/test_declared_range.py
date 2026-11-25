# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A date column's declared statistics are cut at the contract's range before the draw (ADR 0024, amendment of 2026-11-25).

``validationRules: [{type: range, constraint: ...}]`` on a DATE or TIMESTAMP column is read as the
FLUID profiler's spelling reads it (:func:`flux_engine.author.window.declared_instants`). On kernel
0.2.0 the statistics the column is drawn from are cut at it: a range is intersected, a bin is cut at
the range's ends and keeps weight in proportion to the width it keeps, and a bin outside is dropped.
Nothing inside the range is ``FLX-3403``. A column whose statistics lie inside keeps its plan and
every byte; kernel 0.1.0 keeps its bytes. Pinned here on the plan and the rows, with the column
path held to the per-row path.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
from functools import partial
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SeedTreeRoot, derive_root, digest_bytes
from flux_engine.generator import columns
from flux_engine.generator.declared_range import (
    ColumnRange,
    declared_ranges_of,
    range_cut_notes,
    range_problems,
)
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_rows,
)
from flux_engine.generator.statistics import DATE_EPOCH_ORDINAL
from flux_engine.seam.range_rules import RangeBounds, declared_instants, declared_range_bounds

from .conftest import SIM_ID, TIME_BOUNDS, H, expose_from_schema, fidelity
from .test_columns import _both

pytestmark = pytest.mark.unit

DAY_MS = 86_400_000


def _days(text: str) -> int:
    return date.fromisoformat(text).toordinal() - DATE_EPOCH_ORDINAL


def _range_rule(text: str) -> List[Dict[str, str]]:
    return [{"type": "range", "constraint": text}]


def _plan(
    declared: Dict[str, Any],
    rule: Optional[str],
    *,
    column_type: str = "DATE",
    version: str = GENERATOR_VERSION_2,
    rows: int = 400,
) -> GeneratorPlan:
    entry: Dict[str, Any] = {"name": "shipped_on", "type": column_type}
    if rule is not None:
        entry["validationRules"] = _range_rule(rule)
    return build_plan(
        expose_from_schema([entry]),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=rows,
        fidelity=fidelity({"shipped_on": declared}),
        time_bounds=TIME_BOUNDS,
        generator_version=version,
    )


def _root(plan: GeneratorPlan) -> SeedTreeRoot:
    if plan.generator_version == GENERATOR_VERSION_2:
        return derive_root(digest_bytes(H), 42, seed_tree_version=kernel2.SEED_TREE_VERSION)
    return derive_root(digest_bytes(H), 42)


def _values(plan: GeneratorPlan) -> List[date]:
    return [row[0] for row in generate_rows(plan, root=_root(plan))]


def _spans(values: List[date]) -> List[str]:
    return [min(values).isoformat(), max(values).isoformat()]


# A histogram whose first bin starts before the range, whose last bin lies past it, and whose
# middle bin lies inside: 2000 (leap year) to 2009.
PAST = {
    "bins": ["2000-01-01", "2003-01-01", "2006-01-01", "2009-01-01"],
    "binWeights": ["3", "4", "3"],
}
RANGE = "between '2001-01-01' and '2005-12-31'"


def test_bins_that_pass_the_range_are_cut_at_it_and_keep_weight_by_the_width_they_keep() -> None:
    statistics = _plan(PAST, RANGE).columns[0].statistics
    # The first bin keeps 2001-01-01 to 2002-12-31 of its 1096 days (2000 is a leap year), the second
    # all of its 1095, and the third is dropped. The cut top is inclusive: the last bin is closed.
    assert statistics.bins == tuple(
        Decimal(_days(day)) for day in ("2001-01-01", "2003-01-01", "2005-12-31")
    )
    assert statistics.bin_texts == ("2001-01-01", "2003-01-01", "2005-12-31")
    weights = statistics.bin_weights
    assert weights is not None and len(weights) == 2
    assert Fraction(weights[0]) / Fraction(weights[1]) == Fraction(3 * 730, 1096) / 4


def test_a_column_drawn_from_bins_that_pass_its_range_is_drawn_inside_it() -> None:
    values = _values(_plan(PAST, RANGE))
    low, high = date(2001, 1, 1), date(2005, 12, 31)
    assert len(values) == 400
    assert all(low <= value <= high for value in values)
    # Both ends of the range are drawn, so the cut did not shrink it.
    assert min(values) < date(2001, 6, 1) and max(values) > date(2005, 6, 1)


def test_a_column_that_is_not_cut_is_drawn_outside_the_range_on_kernel_0_1_0() -> None:
    plan = _plan(PAST, RANGE, version=GENERATOR_VERSION)
    assert plan.columns[0].statistics.bins == tuple(Decimal(_days(day)) for day in PAST["bins"])
    values = _values(plan)
    assert any(value < date(2001, 1, 1) or value > date(2005, 12, 31) for value in values)
    assert plan.digest() == _plan(PAST, None, version=GENERATOR_VERSION).digest()


def test_statistics_that_lie_inside_the_range_keep_their_plan_and_their_rows() -> None:
    inside = {"bins": ["2001-01-01", "2003-01-01", "2005-12-31"], "binWeights": ["1", "3"]}
    ruled = _plan(inside, RANGE)
    plain = _plan(inside, None)
    assert ruled.columns[0].statistics == plain.columns[0].statistics
    assert ruled.digest() == plain.digest()
    assert ruled.columns[0].range_cut is None
    assert repr(_values(ruled)) == repr(_values(plain))


def test_a_range_wider_than_the_contract_s_is_intersected_with_it() -> None:
    plan = _plan({"range": {"min": "2000-01-01", "max": "2009-12-31"}}, RANGE)
    statistics = plan.columns[0].statistics
    assert (statistics.range_min, statistics.range_max) == (
        Decimal(_days("2001-01-01")),
        Decimal(_days("2005-12-31")),
    )
    assert all(date(2001, 1, 1) <= value <= date(2005, 12, 31) for value in _values(plan))


def test_a_range_the_contract_s_holds_keeps_its_ends() -> None:
    plan = _plan({"range": {"min": "2002-01-01", "max": "2003-12-31"}}, RANGE)
    assert plan.columns[0].range_cut is None
    values = _values(plan)
    assert _spans(values)[0] >= "2002-01-01" and _spans(values)[1] <= "2003-12-31"


def test_a_column_with_no_statistics_is_drawn_within_the_window_and_the_range() -> None:
    # The window is 2026-07-01 to 2026-09-30; a range that holds part of it cuts the draw to that part.
    plan = _plan({}, "between '2026-08-01' and '2026-12-31'")
    assert all(date(2026, 8, 1) <= value <= date(2026, 9, 30) for value in _values(plan))


def test_a_rate_column_starts_no_earlier_than_the_range() -> None:
    plan = _plan(
        {"family": "rate", "rate": "0.01", "range": {"min": "2000-01-01", "max": "2009-12-31"}},
        RANGE,
    )
    values = _values(plan)
    assert all(date(2001, 1, 1) <= value <= date(2005, 12, 31) for value in values)


def test_a_bin_cut_to_one_day_at_the_top_is_still_drawn() -> None:
    # The range ends on the edge of the last bin that is kept: one day of it is left.
    declared = {
        "bins": ["2001-01-01", "2002-01-01", "2003-01-01"],
        "binWeights": ["1", "366"],
    }
    plan = _plan(declared, "between '2001-01-01' and '2002-01-01'")
    values = _values(plan)
    assert all(date(2001, 1, 1) <= value <= date(2002, 1, 1) for value in values)
    assert date(2002, 1, 1) in values


def test_a_bin_held_between_two_cuts_keeps_its_whole_weight() -> None:
    declared = {
        "bins": ["2000-01-01", "2002-01-01", "2004-01-01", "2006-01-01"],
        "binWeights": ["1", "1", "1"],
    }
    plan = _plan(declared, "between '2000-07-01' and '2005-06-30'")
    weights = plan.columns[0].statistics.bin_weights
    assert weights is not None
    # The first bin keeps 549 of its 731 days, the second all 730, the third 547 of its 732.
    assert Fraction(weights[1]) / Fraction(weights[0]) == Fraction(731, 549)
    assert Fraction(weights[1]) / Fraction(weights[2]) == Fraction(732, 547)


def test_a_conditional_histogram_is_cut_in_every_level() -> None:
    declared = {
        "bins": PAST["bins"],
        "binWeightsGiven": {"north": ["1", "2", "3"], "south": ["3", "2", "1"]},
    }
    entries: List[Mapping[str, Any]] = [
        {"name": "region", "type": "STRING"},
        {
            "name": "shipped_on",
            "type": "DATE",
            "validationRules": _range_rule(RANGE),
        },
    ]
    plan = build_plan(
        expose_from_schema(entries),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=300,
        fidelity=fidelity(
            {
                "region": {"levels": {"north": "0.5", "south": "0.5"}},
                "shipped_on": {"given": "region", **declared},
            }
        ),
        time_bounds=TIME_BOUNDS,
        generator_version=GENERATOR_VERSION_2,
    )
    rows = generate_rows(plan, root=_root(plan))
    assert all(date(2001, 1, 1) <= row[1] <= date(2005, 12, 31) for row in rows)


def test_a_timestamp_column_is_cut_at_the_millisecond() -> None:
    declared = {
        "bins": ["2000-01-01T00:00:00Z", "2003-01-01T00:00:00Z", "2006-01-01T00:00:00Z"],
        "binWeights": ["1", "1"],
    }
    plan = _plan(
        declared,
        "between '2001-06-01T12:00:00Z' and '2004-02-01T00:00:00Z'",
        column_type="TIMESTAMP",
    )
    statistics = plan.columns[0].statistics
    low = int(datetime(2001, 6, 1, 12, tzinfo=timezone.utc).timestamp()) * 1000
    high = int(datetime(2004, 2, 1, tzinfo=timezone.utc).timestamp()) * 1000
    assert statistics.bins is not None
    assert (int(statistics.bins[0]), int(statistics.bins[-1])) == (low, high)
    drawn = [row[0] for row in generate_rows(plan, root=_root(plan))]
    assert drawn and all(isinstance(value, int) for value in drawn)
    assert all(low <= value <= high for value in drawn)


def test_a_range_that_holds_none_of_the_declared_dates_is_flx_3403() -> None:
    with pytest.raises(FluxError) as refused:
        _plan(PAST, "between '2012-01-01' and '2013-12-31'")
    assert refused.value.code.code == "FLX-3403"
    detail = str(refused.value.detail)
    assert "widget_stream.shipped_on" in detail and "2012-01-01 to 2013-12-31" in detail


def test_bins_given_a_column_that_is_drawn_given_them_cannot_be_cut() -> None:
    entries: List[Mapping[str, Any]] = [
        {"name": "shipped_on", "type": "DATE", "validationRules": _range_rule(RANGE)},
        {"name": "carrier", "type": "STRING"},
    ]
    declared = {
        "shipped_on": PAST,
        "carrier": {
            "given": "shipped_on",
            "levelWeightsGiven": {
                "2000-01-01": {"a": "1", "b": "1"},
                "2003-01-01": {"a": "1", "b": "1"},
                "2006-01-01": {"a": "1", "b": "1"},
            },
        },
    }
    with pytest.raises(FluxError) as refused:
        build_plan(
            expose_from_schema(entries),
            sim_id=SIM_ID,
            seam_digest=H,
            rows=50,
            fidelity=fidelity(declared),
            time_bounds=TIME_BOUNDS,
            generator_version=GENERATOR_VERSION_2,
        )
    assert refused.value.code.code == "FLX-3403"
    assert "carrier" in str(refused.value.detail)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("between '2001-01-01' and '2005-12-31'", ("2001-01-01", "2005-12-31")),
        (">= '2001-01-01' and <= '2005-12-31'", ("2001-01-01", "2005-12-31")),
        ('between "2001-01-01" and "2005-12-31"', ("2001-01-01", "2005-12-31")),
        ("BETWEEN '2001-01-01' AND '2005-12-31'", ("2001-01-01", "2005-12-31")),
    ],
)
def test_the_grammar_reads_the_forms_the_profiler_reads(
    text: str, expected: tuple[str, str]
) -> None:
    column = expose_from_schema(
        [{"name": "shipped_on", "type": "DATE", "validationRules": _range_rule(text)}]
    ).columns[0]
    assert column.declared_range == (_days(expected[0]) * DAY_MS, _days(expected[1]) * DAY_MS)


def test_a_strict_end_is_held_one_millisecond_inside_and_the_instants_are_unchanged() -> None:
    rule = _range_rule("> 2001-01-01 and < 2005-12-31")
    entry = {"name": "shipped_on", "type": "DATE", "validationRules": rule}
    column = expose_from_schema([entry]).columns[0]
    assert column.declared_range == (
        _days("2001-01-01") * DAY_MS + 1,
        _days("2005-12-31") * DAY_MS - 1,
    )
    assert column.declared_range_strict == (True, True)
    # `declared_instants` is what `init` reads: it keeps both ends at the instants they name.
    assert declared_instants(entry) == (_days("2001-01-01") * DAY_MS, _days("2005-12-31") * DAY_MS)
    assert declared_instants(
        {"validationRules": _range_rule(">= '2001-01-01' and <= '2005-12-31'")}
    ) == (_days("2001-01-01") * DAY_MS, _days("2005-12-31") * DAY_MS)


def test_the_stricter_of_two_rules_at_one_instant_wins() -> None:
    entry = {
        "name": "shipped_on",
        "type": "DATE",
        "validationRules": [
            *_range_rule(">= '2001-01-01' and <= '2005-12-31'"),
            *_range_rule("> '2001-01-01' and < '2005-12-31'"),
        ],
    }
    assert declared_range_bounds(entry) == RangeBounds(
        _days("2001-01-01") * DAY_MS, _days("2005-12-31") * DAY_MS, True, True
    )


def test_a_strict_high_end_is_never_drawn_and_a_strict_low_end_likewise() -> None:
    values = _values(_plan(PAST, ">= '2001-01-01' and < '2006-01-01'"))
    assert date(2006, 1, 1) not in values
    assert all(date(2001, 1, 1) <= value < date(2006, 1, 1) for value in values)
    low = _values(_plan(PAST, "> '2000-12-31' and <= '2005-12-31'"))
    assert all(date(2001, 1, 1) <= value <= date(2005, 12, 31) for value in low)
    statistics = _plan(PAST, "> '2000-12-31' and < '2006-01-01'").columns[0].statistics
    assert statistics.bin_texts is not None
    assert (statistics.bin_texts[0], statistics.bin_texts[-1]) == ("2001-01-01", "2005-12-31")


def test_a_strict_timestamp_end_is_cut_at_the_millisecond_inside_it() -> None:
    declared = {
        "bins": ["2000-01-01T00:00:00Z", "2003-01-01T00:00:00Z", "2006-01-01T00:00:00Z"],
        "binWeights": ["1", "1"],
    }
    plan = _plan(
        declared,
        "> '2001-01-01T00:00:00Z' and < '2004-01-01T00:00:00Z'",
        column_type="TIMESTAMP",
    )
    low = int(datetime(2001, 1, 1, tzinfo=timezone.utc).timestamp()) * 1000
    high = int(datetime(2004, 1, 1, tzinfo=timezone.utc).timestamp()) * 1000
    bins = plan.columns[0].statistics.bins
    assert bins is not None and (int(bins[0]), int(bins[-1])) == (low + 1, high - 1)
    drawn = [row[0] for row in generate_rows(plan, root=_root(plan))]
    assert all(low < value < high for value in drawn)


def test_the_quoted_range_is_the_contracts_and_the_note_names_the_ends_drawn() -> None:
    column = expose_from_schema(
        [
            {
                "name": "shipped_on",
                "type": "DATE",
                "validationRules": _range_rule(">= '2001-01-01' and < '2006-01-01'"),
            }
        ]
    )
    [held] = declared_ranges_of({}, column)
    assert held.text == ">= 2001-01-01 and < 2006-01-01"
    assert (held.low, held.high) == ("2001-01-01", "2005-12-31")
    # `verify` counts a row on the excluded end, as the contract (and forge's own test) does.
    problems = range_problems(
        "widget_stream", ["shipped_on"], [["2001-01-01"], ["2005-12-31"], ["2006-01-01"]], [held]
    )
    assert problems == [
        "widget_stream.shipped_on: 1 of 3 rows are outside the declared range "
        "(>= 2001-01-01 and < 2006-01-01)"
    ]
    plan = _plan(PAST, ">= '2001-01-01' and < '2006-01-01'")
    [note] = range_cut_notes("widget_stream", plan.columns)
    assert "2001-01-01 to 2005-12-31" in note


def test_a_strict_range_that_holds_no_day_is_flx_3403_at_plan_and_never_a_crash() -> None:
    with pytest.raises(FluxError) as refused:
        _plan(PAST, "> '2005-01-01' and < '2005-01-02'")
    assert refused.value.code.code == "FLX-3403"
    # Not cut (no statistics reach a column of another kind), it only quotes the contract.
    column = expose_from_schema(
        [
            {
                "name": "shipped_on",
                "type": "DATE",
                "validationRules": _range_rule("> '2005-01-01' and < '2005-01-02'"),
            }
        ]
    )
    [held] = declared_ranges_of({}, column)
    assert held.text == "> 2005-01-01 and < 2005-01-02"


@pytest.mark.parametrize(
    "text",
    [
        "<= '9999-12-31'",
        ">= '2001-01-01' and < 'last spring'",
        "between 1 and 10",
        "between 'last spring' and 'this spring'",
        "in ('2001-01-01', '2002-01-01')",
    ],
)
def test_the_grammar_declares_nothing_for_any_other_text(text: str) -> None:
    column = expose_from_schema(
        [{"name": "shipped_on", "type": "DATE", "validationRules": _range_rule(text)}]
    ).columns[0]
    assert column.declared_range is None


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [
        (">= '2001-01-01'", ("2001-01-01", 0), None),
        ("> '2001-01-01'", ("2001-01-01", 1), None),
        ("<= '2005-12-31'", None, ("2005-12-31", 0)),
        ("< '2005-12-31'", None, ("2005-12-31", -1)),
        (">= '2001-01-01' and <= '9999-12-31'", ("2001-01-01", 0), None),
        ("between '2001-01-01' and '9999-12-31'", ("2001-01-01", 0), None),
        ("between '2001-01-01' and '9999-01-01T00:00:00Z'", ("2001-01-01", 0), None),
    ],
)
def test_a_range_with_one_end_is_declared_and_the_open_end_is_no_end(
    text: str, low: Optional[Tuple[str, int]], high: Optional[Tuple[str, int]]
) -> None:
    entry = {"name": "shipped_on", "type": "DATE", "validationRules": _range_rule(text)}
    column = expose_from_schema([entry]).columns[0]
    assert column.declared_range == (
        None if low is None else _days(low[0]) * DAY_MS + low[1],
        None if high is None else _days(high[0]) * DAY_MS + high[1],
    )
    # None of these is a range `init` reads: `declared_instants` needs both ends.
    assert declared_instants(entry) is None
    bounds = declared_range_bounds(entry)
    assert bounds is not None
    assert (bounds.low is None, bounds.high is None) == (low is None, high is None)


def test_a_range_with_both_ends_open_declares_nothing() -> None:
    entry = {"validationRules": _range_rule("<= '9999-12-31'")}
    assert declared_range_bounds(entry) is None and declared_instants(entry) is None


def test_a_one_sided_range_cuts_the_draw_and_names_only_its_end() -> None:
    values = _values(_plan(PAST, ">= '2003-01-01'"))
    assert min(values) >= date(2003, 1, 1) and max(values) > date(2006, 1, 1)
    values = _values(_plan(PAST, "<= '2004-12-31'"))
    assert max(values) <= date(2004, 12, 31) and min(values) < date(2001, 1, 1)
    [held] = declared_ranges_of(
        {},
        expose_from_schema(
            [{"name": "d", "type": "DATE", "validationRules": _range_rule(">= '2003-01-01'")}]
        ),
    )
    assert held.text == ">= 2003-01-01"
    assert range_problems("t", ["d"], [["2002-12-31"], ["2003-01-01"], ["9000-01-01"]], [held]) == [
        "t.d: 1 of 3 rows are outside the declared range (>= 2003-01-01)"
    ]
    [note] = range_cut_notes("widget_stream", _plan(PAST, ">= '2003-01-01'").columns)
    assert "the contract's range from 2003-01-01:" in note


def test_a_high_only_timestamp_range_is_counted_at_the_millisecond() -> None:
    column = expose_from_schema(
        [
            {
                "name": "at",
                "type": "TIMESTAMP",
                "validationRules": _range_rule("< '2004-01-01T00:00:00Z'"),
            }
        ]
    )
    [held] = declared_ranges_of({}, column)
    assert held.text == "< 2004-01-01T00:00:00.000Z"
    outside = range_problems(
        "t", ["at"], [["2003-12-31T23:59:59.999Z"], ["2004-01-01T00:00:00.000Z"]], [held]
    )
    assert outside == [
        "t.at: 1 of 2 rows are outside the declared range (< 2004-01-01T00:00:00.000Z)"
    ]


def test_only_a_date_or_timestamp_column_holds_a_declared_range() -> None:
    schema: List[Mapping[str, Any]] = [
        {"name": "shipped_on", "type": "DATE", "validationRules": _range_rule(RANGE)},
        {"name": "count", "type": "INT", "validationRules": _range_rule("between 1 and 9")},
        {"name": "plain", "type": "DATE"},
    ]
    held = [column.declared_range for column in expose_from_schema(schema).columns]
    assert held[0] is not None and held[1] is None and held[2] is None


def test_the_column_path_draws_a_cut_column_as_the_per_row_path_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", 64)
    for declared, rule in (
        (PAST, RANGE),
        ({"range": {"min": "2000-01-01", "max": "2009-12-31"}}, RANGE),
        (
            {"family": "rate", "rate": "0.01", "range": {"min": "2000-01-01", "max": "2009-12-31"}},
            RANGE,
        ),
    ):
        plan = _plan(declared, rule, rows=300)
        by_columns, by_rows, taken = _both(
            monkeypatch, partial(generate_rows, plan, root=_root(plan))
        )
        assert by_columns == by_rows
        assert taken == [True]


def _between_5_and_100() -> ColumnRange:
    return ColumnRange("cost", "number", ">= 5 and <= 100", Fraction(5), Fraction(100))


def _counted(cells: List[Any], held: ColumnRange) -> int:
    found = range_problems("visit", ["cost"], [[cell] for cell in cells], [held])
    return int(found[0].split(": ")[1].split(" of ")[0]) if found else 0


def test_a_cell_with_a_huge_exponent_is_counted_outside_without_expanding_it() -> None:
    # A handed-over run can carry any text in a cell; Fraction("1e-30000000") took 126 s to parse.
    held = _between_5_and_100()
    started = time.perf_counter()
    counted = _counted(
        ["1e999999999", "-1e-999999999", "1e-999999999", "1e99999999999999999999"], held
    )
    assert time.perf_counter() - started < 2
    # every one is outside 5..100: 1e999999999 and the 22-digit exponent above, and both near zero
    assert counted == 4


def test_a_huge_exponent_cell_beside_ordinary_cells_leaves_their_answers_alone() -> None:
    held = _between_5_and_100()
    cells = [
        "5",
        "100",
        "4.999999",
        "100.000001",
        "5e0",
        "1e2",
        "0.05e2",
        "-1e-999999999",
        "1e999999999",
    ]
    started = time.perf_counter()
    counted = _counted(cells, held)
    assert time.perf_counter() - started < 2
    assert counted == 4  # 4.999999, 100.000001, -1e-999999999, 1e999999999


def test_a_cell_that_is_not_a_finite_number_is_outside_the_range() -> None:
    held = _between_5_and_100()
    cells = ["nan", "NaN", "sNaN", "inf", "-Infinity", "abc", "", " ", "1.2.3", True, [], {}]
    assert _counted(cells, held) == len(cells)
    assert _counted([None, None, "50"], held) == 0  # a null cell is no value


def test_a_strict_bound_and_a_bound_of_a_thousand_digits_compare_exactly() -> None:
    long_bound = Fraction("0." + "1" * 1000)
    held = ColumnRange("cost", "number", "", None, long_bound, False, True)
    assert _counted(["0." + "1" * 1000, "0." + "1" * 999, "0." + "1" * 1000 + "1"], held) == 2
    strict = ColumnRange("cost", "number", "", Fraction(5), None, True, False)
    assert _counted(["5", "5.0", "5.000000001", "-1e999999999"], strict) == 3


def test_a_column_whose_contract_name_is_not_nfc_keeps_its_number_range() -> None:
    decomposed, composed = "café", "café"
    schema: List[Mapping[str, Any]] = [
        {
            "name": decomposed,
            "type": "INT",
            "validationRules": _range_rule("between 1 and 5"),
        },
        {"name": "d", "type": "DATE", "validationRules": _range_rule(RANGE)},
    ]
    compiled = expose_from_schema(schema)
    assert [column.name for column in compiled.columns][0] == composed
    held = declared_ranges_of({"contract": {"schema": schema}}, compiled)
    assert [item.column for item in held] == [composed, "d"]
    assert range_problems("t", [composed], [[3], [9]], held[:1]) == [
        "t.café: 1 of 2 rows are outside the declared range (>= 1 and <= 5)"
    ]


def test_an_inclusive_range_whose_ends_meet_holds_that_one_day() -> None:
    rule = "between '2003-05-05' and '2003-05-05'"
    entry = {"name": "shipped_on", "type": "DATE", "validationRules": _range_rule(rule)}
    day = _days("2003-05-05") * DAY_MS
    assert declared_range_bounds(entry) == RangeBounds(day, day, False, False)
    assert expose_from_schema([entry]).columns[0].declared_range == (day, day)
    assert set(_values(_plan(PAST, rule))) == {date(2003, 5, 5)}
    [held] = declared_ranges_of({}, expose_from_schema([entry]))
    assert held.text == ">= 2003-05-05 and <= 2003-05-05"
    assert range_problems("t", ["shipped_on"], [["2003-05-05"], ["2003-05-06"]], [held]) == [
        "t.shipped_on: 1 of 2 rows are outside the declared range (>= 2003-05-05 and <= 2003-05-05)"
    ]


def test_an_inclusive_timestamp_range_whose_ends_meet_holds_that_one_instant() -> None:
    rule = ">= '2003-05-05T10:00:00Z' and <= '2003-05-05T10:00:00Z'"
    declared = {
        "bins": ["2003-01-01T00:00:00Z", "2004-01-01T00:00:00Z"],
        "binWeights": ["1"],
    }
    plan = _plan(declared, rule, column_type="TIMESTAMP")
    moment = int(datetime(2003, 5, 5, 10, tzinfo=timezone.utc).timestamp()) * 1000
    assert plan.columns[0].column.declared_range == (moment, moment)
    assert len({row[0] for row in generate_rows(plan, root=_root(plan))}) == 1


@pytest.mark.parametrize(
    ("rule", "spelled"),
    [
        ("between '2005-01-02' and '2005-01-01'", ">= 2005-01-02 and <= 2005-01-01"),
        (">= '2005-01-01' and < '2005-01-01'", ">= 2005-01-01 and < 2005-01-01"),
        ("> '2005-01-01' and <= '2005-01-01'", "> 2005-01-01 and <= 2005-01-01"),
        ("> '2005-01-01' and < '2005-01-01'", "> 2005-01-01 and < 2005-01-01"),
    ],
)
@pytest.mark.parametrize(
    ("declared", "version"), [(PAST, GENERATOR_VERSION_2), ({}, GENERATOR_VERSION)]
)
def test_an_empty_range_is_flx_3403_at_plan_naming_the_column_and_both_ends(
    rule: str, spelled: str, declared: Dict[str, Any], version: str
) -> None:
    # On both kernels, and for a column that declares no statistics.
    held = re.escape(f"widget_stream.shipped_on: the contract's range ({spelled}) holds no value")
    with pytest.raises(FluxError, match=f"FLX-3403.*{held}"):
        _plan(declared, rule, version=version)


def test_the_empty_ranges_are_ranges_to_declared_range_bounds_and_none_to_declared_instants() -> (
    None
):
    for rule in (
        "between '2005-01-02' and '2005-01-01'",
        ">= '2005-01-01' and < '2005-01-01'",
        "between '2005-01-01' and '2005-01-01'",
    ):
        entry = {"validationRules": _range_rule(rule)}
        assert declared_range_bounds(entry) is not None
        assert declared_instants(entry) is None
