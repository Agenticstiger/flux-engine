# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bridge table's pairs are unique, and both sides exist (ADR 0022 section 8).

``e`` points at ``s`` (12 rows) through ``student`` and at ``c`` (10 rows) through ``course``.
Declared as a bridge, the two relationships are one key of two parts: the point
``permute/2(k, 12 * 10, rowIndex)``, decoded in mixed radix with ``student`` first, so no pair of
parent rows repeats and each side names a row its parent holds.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.permute import permute2
from flux_engine.generator import columns
from flux_engine.generator.relations import CompositeMember
from flux_engine.generator.rows import GeneratorPlan, generate_row_slice, generate_rows
from flux_engine.seam.links import LinkEntry

from .test_relations import _column, _expose, _h, _primary, _rel
from .test_rules_over_links import _by_both_paths
from .test_shared_copies import plans_of, root_for, table

pytestmark = pytest.mark.unit

S, C = 12, 10
BRIDGE = {"e": [("student", "course")]}


def bridge_contract(**course: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.bridge",
        "exposes": [
            _expose(
                "s",
                [_column("s_id", "INT"), _column("s_name", "STRING", False)],
                _primary("s", "s_id"),
            ),
            _expose(
                "c",
                [_column("c_id", "INT"), _column("c_title", "STRING", False)],
                _primary("c", "c_id"),
            ),
            _expose(
                "e",
                [
                    _column("e_student", "INT", False),
                    _column("e_course", "INT", False),
                    _column("s_name", "STRING", False),
                    _column("c_title", "STRING", False),
                    _column("e_note", "STRING", False),
                ],
                {
                    "relationships": [
                        _rel("student", "s", ["e_student"], ["s_id"], sharedColumns=["s_name"]),
                        _rel(
                            "course",
                            "c",
                            ["e_course"],
                            ["c_id"],
                            sharedColumns=["c_title"],
                            **course,
                        ),
                    ]
                },
            ),
        ],
    }


def declared(rows: int) -> Dict[str, Tuple[Dict[str, Any], int]]:
    return {
        "s": ({"s_name": {"levels": {"ann": "0.5", "bo": "0.5"}}}, S),
        "c": ({"c_title": {"levels": {"x": "0.5", "y": "0.5"}}}, C),
        "e": ({"e_note": {"levels": {"a": "0.5", "b": "0.5"}}, "s_name": {}, "c_title": {}}, rows),
    }


def planned(rows: int = 110, **kwargs: Any) -> Dict[str, GeneratorPlan]:
    kwargs.setdefault("bridges", BRIDGE)
    return plans_of(bridge_contract(), declared=declared(rows), **kwargs)


def pairs_of(plan: GeneratorPlan) -> List[Tuple[Any, Any]]:
    return [
        (row["e_student"], row["e_course"])
        for row in table(plan, generate_rows(plan, root=root_for()))
    ]


def test_no_pair_repeats_across_a_bridge_with_rows_close_to_the_pairs() -> None:
    pairs = pairs_of(planned(110)["e"])
    assert len(pairs) == 110 and len(set(pairs)) == 110
    assert set(pairs) <= {(s, c) for s in range(1, S + 1) for c in range(1, C + 1)}


def test_a_bridge_with_as_many_rows_as_pairs_holds_every_pair_once() -> None:
    pairs = pairs_of(planned(S * C)["e"])
    assert sorted(pairs) == [(s, c) for s in range(1, S + 1) for c in range(1, C + 1)]


def test_both_sides_name_a_row_their_parent_holds() -> None:
    world = {
        name: table(plan, generate_rows(plan, root=root_for())) for name, plan in planned().items()
    }
    students = {row["s_id"] for row in world["s"]}
    courses = {row["c_id"] for row in world["c"]}
    assert all(row["e_student"] in students and row["e_course"] in courses for row in world["e"])


def test_a_copy_through_either_side_is_the_parents_value() -> None:
    world = {
        name: table(plan, generate_rows(plan, root=root_for())) for name, plan in planned().items()
    }
    names = {row["s_id"]: row["s_name"] for row in world["s"]}
    titles = {row["c_id"]: row["c_title"] for row in world["c"]}
    for row in world["e"]:
        assert row["s_name"] == names[row["e_student"]]
        assert row["c_title"] == titles[row["e_course"]]


# The layout key of the bridge ``student``, ``course`` of ``e``, worked by hand from KERNEL-SPEC
# section 8.2 and KERNEL-SPEC-2 section 8: the digest of ``flux-key/2``, NUL, the emit's seam
# digest's bytes, NUL and the label ("sim", sim_id, "gen", "e", "pair", "student", "course"),
# whose parts are joined by U+001F.
PAIR_KEY = "be811b697f0014ac47cdd917e1431f9d55420c0e33e386ac6dd73189ff42e094"


def _hand_key(h: str, parts: Sequence[str]) -> bytes:
    material = (
        b"flux-key/2"
        + b"\x00"
        + bytes.fromhex(h.split(":")[1])
        + b"\x00"
        + b"\x1f".join(part.encode("utf-8") for part in parts)
    )
    return hashlib.sha256(material).digest()


