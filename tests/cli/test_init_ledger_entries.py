# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ledger lists an entry of a format 1-3-0 profile whole when no member of it is used.

The profile reader refuses a calendar entry that holds none of its histograms, a date pair with no
``order`` and an exposure with only one of its two counts. A statistic the ledger lists as unused
is one an owner may drop to publish less, so the ledger must not list a member the reader needs
beside one the bundle still reads, and it names the whole entry when nothing of it is read.

Pinned here, from small hand-written profiles of a lending library:

* an entry no member of which is read is listed once, by its own pointer, and a profile with every
  listed pointer dropped is one ``init`` reads again and writes the same bundle from;
* a member that is read keeps its entry, and the members the reader does not need beside it are
  listed alone, while one it does need is not listed;
* the register's list is a list ``plan`` accepts for the pinned profile.
"""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, cast

import pytest
import yaml

from flux_engine.author import bundle as authoring
from flux_engine.author.ledger import Ledger
from flux_engine.cli._init import run_init
from flux_engine.seam.parameters import (
    FLX_3113,
    Citation,
    ParameterRegister,
    Source,
    _check_citation,
    check_profile_citations,
    parse_pointer,
    read_unused_block,
)
from flux_engine.seam.profile import load_profile
from flux_engine.stages.plan import plan as plan_stage
from flux_stats.errors import FluxError
from flux_stats.profile import SCHEMA_URL_1_4_0, load_profile_text
from tests.author import test_history_init as history_init
from tests.author.test_noisy_profile import _noise
from tests.author.test_noisy_profile import _noisy as _noisy_levels
from tests.cli.test_init_dates import (
    HOLDS,
    SCHEMA_URL,
    _calendar,
    _exposure,
    _levels,
    _link_pair,
    _months,
    _pair,
    _stamp,
    _stat,
)

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"

#: Members of this library joined at a date, lapsed at another, and came back to borrow.
CONTRACT = """\
fluidVersion: 0.7.7
kind: DataProduct
id: library.silver.lending
name: Lending library
metadata:
  owner:
    team: library
exposes:
- exposeId: member
  kind: table
  contract:
    schema:
    - name: member_id
      type: STRING
      required: true
    - name: joined_on
      type: TIMESTAMP
      required: true
    - name: lapsed_on
      type: TIMESTAMP
    - name: last_visit
      type: TIMESTAMP
    - name: renewed_on
      type: TIMESTAMP
  semantics:
    entities:
    - name: member
      type: primary
      expr: member_id
  binding:
    platform: local
    format: parquet
    location:
      path: ./out/member.parquet
- exposeId: loan
  kind: table
  contract:
    schema:
    - name: loan_id
      type: STRING
      required: true
    - name: member_id
      type: STRING
      required: true
    - name: borrowed_at
      type: TIMESTAMP
      required: true
    - name: returned_at
      type: TIMESTAMP
  semantics:
    entities:
    - name: loan
      type: primary
      expr: loan_id
    relationships:
    - name: loan_to_member
      to: member
      fromColumns:
      - member_id
      toColumns:
      - member_id
      cardinality: many_to_one
  binding:
    platform: local
    format: parquet
    location:
      path: ./out/loan.parquet
"""

#: Neither after nor before holds on nine tenths of the rows, so the pair is no lifetime.
LOOSE = _levels(after=40, before=30, same=30)
#: The days between a member's joining and a loan's return, ordered after on every pair.
AFTER = _levels(after=300, before=0, same=100)


@pytest.fixture(autouse=True)
def _declarations_are_read_before_any_trial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read what ``init`` declares from these small profiles, before the trial of the bundle."""
    monkeypatch.setattr(authoring, "_refuse_unless_reproduced", lambda *args, **kwargs: None)


