# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A lifetime that orders two dates of a row keeps its end inside the range its contract declares.

ADR 0024, amendment of 2026-11-26. ``init`` turns a check such as ``returned_on IS NULL OR
returned_on >= borrowed_on`` into a lifetime whose gap is drawn evenly over the run's window. The
generator cuts that gap so that the end stays inside the end column's declared range, but a column
the profile gives no distribution declares none, so nothing was cut and a wide window drew most
ends far outside the range the contract holds the column to. ``init`` now declares the contract's
range on such an end, and bounds the gap by it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

from flux_engine.cli._init import run_init
from tests.author.test_init_date_order import SCHEMA, _binned, _block, _notes, _rows

pytestmark = pytest.mark.integration

#: A wide window, as a release that publishes no extremes gets: far more than the data spans.
WIDE: Tuple[str, str] = ("2000-01-01", "2100-01-01")
RANGE = "between '2020-01-01' and '2024-12-31'"

#: A lending library's loans: borrowed inside five years, and returned inside the same five.
LOAN = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: library.loans
name: Loans
metadata:
  owner:
    team: library-team
exposes:
  - exposeId: loan
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/loan.parquet }
    contract:
      schema:
        - { name: loan_id, type: INT, required: true }
        - name: borrowed_on
          type: DATE
          required: true
          validationRules: [{ type: range, constraint: "@RANGE@" }]
        - name: returned_on
          type: DATE
          validationRules: [{ type: range, constraint: "@RANGE@" }]
      invariants:
        - id: loan_returned_after_borrowed
          check: "returned_on IS NULL OR returned_on >= borrowed_on"
          mustBeLessOrEqualTo: 5
          unit: percent
    semantics:
      entities:
        - { name: loan, type: primary, expr: loan_id }
""".replace("@RANGE@", RANGE)

#: The same loans, each of which has been returned (the end is required, and the check plain).
RETURNED = LOAN.replace(
    "        - name: returned_on\n          type: DATE\n",
    "        - name: returned_on\n          type: DATE\n          required: true\n",
).replace("returned_on IS NULL OR returned_on >= borrowed_on", "returned_on >= borrowed_on")


def _profile(**columns: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A profile of the loans table; a date column given ``None`` publishes no statistic."""
    entry: Dict[str, Any] = {
        "rowCount": 600,
        "columns": {"loan_id": {"domain": "number"}},
    }
    for name in ("borrowed_on", "returned_on"):
        statistic = columns.get(name)
        entry["columns"][name] = {"domain": "timestamp"} | (
            {} if statistic is None else {"statistics": [statistic]}
        )
    return {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": SCHEMA,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"noisy": {}, "none": {}},
        "privacy": {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "0.5"},
            "privacyUnit": {"expose": "loan"},
        },
        "tables": {"loan": entry},
    }


def _init(tmp_path: Path, contract: str, profile: Dict[str, Any]) -> Any:
    (tmp_path / "c.fluid.yaml").write_text(contract, encoding="utf-8")
    (tmp_path / "c.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "c.fluid.yaml"),
        profile=str(tmp_path / "c.profile.json"),
        window=WIDE,
    )
    assert result.exit_code == 0, result.stderr
    return result


_END = ("emits", "loan", "fidelity", "columns", "returned_on")


def _end_column(tmp_path: Path) -> Dict[str, Any]:
    node: Any = _block(tmp_path)
    for key in _END:
        node = node[key]
    return dict(node)


def test_every_end_lands_inside_the_range_the_contract_declares(tmp_path: Path) -> None:
    _init(tmp_path, LOAN, _profile())
    (life,) = _block(tmp_path)["lifetimes"]
    assert (life["emit"], life["start"], life["end"]) == ("loan", "borrowed_on", "returned_on")
    # The end declares the contract's range, so the generator's own cut applies.
    assert _end_column(tmp_path)["range"] == {"min": "2020-01-01", "max": "2024-12-31"}
    rows = _rows(tmp_path, "loan")
    ends = [row[2] for row in rows if row[2] is not None]
    assert len(rows) == 600 and len(ends) > 500
    assert min(ends) >= "2020-01-01" and max(ends) <= "2024-12-31"
    assert not [row for row in rows if row[2] is not None and row[2] < row[1]]


