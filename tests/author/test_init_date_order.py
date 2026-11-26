# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` keeps the order a contract's ``check`` declares between two dates of a row.

ADR 0024, amendment of 2026-11-26. A check such as ``cancelled_on IS NULL OR cancelled_on >=
issued_on`` orders two dates of one row. A private release publishes no ``datePairs``, so each date
was drawn on its own and the check held on a share of the rows. With no published pair, ``init``
estimates from the two histograms the dates are drawn from how many rows would break the order if
they were drawn apart. Within the check's tolerance, both histograms stay. Above it, or where a date
has no histogram, ``init`` writes a lifetime (the form that orders two dates of one row) whose gap
is drawn evenly over what the run's window allows, as an assumption; a published pair is read as
before.
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

from ._written import emitted_rows

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


def _plain(counts: List[str]) -> Dict[str, Any]:
    return {
        "definition": "marginalHistogram/1",
        "sampleSize": SAMPLE,
        "disclosure": "none",
        "bins": [_ms(year) for year in YEARS],
        "groups": [
            {
                "group": "",
                "k": sum(int(n) for n in counts),
                "status": "F",
                "binCounts": [int(n) for n in counts],
            }
        ],
    }


#: Cards issued in 2020 to 2022; with these ends, drawn apart a card ends before it starts on 2.9%.
STARTED = ["0", "0", "300", "500", "200", "0", "0", "0", "0"]
ENDED = ["0", "0", "0", "0", "200", "400", "100", "0", "0"]
#: Ends spread as the starts are: drawn apart, about half the cards end before they start.
OVERLAPPING = STARTED


def _profile(
    table: str,
    first: str,
    second: str,
    pairs: bool = False,
    *,
    ended: List[str] = ENDED,
    plain: bool = False,
    rows: int = 0,
    bare: str = "",
) -> Dict[str, Any]:
    """A profile of one table; *bare* names the date column that publishes no statistic."""
    make = _plain if plain else _noisy
    entry: Dict[str, Any] = {
        "columns": {
            f"{table}_id": {"domain": "number"},
            first: {"domain": "timestamp", "statistics": [make(STARTED)]},
            second: {"domain": "timestamp", "statistics": [make(ended)]},
        }
    }
    if rows:
        entry["rowCount"] = rows
    if bare:
        entry["columns"][bare] = {"domain": "timestamp"}
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
    return emitted_rows(tmp_path, table, "c.profile.json", verify=True)


def _notes(result: Any) -> Dict[str, str]:
    return {item["subject"]: item["assumption"] for item in result.artifacts["notes"]}


def _holds(rows: List[List[Any]], strict: bool = False) -> float:
    ok = sum(r[2] is None or (r[2] > r[1] if strict else r[2] >= r[1]) for r in rows)
    return ok / len(rows)


def test_a_card_cancelled_before_issued_is_not_drawn(tmp_path: Path) -> None:
    # Ends spread as the starts are: drawn apart, half the cards end before they start, far above 5%.
    result = _init(tmp_path, CARD, _profile("card", "issued_on", "cancelled_on", ended=OVERLAPPING))
    (life,) = _block(tmp_path)["lifetimes"]
    assert (life["emit"], life["start"], life["end"]) == ("card", "issued_on", "cancelled_on")
    assert life["duration"]["bins"][0] == "0" and "target" not in life
    rows = _rows(tmp_path, "card")
    assert _holds(rows) == 1.0
    assert any(row[2] is None for row in rows) and any(row[2] is not None for row in rows)
    entries = [item for item in _block(tmp_path)["parameters"] if "lifetimes" in item["pointer"]]
    assert [item["source"] for item in entries] == ["assumption"]
    assert "card_cancelled_after_issued" in entries[0]["reason"]
    note = _notes(result)["card.card_cancelled_after_issued"]
    assert "histogram of 'cancelled_on' is traded for a lifetime" in note
    assert "tolerance of 5%" in note and "on about 49.9% of the rows" in note