def _histogram(low: int, high: int, counts: Sequence[int]) -> Dict[str, Any]:
    edges = [low + step * (high - low) // len(counts) for step in range(len(counts) + 1)]
    size = {"min": sum(counts), "max": sum(counts)}
    return {
        "definition": "marginalHistogram/1",
        "bins": [str(edge) for edge in edges],
        "sampleSize": size,
        "disclosure": "none",
        "groups": [{"group": "", "k": sum(counts), "binCounts": list(counts), "status": "F"}],
    }


def _profile() -> Dict[str, Any]:
    """A lending library whose members and loans publish every 1-3-0 entry once or twice."""
    month = _stat("month", _months(m3=300, m10=100, m1=40))
    week = _stat("dayOfWeek", [10, 10, 10, 10, 10, 1, 1])
    hours = _stat("hourOfWeek", [10 if day < 5 else 1 for day in range(7) for _ in range(24)])
    return {
        "_producer": "https://example.test/producer",
        "_schemaURL": SCHEMA_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "member": {
                "rowCount": 100,
                "columns": {
                    "member_id": {"domain": "category"},
                    "joined_on": _stamp(),
                    "lapsed_on": _stamp(),
                    "last_visit": _stamp(),
                    "renewed_on": _stamp(),
                },
                "datePairs": [
                    # Read: the pair is the lifetime of a membership.
                    _pair("joined_on", "lapsed_on", HOLDS),
                    # Not read: the order of the two dates is loose, so there is no lifetime.
                    _pair("joined_on", "last_visit", LOOSE),
                ],
                "calendar": [
                    # Not read: no entry of the bundle sets a renewal date.
                    _calendar("renewed_on", month=month, dayOfWeek=week, hourOfWeek=hours),
                    _calendar("last_visit", month=month),
                ],
            },
            "loan": {
                "rowCount": 400,
                "columns": {
                    "loan_id": {"domain": "category"},
                    "member_id": {"domain": "category"},
                    "borrowed_at": _stamp(),
                    "returned_at": _stamp(),
                },
            },
        },
        "relationships": [
            {
                "parent": {"table": "member", "columns": ["member_id"]},
                "child": {"table": "loan", "columns": ["member_id"]},
                "statistics": [_histogram(0, 8, (60, 40))],
                "dates": [
                    # Read: a loan is borrowed after its member joined.
                    _link_pair("joined_on", "borrowed_at", AFTER, (0, 150, 360), (50, 50)),
                    # Not read: the order is loose.
                    _link_pair("last_visit", "returned_at", LOOSE),
                ],
                "exposure": [
                    _exposure("joined_on", "lapsed_on"),
                    # Not read: one exposure of a relationship is, and this is the second.
                    _exposure("renewed_on", "last_visit"),
                ],
            }
        ],
    }


class Authored:
    """The bundle ``init`` writes for a profile, with what it says of the profile's statistics."""

    def __init__(self, root: Path, profile: Dict[str, Any], contract: str = CONTRACT) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "library.fluid.yaml").write_text(contract, encoding="utf-8")
        self.profile_path = root / "library.profile.json"
        self.profile_path.write_text(json.dumps(profile), encoding="utf-8")
        self.directory = root / "bundle"
        self.result = run_init(
            str(self.directory),
            contract=str(root / "library.fluid.yaml"),
            profile=str(self.profile_path),
        )

    @property
    def simulation(self) -> Dict[str, Any]:
        text = (self.directory / "simulation.flux.yml").read_text(encoding="utf-8")
        document = yaml.safe_load(text)
        assert isinstance(document, dict)
        return document

    @property
    def unused(self) -> List[Dict[str, Any]]:
        """What the Simulation lists as unused: each item's pointer, and its reason."""
        found = self.simulation["extensions"][NAMESPACE]["unused"]
        assert isinstance(found, list)
        return found

    @property
    def pointers(self) -> List[str]:
        return [item["statistic"]["pointer"] for item in self.unused]

    def comparable(self) -> Dict[str, Any]:
        """The Simulation less the two parts that name the profile: its digest and the unused list."""
        document = copy.deepcopy(self.simulation)
        block = document["extensions"][NAMESPACE]
        block.pop("profile", None)
        block.pop("unused", None)
        return document


def _drop(profile: Dict[str, Any], pointers: Sequence[str]) -> Dict[str, Any]:
    """*profile* without what each pointer reaches, the later entries of a list first."""
    found = copy.deepcopy(profile)

    def order(pointer: str) -> Tuple[Tuple[int, Any], ...]:
        return tuple((0, int(t)) if t.isdigit() else (1, t) for t in parse_pointer(pointer))

    for pointer in sorted(pointers, key=order, reverse=True):
        *parents, last = parse_pointer(pointer)
        node: Any = found
        for token in parents:
            node = node[int(token)] if isinstance(node, list) else node[token]
        if isinstance(node, list):
            del node[int(last)]
        else:
            del node[last]
    return found


def _read(profile: Dict[str, Any]) -> None:
    """Read *profile* as the engine does, so a document the reader refuses raises here."""
    load_profile_text(json.dumps(profile))


def test_a_profile_with_the_listed_statistics_dropped_is_read_again_and_gives_the_same_bundle(
    tmp_path: Path,
) -> None:
    profile = _profile()
    first = Authored(tmp_path / "first", profile)
    assert first.result.exit_code == 0, first.result.stderr
    dropped = _drop(profile, first.pointers)
    _read(dropped)
    second = Authored(tmp_path / "second", dropped)
    assert second.result.exit_code == 0, second.result.stderr
    assert second.comparable() == first.comparable()


def test_an_entry_no_member_of_which_is_read_is_listed_by_its_own_pointer(tmp_path: Path) -> None:
    written = Authored(tmp_path, _profile())
    assert written.result.exit_code == 0, written.result.stderr
    pointers = written.pointers
    for whole in (
        "/tables/member/datePairs/1",
        "/tables/member/calendar/0",
        "/relationships/0/dates/1",
        "/relationships/0/exposure/1",
    ):
        assert whole in pointers
        assert not [pointer for pointer in pointers if pointer.startswith(whole + "/")]


