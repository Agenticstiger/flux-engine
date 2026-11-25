# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A date pair of a table never gives a second source to a column its history sets.

Pinned here, through ``run_init`` and the plan stage, on the tier history of a gym's members:

* a table's date pair on the history's validity start or end, or on its activation column, is
  recorded unused, and the history is written, with no ``lifetimes`` or ``within`` entry on those
  columns, whether the history comes from the profile or from ``--history FILE``;
* when the plan refuses the history, none is written, and the same date pairs declare as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages.plan import plan
from tests.cli.test_init_dates import HOLDS, _pair

from .test_history_init import NAMESPACE, _history, _metrics, _ms

pytestmark = pytest.mark.integration

HISTORY_REASON = "the table's history sets"

#: The tier a gym member holds over time. ``pass_id`` is the entity the versions follow;
#: ``enrolled_on`` is the activation column of a declared history.
TIERS = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: gym.tiers
name: Tiers
metadata:
  owner:
    team: gym-team
exposes:
  - exposeId: gym_member
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/gym_member.parquet }
    contract:
      schema:
        - { name: member_id, type: INT, required: true }
    semantics:
      entities:
        - { name: member, type: primary, expr: member_id }
  - exposeId: tier_version
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/tier_version.parquet }
    contract:
      schema:
        - { name: version_id, type: INT, required: true }
        - { name: pass_id, type: INT, required: true }
        - { name: member_id, type: INT, required: true }
        - { name: valid_from, type: DATE, required: true }
        - { name: valid_to, type: DATE, required: false }
        - { name: tier, type: STRING, required: true }
        - { name: channel, type: STRING, required: true }
        - { name: enrolled_on, type: DATE, required: true }
        - { name: renewed_on, type: DATE, required: false }
    semantics:
      entities:
        - { name: tier_version, type: primary, expr: version_id }
        - { name: pass, type: natural, expr: pass_id }
      dimensions:
        - { name: valid_from, type: time, typeParams: { validityParams: { isStart: true } } }
        - { name: valid_to, type: time, typeParams: { validityParams: { isEnd: true } } }
        - { name: tier, type: categorical }
        - { name: channel, type: categorical }
        - { name: enrolled_on, type: time }
        - { name: renewed_on, type: time }
      relationships:
        - name: member
          to: gym_member
          fromColumns: [member_id]
          toColumns: [member_id]
          cardinality: many_to_one