def test_the_gap_is_bounded_by_the_range_not_the_window(tmp_path: Path) -> None:
    _init(tmp_path, LOAN, _profile())
    (life,) = _block(tmp_path)["lifetimes"]
    # From 2020-01-01, the earliest a loan starts, to 2024-12-31, the latest it may end.
    assert life["duration"]["bins"] == ["0", "1826"]
    entries = [item for item in _block(tmp_path)["parameters"] if "lifetimes" in item["pointer"]]
    assert "the declared range of 'returned_on' allows" in entries[0]["reason"]


def test_the_range_is_said_to_be_the_contracts(tmp_path: Path) -> None:
    result = _init(tmp_path, LOAN, _profile())
    assert "its range is the one its contract declares" in result.stdout
    assert "returned_on:" in result.stdout


def test_the_end_stays_open_where_the_check_lets_it_be_null(tmp_path: Path) -> None:
    # The check holds on a NULL end and the column is nullable: an end past the window is NULL, not kept.
    _init(tmp_path, LOAN, _profile())
    (life,) = _block(tmp_path)["lifetimes"]
    assert "endPastWindow" not in life


def test_a_required_end_is_ordered_and_held_inside_its_range(tmp_path: Path) -> None:
    # Before, no end range could be shown, so no lifetime was written and the check broke on about
    # half of the rows; now the contract's range is the one the gap is held to.
    _init(tmp_path, RETURNED, _profile())
    (life,) = _block(tmp_path)["lifetimes"]
    assert "endPastWindow" not in life and life["duration"]["bins"] == ["0", "1826"]
    rows = _rows(tmp_path, "loan")
    assert all(row[2] is not None and "2020-01-01" <= row[2] <= "2024-12-31" for row in rows)
    assert not [row for row in rows if row[2] < row[1]]


def test_a_strict_end_of_the_contract_range_is_not_reached(tmp_path: Path) -> None:
    strict = LOAN.replace(RANGE, ">= '2020-01-01' and < '2025-01-01'")
    _init(tmp_path, strict, _profile())
    assert _end_column(tmp_path)["range"] == {"min": "2020-01-01", "max": "2024-12-31"}


def test_an_end_the_contract_gives_no_closed_range_declares_none(tmp_path: Path) -> None:
    open_ended = LOAN.replace(RANGE, ">= '2020-01-01'")
    _init(tmp_path, open_ended, _profile())
    (life,) = _block(tmp_path)["lifetimes"]
    assert "range" not in _end_column(tmp_path)
    assert life["duration"]["bins"] == ["0", "36525"]


def test_a_range_with_no_room_after_the_earliest_start_writes_no_lifetime(tmp_path: Path) -> None:
    # Every loan is borrowed in 2023 or later, and a return must come by the end of 2021.
    contract = LOAN.replace(
        "          type: DATE\n          validationRules: [{ type: range, constraint: "
        f'"{RANGE}" }}]\n      invariants',
        "          type: DATE\n          validationRules: [{ type: range, constraint: "
        "\"between '2020-01-01' and '2021-12-31'\" }]\n      invariants",
    ).replace(
        f'          required: true\n          validationRules: [{{ type: range, constraint: "{RANGE}" }}]',
        "          required: true\n          validationRules: [{ type: range, constraint: "
        "\"between '2023-01-01' and '2024-12-31'\" }]",
    )
    assert contract != LOAN
    result = _init(tmp_path, contract, _profile())
    assert "lifetimes" not in _block(tmp_path)
    reason = _notes(result)["loan.loan_returned_after_borrowed"]
    assert "no lifetime is written" in reason and "too few to draw a gap in" in reason


def test_an_end_that_declares_a_span_keeps_the_gap_and_the_bytes_it_had(tmp_path: Path) -> None:
    # A published histogram gives the end a span of its own, which the generator already cuts to:
    # the lifetime keeps the window's gap and the column keeps the range its histogram gives.
    started = _binned(["2020-01-01", "2022-01-01", "2024-12-31"], [300, 300])
    ended = _binned(["2020-01-01", "2022-01-01", "2024-06-30"], [100, 500])
    _init(tmp_path, LOAN, _profile(borrowed_on=started, returned_on=ended))
    (life,) = _block(tmp_path)["lifetimes"]
    assert life["duration"]["bins"] == ["0", "36525"]
    column = _end_column(tmp_path)
    assert column["bins"][-1] == "2024-06-30" and column["range"]["max"] == "2024-06-30"
    rows = _rows(tmp_path, "loan")
    assert all(row[2] is None or "2020-01-01" <= row[2] <= "2024-12-31" for row in rows)
