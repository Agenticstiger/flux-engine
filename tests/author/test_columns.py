# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The column rules of ``fluxsim init --profile``, one profile entry at a time.

Each rule turns what a profile publishes about a column, and what the contract
declares, into a declaration and the basis of each of its numbers. Pinned here:
the order the rules run in (a statistic, then the metrics, then the contract,
then an assumption), that the keys the fidelity check reads come only from
published values or the contract, and the arithmetic of shares and weights.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

import pytest

from flux_engine.author.columns import (
    OPAQUE_LEVEL_CAP,
    ColumnContext,
    ColumnPlan,
    plan_column,
    share_texts,
)
from flux_engine.seam.contract import CompiledColumn, parse_logical_type
from flux_engine.seam.parameters import Source

pytestmark = pytest.mark.unit

PRODUCT = "shop.orders"


def _metric(definition: str, value: Any = None, status: str = "F") -> Dict[str, Any]:
    metric: Dict[str, Any] = {
        "definition": definition,
        "status": status,
        "disclosure": "acro-default",
        "sampleSize": {"min": 600, "max": 600},
    }
    if status == "F":
        metric["values" if definition == "quantiles/1" else "value"] = value
    return metric


def _histogram(
    bins: List[str], counts: List[Optional[int]], statuses: Optional[List[str]] = None
) -> Dict[str, Any]:
    group: Dict[str, Any] = {"group": "", "k": 600, "binCounts": counts, "status": "F"}
    if statuses is not None:
        group["cellStatus"] = statuses
    return {
        "definition": "marginalHistogram/1",
        "bins": bins,
        "sampleSize": {"min": 600, "max": 600},
        "disclosure": "acro-default",
        "groups": [group],
    }


def _levels(levels: List[str], counts: List[Optional[int]], statuses: List[str]) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "levels": levels,
        "sampleSize": {"min": 600, "max": 600},
        "disclosure": "acro-default",
        "groups": [
            {"group": "", "k": 600, "levelCounts": counts, "cellStatus": statuses, "status": "F"}
        ],
    }


def _plan(
    declared_type: str,
    entry: Optional[Dict[str, Any]],
    *,
    required: bool = False,
    key: bool = False,
    foreign: Optional[str] = None,
    rows: int = 600,
) -> ColumnPlan:
    column = CompiledColumn(
        name="c",
        logical_type=parse_logical_type(declared_type),
        required=required,
        declared_type=declared_type,
        key=key,
    )
    return plan_column(
        ColumnContext(
            column=column,
            expose_index=0,
            schema_index=2,
            product_ref=PRODUCT,
            profile_tokens=("tables", "orders", "columns", "c"),
            entry=entry,
            rows=rows,
            minimum_cell=10,
            key=key,
            foreign=foreign,
        )
    )


def _sources(plan: ColumnPlan) -> Dict[Tuple[str, ...], Source]:
    return {label.tokens: label.basis.source for label in plan.labels}


# ---------------------------------------------------------------------------
# Shares
# ---------------------------------------------------------------------------


def test_shares_are_exact_when_they_can_be_and_add_up_to_one() -> None:
    assert share_texts([Fraction(1)] * 4) == ["0.25"] * 4
    thirds = share_texts([Fraction(1)] * 3)
    assert sum(Decimal(text) for text in thirds) == 1
    assert thirds[0] == "0.333333334"  # the residue goes to the first of equals
    assert share_texts([Fraction(150), Fraction(450)]) == ["0.25", "0.75"]
    with pytest.raises(ValueError):
        share_texts([Fraction(0), Fraction(0)])


# ---------------------------------------------------------------------------
# Numbers, dates and timestamps
# ---------------------------------------------------------------------------


def test_quartiles_between_a_published_min_and_max_are_a_cited_histogram() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "10"),
            "max": _metric("max/1", "100"),
            "quantiles": _metric("quantiles/1", {"0.25": "20", "0.5": "35", "0.75": "50"}),
            "nullCount": _metric("nullCount/1", 30),
            "count": _metric("count/1", 570),
        },
    }
    plan = _plan("decimal(10,2)", entry)
    assert plan.declaration["range"] == {"min": "10", "max": "100"}
    assert plan.declaration["bins"] == ["10", "20", "35", "50", "100"]
    assert plan.declaration["binWeights"] == ["0.25"] * 4
    assert plan.declaration["nullFraction"] == "0.05"
    sources = _sources(plan)
    assert set(sources.values()) == {Source.PROFILE}
    assert plan.properties[0].document["property"] == "marginalHistogram"
    assert plan.properties[0].scorable is False  # the profile holds no marginalHistogram/1


