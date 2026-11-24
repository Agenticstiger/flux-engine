# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``cliqueMarginal/2`` (profile format 1-4-0): measured cliques over categories and bins.

The reader, its agreement with ``cliqueMarginal/1``, the marginals it forms, every refusal of
PROFILE-SPEC.md section 5a, the limits, and the schema that validates it.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from flux_stats import cliques as cliques_module
from flux_stats.errors import FluxError
from flux_stats.profile import SCHEMA_URL_1_3_0, parse_profile

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _spec()
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-1-4-cliques-example.json").read_text("utf-8"))
NOISE = {"mechanism": "gaussian", "scale": "5", "privacyLoss": {"rho": "0.02"}}


def _document(change: Callable[[Dict[str, Any]], None] = lambda d: None) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def _table(document: Dict[str, Any]) -> Dict[str, Any]:
    table: Dict[str, Any] = document["tables"]["subscriber"]
    return table


def _clique_2(document: Dict[str, Any]) -> Dict[str, Any]:
    clique: Dict[str, Any] = _table(document)["cliques"][1]
    return clique


def _refused(document: Dict[str, Any], *words: str) -> str:
    with pytest.raises(FluxError) as raised:
        parse_profile(document)
    message = str(raised.value)
    assert "FLX-5036" in message
    for word in words:
        assert word in message, message
    return message


def _small(columns: List[str], domains: List[Dict[str, Any]], counts: List[str]) -> Dict[str, Any]:
    return {
        "definition": "cliqueMarginal/2",
        "columns": columns,
        "domains": domains,
        "noisyCounts": counts,
        "noise": copy.deepcopy(NOISE),
    }


