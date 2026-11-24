# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` draws a binned number column from a ``cliqueMarginal/2`` (ADR 0036 decision 4).

Pinned here, through the command and a run: a number or timestamp column of a clique is drawn from
the clique's edges, alone or given its separator, and a column drawn given it keys its weights by
the text of each bin's lower edge as the column's own bins write it; the clique's edges win over the
column's histogram and the ledger says so; edges that do not cover the range the contract declares
are refused (FLX-3402); a binned column is never a parent of the Chow-Liu forest; and a plan on
kernel 0.1.0, which draws no column given a bin, leaves a binned clique alone. The bundles plan,
apply and verify with every property within its tolerance.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.calibrate.junction import junction_tree
from flux_engine.cli._init import run_init
from flux_engine.stages.apply import apply
from flux_engine.stages.verify import verify
from tests.author import rank_profile as rp

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
ROWS = 20000


@pytest.fixture(scope="module")
def data() -> List[rp.Row]:
    return rp.rows(6000)


def _canon(edges: List[str]) -> List[str]:
    """Edges as the canonical decimal text a bundle spells them in: no trailing zero after the point."""
    return [format(Decimal(edge).normalize(), "f") for edge in edges]


def _document(
    data: List[rp.Row], *cliques: Any, ranks: bool = False, **edges: List[str]
) -> Dict[str, Any]:
    document = rp.profile(data, None if ranks else {}, rows=ROWS)
    for columns in cliques:
        rp.add_clique(document, data, columns, edges=edges)
    return document


def _init(tmp_path: Path, profile: Dict[str, Any], contract: str = rp.CONTRACT) -> Any:
    (tmp_path / "ledger.fluid.yaml").write_text(contract, encoding="utf-8")
    (tmp_path / "ledger.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "ledger.fluid.yaml"),
        profile=str(tmp_path / "ledger.profile.json"),
    )


