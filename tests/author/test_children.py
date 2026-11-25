# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Children per parent become a foreign key's spread (ADR 0014 section 5).

A relationship the counted layout can place is written as a ``counts`` entry instead
(``tests/author/test_counts_init.py``). What stays here is the spread, which every other
relationship keeps: one whose foreign key is nullable with a published share of nulls, which
the layout cannot place. Pinned here, through ``fluxsim init``:

* a relationship's histogram of children per parent lays its classes over the
  parent's keys, each a share of the keys equal to its share of the parents and
  weighted by its children, the class of none at 0;
* a mean alone leaves the spread even; a relationship the contract does not
  declare is unused, with the reason;
* a foreign key with a histogram of its own keeps each of its bins' children,
  the classes laid out inside each bin.
"""

from __future__ import annotations

import collections
import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.stages.run import run
from tests.bundles import init_from_texts

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
SAMPLE = {"min": 950, "max": 1050}
BINS = ["0", "1", "2", "3", "5", "10", "20", "100"]
PARENTS = [150, 150, 150, 200, 200, 100, 50]

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: children.shop
name: Children shop
metadata:
  owner:
    team: shop-team
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


def _metric(definition: str, value: Any) -> Dict[str, Any]:
    return {
        "definition": definition,
        "status": "F",
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "value": value,
    }


def _histogram(bins: List[str], counts: List[int], k: int) -> Dict[str, Any]:
    return {
        "definition": "marginalHistogram/1",
        "bins": bins,
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "groups": [{"group": "", "k": k, "binCounts": counts, "status": "F"}],
    }


def _mean(total: int, k: int) -> Dict[str, Any]:
    return {
        "definition": "mean/1",
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "groups": [{"group": "", "k": k, "Sx": str(total * 2**16), "status": "F"}],
    }


def _profile(
    statistics: List[Dict[str, Any]],
    *,
    child: Optional[Dict[str, Any]] = None,
    columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    key = {"count": _metric("count/1", 1000), "min": _metric("min/1", "1")}
    key["max"] = _metric("max/1", "1000")
    order_key = {"count": _metric("count/1", 5000), "min": _metric("min/1", "1")}
    order_key["max"] = _metric("max/1", "5000")
    return {
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
            "customer": {
                "rowCount": 1000,
                "columns": {"customer_id": {"domain": "number", "metrics": key}},
            },
            "orders": {
                "rowCount": 5000,
                "columns": {
                    "order_id": {"domain": "number", "metrics": order_key},
                    "customer_id": child
                    or {"domain": "number", "metrics": {"count": _metric("count/1", 5000)}},
                },
            },
        },
        "relationships": [
            {
                "parent": {"table": "customer", "columns": ["customer_id"]},
                "child": {"table": "orders", "columns": columns or ["customer_id"]},
                "statistics": statistics,
            }
        ],
    }


_FOREIGN_KEY = "        - { name: customer_id, type: INT, required: true }\n"
_AT = CONTRACT.rindex(_FOREIGN_KEY)
#: The contract whose ``orders.customer_id`` may be NULL.
NULLABLE = (
    CONTRACT[:_AT]
    + _FOREIGN_KEY.replace("required: true", "required: false")
    + CONTRACT[_AT + len(_FOREIGN_KEY) :]
)


def _uncounted(profile: Dict[str, Any]) -> Dict[str, Any]:
    """*profile* with a share of the foreign key's rows NULL, so the layout cannot place it."""
    found = copy.deepcopy(profile)
    key = found["tables"]["orders"]["columns"]["customer_id"]
    key.setdefault("metrics", {})["nullCount"] = _metric("nullCount/1", 250)
    return found


def _init(tmp_path: Path, profile: Dict[str, Any], *, counted: bool = True) -> Any:
    """Write a bundle from *profile*; with ``counted=False`` the foreign key is nullable."""
    contract = CONTRACT
    if not counted:
        profile, contract = _uncounted(profile), NULLABLE
    result = init_from_texts(tmp_path, contract, profile, ("shop.fluid.yaml", "shop.profile.json"))
    assert result.exit_code == 0, result.stderr
    return result


