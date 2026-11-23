# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Refusals of a counted layout name what to change (ADR 0016 sections 4 and 5).

* A required parent aggregate with no ``ifNone`` over a layout that can leave a parent without
  children is refused at plan, naming the expose, the column and ``ifNone``, not at generate with a
  row missing a column.
* A ``parentWhen`` no parent meets names the child expose, the relationship and the condition.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import pipeline

from .conftest import REPO_ROOT

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "counts-invoices"
LINES_OLD = """        minChildren: 1
        children: { levels: ["1", "3", "6"], levelWeights: ["0.4", "0.4", "0.2"] }"""
MAX_QTY_OLD = "aggregate: { function: max, column: quantity, from: invoice_line, via: invoice }"
MAX_QTY_NEW = (
    "aggregate: { function: max, column: quantity, from: invoice_line, via: invoice, ifNone: 0 }"
)


def _bundle(
    tmp_path: Path,
    *,
    lines: Optional[str] = None,
    if_none: bool = False,
    closed: bool = False,
) -> Path:
    """A copy of ``tests/fixtures/counts-invoices`` with the lines' entry and the aggregate changed."""
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE, target)
    simulation = target / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    if lines is not None:
        assert text.count(LINES_OLD) == 1
        text = text.replace(LINES_OLD, lines)
    if closed:
        old = "status: { levels: { active: '0.8', closed: '0.2' }, allocation: exact }"
        assert text.count(old) == 1
        text = text.replace(old, "status: { levels: { closed: '1' }, allocation: exact }")
    simulation.write_text(text, "utf-8")
    if if_none:
        contract = target / "invoice-book.fluid.yml"
        body = contract.read_text("utf-8")
        assert body.count(MAX_QTY_OLD) == 1
        contract.write_text(body.replace(MAX_QTY_OLD, MAX_QTY_NEW), "utf-8")
        # The simulation pins the contract's digest, which the edit moved.
        text = simulation.read_text("utf-8")
        simulation.write_text(text.replace(FIXTURE_DIGEST, IF_NONE_DIGEST), "utf-8")
    return target


FIXTURE_DIGEST = "sha256:9ccad82cd26ee53e343403e2a30ac58f1204c528df72571540cf39bb6755652c"
IF_NONE_DIGEST = "sha256:3e63bacc161660e5ea44bbf2516a4c19cd3df3d07df251b04199331c7f7a9966"
CHILDLESS = """        minChildren: 0
        children: { levels: ["0", "1"], levelWeights: ["0.2", "0.8"] }"""


PARENT_WHEN_LINES = """        minChildren: 1
        parentWhen: "status <> 'void'"
        children: { levels: ["1", "3", "6"], levelWeights: ["0.4", "0.4", "0.2"] }"""


def _refused(target: Path) -> FluxError:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2)
    return caught.value


def test_a_required_aggregate_with_no_if_none_over_childless_parents_is_refused_at_plan(
    tmp_path: Path,
) -> None:
    error = _refused(_bundle(tmp_path, lines=CHILDLESS))
    text = str(error)
    assert error.code.code == "FLX-3403", text
    assert "invoice" in text and "max_qty" in text and "ifNone" in text


def test_a_parent_when_leaves_parents_childless_whatever_the_minimum_so_the_aggregate_is_refused(
    tmp_path: Path,
) -> None:
    error = _refused(_bundle(tmp_path, lines=PARENT_WHEN_LINES))
    text = str(error)
    assert error.code.code == "FLX-3403", text
    assert "invoice" in text and "max_qty" in text and "ifNone" in text


def test_an_if_none_lets_a_parent_when_layout_plan(tmp_path: Path) -> None:
    pipeline.execute(_bundle(tmp_path, lines=PARENT_WHEN_LINES, if_none=True), kernel=KERNEL_V2)


def test_a_layout_that_leaves_no_parent_childless_still_plans_without_if_none(
    tmp_path: Path,
) -> None:
    # The committed fixture gives every invoice at least one line.
    pipeline.execute(_bundle(tmp_path), kernel=KERNEL_V2)


def test_an_if_none_lets_a_layout_with_childless_parents_plan(tmp_path: Path) -> None:
    pipeline.execute(_bundle(tmp_path, lines=CHILDLESS, if_none=True), kernel=KERNEL_V2)


def test_a_parent_when_no_parent_meets_names_the_expose_relationship_and_condition(
    tmp_path: Path,
) -> None:
    error = _refused(_bundle(tmp_path, closed=True))
    text = str(error)
    assert error.code.code == "FLX-3403", text
    assert "no parent is eligible" in text
    assert "'invoice'" in text and "'account'" in text
    assert "parentWhen" in text and "status <> 'closed'" in text
