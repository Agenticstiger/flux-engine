# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``privacyUnit.follows`` (profile format 1-4-0): a unit of privacy that takes its child rows with it.

ADR 0037 decision 1 and ``docs/spec/PROFILE-SPEC.md`` section 3a describe it. The listed
relationships form a tree rooted at the expose, ``m_T`` is the product of the caps along the one
path into a table, and a noisy statistic over table ``T`` is charged ``m_T`` times the sensitivity of
one row of ``T``. Each refusal is ``FLX-5036`` naming where, and a profile without ``follows`` reads
exactly as it did.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from jsonschema import Draft202012Validator

from flux_stats.errors import FluxError
from flux_stats.noise import Follow, reach_of
from flux_stats.profile import SCHEMA_URL_1_3_0, parse_profile

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _spec()
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-1-4-follows-example.json").read_text("utf-8"))
SCHEMA: Dict[str, Any] = json.loads((SPEC / "profile-1-4.schema.json").read_text("utf-8"))


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


def _key(table: str, *columns: str) -> Dict[str, Any]:
    return {"table": table, "columns": list(columns)}


def _histogram(scale: str, rho: str) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "levels": ["a", "b"],
        "sampleSize": {"min": 5000, "max": 9999},
        "disclosure": "noisy",
        "noise": {"mechanism": "gaussian", "scale": scale, "privacyLoss": {"rho": rho}},
        "groups": [{"group": "", "status": "F", "noisyCounts": ["10", "20"]}],
    }


def _tables(names: List[str]) -> Dict[str, Any]:
    return {
        name: {
            "columns": {
                "id": {"domain": "category"},
                "parent_id": {"domain": "category"},
                "other_id": {"domain": "category"},
                "kind": {"domain": "category"},
                "x": {"domain": "number"},
                "y": {"domain": "number"},
            }
        }
        for name in names
    }


def _edge(parent: str, child: str, column: str = "parent_id") -> Dict[str, Any]:
    return {"parent": _key(parent, "id"), "child": _key(child, column), "statistics": []}


def _follow(parent: str, child: str, cap: int, column: str = "parent_id") -> Dict[str, Any]:
    return {"parent": _key(parent, "id"), "child": _key(child, column), "maxChildren": cap}


def _chain(
    follows: Optional[List[Dict[str, Any]]] = None,
    edges: Optional[List[Dict[str, Any]]] = None,
    *,
    total: str = "1000",
) -> Dict[str, Any]:
    """Tables ``acct``, ``inv``, ``line``, ``other``; a chain ``acct -> inv (3) -> line (4)``."""
    document = copy.deepcopy(EXAMPLE)
    document["tables"] = _tables(["acct", "inv", "line", "other"])
    document["privacy"]["privacyLoss"] = {"rho": total}
    document["privacy"]["privacyUnit"] = {
        "expose": "acct",
        "follows": (
            follows
            if follows is not None
            else [_follow("acct", "inv", 3), _follow("inv", "line", 4)]
        ),
    }
    document["relationships"] = (
        edges if edges is not None else [_edge("acct", "inv"), _edge("inv", "line")]
    )
    # the expose holds one noisy histogram, so the account is spent
    document["tables"]["acct"]["columns"]["kind"]["statistics"] = [_histogram("10", "0.005")]
    return document


def _noisy(document: Dict[str, Any], table: str, scale: str, rho: str) -> Dict[str, Any]:
    document["tables"][table]["columns"]["kind"]["statistics"] = [_histogram(scale, rho)]
    return document


def _reach(document: Dict[str, Any]) -> Dict[str, int]:
    profile = parse_profile(document)
    assert profile.accounting is not None
    return dict(profile.accounting.reached)


# ---------------------------------------------------------------------------
# The example and the schema
# ---------------------------------------------------------------------------


def test_the_example_reads_validates_and_reaches_the_child_table() -> None:
    Draft202012Validator(SCHEMA).validate(EXAMPLE)
    profile = parse_profile(copy.deepcopy(EXAMPLE))
    assert profile.accounting is not None
    assert profile.accounting.reach("account") == 1
    assert profile.accounting.reach("invoice") == 3
    assert profile.accounting.reach("elsewhere") == 0
    assert [entry.max_children for entry in profile.accounting.follows] == [3]
    assert len(profile.parts) == 2