def test_a_withheld_min_folds_its_tail_into_the_first_bin_and_declares_no_range() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", status="A"),
            "max": _metric("max/1", "100"),
            "quantiles": _metric("quantiles/1", {"0.25": "20", "0.5": "35", "0.75": "50"}),
        },
    }
    plan = _plan("int", entry)
    assert "range" not in plan.declaration  # the check never reads a guessed bound
    assert plan.declaration["bins"] == ["20", "35", "50", "100"]
    assert plan.declaration["binWeights"] == ["0.5", "0.25", "0.25"]
    assert _sources(plan)[("binWeights",)] is Source.ASSUMPTION
    assert plan.properties == []


def test_a_published_histogram_comes_first_and_a_withheld_cell_is_half_the_minimum() -> None:
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "0"), "max": _metric("max/1", "120")},
        "statistics": [_histogram(["0", "12", "48", "120"], [300, None, 295], ["F", "A", "F"])],
    }
    plan = _plan("int", entry)
    assert plan.declaration["bins"] == ["0", "12", "48", "120"]
    assert plan.declaration["binWeights"] == ["300", "5", "295"]
    sources = _sources(plan)
    assert sources[("binWeights", "1")] is Source.ASSUMPTION
    assert sources[("binWeights", "0")] is Source.PROFILE
    assert sources[("range", "min")] is Source.PROFILE
    assert [prop.scorable for prop in plan.properties] == [True]


def test_a_histogram_with_no_mass_is_passed_over() -> None:
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "1"), "max": _metric("max/1", "9")},
        "statistics": [_histogram(["1", "5", "9"], [0, 0])],
    }
    plan = _plan("int", entry)
    assert plan.declaration["bins"] == ["1", "9"]  # from the metrics instead
    assert plan.declaration["binWeights"] == ["1"]


def _mean_statistic(mean: int) -> Dict[str, Any]:
    return {
        "definition": "mean/1",
        "sampleSize": {"min": 600, "max": 600},
        "disclosure": "acro-default",
        "groups": [{"group": "", "k": 600, "Sx": str(mean * 600 * 2**16), "status": "F"}],
    }


def test_a_mean_tolerance_is_a_tenth_of_the_published_mean() -> None:
    # A range that spans zero takes the tenth as an absolute margin (FLX-5002).
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "-5"), "max": _metric("max/1", "15")},
        "statistics": [_histogram(["-5", "0", "15"], [300, 300]), _mean_statistic(4)],
    }
    plan = _plan("int", entry)
    tolerances = {prop.document["property"]: prop.document["tolerance"] for prop in plan.properties}
    assert tolerances["mean"] == {"type": "absolute", "value": "0.40"}
    entry["metrics"]["min"] = _metric("min/1", "5")
    entry["statistics"] = [_histogram(["5", "10", "15"], [300, 300]), _mean_statistic(10)]
    plan = _plan("int", entry)
    tolerances = {prop.document["property"]: prop.document["tolerance"] for prop in plan.properties}
    assert tolerances["mean"] == {"type": "relative", "value": "0.10"}


def test_a_mean_of_zero_gives_no_mean_property() -> None:
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "-5"), "max": _metric("max/1", "5")},
        "statistics": [_histogram(["-5", "0", "5"], [300, 300]), _mean_statistic(0)],
    }
    kinds = [prop.document["property"] for prop in _plan("int", entry).properties]
    assert kinds == ["marginalHistogram"]


def test_a_histogram_s_range_is_its_edges_even_inside_a_published_min_and_max() -> None:
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "10"), "max": _metric("max/1", "100")},
        "statistics": [_histogram(["0", "50", "200"], [300, 300])],
    }
    plan = _plan("decimal(10,2)", entry)
    assert plan.declaration["range"] == {"min": "0", "max": "200"}  # bins partition the range


def test_a_histogram_is_shaped_to_its_mean_and_keeps_every_bin_count() -> None:
    entry = {
        "domain": "number",
        "metrics": {},
        "statistics": [_histogram(["0", "10", "1000"], [540, 60]), _mean_statistic(14)],
    }
    plan = _plan("int", entry)
    bins, weights = plan.declaration["bins"], plan.declaration["binWeights"]
    assert bins[0] == "0" and bins[-1] == "1000" and "10" in bins
    assert sorted(weight for weight in weights if weight != "0") == ["540", "60"]
    sources = _sources(plan)
    kept = [position for position, weight in enumerate(weights) if weight != "0"]
    assert all(sources[("binWeights", str(position))] is Source.PROFILE for position in kept)
    total = sum(Fraction(Decimal(w)) for w in weights)
    mean = sum(
        (
            Fraction(Decimal(w)) / total * (Fraction(Decimal(a)) + Fraction(Decimal(b))) / 2
            for w, a, b in zip(weights, bins[:-1], bins[1:], strict=True)
        ),
        Fraction(0),
    )
    assert abs(mean - 14) < 1
    tolerances = {prop.document["property"]: prop.document["tolerance"] for prop in plan.properties}
    assert tolerances["mean"] == {"type": "absolute", "value": "1.40"}