def test_no_row_ends_before_it_starts_when_the_check_is_plain(tmp_path: Path) -> None:
    # An end every row holds, and a check with no NULL branch: each row's end is on or after its start.
    plain = CARD.replace(
        "{ name: cancelled_on, type: DATE }", "{ name: cancelled_on, type: DATE, required: true }"
    ).replace("cancelled_on IS NULL OR cancelled_on >= issued_on", "cancelled_on >= issued_on")
    assert plain != CARD
    _init(tmp_path, plain, _profile("card", "issued_on", "cancelled_on", ended=OVERLAPPING))
    rows = _rows(tmp_path, "card")
    assert len(rows) > 100 and all(row[2] is not None for row in rows)
    assert not [row for row in rows if row[2] < row[1]]


def test_a_timestamp_pair_is_ordered_too(tmp_path: Path) -> None:
    _init(tmp_path, LOAN, _profile("loan", "borrowed_at", "returned_at", ended=OVERLAPPING))
    (life,) = _block(tmp_path)["lifetimes"]
    assert (life["start"], life["end"]) == ("borrowed_at", "returned_at")
    assert _holds(_rows(tmp_path, "loan")) == 1.0


def test_a_strict_order_starts_the_gap_at_one_day(tmp_path: Path) -> None:
    contract = CARD.replace("cancelled_on >= issued_on", "cancelled_on > issued_on")
    _init(tmp_path, contract, _profile("card", "issued_on", "cancelled_on", ended=OVERLAPPING))
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
    # A guard for each end, nested to the left as the parser reads `x OR y OR z`, or to the right.
    for check in ("a IS NULL OR b IS NULL OR b >= a", "b IS NULL OR (a IS NULL OR b >= a)"):
        (found,) = orders(check)
        assert (found.start, found.end) == ("a", "b")
        assert found.null_start_holds and found.null_end_holds
    assert orders("b >= a OR b IS NULL") == []
    assert orders(" OR ".join(["c IS NULL"] * 3000) + " OR b >= a") == []


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
    reason = _notes(result)["card.card_cancelled_after_issued"]
    assert "already sets 'cancelled_on'" in reason and "no lifetime is written" in reason


# -- dates the published histograms already keep in order --------------------------------------


def _plain_card(
    tmp_path: Path, check: str = "cancelled_on >= issued_on", band: str = "", rows: int = 20000
) -> Any:
    """Cards both dates of which are required, from plain histograms of *rows* rows."""
    contract = (
        CARD.replace(
            "{ name: cancelled_on, type: DATE }",
            "{ name: cancelled_on, type: DATE, required: true }",
        )
        .replace("cancelled_on IS NULL OR cancelled_on >= issued_on", check)
        .replace(
            "mustBeLessOrEqualTo: 5\n          unit: percent",
            band or "mustBeLessOrEqualTo: 5\n          unit: percent",
        )
    )
    return _init(
        tmp_path, contract, _profile("card", "issued_on", "cancelled_on", plain=True, rows=rows)
    )


def test_dates_drawn_apart_that_keep_the_check_within_its_tolerance_keep_their_histograms(
    tmp_path: Path,
) -> None:
    result = _plain_card(tmp_path)
    block = _block(tmp_path)
    assert "lifetimes" not in block
    (note,) = (text for subject, text in _notes(result).items() if subject.startswith("card."))
    assert "'issued_on' and 'cancelled_on'" in note
    assert "tolerance of 5%" in note and "on about 2.8% of the rows" in note
    # The end is drawn from its own histogram: its cells come from 2022 to 2024 only.
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "c.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr + outcome.stdout
    (stream,) = (tmp_path / "state").glob("runs/*/emits/card.ndjson")
    rows = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 20000
    assert 0 < len([row for row in rows if row[2] < row[1]]) <= 0.05 * len(rows)
    assert (
        min(row[2] for row in rows) >= "2022-01-01" and max(row[2] for row in rows) < "2025-01-01"
    )


