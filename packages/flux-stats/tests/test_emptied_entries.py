# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An entry that holds only the keys that name it, and the reader's ``allow_emptied_entries``.

A release step that drops each statistic an inventory marks unused can leave a date pair, a
relationship's date pair, an exposure, a history or a tracked column of one with its names alone.
The strict reader refuses each (PROFILE-SPEC.md section 5). With the option the reader keeps the
entry in its place, holding nothing, and reads an entry the release cut in part with the members
left. The calendar entry has its own tests in ``test_calendar_exposure.py``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict

import pytest

from flux_stats.errors import FluxError
from flux_stats.profile import load_profile_text, parse_profile

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


EXAMPLE: Dict[str, Any] = json.loads((_spec() / "profile-1-3-example.json").read_text("utf-8"))


def _edit(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def _pair(document: Dict[str, Any]) -> Dict[str, Any]:
    return document["tables"]["payment"]["datePairs"][0]


def _link(document: Dict[str, Any]) -> Dict[str, Any]:
    return document["relationships"][0]["dates"][0]


def _exposure(document: Dict[str, Any]) -> Dict[str, Any]:
    return document["relationships"][0]["exposure"][0]


def _history(document: Dict[str, Any]) -> Dict[str, Any]:
    return document["tables"]["plan_version"]["history"]


def _column(document: Dict[str, Any]) -> Dict[str, Any]:
    return _history(document)["columns"]["tier"]


def _strip(node: Dict[str, Any], *keys: str) -> None:
    for key in keys:
        node.pop(key, None)


def _empty_pair(document: Dict[str, Any]) -> None:
    _pair(document)["statistics"] = []  # each statistic of the list dropped
    _strip(_pair(document), "order")


def _empty_link(document: Dict[str, Any]) -> None:
    _link(document)["statistics"] = []
    _strip(_link(document), "order")


def _empty_exposure(document: Dict[str, Any]) -> None:
    _strip(_exposure(document), "children", "activeDays")


def _empty_history(document: Dict[str, Any]) -> None:
    _strip(
        _history(document),
        "versionsPerEntity",
        "firstStart",
        "versionLength",
        "chain",
        "lastVersion",
        "reasons",
    )
    _history(document)["columns"] = {"tier": {}}


def _empty_column(document: Dict[str, Any]) -> None:
    _column(document).clear()
    _strip(_history(document), "reasons")


EMPTIED = [
    pytest.param(_empty_pair, id="date-pair"),
    pytest.param(_empty_link, id="relationship-date-pair"),
    pytest.param(_empty_exposure, id="exposure"),
    pytest.param(_empty_history, id="history"),
    pytest.param(_empty_column, id="tracked-column"),
]


@pytest.mark.parametrize("change", EMPTIED)
def test_an_entry_that_holds_nothing_is_refused_by_default(
    change: Callable[[Dict[str, Any]], None],
) -> None:
    for strict in (
        lambda document: parse_profile(document),
        lambda document: parse_profile(document, allow_emptied_entries=False),
        lambda document: load_profile_text(json.dumps(document)),
    ):
        with pytest.raises(FluxError) as refused:
            strict(_edit(change))
        assert refused.value.code.code == "FLX-5036"


@pytest.mark.parametrize("change", EMPTIED)
def test_loading_the_text_passes_the_option_on(change: Callable[[Dict[str, Any]], None]) -> None:
    profile, _ = load_profile_text(json.dumps(_edit(change)), allow_emptied_entries=True)
    assert profile.tables["plan_version"].history is not None


def test_a_date_pair_that_holds_nothing_stays_in_its_place() -> None:
    def change(document: Dict[str, Any]) -> None:
        pairs = document["tables"]["payment"]["datePairs"]
        pairs.insert(0, {"start": pairs[0]["end"], "end": pairs[0]["start"], "statistics": []})

    profile = parse_profile(_edit(change), allow_emptied_entries=True)
    empty, kept = profile.tables["payment"].date_pairs
    assert (empty.start, empty.end) == (kept.end, kept.start)
    assert empty.statistics == () and empty.order is None and empty.holds_nothing
    # The entry after it keeps its place, and what it holds.
    assert kept.order is not None and kept.statistics and not kept.holds_nothing


def test_a_relationship_date_pair_that_holds_nothing_is_kept() -> None:
    document = _edit(_empty_link)
    profile = parse_profile(document, allow_emptied_entries=True)
    (empty,) = profile.relationships[0].dates
    assert (empty.parent, empty.child) == (_link(document)["parent"], _link(document)["child"])
    assert empty.statistics == () and empty.order is None and empty.holds_nothing


def test_an_exposure_that_holds_nothing_keeps_its_columns_and_its_window() -> None:
    document = _edit(_empty_exposure)
    window = _exposure(document)["window"]
    profile = parse_profile(document, allow_emptied_entries=True)
    (empty,) = profile.relationships[0].exposure
    assert empty.start == "joined_at" and empty.end is None
    assert empty.children is None and empty.active_days is None and empty.holds_nothing
    assert (str(empty.window_start), str(empty.window_end)) == (window["start"], window["end"])


def test_a_history_that_holds_nothing_names_its_columns_and_its_tracked_columns() -> None:
    profile = parse_profile(_edit(_empty_history), allow_emptied_entries=True)
    history = profile.tables["plan_version"].history
    assert history is not None and history.holds_nothing
    assert history.entity and history.start and history.end
    assert history.versions_per_entity is None and history.chain is None
    assert history.first_start is None and history.last_version is None
    assert history.version_length is None and history.reasons is None
    assert set(history.columns) == {"tier"} and history.columns["tier"].holds_nothing


def test_a_tracked_column_that_holds_nothing_leaves_the_history_as_it_was() -> None:
    profile = parse_profile(_edit(_empty_column), allow_emptied_entries=True)
    history = profile.tables["plan_version"].history
    assert history is not None and not history.holds_nothing
    assert history.chain is not None and history.reasons is None
    column = history.columns["tier"]
    assert column.initial is None and column.transitions is None and column.holds_nothing


def test_a_history_without_columns_that_holds_nothing_is_read_too() -> None:
    def change(document: Dict[str, Any]) -> None:
        _empty_history(document)
        del _history(document)["columns"]

    history = (
        parse_profile(_edit(change), allow_emptied_entries=True).tables["plan_version"].history
    )
    assert history is not None and history.holds_nothing and not history.columns


def _drop(
    path: Callable[[Dict[str, Any]], Dict[str, Any]], key: str
) -> Callable[[Dict[str, Any]], None]:
    return lambda document: path(document).pop(key)


def _empty_all_but(
    path: Callable[[Dict[str, Any]], Dict[str, Any]], key: str
) -> Callable[[Dict[str, Any]], None]:
    def change(document: Dict[str, Any]) -> None:
        node = path(document)
        kept = node[key]
        for member in ("statistics", "order", "children", "activeDays"):
            node.pop(member, None)
        node[key] = kept

    return change


PARTIAL = [
    pytest.param(_empty_all_but(_pair, "statistics"), "order", id="pair-statistics-no-order"),
    pytest.param(_empty_all_but(_pair, "order"), "statistics", id="pair-order-no-statistics"),
    pytest.param(_empty_all_but(_link, "statistics"), "order", id="link-statistics-no-order"),
    pytest.param(_empty_all_but(_link, "order"), "statistics", id="link-order-no-statistics"),
    pytest.param(_empty_all_but(_exposure, "children"), "activeDays", id="exposure-one-count"),
    pytest.param(_empty_all_but(_exposure, "activeDays"), "children", id="exposure-other-count"),
    pytest.param(_drop(_history, "chain"), "chain", id="history-no-chain"),
    pytest.param(_drop(_history, "lastVersion"), "lastVersion", id="history-no-last-version"),
    pytest.param(_drop(_column, "initial"), "initial", id="column-no-initial"),
    pytest.param(_drop(_column, "transitions"), "transitions", id="column-no-transitions"),
]


@pytest.mark.parametrize(("change", "key"), PARTIAL)
def test_an_entry_a_release_cut_is_read_with_the_members_left(
    change: Callable[[Dict[str, Any]], None], key: str
) -> None:
    # A release that drops each unused statistic can leave part of an entry: the strict reader
    # refuses it, naming the member it misses, and the engine reads the members that are left.
    with pytest.raises(FluxError) as refused:
        parse_profile(_edit(change))
    assert key in str(refused.value)
    parse_profile(_edit(change), allow_emptied_entries=True)


def test_a_history_that_holds_only_a_tracked_column_is_read_with_it() -> None:
    def change(document: Dict[str, Any]) -> None:
        columns = copy.deepcopy(_history(document)["columns"])
        _empty_history(document)
        _history(document)["columns"] = columns

    with pytest.raises(FluxError, match="chain|versionsPerEntity|firstStart"):
        parse_profile(_edit(change))
    history = (
        parse_profile(_edit(change), allow_emptied_entries=True).tables["plan_version"].history
    )
    assert history is not None and history.chain is None and history.versions_per_entity is None
    assert history.columns and all(
        column.initial is not None and column.transitions is not None
        for column in history.columns.values()
    )


def _then(
    emptied: Callable[[Dict[str, Any]], None], change: Callable[[Dict[str, Any]], Any]
) -> Callable[[Dict[str, Any]], None]:
    """*emptied*, then *change*: an entry that holds nothing, with one more thing wrong."""

    def both(document: Dict[str, Any]) -> None:
        emptied(document)
        change(document)

    return both


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        pytest.param(
            _then(_empty_pair, lambda d: _pair(d).update(extra=1)), "extra", id="pair-unknown-key"
        ),
        pytest.param(
            _then(_empty_pair, lambda d: _pair(d).update(end=_pair(d)["start"])),
            "two different columns",
            id="pair-one-column",
        ),
        pytest.param(
            _then(_empty_pair, lambda d: _pair(d).update(end="amount")),
            "datePairs[0].end",
            id="pair-not-a-timestamp",
        ),
        pytest.param(
            _then(_empty_link, lambda d: _link(d).update(child="amount")),
            "dates[0].child",
            id="link-not-a-timestamp",
        ),
        pytest.param(
            _then(_empty_exposure, lambda d: _exposure(d).pop("window")),
            "window",
            id="exposure-no-window",
        ),
        pytest.param(
            _then(_empty_exposure, lambda d: _exposure(d).update(start="amount")),
            "exposure[0].start",
            id="exposure-not-a-timestamp",
        ),
        pytest.param(
            _then(_empty_history, lambda d: _history(d).pop("entity")),
            "entity",
            id="history-no-entity",
        ),
        pytest.param(
            _then(_empty_history, lambda d: _history(d).update(extra=1)),
            "extra",
            id="history-unknown-key",
        ),
        pytest.param(
            _then(_empty_column, lambda d: _column(d).update(extra=1)),
            "extra",
            id="column-unknown-key",
        ),
        pytest.param(
            _then(_empty_history, lambda d: _history(d)["columns"].update(amount={})),
            "tracked column",
            id="column-not-categorical",
        ),
    ],
)
def test_the_option_loosens_no_other_rule_of_an_entry(
    change: Callable[[Dict[str, Any]], Any], fragment: str
) -> None:
    with pytest.raises(FluxError) as refused:
        parse_profile(_edit(change), allow_emptied_entries=True)
    assert fragment in str(refused.value)