def test_a_withheld_distinct_count_is_half_the_minimum_cell_of_levels() -> None:
    entry = {
        "domain": "category",
        "metrics": {"distinctCount": _metric("distinctCount/1", status="A")},
    }
    assert len(_plan("string", entry).declaration["levels"]) == 5
    assert len(_plan("string", {"domain": "category", "metrics": {}}).declaration["levels"]) == 1


def test_a_timestamp_with_no_published_order_statistic_is_left_to_the_window() -> None:
    entry = {
        "domain": "timestamp",
        "metrics": {"min": _metric("min/1", status="A"), "max": _metric("max/1", status="A")},
    }
    plan = _plan("timestamp", entry)
    assert set(plan.declaration) == {"nullFraction"}
    assert any("time window" in sentence for sentence in plan.assumptions)


def test_a_timestamp_range_is_rfc3339_and_spans_the_run() -> None:
    entry = {
        "domain": "timestamp",
        "metrics": {
            "min": _metric("min/1", "1782864000000"),
            "max": _metric("max/1", "1790812799000"),
        },
    }
    plan = _plan("timestamp", entry)
    assert plan.declaration["range"] == {
        "min": "2026-07-01T00:00:00.000Z",
        "max": "2026-09-30T23:59:59.000Z",
    }
    assert plan.time_span == (1782864000000, 1790812799000)


def test_an_integer_key_is_an_assumed_span_the_check_does_not_read() -> None:
    plan = _plan("int", {"domain": "number", "metrics": {}}, key=True, rows=50)
    assert plan.declaration["bins"] == ["1", "50"]
    assert "range" not in plan.declaration
    assert _sources(plan)[("bins",)] is Source.ASSUMPTION


def test_a_sum_and_count_with_no_order_statistic_spread_around_the_mean() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "sum": _metric("sum/1", "6000"),
            "count": _metric("count/1", 600),
            "distinctCount": _metric("distinctCount/1", 5),
        },
    }
    plan = _plan("int", entry)
    assert plan.declaration["bins"] == ["8", "12"]
    assert _sources(plan)[("bins",)] is Source.ASSUMPTION


def test_nothing_published_is_a_placeholder_said_to_be_one() -> None:
    plan = _plan("int", None)
    assert plan.declaration["levels"] == {"0": "1"}
    assert "placeholder" in plan.how


# ---------------------------------------------------------------------------
# Categorical columns
# ---------------------------------------------------------------------------


def test_an_enum_is_the_contract_s_support_and_its_size_is_cited_there() -> None:
    plan = _plan("enum('DE','FR','GB')", {"domain": "category", "metrics": {}})
    assert plan.declaration["support"] == "declared"
    assert plan.declaration["maxCardinality"] == 3
    (label,) = [label for label in plan.labels if label.tokens == ("maxCardinality",)]
    assert label.basis.source is Source.CONTRACT
    assert label.basis.contract == (PRODUCT, ("exposes", "0", "contract", "schema", "2", "type"))
    assert "levels" not in plan.declaration  # equal weights, the generator's own default


def test_level_counts_weigh_the_enum_and_a_withheld_count_is_half_the_minimum() -> None:
    entry = {
        "domain": "category",
        "statistics": [_levels(["DE", "FR", "GB"], [300, None, 295], ["F", "A", "F"])],
    }
    plan = _plan("enum('DE','FR','GB')", entry)
    assert plan.declaration["levels"] == {"DE": "0.5", "FR": "0.008333333", "GB": "0.491666667"}
    assert sum(Decimal(share) for share in plan.declaration["levels"].values()) == 1
    sources = _sources(plan)
    assert sources[("levels", "FR")] is Source.ASSUMPTION
    assert sources[("levels", "DE")] is Source.PROFILE
    assert [prop.document["property"] for prop in plan.properties] == ["levelHistogram"]


def test_a_unique_string_is_an_identifier_unless_it_is_a_foreign_key() -> None:
    entry = {
        "domain": "category",
        "metrics": {
            "count": _metric("count/1", 600),
            "distinctCount": _metric("distinctCount/1", 600),
        },
    }
    assert _plan("string", entry).declaration == {"unique": True}
    foreign = _plan("string", entry, foreign="customers")
    assert "unique" not in foreign.declaration
    assert "a foreign key to customers" in foreign.how


def test_a_key_the_contract_declares_is_derived_and_declares_nothing() -> None:
    plan = _plan("string", None, key=True)
    assert plan.declaration == {}
    assert "the contract makes it a key" in plan.how


