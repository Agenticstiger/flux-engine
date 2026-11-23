# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes children per parent given a parent column (ADR 0037 decisions 4 and 6).

Pinned here, through the command and a run: a relationship's histogram of children per parent,
grouped by a category column of the parent or by the bins of a number column, becomes the ``given``
and ``childrenGiven`` of its ``counts`` entry beside the ``children`` of the ungrouped histogram,
or of the sum of the groups; a noisy histogram is read as its point estimate; what the generator
would refuse (a column with a null fraction, one a summary over the children sets, bins that are
not the column's) is passed over in the ledger with the reason and leaves the entry without a
``given``; and the bundle plans, applies and verifies with the children of every class within
tolerance. A noisy grouping by a link is read the same way.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.apply import apply
from flux_engine.stages.verify import verify
from flux_stats.profile import SCHEMA_URL_1_4_0

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
CUSTOMERS = 4000
EDGES = ["0", "1", "3", "10"]
#: Customers per segment, and the customers of each segment by their number of orders (0, 1 to 2, 3 to 9).
SEGMENTS: Dict[str, Tuple[int, List[int]]] = {
    "business": (1500, [150, 450, 900]),
    "personal": (2500, [1250, 1000, 250]),
}
SIZES = {"small": (0, 1000), "large": (1000, 4000)}

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: shop.customers
name: Customers
metadata:
  owner:
    team: shop
exposes:
  - exposeId: customer
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/customer.parquet }
    contract:
      schema:
        - { name: customer_id, type: INT, required: true }
        - { name: segment, type: "enum('business','personal')", required: true }
        - { name: size, type: INT, required: true }
    semantics:
      entities:
        - { name: customer, type: primary, expr: customer_id }
  - exposeId: orders
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/orders.parquet }
    contract:
      schema:
        - { name: order_id, type: INT, required: true }
        - { name: customer_id, type: INT, required: true }
    semantics:
      entities:
        - { name: order, type: primary, expr: order_id }
      relationships:
        - name: order_to_customer
          to: customer
          fromColumns: [customer_id]
          toColumns: [customer_id]
          cardinality: many_to_one
"""

ORDERS = 6000
SAMPLE = {"min": 1, "max": 99999}


def _metric(definition: str, value: Any) -> Dict[str, Any]:
    return {
        "definition": definition,
        "status": "F",
        "sampleSize": SAMPLE,
        "disclosure": "none",
        "value": value,
    }


def _histogram(
    edges: List[str], counts: List[int], *, group: str = "", more: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    statistic: Dict[str, Any] = {
        "definition": "marginalHistogram/1",
        "bins": edges,
        "sampleSize": SAMPLE,
        "disclosure": "none",
        "groups": [{"group": group, "k": sum(counts), "binCounts": counts, "status": "F"}],
    }
    statistic.update(more or {})
    return statistic


def _by_segment() -> Dict[str, Any]:
    """The children per parent grouped by the segment of the parent: one group a level."""
    statistic = _histogram(EDGES, [0, 0, 0], more={"groupBy": "segment"})
    statistic["groups"] = [
        {"group": name, "k": sum(counts), "binCounts": counts, "status": "F"}
        for name, (_, counts) in sorted(SEGMENTS.items())
    ]
    return statistic


def _total() -> List[int]:
    return [sum(counts[position] for _, counts in SEGMENTS.values()) for position in range(3)]


def _by_size() -> Dict[str, Any]:
    """The children per parent grouped by the bins of the parent's size: one group a lower edge."""
    edges = ["0", "1000", "4000"]
    statistic = _histogram(
        EDGES, [0, 0, 0], more={"groupByBins": {"column": "size", "bins": edges}}
    )
    # The same customers, cut by size instead: half of each class sits in each size.
    statistic["groups"] = [
        {
            "group": key,
            "k": sum(share),
            "binCounts": share,
            "status": "F",
        }
        for key, share in (("0", [300, 600, 100]), ("1000", [1100, 1850, 50]))
    ]
    return statistic


