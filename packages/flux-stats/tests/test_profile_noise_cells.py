# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A noisy statistic that can move an entity between two cells is refused.

One changed row can move an entity between two cells of a statistic with ``per`` and of every
statistic of a ``history``, and the reader charges a sensitivity of 1 for a noisy statistic.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_stats.errors import FluxError
from flux_stats.profile import SCHEMA_URL_1_3_0, parse_profile

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _spec()
PRIVATE: Dict[str, Any] = json.loads((SPEC / "profile-1-2-private-example.json").read_text("utf-8"))
HISTORY: Dict[str, Any] = json.loads((SPEC / "profile-1-3-example.json").read_text("utf-8"))
NOISE = {"mechanism": "gaussian", "scale": "2.24", "privacyLoss": {"rho": "0.1"}}


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


def _noisy_per() -> Dict[str, Any]:
    document = copy.deepcopy(PRIVATE)
    document["_schemaURL"] = SCHEMA_URL_1_3_0
    statistic = document["tables"]["subscriber"]["columns"]["region"]["statistics"][0]
    statistic["per"] = ["plan"]
    return document


def test_the_noisy_example_without_per_still_reads() -> None:
    document = copy.deepcopy(PRIVATE)
    document["_schemaURL"] = SCHEMA_URL_1_3_0
    parse_profile(document)


def test_a_noisy_statistic_with_per_is_refused() -> None:
    message = _refused(_noisy_per())
    assert "noise" in message and "per" in message
    assert "between two cells" in message and "sensitivity of 1" in message


def _linked(parent: Dict[str, Any], column: str, noisy: bool) -> Dict[str, Any]:
    """The noisy example, its first statistic grouped by a link of the given parent."""
    document = copy.deepcopy(PRIVATE)
    document["_schemaURL"] = SCHEMA_URL_1_3_0
    table = document["tables"]["subscriber"]
    table["columns"]["id"] = {"domain": "category"}
    table["columns"]["referrer"] = {"domain": "category"}
    statistic = table["columns"]["region"]["statistics"][0]
    statistic["groupByLink"] = {"parent": parent, "columns": [column], "column": "channel"}
    statistic["groups"] = [
        {"group": "online", "status": "F", "noisyCounts": ["3000", "2000", "1500"]},
        {"group": "store", "status": "F", "noisyCounts": ["2999.5", "2501", "2000.5"]},
    ]
    if not noisy:
        del statistic["noise"]
        table.pop("cliques", None)
        document.pop("privacy", None)
        for group in statistic["groups"]:
            group["levelCounts"] = [int(float(n)) for n in group.pop("noisyCounts")]
            group["k"] = 3000
    return document


SELF_LINK = {"table": "subscriber", "columns": ["id"]}
PARENT_LINK = {"table": "plan", "columns": ["id"]}


@pytest.mark.parametrize("parent", [SELF_LINK, PARENT_LINK], ids=["self-link", "parent-link"])
def test_a_noisy_statistic_with_group_by_link_is_refused(parent: Dict[str, Any]) -> None:
    document = _linked(parent, "referrer", noisy=True)
    if parent is PARENT_LINK:
        document["tables"]["plan"] = {
            "columns": {"id": {"domain": "category"}, "channel": {"domain": "category"}}
        }
    message = _refused(document)
    assert "tables.subscriber.columns.region.statistics[0].noise" in message
    assert "groupByLink" in message and "sensitivity of 1" in message


def test_a_statistic_with_group_by_link_and_no_noise_still_reads() -> None:
    document = _linked(SELF_LINK, "referrer", noisy=False)
    statistic = document["tables"]["subscriber"]["columns"]["region"]["statistics"][0]
    assert "noise" not in statistic
    parse_profile(document)


def test_a_noisy_history_statistic_is_refused() -> None:
    document = copy.deepcopy(HISTORY)
    history = document["tables"]["plan_version"]["history"]
    history["versionsPerEntity"]["noise"] = NOISE
    message = _refused(document)
    assert "history.versionsPerEntity.noise" in message
    assert "between two cells" in message and "sensitivity of 1" in message


def test_a_noisy_tracked_column_statistic_of_a_history_is_refused() -> None:
    document = copy.deepcopy(HISTORY)
    history = document["tables"]["plan_version"]["history"]
    history["columns"]["tier"]["transitions"]["noise"] = NOISE
    assert "transitions.noise" in _refused(document)
