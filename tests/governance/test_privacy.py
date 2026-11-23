# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column's sensitivity sets the minimum grade of the run (ADR 0020), and a bundle only raises it."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.grades import Pattern
from flux_engine.governance.privacy import (
    BUILT_IN_FLOORS,
    FLX_4005,
    SENSITIVITY_LABELS,
    GradeBelowMinimum,
    effective_floors,
    enforce_minimum_grades,
    read_floors,
    sensitivities_of,
)
from flux_engine.seam.resolve import FLX_3105

pytestmark = pytest.mark.unit


def test_the_labels_are_fluids_twelve() -> None:
    assert len(SENSITIVITY_LABELS) == 12 and len(set(SENSITIVITY_LABELS)) == 12
    assert SENSITIVITY_LABELS[:4] == ("none", "internal", "confidential", "restricted")


def test_the_built_in_floors_are_the_ones_the_adr_states() -> None:
    for label in ("pii", "phi", "restricted", "pseudonymized", "tokenized"):
        assert BUILT_IN_FLOORS[label] is Pattern.P3
    for label in ("confidential", "internal"):
        assert BUILT_IN_FLOORS[label] is Pattern.P1
    for label in ("none", "cleartext", "treated", "anonymized", "encrypted"):
        assert label not in BUILT_IN_FLOORS


COLUMNS = [("shop", "a"), ("shop", "b"), ("shop", "c"), ("shop", "d")]


@pytest.mark.parametrize(
    ("label", "pattern", "refused"),
    [
        ("pii", Pattern.P1, True),
        ("pii", Pattern.P3, False),
        ("pii", Pattern.P4, False),
        ("pii", Pattern.P0, False),
        ("phi", Pattern.P1, True),
        ("restricted", Pattern.P1, True),
        ("pseudonymized", Pattern.P1, True),
        ("tokenized", Pattern.P1, True),
        ("confidential", Pattern.P1, False),
        ("internal", Pattern.P1, False),
        ("none", Pattern.P1, False),
        ("cleartext", Pattern.P1, False),
        ("treated", Pattern.P1, False),
        ("anonymized", Pattern.P1, False),
        ("encrypted", Pattern.P1, False),
    ],
)
def test_the_gate_refuses_a_run_below_the_floor_of_a_labelled_column(
    label: str, pattern: Pattern, refused: bool
) -> None:
    labels = {("shop", "c"): label}
    floors = effective_floors()
    if refused:
        with pytest.raises(GradeBelowMinimum) as error:
            enforce_minimum_grades(pattern, labels, floors, COLUMNS)
        assert error.value.code is FLX_4005
        assert "shop.c is " + label in str(error.value)
        assert "the run's grade is P1" in str(error.value)
        (found,) = error.value.shortfalls
        assert (found.minimum, found.grade) == (BUILT_IN_FLOORS[label], Pattern.P1)
    else:
        enforce_minimum_grades(pattern, labels, floors, COLUMNS)


def test_every_labelled_column_below_the_floor_is_named_and_an_unlabelled_one_is_not() -> None:
    labels = {("shop", "a"): "pii", ("shop", "c"): "phi", ("shop", "b"): "internal"}
    with pytest.raises(GradeBelowMinimum) as error:
        enforce_minimum_grades(Pattern.P1, labels, effective_floors(), COLUMNS)
    assert [item.column for item in error.value.shortfalls] == ["a", "c"]
    assert "shop.b" not in str(error.value) and "shop.d" not in str(error.value)


def test_a_labelled_column_the_run_does_not_emit_is_refused_and_not_skipped() -> None:
    with pytest.raises(FluxError, match="not a column the run emits") as error:
        enforce_minimum_grades(Pattern.P3, {("other", "x"): "pii"}, effective_floors(), COLUMNS)
    assert error.value.code is FLX_4005


def test_a_bundle_raises_a_floor() -> None:
    raised = read_floors(
        {"minimumGrade": {"confidential": "P3", "cleartext": "P1"}}, where="w", code=FLX_3105
    )
    floors = effective_floors(raised)
    assert floors["confidential"] is Pattern.P3 and floors["cleartext"] is Pattern.P1
    assert floors["pii"] is Pattern.P3
    with pytest.raises(GradeBelowMinimum):
        enforce_minimum_grades(Pattern.P1, {("shop", "c"): "confidential"}, floors, COLUMNS)
    # P4 is above P3, and P0 above both: a bundle can ask for a model, or for no data at all
    assert effective_floors({"pii": Pattern.P4})["pii"] is Pattern.P4
    with pytest.raises(GradeBelowMinimum):
        enforce_minimum_grades(
            Pattern.P3, {("shop", "c"): "pii"}, effective_floors({"pii": Pattern.P0}), COLUMNS
        )


@pytest.mark.parametrize(
    "block",
    [
        {"minimumGrade": {"pii": "P1"}},
        {"minimumGrade": {"phi": "P1"}},
        {"minimumGrade": {"restricted": "P1"}},
        {"minimumGrade": {"tokenized": "P1"}},
        {"minimumGrade": {"pseudonymized": "P1"}},
    ],
)
def test_a_bundle_cannot_lower_a_floor(block: Dict[str, Any]) -> None:
    with pytest.raises(FluxError, match="may raise a floor and never lower one") as error:
        read_floors(block, where="w", code=FLX_3105)
    assert error.value.code is FLX_3105


def test_a_floor_equal_to_the_built_in_one_is_accepted() -> None:
    assert read_floors({"minimumGrade": {"pii": "P3"}}, where="w", code=FLX_3105) == {
        "pii": Pattern.P3
    }
    assert (
        effective_floors(read_floors({"minimumGrade": {"pii": "P3"}}, where="w", code=FLX_3105))[
            "pii"
        ]
        is Pattern.P3
    )


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("P3", "is a mapping"),
        ({"floors": {}}, "carries only minimumGrade"),
        ({"minimumGrade": ["pii"]}, "maps a sensitivity to a pattern"),
        ({"minimumGrade": {"secret": "P3"}}, "not one of"),
        ({"minimumGrade": {"pii": "P2"}}, "P2 is not assigned"),
        ({"minimumGrade": {"pii": "p3"}}, "is a pattern"),
        ({"minimumGrade": {"pii": 3}}, "is a pattern"),
    ],
)
def test_a_malformed_block_is_refused(block: Any, message: str) -> None:
    with pytest.raises(FluxError, match=message) as error:
        read_floors(block, where="w", code=FLX_3105)
    assert error.value.code is FLX_3105


def test_no_block_raises_nothing() -> None:
    assert read_floors(None, where="w", code=FLX_3105) == {}
    assert read_floors({"minimumGrade": None}, where="w", code=FLX_3105) == {}


def test_the_labels_are_read_off_the_contract_columns() -> None:
    exposes = [
        (
            "shop",
            {
                "contract": {
                    "schema": [
                        {"name": "id", "type": "INT"},
                        {"name": "email", "type": "STRING", "sensitivity": "pii"},
                        {"name": "note", "type": "STRING", "sensitivity": "made-up"},
                        {"name": "plan", "type": "STRING", "sensitivity": "internal"},
                    ]
                }
            },
        )
    ]
    assert sensitivities_of(exposes) == {("shop", "email"): "pii", ("shop", "plan"): "internal"}
