# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Edge cases of ``fluxsim init``'s rules (ADR 0014), each once a wrong bundle or a refusal.

Pinned here:

* a fill whose mean lies near its bound meets the mean, and never claims a
  ``distinctCount`` its bins could not hold;
* a span at the limit of its type is one value, never a span turned around;
  a centred span keeps within its type; one distinct value is one value;
* a lone bound is cited from the bound, and an equal quantile is checked;
* children per parent: a class of none takes no child, a merged class with
  children takes its share, an empty bin of the foreign key's own histogram
  stays empty, few keys for many classes keep the children, an open class
  with no published mean takes its lower bound, a histogram of many narrow
  bins is laid out inside at most 32 of them, a top key of its own keeps its
  children, and many classes merge in runs, never the class of none;
* a grouped link is drawn in the direction its table can be read;
* a column a rule sets keeps none of its histogram's notes, a rule over a key
  reads the span the key is laid out over, and a chain of products gets no
  bound past what the bundle can write;
* a DECIMAL whose values reach past its digits is a placeholder, as an INT is;
* ``--allow ""`` would accept everything, so it is refused.
"""

from __future__ import annotations

import time
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict

import pytest

from flux_engine.author.bundle import AuthoredBundle, strict_findings
from flux_engine.author.children import MOST_BUCKETS, _bucket_layout, _class_means, _merged
from flux_engine.author.fill import drawn_mean, tilted_fill

from .test_columns import _histogram, _metric, _plan

pytestmark = pytest.mark.unit
F = Fraction


def _drawn(plan: Any, step: str = "1") -> Fraction:
    edges = [Fraction(Decimal(edge)) for edge in plan.declaration["bins"]]
    weights = [Fraction(Decimal(weight)) for weight in plan.declaration["binWeights"]]
    return drawn_mean(edges, weights, Fraction(Decimal(step)))


def test_a_mean_near_its_bound_is_met_with_fine_bins() -> None:
    for mean, distinct in ((F(1), 1000), (F(2), 200)):
        edges, weights = tilted_fill(F(0), mean, F(1), 1000, distinct=distinct)  # type: ignore[misc]
        assert abs(drawn_mean(edges, weights, F(1)) - mean) <= F(1)
        assert edges[1] - edges[0] == 1  # the first bin is one grid value
    entry = {"domain": "number", "metrics": {"min": _metric("min/1", "0")}}
    entry["metrics"]["sum"] = _metric("sum/1", "1200")
    entry["metrics"]["count"] = _metric("count/1", 600)
    entry["metrics"]["distinctCount"] = _metric("distinctCount/1", 200)
    plan = _plan("int", entry)
    assert abs(_drawn(plan) - 2) <= F(1, 10**6)
    passed = {tokens[-1]: why for tokens, why in plan.passed}
    assert "too near the bound" in passed["distinctCount"]


@pytest.mark.parametrize(
    ("declared", "metrics", "value"),
    [
        ("int", {"min": str(2**53 - 1), "distinctCount": 5}, str(2**53 - 1)),
        ("decimal(5,2)", {"min": "999.99", "distinctCount": 5}, "999.99"),
        ("decimal(5,2)", {"max": "-999.99", "distinctCount": 5}, "-999.99"),
    ],
)
def test_a_span_at_the_limit_of_its_type_is_one_value(
    declared: str, metrics: Dict[str, Any], value: str
) -> None:
    entry = {
        "domain": "number",
        "metrics": {name: _metric(f"{name}/1", raw) for name, raw in metrics.items()},
    }
    assert _plan(declared, entry).declaration["levels"] == {value: "1"}


def test_a_centred_span_keeps_within_its_type_and_one_distinct_value_is_one() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "count": _metric("count/1", 600),
            "sum": _metric("sum/1", str(Decimal("999.94") * 600)),
            "distinctCount": _metric("distinctCount/1", 100),
        },
    }
    low, high = (Decimal(edge) for edge in _plan("decimal(5,2)", entry).declaration["bins"])
    assert high <= Decimal("999.99") and high - low >= Decimal("0.99")
    entry["metrics"]["distinctCount"] = _metric("distinctCount/1", 1)
    assert _plan("decimal(5,2)", entry).declaration["levels"] == {"999.94": "1"}


def test_a_lone_bound_is_cited_from_the_bound() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "max": _metric("max/1", "5"),
            "quantiles": _metric("quantiles/1", {"0.5": "5"}),
            "sum": _metric("sum/1", "1200"),
            "count": _metric("count/1", 600),
        },
    }
    plan = _plan("int", entry)
    top = next(
        label
        for label in plan.labels
        if label.tokens == ("bins", str(len(plan.declaration["bins"]) - 1))
    )
    assert top.basis.statistic is not None and top.basis.statistic[0][-1] == "max"
    reads = {tokens[-1]: how for tokens, how in plan.reads}
    assert reads["0.5"].startswith("checked: equal to the published bound")


def test_children_classes_take_their_children() -> None:
    edges = [F(0), F(3), F(6), F(9)]
    means = _class_means(edges, [F(100), F(200), F(300)], None)
    assert means[0] == 1  # a class of 0 to 2 children has children
    assert means[-1] == 6  # an open class with no published mean takes its lower bound
    assert _class_means([F(0), F(1), F(2)], [F(5), F(5)], None)[0] == 0  # none is none


def test_few_keys_for_many_classes_keep_the_children() -> None:
    layout = _bucket_layout(1, 3, F(10), [F(98), F(1), F(1)], [F(0), F(1), F(2)], [0, 1, 2])
    assert layout == [(1, 3, F(10))]
    assert _bucket_layout(5, 4, F(0), [F(1)], [F(1)], [0]) == [(5, 4, F(0))]


def test_many_narrow_bins_are_laid_out_inside_at_most_32() -> None:
    buckets = [(first, 1, F(first % 3)) for first in range(1, 2001)]
    merged = _merged(buckets)
    assert len(merged) == MOST_BUCKETS
    assert merged[0][0] == 1 and merged[-1][0] + merged[-1][1] == 2001
    assert sum(weight for _, _, weight in merged) == sum(weight for _, _, weight in buckets)
    started = time.perf_counter()
    _merged(buckets * 5)
    assert time.perf_counter() - started < 1


def test_a_derived_column_keeps_none_of_its_histogram_s_notes() -> None:
    from flux_engine.author.columns import ColumnContext, determine_column
    from flux_engine.seam.contract import CompiledColumn, parse_logical_type

    histogram = _histogram(["0.00", "4.00", "10.00"], [300, None], ["F", "A"])
    entry = {"domain": "number", "metrics": {}, "statistics": [histogram]}
    column = CompiledColumn(
        name="vat",
        logical_type=parse_logical_type("decimal(10,2)"),
        required=True,
        declared_type="decimal(10,2)",
    )
    ctx = ColumnContext(
        column=column,
        expose_index=0,
        schema_index=0,
        product_ref="shop",
        profile_tokens=("tables", "orders", "columns", "vat"),
        entry=entry,
        rows=600,
        minimum_cell=10,
    )
    plan = _plan("decimal(10,2)", entry)
    determine_column(
        ctx, plan, ("exposes", "0", "contract", "invariants", "0", "expr"), (F(1), F(8))
    )
    assert plan.declaration == {"range": {"min": "0.00", "max": "10.00"}}
    assert [sentence for sentence in plan.assumptions if "withheld" in sentence] == []


def test_the_empty_allow_pointer_is_refused() -> None:
    bundle = AuthoredBundle(
        files=(), exposes=(), counts={}, notes=(), profile_digest="", simulation_id=""
    )
    with pytest.raises(ValueError, match="accept every finding"):
        strict_findings(bundle, [""])


def test_a_link_is_drawn_in_the_direction_its_table_can_be_read() -> None:
    """segment grouped by tier, with a group missing: the forest roots at tier, not by name."""
    from flux_engine.author.columns import ColumnPlan
    from flux_engine.author.given import choose_givens
    from flux_engine.seam.contract import LogicalKind

    segment = ColumnPlan(
        name="segment", declaration={"support": "declared", "levels": {"A": "0.5", "B": "0.5"}}
    )
    segment.level_names = ("A", "B")
    tier = ColumnPlan(
        name="tier",
        declaration={
            "support": "declared",
            "levels": {"gold": "0.5", "silver": "0.5", "bronze": "0"},
        },
    )
    tier.level_names = ("gold", "silver", "bronze")
    grouped = {
        "definition": "levelHistogram/1",
        "levels": ["A", "B"],
        "groupBy": "tier",
        "groups": [
            {"group": "gold", "k": 300, "levelCounts": [270, 30], "status": "F"},
            {"group": "silver", "k": 300, "levelCounts": [30, 270], "status": "F"},
        ],
    }
    choose_givens(
        [segment, tier],
        {"segment": LogicalKind.STRING, "tier": LogicalKind.STRING},
        {"segment": {"statistics": [grouped]}, "tier": {}},
        ("tables", "t"),
        minimum_cell=10,
    )
    assert segment.declaration.get("given") == "tier"
    assert set(segment.declaration["levelWeightsGiven"]) == {"gold", "silver", "bronze"}


@pytest.mark.integration
def test_a_rule_over_a_key_reads_the_span_the_key_is_laid_out_over(tmp_path: Any) -> None:
    import json

    import yaml

    from flux_engine.cli._init import run_init

    contract = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: keys.shop
name: Keys shop
metadata:
  owner:
    team: shop-team
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: parquet, location: {path: ./out/orders.parquet}}
    contract:
      schema:
        - {name: order_id, type: INT, required: true}
        - {name: twice, type: INT, required: true}
      invariants:
        - {id: twice_of_id, derive: twice, expr: "order_id * 2"}
    semantics:
      entities:
        - {name: order, type: primary, expr: order_id}
  - exposeId: lines
    kind: table
    binding: {platform: local, format: parquet, location: {path: ./out/lines.parquet}}
    contract:
      schema:
        - {name: line_id, type: INT, required: true}
        - {name: order_id, type: INT, required: true}
    semantics:
      entities:
        - {name: line, type: primary, expr: line_id}
      relationships:
        - {name: line_to_order, to: orders, fromColumns: [order_id], toColumns: [order_id], cardinality: many_to_one}
"""
    sample = {"min": 950, "max": 1050}

    def metric(definition: str, value: Any) -> Dict[str, Any]:
        return {
            "definition": definition,
            "status": "F",
            "sampleSize": sample,
            "disclosure": "acro-default",
            "value": value,
        }

    profile = {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile/1-0-0/FluxProfile.json",
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {
            "acro-default": {
                "dominance": {"k": "0.90", "n": 2},
                "minimumCell": 10,
                "pRatio": "0.10",
                "roundingBase": 5,
                "zerosDisclosive": True,
            }
        },
        "tables": {
            "orders": {
                "rowCount": 100,
                "columns": {
                    "order_id": {"domain": "number", "metrics": {"count": metric("count/1", 100)}}
                },
            },
            "lines": {
                "rowCount": 500,
                "columns": {
                    "line_id": {
                        "domain": "number",
                        "metrics": {"min": metric("min/1", "1"), "max": metric("max/1", "500")},
                    },
                    "order_id": {
                        "domain": "number",
                        "metrics": {"min": metric("min/1", "1"), "max": metric("max/1", "9000")},
                    },
                },
            },
        },
    }
    (tmp_path / "c.fluid.yaml").write_text(contract, encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "b"),
        contract=str(tmp_path / "c.fluid.yaml"),
        profile=str(tmp_path / "p.json"),
    )
    assert result.exit_code == 0, result.stderr
    block = yaml.safe_load((tmp_path / "b" / "simulation.flux.yml").read_text(encoding="utf-8"))
    columns = block["extensions"]["com.agentics.flux/1"]["emits"]["orders"]["fidelity"]["columns"]
    key_high = Decimal(columns["order_id"]["bins"][-1])
    assert key_high >= 9000  # widened to the foreign key that references it
    assert Decimal(columns["twice"]["range"]["max"]) >= 2 * key_high