@pytest.mark.parametrize(
    ("band", "written"),
    [
        ("mustBeLessOrEqualTo: 5\n          unit: percent", False),
        ("mustBeLessThan: 5\n          unit: percent", False),
        ("mustBeBetween: [0, 5]\n          unit: percent", False),
        ("mustBeLessOrEqualTo: 2\n          unit: percent", True),
        ("mustBeLessThan: 2\n          unit: percent", True),
        ("mustBeLessOrEqualTo: 0\n          unit: percent", True),
        ("mustBeLessOrEqualTo: 50\n          unit: rows", True),
        ("mustBe: 3\n          unit: percent", True),
    ],
)
def test_the_lifetime_is_written_only_where_the_checks_tolerance_is_below_the_share(
    tmp_path: Path, band: str, written: bool
) -> None:
    # The same dates, drawn apart, break the order on about 2.8% of the rows.
    result = _plain_card(tmp_path, band=band)
    assert ("lifetimes" in _block(tmp_path)) is written
    (note,) = (text for subject, text in _notes(result).items() if subject.startswith("card."))
    assert ("is traded for a lifetime" in note) is written
    assert "on about 2.8% of the rows" in note


def test_a_check_that_declares_no_tolerance_counts_as_none(tmp_path: Path) -> None:
    contract = CARD.replace("          mustBeLessOrEqualTo: 5\n          unit: percent\n", "")
    assert contract != CARD
    result = _init(tmp_path, contract, _profile("card", "issued_on", "cancelled_on"))
    assert "lifetimes" in _block(tmp_path)
    assert "tolerance of 0%" in _notes(result)["card.card_cancelled_after_issued"]


def test_dates_that_cannot_break_the_order_need_no_lifetime(tmp_path: Path) -> None:
    # Every end is later than every start: a check with no tolerance at all is kept as it is.
    contract = CARD.replace("          mustBeLessOrEqualTo: 5\n          unit: percent\n", "")
    later = ["0", "0", "0", "0", "0", "0", "300", "500", "200"]
    result = _init(tmp_path, contract, _profile("card", "issued_on", "cancelled_on", ended=later))
    assert "lifetimes" not in _block(tmp_path)
    assert "on about 0% of the rows" in _notes(result)["card.card_cancelled_after_issued"]


def test_a_strict_check_counts_the_rows_that_end_the_day_they_start(tmp_path: Path) -> None:
    # The lending library's cards are dated by the day: independent ends tie a start on some days.
    loose = _plain_card(tmp_path)
    strict_dir = tmp_path / "strict"
    strict_dir.mkdir()
    strict = _plain_card(strict_dir, check="cancelled_on > issued_on")
    share = lambda result: next(  # noqa: E731
        text for subject, text in _notes(result).items() if subject.startswith("card.")
    )
    assert "about 2.8%" in share(loose) and "about 2.9%" in share(strict)


# -- dates with no histogram to be drawn apart from --------------------------------------------


def test_an_end_the_profile_gives_no_distribution_is_ordered_by_a_lifetime(tmp_path: Path) -> None:
    result = _init(
        tmp_path, CARD, _profile("card", "issued_on", "cancelled_on", bare="cancelled_on")
    )
    (life,) = _block(tmp_path)["lifetimes"]
    assert (life["start"], life["end"]) == ("issued_on", "cancelled_on")
    assert "traded" not in _notes(result).get("card.card_cancelled_after_issued", "")


def test_an_end_no_bin_of_which_stands_clear_of_the_noise_is_ordered_by_a_lifetime(
    tmp_path: Path,
) -> None:
    faint = ["1", "0", "2", "1", "0", "1", "2", "0", "1"]
    _init(tmp_path, CARD, _profile("card", "issued_on", "cancelled_on", ended=faint))
    (life,) = _block(tmp_path)["lifetimes"]
    assert (life["start"], life["end"]) == ("issued_on", "cancelled_on")


def test_a_start_the_profile_gives_no_distribution_is_ordered_by_a_lifetime(tmp_path: Path) -> None:
    _init(tmp_path, CARD, _profile("card", "issued_on", "cancelled_on", bare="issued_on"))
    (life,) = _block(tmp_path)["lifetimes"]
    assert (life["start"], life["end"]) == ("issued_on", "cancelled_on")


# -- one date, two checks: a kept check's histograms are not replaced by another check's lifetime ------

#: Cards that are issued, renewed and cancelled, both checks of which say cancelled comes last.
_TWO_CHECKS = (
    CARD.replace(
        "{ name: cancelled_on, type: DATE }",
        "{ name: cancelled_on, type: DATE, required: true }\n"
        "        - { name: renewed_on, type: DATE, required: true }",
    )
    .replace("cancelled_on IS NULL OR cancelled_on >= issued_on", "cancelled_on >= issued_on")
    .replace(
        "    semantics:",
        '        - id: cancelled_after_renewed\n          check: "cancelled_on >= renewed_on"\n'
        "    semantics:",
    )
)

