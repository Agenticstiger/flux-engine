# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A property grouped by the bins of another column (``groupByBins``, ADR 0036 decision 8).

The groups are the bins, keyed by lower edge and named by ordinal in the order of the bins. The
reference is a column statistic of the profile that holds the same grouping, or one the reader
forms from a ``cliqueMarginal/2`` whose columns are binned. Every engine consumer that tells a
grouped statistic from an ungrouped one reads a statistic with bins as grouped.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.scorecard import (
    SCHEMA_URL,
    SCHEMA_URL_1_5_0,
    TableInput,
    build_scorecard,
)
from flux_engine.concordance.verifier import OUTSIDE, WITHIN, ClearProfile
from flux_engine.core.errors import FluxError
from flux_stats.fidelity import parse_fidelity_block
from flux_stats.profile import SCHEMA_URL_1_4_0, parse_profile
from tests.concordance.conftest import build_compiled, build_expose

pytestmark = pytest.mark.unit

EXPOSE = "readings"
BINS = ["0", "5", "10", "20"]
GRID = 1 << 16
TOLERANCE = {"type": "absolute", "value": "5"}


def _document(properties: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "contract": {
            "fluidVersion": "0.7.5",
            "kind": "DataProduct",
            "id": "test.private.readings",
            "exposes": [
                {
                    "exposeId": EXPOSE,
                    "kind": "stream",
                    "contract": {
                        "schema": [
                            {"name": "a", "type": "INT"},
                            {"name": "b", "type": "INT"},
                            {"name": "d", "type": "DATE"},
                            {"name": "tag", "type": "STRING"},
                        ]
                    },
                }
            ],
        },
        "emit": {
            "productRef": "test.private.readings",
            "exposeId": EXPOSE,
            "fluidVersion": "^0.7.3",
        },
        "extension": {
            "fidelity": {
                "encoding": {"fractionalBits": 16, "rounding": "floor"},
                "records": {"min": 2, "max": 1_000_000},
                "columns": {
                    "a": {"range": {"min": "0", "max": "100"}, "null": "exclude"},
                    "b": {"range": {"min": "0", "max": "20"}, "null": "exclude"},
                    "d": {"range": {"min": "2026-01-01", "max": "2026-03-01"}, "null": "exclude"},
                    "tag": {"support": {"values": ["x", "y"]}, "maxCardinality": 2, "null": "fail"},
                },
                "properties": properties,
                "budget": {"maxChecksPerState": 20},
                "failureAction": "withholdBundle",
            }
        },
        "expose": {"exposeId": EXPOSE, "columns": ["a", "b", "d", "tag"]},
    }


def _mean_by_bins(**change: Any) -> Dict[str, Any]:
    return {
        "property": "mean",
        "column": "a",
        "groupByBins": {"column": "b", "bins": list(BINS)},
        "tolerance": dict(TOLERANCE),
        **change,
    }


def _compiled(*properties: Dict[str, Any]) -> CompiledFidelity:
    return build_compiled(_document(list(properties)))


def _rows(means: Sequence[int]) -> List[Dict[str, Any]]:
    """Thirty rows in each of the three bins of ``b``, each holding the mean of its bin as ``a``."""
    rows: List[Dict[str, Any]] = []
    for low, mean in zip((0, 5, 10), means, strict=True):
        rows.extend({"a": mean, "b": low + i % 5, "d": "2026-01-05", "tag": "x"} for i in range(30))
    return rows


def _mean_profile(means: Sequence[int], keys: Sequence[str] = ("0", "5", "10")) -> Dict[str, Any]:
    return {
        "_producer": "urn:example:flux-test-profiler",
        "_schemaURL": SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            EXPOSE: {
                "columns": {
                    "a": {
                        "domain": "number",
                        "statistics": [
                            {
                                "definition": "mean/1",
                                "groupByBins": {"column": "b", "bins": list(BINS)},
                                "sampleSize": {"min": 1},
                                "disclosure": "none",
                                "groups": [
                                    {
                                        "group": key,
                                        "k": 30,
                                        "Sx": str(30 * mean * GRID),
                                        "status": "F",
                                    }
                                    # A profile lists its groups sorted by key as text.
                                    for key, mean in sorted(zip(keys, means, strict=True))
                                ],
                            }
                        ],
                    },
                    "b": {"domain": "number"},
                }
            }
        },
    }