def test_opaque_levels_stand_for_values_the_profile_never_publishes() -> None:
    entry = {
        "domain": "category",
        "metrics": {"distinctCount": _metric("distinctCount/1", 4)},
    }
    plan = _plan("string", entry)
    assert plan.declaration["levels"] == {
        "c-1": "0.25",
        "c-2": "0.25",
        "c-3": "0.25",
        "c-4": "0.25",
    }
    assert "support" not in plan.declaration  # the check never holds real data to them
    assert _sources(plan)[("levels",)] is Source.ASSUMPTION
    big = _plan(
        "string",
        {"domain": "category", "metrics": {"distinctCount": _metric("distinctCount/1", 5000)}},
    )
    assert len(big.declaration["levels"]) == OPAQUE_LEVEL_CAP


def test_a_boolean_without_counts_is_drawn_half_the_time() -> None:
    plan = _plan("boolean", None)
    assert plan.declaration["levels"] == {"false": "0.5", "true": "0.5"}


# ---------------------------------------------------------------------------
# Nulls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metrics", "expected", "source"),
    [
        ({"nullCount": _metric("nullCount/1", 0)}, "0", Source.PROFILE),
        ({"nullCount": _metric("nullCount/1", status="A")}, "0", Source.ASSUMPTION),
        ({}, "0", Source.ASSUMPTION),
    ],
)
def test_the_null_fraction_rests_on_the_null_count(
    metrics: Dict[str, Any], expected: str, source: Source
) -> None:
    plan = _plan("string", {"domain": "category", "metrics": metrics})
    assert plan.declaration["nullFraction"] == expected
    assert _sources(plan)[("nullFraction",)] is source


def test_a_required_column_declares_no_null_fraction() -> None:
    entry = {"domain": "category", "metrics": {"nullCount": _metric("nullCount/1", 20)}}
    plan = _plan("string", entry, required=True)
    assert "nullFraction" not in plan.declaration
    assert any("the contract makes the column required" in text for text in plan.assumptions)


# ---------------------------------------------------------------------------
# A table with nothing published for the check
# ---------------------------------------------------------------------------


def test_an_assumed_span_becomes_the_checked_range_only_when_asked() -> None:
    from flux_engine.author.columns import assumed_check
    from flux_engine.seam.contract import LogicalKind

    plan = _plan("int", {"domain": "number", "metrics": {}}, key=True, rows=35)
    assert "range" not in plan.declaration
    prop = assumed_check(plan, LogicalKind.INT)
    assert prop is not None
    assert plan.declaration["range"] == {"min": "1", "max": "35"}
    assert _sources(plan)[("range",)] is Source.ASSUMPTION
    assert prop.document == {
        "property": "mean",
        "column": "c",
        "tolerance": {"type": "absolute", "value": "3.40"},
    }
    assert prop.scorable is False


def test_assumed_levels_become_the_checked_support_only_when_asked() -> None:
    from flux_engine.author.columns import assumed_check
    from flux_engine.seam.contract import LogicalKind

    entry = {"domain": "category", "metrics": {"distinctCount": _metric("distinctCount/1", 2)}}
    plan = _plan("string", entry)
    prop = assumed_check(plan, LogicalKind.STRING)
    assert prop is not None
    assert plan.declaration["support"] == {"values": ["c-1", "c-2"]}
    assert plan.declaration["maxCardinality"] == 2
    assert prop.document["property"] == "support"
    one_value = _plan("int", {"domain": "number", "metrics": {"min": _metric("min/1", "4")}})
    assert assumed_check(one_value, LogicalKind.INT) is None


def test_a_name_that_is_not_plain_text_never_reaches_a_reason_as_it_is() -> None:
    column = CompiledColumn(
        name="c\x1b[2Jx",
        logical_type=parse_logical_type("string"),
        required=False,
        declared_type="string",
    )
    plan = plan_column(
        ColumnContext(
            column=column,
            expose_index=0,
            schema_index=0,
            product_ref=PRODUCT,
            profile_tokens=("tables", "orders", "columns", column.name),
            entry={
                "domain": "category",
                "metrics": {"distinctCount": _metric("distinctCount/1", 2)},
            },
            rows=600,
            minimum_cell=10,
        )
    )
    reasons = [label.basis.reason for label in plan.labels if label.basis.reason]
    assert reasons and all(reason.isprintable() for reason in reasons)
    assert "'c\\x1b[2Jx'-<n>" in reasons[0]


# ---------------------------------------------------------------------------
# Keys: what a key layout reads, and nulls a key or an inner join cannot hold
# ---------------------------------------------------------------------------


def _keyed_plan(declared_type: str, entry: Optional[Dict[str, Any]], **flags: Any) -> ColumnPlan:
    column = CompiledColumn(
        name="c",
        logical_type=parse_logical_type(declared_type),
        required=False,
        declared_type=declared_type,
        key=bool(flags.get("key")),
    )
    return plan_column(
        ColumnContext(
            column=column,
            expose_index=0,
            schema_index=0,
            product_ref=PRODUCT,
            profile_tokens=("tables", "orders", "columns", "c"),
            entry=entry,
            rows=flags.get("rows", 600),
            minimum_cell=10,
            **{name: value for name, value in flags.items() if name != "rows"},
        )
    )


