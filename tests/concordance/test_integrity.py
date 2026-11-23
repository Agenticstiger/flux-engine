# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Referential integrity of a declared relationship, as four shares that hold."""

from __future__ import annotations

from fractions import Fraction
from typing import Any, List

import pytest

from flux_engine.concordance.integrity import CHECKS, link_integrity
from flux_engine.seam.keys import Relationship
from flux_engine.stages._module_checks import ModuleChecks

pytestmark = pytest.mark.unit


def _link(
    cardinality: str = "many_to_one", join_type: str = "left", columns: int = 1
) -> Relationship:
    return Relationship(
        name="owner",
        source="child",
        target="parent",
        from_columns=("fk", "fk2")[:columns],
        to_columns=("pk", "pk2")[:columns],
        cardinality=cardinality,
        join_type=join_type,
    )


PARENT_NAMES = ["pk", "pk2"]
CHILD_NAMES = ["fk", "fk2", "id"]


def _integrity(child: List[List[Any]], parent: List[List[Any]], **kwargs: Any):
    columns = kwargs.pop("columns", 1)
    return link_integrity(
        _link(columns=columns, **kwargs), CHILD_NAMES, child, PARENT_NAMES, parent
    )


def test_a_whole_relationship_scores_one_on_every_check() -> None:
    found = _integrity([[1, 0, "a"], [2, 0, "b"], [1, 0, "c"]], [[1, 0], [2, 0], [3, 0]])
    assert found.whole and found.score == 1
    assert [name for name, _ in found.checks] == ["found", "unique"]
    assert (found.found.satisfied, found.found.total) == (3, 3)


def test_a_child_key_the_parent_lacks_shows_as_a_share_under_one() -> None:
    found = _integrity([[1, 0, "a"], [9, 0, "b"], [2, 0, "c"], [8, 0, "d"]], [[1, 0], [2, 0]])
    assert (found.found.satisfied, found.found.total) == (2, 4)
    assert not found.whole
    assert found.found.share == Fraction(1, 2)
    assert found.score == Fraction(3, 4)  # found 1/2, unique 1


def test_a_duplicated_parent_key_shows_as_a_share_under_one() -> None:
    found = _integrity([[1, 0, "a"]], [[1, 0], [1, 0], [2, 0], [3, 0]])
    assert (found.unique.satisfied, found.unique.total) == (3, 4)  # type: ignore[union-attr]
    assert not found.whole


def test_a_versioned_parent_repeats_a_key_by_design() -> None:
    found = link_integrity(
        _link(), CHILD_NAMES, [[1, 0, "a"]], PARENT_NAMES, [[1, 0], [1, 0]], parent_versioned=True
    )
    assert found.unique is None and found.whole


def test_a_null_key_is_allowed_by_a_left_join_and_counted_under_an_inner_one() -> None:
    child = [[1, 0, "a"], [None, 0, "b"], [2, 0, "c"], [None, 0, "d"]]
    parent = [[1, 0], [2, 0]]
    left = _integrity(child, parent)
    assert left.nulls is None and left.whole
    assert (left.found.satisfied, left.found.total) == (2, 2)  # a NULL key reaches nothing
    inner = _integrity(child, parent, join_type="inner")
    assert inner.nulls is not None
    assert (inner.nulls.satisfied, inner.nulls.total) == (2, 4)
    assert not inner.whole and inner.score == Fraction(5, 6)  # found 1, unique 1, nulls 1/2


def test_a_one_to_one_parent_has_at_most_one_child() -> None:
    child = [[1, 0, "a"], [1, 0, "b"], [2, 0, "c"], [3, 0, "d"]]
    parent = [[1, 0], [2, 0], [3, 0], [4, 0]]
    many = _integrity(child, parent)
    assert many.one_to_one is None and many.whole
    one = _integrity(child, parent, cardinality="one_to_one")
    assert one.one_to_one is not None
    assert (one.one_to_one.satisfied, one.one_to_one.total) == (
        2,
        3,
    )  # parents 1, 2 and 3 have a child
    assert [name for name, _ in one.checks] == ["found", "unique", "oneToOne"]
    assert not one.whole


def test_a_composite_key_is_compared_as_a_tuple() -> None:
    child = [[1, 5, "a"], [1, 6, "b"]]
    parent = [[1, 5], [2, 6]]
    found = _integrity(child, parent, columns=2)
    assert (found.found.satisfied, found.found.total) == (1, 2)


def test_a_relationship_that_covers_no_row_has_no_score() -> None:
    found = _integrity([], [])
    assert found.score is None and found.whole
    assert found.found.share is None


def test_a_column_the_emit_does_not_carry_is_a_problem_not_a_value() -> None:
    with pytest.raises(ValueError, match="names a column its emit does not carry"):
        link_integrity(_link(), ["other"], [], PARENT_NAMES, [])


