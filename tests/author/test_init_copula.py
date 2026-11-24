# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes one copula per table from a profile's rank correlations (ADR 0035 decision 3).

Pinned here, through the command and a run: the number and timestamp columns that init draws from
histograms and that nothing else decides join one ``fidelity.copula`` whose pairs are the profile's
``kendallTau/1`` (``spearmanRho/1`` when no Kendall value is published), a noisy value as its clamped
release; a column another declaration draws, a column a rule sets and a column whose histogram ties
too often stay out, each with its reason in the ledger; a pair the profile withheld is held at 0 and
said so; under kernel 0.1.0 no key is written; and the bundle plans, applies and verifies with every
``rankCorrelation`` row within its tolerance.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.author.copula import TIE_SHARE, tie_share
from flux_engine.cli._init import run_init
from flux_engine.stages.apply import apply
from flux_engine.stages.verify import verify
from flux_stats.rank_correlation import kendall_tau, spearman_rho
from tests.author import rank_profile as rp

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
ROWS = 20000


@pytest.fixture(scope="module")
def data() -> List[rp.Row]:
    return rp.rows(6000)


def _init(
    tmp_path: Path,
    profile: Dict[str, Any],
    contract: str = rp.CONTRACT,
    name: str = "bundle",
) -> Any:
    (tmp_path / "ledger.fluid.yaml").write_text(contract, encoding="utf-8")
    (tmp_path / "ledger.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return run_init(
        str(tmp_path / name),
        contract=str(tmp_path / "ledger.fluid.yaml"),
        profile=str(tmp_path / "ledger.profile.json"),
    )


def _block(tmp_path: Path, name: str = "bundle") -> Dict[str, Any]:
    text = (tmp_path / name / "simulation.flux.yml").read_text(encoding="utf-8")
    block: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block


def _fidelity(tmp_path: Path) -> Dict[str, Any]:
    fidelity: Dict[str, Any] = _block(tmp_path)["emits"]["account"]["fidelity"]
    return fidelity