def test_a_column_of_a_key_or_an_inner_join_declares_no_null_fraction() -> None:
    entry = {"domain": "category", "metrics": {"nullCount": _metric("nullCount/1", status="A")}}
    assert "nullFraction" not in _keyed_plan("string", entry, keyed=True).declaration
    assert "nullFraction" not in _keyed_plan("string", entry, inner=True).declaration
    assert _keyed_plan("string", entry).declaration["nullFraction"] == "0"


def test_a_column_of_a_key_is_never_declared_an_identifier_of_its_own() -> None:
    entry = {
        "domain": "category",
        "metrics": {
            "count": _metric("count/1", 600),
            "distinctCount": _metric("distinctCount/1", 600),
        },
    }
    plan = _keyed_plan("string", entry, keyed=True)
    assert "unique" not in plan.declaration


def test_a_decimal_key_is_laid_out_over_an_assumed_span_too() -> None:
    plan = _keyed_plan("decimal(18,2)", {"domain": "number", "metrics": {}}, key=True, rows=40)
    assert plan.declaration["bins"] == ["1", "40"]
    assert plan.key_span and "levels" not in plan.declaration


def test_an_assumed_key_span_widens_to_the_foreign_keys_that_reference_it() -> None:
    from flux_engine.author.columns import declared_span, widen_key_span

    child = _plan(
        "int",
        {
            "domain": "number",
            "metrics": {
                "min": _metric("min/1", "1001"),
                "max": _metric("max/1", "1500"),
                "quantiles": _metric("quantiles/1", {"0.5": "1200"}),
            },
        },
    )
    assert declared_span(child) == [Decimal(v) for v in ("1001", "1500", "1001", "1200", "1500")]
    key = _keyed_plan("int", {"domain": "number", "metrics": {}}, key=True, rows=800)
    widen_key_span(key, declared_span(child), 800)
    assert key.declaration["bins"] == ["1001", "1800"]  # at least one value per row
    assert "foreign keys that reference it" in key.how
    reasons = {label.basis.reason for label in key.labels if label.tokens == ("bins",)}
    assert len(reasons) == 1 and "foreign keys that reference it" in reasons.pop()
    assert declared_span(_plan("int", None)) == []  # a placeholder says nothing


def test_a_key_span_from_published_statistics_is_not_widened() -> None:
    from flux_engine.author.columns import widen_key_span

    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "1"), "max": _metric("max/1", "9")},
        "statistics": [_histogram(["1", "5", "9"], [300, 300])],
    }
    plan = _keyed_plan("int", entry, key=True)
    widen_key_span(plan, [Decimal(100)], 600)
    assert plan.declaration["bins"] == ["1", "5", "9"]


# ---------------------------------------------------------------------------
# A histogram shaped to the published mean
# ---------------------------------------------------------------------------


def _drawn(edges: List[Decimal], weights: List[Fraction], step: str) -> Fraction:
    from flux_engine.author.columns import _drawn_mean

    return _drawn_mean([Fraction(edge) for edge in edges], weights, Fraction(Decimal(step)))


def test_quartile_bins_are_shaped_to_the_published_mean() -> None:
    from flux_engine.author.columns import shape_to_mean

    edges = [Decimal(v) for v in ("10", "20", "35", "50", "100")]
    quarter = [Fraction(1, 4)] * 4
    # Drawn on the cent grid, these quartile bins average 39.99625; the guide's orders 46.25.
    assert _drawn(edges, quarter, "0.01") == Fraction(3199700, 80000)
    shaped = shape_to_mean(edges, quarter, Fraction(4625, 100), Decimal("0.01"))
    assert shaped is not None
    new_edges, weights, inserted = shaped
    assert [str(edge) for edge in new_edges] == [
        "10",
        "15.56",
        "20",
        "28.34",
        "35",
        "43.34",
        "50",
        "77.79",
        "100",
    ]
    assert weights == [0, quarter[0], 0, quarter[0], 0, quarter[0], 0, quarter[0]]
    assert inserted == [False, True, False, True, False, True, False, True, False]
    # The mean as the generator draws it: each bin evenly over its cents, the last one closed.
    assert _drawn(new_edges, weights, "0.01") == Fraction(4625, 100)


def test_a_mean_below_the_midpoints_empties_the_top_of_each_bin() -> None:
    from flux_engine.author.columns import shape_to_mean

    shaped = shape_to_mean([Decimal(0), Decimal(10)], [Fraction(1)], Fraction(2), Decimal(1))
    # 0 to 4, drawn evenly, average 2.
    assert shaped == ([Decimal(0), Decimal(5), Decimal(10)], [Fraction(1), 0], [False, True, False])