_AFTER_ISSUED = (
    "        - id: card_cancelled_after_issued\n"
    '          check: "cancelled_on >= issued_on"\n'
    "          mustBeLessOrEqualTo: 5\n"
    "          unit: percent\n"
)
_AFTER_RENEWED = (
    '        - id: cancelled_after_renewed\n          check: "cancelled_on >= renewed_on"\n'
)


def _renewing_profile(first: str, **keep: Any) -> Dict[str, Any]:
    profile = _profile("card", first, "cancelled_on", plain=True, rows=20000, **keep)
    other = "renewed_on" if first == "issued_on" else "issued_on"
    profile["tables"]["card"]["columns"][other] = {
        "domain": "timestamp",
        "statistics": [_plain(STARTED)],
    }
    return profile


def _swapped(contract: str, first: str = _AFTER_ISSUED, second: str = _AFTER_RENEWED) -> str:
    """The same contract with its two checks listed the other way round."""
    swapped = contract.replace(first, "@@A@@").replace(second, "@@B@@")
    assert "@@A@@" in swapped and "@@B@@" in swapped
    return swapped.replace("@@A@@", second).replace("@@B@@", first)


def _bundle_without_the_contract_digest(folder: Path) -> Dict[str, bytes]:
    """The World, the Persona and the Simulation, but for the digest of the contract's own text."""
    found = {}
    for path in sorted((folder / "bundle").glob("*.flux.yml")):
        lines = path.read_bytes().splitlines()
        found[path.name] = b"\n".join(line for line in lines if b"contractDigest" not in line)
    assert len(found) == 3
    return found


def test_a_later_check_writes_no_lifetime_that_replaces_a_kept_checks_histogram(
    tmp_path: Path,
) -> None:
    # Dates drawn apart break the first check on 2.8% of the rows, within its band of 5%: both
    # histograms stay. The second check has no band and the same start histogram, so its lifetime
    # would end on 'cancelled_on' and replace the histogram the first check was kept on.
    result = _init(tmp_path, _TWO_CHECKS, _renewing_profile("issued_on"))
    assert "lifetimes" not in _block(tmp_path)
    notes = _notes(result)
    assert "within its tolerance of 5%" in notes["card.card_cancelled_after_issued"]
    refused = notes["card.cancelled_after_renewed"]
    assert "no lifetime is written for it" in refused
    assert "'card_cancelled_after_issued'" in refused and "published histogram" in refused
    assert "traded" not in refused
    rows = _rows(tmp_path, "card")  # the run verifies: the kept check holds within its band
    assert 0 < len([row for row in rows if row[2] < row[1]]) <= 0.05 * len(rows)


