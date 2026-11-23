# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column drawn ``given`` several columns, keyed by the joint level of its parents.

``given`` may be a list of two or more column names. The column then carries one weight table per
combination of the parents' levels, nested one mapping per parent in the order ``given`` lists them.
The per-row path (:func:`flux_engine.generator.rows._cell`) is the oracle: the column path of kernel
0.2.0 (:mod:`flux_engine.generator.columns`) must hold exactly its rows. The seed paths are those
of any other column of the plan, so nothing that exists moves.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator import columns
from flux_engine.generator.rows import (
    build_plan,
    generate_column,
    generate_row_slice,
    generate_rows,
)
from flux_engine.generator.statistics import joint_level, statistics_for

from .conftest import SIM_ID, H, expose_from_schema, fidelity
from .test_columns import _both, _plan, _root

pytestmark = pytest.mark.unit

_SCHEMA: List[Dict[str, Any]] = [
    {"name": "region", "type": "STRING"},
    {"name": "plan", "type": "STRING"},
    {"name": "channel", "type": "STRING"},
    {"name": "tier", "type": "STRING"},
    {"name": "spend", "type": "DECIMAL(18,2)"},
]

#: channel is given (region, plan); tier is given (channel, region), so the links form a graph
#: and not a forest; spend is a histogram given (region, plan).
_DECLARED: Dict[str, Any] = {
    "region": {"levels": {"east": "0.5", "north": "0.3", "west": "0.2"}},
    "plan": {"levels": {"basic": "0.7", "pro": "0.3"}},
    "channel": {
        "given": ["region", "plan"],
        "levelWeightsGiven": {
            "east": {
                "basic": {"online": "9", "store": "1"},
                "pro": {"online": "1", "store": "1"},
            },
            "north": {
                "basic": {"online": "0", "store": "1"},
                "pro": {"online": "3", "store": "1"},
            },
            "west": {
                "basic": {"online": "1", "store": "0"},
                "pro": {"online": "2", "store": "5"},
            },
        },
    },
    "tier": {
        "given": ["channel", "region"],
        "levelWeightsGiven": {
            "online": {
                "east": {"gold": "1", "silver": "3"},
                "north": {"gold": "1", "silver": "1"},
                "west": {"gold": "0", "silver": "1"},
            },
            "store": {
                "east": {"gold": "1", "silver": "0"},
                "north": {"gold": "2", "silver": "1"},
                "west": {"gold": "1", "silver": "1"},
            },
        },
    },
    "spend": {
        "range": {"min": "0", "max": "300"},
        "bins": ["0", "100", "300"],
        "given": ["region", "plan"],
        "binWeightsGiven": {
            "east": {"basic": ["1", "0"], "pro": ["1", "1"]},
            "north": {"basic": ["0", "1"], "pro": ["3", "1"]},
            "west": {"basic": ["1", "1"], "pro": ["1", "3"]},
        },
    },
}


def _declared() -> Dict[str, Any]:
    import copy

    return copy.deepcopy(_DECLARED)