def test_the_top_key_takes_the_key_below_it_and_keeps_its_weight() -> None:
    from flux_engine.author.children import _edges

    # One key with every child, at the top, after 999 with none: not spread over all 1000.
    assert _edges([(1, 999, F(0)), (1000, 1, F(3))], 1000) == ([1, 999, 1000], [F(0), F(3)])
    edges, weights = _edges([(1, 950, F(0)), (951, 49, F(2)), (1000, 1, F(1))], 1000)
    assert edges == [1, 951, 999, 1000] and weights == [F(0), F(2), F(1)]
    # Two parts of one key at the top are one bin of two keys.
    edges, weights = _edges([(1, 998, F(0)), (999, 1, F(2)), (1000, 1, F(1))], 1000)
    assert edges == [1, 999, 1000] and weights == [F(0), F(3)]


def test_many_classes_are_merged_in_runs() -> None:
    from flux_engine.author.children import _merged_classes

    parents = [F(1)] * 1000
    means = [F(index) for index in range(1000)]
    merged_parents, merged_means = _merged_classes(parents, means)
    assert len(merged_parents) == MOST_BUCKETS and sum(merged_parents) == 1000
    assert sum(p * m for p, m in zip(merged_parents, merged_means, strict=True)) == sum(means)


def test_the_class_of_no_children_is_never_merged() -> None:
    from flux_engine.author.children import _merged_classes

    parents = [F(400)] + [F(60)] * 10 + [F(0)] * 89
    means = _class_means([F(edge) for edge in range(101)], parents, None)
    merged_parents, merged_means = _merged_classes(parents, means)
    assert len(merged_parents) == MOST_BUCKETS and sum(merged_parents) == 1000
    assert (merged_parents[0], merged_means[0]) == (F(400), F(0))
    rest = zip(merged_parents[1:], merged_means[1:], strict=True)
    assert all(mean > 0 for count, mean in rest if count)