def test_an_integer_column_with_a_small_mean_is_shaped_on_its_grid() -> None:
    from flux_engine.author.columns import shape_to_mean

    # The bins are half-open and drawn over whole numbers: 1, 2, 3 to 4, and 5 to 20.
    edges = [Decimal(v) for v in ("1", "2", "3", "5", "20")]
    quarter = [Fraction(1, 4)] * 4
    assert _drawn(edges, quarter, "1") == Fraction(19, 4)
    shaped = shape_to_mean(edges, quarter, Fraction(9, 2), Decimal(1))
    assert shaped is not None
    assert _drawn(shaped[0], shaped[1], "1") == Fraction(9, 2)


def test_a_mean_the_grid_cannot_reach_is_drawn_as_near_as_it_allows() -> None:
    from flux_engine.author.columns import shape_to_mean

    # Bins two wide on whole numbers: drawing each bin's top value is the furthest a cut
    # can go, and that averages 4.125, short of 4.9 but nearer than the even 3.625.
    edges = [Decimal(v) for v in (0, 2, 4, 6, 8)]
    shaped = shape_to_mean(edges, [Fraction(1)] * 4, Fraction(49, 10), Decimal(1))
    assert shaped is not None
    assert _drawn(shaped[0], shaped[1], "1") == Fraction(33, 8)


def test_no_shape_when_the_bins_already_give_the_mean_or_cannot() -> None:
    from flux_engine.author.columns import shape_to_mean

    edges, weights = [Decimal(0), Decimal(10)], [Fraction(1)]
    assert shape_to_mean(edges, weights, Fraction(5), Decimal(1)) is None
    assert shape_to_mean(edges, weights, Fraction(11), Decimal(1)) is None


def test_order_statistics_with_a_published_mean_carry_a_scorable_mean_property() -> None:
    mean = {
        "definition": "mean/1",
        "sampleSize": {"min": 600, "max": 600},
        "disclosure": "acro-default",
        "groups": [{"group": "", "k": 600, "Sx": str(27750 * 2**16), "status": "F"}],
    }
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "10"),
            "max": _metric("max/1", "100"),
            "quantiles": _metric("quantiles/1", {"0.25": "20", "0.5": "35", "0.75": "50"}),
        },
        "statistics": [mean],
    }
    plan = _plan("decimal(10,2)", entry)
    assert plan.declaration["bins"][1] == "15.56"  # shaped to Sx / k, the mean of 46.25
    sources = _sources(plan)
    assert sources[("bins", "1")] is Source.ASSUMPTION
    assert sources[("bins", "2")] is Source.PROFILE
    assert sources[("binWeights", "0")] is Source.ASSUMPTION
    assert sources[("binWeights", "1")] is Source.PROFILE
    kinds = {prop.document["property"]: prop.scorable for prop in plan.properties}
    assert kinds == {"marginalHistogram": False, "mean": True}
    assert "shaped to the published mean" in plan.how


# ---------------------------------------------------------------------------
# What the check cannot read, and what a plan cannot carry
# ---------------------------------------------------------------------------


def test_a_boolean_s_counts_shape_the_draw_and_nothing_the_check_reads() -> None:
    entry = {
        "domain": "category",
        "statistics": [_levels(["false", "true"], [450, 150], ["F", "F"])],
    }
    plan = _plan("boolean", entry)
    assert plan.declaration["levels"] == {"false": "0.75", "true": "0.25"}
    # The check reads a categorical column's values as text: a boolean has no support to check.
    assert "support" not in plan.declaration and "maxCardinality" not in plan.declaration
    assert plan.properties == []
    assert _sources(plan)[("levels", "true")] is Source.PROFILE


def test_a_date_column_carries_no_property() -> None:
    day = 86_400_000
    entry = {
        "domain": "timestamp",
        "metrics": {
            "min": _metric("min/1", str(19_000 * day)),
            "max": _metric("max/1", str(19_400 * day)),
        },
        "statistics": [
            _histogram([str(19_000 * day), str(19_200 * day), str(19_400 * day)], [300, 300])
        ],
    }
    plan = _plan("date", entry)
    # A profile reads a date as a timestamp, the block declares days: no property could score.
    assert plan.declaration["range"] == {"min": 19_000, "max": 19_400}
    assert plan.properties == []


def test_a_key_lies_between_its_published_min_and_max() -> None:
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "1000"), "max": _metric("max/1", "1199")},
    }
    plan = _keyed_plan("int", entry, key=True, rows=200)
    assert plan.declaration["bins"] == ["1000", "1199"]
    assert plan.declaration["range"] == {"min": "1000", "max": "1199"}
    assert "drawn evenly between the published min and max" in plan.assumptions[0]
    assert not plan.key_span
    assert _sources(plan)[("bins", "0")] is Source.PROFILE
    assert _sources(plan)[("range", "max")] is Source.PROFILE