def test_the_schema_refuses_what_the_reader_refuses_in_a_follows_entry() -> None:
    validator = Draft202012Validator(SCHEMA)
    for change in (
        lambda f: f.pop("maxChildren"),
        lambda f: f.update(maxChildren=0),
        lambda f: f.update(maxChildren="3"),
        lambda f: f.update(extra=1),
    ):
        document = copy.deepcopy(EXAMPLE)
        change(document["privacy"]["privacyUnit"]["follows"][0])
        assert not validator.is_valid(document)
        with pytest.raises(FluxError):
            parse_profile(document)
    document = copy.deepcopy(EXAMPLE)
    document["privacy"]["privacyUnit"]["follows"] = []
    assert not validator.is_valid(document)
    with pytest.raises(FluxError):
        parse_profile(document)


# ---------------------------------------------------------------------------
# Opt in, and the format
# ---------------------------------------------------------------------------


def test_without_follows_the_unit_is_one_row_of_the_expose_as_before() -> None:
    document = _chain()
    del document["privacy"]["privacyUnit"]["follows"]
    profile = parse_profile(document)
    assert profile.accounting is not None and profile.accounting.follows == ()
    assert profile.accounting.reach("acct") == 1 and profile.accounting.reach("inv") == 0
    _noisy(document, "inv", "10", "0.005")
    assert "another expose" in _refused(document)


def test_follows_needs_format_1_4_0() -> None:
    document = _chain()
    document["_schemaURL"] = SCHEMA_URL_1_3_0
    message = _refused(document)
    assert "privacyUnit.follows" in message and "1-4-0" in message


def test_follows_with_nothing_noisy_in_the_followed_tables_reads_and_reaches_them() -> None:
    document = _chain()
    assert _reach(document) == {"acct": 1, "inv": 3, "line": 12}


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


def test_two_followed_foreign_keys_into_one_table_are_refused() -> None:
    # cap 1 on each: a reach of 2 would be stated, while one parent row brings rows in through both
    document = _chain(
        follows=[
            _follow("acct", "inv", 1, "parent_id"),
            _follow("acct", "inv", 1, "other_id"),
        ],
        edges=[_edge("acct", "inv", "parent_id"), _edge("acct", "inv", "other_id")],
    )
    message = _refused(document)
    assert "follows[1]" in message and "second path" in message


def test_two_paths_into_one_table_through_different_parents_are_refused() -> None:
    document = _chain(
        follows=[
            _follow("acct", "inv", 2),
            _follow("acct", "other", 2),
            _follow("inv", "line", 2),
            _follow("other", "line", 2, "other_id"),
        ],
        edges=[
            _edge("acct", "inv"),
            _edge("acct", "other"),
            _edge("inv", "line"),
            _edge("other", "line", "other_id"),
        ],
    )
    assert "follows[3]" in _refused(document)


def test_a_followed_relationship_whose_parent_is_not_reached_is_refused() -> None:
    document = _chain(
        follows=[_follow("acct", "inv", 2), _follow("other", "line", 5)],
        edges=[_edge("acct", "inv"), _edge("other", "line")],
    )
    message = _refused(document)
    assert "follows[1]" in message and "does not reach" in message


def test_a_cycle_is_refused() -> None:
    document = _chain(
        follows=[_follow("acct", "inv", 2), _follow("inv", "line", 2), _follow("line", "inv", 2)],
        edges=[_edge("acct", "inv"), _edge("inv", "line"), _edge("line", "inv")],
    )
    assert "follows[" in _refused(document)
    # a cycle that does not touch the expose has no parent that is reached
    detached = _chain(
        follows=[_follow("inv", "line", 2), _follow("line", "inv", 2)],
        edges=[_edge("inv", "line"), _edge("line", "inv")],
    )
    assert "follows[" in _refused(detached)


def test_the_expose_is_never_a_child() -> None:
    document = _chain(
        follows=[_follow("acct", "inv", 2), _follow("inv", "acct", 2)],
        edges=[_edge("acct", "inv"), _edge("inv", "acct")],
    )
    message = _refused(document)
    assert "follows[1]" in message and "expose" in message


