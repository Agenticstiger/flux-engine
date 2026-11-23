# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``validate`` refuses an overlay that names a table or column the pinned contract lacks.

``plan`` checks an overlay against the profile it is handed (``FLX-3105``). ``validate`` has no
profile, but a profile describes the contract's tables and columns, so a name the contract does
not have is refused by ``validate`` too, with the same code, naming the overlay and the name.
A name the contract has and the profile lacks stays ``plan``'s to refuse.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.cli import main
from tests.overlays import ORDERS_PROFILE, declare_overlay, init_orders

pytestmark = pytest.mark.integration

_NO_TABLE = """overlay:
  name: ghost-table
  patch:
    tables:
      ghosts: {rowCount: 10}
"""
_NO_COLUMN = """overlay:
  name: ghost-column
  patch:
    tables:
      orders:
        columns:
          ghost: {metrics: {}}
"""
_GOOD = """overlay:
  name: double-orders
  patch:
    tables:
      orders: {rowCount: 1200}
"""


def _code(message: str) -> str:
    start = message.index("FLX-")
    return message[start : start + 8]


def _plan_args(bundle: Path, tmp_path: Path, profile: Path = ORDERS_PROFILE) -> "list[str]":
    return ["plan", str(bundle), "--profile", str(profile), "--state-dir", str(tmp_path / "state")]


@pytest.mark.parametrize(
    ("overlay", "name", "words"),
    [
        (_NO_TABLE, "ghost-table", ("'ghosts'",)),
        (_NO_COLUMN, "ghost-column", ("'orders'", "'ghost'")),
    ],
    ids=["table", "column"],
)
def test_an_overlay_naming_what_the_contract_lacks_is_refused_like_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    overlay: str,
    name: str,
    words: "tuple[str, ...]",
) -> None:
    bundle = declare_overlay(init_orders(tmp_path / "bundle"), overlay)
    capsys.readouterr()
    assert main(["validate", str(bundle)]) == 1
    validated = capsys.readouterr().err
    assert main(_plan_args(bundle, tmp_path)) == 1
    planned = capsys.readouterr().err
    assert _code(validated) == _code(planned) == "FLX-3105"
    assert name in validated
    for word in words:
        assert word in validated and word in planned


def test_a_correct_overlay_still_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = declare_overlay(init_orders(tmp_path / "bundle"), _GOOD)
    capsys.readouterr()
    assert main(["validate", str(bundle)]) == 0, capsys.readouterr().err


def test_a_name_the_profile_lacks_but_the_contract_has_passes_validate_and_plan_refuses_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The profile the bundle pins holds no `country` column; the contract does.
    document = json.loads(ORDERS_PROFILE.read_text("utf-8"))
    del document["tables"]["orders"]["columns"]["country"]
    profile = tmp_path / "narrow.profile.json"
    profile.write_text(json.dumps(document), "utf-8")
    overlay = _NO_COLUMN.replace("ghost-column", "country-only").replace("ghost:", "country:")
    bundle = declare_overlay(init_orders(tmp_path / "bundle", profile), overlay)
    capsys.readouterr()
    assert main(["validate", str(bundle)]) == 0, capsys.readouterr().err
    assert main(_plan_args(bundle, tmp_path, profile)) == 1
    message = capsys.readouterr().err
    assert _code(message) == "FLX-3105" and "'country'" in message