def _columns(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    columns: Dict[str, Any] = block["emits"]["account"]["fidelity"]["columns"]
    return columns


def _ledger(result: Any, ending: str) -> Dict[str, Any]:
    (entry,) = [
        each
        for each in result.artifacts["ledger"]["statistics"]
        if each["pointer"].endswith(ending)
    ]
    return entry  # type: ignore[no-any-return]


def _properties(tmp_path: Path) -> List[Dict[str, Any]]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    found: List[Dict[str, Any]] = block["emits"]["account"]["fidelity"]["properties"]
    return found


def _verified(tmp_path: Path) -> Dict[str, Any]:
    profile = tmp_path / "ledger.profile.json"
    applied = apply(tmp_path / "bundle", state_dir=tmp_path / "state", profile=profile)
    assert applied.success, applied.stderr
    result = verify(
        tmp_path / "bundle",
        run_id=str(applied.artifacts["run_id"]),
        state_dir=tmp_path / "state",
        profile=profile,
    )
    assert result.success, result.stderr
    (table,) = result.artifacts["scorecard"]["document"]["tables"]
    return {row["propertyId"]: row for row in table["properties"]}


# ---------------------------------------------------------------------------
# A binned column in a clique
# ---------------------------------------------------------------------------


def test_a_binned_column_is_drawn_over_the_cliques_edges_and_the_category_is_given_its_bin(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    document = _document(data, ["balance", "region"])
    clique = document["tables"]["account"]["cliques"][0]
    edges = clique["domains"][0]["bins"]
    result = _init(tmp_path, document)
    assert result.exit_code == 0, result.stderr
    columns = _columns(tmp_path)
    balance, region = columns["balance"], columns["region"]
    assert balance["bins"] == edges and len(balance["binWeights"]) == len(edges) - 1
    assert balance["range"] == {"min": edges[0], "max": edges[-1]}
    assert "nullFraction" not in balance and "given" not in balance
    assert region["given"] == "balance"
    assert list(region["levelWeightsGiven"]) == edges[:-1]
    assert all(
        set(row) == {"east", "north", "west"} for row in region["levelWeightsGiven"].values()
    )
    assert "allocation" not in region
    assert _ledger(result, "cliques/0")["state"] == "used"


def test_the_cliques_edges_win_over_the_columns_histogram_and_the_ledger_names_it(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    own = rp.profile(data, {}, rows=ROWS)["tables"]["account"]["columns"]["balance"]
    own_edges = own["statistics"][0]["bins"]
    cut = ["0", "500", "1000", "1500", "2400"]
    assert cut != own_edges
    result = _init(tmp_path, _document(data, ["balance", "region"], balance=cut))
    assert result.exit_code == 0, result.stderr
    balance = _columns(tmp_path)["balance"]
    assert balance["bins"] == cut and balance["range"] == {"min": "0", "max": "2400"}
    note = _ledger(result, "columns/balance/statistics/0")
    assert note["state"] == "unused" and "which win over the column's histogram" in note["note"]
    # The property over the column's own histogram is replaced by one over the clique's bins.
    sums = [
        each
        for each in _properties(tmp_path)
        if each["property"] == "marginalHistogram" and each["column"] == "balance"
    ]
    assert sums and all(each["bins"] == cut for each in sums)


def test_a_number_column_given_a_number_column_keys_its_weights_by_the_lower_edges(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    document = _document(data, ["amount", "balance"])
    amount_edges = document["tables"]["account"]["cliques"][0]["domains"][0]["bins"]
    result = _init(tmp_path, document)
    assert result.exit_code == 0, result.stderr
    balance = _columns(tmp_path)["balance"]
    assert balance["given"] == "amount"
    assert list(balance["binWeightsGiven"]) == _canon(amount_edges)[:-1]
    grouped = [
        each
        for each in _properties(tmp_path)
        if each.get("groupByBins", {}).get("column") == "balance"
    ]
    assert [each["column"] for each in grouped] == ["amount"]
    assert (
        grouped[0]["groupByBins"]["bins"]
        == document["tables"]["account"]["cliques"][0]["domains"][1]["bins"]
    )
    rows = _verified(tmp_path)
    assert all(row["outcome"] == "within" for row in rows.values())
    assert any("#bins" in key for key in rows)


def test_a_timestamp_parent_is_keyed_by_its_bins_as_they_are_spelled(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    result = _init(tmp_path, _document(data, ["opened_at", "region"]))
    assert result.exit_code == 0, result.stderr
    columns = _columns(tmp_path)
    assert list(columns["region"]["levelWeightsGiven"]) == columns["opened_at"]["bins"][:-1]
    assert all(key.endswith("Z") for key in columns["region"]["levelWeightsGiven"])
    assert all(row["outcome"] == "within" for row in _verified(tmp_path).values())


def test_a_column_given_two_binned_columns_nests_its_weights_in_the_order_it_is_given(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    result = _init(tmp_path, _document(data, ["amount", "balance", "region"]))
    assert result.exit_code == 0, result.stderr
    columns = _columns(tmp_path)
    region = columns["region"]
    assert region["given"] == ["amount", "balance"]
    assert list(region["levelWeightsGiven"]) == columns["amount"]["bins"][:-1]
    for inner in region["levelWeightsGiven"].values():
        assert list(inner) == columns["balance"]["bins"][:-1]
    assert all(row["outcome"] == "within" for row in _verified(tmp_path).values())


def test_a_clique_that_holds_no_number_draws_as_it_did(tmp_path: Path, data: List[rp.Row]) -> None:
    document = _document(data)
    table = document["tables"]["account"]
    table["columns"]["channel"] = rp._region([row[3] for row in data])
    contract = rp.CONTRACT.replace(
        "        - { name: region,",
        "        - { name: channel, type: \"enum('east','north','west')\", required: true }\n"
        "        - { name: region,",
    )
    levels = ["east", "north", "west"]
    table["cliques"] = [
        {
            "definition": "cliqueMarginal/2",
            "columns": ["channel", "region"],
            "domains": [{"levels": levels}, {"levels": levels}],
            "noisyCounts": ["30", "10", "10", "10", "30", "10", "10", "10", "30"],
            "noise": {"mechanism": "gaussian", "scale": "1", "privacyLoss": {"rho": "0.5"}},
        }
    ]
    table["rowCount"] = ROWS
    document["privacy"] = {
        "measure": "zeroConcentratedDP",
        "privacyLoss": {"rho": "1000"},
        "privacyUnit": {"expose": "account"},
        "publicStructure": True,
    }
    result = _init(tmp_path, document, contract)
    assert result.exit_code == 0, result.stderr
    columns = _columns(tmp_path)
    assert columns["region"]["given"] == "channel"
    assert "levels" in columns["channel"] and "bins" not in columns["channel"]


# ---------------------------------------------------------------------------
# What is refused, and what is left alone
# ---------------------------------------------------------------------------

RANGED = rp.CONTRACT.replace(
    "        - { name: balance, type: INT, required: false }",
    "        - name: balance\n"
    "          type: INT\n"
    "          required: false\n"
    "          validationRules:\n"
    "            - { type: range, constraint: 'between 0 and 5000' }",
)


def test_edges_that_do_not_cover_the_contracts_range_are_refused(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    assert RANGED != rp.CONTRACT
    result = _init(tmp_path, _document(data, ["balance", "region"]), RANGED)
    assert result.exit_code == 1
    assert (
        "FLX-3402" in result.stderr
        and "do not cover the range the contract declares" in result.stderr
    )
    assert not (tmp_path / "bundle").exists()


def test_edges_that_cover_the_contracts_range_are_drawn(tmp_path: Path, data: List[rp.Row]) -> None:
    result = _init(
        tmp_path, _document(data, ["balance", "region"], balance=["0", "1000", "5000"]), RANGED
    )
    assert result.exit_code == 0, result.stderr
    assert _columns(tmp_path)["balance"]["range"] == {"min": "0", "max": "5000"}


def test_edges_that_reach_past_the_contracts_range_are_refused_and_named(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    # The range of the column is written from the edges, so edges of -500 to 6000 over a contract
    # that allows 0 to 5000 had the run emit balances up to 5995.
    document = _document(data, ["balance", "region"], balance=["-500", "0", "1000", "2000", "6000"])
    result = _init(tmp_path, document, RANGED)
    assert result.exit_code == 1
    assert "FLX-3402" in result.stderr
    assert "reach past the range the contract declares" in result.stderr
    assert "lowest edge -500 lies beyond 0" in result.stderr
    assert not (tmp_path / "bundle").exists()


def test_edges_that_reach_past_only_the_top_of_the_contracts_range_are_refused(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    document = _document(data, ["balance", "region"], balance=["0", "1000", "2000", "6000"])
    result = _init(tmp_path, document, RANGED)
    assert result.exit_code == 1
    assert "highest edge 6000 lies beyond 5000" in result.stderr
    assert not (tmp_path / "bundle").exists()


def test_a_timestamp_clique_column_is_held_to_the_contracts_range_too(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    ranged = rp.CONTRACT.replace(
        "        - { name: opened_at, type: TIMESTAMP, required: false }",
        "        - name: opened_at\n"
        "          type: TIMESTAMP\n"
        "          required: false\n"
        "          validationRules:\n"
        "            - { type: range, constraint: 'between 1000 and 2000' }",
    )
    assert ranged != rp.CONTRACT
    document = _document(data, ["opened_at", "region"], opened_at=["500", "1000", "2000"])
    result = _init(tmp_path, document, ranged)
    assert result.exit_code == 1
    assert "FLX-3402" in result.stderr and "reach past the range" in result.stderr
    assert not (tmp_path / "bundle").exists()


def test_a_column_that_holds_nulls_is_not_drawn_from_a_clique(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    document = _document(data, ["balance", "region"])
    document["tables"]["account"]["columns"]["balance"]["metrics"]["nullCount"]["value"] = 600
    result = _init(tmp_path, document)
    assert result.exit_code == 1
    assert "holds nulls" in result.stderr and "FLX-3402" in result.stderr


def test_a_grouped_statistic_by_the_bins_of_a_number_column_is_no_given_of_the_forest(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    document = _document(data)
    columns = document["tables"]["account"]["columns"]
    by = columns["amount"]["statistics"][0]["bins"]
    edges = columns["balance"]["statistics"][0]["bins"]
    at = {name: position for position, name in enumerate(rp.NUMBERS)}
    groups = []
    for index in range(len(by) - 1):
        inside = [row[at["balance"]] for row in data if rp._cell(row[at["amount"]], by) == index]
        groups.append(
            {
                "group": _canon(by)[index],
                "k": len(inside),
                "binCounts": rp._counts(inside, edges),
                "status": "F",
            }
        )
    columns["balance"]["statistics"].append(
        {
            "definition": "marginalHistogram/1",
            "bins": edges,
            "groupByBins": {"column": "amount", "bins": by},
            "disclosure": "none",
            "sampleSize": {"min": 1},
            "groups": groups,
        }
    )
    result = _init(tmp_path, document)
    assert result.exit_code == 0, result.stderr
    assert "given" not in _columns(tmp_path)["balance"]
    assert _ledger(result, "columns/balance/statistics/1")["state"] == "unused"


def test_a_plan_on_kernel_0_1_0_leaves_a_binned_clique_alone_and_says_why(
    tmp_path: Path, data: List[rp.Row], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("flux_engine.core.kernels.new_plan_is_counter_based", lambda: False)
    result = _init(tmp_path, _document(data, ["balance", "region"]))
    assert result.exit_code == 0, result.stderr
    columns = _columns(tmp_path)
    assert "given" not in columns["region"] and "binWeightsGiven" not in columns["balance"]
    note = _ledger(result, "cliques/0")
    assert note["state"] == "unused" and "kernel 0.1.0" in note["note"]


def test_a_column_given_a_binned_column_is_never_allocated_exactly(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    document = _document(data, ["balance", "region"])
    document["tables"]["account"]["rowCount"] = 200  # a small table, whose columns are exact
    result = _init(tmp_path, document)
    assert result.exit_code == 0, result.stderr
    columns = _columns(tmp_path)
    assert columns["balance"].get("allocation") == "exact"
    assert "allocation" not in columns["region"]


# ---------------------------------------------------------------------------
# The junction tree
# ---------------------------------------------------------------------------


def test_the_junction_tree_arranges_binned_columns_as_it_arranges_any_column() -> None:
    steps = junction_tree(
        [["amount", "balance"], ["balance", "region"]],
        order=["amount", "balance", "opened_at", "region"],
        names=["clique 0", "clique 1"],
    )
    first, second = steps
    assert (first.separator, first.new) == ((), ("amount", "balance"))
    assert (second.separator, second.new) == (("balance",), ("region",))
    # A column of the second clique is drawn given the column the cliques share, a bin or a level.
    assert second.parents_of(0, ["amount", "balance", "opened_at", "region"]) == ("balance",)


def test_cliques_of_both_definitions_arrange_into_one_tree(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    document = _document(data, ["amount", "balance"], ["balance", "region"])
    result = _init(tmp_path, document)
    assert result.exit_code == 0, result.stderr
    columns = _columns(tmp_path)
    assert "given" not in columns["amount"]
    assert columns["balance"]["given"] == "amount"
    assert columns["region"]["given"] == "balance"
    assert all(row["outcome"] == "within" for row in _verified(tmp_path).values())


def test_the_ledger_names_a_pair_and_a_clique_the_registers_unused_list_cannot_hold(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    document = _document(data, ["amount", "balance"], ranks=True)
    document["tables"]["account"]["cliques"][0]["noisyCounts"][0] = "0"
    result = _init(tmp_path, document)
    assert result.exit_code == 0, result.stderr
    # The pair of two columns a clique draws is passed over, with its reason, in the ledger.
    note = _ledger(result, "columnPairs/0/statistics/0")
    assert note["state"] == "unused" and "a clique draws it" in note["note"]
    # The register cites no definition it does not hold, so it lists neither.
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    unused = yaml.safe_load(text)["extensions"][NAMESPACE]["unused"]
    named = {item["statistic"].get("definition") for item in unused}
    assert not named & {"kendallTau/1", "spearmanRho/1", "cliqueMarginal/2"}