def _foreign(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block["emits"]["orders"]["fidelity"]["columns"]["customer_id"]


def _states(result: Any) -> Dict[str, Dict[str, Any]]:
    return {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}


def _children(tmp_path: Path) -> List[int]:
    state = tmp_path / "state"
    outcome = run(tmp_path / "bundle", state_dir=state, profile=tmp_path / "shop.profile.json")
    assert outcome.success, outcome.stderr
    run_id = outcome.artifacts["plan"]["run_id"]
    rows = (state / "runs" / run_id / "emits" / "orders.ndjson").read_text(encoding="utf-8")
    counts = collections.Counter(json.loads(line)[1] for line in rows.splitlines() if line)
    return [counts.get(key, 0) for key in range(1, 1001)]


def test_the_classes_are_laid_over_the_parent_s_keys(tmp_path: Path) -> None:
    profile = _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)])
    result = _init(tmp_path, profile, counted=False)
    spread = _foreign(tmp_path)
    # Busiest class first: 50 keys of 20 or more (its mean solved to 20 from the total of
    # 5000), 100 of 10 to 19, ... and 150 keys of none, at weight 0.
    assert spread["bins"] == ["1", "51", "151", "351", "551", "701", "851", "1000"]
    assert spread["binWeights"] == ["0.2", "0.29", "0.28", "0.14", "0.06", "0.03", "0"]
    states = _states(result)
    assert states["/relationships/0/statistics/0"]["state"] == "used"
    assert states["/relationships/0/statistics/1"]["state"] == "used"
    assert "e^-m" in result.stdout


def test_a_mean_alone_leaves_the_spread_even(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile([_mean(5000, 1000)]))
    spread = _foreign(tmp_path)
    assert spread["binWeights"] == ["1"]
    assert spread["bins"] == ["1", "1000"]
    assert "give this mean" in _states(result)["/relationships/0/statistics/0"]["note"]


def test_a_relationship_the_contract_does_not_declare_is_unused(tmp_path: Path) -> None:
    profile = _profile([_histogram(BINS, PARENTS, 1000)], columns=["order_id"])
    result = _init(tmp_path, profile)
    state = _states(result)["/relationships/0/statistics/0"]
    assert state["state"] == "unused"
    assert "declares no such relationship" in state["note"]


def test_a_foreign_key_s_own_histogram_keeps_each_bin_s_children(tmp_path: Path) -> None:
    child = {
        "domain": "number",
        "metrics": {"count": _metric("count/1", 5000)},
        "statistics": [_histogram(["1", "501", "1000"], [4000, 1000], 5000)],
    }
    profile = _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)], child=child)
    result = _init(tmp_path, profile, counted=False)
    spread = _foreign(tmp_path)
    edges = [int(edge) for edge in spread["bins"]]
    weights = [float(weight) for weight in spread["binWeights"]]
    below = sum(weight for edge, weight in zip(edges, weights, strict=False) if edge < 501)
    assert abs(below - 0.8) < 1e-6  # the first of its own bins keeps 4000 of 5000 children
    states = _states(result)
    assert states["/tables/orders/columns/customer_id/statistics/0"]["state"] == "used"


def test_one_parent_with_every_child_at_the_top_keeps_them(tmp_path: Path) -> None:
    child = {
        "domain": "number",
        "metrics": {"count": _metric("count/1", 5000), "sum": _metric("sum/1", "2500000")},
    }
    profile = _profile([_histogram(["0", "1", "2"], [999, 1], 1000)], child=child)
    _init(tmp_path, profile, counted=False)
    assert _children(tmp_path).count(0) >= 990  # all but the top keys stay childless


def test_many_classes_keep_the_childless_share(tmp_path: Path) -> None:
    parents = [400] + [60] * 10 + [0] * 89
    bins = [str(edge) for edge in range(101)]
    profile = _profile([_histogram(bins, parents, 1000), _mean(5000, 1000)])
    _init(tmp_path, profile, counted=False)
    assert abs(_children(tmp_path).count(0) / 1000 - 0.4) < 0.05


def _quantile(bins: List[str], position: int, below: int, through: int) -> Dict[str, Any]:
    return {
        "definition": "quantile/2",
        "level": "0.5",
        "bins": bins,
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "groups": [
            {
                "group": "",
                "k": 5000,
                "quantileBin": position,
                "binBelow": below,
                "binThrough": through,
                "status": "F",
            }
        ],
    }


LADDER = ["-9223372036854775808", "1", "251", "501", "1000", "9223372036854775807"]


def test_a_foreign_key_s_binned_quantile_cuts_the_keys_the_classes_are_laid_in(
    tmp_path: Path,
) -> None:
    child = {
        "domain": "number",
        "metrics": {"count": _metric("count/1", 5000)},
        "statistics": [_quantile(LADDER, 2, 2000, 4000)],
    }
    profile = _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)], child=child)
    profile["_schemaURL"] = profile["_schemaURL"].replace("1-0-0", "1-1-0")
    result = _init(tmp_path, profile, counted=False)
    spread = _foreign(tmp_path)
    edges = [int(edge) for edge in spread["bins"]]
    weights = [float(weight) for weight in spread["binWeights"]]
    below = lambda cut: sum(  # noqa: E731
        weight for edge, weight in zip(edges, weights, strict=False) if edge < cut
    )
    assert abs(below(251) - 0.4) < 1e-6  # 2000 of 5000 children lie below key 251
    assert abs(below(501) - 0.8) < 1e-6
    note = _states(result)["/tables/orders/columns/customer_id/statistics/0"]
    assert note["state"] == "used"