def test_a_followed_relationship_must_be_one_of_the_profiles_relationships() -> None:
    document = _chain(edges=[_edge("acct", "inv")])
    message = _refused(document)
    assert "follows[1]" in message and "relationships" in message
    document = _chain(follows=[_follow("acct", "inv", 3, "other_id")], edges=[_edge("acct", "inv")])
    assert "follows[0]" in _refused(document)


def test_entries_are_read_strictly() -> None:
    for follows in (
        [{"parent": _key("acct", "id"), "child": _key("inv", "parent_id")}],
        [{**_follow("acct", "inv", 3), "x": 1}],
        [{**_follow("acct", "inv", 3), "maxChildren": 0}],
        [{**_follow("acct", "inv", 3), "maxChildren": True}],
        [{**_follow("acct", "inv", 3), "maxChildren": "3"}],
        [{**_follow("acct", "inv", 3), "parent": {"table": "acct"}}],
        [{**_follow("acct", "inv", 3), "child": _key("inv", "parent_id", "kind")}],
        [{**_follow("acct", "inv", 3), "child": _key("inv")}],
        [],
        {},
    ):
        document = _chain(follows=follows)  # type: ignore[arg-type]
        _refused(document)
    document = _chain()
    document["privacy"]["privacyUnit"]["other"] = 1
    _refused(document)


def test_the_tree_does_not_depend_on_the_order_of_the_entries() -> None:
    forward = _chain()
    backward = _chain(
        follows=[_follow("inv", "line", 4), _follow("acct", "inv", 3)],
        edges=[_edge("inv", "line"), _edge("acct", "inv")],
    )
    assert _reach(forward) == _reach(backward) == {"acct": 1, "inv": 3, "line": 12}


# ---------------------------------------------------------------------------
# The reach
# ---------------------------------------------------------------------------


def test_the_reach_of_a_three_level_chain_is_the_product_of_the_caps() -> None:
    document = _chain(
        follows=[_follow("acct", "inv", 3), _follow("inv", "line", 4), _follow("line", "other", 5)],
        edges=[_edge("acct", "inv"), _edge("inv", "line"), _edge("line", "other")],
    )
    assert _reach(document) == {"acct": 1, "inv": 3, "line": 12, "other": 60}


def test_branches_each_multiply_along_their_own_path() -> None:
    document = _chain(
        follows=[_follow("acct", "inv", 7), _follow("acct", "other", 2), _follow("inv", "line", 3)],
        edges=[_edge("acct", "inv"), _edge("acct", "other"), _edge("inv", "line")],
    )
    assert _reach(document) == {"acct": 1, "inv": 7, "other": 2, "line": 21}


def test_reach_of_is_exact_integer_arithmetic_beyond_a_float() -> None:
    follows = [Follow("a", ("id",), "b", ("id",), 2**40), Follow("b", ("id",), "c", ("id",), 2**40)]
    assert reach_of("a", follows, "x")["c"] == 2**80


# ---------------------------------------------------------------------------
# What a noisy statistic is charged
# ---------------------------------------------------------------------------

# A count table of reach m spends rho = m^2 / (2 s^2) under a Gaussian of scale s.


@pytest.mark.parametrize(
    ("table", "reach", "scale"),
    [("acct", 1, "1"), ("inv", 3, "3"), ("line", 12, "12")],
)
def test_a_count_table_is_charged_its_reach_in_the_squared_l2(
    table: str, reach: int, scale: str
) -> None:
    assert reach * reach == int(scale) ** 2  # rho = 1/2 exactly at this scale
    accepted = _noisy(_chain(), table, scale, "0.5")
    profile = parse_profile(accepted)
    assert dict(profile.parts)[f"profile.tables.{table}.columns.kind.statistics[0]"].loss.rho == (
        Fraction(1, 2)
    )
    refused = _noisy(_chain(), table, scale, "0.4999")
    assert "what a count table spends" in _refused(refused)


def test_a_count_table_of_the_expose_is_charged_one_with_or_without_follows() -> None:
    document = _noisy(_chain(), "acct", "1", "0.5")
    parse_profile(document)
    _refused(_noisy(_chain(), "acct", "1", "0.499"))
    document = _noisy(_chain(), "acct", "1", "0.5")
    del document["privacy"]["privacyUnit"]["follows"]
    parse_profile(document)