def test_the_listing_of_the_checks_decides_nothing(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    one = _init(first, _TWO_CHECKS, _renewing_profile("issued_on"))
    two = _init(second, _swapped(_TWO_CHECKS), _renewing_profile("issued_on"))
    assert _bundle_without_the_contract_digest(first) == _bundle_without_the_contract_digest(second)
    assert _notes(one) == _notes(two)
    assert "lifetimes" not in _block(second)


def test_of_two_checks_that_each_need_a_lifetime_the_first_by_id_writes_it(tmp_path: Path) -> None:
    # Neither check has a band, and both starts are drawn over the same years as 'cancelled_on'.
    bare = (
        '        - id: card_cancelled_after_issued\n          check: "cancelled_on >= issued_on"\n'
    )
    contract = _TWO_CHECKS.replace(_AFTER_ISSUED, bare)
    assert contract != _TWO_CHECKS
    results = []
    for index, text in enumerate((contract, _swapped(contract, bare))):
        folder = tmp_path / str(index)
        folder.mkdir()
        results.append(_init(folder, text, _renewing_profile("issued_on")))
        (life,) = _block(folder)["lifetimes"]
        # 'cancelled_after_renewed' sorts before 'card_cancelled_after_issued'.
        assert (life["start"], life["end"]) == ("renewed_on", "cancelled_on")
    assert _bundle_without_the_contract_digest(
        tmp_path / "0"
    ) == _bundle_without_the_contract_digest(tmp_path / "1")
    assert _notes(results[0]) == _notes(results[1])
    assert "one lifetime" in _notes(results[0])["card.card_cancelled_after_issued"]


def test_a_check_whose_end_a_published_pair_lays_out_is_not_called_kept(tmp_path: Path) -> None:
    # The profile publishes the days from 'renewed_on' to 'cancelled_on': the table's lifetime
    # draws 'cancelled_on', so its own histogram keeps no check.
    contract = _TWO_CHECKS.replace(_AFTER_RENEWED, "")
    profile = _profile("card", "renewed_on", "cancelled_on", pairs=True, plain=True, rows=20000)
    profile["tables"]["card"]["columns"]["issued_on"] = {
        "domain": "timestamp",
        "statistics": [_plain(STARTED)],
    }
    result = _init(tmp_path, contract, profile)
    (life,) = _block(tmp_path)["lifetimes"]
    assert (life["start"], life["end"]) == ("renewed_on", "cancelled_on")
    note = _notes(result)["card.card_cancelled_after_issued"]
    assert "'cancelled_on' is laid out by the table's published date pair" in note
    assert "already keep it" not in note


# -- the draw of the last bin, and the margin of a sample --------------------------------------------


def _ms_of(day: str) -> str:
    year, month, date = (int(part) for part in day.split("-"))
    return str(int(datetime(year, month, date, tzinfo=timezone.utc).timestamp() * 1000))


def _binned(edges: List[str], counts: List[int]) -> Dict[str, Any]:
    return {
        "definition": "marginalHistogram/1",
        "sampleSize": SAMPLE,
        "disclosure": "none",
        "bins": [_ms_of(day) for day in edges],
        "groups": [{"group": "", "k": sum(counts), "status": "F", "binCounts": counts}],
    }


def test_the_last_bin_is_drawn_closed_so_a_day_of_adjacency_needs_a_lifetime(
    tmp_path: Path,
) -> None:
    # Cards are issued from 1 June to 1 September and cancelled from 31 August to 1 December. Read
    # as half-open, the latest start is 31 August and the earliest end 31 August: the order always
    # holds. The generator draws a last bin closed at its top, so a card can be issued on 1 September
    # and cancelled on 31 August, and a check with no band is not kept by the histograms.
    contract = (
        CARD.replace(
            "{ name: cancelled_on, type: DATE }",
            "{ name: cancelled_on, type: DATE, required: true }",
        )
        .replace("cancelled_on IS NULL OR cancelled_on >= issued_on", "cancelled_on >= issued_on")
        .replace("          mustBeLessOrEqualTo: 5\n          unit: percent\n", "")
    )
    profile = _profile("card", "issued_on", "cancelled_on", plain=True, rows=20000)
    columns = profile["tables"]["card"]["columns"]
    columns["issued_on"]["statistics"] = [_binned(["2022-06-01", "2022-09-01"], [1000])]
    columns["cancelled_on"]["statistics"] = [_binned(["2022-08-31", "2022-12-01"], [1000])]
    result = _init(tmp_path, contract, profile)
    (life,) = _block(tmp_path)["lifetimes"]
    assert (life["start"], life["end"]) == ("issued_on", "cancelled_on")
    assert "is traded for a lifetime" in _notes(result)["card.card_cancelled_after_issued"]


@pytest.mark.parametrize(
    ("rows", "written"),
    [(20000, False), (5000, False), (1000, True), (100, True)],
)
def test_a_share_just_under_the_tolerance_is_kept_only_where_the_rows_are_many_enough(
    tmp_path: Path, rows: int, written: bool
) -> None:
    # Drawn apart the dates break the check on 2.8% of the rows, and its band is 4%. A sample of
    # 1,000 rows falls about 0.5 points either side of its share, so 2.8% and three such steps
    # are past 4%; 20,000 rows fall a fifth as far.
    result = _plain_card(
        tmp_path, band="mustBeLessOrEqualTo: 4\n          unit: percent", rows=rows
    )
    assert ("lifetimes" in _block(tmp_path)) is written
    (note,) = (text for subject, text in _notes(result).items() if subject.startswith("card."))
    assert ("is traded for a lifetime" in note) is written
    assert "on about 2.8% of the rows" in note
