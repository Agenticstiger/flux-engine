# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A profile's budget is enumerated once: the reader's parts are the account's parts (ADR 0020).

The grade used to list the noisy parts of a profile by hand, so a statistic the list did not know
(a rank correlation, say) was missing from the spent figure, and a profile whose only noisy
statistics were those graded P1 with "an account spent by nothing". It now counts the reader's own
``parts``. These tests walk the document generically and check that every ``noise`` block is one part,
whichever place it sits in, and that a ``cliqueMarginal/2`` is one part like any clique.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from flux_engine.core.grades import Pattern
from flux_engine.evidence.grades import accounting_of, grade_profile
from flux_stats.profile import parse_profile

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "docs" / "spec"
TELCO = REPO / "tests" / "fixtures" / "private-telco" / "telco.profile.json"
MONTHS = [f"{n:02d}" for n in range(1, 13)]
NOISE = {"mechanism": "gaussian", "scale": "10", "privacyLoss": {"rho": "0.05"}}


def _load(path: Path) -> Dict[str, Any]:
    document: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _noise_blocks(node: Any) -> Iterator[Any]:
    """Every value stored under a ``noise`` key, anywhere in a JSON document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "noise" and isinstance(value, dict):
                yield value
            yield from _noise_blocks(value)
    elif isinstance(node, list):
        for item in node:
            yield from _noise_blocks(item)


def _histogram(levels: List[str], **more: Any) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "levels": levels,
        "sampleSize": {"min": 1000, "max": 9999},
        "disclosure": "noisy",
        "noise": copy.deepcopy(NOISE),
        "groups": [
            {"group": "", "status": "F", "noisyCounts": [str(100 + n) for n in range(len(levels))]}
        ],
    } | more


def _rank(definition: str, value: str) -> Dict[str, Any]:
    return {
        "definition": definition,
        "rowFloor": 99,
        "sampleSize": {"min": 1000, "max": 9999},
        "disclosure": "noisy",
        "noise": {"mechanism": "gaussian", "scale": "0.5", "privacyLoss": {"rho": "0.0162"}},
        "groups": [{"group": "", "status": "F", "noisyValue": value}],
    }


def _everywhere() -> Dict[str, Any]:
    """A 1-4-0 profile with a noise block in every place one can sit today."""
    document = _load(SPEC / "profile-1-4-example.json")
    document["privacy"]["privacyLoss"] = {"rho": "5"}
    table = document["tables"]["subscriber"]
    columns = table["columns"]
    columns["plan"] = {"domain": "category"}
    columns["channel"] = {"domain": "category"}
    columns["joined_at"] = {"domain": "timestamp"}
    columns["left_at"] = {"domain": "timestamp"}
    columns["usage"] = {"domain": "number"}
    # a grouped histogram: one noise block for all of its groups
    columns["plan"]["statistics"] = [
        _histogram(
            ["basic", "pro"],
            groupBy="region",
            groups=[
                {"group": region, "status": "F", "noisyCounts": ["10", "20"]}
                for region in ("east", "north", "west")
            ],
        )
    ]
    columns["usage"]["statistics"] = [
        {
            "definition": "marginalHistogram/1",
            "bins": ["0", "50", "100"],
            "sampleSize": {"min": 1000, "max": 9999},
            "disclosure": "noisy",
            "noise": copy.deepcopy(NOISE),
            "groups": [{"group": "", "status": "F", "noisyCounts": ["400", "600"]}],
        }
    ]
    table["cliques"] = [
        {
            "definition": "cliqueMarginal/1",
            "columns": ["plan", "channel"],
            "levels": [["basic", "pro"], ["online", "store"]],
            "noisyCounts": ["10", "20", "30", "40"],
            "noise": copy.deepcopy(NOISE),
        }
    ]
    table["cliques"].append(
        {
            "definition": "cliqueMarginal/2",
            "columns": ["plan", "usage"],
            "domains": [{"levels": ["basic", "pro"]}, {"bins": ["0", "50", "100"]}],
            "noisyCounts": ["10", "20", "30", "40"],
            "noise": copy.deepcopy(NOISE),
        }
    )
    table["calendar"] = [{"column": "joined_at", "month": _histogram(MONTHS)}]
    table["datePairs"] = [
        {
            "start": "joined_at",
            "end": "left_at",
            "statistics": [],
            "order": _histogram(["after", "before", "same"]),
        }
    ]
    table["columnPairs"].append(
        {"columns": ["age", "usage"], "statistics": [_rank("kendallTau/1", "0.31")]}
    )
    return document


FIXTURES = {
    "the telco fixture": lambda: _load(TELCO),
    "the private 1-2-0 example": lambda: _load(SPEC / "profile-1-2-private-example.json"),
    "the 1-4-0 example, with two rank correlations": lambda: _load(
        SPEC / "profile-1-4-example.json"
    ),
    "the 1-4-0 example of cliques": lambda: _load(SPEC / "profile-1-4-cliques-example.json"),
    "every place a noise block sits": _everywhere,
}


@pytest.mark.parametrize("name", FIXTURES)
def test_every_noise_block_is_one_part_of_the_reader_and_of_the_account(name: str) -> None:
    document = FIXTURES[name]()
    blocks = list(_noise_blocks(document))
    profile = parse_profile(document)
    assert len(profile.parts) == len(blocks) > 0
    accounting = accounting_of(document)
    assert accounting is not None and accounting.parts == len(blocks)
    # the places are distinct, so no block is entered twice
    assert len({where for where, _ in profile.parts}) == len(profile.parts)


def test_the_everywhere_profile_holds_a_part_in_every_kind_of_place() -> None:
    places = [where for where, _ in parse_profile(_everywhere()).parts]
    for section in (
        "columns.region",
        "columns.plan",
        "columns.usage",
        "cliques[0]",
        "cliques[1]",
        "calendar",
    ):
        assert any(f".{section}" in where for where in places), section
    assert sum(".columnPairs[" in where for where in places) == 3
    assert any(".datePairs[0]" in where for where in places)
    assert len(places) == 10


def test_the_spent_figure_adds_the_share_of_every_part_including_a_rank_correlation() -> None:
    document = _load(SPEC / "profile-1-4-example.json")
    accounting = accounting_of(document)
    assert accounting is not None
    # the histogram's 0.1, the Kendall's 0.02 and the Spearman's 0.0162
    assert accounting.parts == 3 and accounting.spent == {"rho": "0.1362"}
    assert accounting.total == {"rho": "0.15"}


def test_a_profile_whose_only_noisy_statistics_are_rank_correlations_is_p3() -> None:
    document = _load(SPEC / "profile-1-4-example.json")
    del document["tables"]["subscriber"]["columns"]["region"]
    document["privacy"]["privacyLoss"] = {"rho": "0.05"}
    graded = grade_profile(document, pinned=True)
    assert graded.accounting is not None and graded.accounting.parts == 2
    assert graded.reasons == () and graded.pattern is Pattern.P3


def test_the_spent_figure_of_every_place_adds_up() -> None:
    accounting = accounting_of(_everywhere())
    assert accounting is not None and accounting.parts == 10
    # the example's 0.1362, six blocks of 0.05 (two histograms, two cliques, a calendar, an order)
    # and the second pair's Kendall's 0.0162
    assert accounting.spent == {"rho": "0.4524"}


def test_a_profile_the_reader_refuses_has_no_parts_and_is_not_p3() -> None:
    document = _everywhere()
    document["tables"]["subscriber"]["columnPairs"][0]["statistics"].append(
        copy.deepcopy(document["tables"]["subscriber"]["columnPairs"][0]["statistics"][0])
    )
    accounting = accounting_of(document)
    assert accounting is not None and accounting.parts == 0 and accounting.spent == {"rho": "0"}
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P1
    assert any("refuses the profile" in reason for reason in graded.reasons)