@pytest.mark.parametrize(
    ("metrics", "why"),
    [
        ({}, "whose min and max the profile does not publish"),
        ({"min": _metric("min/1", "1")}, "whose max the profile does not publish"),
        (
            {"min": _metric("min/1", "1"), "max": _metric("max/1", "50")},
            "whose published min and max do not hold one value per row",
        ),
        (
            {"min": _metric("min/1", "1"), "max": _metric("max/1", str(2**60))},
            "whose published min and max lie beyond the integers a row carries exactly",
        ),
    ],
)
def test_a_key_s_assumed_span_says_why_it_is_assumed(metrics: Dict[str, Any], why: str) -> None:
    plan = _keyed_plan("int", {"domain": "number", "metrics": metrics}, key=True, rows=200)
    assert plan.declaration["bins"] == ["1", "200"]
    assert plan.key_span
    reason = next(label.basis.reason for label in plan.labels if label.tokens == ("bins",))
    assert reason is not None and why in reason


def test_an_integer_beyond_what_a_row_carries_is_not_declared() -> None:
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "1"), "max": _metric("max/1", "20000000000000000")},
    }
    plan = _plan("int", entry)
    assert plan.declaration["levels"] == {"0": "1"} and plan.placeholder
    assert "range" not in plan.declaration and "bins" not in plan.declaration
    assert any("2^53" in sentence for sentence in plan.assumptions)
    # Within the limit, the same column is declared from its bounds.
    entry["metrics"]["max"] = _metric("max/1", str(2**53 - 1))
    assert _plan("int", entry).declaration["range"]["max"] == str(2**53 - 1)


def test_a_cell_withheld_under_another_status_is_the_mean_of_the_published_ones() -> None:
    entry = {
        "domain": "category",
        "statistics": [_levels(["a", "b", "c", "d"], [100, None, 300, None], ["F", "D", "F", "A"])],
    }
    plan = _plan("string", entry)
    # 200 for the cell withheld under D (any size), 5 for the small one (A).
    assert plan.declaration["levels"] == share_texts_of([100, 200, 300, 5], ["a", "b", "c", "d"])
    reasons = {label.tokens[-1]: label.basis.reason for label in plan.labels}
    assert "status D" in str(reasons["b"]) and "does not say how large" in str(reasons["b"])
    assert "status A" in str(reasons["d"]) and "below the minimum cell" in str(reasons["d"])


def share_texts_of(counts: List[int], levels: List[str]) -> Dict[str, str]:
    return dict(zip(levels, share_texts([Fraction(count) for count in counts]), strict=True))


def test_a_null_count_withheld_under_another_status_is_said_to_be_of_unknown_size() -> None:
    entry = {"domain": "category", "metrics": {"nullCount": _metric("nullCount/1", status="C")}}
    plan = _plan("string", entry)
    assert plan.declaration["nullFraction"] == "0"
    reason = next(label.basis.reason for label in plan.labels if label.tokens == ("nullFraction",))
    assert reason is not None and "status C" in reason and "does not say how many" in reason


def test_a_placeholder_foreign_key_draws_over_its_parent_s_span() -> None:
    from flux_engine.author.columns import declared_span, draw_within_key
    from flux_engine.seam.contract import LogicalKind

    parent = _keyed_plan("int", {"domain": "number", "metrics": {}}, key=True, rows=200)
    child = _plan("int", {"domain": "number", "metrics": {}}, foreign="parent")
    assert child.placeholder and declared_span(child) == []
    assert draw_within_key(child, parent, LogicalKind.INT)
    assert child.declaration["bins"] == ["1", "200"] and "levels" not in child.declaration
    assert _sources(child)[("bins",)] is Source.ASSUMPTION
    assert not any("placeholder" in sentence for sentence in child.assumptions)
    assert declared_span(child) == [Decimal(1), Decimal(200)]


def test_a_key_s_published_range_is_checked_as_it_is_when_nothing_else_can_be() -> None:
    from flux_engine.author.columns import assumed_check
    from flux_engine.seam.contract import LogicalKind

    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "1000"), "max": _metric("max/1", "1199")},
    }
    plan = _keyed_plan("int", entry, key=True, rows=200)
    prop = assumed_check(plan, LogicalKind.INT)
    assert prop is not None and prop.document["property"] == "mean"
    # The range stays the profile's: no assumed label is laid over it.
    assert [label.tokens for label in plan.labels if label.tokens[0] == "range"] == [
        ("range", "min"),
        ("range", "max"),
    ]