def _evaluate(compiled: CompiledFidelity, document: Dict[str, Any], rows: Any) -> Any:
    return ClearProfile(
        compiled=compiled, profile=parse_profile(document), synthetic_rows=rows
    ).evaluate()


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def test_a_property_may_hold_group_by_bins_with_the_profiles_spelling() -> None:
    compiled = _compiled(_mean_by_bins())
    declaration = compiled.properties[0].declaration
    assert declaration.group_by is None and declaration.grouped
    assert declaration.group_by_bins is not None
    assert declaration.group_by_bins.column == "b"
    assert declaration.group_by_bins.keys == ("0", "5", "10")
    assert declaration.property_id == "000:mean:a@b#bins"
    fragment = compiled.plan_document()["properties"][0]
    assert fragment["groupByBins"] == {"column": "b", "bins": BINS}
    assert "b" in compiled.block.columns_covered


@pytest.mark.parametrize(
    ("grouping", "text"),
    [
        ({"column": "b", "bins": ["0"]}, "at least two"),
        ({"column": "b", "bins": ["0", "10", "10", "20"]}, "does not increase"),
        ({"column": "b", "bins": ["1", "10", "20"]}, "do not cover"),
        ({"column": "b", "bins": ["0", "10", "19"]}, "do not cover"),
        ({"column": "b", "bins": ["0", "0.0000001", "20"]}, "grid"),
        ({"column": "tag", "bins": ["0", "20"]}, "range column"),
        ({"column": "a", "bins": ["0", "100"]}, "own column"),
        ({"column": "nope", "bins": ["0", "20"]}, "no declaration"),
        ({"column": "b"}, "needs a column and bins"),
        ({"column": "b", "bins": ["0", "20"], "extra": 1}, "extra"),
    ],
)
def test_a_malformed_group_by_bins_is_refused(grouping: Dict[str, Any], text: str) -> None:
    document = _document([_mean_by_bins(groupByBins=grouping)])
    with pytest.raises(FluxError) as refused:
        parse_fidelity_block(document["extension"]["fidelity"], expose=build_expose(document))
    assert text in str(refused.value)


def test_a_property_holds_at_most_one_grouping_and_a_rank_correlation_none() -> None:
    both = _document([_mean_by_bins(groupBy="tag")])
    with pytest.raises(FluxError, match="at most one of groupBy and groupByBins"):
        parse_fidelity_block(both["extension"]["fidelity"], expose=build_expose(both))
    rank = _document(
        [
            {
                "property": "rankCorrelation",
                "columns": ["a", "b"],
                "measure": "kendall",
                "groupByBins": {"column": "d", "bins": ["2026-01-01", "2026-03-01"]},
            }
        ]
    )
    with pytest.raises(FluxError, match="ungrouped"):
        parse_fidelity_block(rank["extension"]["fidelity"], expose=build_expose(rank))


def test_a_date_column_is_grouped_by_edges_written_as_dates() -> None:
    compiled = _compiled(
        _mean_by_bins(
            groupByBins={"column": "d", "bins": ["2026-01-01", "2026-02-01", "2026-03-01"]}
        )
    )
    keys = compiled.properties[0].declaration.group_by_bins.keys  # type: ignore[union-attr]
    assert keys == ("20454", "20485")  # days since 1970-01-01


# ---------------------------------------------------------------------------
# The groups and their order
# ---------------------------------------------------------------------------


def test_groups_are_the_bins_keyed_by_lower_edge_and_named_by_ordinal_in_bin_order() -> None:
    compiled = _compiled(_mean_by_bins())
    found = _evaluate(compiled, _mean_profile([10, 50, 90]), _rows([10, 50, 90])).outcomes[0]
    assert found.outcome == WITHIN
    assert [group.group for group in found.groups] == ["0", "5", "10"]
    assert [group.synthetic_count for group in found.groups] == [30, 30, 30]