def test_a_foreign_key_that_gets_a_spread_keeps_no_passed_over_reason(tmp_path: Path) -> None:
    child = {
        "domain": "number",
        "metrics": {
            "count": _metric("count/1", 5000),
            "sum": _metric("sum/1", "2500000"),
            "min": _metric("min/1", "1"),
            "max": _metric("max/1", "5000"),
        },
        "statistics": [_mean(2500000, 5000)],
    }
    profile = _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)], child=child)
    result = _init(tmp_path, profile, counted=False)
    states = _states(result)
    prefix = "/tables/orders/columns/customer_id"
    assert states[f"{prefix}/metrics/min"]["state"] == "used"
    assert "within the parent's keys" in states[f"{prefix}/metrics/min"]["note"]
    assert states[f"{prefix}/statistics/0"]["state"] == "used"  # its mean agrees with sum / count
    for name in ("min", "max", "sum", "count"):
        assert "gives no spread for" not in states[f"{prefix}/metrics/{name}"]["note"]


def test_a_foreign_key_s_binned_quantile_with_one_cut_lays_the_keys_out_without_it(
    tmp_path: Path,
) -> None:
    # One interior edge of the ladder is one point: it bounds no bucket, so the keys are laid
    # out as without the statistic, and the ledger says it was passed over.
    ladder = ["-9223372036854775808", "500", "9223372036854775807"]
    child = {
        "domain": "number",
        "metrics": {"count": _metric("count/1", 5000)},
        "statistics": [_quantile(ladder, 0, 0, 4000)],
    }
    profile = _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)], child=child)
    profile["_schemaURL"] = profile["_schemaURL"].replace("1-0-0", "1-1-0")
    result = _init(tmp_path, profile, counted=False)
    plain = tmp_path / "plain"
    plain.mkdir()
    _init(plain, _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)]), counted=False)
    assert _foreign(tmp_path) == _foreign(plain)
    state = _states(result)["/tables/orders/columns/customer_id/statistics/0"]
    assert state["state"] == "unused"


def test_the_cuts_of_fewer_than_two_points_are_none() -> None:
    from flux_engine.author.columns import binned_cuts

    tokens = ("tables", "orders", "columns", "customer_id")
    ladder = ["0", "500", "2000", "5000"]
    one = {"statistics": [_quantile(["0", "500", "5000"], 0, 0, 4000)]}
    assert binned_cuts(one, tokens) == []
    twice = {"statistics": [_quantile(ladder, 1, 100, 100), _quantile(ladder, 1, 100, 100)]}
    assert [cut[0] for cut in binned_cuts(twice, tokens)] == [500, 2000]
    assert binned_cuts({"statistics": []}, tokens) == []


def test_a_foreign_key_end_outside_the_parent_s_keys_is_passed_with_the_span() -> None:
    from flux_engine.author.children import FOREIGN_OWN, _check_ends
    from flux_engine.author.columns import ColumnPlan

    tokens = ("tables", "orders", "columns", "customer_id")
    entry = {"metrics": {"min": _metric("min/1", "0"), "max": _metric("max/1", "5000")}}
    plan = ColumnPlan(name="customer_id", passed=[(tokens + ("metrics", "sum"), FOREIGN_OWN)])
    used = _check_ends(plan, entry, tokens, 1, 1000)
    assert used == []
    why = {pointer[-1]: text for pointer, text in plan.passed}
    assert (
        why["min"]
        == "the published min is 0, and the parent's keys span 1 to 1000: a foreign key is drawn over them only"
    )  # noqa: E501
    assert why["max"].startswith("the published max is 5000, and the parent's keys span 1 to 1000")
    assert "adds no bound" in why["sum"]  # the passed-over reason a spread replaces
    inside = {"metrics": {"min": _metric("min/1", "1"), "max": _metric("max/1", "5000")}}
    plan = ColumnPlan(name="customer_id")
    used = _check_ends(plan, inside, tokens, 1, 1000)
    assert [pointer[-1] for pointer, _ in used] == ["min"]
    assert [pointer[-1] for pointer, _ in plan.passed] == ["max"]