@pytest.mark.parametrize("chunk", [1, 7, 64, columns.CHUNK_ROWS])
def test_the_column_path_holds_the_per_row_rows(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    plan, root = _plan(rows=300, schema=_SCHEMA, declared=_declared()), _root()
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows
    assert taken == [True]


@pytest.mark.parametrize("seed", [0, 1, 7, 99])
def test_it_agrees_under_any_seed(monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    from flux_engine.core import kernel2
    from flux_engine.core.seedtree import derive_root, digest_bytes

    plan = _plan(rows=200, schema=_SCHEMA, declared=_declared())
    root = derive_root(digest_bytes(H), seed, seed_tree_version=kernel2.SEED_TREE_VERSION)
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows
    assert taken == [True]


@pytest.mark.parametrize(("start", "stop"), [(0, 0), (0, 1), (5, 9), (13, 29), (299, 300)])
def test_a_slice_anywhere_is_the_per_row_slice(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)
    plan, root = _plan(rows=300, schema=_SCHEMA, declared=_declared()), _root()
    by_columns, by_rows, taken = _both(
        monkeypatch, lambda: generate_row_slice(plan, start, stop, root=root)
    )
    assert by_columns == by_rows
    assert taken == [True]


def test_every_combination_is_drawn_with_its_own_weights() -> None:
    plan, root = _plan(rows=4000, schema=_SCHEMA, declared=_declared()), _root()
    rows = generate_rows(plan, root=root)
    seen = {(row[0], row[1], row[2]) for row in rows}
    # Weights of zero are never drawn, and every other combination is.
    assert ("north", "basic", "online") not in seen
    assert ("west", "basic", "store") not in seen
    assert len({(row[0], row[1]) for row in rows}) == 6
    assert {c for c in seen if c[:2] == ("east", "basic")} == {
        ("east", "basic", "online"),
        ("east", "basic", "store"),
    }
    east_basic = [row for row in rows if row[:2] == ("east", "basic")]
    online = sum(1 for row in east_basic if row[2] == "online") / len(east_basic)
    assert 0.85 < online < 0.95  # 9 of 10
    # A histogram given the same pair: east and basic draws bin 0 alone, north and basic bin 1.
    for row in rows:
        if row[:2] == ("east", "basic"):
            assert row[4] < 100
        if row[:2] == ("north", "basic"):
            assert row[4] >= 100


def test_column_first_equals_row_first() -> None:
    plan, root = _plan(rows=120, schema=_SCHEMA, declared=_declared()), _root()
    rows = generate_rows(plan, root=root)
    for position, name in enumerate(["region", "plan", "channel", "tier", "spend"]):
        assert generate_column(plan, name, root=root) == tuple(row[position] for row in rows)


def test_the_parents_and_the_digest_of_a_several_column_given() -> None:
    plan = _plan(rows=10, schema=_SCHEMA, declared=_declared())
    channel = plan.column_plan("channel").statistics
    assert channel.given is None and channel.given_columns == ("region", "plan")
    assert channel.parents == ("region", "plan")
    document = channel.as_document()
    assert document["given"] == ["region", "plan"]
    assert joint_level(["east", "basic"]) in document["levelWeightsGiven"]
    # One parent is as it was: a name, with no new key.
    single = _plan(
        rows=10,
        schema=_SCHEMA[:3],
        declared={
            "region": {"levels": {"east": "1"}},
            "plan": {"levels": {"basic": "1"}},
            "channel": {"given": "region", "levelWeightsGiven": {"east": {"online": "1"}}},
        },
    )
    assert single.column_plan("channel").statistics.as_document()["given"] == "region"
    assert single.column_plan("channel").statistics.given_columns is None


def test_the_joint_level_cannot_be_mistaken_for_another_combination() -> None:
    assert joint_level(["a,b", "c"]) != joint_level(["a", "b,c"])
    assert joint_level(['a"', "b"]) != joint_level(["a", '"b'])


def _refused(declared: Dict[str, Any], match: str, schema: Any = _SCHEMA) -> None:
    with pytest.raises(FluxError, match=match) as raised:
        _plan(rows=10, schema=schema, declared=declared)
    assert raised.value.code.code == "FLX-3402"


def test_a_declaration_the_generator_cannot_draw_is_refused() -> None:
    declared = _declared()
    declared["channel"]["given"] = ["region"]
    _refused(declared, "write a single column as a name")
    declared = _declared()
    declared["channel"]["given"] = ["region", "region"]
    _refused(declared, "names one column twice")
    declared = _declared()
    declared["channel"]["given"] = ["region", "channel"]
    _refused(declared, "not drawn conditionally on itself")
    declared = _declared()
    del declared["channel"]["levelWeightsGiven"]["east"]["pro"]
    _refused(declared, "levelWeightsGiven declares levels")
    declared = _declared()
    declared["channel"]["levelWeightsGiven"]["east"] = {"online": "1", "store": "1"}
    _refused(declared, "is a non-empty level -> weight mapping")
    declared = _declared()
    declared["channel"]["given"] = ["region", "missing"]
    _refused(declared, "not a column of this expose")


def test_a_cycle_through_several_parents_is_refused_naming_it() -> None:
    declared = {
        "region": {
            "given": ["plan", "channel"],
            "levelWeightsGiven": _two_by_two(["east", "west"]),
        },
        "plan": {"levels": {"basic": "0.5", "pro": "0.5"}},
        "channel": {
            "given": "region",
            "levelWeightsGiven": {
                "east": {"online": "1", "store": "1"},
                "west": {"online": "1", "store": "1"},
            },
        },
    }
    with pytest.raises(FluxError, match="given forms a cycle"):
        _plan(rows=10, schema=_SCHEMA[:3], declared=declared)


def _two_by_two(levels: List[str]) -> Dict[str, Any]:
    return {
        "basic": {"online": {k: "1" for k in levels}, "store": {k: "1" for k in levels}},
        "pro": {"online": {k: "1" for k in levels}, "store": {k: "1" for k in levels}},
    }


def test_a_parent_with_nulls_is_refused() -> None:
    declared = _declared()
    declared["plan"]["nullFraction"] = "0.1"
    _refused(declared, "declares a nullFraction")


def test_a_one_parent_declaration_reads_as_it_did() -> None:
    column = expose_from_schema(_SCHEMA[:2]).columns[1]
    statistics = statistics_for(
        column,
        fidelity(
            {
                "plan": {
                    "given": "region",
                    "levelWeightsGiven": {"east": {"basic": "1", "pro": "1"}},
                }
            }
        ),
    )
    assert statistics.given == "region" and statistics.parents == ("region",)
    assert statistics.level_weights_given == {"east": {"basic": 1, "pro": 1}}


def test_the_rows_are_pinned_so_every_platform_draws_the_same_bytes() -> None:
    plan, root = _plan(rows=500, schema=_SCHEMA, declared=_declared()), _root()
    digest = hashlib.sha256(repr(generate_rows(plan, root=root)).encode("utf-8")).hexdigest()
    assert digest == PINNED_ROWS


PINNED_ROWS = "fc46c61ac1d406cded2a5050729dddd96a7e03c6f56b293d12abbbd00088e392"


def test_kernel_one_draws_a_several_column_given_on_its_per_row_path() -> None:
    from flux_engine.core.seedtree import derive_root, digest_bytes

    plan = build_plan(
        expose_from_schema(list(_SCHEMA)),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=50,
        fidelity=fidelity(_declared()),
    )
    rows = generate_rows(plan, root=derive_root(digest_bytes(H), 42))
    assert len(rows) == 50 and {row[0] for row in rows} <= {"east", "north", "west"}
