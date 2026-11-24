# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copula column whose bin holds more grid values than an int64 can count (ADR 0035 decision 6).

A ``DECIMAL(38,10)`` column over ``0`` to ``6e8`` has 6e18 grid values in its one bin, more than
``2**62``. The column path's bisection added ``lo + hi`` in int64 and wrapped, so values fell below
the declared range and the run's rows depended on which path made them; a bin of ``2**63`` or more
values did not even build (``OverflowError`` from the widths array). Pinned here, end to end:
every value is inside the declared range, both paths draw the same rows, and a bin wider than
int64 validates and runs.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, List

import pytest

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator import columns
from flux_engine.stages import pipeline
from flux_engine.stages.validate import validate
from tests.generator.test_rules_over_links import _by_both_paths
from tests.seam.test_copula_block import COPULA, bundle_with

pytestmark = pytest.mark.integration


def _wide(tmp_path: Path, top: str, rows: int = 600) -> Path:
    bundle = bundle_with(tmp_path, COPULA, "wide")
    contract = bundle / "ledger.fluid.yml"
    text = contract.read_text("utf-8")
    assert text.count('"DECIMAL(12,2)"') == 1
    contract.write_text(text.replace('"DECIMAL(12,2)"', '"DECIMAL(38,10)"'), "utf-8")
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    old = (
        "              range: { min: '0', max: '1000' }\n"
        "              bins: ['0', '100', '400', '1000']\n"
        "              binWeights: ['0.5', '0.3', '0.2']\n"
    )
    assert text.count(old) == 1
    new = (
        f"              range: {{ min: '0', max: '{top}' }}\n"
        f"              bins: ['0', '{top}']\n"
        "              binWeights: ['1']\n"
    )
    text = text.replace(old, new).replace("        rows: 400\n", f"        rows: {rows}\n")
    simulation.write_text(text, "utf-8")
    return bundle


def _amounts(outcome: Any) -> List[Decimal]:
    # The fixture's columns are acct_id, amount, ...: the amount is the second.
    return [Decimal(str(row[1])) for row in outcome.emit("accounts").rows]


@pytest.mark.parametrize("chunk", [4096, 11])
def test_a_bin_of_more_than_two_to_the_62_values_emits_only_values_in_range_by_either_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    bundle = _wide(tmp_path, "600000000")

    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(bundle, kernel=KERNEL_V2)
    )

    amounts = _amounts(by_columns)
    assert taken and all(taken)
    assert amounts == _amounts(by_rows)
    assert all(Decimal(0) <= value <= Decimal(600000000) for value in amounts), min(amounts)
    assert max(amounts) > Decimal(300000000)


def test_a_bin_wider_than_int64_validates_and_runs(tmp_path: Path) -> None:
    bundle = _wide(tmp_path, "1300000000", rows=200)  # 1.3e19 grid values: above 2**63
    checked = validate(bundle)
    assert checked.success, checked.stderr
    amounts = _amounts(pipeline.execute(bundle, kernel=KERNEL_V2))
    assert len(amounts) == 200
    assert all(Decimal(0) <= value <= Decimal(1300000000) for value in amounts)