"""

#: The same contract with the member as the natural entity, which is also the relationship's
#: foreign key: a history lays its natural entity out on its own, so the plan refuses it.
REFUSED = TIERS.replace("expr: pass_id }", "expr: member_id }")


def _profile(*pairs: Dict[str, Any], entity: str = "pass_id") -> Dict[str, Any]:
    """The profile of ``TIERS``: a published history of ``tier_version`` and its *pairs*."""

    def column(low: str, high: str) -> Dict[str, Any]:
        return {"domain": "timestamp", "metrics": _metrics(low, high, 1850)}

    def number(high: str) -> Dict[str, Any]:
        return {"domain": "number", "metrics": _metrics("1", high, 1850)}

    history = _history()
    history["entity"] = [entity]
    version: Dict[str, Any] = {
        "rowCount": 1850,
        "columns": {
            "version_id": number("1850"),
            "pass_id": number("1000"),
            "member_id": number("1000"),
            "valid_from": column(_ms(2025, 1, 1), _ms(2025, 9, 1)),
            "valid_to": column(_ms(2025, 2, 1), _ms(2025, 10, 1)),
            "tier": {
                "domain": "category",
                "statistics": [
                    _history()["columns"]["tier"]["initial"],
                ],
            },
            "channel": {
                "domain": "category",
                "statistics": [
                    _history()["columns"]["channel"]["initial"],
                ],
            },
            "enrolled_on": column(_ms(2024, 6, 1), _ms(2025, 6, 1)),
            "renewed_on": column(_ms(2024, 7, 1), _ms(2025, 7, 1)),
        },
        "history": history,
    }
    if pairs:
        version["datePairs"] = list(pairs)
    return {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile/1-3-0/FluxProfile.json",
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {
            "none": {},
            "acro-default": {
                "dominance": {"k": "0.90", "n": 2},
                "minimumCell": 10,
                "pRatio": "0.10",
                "roundingBase": 5,
                "zerosDisclosive": True,
            },
        },
        "tables": {
            "gym_member": {"rowCount": 1000, "columns": {"member_id": number("1000")}},
            "tier_version": version,
        },
    }


def _init(
    tmp_path: Path, profile: Dict[str, Any], contract: str = TIERS, history: Optional[Path] = None
) -> Any:
    (tmp_path / "tiers.fluid.yml").write_text(contract, encoding="utf-8")
    (tmp_path / "tiers.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "tiers.fluid.yml"),
        profile=str(tmp_path / "tiers.profile.json"),
        history=None if history is None else str(history),
    )


def _block(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block


def _reasons(tmp_path: Path, pointer: str) -> list[str]:
    return [
        item["reason"]
        for item in _block(tmp_path)["unused"]
        if item["statistic"]["pointer"].startswith(pointer)
    ]


def _planned(tmp_path: Path) -> None:
    planned = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "tiers.profile.json",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr


def _history_file(tmp_path: Path) -> Path:
    """A history file that gives ``tier_version`` the activation column ``enrolled_on``."""
    document = {
        "timeBounds": {"start": "2024-01-01", "end": "2025-12-31"},
        "history": [
            {
                "emit": "tier_version",
                "entities": 1000,
                "versions": {"bins": ["1", "2", "4"], "binWeights": ["0.6", "0.4"]},
                "start": {"bins": ["2025-01-01", "2025-06-01"], "binWeights": ["1"]},
                "length": {
                    "bins": ["30", "90", "365"],
                    "binWeights": ["0.5", "0.5"],
                    "closed": "lower",
                },
                "until": "2025-12-31",
                "chain": "nextStart",
                "activation": "enrolled_on",
            }
        ],
    }
    path = tmp_path / "history.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_a_date_pair_on_the_validity_window_of_a_published_history_is_unused(
    tmp_path: Path,
) -> None:
    pair = _pair("valid_from", "valid_to", HOLDS)
    result = _init(tmp_path, _profile(pair))
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    assert "lifetimes" not in block and "within" not in block
    reasons = _reasons(tmp_path, "/tables/tier_version/datePairs/0")
    assert reasons and all(HISTORY_REASON in reason for reason in reasons)
    _planned(tmp_path)


def test_a_date_pair_on_the_activation_column_of_a_history_file_is_unused(tmp_path: Path) -> None:
    pair = _pair("enrolled_on", "valid_to", HOLDS)
    result = _init(tmp_path, _profile(pair), history=_history_file(tmp_path))
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    assert "lifetimes" not in block and "within" not in block
    reasons = _reasons(tmp_path, "/tables/tier_version/datePairs/0")
    assert reasons and all(HISTORY_REASON in reason for reason in reasons)
    _planned(tmp_path)


def test_a_date_pair_on_the_validity_window_of_a_history_file_is_unused(tmp_path: Path) -> None:
    pair = _pair("valid_from", "valid_to", HOLDS)
    result = _init(tmp_path, _profile(pair), history=_history_file(tmp_path))
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    assert "lifetimes" not in block
    reasons = _reasons(tmp_path, "/tables/tier_version/datePairs/0")
    assert reasons and all(HISTORY_REASON in reason for reason in reasons)


def test_a_date_pair_on_dates_the_history_leaves_alone_is_still_a_lifetime(tmp_path: Path) -> None:
    pair = _pair("enrolled_on", "renewed_on", HOLDS)
    result = _init(tmp_path, _profile(pair))
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    assert [(entry["start"], entry["end"]) for entry in block["lifetimes"]] == [
        ("enrolled_on", "renewed_on")
    ]
    _planned(tmp_path)


def test_a_history_the_plan_refuses_leaves_its_date_pairs_to_the_lifetime(tmp_path: Path) -> None:
    pair = _pair("valid_from", "valid_to", HOLDS)
    result = _init(tmp_path, _profile(pair, entity="member_id"), REFUSED)
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    assert "history" not in block
    assert block["lifetimes"] == [
        {
            "emit": "tier_version",
            "start": "valid_from",
            "end": "valid_to",
            "duration": {"bins": ["0", "1", "10", "30"], "binWeights": ["4", "3", "3"]},
        }
    ]
    assert _reasons(tmp_path, "/tables/tier_version/datePairs/0") == []
