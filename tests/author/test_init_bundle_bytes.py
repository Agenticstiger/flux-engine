# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bundle's bytes, and so its run id, follow the contract and the profile, not the profile's file name."""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import pytest

from flux_engine.cli._init import run_init
from flux_engine.stages.plan import plan

pytestmark = pytest.mark.integration

AUTHOR = Path(__file__).resolve().parents[1] / "fixtures" / "author"
RENAMES = (
    ("orders", "loans"),
    ("order_id", "loan_id"),
    ("amount", "late_fee"),
    ("created_at", "borrowed_at"),
)


def _library(text: str) -> str:
    for old, new in RENAMES:
        text = text.replace(old, new)
    return text


def _init(root: Path, directory: str, contract_name: str, profile_name: str) -> Path:
    where = root / directory
    where.mkdir()
    contract = where / contract_name
    profile = where / "profiles" / profile_name
    profile.parent.mkdir()
    contract.write_text(
        _library((AUTHOR / "orders.fluid.yaml").read_text(encoding="utf-8")), encoding="utf-8"
    )
    profile.write_text(
        _library((AUTHOR / "orders.profile.json").read_text(encoding="utf-8")), encoding="utf-8"
    )
    result = run_init(str(where / "bundle"), contract=str(contract), profile=str(profile))
    assert result.exit_code == 0, result.stderr
    return where / "bundle"


def _bytes(bundle: Path) -> Dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(bundle.iterdir()) if path.is_file()}


def test_the_same_contract_and_profile_under_another_profile_name_write_the_same_bytes(
    tmp_path: Path,
) -> None:
    first = _init(tmp_path, "one", "loans.fluid.yaml", "branch-a.profile.json")
    second = _init(tmp_path, "two", "loans.fluid.yaml", "a-different-name.json")
    assert _bytes(first) == _bytes(second)


def test_the_run_id_does_not_move_with_the_profile_file_name(tmp_path: Path) -> None:
    first = _init(tmp_path, "one", "loans.fluid.yaml", "branch-a.profile.json")
    second = _init(tmp_path, "two", "loans.fluid.yaml", "a-different-name.json")
    planned = []
    for bundle in (first, second):
        found = plan(
            bundle,
            state_dir=bundle.parent / "state",
            profile=next((bundle.parent / "profiles").iterdir()),
            kernel="0.2.0",
        )
        assert found.success, found.stderr
        planned.append(found.artifacts)
    assert planned[0]["run_id"] == planned[1]["run_id"]
