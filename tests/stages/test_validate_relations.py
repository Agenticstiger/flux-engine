# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``validate`` refuses the counts and weights ``plan`` refuses, with the code ``plan`` gives.

A ``parentWhen`` that does not parse or names no column, a ``when`` or a ``weight`` on a counted
relationship, and a ``links`` weight that names a missing or a text column are all read when the
relationships are laid out, so ``validate`` has to read them there too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate

from .test_counts_run import bundle_with

pytestmark = pytest.mark.integration


def _with_links(target: Path, *entries: str) -> Path:
    path = target / "simulation.flux.yml"
    text = path.read_text("utf-8")
    anchor = "  com.agentics.flux/1:\n"
    assert text.count(anchor) == 1
    block = "    links:\n" + "".join(f"      - {{ {entry} }}\n" for entry in entries)
    path.write_text(text.replace(anchor, anchor + block), "utf-8")
    return target


def _plan_code(target: Path, tmp_path: Path) -> Optional[str]:
    try:
        result = plan(target, state_dir=tmp_path / "state")
    except FluxError as error:
        return error.code.code
    return None if result.success else result.error_code


def _agree(target: Path, tmp_path: Path) -> str:
    """The code both refuse with; the test fails when one of them accepts or the codes differ."""
    wanted = _plan_code(target, tmp_path)
    assert wanted is not None
    refused = validate(target)
    assert refused.exit_code == 1 and refused.error_code == wanted
    return refused.stderr


def test_a_parent_when_that_does_not_parse_is_refused_and_names_its_key(tmp_path: Path) -> None:
    message = _agree(bundle_with(tmp_path, "syntax", account_when="status <>"), tmp_path)
    assert "counts[invoice.account].parentWhen" in message


def test_a_parent_when_that_names_no_column_is_refused_and_names_its_key(tmp_path: Path) -> None:
    message = _agree(bundle_with(tmp_path, "column", account_when="no_such_col = 1"), tmp_path)
    assert "counts[invoice.account].parentWhen" in message


def test_a_when_on_a_counted_relationship_is_refused(tmp_path: Path) -> None:
    target = _with_links(
        bundle_with(tmp_path, "when"),
        "emit: invoice, relationship: account, when: \"issued_on > DATE '2026-08-01'\"",
    )
    _agree(target, tmp_path)


def test_a_weight_on_a_counted_relationship_is_refused(tmp_path: Path) -> None:
    target = _with_links(
        bundle_with(tmp_path, "weight"), "emit: invoice_line, relationship: invoice, weight: amount"
    )
    _agree(target, tmp_path)


@pytest.mark.parametrize("column", ["no_such_col", "category"])
def test_a_weight_naming_a_missing_or_text_column_is_refused(tmp_path: Path, column: str) -> None:
    target = _with_links(
        bundle_with(tmp_path, "weighted", counted=False),
        f"emit: invoice_line, relationship: product, weight: {column}",
    )
    _agree(target, tmp_path)


def test_a_valid_counted_bundle_still_validates(tmp_path: Path) -> None:
    target = bundle_with(tmp_path, "ok", account_when="status <> 'closed'")
    assert validate(target).exit_code == 0