def test_the_checks_are_named_in_one_order() -> None:
    assert CHECKS == ("found", "unique", "nulls", "oneToOne")


def test_a_broken_key_fails_the_run_and_names_the_relationship() -> None:
    broken = _integrity([[1, 0, "a"], [9, 0, "b"]], [[1, 0]])
    checks = ModuleChecks(links=(broken,))
    (line,) = checks.failures()
    assert line == "integrity child.owner -> parent: found 1 of 2 (50.00%)"
    document = checks.artifact()["integrity"]
    assert document["ok"] is False
    assert document["relationships"][0]["checks"]["found"] == {
        "satisfied": 1,
        "total": 2,
        "share": "0.5",
    }
    assert ModuleChecks(links=(_integrity([[1, 0, "a"]], [[1, 0]]),)).failures() == []


def test_the_decoded_columns_give_the_shares_the_row_path_gives() -> None:
    from flux_engine.concordance.decoded import DecodedRows
    from flux_engine.concordance.integrity import key_columns, link_integrity_columns

    link = Relationship(
        "owner", "child", "parent", ("fk", "name"), ("pk", "name"), "one_to_one", "inner"
    )
    child = [[1, "a"], [1, "a"], [2, "b"], [9, "c"], [None, "d"], [3, "zz"]]
    parent = [[1, "a"], [2, "b"], [3, "c"], [3, "c"], [None, "e"]]
    types = {"fk": "INT", "pk": "INT", "name": "STRING"}
    by_rows = link_integrity(link, ["fk", "name"], child, ["pk", "name"], parent)
    c = key_columns(DecodedRows(["fk", "name"], child), ["fk", "name"], types)
    p = key_columns(DecodedRows(["pk", "name"], parent), ["pk", "name"], types)
    assert c is not None and p is not None
    by_columns = link_integrity_columns(link, c, p)
    assert by_columns is not None
    assert by_columns.to_document() == by_rows.to_document()


def test_a_key_the_decoded_reader_does_not_take_is_left_to_the_row_path() -> None:
    from flux_engine.concordance.decoded import DecodedRows
    from flux_engine.concordance.integrity import key_columns

    table = DecodedRows(["d"], [["2026-01-01"]])
    assert key_columns(table, ["d"], {"d": "DATE"}) is None
    assert key_columns([{"d": 1}], ["d"], {"d": "INT"}) is None
    # a string that looks like a number is read as a string, never as a number
    both = key_columns(DecodedRows(["s"], [["007"], ["7"]]), ["s"], {"s": "STRING"})
    assert both is not None and both.alphabets[0] == ("007", "7")


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        ([-1, 0, 1, 2], [-1, 1, 2, 2]),
        ([-5, -3], [-5, -3, -4]),
        ([0, 10**13], [0, 10**13, 10**13, 7]),
        ([-(10**13), 10**13, 5], [5, -(10**13), 10**13 + 1]),
    ],
    ids=["negative", "all negative", "sparse 1e13", "both ends"],
)
def test_the_decoded_columns_give_the_row_shares_for_negative_and_sparse_keys(
    parent: List[int], child: List[int]
) -> None:
    from flux_engine.concordance.decoded import DecodedRows
    from flux_engine.concordance.integrity import key_columns, link_integrity_columns

    link = Relationship("r", "child", "parent", ("pid",), ("id",), "many_to_one", "inner")
    types = {"pid": "INT", "id": "INT"}
    by_rows = link_integrity(link, ["pid"], [[v] for v in child], ["id"], [[v] for v in parent])
    c = key_columns(DecodedRows(["pid"], [[v] for v in child]), ["pid"], types)
    p = key_columns(DecodedRows(["id"], [[v] for v in parent]), ["id"], types)
    assert c is not None and p is not None
    by_columns = link_integrity_columns(link, c, p)
    assert by_columns is not None
    assert by_columns.to_document() == by_rows.to_document()


def test_a_table_of_no_rows_has_empty_columns_and_a_clean_relationship() -> None:
    from flux_engine.concordance.decoded import DecodedRows
    from flux_engine.concordance.integrity import key_columns, link_integrity_columns

    empty = DecodedRows(["fk", "name"], [])
    assert empty.cells("fk") == () and empty.cells("name") == ()
    link = Relationship("r", "child", "parent", ("fk",), ("pk",), "many_to_one", "inner")
    types = {"fk": "INT", "pk": "INT"}
    for child_rows, parent_rows in (([], [[1], [2]]), ([[1]], []), ([], [])):
        by_rows = link_integrity(link, ["fk"], child_rows, ["pk"], parent_rows)
        c = key_columns(DecodedRows(["fk"], child_rows), ["fk"], types)
        p = key_columns(DecodedRows(["pk"], parent_rows), ["pk"], types)
        assert c is not None and p is not None
        by_columns = link_integrity_columns(link, c, p)
        assert by_columns is not None
        assert by_columns.to_document() == by_rows.to_document()