def test_a_chain_of_products_gets_no_bound_past_its_size() -> None:
    from flux_engine.author.derived import bounds

    spans = {"a": (F(2), F(10))}
    text = "a"
    for _ in range(14):
        text = f"({text}) * ({text})"
    started = time.perf_counter()
    assert bounds(text, spans) is None
    assert time.perf_counter() - started < 2


def test_a_count_is_never_a_boolean() -> None:
    from flux_engine.author.children import _whole

    # The profile reader refuses a boolean count (FLX-5036); the layout does not rely on it.
    assert _whole(True) is None and _whole(0) is None and _whole(3) == 3


def test_a_bound_is_one_the_bundle_can_write() -> None:
    from flux_engine.author.derived import MOST_BITS, bounds
    from flux_stats.spelling import MOST_DIGITS

    assert len(str(2**MOST_BITS)) + 38 <= MOST_DIGITS
    assert bounds("a * a", {"a": (F(0), F(10) ** 400)}) == (F(0), F(10) ** 800)
    # 10^1200 has more digits than a fidelity block's number may: no bound, not a refusal.
    assert bounds("a * a", {"a": (F(0), F(10) ** 600)}) is None


def test_a_decimal_beyond_its_digits_is_a_placeholder() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "-9007199254740992"),
            "count": _metric("count/1", 600),
            "sum": _metric("sum/1", str(-9007199254740991 * 600)),
        },
    }
    plan = _plan("decimal(9,2)", entry)
    assert plan.placeholder and plan.declaration["bins"] == ["0.00", "0.99"]
