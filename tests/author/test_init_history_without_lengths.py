# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A release that withholds a history's lengths still keeps each version's end after its start.

Pinned here, through ``run_init``, on the tier history of a gym's members:

* a history whose version lengths (and, with them, the first starts, the versions per entity, the
  chain and the share of last versions that close) are withheld is written from what the release
  does publish: the entities from the natural entity's distinct count, the first starts from the
  start column's histogram, the length from the published histograms of the start and the end, and
  the closed share from the open mass of the end's histogram, each as an assumption in the register;
* some members published and some withheld, one version or several per entity, an open end and a
  closed share;
* a history that cannot be written (a required end with no declared open end, or a column the
  release publishes on the start's day that no activation keeps with it) still lays the start and
  the end out as one lifetime, so no version ends before it starts;
* a pair the release publishes as a date pair is that pair's, as before.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.author.gap_fit import (
    GAP_FIT_ITERATIONS,
    dominance_gap,
    fit_gap,
    longest_without_leak,
)
from flux_engine.cli._init import run_init

from .test_history_date_pairs import TIERS
from .test_history_date_pairs import _profile as _published_profile
from .test_history_init import SAMPLE, _bins, _levels, _ms, _namespace, _states

pytestmark = pytest.mark.integration

#: A contract whose validity end declares the value an open version holds.
OPEN_ENDED = TIERS.replace(
    "{ name: valid_to, type: time, typeParams: { validityParams: { isEnd: true } } }",
    "{ name: valid_to, type: time, typeParams: { validityParams: "
    '{ isEnd: true, openEnd: "9999-12-31", chain: nextStart } } }',
)
#: The same, with the end required: no open version could hold a NULL.
REQUIRED_END = OPEN_ENDED.replace(
    "{ name: valid_to, type: DATE, required: false }",
    "{ name: valid_to, type: DATE, required: true }",
)
#: A contract that requires the end and declares no open end: the history cannot be written.
NO_OPEN_END = TIERS.replace(
    "{ name: valid_to, type: DATE, required: false }",
    "{ name: valid_to, type: DATE, required: true }",
)
#: The activation column the contract names, which keeps a column with the first start.
ACTIVATED = OPEN_ENDED.replace(
    "{ name: pass, type: natural, expr: pass_id }",
    "{ name: pass, type: natural, expr: pass_id, lifecycle: { activation: enrolled_on } }",
)

OPEN = _ms(9999, 12, 31)
UNTIL = _ms(2025, 10, 1)
START_EDGES = [_ms(2025, 1, 1), _ms(2025, 3, 1), _ms(2025, 5, 1), _ms(2025, 7, 1), _ms(2025, 9, 1)]
END_EDGES = [_ms(2025, 2, 1), _ms(2025, 5, 1), _ms(2025, 8, 1), UNTIL, OPEN]
HISTORY = "/tables/tier_version/history"


def _withheld(node: Dict[str, Any]) -> Dict[str, Any]:
    """*node* with its one group withheld, as a release cuts a cell that holds too few records."""
    found = copy.deepcopy(node)
    found["groups"] = [{"group": "", "status": "C"}]
    for key in ("k", "levelCounts", "binCounts"):
        found["groups"][0].pop(key, None)
    return found


