# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` keeps the order a contract's ``check`` declares between two dates of a row.

ADR 0024, amendment of 2026-11-26. A check such as ``cancelled_on IS NULL OR cancelled_on >=
issued_on`` orders two dates of one row. A private release publishes no ``datePairs``, so each date
was drawn on its own and the check held on a share of the rows. With no published pair, ``init``
writes a lifetime (the form that orders two dates of one row) whose gap is drawn evenly over what
the run's window allows, as an assumption; a published pair is read as before.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.author.orders import contract_date_orders
from flux_engine.cli._init import run_init
from flux_engine.stages.run import run

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
SAMPLE = {"min": 900, "max": 1100}
SCHEMA = "https://agenticstiger.github.io/flux/engine/spec/profile/1-3-0/FluxProfile.json"
YEARS = list(range(2018, 2028))

#: A lending library's cards: a card is issued, and may be cancelled afterwards.
CARD = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: library.cards
name: Cards
metadata:
  owner:
    team: library-team
exposes:
  - exposeId: card
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/card.parquet }
    contract:
      schema:
        - { name: card_id, type: INT, required: true }
        - { name: issued_on, type: DATE, required: true }
        - { name: cancelled_on, type: DATE }
      invariants:
        - id: card_cancelled_after_issued
          check: "cancelled_on IS NULL OR cancelled_on >= issued_on"
          mustBeLessOrEqualTo: 5
          unit: percent
    semantics:
      entities:
        - { name: card, type: primary, expr: card_id }
