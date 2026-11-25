# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Profile format 1-5-0: a noisy history and a noisy null count (PROFILE-SPEC.md section 3a).

``docs/spec/profile-1-5-history-example.json`` holds a history on ``card_version``, a table the unit
of privacy follows into at a cap of 4, and a ``nullCount/2`` on a column of the unit's expose and
one on a column of ``card_version``. Each rule has a test: what is accepted, what is charged
(``m_T`` for every noisy member and for a null count), and each refusal, which is ``FLX-5036`` and
names where.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict

import pytest

from flux_stats.errors import FluxError
from flux_stats.profile import (
    NULL_COUNT_LEVELS,
    SCHEMA_URL_1_4_0,
    SCHEMA_URL_1_5_0,
    parse_profile,
)

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _spec()
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-1-5-history-example.json").read_text("utf-8"))
HISTORY_AT = "profile.tables.card_version.history"


def _edit(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def _history(document: Dict[str, Any]) -> Dict[str, Any]:
    return document["tables"]["card_version"]["history"]


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


def _null_count(document: Dict[str, Any], table: str, column: str) -> Dict[str, Any]:
    return document["tables"][table]["columns"][column]["statistics"][0]


# --- accepted ------------------------------------------------------------------


def test_the_example_reads_with_its_noisy_members_and_null_counts() -> None:
    profile = parse_profile(EXAMPLE)
    entry = profile.tables["card_version"].history
    assert entry is not None
    assert entry.versions_per_entity.noise is not None
    assert entry.chain.noise is not None
    tier = entry.columns["tier"]
    assert tier.initial.noise is not None and tier.transitions.noise is not None
    assert len(profile.parts) == 11  # six members, two null counts, three queries
    counts = profile.tables["member"].columns["segment"].null_counts
    assert len(counts) == 1 and NULL_COUNT_LEVELS == ("known", "null")
    assert counts[0].group.noisy == (Fraction("4412.6"), Fraction("587.9"))


def test_a_history_may_mix_noisy_and_exact_members() -> None:
    entry = parse_profile(EXAMPLE).tables["card_version"].history
    assert entry is not None
    assert entry.version_length.noise is None  # the example states this one exact
    assert entry.first_start.noise is not None


def test_a_noisy_group_of_a_history_is_read_as_a_noisy_group() -> None:
    entry = parse_profile(EXAMPLE).tables["card_version"].history
    assert entry is not None
    group = entry.last_version.groups[0]
    assert group.noisy == (Fraction("1190.8"), Fraction("3702.1"))
    assert group.statistic is not None and sum(group.statistic.level_counts) == group.statistic.k


# --- the charge ----------------------------------------------------------------


def test_a_noisy_history_member_is_charged_the_reach_of_its_table() -> None:
    """m_T is 4 (cap 4 from the expose): scale 8 needs rho 16 / 128 = 1/8, and 1/128 is refused."""
    assert parse_profile(EXAMPLE).accounting.reach("card_version") == 4  # type: ignore[union-attr]

    def understate(document: Dict[str, Any]) -> None:
        _history(document)["chain"]["noise"]["privacyLoss"]["rho"] = "0.0078125"

    message = _refused(_edit(understate))
    assert f"{HISTORY_AT}.chain.noise" in message


def test_a_null_count_is_charged_the_reach_of_its_table() -> None:
    def understate(document: Dict[str, Any]) -> None:
        _null_count(document, "card_version", "valid_to")["noise"]["privacyLoss"]["rho"] = "0.01"

    assert "valid_to.statistics[0].noise" in _refused(_edit(understate))

    def on_the_expose(document: Dict[str, Any]) -> None:
        # the unit's own expose has m = 1: scale 2 needs 1/8, scale 8 needs only 1/128
        statistic = _null_count(document, "member", "segment")
        statistic["noise"]["scale"] = "8"
        statistic["noise"]["privacyLoss"]["rho"] = "0.0078125"

    parse_profile(_edit(on_the_expose))


def test_every_new_share_adds_to_the_budget() -> None:
    def overspend(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyLoss"]["rho"] = "0.9"

    assert "privacy" in _refused(_edit(overspend))


# --- refusals of a noisy history -------------------------------------------------


def test_a_noisy_member_in_a_table_the_unit_does_not_reach_is_refused() -> None:
    def unreach(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyUnit"] = {"expose": "member"}
        document["tables"]["member"]["columns"]["segment"]["statistics"] = []
        document["tables"]["card_version"]["columns"]["valid_to"]["statistics"] = []

    message = _refused(_edit(unreach))
    assert f"{HISTORY_AT}." in message and "noise" in message


def test_a_noisy_member_in_the_units_own_expose_is_refused() -> None:
    def history_on_the_expose(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyUnit"] = {"expose": "card_version"}
        del document["tables"]["member"]
        document["relationships"] = []
        document["tables"]["card_version"]["columns"]["valid_to"]["statistics"] = []

    message = _refused(_edit(history_on_the_expose))
    assert f"{HISTORY_AT}." in message and "own expose" in message


def test_a_noisy_member_names_itself_and_no_value() -> None:
    def unreach(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyUnit"] = {"expose": "member"}
        document["tables"]["member"]["columns"]["segment"]["statistics"] = []
        document["tables"]["card_version"]["columns"]["valid_to"]["statistics"] = []

    message = _refused(_edit(unreach))
    assert f"{HISTORY_AT}." in message
    for value in ("2100.4", "1604", "3120.3"):
        assert value not in message


def test_a_noisy_history_in_a_document_of_an_earlier_format_is_refused() -> None:
    document = _edit(lambda d: d.update(_schemaURL=SCHEMA_URL_1_4_0))
    document["tables"]["member"]["columns"]["segment"]["statistics"] = []
    document["tables"]["card_version"]["columns"]["valid_to"]["statistics"] = []
    message = _refused(document)
    assert "format 1-5-0" in message and HISTORY_AT in message


def test_a_noisy_transitions_publishes_every_current_value_group() -> None:
    def drop(document: Dict[str, Any]) -> None:
        _history(document)["columns"]["tier"]["transitions"]["groups"].pop()

    assert "transitions.groups" in _refused(_edit(drop))


def test_a_noisy_history_member_still_holds_no_per_and_no_link() -> None:
    def per(document: Dict[str, Any]) -> None:
        _history(document)["chain"]["per"] = ["member_id"]

    assert HISTORY_AT in _refused(_edit(per))


# --- nullCount/2 -----------------------------------------------------------------


def test_a_null_count_without_noise_is_refused() -> None:
    def strip(document: Dict[str, Any]) -> None:
        del _null_count(document, "member", "segment")["noise"]

    assert "noise block only" in _refused(_edit(strip))


def test_a_null_count_names_its_levels_and_has_two_cells() -> None:
    def levels(document: Dict[str, Any]) -> None:
        _null_count(document, "member", "segment")["levels"] = ["known", "unknown"]

    def cells(document: Dict[str, Any]) -> None:
        _null_count(document, "member", "segment")["groups"][0]["noisyCounts"].append("1")

    assert "segment.statistics[0].levels" in _refused(_edit(levels))
    assert "noisyCounts" in _refused(_edit(cells))


def test_a_null_count_is_ungrouped() -> None:
    def keyed(document: Dict[str, Any]) -> None:
        _null_count(document, "member", "segment")["groups"][0]["group"] = "west"

    def grouped(document: Dict[str, Any]) -> None:
        _null_count(document, "member", "segment")["groupBy"] = "id"

    assert "ungrouped" in _refused(_edit(keyed))
    assert "groupBy" in _refused(_edit(grouped))


def test_a_null_count_of_a_table_the_unit_does_not_reach_is_refused() -> None:
    def unreach(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyUnit"] = {"expose": "member"}
        document["tables"]["card_version"].pop("history")

    message = _refused(_edit(unreach))
    assert "valid_to.statistics[0].noise" in message


def test_a_null_count_of_any_domain_is_read_and_a_second_one_is_refused() -> None:
    profile = parse_profile(EXAMPLE)
    domains = {
        profile.tables["member"].columns["segment"].domain,
        profile.tables["card_version"].columns["valid_to"].domain,
    }
    assert domains == {"category", "timestamp"}

    def twice(document: Dict[str, Any]) -> None:
        statistics = _null_count(document, "member", "segment")
        document["tables"]["member"]["columns"]["segment"]["statistics"] = [statistics, statistics]

    assert "repeats" in _refused(_edit(twice))


def test_a_null_count_in_a_document_of_an_earlier_format_is_refused() -> None:
    def older(document: Dict[str, Any]) -> None:
        document["_schemaURL"] = SCHEMA_URL_1_4_0
        document["tables"]["card_version"].pop("history")
        _null_count(document, "card_version", "valid_to")  # kept: one is enough

    message = _refused(_edit(older))
    assert "nullCount/2" in message and "1-5-0" in message


# --- documents of earlier formats read as before ---------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "profile-1-4-example.json",
        "profile-1-4-cliques-example.json",
        "profile-1-4-follows-example.json",
    ],
)
def test_a_1_4_0_document_reads_as_before(name: str) -> None:
    document = json.loads((SPEC / name).read_text("utf-8"))
    assert document["_schemaURL"] == SCHEMA_URL_1_4_0
    profile = parse_profile(document)
    assert all(
        not column.null_counts
        for table in profile.tables.values()
        for column in table.columns.values()
    )


def test_the_format_url_is_the_one_the_schema_names() -> None:
    schema = json.loads((SPEC / "profile-1-5.schema.json").read_text("utf-8"))
    assert schema["$id"] == SCHEMA_URL_1_5_0 == EXAMPLE["_schemaURL"]
