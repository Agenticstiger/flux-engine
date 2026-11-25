# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Profile format 1-2-0 released under differential privacy: accounting, noise and cliques.

``docs/spec/profile-1-2-private-example.json`` holds a ``privacy`` block, a noisy statistic and
two noisy cliques that agree on the column they share. It must read, validate against the schema,
and each broken copy must be refused with ``FLX-5036`` naming where.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jsonschema import Draft202012Validator, FormatChecker

from flux_stats.errors import FluxError
from flux_stats.noise import Noise, PrivacyLoss, point_estimate
from flux_stats.profile import SCHEMA_URL_1_1_0, parse_profile

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _spec()
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-1-2-private-example.json").read_text("utf-8"))
SCHEMA: Dict[str, Any] = json.loads((SPEC / "profile-1-2.schema.json").read_text("utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


def _edit(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def _table(document: Dict[str, Any]) -> Dict[str, Any]:
    table: Dict[str, Any] = document["tables"]["subscriber"]
    return table


def test_the_example_reads_and_validates() -> None:
    assert not list(VALIDATOR.iter_errors(EXAMPLE))
    profile = parse_profile(copy.deepcopy(EXAMPLE))
    assert profile.accounting is not None
    assert profile.accounting.measure == "zeroConcentratedDP"
    assert profile.accounting.total.rho == Fraction(1, 2)
    assert profile.accounting.unit == "subscriber"
    table = profile.tables["subscriber"]
    assert [clique.columns for clique in table.cliques] == [
        ("region", "plan"),
        ("plan", "channel"),
    ]


def test_a_noisy_statistic_is_read_as_its_point_estimate_with_its_variance() -> None:
    profile = parse_profile(copy.deepcopy(EXAMPLE))
    (statistic,) = [
        s for s in profile.tables["subscriber"].columns["region"].statistics if not s.derived
    ]
    assert statistic.noise is not None and statistic.noise.mechanism == "gaussian"
    assert statistic.cell_variance == Fraction("2.24") ** 2
    (group,) = statistic.groups
    assert group.noisy == (Fraction("5999.5"), Fraction(4501), Fraction("3500.5"))
    assert group.statistic is not None
    assert group.statistic.k == 14001
    assert group.statistic.level_counts == (6000, 4501, 3500)


def test_a_clique_answers_the_one_and_two_way_level_histograms_of_its_columns() -> None:
    profile = parse_profile(copy.deepcopy(EXAMPLE))
    columns = profile.tables["subscriber"].columns
    plan = {(s.group_by, s.derived): s for s in columns["plan"].statistics}
    assert set(plan) == {(None, True), ("region", True), ("channel", True)}
    marginal = plan[(None, True)]
    assert marginal.groups[0].noisy == (Fraction("9503.5"), Fraction("4496.5"))
    # Two cliques sum onto "plan"; each cell of the marginal adds three or two clique cells.
    assert marginal.cell_variance == Fraction("2.24") ** 2 * 3
    grouped = plan[("region", True)]
    assert [group.key for group in grouped.groups] == ["east", "north", "west"]
    assert grouped.cell_variance == Fraction("2.24") ** 2
    # The column's own noisy histogram is kept, and no derived copy of it is added.
    region = [s for s in columns["region"].statistics if s.group_by is None]
    assert len(region) == 1 and not region[0].derived


def test_the_accounting_of_a_loss_that_is_not_above_zero_is_refused() -> None:
    document = _edit(lambda d: d["privacy"]["privacyLoss"].update(rho="0"))
    assert "above zero" in _refused(document)


def test_a_share_below_what_the_mechanism_spends_is_refused() -> None:
    # Gaussian of scale 2.24 spends 1 / (2 * 2.24^2) = 0.0996..., so 0.05 is understated.
    document = _edit(lambda d: _table(d)["cliques"][0]["noise"]["privacyLoss"].update(rho="0.05"))
    assert "below 1 / (2 scale^2)" in _refused(document)
    parse_profile(
        _edit(lambda d: _table(d)["cliques"][0]["noise"]["privacyLoss"].update(rho="0.0997"))
    )

    def laplace(document: Dict[str, Any]) -> None:
        document["privacy"] = {
            "measure": "approximateDP",
            "privacyLoss": {"epsilon": "10", "delta": "0.001"},
            "privacyUnit": {"expose": "subscriber"},
        }
        for clique in _table(document)["cliques"]:
            clique["noise"] = {
                "mechanism": "laplace",
                "scale": "4",
                "privacyLoss": {"epsilon": "0.25", "delta": "0"},
            }
        _table(document)["columns"]["region"]["statistics"][0]["noise"] = copy.deepcopy(
            _table(document)["cliques"][0]["noise"]
        )

    parse_profile(_edit(laplace))

    # a Laplace under zero-concentrated DP spends rho = 1 / (2 b^2): scale 2 is exactly 0.125
    def under_rho(rho: str) -> Dict[str, Any]:
        return _edit(
            lambda d: [
                c["noise"].update(mechanism="laplace", scale="2", privacyLoss={"rho": rho})
                for c in _table(d)["cliques"]
            ]
        )

    parse_profile(under_rho("0.125"))
    assert "below (1 / scale)^2 / 2" in _refused(under_rho("0.1249"))
    low = _edit(laplace)
    _table(low)["cliques"][0]["noise"]["privacyLoss"]["epsilon"] = "0.2"
    assert "below 1 / scale" in _refused(low)


def test_the_declaration_that_the_structure_is_public_is_read_and_must_be_a_boolean() -> None:
    assert parse_profile(copy.deepcopy(EXAMPLE)).accounting.public_structure is True  # type: ignore[union-attr]
    without = _edit(lambda d: d["privacy"].pop("publicStructure"))
    assert parse_profile(without).accounting.public_structure is False  # type: ignore[union-attr]
    assert "true or false" in _refused(_edit(lambda d: d["privacy"].update(publicStructure="yes")))


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_a_present_declaration_that_is_not_a_boolean_is_refused_as_the_schema_refuses_it(
    value: Any,
) -> None:
    """``null`` read as undeclared, and the schema (a boolean) refused the same document."""
    message = _refused(_edit(lambda d: d["privacy"].update(publicStructure=value)))
    assert "publicStructure" in message and "true or false" in message


def test_a_clique_answer_a_statistic_of_the_document_also_answers_is_kept_apart() -> None:
    column = parse_profile(copy.deepcopy(EXAMPLE)).tables["subscriber"].columns["region"]
    # region has its own noisy histogram, so the clique's is not added to its statistics
    assert [s for s in column.statistics if s.derived and s.group_by is None] == []
    (own,) = [s for s in column.clique_statistics if s.group_by is None]
    assert own.derived and own.cell_variance == Fraction("2.24") ** 2 * 2


def test_parts_that_add_up_to_more_than_the_total_are_refused() -> None:
    def spend(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyLoss"]["rho"] = "0.29"  # the parts are 0.1 + 0.1 + 0.1

    assert "more than the total rho" in _refused(_edit(spend))
    ok = _edit(lambda d: d["privacy"]["privacyLoss"].update(rho="0.3"))
    parse_profile(ok)  # exactly the sum is allowed


def _approximate(document: Dict[str, Any], *, epsilon: str, delta: str) -> None:
    document["privacy"] = {
        "measure": "approximateDP",
        "privacyLoss": {"epsilon": epsilon, "delta": delta},
        "privacyUnit": {"expose": "subscriber"},
    }

    def part(block: Dict[str, Any]) -> None:
        # a Laplace of scale 2 spends epsilon 1 / 2, so these parts state what they spend
        block["noise"] = {
            "mechanism": "laplace",
            "scale": "2",
            "privacyLoss": {"epsilon": "0.5", "delta": "0.000001"},
        }

    _table(document)["cliques"][0].update(
        noise=copy.deepcopy(_table(document)["cliques"][0]["noise"])
    )
    for clique in _table(document)["cliques"]:
        part(clique)
    part(_table(document)["columns"]["region"]["statistics"][0])


def test_under_approximate_dp_epsilon_and_delta_add_each_on_its_own() -> None:
    ok = _edit(lambda d: _approximate(d, epsilon="1.5", delta="0.000003"))
    assert parse_profile(ok).accounting is not None
    assert "more than the total epsilon" in _refused(
        _edit(lambda d: _approximate(d, epsilon="1.4", delta="0.000003"))
    )
    assert "more than the total delta" in _refused(
        _edit(lambda d: _approximate(d, epsilon="1.5", delta="0.0000029"))
    )


def test_a_part_in_the_other_measure_is_refused() -> None:
    def other(document: Dict[str, Any]) -> None:
        _table(document)["cliques"][1]["noise"]["privacyLoss"] = {"epsilon": "1", "delta": "0"}

    assert "unknown key" in _refused(_edit(other)) or "missing key" in _refused(_edit(other))


def test_noise_without_a_privacy_block_is_refused() -> None:
    document = _edit(lambda d: d.pop("privacy"))
    assert "privacy block" in _refused(document)


def test_a_privacy_block_that_nothing_spends_is_accepted() -> None:
    # A stated loss is an upper bound, and overstating it is the conservative side, so a block
    # that no statistic or clique spends is read, with no part (ADR 0019, amendment of 2026-11-25).
    def strip(document: Dict[str, Any]) -> None:
        del _table(document)["cliques"]
        del _table(document)["columns"]["region"]["statistics"]

    profile = parse_profile(_edit(strip))
    assert profile.accounting is not None and profile.parts == ()


def test_a_total_above_what_the_parts_spend_is_accepted() -> None:
    parse_profile(_edit(lambda d: d["privacy"]["privacyLoss"].update(rho="5")))


def test_a_total_below_what_the_parts_spend_is_still_refused() -> None:
    assert "more than the total rho" in _refused(
        _edit(lambda d: d["privacy"]["privacyLoss"].update(rho="0.29"))
    )


def test_a_noisy_statistic_in_another_table_than_the_unit_is_refused() -> None:
    document = _edit(lambda d: d["privacy"]["privacyUnit"].update(expose="payment"))
    assert "unit of privacy" in _refused(document)


@pytest.mark.parametrize(
    "key,value",
    [("mechanism", "uniform"), ("scale", "0"), ("scale", "-1")],
)
def test_a_noise_block_is_checked(key: str, value: str) -> None:
    document = _edit(lambda d: _table(d)["cliques"][0]["noise"].update({key: value}))
    _refused(document)


def test_noise_belongs_to_count_statistics() -> None:
    def mean(document: Dict[str, Any]) -> None:
        statistic = _table(document)["columns"]["region"]["statistics"][0]
        statistic["definition"] = "support/1"

    _refused(_edit(mean))


def test_cliques_that_disagree_are_refused_naming_both() -> None:
    def disagree(document: Dict[str, Any]) -> None:
        _table(document)["cliques"][1]["noisyCounts"][0] = "6100.5"

    message = _refused(_edit(disagree))
    assert "cliques[0] (columns region, plan)" in message
    assert "cliques[1] (columns plan, channel)" in message
    assert "disagree on the marginal of plan" in message


def test_cliques_that_list_other_levels_of_a_shared_column_are_refused() -> None:
    def levels(document: Dict[str, Any]) -> None:
        clique = _table(document)["cliques"][1]
        clique["levels"][0] = ["basic", "premium"]

    assert "different levels of column 'plan'" in _refused(_edit(levels))


def test_two_cliques_over_the_same_columns_are_refused() -> None:
    def twice(document: Dict[str, Any]) -> None:
        _table(document)["cliques"].append(copy.deepcopy(_table(document)["cliques"][0]))

    assert "over the same columns" in _refused(_edit(twice))


def test_disjoint_cliques_need_not_agree() -> None:
    def disjoint(document: Dict[str, Any]) -> None:
        _table(document)["columns"]["tier"] = {"domain": "category"}
        _table(document)["columns"]["zone"] = {"domain": "category"}
        _table(document)["cliques"].append(
            {
                "definition": "cliqueMarginal/1",
                "columns": ["tier", "zone"],
                "levels": [["a", "b"], ["x", "y"]],
                "noisyCounts": ["1", "2", "3", "4"],
                "noise": {
                    "mechanism": "laplace",
                    "scale": "3",
                    "privacyLoss": {"rho": "0.1"},
                },
            }
        )

    parse_profile(_edit(disjoint))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(columns=["region", "region"]),
        lambda c: c.update(columns=["region"]),
        lambda c: c.update(columns=["region", "missing"]),
        lambda c: c.update(noisyCounts=c["noisyCounts"][:-1]),
        lambda c: c.update(levels=[["north", "east", "west"], c["levels"][1]]),
        lambda c: c.update(definition="cliqueMarginal/2"),
        lambda c: c.update(extra=1),
    ],
)
def test_a_malformed_clique_is_refused(mutate: Callable[[Dict[str, Any]], None]) -> None:
    document = _edit(lambda d: mutate(_table(d)["cliques"][0]))
    _refused(document)


def test_an_earlier_format_holds_none_of_it() -> None:
    document = _edit(lambda d: d.update(_schemaURL=SCHEMA_URL_1_1_0))
    assert "section of format 1-2-0" in _refused(document)


def test_noise_variance_by_mechanism() -> None:
    loss = PrivacyLoss(rho=Fraction(1))
    assert Noise("gaussian", Fraction(3), loss).variance == 9
    assert Noise("discreteGaussian", Fraction(3), loss).variance == 9
    assert Noise("laplace", Fraction(3), loss).variance == 18


def test_a_non_negative_vector_is_only_normalised() -> None:
    estimate = point_estimate([Fraction(3), Fraction(1), Fraction(0)])
    assert estimate.weights == (3, 1, 0)
    assert estimate.shares == (Fraction(3, 4), Fraction(1, 4), 0)
    assert estimate.records == 4 and estimate.counts == (3, 1, 0)


def test_a_negative_cell_takes_the_mass_it_lacks_from_the_others_evenly() -> None:
    estimate = point_estimate([Fraction(6), Fraction(4), Fraction(-2)])
    # T = 8; t = (6 + 4 - 8) / 2 = 1: weights 5, 3, 0.
    assert estimate.weights == (5, 3, 0)
    assert estimate.records == 8 and estimate.counts == (5, 3, 0)


def test_a_vector_that_sums_to_nothing_has_equal_shares_and_no_records() -> None:
    estimate = point_estimate([Fraction(-3), Fraction(1), Fraction(1)])
    assert estimate.records == 0 and estimate.counts == (0, 0, 0)
    assert estimate.shares == (Fraction(1, 3),) * 3


def test_whole_counts_follow_the_largest_remainder_lowest_first() -> None:
    estimate = point_estimate([Fraction(1), Fraction(1), Fraction(1)])
    assert estimate.records == 3 and estimate.counts == (1, 1, 1)
    estimate = point_estimate([Fraction("0.5"), Fraction("0.5"), Fraction("0.5"), Fraction("0.5")])
    assert estimate.records == 2 and estimate.counts == (1, 1, 0, 0)


@given(st.lists(st.integers(min_value=-500, max_value=500), min_size=1, max_size=12))
def test_a_point_estimate_is_a_distribution_whatever_the_noise(values: List[int]) -> None:
    estimate = point_estimate([Fraction(value, 4) for value in values])
    assert all(weight >= 0 for weight in estimate.weights)
    assert sum(estimate.shares) == 1
    if estimate.total > 0:
        assert sum(estimate.weights) == estimate.total
    assert sum(estimate.counts) == estimate.records
    # the same values in another order give the same weights, matched cell by cell
    flipped = point_estimate([Fraction(value, 4) for value in reversed(values)])
    assert tuple(reversed(flipped.weights)) == estimate.weights