def test_a_histogram_s_draw_keeps_to_the_published_min_and_max() -> None:
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "1"), "max": _metric("max/1", "97")},
        "statistics": [_histogram(["0", "50", "100"], [300, 300])],
    }
    plan = _plan("int", entry)
    # The draw starts at the published min and ends at the published max: no 0, no 100.
    assert plan.declaration["bins"] == ["1", "50", "97"]
    # The check still reads the histogram's own edges, which its property's bins partition.
    assert plan.declaration["range"] == {"min": "0", "max": "100"}
    assert plan.properties[0].document["bins"] == ["0", "50", "100"]
    sources = _sources(plan)
    assert sources[("bins", "0")] is Source.PROFILE and sources[("bins", "2")] is Source.PROFILE
    cited = {label.tokens: label.basis.statistic for label in plan.labels}
    assert cited[("bins", "0")] == (("tables", "orders", "columns", "c", "metrics", "min"), "min/1")


def test_a_quantile_beyond_the_published_max_widens_the_checked_range() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "3"),
            "max": _metric("max/1", "8"),
            "quantiles": _metric("quantiles/1", {"0.25": "4", "0.5": "5", "0.75": "12"}),
        },
    }
    plan = _plan("int", entry)
    # Inconsistent, but published: the range spans every published value, so the
    # property's bins partition it.
    assert plan.declaration["range"] == {"min": "3", "max": "12"}
    assert plan.properties[0].document["bins"][-1] == "12"


def test_a_fine_decimal_grid_gets_an_exact_mean_tolerance() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "0"),
            "max": _metric("max/1", "40"),
            "quantiles": _metric("quantiles/1", {"0.25": "10", "0.5": "20", "0.75": "30"}),
        },
        "statistics": [
            {
                "definition": "mean/1",
                "sampleSize": {"min": 600, "max": 600},
                "disclosure": "acro-default",
                "groups": [{"group": "", "k": 600, "Sx": str(27750 * 2**16), "status": "F"}],
            }
        ],
    }
    plan = _plan("decimal(38,28)", entry)
    # A tenth of 46.25, on the column's 28-place grid: no decimal context rounds it away.
    mean = next(prop for prop in plan.properties if prop.document["property"] == "mean")
    assert mean.document["tolerance"] == {"type": "absolute", "value": "4." + "625".ljust(28, "0")}


def test_a_grid_too_fine_to_draw_on_is_not_shaped() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "sum": _metric("sum/1", "9000"),
            "count": _metric("count/1", 600),
        },
        "statistics": [_histogram(["0", "10", "20", "30", "40"], [150, 150, 150, 150])],
    }
    plan = _plan("decimal(200000,100000)", entry)
    assert "shaped" not in plan.how
    assert plan.declaration["bins"] == ["0", "10", "20", "30", "40"]


def test_an_integer_key_is_never_widened_past_what_a_row_carries() -> None:
    from flux_engine.author.columns import widen_key_span

    key = _keyed_plan("int", {"domain": "number", "metrics": {}}, key=True, rows=600)
    huge = [Decimal(10) ** 33, Decimal(10) ** 33 + 3]
    widen_key_span(key, huge, 600, bound=2**53 - 1)
    assert key.declaration["bins"] == ["1", "600"]
    # Without a bound, the span holds a value per row, in every digit.
    widen_key_span(key, huge, 600)
    assert key.declaration["bins"] == [str(10**33), str(10**33 + 599)]


def test_a_date_histogram_keeps_its_edges_when_moving_them_would_merge_two_days() -> None:
    day = 86_400_000
    entry = {
        "domain": "timestamp",
        "metrics": {"min": _metric("min/1", str(day)), "max": _metric("max/1", str(3 * day))},
        "statistics": [_histogram(["0", str(3 * day // 2), str(3 * day)], [300, 300])],
    }
    plan = _plan("date", entry)
    # Moved in to the published min, the first two edges would both be day 1.
    assert plan.declaration["bins"] == [0, 1, 3]


def test_a_column_of_a_key_of_several_columns_spans_its_rows() -> None:
    # A key over two columns can hold one value per row only if no column of it is one
    # value: a part with nothing published spans the rows, as a one-column key does.
    plan = _keyed_plan("int", {"domain": "number", "metrics": {}}, keyed=True, rows=30)
    assert plan.declaration["bins"] == ["1", "30"] and plan.key_span
    assert "levels" not in plan.declaration and "nullFraction" not in plan.declaration
    assert plan.assumptions[0].startswith("A column of a key the contract declares")
    one_value = {"domain": "number", "metrics": {"max": _metric("max/1", "17")}}
    assert _keyed_plan("int", one_value, keyed=True, rows=30).declaration["bins"] == ["1", "30"]


def test_a_text_column_of_a_key_of_several_columns_takes_as_many_levels_as_rows() -> None:
    plan = _keyed_plan("string", {"domain": "category", "metrics": {}}, keyed=True, rows=30)
    assert len(plan.declaration["levels"]) == 30
    big = _keyed_plan("string", {"domain": "category", "metrics": {}}, keyed=True, rows=5000)
    assert len(big.declaration["levels"]) == OPAQUE_LEVEL_CAP