def test_a_mean_statistic_that_disagrees_with_sum_over_count_is_not_read_beside_it() -> None:
    from fractions import Fraction

    from flux_engine.author.children import _foreign_mean

    entry = {
        "metrics": {"sum": _metric("sum/1", "2500000"), "count": _metric("count/1", 5000)},
        "statistics": [_mean(2500000, 5000)],
    }
    mean, sources = _foreign_mean(entry, 16) or (None, [])
    assert mean == 500
    assert sources == [("metrics", "sum"), ("metrics", "count"), ("statistics", "0")]
    entry["statistics"] = [_mean(4000000, 5000)]
    mean, sources = _foreign_mean(entry, 16) or (None, [])
    assert mean == 500 and sources == [("metrics", "sum"), ("metrics", "count")]
    only = {"metrics": {}, "statistics": [_mean(4000000, 5000)]}
    assert _foreign_mean(only, 16) == (Fraction(800), [("statistics", "0")])
    assert _foreign_mean({"metrics": {}, "statistics": []}, 16) is None


def test_a_key_that_a_foreign_key_widens_is_checked_against_the_span_it_ends_with(
    tmp_path: Path,
) -> None:
    profile = _profile([_histogram(BINS, PARENTS, 1000)])
    ladder = ["-9223372036854775808", "250", "500", "9223372036854775807"]
    key = profile["tables"]["customer"]["columns"]["customer_id"]
    key["metrics"].pop("min")
    key["metrics"].pop("max")
    # Keys 1 to 1000 leave 249 of 1000 records below 250: the span before it is widened.
    key["statistics"] = [_quantile(ladder, 0, 0, 249)]
    profile["_schemaURL"] = profile["_schemaURL"].replace("1-0-0", "1-1-0")
    child = {
        "domain": "number",
        "metrics": {
            "count": _metric("count/1", 5000),
            "min": _metric("min/1", "1"),
            "max": _metric("max/1", "3000"),
        },
    }
    profile["tables"]["orders"]["columns"]["customer_id"] = child
    key["statistics"][0]["groups"][0]["k"] = 1000
    result = _init(tmp_path, profile, counted=False)
    block = yaml.safe_load((tmp_path / "bundle" / "simulation.flux.yml").read_text("utf-8"))
    declared = block["extensions"][NAMESPACE]["emits"]["customer"]["fidelity"]["columns"]
    assert declared["customer_id"]["bins"] == ["1", "3000"]
    state = _states(result)["/tables/customer/columns/customer_id/statistics/0"]
    assert state["state"] == "unused"
    assert "keys laid evenly over the span the bundle declares leave" in state["note"]


def _own(tmp_path: Path, statistics: List[Dict[str, Any]]) -> Any:
    """A foreign key the profile gives no relationship for, publishing only its own values."""
    child = {
        "domain": "number",
        "metrics": {"count": _metric("count/1", 5000)},
        "statistics": statistics,
    }
    profile = _profile([], child=child)
    del profile["relationships"]
    profile["_schemaURL"] = profile["_schemaURL"].replace("1-0-0", "1-1-0")
    return _init(tmp_path, profile)


def test_a_foreign_key_with_no_relationship_is_laid_out_by_its_own_quantiles(
    tmp_path: Path,
) -> None:
    result = _own(tmp_path, [_quantile(LADDER, 2, 2000, 4000)])
    spread = _foreign(tmp_path)
    assert spread["bins"] == ["1", "251", "501", "1000"]
    assert spread["binWeights"] == ["0.4", "0.4", "0.2"]
    assert _states(result)["/tables/orders/columns/customer_id/statistics/0"]["state"] == "used"


def test_its_mean_is_checked_against_the_layout(tmp_path: Path) -> None:
    quantile = _quantile(LADDER, 2, 2000, 4000)
    result = _own(tmp_path, [quantile, _mean(1752500, 5000)])
    assert _states(result)["/tables/orders/columns/customer_id/statistics/1"]["state"] == "used"


def test_a_quantile_with_one_cut_stays_unused_with_its_reason(tmp_path: Path) -> None:
    ladder = ["-9223372036854775808", "500", "9223372036854775807"]
    result = _own(tmp_path, [_quantile(ladder, 0, 0, 4000)])
    state = _states(result)["/tables/orders/columns/customer_id/statistics/0"]
    assert state["state"] == "unused" and state["note"]


def test_the_spread_of_a_foreign_key_cites_the_histogram_it_is_laid_out_from(
    tmp_path: Path,
) -> None:
    import yaml

    _init(tmp_path, _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)]), counted=False)
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    register = yaml.safe_load(text)["extensions"]["com.agentics.flux/1"]["parameters"]
    cited = {
        entry["pointer"].rsplit("/", 1)[-1]: entry
        for entry in register
        if entry["pointer"].endswith(("/bins", "/binWeights"))
        and "/customer_id/" in entry["pointer"]
    }
    assert set(cited) == {"bins", "binWeights"}
    for entry in cited.values():
        assert entry["source"] == "profile"
        assert entry["statistic"]["pointer"].startswith("/relationships/0/statistics/")
        assert entry["statistic"]["definition"] == "marginalHistogram/1"