def _ranked(fidelity: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [each for each in fidelity["properties"] if each["property"] == "rankCorrelation"]


def _ledger(result: Any, ending: str) -> Dict[str, Any]:
    (entry,) = [
        each
        for each in result.artifacts["ledger"]["statistics"]
        if each["pointer"].endswith(ending)
    ]
    return entry  # type: ignore[no-any-return]


def _pair(fidelity: Dict[str, Any], first: str, second: str) -> Optional[str]:
    for each in fidelity["copula"]["rankCorrelations"]["pairs"]:
        if set(each["columns"]) == {first, second}:
            return str(each["value"])
    return None


# ---------------------------------------------------------------------------
# What it writes
# ---------------------------------------------------------------------------


def test_the_published_kendall_values_become_one_copula_in_the_contracts_column_order(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    result = _init(tmp_path, rp.profile(data, rows=ROWS))
    assert result.exit_code == 0, result.stderr
    copula = _fidelity(tmp_path)["copula"]
    # The key, the category and the order of the draws: the contract's own.
    assert copula["columns"] == ["amount", "balance", "opened_at"]
    assert copula["rankCorrelations"]["measure"] == "kendall"
    for first, second in (("amount", "balance"), ("amount", "opened_at"), ("balance", "opened_at")):
        statistic = rp.exact_pair(data, first, second)["groups"][0]
        expected = kendall_tau(int(statistic["S"]), statistic["k"])
        written = Fraction(_pair(_fidelity(tmp_path), first, second) or "x")
        assert abs(written - expected) <= Fraction(1, 10**12)


def test_the_values_are_rounded_half_to_even_at_twelve_places_and_a_zero_is_written_zero(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    pairs = {
        ("amount", "balance"): [rp.noisy_pair("0.1234567890125")],
        ("amount", "opened_at"): [rp.noisy_pair("0.1234567890135")],
        ("balance", "opened_at"): [rp.noisy_pair("0")],
    }
    result = _init(tmp_path, rp.profile(data, pairs, rows=ROWS))
    assert result.exit_code == 0, result.stderr
    fidelity = _fidelity(tmp_path)
    assert _pair(fidelity, "amount", "balance") == "0.123456789012"
    assert _pair(fidelity, "amount", "opened_at") == "0.123456789014"
    assert _pair(fidelity, "balance", "opened_at") == "0"


def test_a_noisy_pair_is_written_as_its_value_clamped_to_one(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    pairs = {
        ("amount", "balance"): [rp.noisy_pair("1.7")],
        ("amount", "opened_at"): [rp.noisy_pair("-3")],
        ("balance", "opened_at"): [rp.noisy_pair("0.25")],
    }
    result = _init(tmp_path, rp.profile(data, pairs, rows=ROWS))
    assert result.exit_code == 0, result.stderr
    fidelity = _fidelity(tmp_path)
    assert _pair(fidelity, "amount", "balance") == "1"
    assert _pair(fidelity, "amount", "opened_at") == "-1"
    assert _pair(fidelity, "balance", "opened_at") == "0.25"
    assert "clamped" in _ledger(result, "columnPairs/0/statistics/0")["note"]


def test_each_pair_is_scored_by_a_rank_correlation_property_with_a_stated_tolerance(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    result = _init(tmp_path, rp.profile(data, rows=ROWS))
    assert result.exit_code == 0, result.stderr
    ranked = _ranked(_fidelity(tmp_path))
    assert [(each["columns"], each["measure"]) for each in ranked] == [
        (["amount", "balance"], "kendall"),
        (["amount", "opened_at"], "kendall"),
        (["balance", "opened_at"], "kendall"),
    ]
    assert all(each["tolerance"] == {"type": "absolute", "max": "0.05"} for each in ranked)
    # The tolerance is a counted number, so the register gives it a reason.
    parameters = _block(tmp_path)["parameters"]
    cited = [
        entry for entry in parameters if entry["pointer"].endswith("/properties/4/tolerance/max")
    ]
    assert [entry["source"] for entry in cited] == ["assumption"]


def test_a_profile_with_no_rank_correlation_gets_no_copula(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    document = rp.profile(data, {}, rows=ROWS)
    document["tables"]["account"]["columnPairs"] = [
        {
            "columns": ["amount", "balance"],
            "statistics": [
                {
                    "definition": "correlation/1",
                    "sampleSize": {"min": 1},
                    "disclosure": "none",
                    "groups": [
                        {
                            "group": "",
                            "k": 1000,
                            "Sx": "1",
                            "Sy": "1",
                            "Sxx": "2",
                            "Sxy": "1",
                            "Syy": "2",
                            "status": "F",
                        }
                    ],
                }
            ],
        }
    ]
    result = _init(tmp_path, document)
    assert result.exit_code == 0, result.stderr
    fidelity = _fidelity(tmp_path)
    assert "copula" not in fidelity and _ranked(fidelity) == []
    note = _ledger(result, "columnPairs/0/statistics/0")["note"]
    assert "rank correlations" in note and "Pearson" in note


# ---------------------------------------------------------------------------
# The measure
# ---------------------------------------------------------------------------


def test_kendall_is_preferred_when_the_profile_publishes_both_and_spearman_says_why_it_is_unused(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    pairs = {
        (a, b): [rp.exact_pair(data, a, b, "spearmanRho/1"), rp.exact_pair(data, a, b)]
        for a, b in (("amount", "balance"), ("amount", "opened_at"), ("balance", "opened_at"))
    }
    result = _init(tmp_path, rp.profile(data, pairs, rows=ROWS))
    assert result.exit_code == 0, result.stderr
    assert _fidelity(tmp_path)["copula"]["rankCorrelations"]["measure"] == "kendall"
    kendall = _ledger(result, "columnPairs/0/statistics/1")
    spearman = _ledger(result, "columnPairs/0/statistics/0")
    assert kendall["state"] == "used" and "Kendall" in kendall["note"]
    assert spearman["state"] == "unused" and "one measure" in spearman["note"]


def test_spearman_is_the_measure_when_it_is_the_only_one_published(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    pairs = {
        (a, b): [rp.exact_pair(data, a, b, "spearmanRho/1")]
        for a, b in (("amount", "balance"), ("amount", "opened_at"), ("balance", "opened_at"))
    }
    result = _init(tmp_path, rp.profile(data, pairs, rows=ROWS))
    assert result.exit_code == 0, result.stderr
    fidelity = _fidelity(tmp_path)
    assert fidelity["copula"]["rankCorrelations"]["measure"] == "spearman"
    statistic = rp.exact_pair(data, "amount", "balance", "spearmanRho/1")["groups"][0]
    expected = spearman_rho(int(statistic["A4"]), statistic["k"])
    assert abs(Fraction(_pair(fidelity, "amount", "balance") or "x") - expected) <= Fraction(
        1, 10**12
    )
    assert {each["measure"] for each in _ranked(fidelity)} == {"spearman"}


# ---------------------------------------------------------------------------
# Who is in, and who is out
# ---------------------------------------------------------------------------


def test_a_column_drawn_given_another_stays_out_and_its_pairs_say_why(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    document = rp.profile(data, rows=ROWS)
    rp.group_by_region(document, data, "balance")
    result = _init(tmp_path, document)
    assert result.exit_code == 0, result.stderr
    fidelity = _fidelity(tmp_path)
    assert "given" in fidelity["columns"]["balance"]
    assert fidelity["copula"]["columns"] == ["amount", "opened_at"]
    note = _ledger(result, "columnPairs/0/statistics/0")
    assert note["state"] == "unused" and "balance" in note["note"] and "one joint" in note["note"]
    assert any("balance is not in the copula" in line for line in result.stdout.splitlines())


def test_a_column_a_rule_sets_stays_out(tmp_path: Path, data: List[rp.Row]) -> None:
    contract = rp.CONTRACT.replace(
        "    semantics:\n",
        "      invariants:\n"
        "        - { id: balance_is_triple, derive: balance, expr: 'ROUND(amount * 3, 0)' }\n"
        "    semantics:\n",
    )
    result = _init(tmp_path, rp.profile(data, rows=ROWS), contract)
    assert result.exit_code == 0, result.stderr
    fidelity = _fidelity(tmp_path)
    assert fidelity["copula"]["columns"] == ["amount", "opened_at"]
    assert "set by a rule of the contract" in _ledger(result, "columnPairs/0/statistics/0")["note"]


def test_a_tie_heavy_column_is_left_to_the_cliques_and_the_ledger_says_so(
    tmp_path: Path,
) -> None:
    rows = rp.rows(6000)
    rows = [(a, index % 4, c, d) for index, (a, _, c, d) in enumerate(rows)]
    result = _init(tmp_path, rp.profile(rows, rows=ROWS))
    assert result.exit_code == 0, result.stderr
    fidelity = _fidelity(tmp_path)
    assert fidelity["copula"]["columns"] == ["amount", "opened_at"]
    note = _ledger(result, "columnPairs/0/statistics/0")["note"]
    assert "balance is left out" in note and "a clique draws it" in note
    assert "balance is not in the copula" in "\n".join(
        line for line in result.stdout.splitlines() if "copula" in line
    )


def test_the_tie_rule_is_the_chance_two_draws_are_equal_and_is_pinned() -> None:
    from flux_engine.author.columns import ColumnPlan
    from flux_engine.seam.contract import compile_expose

    expose = yaml.safe_load(rp.CONTRACT)["exposes"][0]
    (column,) = [each for each in compile_expose(expose).columns if each.name == "balance"]
    plan = ColumnPlan(
        name="balance", declaration={"bins": ["0", "10", "20"], "binWeights": ["0.5", "0.5"]}
    )
    # Ten grid values in the first bin, eleven in the last, which is closed above: half the mass each.
    assert tie_share(column, plan) == Fraction(1, 4) / 10 + Fraction(1, 4) / 11
    assert TIE_SHARE == Fraction(1, 20)
    # A bin that holds weight and no grid value is a draw the copula cannot make.
    plan.declaration["bins"] = ["0", "0.5", "10"]
    plan.declaration["binWeights"] = ["0.5", "0.5"]
    with pytest.raises(ValueError, match="no grid value"):
        tie_share(column, plan)


def test_a_pair_the_profile_withheld_is_held_at_zero_and_stated_as_an_assumption(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    pairs = {
        ("amount", "balance"): [rp.exact_pair(data, "amount", "balance")],
        ("amount", "opened_at"): [rp.exact_pair(data, "amount", "opened_at")],
    }
    result = _init(tmp_path, rp.profile(data, pairs, rows=ROWS))
    assert result.exit_code == 0, result.stderr
    fidelity = _fidelity(tmp_path)
    assert _pair(fidelity, "balance", "opened_at") is None
    assert [each["columns"] for each in _ranked(fidelity)] == [
        ["amount", "balance"],
        ["amount", "opened_at"],
    ]
    sentences = [
        item["assumption"]
        for entry in result.artifacts["exposes"]
        for item in entry.get("assumptions", [])
        if item["subject"] == "copula"
    ]
    assert len(sentences) == 1 and "balance, opened_at" in sentences[0] and "at 0" in sentences[0]


def test_a_column_with_no_published_pair_to_another_stays_out(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    withheld = rp.exact_pair(data, "amount", "opened_at")
    withheld["groups"] = [{"group": "", "status": "A"}]
    pairs = {
        ("amount", "balance"): [rp.exact_pair(data, "amount", "balance")],
        ("amount", "opened_at"): [withheld],
    }
    result = _init(tmp_path, rp.profile(data, pairs, rows=ROWS))
    assert result.exit_code == 0, result.stderr
    fidelity = _fidelity(tmp_path)
    assert fidelity["copula"]["columns"] == ["amount", "balance"]
    notes = result.artifacts["exposes"][0]["copula"]["notes"]
    assert any("opened_at is not in the copula" in each for each in notes)


def test_one_pair_alone_is_still_a_copula_and_none_is_no_copula(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    pairs = {("amount", "balance"): [rp.noisy_pair("0.4")]}
    first = _init(tmp_path, rp.profile(data, pairs, rows=ROWS), name="one")
    assert first.exit_code == 0, first.stderr
    assert _block(tmp_path, "one")["emits"]["account"]["fidelity"]["copula"]["columns"] == [
        "amount",
        "balance",
    ]
    withheld = rp.profile(data, {("amount", "balance"): [rp.exact_pair(data, "amount", "balance")]})
    withheld["tables"]["account"]["columnPairs"][0]["statistics"][0]["groups"] = [
        {"group": "", "status": "A"}
    ]
    second = _init(tmp_path, withheld, name="none")
    assert second.exit_code == 0, second.stderr
    assert "copula" not in _block(tmp_path, "none")["emits"]["account"]["fidelity"]
    assert _ledger(second, "columnPairs/0/statistics/0")["state"] == "withheld"


# ---------------------------------------------------------------------------
# Kernel
# ---------------------------------------------------------------------------


def test_under_kernel_0_1_0_init_writes_no_copula_and_says_the_kernel_has_no_path(
    tmp_path: Path, data: List[rp.Row], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("flux_engine.core.kernels.new_plan_is_counter_based", lambda: False)
    result = _init(tmp_path, rp.profile(data, rows=ROWS))
    assert result.exit_code == 0, result.stderr
    fidelity = _fidelity(tmp_path)
    assert "copula" not in fidelity and _ranked(fidelity) == []
    assert "kernel 0.1.0" in _ledger(result, "columnPairs/0/statistics/0")["note"]


# ---------------------------------------------------------------------------
# The whole way: init, plan, apply, verify
# ---------------------------------------------------------------------------


def test_the_bundle_init_writes_plans_applies_and_verifies_within_every_rank_tolerance(
    tmp_path: Path, data: List[rp.Row]
) -> None:
    profile = tmp_path / "ledger.profile.json"
    result = _init(tmp_path, rp.profile(data, rows=ROWS))
    assert result.exit_code == 0, result.stderr
    applied = apply(tmp_path / "bundle", state_dir=tmp_path / "state", profile=profile)
    assert applied.success, applied.stderr
    verified = verify(
        tmp_path / "bundle",
        run_id=str(applied.artifacts["run_id"]),
        state_dir=tmp_path / "state",
        profile=profile,
    )
    assert verified.success, verified.stderr
    document = verified.artifacts["scorecard"]["document"]
    assert document["_schemaURL"].endswith("/scorecard/1-5-0/FluxScorecard.json")
    (table,) = document["tables"]
    rows = {row["propertyId"]: row for row in table["properties"]}
    ranks = [row for key, row in rows.items() if ":rankCorrelation:" in key]
    assert len(ranks) == 3
    assert all(row["outcome"] == "within" for row in ranks)
    assert all(row["outcome"] == "within" for row in rows.values())


# ---------------------------------------------------------------------------
# What else decides a column, read without a bundle
# ---------------------------------------------------------------------------


def _stub(clique_drawn: Optional[set] = None) -> Any:
    from types import SimpleNamespace

    from flux_engine.author.columns import ColumnPlan
    from flux_engine.seam.contract import LogicalKind

    histogram = {"bins": ["0", "1", "2", "3"], "binWeights": ["0.3", "0.3", "0.4"]}
    names = ("amount", "balance", "opened_at")
    return SimpleNamespace(
        expose_id="account",
        columns=[ColumnPlan(name=name, declaration=dict(histogram)) for name in names],
        kinds={
            "amount": LogicalKind.DECIMAL,
            "balance": LogicalKind.INT,
            "opened_at": LogicalKind.TIMESTAMP,
        },
        contexts={},
        derives={},
        dependents=set(),
        copies={},
        aggregated=set(),
        guarded=set(),
        drawn_by_cliques=clique_drawn or set(),
    )


def _table(data: List[rp.Row]) -> Any:
    from flux_stats.profile import parse_profile

    return parse_profile(rp.profile(data, rows=ROWS)).tables["account"]


def test_a_column_a_lifetimes_within_or_rates_entry_names_stays_out(data: List[rp.Row]) -> None:
    from flux_engine.author.copula import declare_copula

    planned = declare_copula(
        _stub(), _table(data), by_dates={"opened_at"}, whole=None, counter_based=True
    )
    assert planned is not None and planned.columns == ("amount", "balance")
    assert any("opened_at is not in the copula" in each for each in planned.notes)
    assert any("lifetimes, within or rates" in why for _, why in planned.passed)


def test_a_column_a_clique_draws_stays_out_whatever_its_declaration_says(
    data: List[rp.Row],
) -> None:
    from flux_engine.author.copula import declare_copula

    planned = declare_copula(
        _stub({"balance"}), _table(data), by_dates=set(), whole=None, counter_based=True
    )
    assert planned is not None and planned.columns == ("amount", "opened_at")
    assert any("a clique draws it" in why for _, why in planned.passed)


def test_a_table_that_follows_a_history_gets_no_copula_and_says_why(data: List[rp.Row]) -> None:
    from flux_engine.author.copula import declare_copula

    planned = declare_copula(
        _stub(),
        _table(data),
        by_dates=set(),
        whole="its columns follow a history",
        counter_based=True,
    )
    assert planned is not None and planned.declaration is None and planned.properties == []
    assert planned.notes == ["no copula is written: its columns follow a history"]
    assert len(planned.passed) == 3


def test_a_table_with_no_rank_correlation_is_not_read_at_all() -> None:
    from flux_engine.author.copula import declare_copula, has_rank_statistics

    assert declare_copula(_stub(), None, by_dates=set(), whole=None, counter_based=True) is None
    assert has_rank_statistics(None) is False
