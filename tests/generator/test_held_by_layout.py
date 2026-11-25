# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Which dependencies a history's parent layout holds by construction (ADR 0017, amendment of 2026-11-25).

A history with a ``parent`` gives every version of an entity one parent row, so a dependency from
columns that include the natural entity to columns that are all foreign keys of the parent
relationship is true before it is read. :func:`~flux_engine.generator.rules._held_by_layout`
marks it ``by_layout``; this pins each clause on its own, so a clause that is dropped or inverted
moves a result:

* the ``to`` columns are all foreign keys of the parent relationship;
* the ``from`` columns include the natural entity;
* no ``to`` column declares a ``nullFraction`` (a column that may be NULL is not a function of the
  entity);
* an emit with no parent, no natural entity or more than one is left as it is.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence

import pytest

from flux_engine.generator.rules import FORM_DEPENDENCY, PlannedRule, _held_by_layout

FOREIGN = ("depot_id", "region_id")


def _rule(
    from_columns: Sequence[str], to_columns: Sequence[str], form: str = FORM_DEPENDENCY
) -> PlannedRule:
    named = {"from_columns": from_columns, "to_columns": to_columns, "foreign_columns": to_columns}
    return PlannedRule("r", form, "", **{field: tuple(columns) for field, columns in named.items()})


def _keys(naturals: Sequence[str] = ("vehicle_id",)) -> Any:
    return SimpleNamespace(
        relationships=[SimpleNamespace(name="depot", from_columns=FOREIGN)],
        entities=[SimpleNamespace(kind="natural", column=column) for column in naturals],
    )


def _held(
    rule: PlannedRule,
    *,
    nulls: Optional[Mapping[str, Any]] = None,
    keys: Any = None,
    parent: Optional[str] = "depot",
) -> bool:
    history = [
        SimpleNamespace(parent=None if parent is None else SimpleNamespace(relationship=parent))
    ]
    (result,) = _held_by_layout((rule,), history, _keys() if keys is None else keys, nulls or {})
    return result.by_layout


def test_a_dependency_from_the_entity_to_foreign_keys_is_held() -> None:
    assert _held(_rule(["vehicle_id"], ["depot_id"]))
    assert _held(_rule(["vehicle_id", "model"], ["depot_id", "region_id"]))


def test_a_to_column_that_declares_a_null_fraction_is_not_held() -> None:
    assert not _held(_rule(["vehicle_id"], ["depot_id"]), nulls={"depot_id": "0.1"})
    assert not _held(_rule(["vehicle_id"], ["depot_id", "region_id"]), nulls={"region_id": "0.1"})
    # A null fraction on a column the dependency does not set says nothing about it.
    assert _held(_rule(["vehicle_id"], ["depot_id"]), nulls={"model": "0.1"})


@pytest.mark.parametrize(
    "from_columns, to_columns",
    [
        (["model"], ["depot_id"]),
        (["vehicle_id"], ["depot_id", "model"]),
        (["vehicle_id"], ["model"]),
    ],
)
def test_a_dependency_that_is_not_a_function_of_the_entity_is_not_held(
    from_columns: Sequence[str], to_columns: Sequence[str]
) -> None:
    assert not _held(_rule(from_columns, to_columns))


def test_only_a_dependency_is_held() -> None:
    assert not _held(_rule(["vehicle_id"], ["depot_id"], form="check"))


@pytest.mark.parametrize(
    "keys, parent",
    [
        (_keys(()), "depot"),
        (_keys(("vehicle_id", "other_id")), "depot"),
        (None, "depot"),
        (_keys(), None),
        (_keys(), "elsewhere"),
    ],
)
def test_no_parent_no_single_natural_entity_or_no_such_relationship_holds_nothing(
    keys: Any, parent: Optional[str]
) -> None:
    history = [
        SimpleNamespace(parent=None if parent is None else SimpleNamespace(relationship=parent))
    ]
    rule = _rule(["vehicle_id"], ["depot_id"])
    assert _held_by_layout((rule,), history, keys, {}) == (rule,)
    assert not rule.by_layout