def test_a_member_the_reader_needs_beside_one_that_is_read_is_not_listed(tmp_path: Path) -> None:
    written = Authored(tmp_path, _profile())
    assert written.result.exit_code == 0, written.result.stderr
    for pointer in written.pointers:
        assert not pointer.endswith("/order"), pointer
        assert not pointer.endswith("/children"), pointer
        assert not pointer.endswith("/activeDays"), pointer
    # The entries the bundle reads stay whole in the profile it is handed again.
    dropped = _drop(_profile(), written.pointers)
    assert dropped["tables"]["member"]["datePairs"][0]["order"]
    assert dropped["relationships"][0]["exposure"][0]["children"]


def test_a_calendar_histogram_beside_one_that_is_read_is_listed_alone(tmp_path: Path) -> None:
    profile = _profile()
    month = _stat("month", _months(m3=300, m10=100, m1=40))
    hours = _stat("hourOfWeek", [10 if day < 5 else 1 for day in range(7) for _ in range(24)])
    # The bundle sets the loan's borrowed date, and so reads the month of its calendar. The weekday
    # is carried by the hour of the week, and is left out.
    profile["tables"]["loan"]["calendar"] = [
        _calendar(
            "borrowed_at", month=month, dayOfWeek=_stat("dayOfWeek", [3] * 7), hourOfWeek=hours
        )
    ]
    written = Authored(tmp_path, profile)
    assert written.result.exit_code == 0, written.result.stderr
    assert "/tables/loan/calendar/0/dayOfWeek" in written.pointers
    assert "/tables/loan/calendar/0" not in written.pointers
    _read(_drop(profile, written.pointers))


def test_the_register_lists_the_whole_entries_it_names_in_a_form_plan_accepts(
    tmp_path: Path,
) -> None:
    written = Authored(tmp_path, _profile())
    assert written.result.exit_code == 0, written.result.stderr
    planned = plan_stage(
        str(written.directory),
        state_dir=str(tmp_path / "state"),
        profile=str(written.profile_path),
    )
    assert planned.exit_code == 0, planned.stderr


def test_the_report_names_a_whole_entry_once_with_its_reason(tmp_path: Path) -> None:
    written = Authored(tmp_path, _profile())
    assert written.result.exit_code == 0, written.result.stderr
    lines = [line for line in written.result.stdout.splitlines() if "/calendar/" in line]
    assert len(lines) == 2, written.result.stdout
    assert all("rates shape only a date" in line for line in lines), lines
    assert not [line for line in lines if "/calendar/0/" in line or "/calendar/1/" in line]


# -- a table's history ---------------------------------------------------------------------------

FORMATS = ["audio", "hardback", "paperback"]

#: Editions of a title: a validity window, one natural entity, and a format that changes.
EDITION_CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: library.editions
name: Editions
metadata:
  owner:
    team: library
exposes:
  - exposeId: branch
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/branch.parquet }
    contract:
      schema:
        - { name: branch_id, type: INT, required: true }
    semantics:
      entities:
        - { name: branch, type: primary, expr: branch_id }
  - exposeId: edition
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/edition.parquet }
    contract:
      schema:
        - { name: edition_id, type: INT, required: true }
        - { name: title_id, type: INT, required: true }
        - { name: branch_id, type: INT, required: true }
        - { name: valid_from, type: DATE, required: true }
        - { name: valid_to, type: DATE, required: false }
        - { name: binding_kind, type: STRING, required: true }
        - { name: language, type: STRING, required: true }
        - { name: cataloged_on, type: DATE, required: true }
    semantics:
      entities:
        - { name: edition, type: primary, expr: edition_id }
        - { name: title, type: natural, expr: title_id }
      dimensions:
        - { name: valid_from, type: time, typeParams: { validityParams: { isStart: true } } }
        - { name: valid_to, type: time, typeParams: { validityParams: { isEnd: true } } }
        - { name: binding_kind, type: categorical }
        - { name: language, type: categorical }
        - { name: cataloged_on, type: time }
      relationships:
        - name: branch
          to: branch
          fromColumns: [branch_id]
          toColumns: [branch_id]
          cardinality: many_to_one