def test_a_group_outside_is_named_by_its_place_in_the_order_of_the_bins() -> None:
    compiled = _compiled(_mean_by_bins())
    # The text "10" sorts before "5"; the bin of lower edge 10 is the third.
    evaluation = _evaluate(compiled, _mean_profile([10, 50, 90]), _rows([10, 50, 40]))
    found = evaluation.outcomes[0]
    assert found.outcome == OUTSIDE
    assert [group.outcome for group in found.groups] == [WITHIN, WITHIN, OUTSIDE]
    card = _scorecard(compiled, evaluation).to_document()
    (row,) = card["tables"][0]["properties"]
    assert row["groups"] == {"scored": 3, "censored": 0, "outside": [2]}


def test_a_profile_that_groups_by_other_bins_is_refused_naming_the_shape() -> None:
    document = _mean_profile([10, 50, 90])
    document["tables"][EXPOSE]["columns"]["a"]["statistics"][0]["groupByBins"]["bins"] = [
        "0",
        "5",
        "20",
    ]
    groups = document["tables"][EXPOSE]["columns"]["a"]["statistics"][0]["groups"]
    groups[:] = [group for group in groups if group["group"] != "10"]
    evaluation = _evaluate(_compiled(_mean_by_bins()), document, _rows([10, 50, 90]))
    assert [refusal.code for refusal in evaluation.refusals] == ["FLX-5037"]
    assert "groupBy" in evaluation.refusals[0].reason


def test_a_bin_the_run_holds_no_row_of_is_a_group_the_two_sides_disagree_on() -> None:
    compiled = _compiled(_mean_by_bins())
    rows = _rows([10, 50, 90])[:60]
    evaluation = _evaluate(compiled, _mean_profile([10, 50, 90]), rows)
    assert [refusal.code for refusal in evaluation.refusals] == ["FLX-5005"]


def test_a_null_grouping_value_leaves_the_row_out() -> None:
    compiled = _compiled(_mean_by_bins())
    rows = _rows([10, 50, 90]) + [{"a": 99, "b": None, "d": "2026-01-05", "tag": "x"}] * 4
    found = _evaluate(compiled, _mean_profile([10, 50, 90]), rows).outcomes[0]
    assert found.outcome == WITHIN and [g.synthetic_count for g in found.groups] == [30, 30, 30]


def _date_grouping_ending_at(last_edge: int, declared_last: str = "9999-12-31") -> Any:
    """The mean of ``a`` grouped by the bins of ``d`` declared to end at *declared_last*, over a profile whose last edge is *last_edge* ms."""
    compiled = _compiled(
        _mean_by_bins(
            groupByBins={"column": "d", "bins": ["2026-01-01", "2026-02-01", declared_last]}
        )
    )
    january, february = 1767225600000, 1769904000000  # 2026-01-01 and 2026-02-01 in ms
    document = _mean_profile([10, 50, 0])
    stat = document["tables"][EXPOSE]["columns"]["a"]["statistics"][0]
    stat["groupByBins"] = {"column": "d", "bins": [str(january), str(february), str(last_edge)]}
    stat["groups"] = [
        {"group": str(january), "k": 30, "Sx": str(30 * 10 * GRID), "status": "F"},
        {"group": str(february), "k": 30, "Sx": str(30 * 50 * GRID), "status": "F"},
    ]
    document["tables"][EXPOSE]["columns"]["d"] = {"domain": "timestamp"}
    rows = [{"a": 10, "b": 1, "d": "2026-01-05", "tag": "x"} for _ in range(30)] + [
        {"a": 50, "b": 1, "d": "2026-02-05", "tag": "x"} for _ in range(30)
    ]
    return _evaluate(compiled, document, rows)


def test_a_date_grouping_column_reads_a_profile_that_holds_milliseconds() -> None:
    evaluation = _date_grouping_ending_at(1772323200000, declared_last="2026-03-01")
    assert not evaluation.refusals, evaluation.refusals
    assert evaluation.outcomes[0].outcome == WITHIN
    assert [g.group for g in evaluation.outcomes[0].groups] == ["20454", "20485"]


def test_a_date_grouping_ending_at_the_last_instant_is_the_declared_last_day() -> None:
    evaluation = _date_grouping_ending_at(253_402_300_799_999)  # 9999-12-31T23:59:59.999Z
    assert not evaluation.refusals, evaluation.refusals
    assert evaluation.outcomes[0].outcome == WITHIN