def _profile(
    statistics: List[Dict[str, Any]],
    *,
    segment_nulls: int = 0,
    privacy: bool = False,
    noisy_size_edges: Optional[List[str]] = None,
) -> Dict[str, Any]:
    key = {
        "count": _metric("count/1", CUSTOMERS),
        "min": _metric("min/1", "1"),
        "max": _metric("max/1", str(CUSTOMERS)),
        "nullCount": _metric("nullCount/1", 0),
    }
    order_key = {
        "count": _metric("count/1", ORDERS),
        "min": _metric("min/1", "1"),
        "max": _metric("max/1", str(ORDERS)),
    }
    size = noisy_size_edges or ["0", "1000", "4000"]
    segment_counts = [SEGMENTS[name][0] for name in sorted(SEGMENTS)]
    document: Dict[str, Any] = {
        "_producer": "urn:example:children-profiler",
        "_schemaURL": SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}, "noisy": {}},
        "tables": {
            "customer": {
                "rowCount": CUSTOMERS,
                "columns": {
                    "customer_id": {"domain": "number", "metrics": key},
                    "segment": {
                        "domain": "category",
                        "metrics": {
                            "count": _metric("count/1", CUSTOMERS - segment_nulls),
                            "nullCount": _metric("nullCount/1", segment_nulls),
                        },
                        "statistics": [
                            {
                                "definition": "levelHistogram/1",
                                "levels": sorted(SEGMENTS),
                                "sampleSize": SAMPLE,
                                "disclosure": "none",
                                "groups": [
                                    {
                                        "group": "",
                                        "k": sum(segment_counts),
                                        "levelCounts": segment_counts,
                                        "status": "F",
                                    }
                                ],
                            }
                        ],
                    },
                    "size": {
                        "domain": "number",
                        "metrics": {
                            "count": _metric("count/1", CUSTOMERS),
                            "nullCount": _metric("nullCount/1", 0),
                            "min": _metric("min/1", size[0]),
                            "max": _metric("max/1", size[-1]),
                        },
                        "statistics": [
                            _histogram(size, [1500, 2500][: len(size) - 1] or [CUSTOMERS])
                        ],
                    },
                },
            },
            "orders": {
                "rowCount": ORDERS,
                "columns": {
                    "order_id": {"domain": "number", "metrics": order_key},
                    "customer_id": {
                        "domain": "number",
                        "metrics": {"count": _metric("count/1", ORDERS)},
                    },
                },
            },
        },
        "relationships": [
            {
                "parent": {"table": "customer", "columns": ["customer_id"]},
                "child": {"table": "orders", "columns": ["customer_id"]},
                "statistics": statistics,
            }
        ],
    }
    if privacy:
        document["privacy"] = {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "1000"},
            "privacyUnit": {"expose": "customer"},
            "publicStructure": True,
        }
    return document


def _init(tmp_path: Path, profile: Dict[str, Any], contract: str = CONTRACT) -> Any:
    (tmp_path / "shop.fluid.yaml").write_text(contract, encoding="utf-8")
    (tmp_path / "shop.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "shop.fluid.yaml"),
        profile=str(tmp_path / "shop.profile.json"),
    )