def test_the_layout_key_is_the_labelled_pair_and_each_row_is_its_point_decoded() -> None:
    plan = planned()["e"]
    assert plan.relations is not None
    (composite,) = {
        driver.composite
        for _, driver in plan.relations.drivers
        if isinstance(driver, CompositeMember)
    }
    label = ("sim", plan.sim_id, "gen", "e", "pair", "student", "course")
    assert composite.key == _hand_key(_h("e"), label)
    assert composite.key.hex() == PAIR_KEY
    assert composite.columns == ("e_student", "e_course") and composite.space == S * C
    assert [part.relationship for part in composite.parts] == ["student", "course"]  # type: ignore[attr-defined]
    for row, (student, course) in enumerate(pairs_of(plan)):
        point = permute2(composite.key, S * C, row)
        assert (student, course) == (point // C + 1, point % C + 1)


@pytest.mark.parametrize("chunk", [5, 64])
def test_the_column_paths_cells_under_a_bridge_are_the_per_row_paths_rows(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    root = root_for()
    for expose_id, plan in planned().items():
        by_columns, by_rows, taken = _by_both_paths(
            monkeypatch, lambda plan=plan: generate_rows(plan, root=root)
        )
        assert by_columns == by_rows, expose_id
        assert taken and all(taken), expose_id


@pytest.mark.parametrize(("start", "stop"), [(0, 1), (3, 40), (109, 110)])
def test_a_slice_of_a_bridge_is_the_whole_tables_slice(start: int, stop: int) -> None:
    plan = planned()["e"]
    root = root_for()
    assert (
        generate_row_slice(plan, start, stop, root=root)
        == generate_rows(plan, root=root)[start:stop]
    )


def test_a_bridge_is_part_of_the_plan_document() -> None:
    with_bridge = planned()["e"].as_document()
    without = planned(bridges=None)["e"].as_document()
    assert with_bridge != without


def _refused(code: str = "FLX-3403", **kwargs: Any) -> str:
    contract = kwargs.pop("contract", None) or bridge_contract()
    rows = kwargs.pop("rows", 110)
    with pytest.raises(FluxError) as caught:
        plans_of(contract, declared=kwargs.pop("declared", None) or declared(rows), **kwargs)
    assert caught.value.code.code == code, str(caught.value)
    return str(caught.value)


def test_more_rows_than_pairs_is_refused() -> None:
    assert "pairs" in _refused(bridges=BRIDGE, rows=S * C + 1)


def test_a_null_fraction_on_either_first_column_is_refused() -> None:
    for column in ("e_student", "e_course"):
        statistics = declared(110)
        statistics["e"] = ({**statistics["e"][0], column: {"nullFraction": "0.1"}}, 110)
        assert "nullFraction" in _refused(bridges=BRIDGE, declared=statistics)


def test_a_one_to_one_side_is_refused() -> None:
    contract = bridge_contract()
    contract["exposes"][2]["semantics"]["relationships"][1]["cardinality"] = "one_to_one"
    assert "at most once" in _refused(bridges=BRIDGE, contract=contract, rows=C)


def test_a_side_whose_columns_are_a_key_or_part_of_one_is_refused() -> None:
    contract = bridge_contract()
    contract["exposes"][2]["semantics"]["entities"] = [
        {"name": "e", "type": "primary", "expr": "e_student"}
    ]
    assert "key" in _refused(bridges=BRIDGE, contract=contract, rows=S)


def test_a_side_another_relationship_implies_is_refused() -> None:
    from .test_shared_copies import shared_contract, statistics

    contract = shared_contract()
    contract["exposes"][3]["semantics"]["relationships"] += [
        {
            "name": "c_x_only",
            "to": "x",
            "fromColumns": ["c_x"],
            "toColumns": ["x_id"],
            "cardinality": "many_to_one",
        },
        {
            "name": "c_y_only",
            "to": "y",
            "fromColumns": ["c_y"],
            "toColumns": ["y_id"],
            "cardinality": "many_to_one",
        },
    ]
    with pytest.raises(FluxError) as caught:
        plans_of(contract, declared=statistics(), bridges={"c": [("c_x_only", "c_y_only")]})
    assert caught.value.code.code == "FLX-3403" and "implied by 'link'" in str(caught.value)


def test_a_links_condition_on_a_side_is_refused() -> None:
    links = {"e": [LinkEntry("e", "student", "e_note = 'a'")]}
    assert "links condition" in _refused(bridges=BRIDGE, links=links)


def test_the_same_relationship_in_two_bridges_is_refused() -> None:
    bridges: Mapping[str, Sequence[Tuple[str, str]]] = {
        "e": [("student", "course"), ("course", "student")]
    }
    message = _refused(bridges=bridges)
    # The pair is named as plain text, never as a Python list.
    assert "already holds" in message and "bridge (student, course)" in message
    assert "[" not in message


def test_a_bridge_on_an_expose_the_simulation_does_not_generate_is_refused() -> None:
    assert "does not generate" in _refused(bridges={"nowhere": [("a", "b")]})


def test_kernel_0_1_0_refuses_a_bridge_and_names_the_kernel() -> None:
    statistics = declared(110)
    levels = {"levels": {"a": "1"}}
    statistics["e"] = ({**statistics["e"][0], "s_name": levels, "c_title": levels}, 110)
    assert "0.2.0" in _refused("FLX-3105", bridges=BRIDGE, counter_based=False, declared=statistics)