def _with_cliques(
    *cliques: Dict[str, Any], columns: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    document = _document()
    table = _table(document)
    table["cliques"] = list(cliques)
    table.pop("cliqueSelection", None)
    document["privacy"]["privacyLoss"] = {"rho": "5"}
    if columns is not None:
        table["columns"].update(columns)
    return document


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def test_the_example_reads_and_each_clique_is_one_part_of_the_budget() -> None:
    profile = parse_profile(EXAMPLE)
    table = profile.tables["subscriber"]
    assert [clique.definition for clique in table.cliques] == ["cliqueMarginal/1"]
    (binned,) = table.cliques2
    assert binned.definition == "cliqueMarginal/2" and binned.index == 1
    assert binned.columns == ("region", "age")
    assert binned.levels == (("east", "north", "west"), ("0", "30", "60"))
    assert binned.edges == ((), tuple(Fraction(edge) for edge in (0, 30, 60, 100)))
    assert binned.noisy[:3] == (Fraction(10), Fraction(20), Fraction(20))
    assert binned.binned("age") and not binned.binned("region")
    where = [place for place, _ in profile.parts]
    assert where[:2] == [f"profile.tables.subscriber.cliques[{i}]" for i in range(2)]


def test_a_one_column_clique_is_the_marginal_of_that_column() -> None:
    document = _with_cliques(_small(["age"], [{"bins": ["0", "10", "20"]}], ["7", "9"]))
    profile = parse_profile(document)
    (clique,) = profile.tables["subscriber"].cliques2
    assert clique.columns == ("age",) and clique.levels == (("0", "10"),)
    (one_way,) = [s for s in profile.tables["subscriber"].columns["age"].statistics if s.derived]
    assert one_way.identifier == "marginalHistogram/1" and one_way.group_by is None
    assert one_way.bins == tuple(Fraction(edge) for edge in (0, 10, 20))
    assert one_way.cell_variance == 25  # one cell summed per bin


def test_a_clique_over_a_timestamp_column_takes_bins_too() -> None:
    document = _with_cliques(
        _small(
            ["joined", "plan"],
            [{"bins": ["0", "86400000", "172800000"]}, {"levels": ["a", "b"]}],
            ["1", "2", "3", "4"],
        ),
        columns={"joined": {"domain": "timestamp"}},
    )
    (clique,) = parse_profile(document).tables["subscriber"].cliques2
    assert clique.levels[0] == ("0", "86400000")


def test_a_bin_is_named_by_the_canonical_text_of_its_lower_edge() -> None:
    document = _with_cliques(
        _small(["age"], [{"bins": ["-0", "0.50", "1.2500", "10"]}], ["1", "2", "3"])
    )
    (clique,) = parse_profile(document).tables["subscriber"].cliques2
    assert clique.levels == (("0", "0.5", "1.25"),)


def test_a_clique_of_thirty_two_columns_is_read_and_one_of_thirty_three_is_not() -> None:
    names = [f"c{n}" for n in range(33)]
    columns = {name: {"domain": "category"} for name in names}
    one_level = [{"levels": ["x"]}]
    ok = _with_cliques(_small(names[:32], one_level * 32, ["4"]), columns=columns)
    assert len(parse_profile(ok).tables["subscriber"].cliques2[0].columns) == 32
    too_many = _with_cliques(_small(names, one_level * 33, ["4"]), columns=columns)
    _refused(too_many, "cliques[0].columns", "1 to 32")


def test_the_derived_marginals_carry_the_variance_of_the_cells_they_sum() -> None:
    profile = parse_profile(EXAMPLE)
    age = profile.tables["subscriber"].columns["age"].statistics
    one_way, by_region = [s for s in age if s.derived]
    assert one_way.group_by is None and by_region.group_by == "region"
    assert one_way.noise is not None and one_way.noise.variance == 25
    assert one_way.cell_variance == 25 * 3 and by_region.cell_variance == 25
    (only,) = one_way.groups
    assert only.key == "" and only.noisy == (Fraction(40), Fraction(40), Fraction(70))
    assert [g.key for g in by_region.groups] == ["east", "north", "west"]
    assert by_region.groups[2].noisy == (Fraction(5), Fraction(5), Fraction(40))
    assert by_region.groups[0].statistic is not None
    assert by_region.groups[0].statistic.bin_counts == (10, 20, 20)
    region = profile.tables["subscriber"].columns["region"].statistics
    assert [(s.identifier, s.group_by) for s in region if s.derived] == [
        ("levelHistogram/1", None),
        ("levelHistogram/1", "plan"),
    ]


def test_two_binned_columns_give_the_first_grouped_by_the_bins_of_the_second() -> None:
    columns = {"income": {"domain": "number"}}
    document = _with_cliques(
        _small(
            ["age", "income"],
            [{"bins": ["0", "30", "60"]}, {"bins": ["0", "1000", "2000"]}],
            ["1", "2", "3", "4"],
        ),
        columns=columns,
    )
    table = parse_profile(document).tables["subscriber"]
    (grouped,) = [s for s in table.columns["age"].statistics if s.group_by_bins is not None]
    assert grouped.group_by is None and grouped.derived
    assert grouped.group_by_bins is not None and grouped.group_by_bins.column == "income"
    assert [g.key for g in grouped.groups] == ["0", "1000"]
    assert grouped.groups[0].noisy == (Fraction(1), Fraction(3))  # age bins at income 0
    assert grouped.cell_variance == 25 * 1
    # the later column of the pair forms no statistic grouped by the earlier one
    assert not [s for s in table.columns["income"].statistics if s.group_by_bins is not None]


def test_a_statistic_the_column_already_carries_is_kept_apart_not_added() -> None:
    def add(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyLoss"] = {"rho": "5"}
        _table(document)["columns"]["age"]["statistics"] = [
            {
                "definition": "marginalHistogram/1",
                "bins": ["0", "30", "60", "100"],
                "sampleSize": {"min": 1000},
                "disclosure": "noisy",
                "noise": copy.deepcopy(NOISE),
                "groups": [{"group": "", "status": "F", "noisyCounts": ["50", "40", "60"]}],
            }
        ]

    column = parse_profile(_document(add)).tables["subscriber"].columns["age"]
    assert [s.derived for s in column.statistics] == [False, True]
    (own,) = [s for s in column.clique_statistics if s.group_by is None]
    assert own.bins and own.groups[0].noisy == (Fraction(40), Fraction(40), Fraction(70))


# ---------------------------------------------------------------------------
# Agreement with cliqueMarginal/1 and with each other
# ---------------------------------------------------------------------------


def test_the_two_definitions_agree_on_the_levels_of_a_shared_column() -> None:
    def change(document: Dict[str, Any]) -> None:
        _clique_2(document)["domains"][0] = {"levels": ["east", "north", "south", "west"]}
        _clique_2(document)["noisyCounts"] = [str(n) for n in range(12)]

    _refused(_document(change), "cliques[0]", "cliques[1]", "different levels", "region")


def test_two_cliques_agree_on_the_edges_of_a_shared_column_and_not_only_its_lower_edges() -> None:
    first = _small(
        ["age", "plan"], [{"bins": ["0", "10", "20"]}, {"levels": ["a", "b"]}], ["1", "2", "3", "4"]
    )
    second = _small(
        ["age", "region"],
        [{"bins": ["0", "10", "30"]}, {"levels": ["e", "w"]}],
        ["1", "3", "2", "4"],
    )
    _refused(_with_cliques(first, second), "different bin edges", "age")


def test_the_noisy_sums_over_a_shared_column_are_equal_exactly() -> None:
    first = _small(
        ["age", "plan"], [{"bins": ["0", "10", "20"]}, {"levels": ["a", "b"]}], ["1", "2", "3", "4"]
    )
    second = _small(
        ["age", "region"],
        [{"bins": ["0", "10", "20"]}, {"levels": ["e", "w"]}],
        ["1", "2", "3", "4"],
    )
    assert parse_profile(_with_cliques(first, second))
    second["noisyCounts"][0] = "1.000000000001"
    _refused(_with_cliques(first, second), "disagree on the marginal", "age")


def test_a_binned_clique_and_a_cliquemarginal_1_agree_on_a_category_they_share() -> None:
    def change(document: Dict[str, Any]) -> None:
        _clique_2(document)["noisyCounts"][0] = "11"

    _refused(_document(change), "disagree on the marginal", "region")


def test_two_cliques_over_one_set_of_columns_are_refused_whatever_their_definition() -> None:
    twin = _small(
        ["region", "plan"],
        [{"levels": ["east", "north", "west"]}, {"levels": ["basic", "pro"]}],
        ["30", "20", "25", "25", "10", "40"],
    )
    document = _document(lambda d: _table(d)["cliques"].append(twin))
    _refused(document, "over the same columns")
    a = _small(["age"], [{"bins": ["0", "10"]}], ["1"])
    _refused(_with_cliques(a, copy.deepcopy(a)), "over the same columns")


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_cliquemarginal_2_in_a_document_that_names_an_earlier_format_is_refused() -> None:
    def change(document: Dict[str, Any]) -> None:
        document["_schemaURL"] = SCHEMA_URL_1_3_0
        _table(document).pop("cliqueSelection", None)
        _table(document)["cliques"] = _table(document)["cliques"][1:]

    _refused(_document(change), "cliqueMarginal/2", "1-4-0", "earlier format")


@pytest.mark.parametrize(
    ("change", "where"),
    [
        (lambda c: c.update(estimate={"variance": "1"}), "estimate"),
        (lambda c: c.update(extra=1), "unknown key"),
        (lambda c: c.pop("noise"), "missing key 'noise'"),
        (lambda c: c.pop("domains"), "missing key 'domains'"),
        (lambda c: c.update(columns=["region", "region"]), "distinct"),
        (lambda c: c.update(columns=[]), "1 to 32"),
        (lambda c: c.update(columns=["region", "nowhere"]), "no column of its table"),
        (lambda c: c.update(domains=[{"levels": ["east"]}]), "one domain for each column"),
        (
            lambda c: c.update(domains=[{"bins": ["0", "1"]}, {"bins": ["0", "30"]}]),
            "lists its levels",
        ),
        (lambda c: c.update(domains=[{"levels": ["a"]}, {"levels": ["b"]}]), "lists its bins"),
        (
            lambda c: c.update(
                domains=[{"levels": ["a"], "bins": ["0", "1"]}, {"bins": ["0", "1"]}]
            ),
            "domain is an object",
        ),
        (
            lambda c: c["domains"].__setitem__(0, {"levels": ["west", "east"]}),
            "distinct and sorted",
        ),
        (lambda c: c["domains"].__setitem__(0, {"levels": []}), "distinct and sorted"),
        (
            lambda c: c["domains"].__setitem__(1, {"bins": ["0"]}),
            "at least two, strictly increasing",
        ),
        (lambda c: c["domains"].__setitem__(1, {"bins": ["0", "30", "30"]}), "strictly increasing"),
        (lambda c: c["domains"].__setitem__(1, {"bins": ["0", "1e3"]}), "plain decimal"),
        (lambda c: c.update(noisyCounts=["1"]), "one noisy count for each of the 9 cells"),
        (lambda c: c["noisyCounts"].__setitem__(0, 1.5), "plain decimal"),
        (lambda c: c["noise"].update(scale="0"), "scale"),
    ],
)
def test_every_refusal_of_a_cliquemarginal_2_names_where_and_quotes_no_value(
    change: Callable[[Dict[str, Any]], None], where: str
) -> None:
    document = _document(lambda d: change(_clique_2(d)))
    message = _refused(document, "cliques[1]", where)
    assert "FLX-5036" in message


def test_an_estimated_clique_is_refused_and_the_message_says_it_is_not_read() -> None:
    document = _document(lambda d: _clique_2(d).update(estimate={"variance": "1"}))
    message = _refused(document, "estimate")
    assert "estimated cliques are not read in this format" in message
    # a clique with an estimate and no noise block is refused for the estimate, not for the noise
    document = _document(
        lambda d: (_clique_2(d).pop("noise"), _clique_2(d).update(estimate={"variance": "1"}))
    )
    assert "estimated cliques are not read" in _refused(document)


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(estimate={"variance": "1"}),
        lambda c: c.update(measurements=[]),
        lambda c: c.update(published="estimated"),
    ],
)
def test_every_spelling_of_an_estimated_clique_gets_the_same_message(
    change: Callable[[Dict[str, Any]], Any],
) -> None:
    message = _refused(_document(lambda d: change(_clique_2(d))))
    assert "estimated cliques are not read in this format" in message


