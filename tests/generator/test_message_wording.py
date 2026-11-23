# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Counts in a refusal take the singular when there is one, and a refusal names its lists as text."""

from __future__ import annotations

from decimal import Decimal

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator.counts import check_minimum
from flux_engine.seam.contract import LogicalKind
from flux_engine.stages.scaling import _scale_column

pytestmark = pytest.mark.unit


def test_one_child_row_and_one_eligible_parent_read_in_the_singular() -> None:
    with pytest.raises(FluxError) as caught:
        check_minimum(1, 1, 1)
        check_minimum(1, 1, 2)
    message = str(caught.value)
    assert "the 1 child row cannot give each of the 1 eligible parent 2 children" in message


def test_a_minimum_of_one_reads_one_child() -> None:
    with pytest.raises(FluxError) as caught:
        check_minimum(3, 4, 1)
    assert "the 3 child rows cannot give each of the 4 eligible parents 1 child (" in str(
        caught.value
    )


def test_one_bin_reads_in_the_singular() -> None:
    with pytest.raises(FluxError) as caught:
        _scale_column(
            "id",
            {"range": {"min": "1", "max": "100"}, "bins": ["1", "2"]},
            LogicalKind.INT,
            Decimal("0.3"),
        )
    message = str(caught.value)
    assert "fewer values than its 1 bin;" in message and "1 bins" not in message