def test_an_under_charged_share_is_refused_in_a_followed_table() -> None:
    # reach 3 and scale 1: the table spends 9/2, so a share of 4 is below it
    document = _noisy(_chain(), "inv", "1", "4")
    message = _refused(document)
    assert "privacyLoss" in message and "inv" in message
    parse_profile(_noisy(_chain(), "inv", "1", "4.5"))


def test_a_laplace_is_charged_its_reach_in_l1_under_approximate_dp() -> None:
    def build(epsilon: str) -> Dict[str, Any]:
        document = _noisy(_chain(), "inv", "1", "1")
        document["privacy"]["measure"] = "approximateDP"
        document["privacy"]["privacyLoss"] = {"epsilon": "100", "delta": "0"}
        document["tables"]["acct"]["columns"]["kind"]["statistics"][0]["noise"] = {
            "mechanism": "laplace",
            "scale": "10",
            "privacyLoss": {"epsilon": "0.1", "delta": "0"},
        }
        statistic = document["tables"]["inv"]["columns"]["kind"]["statistics"][0]
        statistic["noise"] = {
            "mechanism": "laplace",
            "scale": "2",
            "privacyLoss": {"epsilon": epsilon, "delta": "0"},
        }
        return document

    parse_profile(build("1.5"))  # reach 3 over scale 2
    assert "an epsilon below" in _refused(build("1.4999"))


def test_cliques_and_rank_correlations_are_charged_their_reach_too() -> None:
    document = _chain()
    inv = document["tables"]["inv"]
    inv["cliques"] = [
        {
            "definition": "cliqueMarginal/1",
            "columns": ["kind", "id"],
            "levels": [["a", "b"], ["c", "d"]],
            "noisyCounts": ["1", "2", "3", "4"],
            "noise": {"mechanism": "gaussian", "scale": "3", "privacyLoss": {"rho": "0.5"}},
        }
    ]
    parse_profile(copy.deepcopy(document))
    inv["cliques"][0]["noise"]["privacyLoss"]["rho"] = "0.49"
    _refused(document)
    # Kendall with row floor 9: 4/10 per row, reach 3 gives 6/5; scale 6/5 gives rho 1/2 exactly
    pair = {
        "columns": ["x", "y"],
        "statistics": [
            {
                "definition": "kendallTau/1",
                "rowFloor": 9,
                "sampleSize": {"min": 5000, "max": 9999},
                "disclosure": "noisy",
                "noise": {"mechanism": "gaussian", "scale": "1.2", "privacyLoss": {"rho": "0.5"}},
                "groups": [{"group": "", "status": "F", "noisyValue": "0.3"}],
            }
        ],
    }
    document = _chain()
    document["tables"]["inv"]["columnPairs"] = [copy.deepcopy(pair)]
    parse_profile(copy.deepcopy(document))
    document["tables"]["inv"]["columnPairs"][0]["statistics"][0]["noise"]["privacyLoss"][
        "rho"
    ] = "0.4999"
    _refused(document)
    # the same statistic in the expose is charged 4/10 and needs rho 0.08 / ... only
    document = _chain()
    document["tables"]["acct"]["columnPairs"] = [copy.deepcopy(pair)]
    document["tables"]["acct"]["columnPairs"][0]["statistics"][0]["noise"]["scale"] = "0.4"
    document["tables"]["acct"]["columnPairs"][0]["statistics"][0]["noise"]["privacyLoss"] = {
        "rho": "0.5"
    }
    parse_profile(document)


def test_a_noisy_statistic_over_a_table_the_unit_does_not_reach_is_refused() -> None:
    document = _noisy(_chain(), "other", "10", "0.005")
    message = _refused(document)
    assert "follows no relationship into this table" in message
    # a clique and a selection share of such a table are refused too
    document = _chain()
    document["tables"]["other"]["cliqueSelection"] = {
        "method": "fixed",
        "producer": "https://example.org/profiler/1.0",
        "published": "measured",
        "domain": {"privacyLoss": {"rho": "0.01"}},
    }
    _refused(document)


def test_the_budget_adds_the_scaled_shares_exactly() -> None:
    document = _noisy(_noisy(_chain(total="1"), "acct", "1", "0.5"), "inv", "3", "0.5")
    parse_profile(document)
    document["privacy"]["privacyLoss"] = {"rho": "0.99"}
    assert "more than the total" in _refused(document)