def test_a_clique_needs_the_account_and_the_unit_s_table() -> None:
    def no_account(document: Dict[str, Any]) -> None:
        del document["privacy"]
        _table(document).pop("cliqueSelection", None)

    _refused(_document(no_account), "privacy block")

    def other_unit(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyUnit"] = {"expose": "elsewhere"}
        _table(document).pop("cliqueSelection", None)

    _refused(_document(other_unit), "unit of privacy")


def test_a_clique_s_share_below_what_it_spends_is_refused() -> None:
    def change(document: Dict[str, Any]) -> None:
        _clique_2(document)["noise"]["privacyLoss"] = {"rho": "0.0001"}

    _refused(_document(change), "cliques[1].noise.privacyLoss")


def test_the_cells_of_one_clique_are_limited_before_any_count_is_read() -> None:
    edges = [str(n) for n in range(1002)]  # 1001 bins
    big = _small(["age", "income"], [{"bins": edges}, {"bins": edges}], [])
    document = _with_cliques(big, columns={"income": {"domain": "number"}})
    _refused(document, "cliques[0].domains", "1000000 cells")
    exactly = _small(["age", "income"], [{"bins": edges[:1001]}, {"bins": edges[:1001]}], [])
    assert cliques_module.MOST_CELLS == 1_000_000
    # 1000 x 1000 is the limit: the counts are then missing, which is the next refusal
    _refused(_with_cliques(exactly, columns={"income": {"domain": "number"}}), "noisyCounts")


def test_a_table_holds_at_most_1024_cliques_and_the_limit_is_read_before_any_clique() -> None:
    names = {f"c{n}": {"domain": "category"} for n in range(1025)}
    cliques = [_small([name], [{"levels": ["x"]}], ["1"]) for name in names]
    document = _with_cliques(*cliques[:1024], columns=names)
    document["privacy"]["privacyLoss"] = {"rho": "30"}
    assert len(parse_profile(document).tables["subscriber"].cliques2) == 1024
    over = _with_cliques(*cliques, columns=names)
    over["privacy"]["privacyLoss"] = {"rho": "30"}
    _refused(over, "at most 1024 cliques")


def test_a_table_s_cliques_hold_at_most_ten_million_cells_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert cliques_module.MOST_TABLE_CELLS == 10_000_000
    monkeypatch.setattr(cliques_module, "MOST_TABLE_CELLS", 5)
    a = _small(["age"], [{"bins": ["0", "1", "2", "3"]}], ["1", "1", "1"])
    b = _small(["plan"], [{"levels": ["a", "b", "c"]}], ["1", "1", "1"])
    assert parse_profile(_with_cliques(a))
    _refused(_with_cliques(a, b), "cells together")


def test_the_table_limits_belong_to_format_1_4_0_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cliques_module, "MOST_TABLE_CELLS", 2)

    def older(document: Dict[str, Any]) -> None:
        document["_schemaURL"] = SCHEMA_URL_1_3_0
        _table(document).pop("cliqueSelection", None)
        _table(document)["cliques"] = _table(document)["cliques"][:1]

    assert parse_profile(_document(older)).tables["subscriber"].cliques