def _entry(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    entries = yaml.safe_load(text)["extensions"][NAMESPACE]["counts"]
    (entry,) = entries
    return entry  # type: ignore[no-any-return]


def _ledger(result: Any, ending: str) -> Dict[str, Any]:
    (found,) = [
        each
        for each in result.artifacts["ledger"]["statistics"]
        if each["pointer"].endswith(ending)
    ]
    return found  # type: ignore[no-any-return]


def _ungrouped() -> Dict[str, Any]:
    return _histogram(EDGES, _total())


# ---------------------------------------------------------------------------
# What it writes
# ---------------------------------------------------------------------------


def test_a_histogram_grouped_by_a_category_column_is_the_given_of_the_entry(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _profile([_ungrouped(), _by_segment()]))
    assert result.exit_code == 0, result.stderr
    entry = _entry(tmp_path)
    assert entry["given"] == "segment"
    assert sorted(entry["childrenGiven"]) == ["business", "personal"]
    for name, (_, counts) in SEGMENTS.items():
        shares = [Fraction(text) for text in entry["childrenGiven"][name]]
        assert sum(shares) == 1
        assert all(
            abs(share - Fraction(count, sum(counts))) < Fraction(1, 10**6)
            for share, count in zip(shares, counts, strict=True)
        )
    # The children of the ungrouped histogram, which the classes of the given column share.
    assert entry["children"]["bins"][:3] == ["0", "1", "3"]
    assert _ledger(result, "relationships/0/statistics/0")["state"] == "used"
    assert _ledger(result, "relationships/0/statistics/1")["state"] == "used"


def test_the_children_are_the_sum_of_the_groups_when_no_ungrouped_histogram_is_published(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _profile([_by_segment()]))
    assert result.exit_code == 0, result.stderr
    entry = _entry(tmp_path)
    shares = [Fraction(text) for text in entry["children"]["binWeights"]]
    expected = [Fraction(count, CUSTOMERS) for count in _total()]
    assert all(
        abs(share - want) < Fraction(1, 10**6) for share, want in zip(shares, expected, strict=True)
    )
    assert "given" in entry
    assert "add up" in _ledger(result, "relationships/0/statistics/0")["note"]


def test_a_histogram_grouped_by_the_bins_of_a_number_column_keys_its_classes_by_lower_edge(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _profile([_ungrouped(), _by_size()]))
    assert result.exit_code == 0, result.stderr
    entry = _entry(tmp_path)
    assert entry["given"] == "size"
    assert sorted(entry["childrenGiven"]) == ["0", "1000"]
    columns = yaml.safe_load(
        (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    )["extensions"][NAMESPACE]["emits"]["customer"]["fidelity"]["columns"]
    assert columns["size"]["bins"][:2] == ["0", "1000"]
    # A number column that is given has no null share: an absent one draws none.
    assert "nullFraction" not in columns["size"]


def test_the_first_grouping_that_can_be_used_is_the_given_and_the_others_say_why_not(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _profile([_ungrouped(), _by_segment(), _by_size()]))
    assert result.exit_code == 0, result.stderr
    assert _entry(tmp_path)["given"] == "segment"
    note = _ledger(result, "relationships/0/statistics/2")
    assert note["state"] == "unused" and "one column" in note["note"]


def test_a_class_the_profile_publishes_no_group_for_takes_the_shapes_weights_and_says_so(
    tmp_path: Path,
) -> None:
    grouped = _by_segment()
    grouped["groups"] = grouped["groups"][:1]
    result = _init(tmp_path, _profile([_ungrouped(), grouped]))
    assert result.exit_code == 0, result.stderr
    assert list(_entry(tmp_path)["childrenGiven"]) == ["business"]
    assert any("personal" in line and "own weights" in line for line in result.stdout.splitlines())


# ---------------------------------------------------------------------------
# What the generator would refuse, passed over
# ---------------------------------------------------------------------------


def test_a_column_with_a_null_fraction_is_passed_over(tmp_path: Path) -> None:
    contract = CONTRACT.replace(
        "name: segment, type: \"enum('business','personal')\", required: true",
        "name: segment, type: \"enum('business','personal')\", required: false",
    )
    result = _init(tmp_path, _profile([_ungrouped(), _by_segment()], segment_nulls=200), contract)
    assert result.exit_code == 0, result.stderr
    assert "given" not in _entry(tmp_path)
    assert "nullFraction" in _ledger(result, "relationships/0/statistics/1")["note"]


def test_a_column_a_summary_over_the_children_sets_is_passed_over(tmp_path: Path) -> None:
    contract = CONTRACT.replace(
        "        - { name: size, type: INT, required: true }\n    semantics:\n"
        "      entities:\n        - { name: customer, type: primary, expr: customer_id }\n",
        "        - { name: size, type: INT, required: true }\n"
        "      invariants:\n"
        "        - id: customer_size\n"
        "          derive: size\n"
        "          aggregate: { function: count, from: orders, via: order_to_customer }\n"
        "    semantics:\n"
        "      entities:\n        - { name: customer, type: primary, expr: customer_id }\n",
        1,
    )
    assert contract != CONTRACT
    result = _init(tmp_path, _profile([_ungrouped(), _by_size()]), contract)
    assert result.exit_code == 0, result.stderr
    assert "given" not in _entry(tmp_path)
    assert "summary over the children" in _ledger(result, "relationships/0/statistics/1")["note"]


def test_bins_that_are_not_the_columns_are_passed_over(tmp_path: Path) -> None:
    grouped = _by_size()
    grouped["groupByBins"]["bins"] = ["0", "2000", "4000"]
    grouped["groups"][1]["group"] = "2000"
    result = _init(tmp_path, _profile([_ungrouped(), grouped]))
    assert result.exit_code == 0, result.stderr
    assert "given" not in _entry(tmp_path)
    assert "bins" in _ledger(result, "relationships/0/statistics/1")["note"]


def test_counts_that_are_not_the_shapes_classes_are_passed_over(tmp_path: Path) -> None:
    grouped = _by_segment()
    grouped["bins"] = ["0", "1", "4", "10"]
    result = _init(tmp_path, _profile([_ungrouped(), grouped]))
    assert result.exit_code == 0, result.stderr
    assert "given" not in _entry(tmp_path)
    assert "bins of the entry's children" in _ledger(result, "relationships/0/statistics/1")["note"]


def test_a_profile_with_no_grouped_statistic_writes_the_entry_it_wrote_before(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _profile([_ungrouped()]))
    assert result.exit_code == 0, result.stderr
    assert set(_entry(tmp_path)) == {"emit", "relationship", "children"}


# ---------------------------------------------------------------------------
# Noise
# ---------------------------------------------------------------------------


def _noisy(statistic: Dict[str, Any], scale: str = "0.5") -> Dict[str, Any]:
    noisy = copy.deepcopy(statistic)
    noisy["disclosure"] = "noisy"
    noisy["noise"] = {"mechanism": "gaussian", "scale": scale, "privacyLoss": {"rho": "2"}}
    for group in noisy["groups"]:
        group["noisyCounts"] = [str(count) for count in group.pop("binCounts")]
        group.pop("k")
    return noisy


def test_a_noisy_grouped_histogram_is_read_as_its_point_estimate_and_the_ledger_carries_its_noise(
    tmp_path: Path,
) -> None:
    grouped = _noisy(_by_segment())
    grouped["groups"][0]["noisyCounts"][0] = "-3.2"  # a noisy cell can be negative
    document = _profile([_noisy(_ungrouped()), grouped], privacy=True)
    result = _init(tmp_path, document)
    assert result.exit_code == 0, result.stderr
    entry = _entry(tmp_path)
    assert entry["given"] == "segment"
    shares = [Fraction(text) for text in entry["childrenGiven"]["business"]]
    assert sum(shares) == 1 and all(share >= 0 for share in shares)
    noise = _ledger(result, "relationships/0/statistics/1")["noise"]
    assert noise["mechanism"] == "gaussian" and noise["variance"] == "0.25"


# ---------------------------------------------------------------------------
# The whole way
# ---------------------------------------------------------------------------


def test_the_bundle_plans_applies_and_verifies_with_each_class_within_tolerance(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _profile([_ungrouped(), _by_segment()]))
    assert result.exit_code == 0, result.stderr
    profile = tmp_path / "shop.profile.json"
    applied = apply(tmp_path / "bundle", state_dir=tmp_path / "state", profile=profile)
    assert applied.success, applied.stderr
    verified = verify(
        tmp_path / "bundle",
        run_id=str(applied.artifacts["run_id"]),
        state_dir=tmp_path / "state",
        profile=profile,
    )
    assert verified.success, verified.stderr
    tables = verified.artifacts["scorecard"]["document"]["tables"]
    rows = [row for table in tables for row in table["properties"]]
    assert all(row["outcome"] == "within" for row in rows)
    assert any("children" in row["propertyId"] or "crossTable" in str(row) for row in rows)


def test_a_noisy_grouping_by_a_link_is_read_as_its_point_estimate(tmp_path: Path) -> None:
    from tests.author import test_catalogue_init as catalogue

    document = catalogue.profile_document()
    document["_schemaURL"] = SCHEMA_URL_1_4_0
    grouped = document["tables"]["line"]["columns"]["l_size"]["statistics"][1]
    grouped["disclosure"] = "noisy"
    grouped["noise"] = {"mechanism": "gaussian", "scale": "0.5", "privacyLoss": {"rho": "2"}}
    for group in grouped["groups"]:
        group["noisyCounts"] = [str(count) for count in group.pop("levelCounts")]
        group.pop("k")
    document["disclosureRules"]["noisy"] = {}
    document["privacy"] = {
        "measure": "zeroConcentratedDP",
        "privacyLoss": {"rho": "1000"},
        "privacyUnit": {"expose": "line"},
        "publicStructure": True,
    }
    result = _init(tmp_path, document, catalogue.contract_text())
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    column = yaml.safe_load(text)["extensions"][NAMESPACE]["emits"]["line"]["fidelity"]["columns"][
        "l_size"
    ]
    assert column["given"] == "product.category"
    weights = {key: Fraction(value["large"]) for key, value in column["levelWeightsGiven"].items()}
    assert weights["tools"] > weights["paint"] > weights["lighting"]
    found = _ledger(result, "l_size/statistics/1")
    assert found["state"] == "used" and found["noise"]["variance"] == "0.25"
