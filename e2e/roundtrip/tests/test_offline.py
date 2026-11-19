# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The harness's own logic, offline: the judge math and the truth's pin. No profiler, no run."""

from __future__ import annotations

import math
from fractions import Fraction

import pytest

from flux_roundtrip import cli, judges, owner, truth


def test_total_variation_of_equal_and_disjoint_distributions() -> None:
    assert judges.total_variation({"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5}) == 0.0
    assert judges.total_variation({"a": 1.0}, {"b": 1.0}) == 1.0
    assert judges.total_variation({"a": 0.75, "b": 0.25}, {"a": 0.25, "b": 0.75}) == pytest.approx(
        0.5
    )


def test_distribution_counts_shares_and_tolerates_nothing() -> None:
    assert judges.distribution(["x", "x", "y", "z"]) == {"x": 0.5, "y": 0.25, "z": 0.25}
    assert judges.distribution([]) == {}


def test_cramers_v_of_independence_and_of_a_function() -> None:
    independent = [(a, b) for a in "xy" for b in "pq" for _ in range(5)]
    assert judges.cramers_v(independent) == pytest.approx(0.0)
    determined = [("x", "p")] * 6 + [("y", "q")] * 4
    assert judges.cramers_v(determined) == pytest.approx(1.0)
    # 2x2 table [[30, 10], [10, 30]]: chi2 = 20, n = 80, V = sqrt(20 / 80) = 0.5
    table = [("x", "p")] * 30 + [("x", "q")] * 10 + [("y", "p")] * 10 + [("y", "q")] * 30
    assert judges.cramers_v(table) == pytest.approx(0.5)


def test_cramers_v_is_zero_for_a_constant_column() -> None:
    assert judges.cramers_v([("x", "p"), ("x", "q")]) == 0.0
    assert judges.cramers_v([]) == 0.0


def test_quantile_edges_and_bins() -> None:
    edges = judges.quantile_edges([float(i) for i in range(100)], bins=10)
    assert edges == [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0]
    assert [judges.bin_of(edges, v) for v in (0.0, 9.9, 10.0, 95.0)] == [0, 0, 1, 9]
    assert judges.bin_of(edges, None) == -1
    # a column of few distinct values gets fewer bins, never a repeated edge
    assert judges.quantile_edges([1.0] * 50 + [2.0] * 50) == [2.0]


def test_numeric_value_reads_each_contract_type() -> None:
    assert judges.numeric_value("INT", 7) == 7.0
    assert judges.numeric_value("DECIMAL(10,2)", "12.50") == 12.5
    assert (
        judges.numeric_value("DATE", "2024-01-02") == judges.numeric_value("DATE", "2024-01-01") + 1
    )
    assert judges.numeric_value("INT", None) is None


def test_k_marginal_of_a_copy_is_one_and_of_a_disjoint_table_is_zero() -> None:
    columns = [("colour", "enum('red', 'blue')"), ("size", "INT")]
    truth_rows = [
        {"colour": colour, "size": size} for colour in ("red", "blue") for size in range(50)
    ]
    same = judges.k_marginal(truth_rows, list(truth_rows), columns)
    assert same["score"] == 1.0 and list(same["subsets"]) == ["colour|size"]
    other = [{"colour": "green", "size": size} for size in range(100)]
    assert judges.k_marginal(truth_rows, other, columns)["score"] == pytest.approx(0.0)


def test_k_marginal_averages_every_pair() -> None:
    columns = [("a", "enum('x', 'y')"), ("b", "enum('p', 'q')"), ("c", "enum('m', 'n')")]
    truth_rows = [{"a": "x", "b": "p", "c": "m"}, {"a": "y", "b": "q", "c": "n"}] * 5
    # synthetic: a and b are as in the truth, c is always "m": only the two pairs with c move
    synthetic = [{"a": "x", "b": "p", "c": "m"}, {"a": "y", "b": "q", "c": "m"}] * 5
    result = judges.k_marginal(truth_rows, synthetic, columns)
    assert set(result["subsets"]) == {"a|b", "a|c", "b|c"}
    assert result["subsets"]["a|b"] == 0.0
    assert result["subsets"]["a|c"] == pytest.approx(0.5)
    assert result["meanTotalVariation"] == pytest.approx(1 / 3)
    assert result["score"] == pytest.approx(2 / 3)


def test_childless_and_top_shares() -> None:
    parents = list(range(1, 101))
    children = [1] * 30 + [2] * 10 + [3] * 5 + [4]  # parent 1 holds 30 of 46 children
    counts = judges.children_per_parent(parents, children)
    assert counts[:4] == [30, 10, 5, 1] and len(counts) == 100
    assert judges.childless_share(counts) == pytest.approx(0.96)
    # the top 1 % of 100 parents is one parent
    assert judges.top_share(counts) == pytest.approx(30 / 46)
    assert judges.top_share(counts, Fraction(1, 10)) == pytest.approx(1.0)


def test_top_share_takes_at_least_one_parent_and_survives_no_children() -> None:
    assert judges.top_share([5, 5]) == pytest.approx(0.5)
    assert judges.top_share([0, 0, 0]) == 0.0
    assert judges.childless_share([]) == 0.0


def test_relationship_figures() -> None:
    figures = judges.relationship_figures([1, 2, 3, 4], [1, 1, 2])
    assert figures["parents"] == 4 and figures["children"] == 3
    assert figures["childlessShare"] == 0.5
    assert figures["mostChildren"] == 2


def test_grouped_shares_and_their_distance() -> None:
    rows = [{"g": "a", "v": "x"}] * 3 + [{"g": "a", "v": "y"}] + [{"g": "b", "v": "y"}] * 2
    shares = judges.grouped_shares(rows, "g", "v")
    assert shares == {"a": {"x": 0.75, "y": 0.25}, "b": {"y": 1.0}}
    other = judges.grouped_shares(
        [{"g": "a", "v": "x"}] * 2 + [{"g": "a", "v": "y"}] * 2 + [{"g": "b", "v": "x"}],
        "g",
        "v",
    )
    # a/x 0.75 vs 0.5, a/y 0.25 vs 0.5, b/y 1.0 vs 0.0, b/x 0.0 vs 1.0
    assert judges.max_share_difference(shares, other) == pytest.approx(1.0)
    assert judges.max_share_difference(shares, shares) == 0.0


def test_grouped_figures_compare_cramers_v() -> None:
    linked = [{"g": "a", "v": "x"}] * 10 + [{"g": "b", "v": "y"}] * 10
    mixed = [{"g": g, "v": v} for g in "ab" for v in "xy" for _ in range(5)]
    figures = judges.grouped_figures(linked, mixed, "g", "v")
    assert figures["cramersVTruth"] == pytest.approx(1.0)
    assert figures["cramersVSynthetic"] == pytest.approx(0.0)
    assert figures["cramersVDifference"] == pytest.approx(1.0)
    assert figures["maxShareDifference"] == pytest.approx(0.5)


def test_derives_hold_counts_each_rule() -> None:
    rows = [
        {"net": "100.00", "vat": "8.10", "gross": "108.10"},  # holds
        {"net": "12.35", "vat": "1.00", "gross": "13.35"},  # 12.35 * 0.081 = 1.00035 -> 1.00
        {"net": "12.35", "vat": "1.01", "gross": "13.36"},  # vat is off, gross follows vat
        {"net": "10.00", "vat": "0.81", "gross": "10.00"},  # vat holds, gross does not
    ]
    result = judges.derives_hold(rows)
    assert result["rows"] == 4
    assert result["vatShare"] == 0.75
    assert result["grossShare"] == 0.75
    assert result["bothShare"] == 0.5


def test_derives_round_half_away_from_zero() -> None:
    # 5.00 * 0.081 = 0.405 exactly: half away from zero is 0.41, half to even would be 0.40
    assert (
        judges.derives_hold([{"net": "5.00", "vat": "0.41", "gross": "5.41"}])["bothShare"] == 1.0
    )
    assert judges.derives_hold([{"net": "5.00", "vat": "0.40", "gross": "5.40"}])["vatShare"] == 0.0


def _verify(coverage: dict | None, flags: list | None = None, score: str | None = "1/2") -> dict:
    document: dict = {"overall": {"score": score, "status": "measured"}}
    if coverage is not None:
        document["coverage"] = coverage
    document["shapeFlags"] = {
        "status": "checked",
        "tables": [
            {
                "exposeId": "t",
                "columns": [{"column": "c", "flags": flags or [], "status": "checked"}],
            }
        ],
    }
    return {"scorecard": {"document": document}}


def test_scorecard_figures_read_the_score_and_the_coverage() -> None:
    coverage = {
        "columns": {"covered": 2, "of": 16, "share": "0.125"},
        "relationships": {"covered": 0, "of": 2, "share": "0"},
    }
    figures = judges.scorecard_figures(_verify(coverage))
    assert figures["overall"] == 0.5 and figures["coverageStated"] is True
    assert figures["coverage"]["columns"] == 0.125
    assert figures["coverage"]["relationshipsOf"] == 2


def test_scorecard_without_coverage_or_score() -> None:
    figures = judges.scorecard_figures(_verify(None, score=None))
    assert figures["overall"] is None and figures["coverageStated"] is False
    assert "coverage" not in figures
    assert judges.scorecard_figures({})["coverageStated"] is False


def test_profile_levels_prefers_histograms_then_distinct_count() -> None:
    levels = {"statistics": [{"definition": "levelHistogram/1", "levels": ["a", "b", "c"]}]}
    bins = {"statistics": [{"definition": "marginalHistogram/1", "bins": ["0", "1", "2"]}]}
    distinct = {"metrics": {"distinctCount": {"status": "F", "value": 7}}}
    withheld = {"metrics": {"distinctCount": {"status": "A"}}}
    assert judges.profile_levels(levels) == 3
    assert judges.profile_levels(bins) == 2
    assert judges.profile_levels(distinct) == 7
    assert judges.profile_levels(withheld) == 0
    assert judges.profile_levels({}) == 0


def test_constant_flags_name_only_columns_the_profile_shows_levels_for() -> None:
    verify = _verify(None, flags=[{"alert": "CONSTANT"}])
    shown = {
        "tables": {
            "t": {"columns": {"c": {"metrics": {"distinctCount": {"status": "F", "value": 3}}}}}
        }
    }
    hidden = {"tables": {"t": {"columns": {"c": {"metrics": {}}}}}}
    assert judges.constant_flags(verify, shown) == ["t.c"]
    assert judges.constant_flags(verify, hidden) == []
    assert judges.constant_flags(_verify(None, flags=[{"alert": "IMBALANCE"}]), shown) == []


def test_unused_statistics_reads_the_ledger_counts() -> None:
    assert judges.unused_statistics({"artifacts": {"ledger": {"counts": {"unused": 4}}}}) == 4
    assert judges.unused_statistics({}) is None


def _figures(**overrides: object) -> dict:
    relationship = {"childlessShare": 0.2, "topShare": 0.3, "mostChildren": 10}
    figures: dict = {
        "constantFlags": [],
        "relationships": {"a.b": {"truth": relationship, "synthetic": dict(relationship)}},
        "grouped": {
            "tier|segment": {
                "maxShareDifference": 0.02,
                "cramersVTruth": 0.5,
                "cramersVSynthetic": 0.49,
                "cramersVDifference": 0.01,
            }
        },
        "derives": {"vatShare": 1.0, "grossShare": 1.0, "bothShare": 1.0},
        "unusedStatistics": 0,
        "scorecard": {"coverageStated": True, "coverage": {}},
    }
    figures.update(overrides)
    return figures


def test_targets_all_pass_on_a_faithful_copy() -> None:
    checks = cli.targets(_figures())
    assert all(passed for _, passed, _ in checks)
    assert len(checks) == 1 + 2 + 2 + 1 + 1 + 1


@pytest.mark.parametrize(
    ("override", "failing"),
    [
        ({"constantFlags": ["t.c"]}, "CONSTANT"),
        ({"unusedStatistics": 3}, "unused"),
        ({"derives": {"vatShare": 1.0, "grossShare": 0.99, "bothShare": 0.99}}, "derives"),
        ({"scorecard": {"coverageStated": False}}, "coverage"),
    ],
)
def test_a_target_fails_when_its_number_does(override: dict, failing: str) -> None:
    failed = [name for name, passed, _ in cli.targets(_figures(**override)) if not passed]
    assert len(failed) == 1 and failing in failed[0]


def test_relationship_and_grouped_targets_use_their_tolerances() -> None:
    relationship = {"childlessShare": 0.2, "topShare": 0.3, "mostChildren": 10}
    far = {"childlessShare": 0.31, "topShare": 0.41, "mostChildren": 10}
    grouped = {
        "maxShareDifference": 0.11,
        "cramersVTruth": 0.5,
        "cramersVSynthetic": 0.44,
        "cramersVDifference": 0.06,
    }
    checks = cli.targets(
        _figures(
            relationships={"a.b": {"truth": relationship, "synthetic": far}},
            grouped={"tier|segment": grouped},
        )
    )
    assert [passed for name, passed, _ in checks if "childless" in name] == [False]
    assert len([1 for name, passed, _ in checks if not passed]) == 4
    near = {"childlessShare": 0.29, "topShare": 0.39, "mostChildren": 10}
    ok = cli.targets(_figures(relationships={"a.b": {"truth": relationship, "synthetic": near}}))
    assert all(passed for _, passed, _ in ok)


def test_the_truth_bundle_pins_its_contract() -> None:
    assert truth.pinned_digests(truth.TRUTH_DIR) == [truth.contract_digest(truth.TRUTH_DIR)] * 3


def test_the_truth_bundle_validates() -> None:
    envelope = truth.fluxsim(["validate", "."], cwd=truth.TRUTH_DIR)
    assert envelope["ok"] is True, envelope.get("error")


def test_the_truth_contract_has_the_tables_and_relationships_the_judges_name() -> None:
    schema = owner.read_schema(truth.TRUTH_DIR / truth.CONTRACT)
    assert tuple(schema) == truth.TABLES
    for child, fk, parent, key in truth.RELATIONSHIPS:
        assert fk in [c["name"] for c in schema[child]]
        assert key in [c["name"] for c in schema[parent]]


def test_duckdb_types_of_the_contract_types() -> None:
    assert owner.duckdb_type("INT") == "INTEGER"
    assert owner.duckdb_type("DECIMAL(10,2)") == "DECIMAL(10,2)"
    assert owner.duckdb_type("enum('a', 'b')") == "VARCHAR"
    assert owner.duckdb_type("DATE") == "DATE"
    with pytest.raises(ValueError):
        owner.duckdb_type("MAP")


def test_forge_cli_pin_is_a_full_commit() -> None:
    pin = cli.forge_cli_pin()
    assert len(pin) == 40 and math.isfinite(int(pin, 16))