"""

#: Its loans: a loan is borrowed, and may be returned afterwards.
LOAN = CARD.replace("library.cards", "library.loans").replace("card", "loan")
LOAN = (
    LOAN.replace("issued_on", "borrowed_at")
    .replace("cancelled_on", "returned_at")
    .replace("type: DATE", "type: TIMESTAMP")
    .replace("mustBeLessOrEqualTo: 5", "mustBeLessOrEqualTo: 10")
    .replace("loan_cancelled_after_issued", "loan_returned_after_borrowed")
)


def _ms(year: int) -> str:
    return str(int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000))


def _noisy(counts: List[str]) -> Dict[str, Any]:
    return {
        "definition": "marginalHistogram/1",
        "sampleSize": SAMPLE,
        "disclosure": "noisy",
        "bins": [_ms(year) for year in YEARS],
        "noise": {"mechanism": "gaussian", "scale": "2.5", "privacyLoss": {"rho": "0.1"}},
        "groups": [{"group": "", "status": "F", "noisyCounts": counts}],
    }


def _profile(table: str, first: str, second: str, pairs: bool = False) -> Dict[str, Any]:
    started = ["0", "0", "300", "500", "200", "0", "0", "0", "0"]
    ended = ["0", "0", "0", "0", "200", "400", "100", "0", "0"]
    entry: Dict[str, Any] = {
        "columns": {
            f"{table}_id": {"domain": "number"},
            first: {"domain": "timestamp", "statistics": [_noisy(started)]},
            second: {"domain": "timestamp", "statistics": [_noisy(ended)]},
        }
    }
    if pairs:
        entry["datePairs"] = [
            {
                "start": first,
                "end": second,
                "order": {
                    "definition": "levelHistogram/1",
                    "levels": ["after", "before", "same"],
                    "sampleSize": SAMPLE,
                    "disclosure": "none",
                    "groups": [
                        {"group": "", "k": 1000, "levelCounts": [900, 0, 100], "status": "F"}
                    ],
                },
                "statistics": [
                    {
                        "definition": "marginalHistogram/1",
                        "bins": ["0", "30", "365", "1830"],
                        "sampleSize": SAMPLE,
                        "disclosure": "none",
                        "groups": [
                            {"group": "", "k": 1000, "binCounts": [400, 400, 200], "status": "F"}
                        ],
                    }
                ],
            }
        ]
    return {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": SCHEMA,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"noisy": {}, "none": {}},
        "privacy": {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "0.5"},
            "privacyUnit": {"expose": table},
        },
        "tables": {table: entry},
    }


def _init(tmp_path: Path, contract: str, profile: Dict[str, Any]) -> Any:
    (tmp_path / "c.fluid.yaml").write_text(contract, encoding="utf-8")
    (tmp_path / "c.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "c.fluid.yaml"),
        profile=str(tmp_path / "c.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _block(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block


def _rows(tmp_path: Path, table: str) -> List[List[Any]]:
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "c.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr + outcome.stdout
    (stream,) = (tmp_path / "state").glob(f"runs/*/emits/{table}.ndjson")
    return [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines() if line]


def _holds(rows: List[List[Any]], strict: bool = False) -> float:
    ok = sum(r[2] is None or (r[2] > r[1] if strict else r[2] >= r[1]) for r in rows)
    return ok / len(rows)


def test_a_card_cancelled_before_issued_is_not_drawn(tmp_path: Path) -> None:
    _init(tmp_path, CARD, _profile("card", "issued_on", "cancelled_on"))
    (life,) = _block(tmp_path)["lifetimes"]
    assert (life["emit"], life["start"], life["end"]) == ("card", "issued_on", "cancelled_on")
    assert life["duration"]["bins"][0] == "0" and "target" not in life
    rows = _rows(tmp_path, "card")
    assert _holds(rows) == 1.0
    assert any(row[2] is None for row in rows) and any(row[2] is not None for row in rows)
    entries = [item for item in _block(tmp_path)["parameters"] if "lifetimes" in item["pointer"]]
    assert [item["source"] for item in entries] == ["assumption"]
    assert "card_cancelled_after_issued" in entries[0]["reason"]


def test_no_row_ends_before_it_starts_when_the_check_is_plain(tmp_path: Path) -> None:
    # An end every row holds, and a check with no NULL branch: each row's end is on or after its start.
    plain = CARD.replace(
        "{ name: cancelled_on, type: DATE }", "{ name: cancelled_on, type: DATE, required: true }"
    ).replace("cancelled_on IS NULL OR cancelled_on >= issued_on", "cancelled_on >= issued_on")
    assert plain != CARD
    _init(tmp_path, plain, _profile("card", "issued_on", "cancelled_on"))
    rows = _rows(tmp_path, "card")
    assert len(rows) > 100 and all(row[2] is not None for row in rows)
    assert not [row for row in rows if row[2] < row[1]]


def test_a_timestamp_pair_is_ordered_too(tmp_path: Path) -> None:
    _init(tmp_path, LOAN, _profile("loan", "borrowed_at", "returned_at"))
    (life,) = _block(tmp_path)["lifetimes"]
    assert (life["start"], life["end"]) == ("borrowed_at", "returned_at")
    assert _holds(_rows(tmp_path, "loan")) == 1.0


def test_a_strict_order_starts_the_gap_at_one_day(tmp_path: Path) -> None:
    contract = CARD.replace("cancelled_on >= issued_on", "cancelled_on > issued_on")
    _init(tmp_path, contract, _profile("card", "issued_on", "cancelled_on"))
    (life,) = _block(tmp_path)["lifetimes"]
    assert life["duration"]["bins"][0] == "1"
    assert _holds(_rows(tmp_path, "card"), strict=True) == 1.0


def test_a_published_date_pair_wins(tmp_path: Path) -> None:
    profile = _profile("card", "issued_on", "cancelled_on", pairs=True)
    plain = yaml.safe_load(CARD)
    del plain["exposes"][0]["contract"]["invariants"]
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    _init(tmp_path / "a", CARD, profile)
    _init(tmp_path / "b", yaml.safe_dump(plain), profile)
    with_check, without = _block(tmp_path / "a"), _block(tmp_path / "b")
    assert with_check["lifetimes"] == without["lifetimes"]
    assert with_check["emits"] == without["emits"]
    assert with_check["lifetimes"][0]["duration"]["bins"] == ["0", "30", "365", "1830"]


@pytest.mark.parametrize(
    "check",
    [
        "cancelled_on IS NULL OR cancelled_on >= ADD_DAYS(issued_on, 1)",
        "cancelled_on >= DATE '2020-01-01'",
        "cancelled_on <> issued_on",
        "cancelled_on IS NOT NULL OR cancelled_on >= issued_on",
    ],
)
def test_a_check_of_another_shape_writes_nothing(tmp_path: Path, check: str) -> None:
    contract = CARD.replace("cancelled_on IS NULL OR cancelled_on >= issued_on", check)
    _init(tmp_path, contract, _profile("card", "issued_on", "cancelled_on"))
    assert "lifetimes" not in _block(tmp_path)


def test_the_orders_are_read_from_the_tree() -> None:
    def orders(check: str) -> List[Any]:
        expose = {"contract": {"invariants": [{"id": "r", "check": check}]}}
        return contract_date_orders(expose)

    (found,) = orders("a IS NULL OR   b  >=  a")
    assert (found.start, found.end, found.strict) == ("a", "b", False)
    assert found.null_start_holds and not found.null_end_holds
    (found,) = orders("b IS NULL OR a < b")
    assert (found.start, found.end, found.strict, found.null_end_holds) == ("a", "b", True, True)
    assert orders("a >= b OR b >= a") == []


def test_an_order_that_cannot_be_written_is_reported(tmp_path: Path) -> None:
    # A rule of the contract sets the end, so no lifetime can draw it.
    document = yaml.safe_load(CARD)
    document["exposes"][0]["contract"]["invariants"].append(
        {"id": "cancelled_a_month_on", "derive": "cancelled_on", "expr": "ADD_DAYS(issued_on, 30)"}
    )
    result = _init(
        tmp_path, yaml.safe_dump(document), _profile("card", "issued_on", "cancelled_on")
    )
    assert "lifetimes" not in _block(tmp_path)
    notes = {item["subject"]: item["assumption"] for item in result.artifacts["notes"]}
    reason = notes["card.card_cancelled_after_issued"]
    assert "already sets 'cancelled_on'" in reason and "no lifetime is written" in reason
