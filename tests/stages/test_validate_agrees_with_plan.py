# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``validate`` refuses a bundle ``plan`` refuses on how its columns and tables are laid out.

An ``allocation: exact`` on a key, a ``nullFraction`` on a required column, and a table that declares
no rows are read when ``plan`` builds each emit's plan, so ``validate`` builds it too, with the same
checks and so the same code, and still draws no row.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from flux_engine.cli import main
from flux_engine.generator import rows as rows_module

from .test_counts_run import FIXTURE, bundle_with

SMALL = FIXTURE.parent / "telco-fidelity-small"

pytestmark = pytest.mark.integration


def _edited(tmp_path: Path, old: str, new: str, name: str = "book") -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    path = target / "simulation.flux.yml"
    text = path.read_text("utf-8")
    assert text.count(old) == 1
    path.write_text(text.replace(old, new), "utf-8")
    return target


def _both(bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> "tuple[str, str]":
    """The messages of ``validate`` and ``plan``; both exit 1."""
    capsys.readouterr()
    assert main(["validate", str(bundle)]) == 1
    validated = capsys.readouterr().err
    assert main(["plan", str(bundle), "--state-dir", str(tmp_path / "state")]) == 1
    return validated, capsys.readouterr().err


def _code(message: str) -> str:
    start = message.index("FLX-")
    return message[start : start + 8]


def test_an_exact_allocation_on_a_key_is_refused_by_validate_naming_the_column(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _edited(
        tmp_path,
        "invoice_id: { range: { min: '1', max: '1200' } }",
        "invoice_id: { range: { min: '1', max: '1200' }, allocation: exact }",
    )
    validated, planned = _both(bundle, tmp_path, capsys)
    assert _code(validated) == _code(planned) == "FLX-3402"
    assert "invoice_id: allocation exact is not allowed on a key" in validated


def test_a_null_fraction_on_a_required_column_is_refused_by_validate_naming_the_column(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _edited(
        tmp_path,
        "status: { levels: { paid: '0.7', open: '0.25', void: '0.05' } }",
        "status: { levels: { paid: '0.7', open: '0.25', void: '0.05' }, nullFraction: '0.1' }",
    )
    validated, planned = _both(bundle, tmp_path, capsys)
    assert _code(validated) == _code(planned) == "FLX-3402"
    assert "status: the contract marks this column required" in validated


def test_a_table_that_declares_zero_rows_is_refused_by_validate_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _edited(tmp_path, "        rows: 1200\n", "        rows: 0\n")
    validated, planned = _both(bundle, tmp_path, capsys)
    assert _code(validated) == _code(planned) == "FLX-3403"
    assert "'invoice'" in validated and "declares no rows" in validated


def test_too_few_child_rows_for_the_minimum_are_refused_by_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = bundle_with(tmp_path, "few", minimum=3, line_rows=100)
    validated, planned = _both(bundle, tmp_path, capsys)
    assert _code(validated) == _code(planned) == "FLX-3403"
    assert "N - m * e < 0" in validated


def test_validate_draws_no_row_of_a_bundle_that_declares_billions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    drawn = []

    def spy(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        drawn.append(args)
        raise AssertionError("validate drew rows")

    for name in ("generate_rows", "generate_row_slice", "generate_column"):
        monkeypatch.setattr(rows_module, name, spy)
    bundle = tmp_path / "small"
    shutil.copytree(SMALL, bundle)
    path = bundle / "simulation.flux.yml"
    path.write_text(path.read_text("utf-8").replace("rows: 64 ", "rows: 3000000000 "), "utf-8")
    capsys.readouterr()
    assert main(["validate", str(bundle)]) == 0, capsys.readouterr().err
    assert drawn == []