def test_a_clique_marginal_1_keeps_its_two_column_minimum_and_its_levels_only() -> None:
    def change(document: Dict[str, Any]) -> None:
        _table(document)["cliques"][0]["columns"] = ["region"]
        _table(document)["cliques"][0]["levels"] = [["east", "north", "west"]]

    _refused(_document(change), "at least two distinct columns")


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------


def test_the_schema_validates_the_example_and_refuses_what_the_reader_refuses() -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads((SPEC / "profile-1-4.schema.json").read_text("utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(EXAMPLE)
    for change in (
        lambda c: c.update(estimate={"variance": "1"}),
        lambda c: c.update(columns=[f"c{n}" for n in range(33)]),
        lambda c: c.update(columns=[]),
        lambda c: c["domains"].__setitem__(0, {"levels": ["a"], "bins": ["0", "1"]}),
        lambda c: c["domains"].__setitem__(1, {"bins": ["0"]}),
        lambda c: c["noisyCounts"].__setitem__(0, 1.5),
        lambda c: c.pop("noise"),
    ):
        document = _document(lambda d, change=change: change(_clique_2(d)))
        assert list(validator.iter_errors(document))
    too_many = _document(lambda d: _table(d)["cliques"].extend([_clique_2(d)] * 1024))
    assert list(validator.iter_errors(too_many))