def test_a_date_grouping_ending_a_millisecond_short_of_it_matches_nothing() -> None:
    evaluation = _date_grouping_ending_at(253_402_300_799_998)
    assert [refusal.code for refusal in evaluation.refusals] == ["FLX-5037"]
    assert "groupBy" in evaluation.refusals[0].reason


# ---------------------------------------------------------------------------
# Derived marginals of a clique with bins
# ---------------------------------------------------------------------------

A_BINS = ["0", "50", "100"]
B_BINS = ["0", "10", "20"]
#: The world's cells over (a bin, b bin), row-major, as counts of 400 records.
CELLS = [90, 30, 40, 240]
SCALE = 6


def _clique_profile(offset: int) -> Dict[str, Any]:
    return {
        "_producer": "urn:example:flux-test-profiler",
        "_schemaURL": SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "privacy": {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "10"},
            "privacyUnit": {"expose": EXPOSE},
        },
        "tables": {
            EXPOSE: {
                "columns": {"a": {"domain": "number"}, "b": {"domain": "number"}},
                "cliques": [
                    {
                        "definition": "cliqueMarginal/2",
                        "columns": ["a", "b"],
                        "domains": [{"bins": A_BINS}, {"bins": B_BINS}],
                        "noisyCounts": [str(count + offset) for count in CELLS],
                        "noise": {
                            "mechanism": "discreteGaussian",
                            "scale": str(SCALE),
                            "privacyLoss": {"rho": "1"},
                        },
                    }
                ],
            }
        },
    }


def _clique_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for (a, b), count in zip(((25, 5), (25, 15), (75, 5), (75, 15)), CELLS, strict=True):
        rows.extend({"a": a, "b": b, "d": "2026-01-05", "tag": "x"} for _ in range(count))
    return rows


def _histogram_by_bins() -> Dict[str, Any]:
    return {
        "property": "marginalHistogram",
        "column": "a",
        "bins": list(A_BINS),
        "groupByBins": {"column": "b", "bins": list(B_BINS)},
        "tolerance": {"type": "distance", "measure": "totalVariation", "max": "0.05"},
    }


def test_a_two_way_marginal_of_a_clique_with_bins_is_scored_as_a_histogram_is() -> None:
    compiled = _compiled(_histogram_by_bins())
    found = _evaluate(compiled, _clique_profile(0), _clique_rows()).outcomes[0]
    assert found.outcome == WITHIN
    assert [g.group for g in found.groups] == ["0", "10"]
    # The variance of a cell of the derived histogram is the noise's times the cells it sums (here
    # the two bins of the other column are summed away: none, a two-way table keeps both).
    assert found.cell_variance is not None and found.noise_mechanism == "discreteGaussian"
    assert found.cell_variance == Fraction(SCALE * SCALE)


def test_a_noisy_grouped_reference_is_widened_group_by_group() -> None:
    compiled = _compiled(_histogram_by_bins())
    found = _evaluate(compiled, _clique_profile(3), _clique_rows()).outcomes[0]
    widenings = [group.widening for group in found.groups]
    assert all(widening is not None and widening > 0 for widening in widenings)
    # Each group's widening reads that group's own noisy total, so the two differ.
    assert len(set(widenings)) == 2


def test_a_one_way_marginal_of_a_binned_clique_column_is_scored_too() -> None:
    compiled = _compiled(
        {
            "property": "marginalHistogram",
            "column": "b",
            "bins": list(B_BINS),
            "tolerance": {"type": "distance", "measure": "totalVariation", "max": "0.05"},
        }
    )
    found = _evaluate(compiled, _clique_profile(0), _clique_rows()).outcomes[0]
    assert found.outcome == WITHIN
    # The one-way marginal sums the two bins of ``a`` into each cell: twice the noise's variance.
    assert found.cell_variance == Fraction(2 * SCALE * SCALE)


# ---------------------------------------------------------------------------
# The scorecard
# ---------------------------------------------------------------------------