"""


def _edition_history() -> Dict[str, Any]:
    still = history_init._transitions("language", ["en", "fr"], {"en": [0, 0], "fr": [0, 0]})
    return {
        "entity": ["title_id"],
        "start": "valid_from",
        "end": "valid_to",
        "versionsPerEntity": history_init._bins(["1", "2", "3", "4"], [400, 350, 250], 1000),
        "firstStart": history_init._bins(
            [
                history_init._ms(2025, 1, 1),
                history_init._ms(2025, 4, 1),
                history_init._ms(2025, 7, 1),
                history_init._ms(2025, 9, 1),
            ],
            [420, 330, 250],
            1000,
        ),
        "versionLength": history_init._bins(["1", "30", "90", "365"], [250, 350, 250], 850),
        "chain": history_init._levels(
            ["dayBefore", "gap", "nextStart", "overlap"], [20, 10, 800, 20], 850
        ),
        "lastVersion": history_init._levels(["closed", "open"], [150, 850], 1000),
        "columns": {
            "binding_kind": {
                "initial": history_init._levels(FORMATS, [500, 350, 150], 1000),
                "transitions": history_init._transitions(
                    "binding_kind",
                    FORMATS,
                    {"audio": [0, 300, 100], "hardback": [100, 0, 200], "paperback": [40, 60, 0]},
                ),
            },
            "language": {
                "initial": history_init._levels(["en", "fr"], [600, 400], 1000),
                "transitions": still,
            },
        },
        "reasons": history_init._levels(["binding_kind", "language", "several"], [800, 0, 50], 850),
    }


def _edition_profile() -> Dict[str, Any]:
    metrics = history_init._metrics
    ms = history_init._ms
    return {
        "_producer": "https://example.test/producer",
        "_schemaURL": SCHEMA_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": copy.deepcopy(history_init._profile()["disclosureRules"]),
        "tables": {
            "branch": {
                "rowCount": 1000,
                "columns": {
                    "branch_id": {"domain": "number", "metrics": metrics("1", "1000", 1000)}
                },
            },
            "edition": {
                "rowCount": 1850,
                "columns": {
                    "edition_id": {"domain": "number", "metrics": metrics("1", "1850", 1850)},
                    "title_id": {"domain": "number", "metrics": metrics("1", "1000", 1850)},
                    "branch_id": {"domain": "number", "metrics": metrics("1", "1000", 1850)},
                    "valid_from": {
                        "domain": "timestamp",
                        "metrics": metrics(ms(2025, 1, 1), ms(2025, 9, 1), 1850),
                    },
                    "valid_to": {
                        "domain": "timestamp",
                        "metrics": metrics(ms(2025, 2, 1), ms(2025, 10, 1), 1850),
                    },
                    "binding_kind": {
                        "domain": "category",
                        "statistics": [history_init._levels(FORMATS, [1000, 600, 250], 1850)],
                    },
                    "language": {
                        "domain": "category",
                        "statistics": [history_init._levels(["en", "fr"], [1100, 750], 1850)],
                    },
                    "cataloged_on": {
                        "domain": "timestamp",
                        "metrics": metrics(ms(2024, 6, 1), ms(2025, 6, 1), 1850),
                    },
                },
                "history": _edition_history(),
            },
        },
    }


def test_a_history_no_member_of_which_is_read_is_listed_whole(tmp_path: Path) -> None:
    profile = _edition_profile()
    history = profile["tables"]["edition"]["history"]
    # Fewer than nine of ten versions chain on the next start, so no entry is written.
    history["chain"] = history_init._levels(
        ["dayBefore", "gap", "nextStart", "overlap"], [0, 51, 449, 0], 500
    )
    written = Authored(tmp_path / "first", profile, EDITION_CONTRACT)
    assert written.result.exit_code == 0, written.result.stderr
    assert "/tables/edition/history" in written.pointers
    assert not [pointer for pointer in written.pointers if "/history/" in pointer]
    dropped = _drop(profile, written.pointers)
    _read(dropped)
    again = Authored(tmp_path / "second", dropped, EDITION_CONTRACT)
    assert again.result.exit_code == 0, again.result.stderr
    assert again.comparable() == written.comparable()


def test_a_tracked_column_that_never_moves_leaves_with_both_statistics_and_the_reasons(
    tmp_path: Path,
) -> None:
    profile = _edition_profile()
    written = Authored(tmp_path / "first", profile, EDITION_CONTRACT)
    assert written.result.exit_code == 0, written.result.stderr
    history = "/tables/edition/history"
    listed = [pointer for pointer in written.pointers if pointer.startswith(history)]
    assert sorted(listed) == [f"{history}/columns/language", f"{history}/reasons"]
    dropped = _drop(profile, written.pointers)
    _read(dropped)
    again = Authored(tmp_path / "second", dropped, EDITION_CONTRACT)
    assert again.result.exit_code == 0, again.result.stderr
    assert again.comparable() == written.comparable()


def test_a_history_member_the_reader_needs_is_not_listed_beside_one_that_is_read(
    tmp_path: Path,
) -> None:
    profile = _edition_profile()
    history = profile["tables"]["edition"]["history"]
    # Every first start lies after the run's window, so the histogram is not read.
    history["firstStart"] = history_init._bins(
        [history_init._ms(2025, 1, 1), history_init._ms(2025, 4, 1), history_init._ms(2026, 6, 1)],
        [600, 400],
        1000,
    )
    written = Authored(tmp_path / "first", profile, EDITION_CONTRACT)
    assert written.result.exit_code == 0, written.result.stderr
    assert not [pointer for pointer in written.pointers if pointer.endswith("/firstStart")]
    _read(_drop(profile, written.pointers))


# -- the ledger, one statistic read at a time -----------------------------------------------------


def _unused(profile: Dict[str, Any], *read: str, under: str = "/") -> List[str]:
    """What the ledger of *profile* lists as unused under *under*, once a rule has read *read*."""
    ledger = Ledger(profile)
    for pointer in read:
        ledger.use(tuple(parse_pointer(pointer)), "read")
    return [entry.pointer for entry in ledger.unused_entries() if entry.pointer.startswith(under)]


HISTORY_OF_EDITIONS = "/tables/edition/history"


def test_an_exposure_is_never_listed_by_one_of_its_counts() -> None:
    profile = _profile()
    exposure = "/relationships/0/exposure/1"
    assert exposure in _unused(profile)
    for read in ("children", "activeDays"):
        listed = _unused(profile, f"{exposure}/{read}")
        assert not [pointer for pointer in listed if pointer.startswith(exposure)], listed


def test_a_date_pair_lists_a_statistic_alone_and_its_order_never_while_the_other_is_read() -> None:
    profile = _profile()
    pair = "/tables/member/datePairs/1"
    assert _unused(profile) == [
        pointer for pointer in _unused(profile) if not pointer.startswith(pair + "/")
    ]
    read_order = _unused(profile, f"{pair}/order")
    assert f"{pair}/statistics/0" in read_order and pair not in read_order
    read_statistic = _unused(profile, f"{pair}/statistics/0")
    assert not [p for p in read_statistic if p == pair or p.startswith(pair + "/")], read_statistic


def test_a_calendar_histogram_is_listed_alone_while_another_of_its_entry_is_read() -> None:
    profile = _profile()
    entry = "/tables/member/calendar/0"
    listed = _unused(profile, f"{entry}/month")
    assert f"{entry}/dayOfWeek" in listed and f"{entry}/hourOfWeek" in listed
    assert entry not in listed and f"{entry}/month" not in listed


def test_a_tracked_column_is_not_listed_while_the_reasons_over_it_are_kept() -> None:
    profile = _edition_profile()
    columns = f"{HISTORY_OF_EDITIONS}/columns"
    under = HISTORY_OF_EDITIONS
    assert _unused(profile, f"{under}/reasons", under=under) == []
    assert sorted(_unused(profile, f"{under}/chain", under=under)) == [
        f"{columns}/binding_kind",
        f"{columns}/language",
        f"{under}/reasons",
    ]
    assert sorted(_unused(profile, f"{columns}/binding_kind/initial", under=under)) == [
        f"{columns}/language",
        f"{under}/reasons",
    ]


def test_every_member_of_an_entry_unused_or_withheld_is_listed_whole() -> None:
    profile = _profile()
    profile["relationships"][0]["exposure"][1]["activeDays"] = {
        "definition": "count/1",
        "status": "S",
        "sampleSize": {"min": 100, "max": 100},
        "disclosure": "none",
    }
    assert "/relationships/0/exposure/1" in _unused(profile)


# -- a calendar entry of a date column, and what is left of it once its list is dropped -----------

#: The members' contract with the renewal date a DATE, which has no hour of the week.
DATE_CONTRACT = CONTRACT.replace(
    "    - name: renewed_on\n      type: TIMESTAMP\n", "    - name: renewed_on\n      type: DATE\n"
)
_ENTRY = "/tables/member/calendar/0"


def _date_profile() -> Dict[str, Any]:
    """The library with one calendar entry, of the DATE ``renewed_on``: a month and a weekday."""
    profile = _profile()
    profile["tables"]["member"]["calendar"] = [
        _calendar(
            "renewed_on",
            month=_stat("month", _months(m3=300, m10=100, m1=40)),
            dayOfWeek=_stat("dayOfWeek", [10, 10, 10, 10, 10, 1, 1]),
        )
    ]
    return profile


def _withheld(histogram: Dict[str, Any]) -> Dict[str, Any]:
    """*histogram* as a profile that suppresses its cells publishes it: a status, and no counts."""
    found = copy.deepcopy(histogram)
    group = found["groups"][0]
    group["status"] = "C"
    del group["levelCounts"], group["k"]
    return found


def _released(histogram: Dict[str, Any]) -> Dict[str, Any]:
    """*histogram* as a profile released under differential privacy publishes it: noisy counts."""
    found = copy.deepcopy(histogram)
    group = found["groups"][0]
    group["noisyCounts"] = [str(count) for count in group.pop("levelCounts")]
    del group["k"]
    found["disclosure"] = "noisy"
    found["noise"] = _noise(rho="0.1")
    return found


def test_a_date_calendar_none_of_which_is_read_is_listed_whole_and_dropping_it_is_read(
    tmp_path: Path,
) -> None:
    profile = _date_profile()
    first = Authored(tmp_path / "first", profile, DATE_CONTRACT)
    assert first.result.exit_code == 0, first.result.stderr
    assert _ENTRY in first.pointers
    assert not [pointer for pointer in first.pointers if pointer.startswith(_ENTRY + "/")]
    dropped = _drop(profile, first.pointers)
    _read(dropped)
    second = Authored(tmp_path / "second", dropped, DATE_CONTRACT)
    assert second.result.exit_code == 0, second.result.stderr
    assert second.comparable() == first.comparable()


def _released_library(month: str, weekday: str) -> Dict[str, Any]:
    """The library as a profile released for its loans: each histogram of a DATE's calendar as given.

    *month* and *weekday* are ``exact``, ``released`` (noisy counts) or ``withheld``. The loans'
    region is released too, and a bundle reads it, so the profile's privacy loss is spent.
    """
    forms = {"exact": lambda each: each, "released": _released, "withheld": _withheld}
    profile = _profile()
    profile["_schemaURL"] = SCHEMA_URL_1_4_0
    profile["disclosureRules"] = {"none": {}, "noisy": {}}
    profile["privacy"] = {
        "measure": "zeroConcentratedDP",
        "privacyLoss": {"rho": "0.5"},
        "privacyUnit": {"expose": "loan"},
    }
    profile["tables"]["loan"]["columns"]["region"] = {
        "domain": "category",
        "statistics": [_noisy_levels(["east", "north", "west"], ["520", "500", "300"])],
    }
    profile["tables"]["loan"]["calendar"] = [
        _calendar(
            "borrowed_at",
            month=forms[month](_stat("month", _months(m3=300, m10=100, m1=40))),
            dayOfWeek=forms[weekday](_stat("dayOfWeek", [10, 10, 10, 10, 10, 1, 1])),
        )
    ]
    return profile


_RELEASED_CONTRACT = CONTRACT.replace(
    "    - name: borrowed_at\n      type: TIMESTAMP\n",
    "    - name: borrowed_at\n      type: DATE\n",
).replace(
    "    - name: returned_at\n      type: TIMESTAMP\n",
    "    - name: returned_at\n      type: TIMESTAMP\n"
    "    - name: region\n      type: \"enum('east','north','west')\"\n",
)


@pytest.mark.parametrize(
    ("month", "weekday"), list(itertools.product(("exact", "released", "withheld"), repeat=2))
)
def test_a_released_date_calendar_dropped_by_its_list_keeps_a_histogram_the_reader_accepts(
    tmp_path: Path, month: str, weekday: str
) -> None:
    profile = _released_library(month, weekday)
    _read(profile)
    first = Authored(tmp_path / "first", profile, _RELEASED_CONTRACT)
    assert first.result.exit_code == 0, first.result.stderr
    entry = "/tables/loan/calendar/0"
    members = [pointer for pointer in first.pointers if pointer.startswith(entry + "/")]
    # An entry no histogram of which is read is named whole, never member by member; one that is
    # read leaves a histogram, whichever others are listed beside it.
    if entry in first.pointers:
        assert not members
    else:
        assert len(members) < 2
    dropped = _drop(profile, first.pointers)
    _read(dropped)
    second = Authored(tmp_path / "second", dropped, _RELEASED_CONTRACT)
    assert second.result.exit_code == 0, second.result.stderr


_STATES = ("absent", "used", "unused", "withheld")


@pytest.mark.parametrize(
    "states",
    [
        states
        for states in itertools.product(_STATES, repeat=3)
        if any(state != "absent" for state in states)
    ],
)
def test_no_member_of_a_calendar_entry_leaves_the_entry_with_none_of_its_histograms(
    states: Tuple[str, str, str],
) -> None:
    # Whatever the entry holds of its three histograms, and whichever the rules read or the
    # profile withheld, the pointers the ledger lists leave a calendar entry the reader accepts.
    keys = ("month", "dayOfWeek", "hourOfWeek")
    made = {
        "month": _stat("month", _months(m3=300, m10=100, m1=40)),
        "dayOfWeek": _stat("dayOfWeek", [10, 10, 10, 10, 10, 1, 1]),
        "hourOfWeek": _stat("hourOfWeek", [1] * 168),
    }
    entry: Dict[str, Any] = {"column": "renewed_on"}
    for key, state in zip(keys, states, strict=True):
        if state != "absent":
            entry[key] = _withheld(made[key]) if state == "withheld" else made[key]
    profile = _profile()
    profile["tables"]["member"]["calendar"] = [entry]
    ledger = Ledger(profile)
    for key, state in zip(keys, states, strict=True):
        if state == "used":
            ledger.use(("tables", "member", "calendar", "0", key), "read")
    everything = [item.pointer for item in ledger.unused_entries()]
    _read(_drop(profile, everything))
    listed = [pointer for pointer in everything if "/calendar/" in pointer]
    if "used" in states:
        assert _ENTRY not in listed
    elif "unused" in states:
        assert listed == [_ENTRY]


# -- the ledger of ``init --json`` ---------------------------------------------------------------


def test_the_json_ledger_lists_what_an_owner_can_leave_out_as_the_register_does(
    tmp_path: Path,
) -> None:
    profile = _date_profile()
    written = Authored(tmp_path, profile, DATE_CONTRACT)
    assert written.result.exit_code == 0, written.result.stderr
    ledger = written.result.artifacts["ledger"]
    assert [item["pointer"] for item in ledger["unused"]] == written.pointers
    assert [item["reason"] for item in ledger["unused"]] == [
        item["reason"] for item in written.unused
    ]
    _read(_drop(profile, [item["pointer"] for item in ledger["unused"]]))


def _unused_members(written: "Authored", entry: str) -> List[str]:
    return [
        item["pointer"]
        for item in written.result.artifacts["ledger"]["statistics"]
        if item["state"] == "unused" and item["pointer"].startswith(entry + "/")
    ]


def _plan(written: "Authored", tmp_path: Path) -> Any:
    return plan_stage(
        str(written.directory),
        state_dir=str(tmp_path / "state"),
        profile=str(written.profile_path),
    )


def test_the_statistics_of_the_json_ledger_name_each_member_of_a_whole_entry_on_its_own(
    tmp_path: Path,
) -> None:
    # The inventory is per statistic, with the state each was given. Dropping every member it
    # calls unused leaves an entry that holds none of its histograms: the strict reader of
    # flux-stats refuses it, and the engine reads it as an entry that holds nothing, so the
    # profile still inits and plans.
    profile = _date_profile()
    written = Authored(tmp_path / "first", profile, DATE_CONTRACT)
    assert written.result.exit_code == 0, written.result.stderr
    members = _unused_members(written, _ENTRY)
    assert members == [f"{_ENTRY}/month", f"{_ENTRY}/dayOfWeek"]
    dropped = _drop(profile, members)
    with pytest.raises(FluxError, match="holds at least one of its three histograms"):
        _read(dropped)
    assert dropped["tables"]["member"]["calendar"] == [{"column": "renewed_on"}]
    second = Authored(tmp_path / "second", dropped, DATE_CONTRACT)
    assert second.result.exit_code == 0, second.result.stderr
    planned = _plan(second, tmp_path / "second")
    assert planned.exit_code == 0, planned.stderr


def test_a_released_calendar_dropped_member_by_member_is_read_by_init_and_plan(
    tmp_path: Path,
) -> None:
    # A released profile's calendar of an entry nothing reads, dropped statistic by statistic the way
    # a release step drops what the inventory marks unused, is read by init and plan as it is.
    profile = _released_library("released", "released")
    contract = _RELEASED_CONTRACT
    # The loans' own calendar is read by nothing: no step of the bundle sets a borrowing date.
    first = Authored(tmp_path / "first", profile, contract)
    assert first.result.exit_code == 0, first.result.stderr
    entry = "/tables/loan/calendar/0"
    members = _unused_members(first, entry)
    assert members, "the loans' calendar is expected to be read by nothing"
    dropped = _drop(profile, members)
    assert dropped["tables"]["loan"]["calendar"] == [{"column": "borrowed_at"}]
    second = Authored(tmp_path / "second", dropped, contract)
    assert second.result.exit_code == 0, second.result.stderr
    planned = _plan(second, tmp_path / "second")
    assert planned.exit_code == 0, planned.stderr


# -- a pointer of the register at a whole entry ------------------------------------------------


def _items(pointers: Sequence[str], definition: Optional[str] = None) -> Any:
    statistic: Dict[str, Any] = {} if definition is None else {"definition": definition}
    block = [
        {"statistic": {"pointer": pointer, **statistic}, "reason": "no rule reads it"}
        for pointer in pointers
    ]
    return read_unused_block(block, pinned=True, where="unused", code=FLX_3113)


def _check_unused(profile: Dict[str, Any], pointers: Sequence[str], **named: Any) -> None:
    register = SimpleNamespace(entries=(), unused=_items(pointers, **named))
    check_profile_citations(cast(ParameterRegister, register), profile, where="sim-x")


WHOLE: Tuple[Tuple[Callable[[], Dict[str, Any]], str], ...] = (
    (_profile, "/tables/member/calendar/0"),
    (_profile, "/tables/member/datePairs/1"),
    (_profile, "/relationships/0/dates/1"),
    (_profile, "/relationships/0/exposure/1"),
    (_edition_profile, "/tables/edition/history"),
    (_edition_profile, "/tables/edition/history/columns/language"),
)


@pytest.mark.parametrize(("make", "pointer"), WHOLE)
def test_an_unused_item_may_name_a_whole_entry_of_the_profile(
    make: Callable[[], Dict[str, Any]], pointer: str
) -> None:
    _check_unused(make(), [pointer])


@pytest.mark.parametrize(("make", "pointer"), WHOLE)
def test_a_whole_entry_has_no_definition_and_is_no_citation_of_a_number(
    make: Callable[[], Dict[str, Any]], pointer: str
) -> None:
    with pytest.raises(FluxError, match="has no definition"):
        _check_unused(make(), [pointer], definition="levelHistogram/1")
    with pytest.raises(FluxError, match="reaches no statistic"):
        _check_citation(_items([pointer])[0].citation, make(), "at")


@pytest.mark.parametrize(
    "pointer",
    [
        "/tables/member/calendar/9",
        "/tables/member/calendar",
        "/tables/member/columns/joined_on",
        "/tables/member/datePairs/0/start",
        "/relationships/0",
    ],
)
def test_a_pointer_that_is_no_statistic_and_no_whole_entry_is_still_refused(pointer: str) -> None:
    with pytest.raises(FluxError) as raised:
        _check_unused(_profile(), [pointer])
    assert raised.value.code.code == "FLX-3113"


# -- a calendar entry that holds nothing: nothing reads it, and a citation into it is refused -----


def _emptied_profile() -> Dict[str, Any]:
    """The library with the first calendar entry of ``member`` holding none of its histograms."""
    return _drop(
        _profile(),
        [f"/tables/member/calendar/0/{key}" for key in ("month", "dayOfWeek", "hourOfWeek")],
    )


def _cites(pointer: str, definition: Optional[str] = "levelHistogram/1") -> ParameterRegister:
    entry = SimpleNamespace(
        citation=Citation(pointer=pointer, tokens=parse_pointer(pointer), definition=definition),
        source=Source.PROFILE,
        label=None,
        index=0,
    )
    return cast(ParameterRegister, SimpleNamespace(entries=(entry,), unused=None))


def test_the_engine_keeps_an_emptied_calendar_entry_in_place() -> None:
    profile = _emptied_profile()
    assert profile["tables"]["member"]["calendar"][0] == {"column": "renewed_on"}
    read, _ = load_profile_text(json.dumps(profile), allow_emptied_entries=True)
    calendar = read.tables["member"].calendar
    assert [entry.column for entry in calendar] == ["renewed_on", "last_visit"]
    assert (calendar[0].month, calendar[0].day_of_week, calendar[0].hour_of_week) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    "pointer",
    [
        "/tables/member/calendar/0/month",
        "/tables/member/calendar/0/hourOfWeek",
        "/tables/member/calendar/0",
    ],
)
def test_a_citation_into_a_calendar_entry_that_holds_nothing_is_refused_by_the_calendar_rule(
    pointer: str,
) -> None:
    with pytest.raises(FluxError) as raised:
        check_profile_citations(_cites(pointer), _emptied_profile(), where="sim-x")
    assert raised.value.code.code == "FLX-5036"
    message = str(raised.value)
    assert "/tables/member/calendar/0" in message
    assert "holds none of its statistics" in message
    assert "month, dayOfWeek, hourOfWeek" in message


def test_a_citation_into_a_calendar_entry_that_holds_a_histogram_is_still_read_as_before() -> None:
    check_profile_citations(_cites("/tables/member/calendar/0/month"), _profile(), where="sim-x")
    # The second entry of the emptied profile still holds its month.
    check_profile_citations(
        _cites("/tables/member/calendar/1/month"), _emptied_profile(), where="sim-x"
    )


@pytest.mark.parametrize(
    "pointer",
    [
        "/tables/member/calendar/0",
        "/tables/member/calendar/0/month",
        "/tables/member/calendar/0/dayOfWeek",
        "/tables/member/calendar/0/hourOfWeek",
    ],
)
def test_an_unused_item_may_name_a_calendar_entry_that_holds_nothing_or_a_member_of_it(
    pointer: str,
) -> None:
    _check_unused(_emptied_profile(), [pointer])


def test_an_unused_item_that_is_no_member_of_an_empty_calendar_entry_is_still_refused() -> None:
    with pytest.raises(FluxError) as raised:
        _check_unused(_emptied_profile(), ["/tables/member/calendar/0/column"])
    assert raised.value.code.code == "FLX-3113"


def test_a_bundle_that_reads_a_calendar_histogram_is_refused_against_a_profile_that_dropped_it(
    tmp_path: Path,
) -> None:
    profile = _profile()
    month = _stat("month", _months(m3=300, m10=100, m1=40))
    hours = _stat("hourOfWeek", [10 if day < 5 else 1 for day in range(7) for _ in range(24)])
    # The bundle sets the loan's borrowed date, so its rates cite the month of this calendar entry.
    profile["tables"]["loan"]["calendar"] = [
        _calendar("borrowed_at", month=month, hourOfWeek=hours)
    ]
    written = Authored(tmp_path, profile)
    assert written.result.exit_code == 0, written.result.stderr
    cited = [
        entry["statistic"]["pointer"]
        for entry in written.simulation["extensions"][NAMESPACE]["parameters"]
        if entry.get("source") == "profile"
    ]
    assert any(pointer.startswith("/tables/loan/calendar/0/") for pointer in cited)
    # Plan the same bundle against the profile with that entry emptied. The pin is moved to the
    # emptied profile, so the digest is no reason to refuse and the citation is.
    emptied = _drop(profile, [f"/tables/loan/calendar/0/{key}" for key in ("month", "hourOfWeek")])
    emptied_path = tmp_path / "emptied.profile.json"
    emptied_path.write_text(json.dumps(emptied), encoding="utf-8")
    pinned = load_profile(emptied_path).digest.value
    simulation_path = written.directory / "simulation.flux.yml"
    document = written.simulation
    document["extensions"][NAMESPACE]["profile"] = {"digest": pinned}
    simulation_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    planned = plan_stage(
        str(written.directory), state_dir=str(tmp_path / "state"), profile=str(emptied_path)
    )
    assert planned.exit_code != 0
    assert "FLX-5036" in planned.stderr
    assert "/tables/loan/calendar/0" in planned.stderr