def _profile(
    *,
    rows: int = 1000,
    entities: int = 1000,
    open_cell: int = 500,
    keep: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """A release of ``TIERS`` whose history publishes only the members named in *keep*.

    The start column publishes a histogram of 4 bins over 2025, the end column one whose last bin,
    from the horizon to the open end, holds *open_cell* of the *rows*.
    """
    keep = keep or []
    profile = _published_profile()
    version = profile["tables"]["tier_version"]
    version["rowCount"] = rows
    closed = rows - open_cell
    weights = [closed // 4, closed // 4, closed - 2 * (closed // 4), 0]
    version["columns"]["valid_from"]["statistics"] = [
        _bins(START_EDGES, [rows // 4, rows // 4, rows // 4, rows - 3 * (rows // 4)], rows)
    ]
    version["columns"]["valid_to"]["statistics"] = [
        _bins(END_EDGES, [weights[0], weights[1], weights[2], open_cell], rows)
    ]
    version["columns"]["valid_to"]["metrics"] = {
        **version["columns"]["valid_to"]["metrics"],
        "max": {**version["columns"]["valid_to"]["metrics"]["max"], "value": OPEN},
    }
    version["columns"]["pass_id"]["metrics"]["distinctCount"] = {
        "definition": "distinctCount/1",
        "status": "F",
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "value": entities,
    }
    version["columns"]["pass_id"]["metrics"]["count"]["value"] = rows
    history = version["history"]
    for name in ("versionsPerEntity", "firstStart", "versionLength", "chain", "lastVersion"):
        if name not in keep and name in history:
            history[name] = _withheld(history[name])
    for name in ("versionsPerEntity", "firstStart"):
        if name not in keep:
            history.pop(name, None)
    if "versionLength" not in keep:
        history["versionLength"] = {
            "definition": "marginalHistogram/1",
            "bins": ["1", "30", "90"],
            "sampleSize": SAMPLE,
            "disclosure": "acro-default",
            "groups": [{"group": "", "status": "C"}],
        }
    version["history"] = history
    return profile


def _init(tmp_path: Path, profile: Dict[str, Any], contract: str = OPEN_ENDED) -> Any:
    (tmp_path / "tiers.fluid.yml").write_text(contract, encoding="utf-8")
    (tmp_path / "tiers.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "tiers.fluid.yml"),
        profile=str(tmp_path / "tiers.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _entry(tmp_path: Path) -> Dict[str, Any]:
    entries = _namespace(tmp_path).get("history", [])
    assert len(entries) == 1, entries
    found: Dict[str, Any] = entries[0]
    return found


def _lifetimes(tmp_path: Path) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = _namespace(tmp_path).get("lifetimes", [])
    return found


def _reasons(tmp_path: Path, under: str) -> List[str]:
    """The assumptions the register holds for the numbers below *under* in the namespace."""
    prefix = "/extensions/com.agentics.flux~11/" + under
    return [
        item["reason"]
        for item in _namespace_parameters(tmp_path)
        if item["pointer"].startswith(prefix) and item["source"] == "assumption"
    ]


def _namespace_parameters(tmp_path: Path) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = _namespace(tmp_path)["parameters"]
    return found


def _weights(entry: Dict[str, Any]) -> List[Fraction]:
    return [Fraction(text) for text in entry["length"]["binWeights"]]


# ---------------------------------------------------------------------------
# A history written from what the release does publish
# ---------------------------------------------------------------------------


def test_a_single_version_history_is_written_with_every_length_member_withheld(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _profile(), ACTIVATED)
    entry = _entry(tmp_path)
    # One version per entity: the entities are the natural entity's distinct count.
    assert entry["entities"] == 1000 and "versions" not in entry
    # The first starts are the start column's own histogram, which is exact for one version.
    assert entry["start"]["bins"][0] == "2025-01-01" and entry["start"]["bins"][-1] == "2025-09-01"
    assert [Fraction(text) for text in entry["start"]["binWeights"]] == [Fraction(1, 4)] * 4
    # The length is derived: gaps of a day or more that sum to 1, not the one even bin.
    assert entry["length"]["bins"][0] == "1" and entry["length"]["closed"] == "lower"
    assert len(entry["length"]["bins"]) > 2 and sum(_weights(entry)) == 1
    # Half of the versions are open at the horizon, so half of the entities have not ended.
    assert entry["end"] == {"mode": "closeLast", "share": "0.5"}
    assert entry["openEnd"] == "9999-12-31" and entry["chain"] == "nextStart"
    assert _lifetimes(tmp_path) == []
    assert any(
        "derived from the published histograms" in text
        for text in _reasons(tmp_path, "history/0/length")
    )
    assert result.exit_code == 0


def test_what_the_release_withholds_is_assumed_in_the_register(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile(), ACTIVATED)
    assert _states(result)[f"{HISTORY}/versionLength"]["state"] == "withheld"
    assert any("withholds" in text for text in _reasons(tmp_path, "history/0/length"))
    assert any("distinct count" in text for text in _reasons(tmp_path, "history/0/entities"))
    assert any("withholds" in text for text in _reasons(tmp_path, "history/0/start"))
    assert any(
        "not ended" in text or "have not ended" in text
        for text in _reasons(tmp_path, "history/0/end")
    )


def test_several_versions_per_entity_spread_evenly_over_the_distinct_count(tmp_path: Path) -> None:
    _init(tmp_path, _profile(rows=1850, entities=1000, open_cell=850), ACTIVATED)
    entry = _entry(tmp_path)
    assert entry["entities"] == 1000 and "versions" not in entry
    assert entry["chain"] == "nextStart"
    # 850 of 1850 rows are open: every entity that has not ended keeps one, so 150 ended.
    assert entry["end"] == {"mode": "closeLast", "share": "0.15"}
    assert entry["length"]["bins"][0] == "1" and sum(_weights(entry)) == 1


def test_published_members_are_read_and_only_the_lengths_are_derived(tmp_path: Path) -> None:
    keep = ["versionsPerEntity", "firstStart", "chain"]
    result = _init(
        tmp_path, _profile(rows=1850, entities=1000, open_cell=1000, keep=keep), ACTIVATED
    )
    entry = _entry(tmp_path)
    assert entry["versions"]["bins"] == ["1", "2", "3", "4"]
    assert entry["start"]["bins"] == ["2025-01-01", "2025-04-01", "2025-07-01", "2025-09-01"]
    states = _states(result)
    assert states[f"{HISTORY}/versionsPerEntity"]["state"] == "used"
    assert states[f"{HISTORY}/firstStart"]["state"] == "used"
    assert states[f"{HISTORY}/chain"]["state"] == "used"
    assert states[f"{HISTORY}/versionLength"]["state"] == "withheld"
    assert entry["length"]["bins"][0] == "1" and len(entry["length"]["bins"]) > 2


def test_the_closed_share_follows_the_open_cell_of_the_end(tmp_path: Path) -> None:
    for open_cell, share in ((250, "0.75"), (900, "0.1")):
        folder = tmp_path / str(open_cell)
        folder.mkdir()
        _init(folder, _profile(open_cell=open_cell), ACTIVATED)
        assert _entry(folder)["end"] == {"mode": "closeLast", "share": share}


def test_a_nullable_end_with_no_open_cell_ends_every_version(tmp_path: Path) -> None:
    _init(tmp_path, _profile(open_cell=0), ACTIVATED)
    entry = _entry(tmp_path)
    assert entry["end"] == {"mode": "closeLast", "share": "1"}


def test_published_lengths_are_read_as_before(tmp_path: Path) -> None:
    profile = _published_profile()
    _init(tmp_path, profile, ACTIVATED)
    entry = _entry(tmp_path)
    assert entry["length"]["bins"] == ["1", "30", "90", "365"]


# ---------------------------------------------------------------------------
# A history that cannot be written keeps the end after the start
# ---------------------------------------------------------------------------


def _lifetime(tmp_path: Path) -> Dict[str, Any]:
    found = [item for item in _lifetimes(tmp_path) if item["emit"] == "tier_version"]
    assert len(found) == 1, found
    return found[0]


def test_a_required_end_with_no_open_end_lays_the_start_and_the_end_out_as_a_lifetime(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _profile(), NO_OPEN_END)
    assert _namespace(tmp_path).get("history", []) == []
    life = _lifetime(tmp_path)
    assert (life["start"], life["end"]) == ("valid_from", "valid_to")
    # A version lasts a day or more, and the gaps are derived from the two histograms.
    assert life["duration"]["bins"][0] == "1" and len(life["duration"]["bins"]) > 2
    assert sum(Fraction(text) for text in life["duration"]["binWeights"]) == 1
    assert any(
        "derived from the published histograms" in text
        for text in _reasons(tmp_path, "lifetimes/0/duration")
    )
    assert "is traded for a lifetime" in result.stdout


def test_a_column_published_on_the_start_day_keeps_the_history_out_unless_activation_ties_it(
    tmp_path: Path,
) -> None:
    profile = _profile()
    same = _levels(["after", "before", "same"], [0, 0, 1000], 1000)
    profile["tables"]["tier_version"]["datePairs"] = [
        {"start": "valid_from", "end": "enrolled_on", "order": same, "statistics": []}
    ]
    result = _init(tmp_path, profile, OPEN_ENDED)
    assert _namespace(tmp_path).get("history", []) == []
    life = _lifetime(tmp_path)
    assert (life["start"], life["end"]) == ("valid_from", "valid_to")
    assert "declare lifecycle.activation" in json.dumps(result.artifacts["ledger"]["statistics"])
    # With the activation declared, the history is written and keeps the column with the start.
    folder = tmp_path / "activated"
    folder.mkdir()
    _init(folder, profile, ACTIVATED)
    assert _entry(folder)["activation"] == "enrolled_on"


def test_a_pair_the_release_publishes_as_a_date_pair_is_that_pairs(tmp_path: Path) -> None:
    profile = _profile()
    pair = {
        "start": "valid_from",
        "end": "valid_to",
        "order": _levels(["after", "before", "same"], [1000, 0, 0], 1000),
        "statistics": [_bins(["1", "30", "90", "365"], [300, 400, 300], 1000)],
    }
    profile["tables"]["tier_version"]["datePairs"] = [pair]
    _init(tmp_path, profile, NO_OPEN_END)
    life = _lifetime(tmp_path)
    assert life["duration"]["bins"] == ["1", "30", "90", "365"]
    assert not any("derived" in text for text in _reasons(tmp_path, "lifetimes/0/duration"))


# ---------------------------------------------------------------------------
# The fit
# ---------------------------------------------------------------------------


def _bins_of(edges: List[int], shares: List[int]) -> List[Any]:
    total = sum(shares)
    return [
        (Fraction(low), Fraction(high), Fraction(share, total))
        for low, high, share in zip(edges[:-1], edges[1:], shares, strict=True)
    ]


def test_the_fit_is_the_same_on_every_run_and_uses_no_float() -> None:
    starts = _bins_of([0, 100, 200, 300], [3, 2, 1])
    ends = _bins_of([10, 150, 300, 500], [2, 3, 1])
    first = fit_gap(starts, ends, 1, 400)
    assert first == fit_gap(starts, ends, 1, 400)
    assert all(isinstance(weight, Fraction) for weight in first.weights)
    assert (
        sum(first.weights) == 1 and first.edges[0] == 1 and list(first.edges) == sorted(first.edges)
    )


def test_the_fit_starts_even_and_each_update_moves_the_ends_closer() -> None:
    starts = _bins_of([0, 100, 200, 300], [3, 2, 1])
    ends = _bins_of([10, 150, 300, 500], [2, 3, 1])
    none = fit_gap(starts, ends, 1, 400, iterations=0)
    some = fit_gap(starts, ends, 1, 400, iterations=GAP_FIT_ITERATIONS)
    assert len(set(none.weights)) == 1 or none.distance > some.distance
    assert some.distance < none.distance


def test_the_fit_recovers_a_known_gap() -> None:
    # Every start in [0, 100) and every gap in [200, 300): the ends fall in [200, 400).
    starts = _bins_of([0, 100], [1])
    ends = _bins_of([200, 300, 400], [1, 1])
    fitted = fit_gap(starts, ends, 1, 1000)
    inside = sum(
        weight
        for (low, high), weight in zip(
            zip(fitted.edges[:-1], fitted.edges[1:], strict=True), fitted.weights, strict=True
        )
        if low >= 100 and high <= 400
    )
    assert inside > Fraction(9, 10)
    assert fitted.distance < Fraction(1, 20)


def test_ends_that_rise_above_the_starts_report_their_gap() -> None:
    starts = _bins_of([100, 200], [1])
    ends = _bins_of([0, 100], [1])
    assert dominance_gap(starts, ends) == 1
    assert dominance_gap(ends, starts) == 0
    fitted = fit_gap(starts, ends, 1, 100)
    assert fitted.gap == 1


def test_the_longest_gap_without_leak_is_the_last_end_less_the_last_start() -> None:
    starts = _bins_of([0, 100, 200], [1, 1])
    ends = _bins_of([50, 400], [1])
    assert longest_without_leak(starts, ends) == 200


def test_a_start_histogram_with_no_room_for_a_gap_is_even() -> None:
    starts = _bins_of([0, 100], [1])
    ends = _bins_of([0, 100], [1])
    assert fit_gap(starts, ends, 1, 1).even


def test_timestamp_columns_derive_their_gaps_in_whole_days(tmp_path: Path) -> None:
    contract = (
        ACTIVATED.replace(
            "{ name: valid_from, type: DATE, required: true }",
            "{ name: valid_from, type: TIMESTAMP, required: true }",
        )
        .replace(
            "{ name: valid_to, type: DATE, required: false }",
            "{ name: valid_to, type: TIMESTAMP, required: false }",
        )
        .replace('openEnd: "9999-12-31"', 'openEnd: "9999-12-31T00:00:00.000Z"')
    )
    _init(tmp_path, _profile(), contract)
    entry = _entry(tmp_path)
    assert entry["length"]["bins"][0] == "1" and len(entry["length"]["bins"]) > 2
    assert all(text.isdigit() for text in entry["length"]["bins"])
    assert entry["end"] == {"mode": "closeLast", "share": "0.5"}


# ---------------------------------------------------------------------------
# What the history's end holds, and what it declines
# ---------------------------------------------------------------------------


def test_a_nullable_end_with_no_open_end_keeps_the_ends_past_the_window(tmp_path: Path) -> None:
    from tests.cli.test_init_verifies import _verified

    profile = _profile()
    same = _levels(["after", "before", "same"], [0, 0, 1000], 1000)
    profile["tables"]["tier_version"]["datePairs"] = [
        {"start": "valid_from", "end": "enrolled_on", "order": same, "statistics": []}
    ]
    # The contract's end is nullable and declares no open end, so the history is not written.
    _init(tmp_path, profile, TIERS)
    assert _namespace(tmp_path).get("history", []) == []
    assert _lifetime(tmp_path)["endPastWindow"] == "kept"
    verified = _verified(tmp_path / "bundle", tmp_path / "tiers.profile.json", tmp_path / "state")
    assert verified.exit_code == 0, verified.stdout + verified.stderr


def test_a_published_null_share_of_the_end_lowers_the_closed_share(tmp_path: Path) -> None:
    profile = _profile()
    profile["tables"]["tier_version"]["columns"]["valid_to"]["metrics"]["nullCount"] = {
        "definition": "nullCount/1",
        "status": "F",
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "value": 100,
    }
    _init(tmp_path, profile, ACTIVATED)
    # Half of the dated ends are closed, and some ends are NULL: fewer than half of all rows.
    assert _entry(tmp_path)["end"] == {"mode": "closeLast", "share": "0.4743589745"}


def test_a_declared_end_range_the_gaps_cannot_fill_declines_the_lifetime(tmp_path: Path) -> None:
    profile = _profile()
    column = profile["tables"]["tier_version"]["columns"]["valid_to"]
    del column["statistics"]
    column["metrics"]["max"]["value"] = _ms(2025, 10, 1)
    result = _init(tmp_path, profile, NO_OPEN_END)
    assert _lifetimes(tmp_path) == []
    assert "the declared range of 'valid_to' cannot be shown to hold every end" in result.stdout