def _scorecard(compiled: CompiledFidelity, evaluation: Any) -> Any:
    return build_scorecard(
        run_id="0" * 16,
        plan_digest="sha256:" + "0" * 64,
        run_digest="sha256:" + "1" * 64,
        profile_digest="sha256:" + "2" * 64,
        tables=[TableInput(expose_id=EXPOSE, compiled=compiled, evaluation=evaluation)],
    )


def test_a_row_grouped_by_bins_names_1_5_0_and_validates() -> None:
    from jsonschema import Draft202012Validator

    compiled = _compiled(_mean_by_bins())
    evaluation = _evaluate(compiled, _mean_profile([10, 50, 90]), _rows([10, 50, 90]))
    document = _scorecard(compiled, evaluation).to_document()
    assert document["_schemaURL"] == SCHEMA_URL_1_5_0
    (row,) = document["tables"][0]["properties"]
    assert row["dimension"] == "joint" and "groupBy" not in row
    assert row["groupByBins"] == {"column": "b", "bins": BINS}
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "docs" / "spec" / "scorecard-1-5.schema.json"
        ).read_text("utf-8")
    )
    Draft202012Validator(schema).validate(document)


def test_a_scorecard_with_no_bins_row_keeps_its_format() -> None:
    compiled = _compiled({"property": "mean", "column": "a", "tolerance": dict(TOLERANCE)})
    document = _mean_profile([10, 50, 90])
    document["tables"][EXPOSE]["columns"]["a"]["statistics"] = [
        {
            "definition": "mean/1",
            "sampleSize": {"min": 1},
            "disclosure": "none",
            "groups": [{"group": "", "k": 90, "Sx": str(90 * 50 * GRID), "status": "F"}],
        }
    ]
    evaluation = _evaluate(compiled, document, _rows([10, 50, 90]))
    assert _scorecard(compiled, evaluation).to_document()["_schemaURL"] == SCHEMA_URL


# ---------------------------------------------------------------------------
# A statistic with bins is never read as ungrouped
# ---------------------------------------------------------------------------


def _only_binned_profile() -> Any:
    """A profile whose column ``a`` holds nothing ungrouped, only a mean and a histogram by bins."""
    document = _mean_profile([10, 50, 90])
    column = document["tables"][EXPOSE]["columns"]["a"]
    column["statistics"].append(
        {
            "definition": "marginalHistogram/1",
            "bins": ["0", "50", "100"],
            "groupByBins": {"column": "b", "bins": list(BINS)},
            "sampleSize": {"min": 1},
            "disclosure": "none",
            "groups": [
                {"group": key, "k": 30, "binCounts": [15, 15], "status": "F"}
                for key in ("0", "10", "5")
            ],
        }
    )
    return parse_profile(document)


def test_the_reach_of_a_mean_reads_no_statistic_with_bins_as_ungrouped() -> None:
    from flux_engine.seam.mean_reach import _stated_mean, mean_bounds

    profile = _only_binned_profile()
    column = profile.tables[EXPOSE].columns["a"]
    assert _stated_mean(profile, column) is None
    assert mean_bounds(column) == (None, None)


def test_the_shape_check_reads_no_statistic_with_bins_as_the_columns_histogram() -> None:
    from flux_engine.concordance.shape import _cells
    from flux_stats.fixedpoint import Grid

    profile = _only_binned_profile()
    column = profile.tables[EXPOSE].columns["a"]
    assert _cells(profile, column, Grid(16)) is None


def test_the_drift_comparison_names_each_group_of_a_statistic_with_bins() -> None:
    from flux_engine.outcomes.drift import statistics_of

    named = sorted(statistics_of(_only_binned_profile(), ()))
    assert named == [
        f"{EXPOSE}.a/marginalHistogram/1/by the bins of b={key}" for key in ("0", "10", "5")
    ]


def test_a_sample_of_blocks_names_a_property_grouped_by_bins_and_estimates_nothing() -> None:
    from flux_engine.concordance.sampling import _sampled_kind

    compiled = _compiled(
        _mean_by_bins(), {"property": "mean", "column": "a", "tolerance": dict(TOLERANCE)}
    )
    assert [_sampled_kind(entry) for entry in compiled.properties] == ["", "mean"]