def test_an_entry_a_release_cut_in_part_does_not_hold_nothing() -> None:
    def cut(document: Dict[str, Any]) -> None:
        _strip(_pair(document), "order")
        _strip(_link(document), "order")
        _strip(_history(document), "versionsPerEntity")

    profile = parse_profile(_edit(cut), allow_emptied_entries=True)
    (pair,) = profile.tables["payment"].date_pairs
    assert pair.order is None and pair.statistics and not pair.holds_nothing
    (link,) = profile.relationships[0].dates
    assert link.order is None and link.statistics and not link.holds_nothing
    history = profile.tables["plan_version"].history
    assert history is not None and history.versions_per_entity is None
    assert history.first_start is not None and not history.holds_nothing


def test_a_date_pair_left_with_only_its_order_does_not_hold_nothing() -> None:
    def cut(document: Dict[str, Any]) -> None:
        _pair(document)["statistics"] = []

    (pair,) = parse_profile(_edit(cut), allow_emptied_entries=True).tables["payment"].date_pairs
    assert pair.statistics == () and pair.order is not None and not pair.holds_nothing


def test_a_history_left_with_only_its_reasons_does_not_hold_nothing() -> None:
    def cut(document: Dict[str, Any]) -> None:
        reasons = _history(document)["reasons"]
        _empty_history(document)
        _history(document)["reasons"] = reasons

    history = parse_profile(_edit(cut), allow_emptied_entries=True).tables["plan_version"].history
    assert history is not None and history.reasons is not None and not history.holds_nothing
