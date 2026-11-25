# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` reads a release's noisy history and noisy null counts (profile format 1-5-0).

A history member released with noise is read through its point estimate, and only its cells that
stand clear of the noise (:func:`flux_stats.noise.noise_floor` over all its cells) carry weight; a
member with no cell clear of it says nothing and is read as withheld. A ``nullCount/2`` gives a
nullable column its share of NULL rows when its NULL cell stands clear of the noise. Otherwise the
release cannot tell the count from none, and no null is drawn, as for a null count withheld as
small, with an assumption that names the interval the release allows. A column a clique draws holds
no null, so a released share of one is set aside (ADR 0019, amendments of 2026-11-25).
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pytest
import yaml

import tests.author.test_cliques as private_cliques
import tests.author.test_history_date_pairs as tiers
import tests.author.test_init_noise_floor as library
from flux_engine.author.columns import _share_text
from flux_engine.cli._init import run_init
from flux_engine.stages.plan import plan
from flux_stats.noise import noise_floor
from flux_stats.profile import SCHEMA_URL_1_5_0

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
SCALE = "10"


def _noisy(statistic: Dict[str, Any], tiny: Iterable[int] = ()) -> Dict[str, Any]:
    """*statistic* released with Gaussian noise: its counts as noisy values, those at *tiny* as 1."""
    found = copy.deepcopy(statistic)
    small = set(tiny)
    for group in found["groups"]:
        key = "levelCounts" if "levelCounts" in group else "binCounts"
        counts = group.pop(key)
        group.pop("k", None)
        group.pop("cellStatus", None)
        group["noisyCounts"] = ["1" if i in small else str(c) for i, c in enumerate(counts)]
    found["disclosure"] = "noisy"
    found["noise"] = {"mechanism": "gaussian", "scale": SCALE, "privacyLoss": {"rho": "0.5"}}
    return found


def _released_tiers(
    tiny_member: Optional[str] = None, tiny_cells: Iterable[int] = ()
) -> Dict[str, Any]:
    """The tiers' profile released with the gym's members as the unit, its history noisy."""
    profile = tiers._profile()
    profile["_schemaURL"] = SCHEMA_URL_1_5_0
    profile["disclosureRules"] = {**profile["disclosureRules"], "noisy": {}}
    link = {
        "parent": {"table": "gym_member", "columns": ["member_id"]},
        "child": {"table": "tier_version", "columns": ["member_id"]},
    }
    links = profile.setdefault("relationships", [])
    if not any(
        item.get("parent") == link["parent"] and item.get("child") == link["child"]
        for item in links
    ):
        links.append({**link, "statistics": []})
    profile["privacy"] = {
        "measure": "zeroConcentratedDP",
        "privacyLoss": {"rho": "10"},
        "privacyUnit": {"expose": "gym_member", "follows": [{**link, "maxChildren": 10}]},
    }
    history = profile["tables"]["tier_version"]["history"]
    for key in ("versionsPerEntity", "firstStart", "versionLength", "chain", "lastVersion"):
        if key in history:
            history[key] = _noisy(history[key], tiny_cells if key == tiny_member else ())
    for column in history.get("columns", {}).values():
        for key in ("initial", "transitions"):
            if key in column:
                column[key] = _noisy(column[key])
    return profile


def _init(tmp_path: Path, profile: Dict[str, Any], contract: str) -> Any:
    (tmp_path / "c.fluid.yaml").write_text(contract, encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "c.fluid.yaml"),
        profile=str(tmp_path / "p.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _block(tmp_path: Path) -> Dict[str, Any]:
    document = yaml.safe_load(
        (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    )
    block: Dict[str, Any] = document["extensions"][NAMESPACE]
    return block


def _plans(tmp_path: Path) -> None:
    planned = plan(
        tmp_path / "bundle", state_dir=tmp_path / "state", profile=tmp_path / "p.json", stage=False
    )
    assert planned.success, planned.stderr


def test_a_noisy_history_is_written_from_its_point_estimates(tmp_path: Path) -> None:
    result = _init(tmp_path, _released_tiers(), tiers.TIERS)
    (history,) = _block(tmp_path)["history"]
    assert history["versions"]["binWeights"] == ["0.4", "0.35", "0.25"]
    assert history["chain"] == "nextStart"
    assert "/tables/tier_version/history/chain: gaussian, scale 10, variance 100" in result.stdout
    _plans(tmp_path)


def test_a_cell_inside_the_noise_carries_no_weight(tmp_path: Path) -> None:
    # The second bin of the versions per entity is released as 1, inside a floor of about 37.
    profile = _released_tiers("versionsPerEntity", tiny_cells=[1])
    floor = noise_floor("gaussian", Fraction(SCALE), 3)
    assert 1 < floor < 100
    _init(tmp_path, profile, tiers.TIERS)
    (history,) = _block(tmp_path)["history"]
    weights = history["versions"]["binWeights"]
    assert weights[1] == "0" and weights[0] != "0" and weights[2] != "0"
    _plans(tmp_path)


def test_a_member_with_no_cell_clear_of_the_noise_is_read_as_withheld(tmp_path: Path) -> None:
    result = _init(tmp_path, _released_tiers("chain", tiny_cells=range(4)), tiers.TIERS)
    assert "history" not in _block(tmp_path)
    assert "/tables/tier_version/history: the versions do not chain" in result.stdout
    _plans(tmp_path)


LIBRARY_CONTRACT = library.CONTRACT.replace(
    "        - { name: joined_on, type: DATE, required: true }\n",
    "        - { name: joined_on, type: DATE, required: true }\n"
    "        - { name: left_on, type: DATE, required: false }\n",
)


def _with_null_count(known: str, null: str) -> Dict[str, Any]:
    """The library released with a noisy count of its members' known and NULL leaving days."""
    profile = library._profile([str(80 + month) for month in range(12)], None)
    profile["_schemaURL"] = SCHEMA_URL_1_5_0
    profile["tables"]["member"]["columns"]["left_on"] = {
        "domain": "timestamp",
        "statistics": [
            {
                "definition": "nullCount/2",
                "sampleSize": library.SAMPLE,
                "disclosure": "noisy",
                "levels": ["known", "null"],
                "noise": library._noise("2.5"),
                "groups": [{"group": "", "status": "F", "noisyCounts": [known, null]}],
            }
        ],
    }
    return profile


def _left_on(tmp_path: Path) -> Dict[str, Any]:
    column: Dict[str, Any] = _block(tmp_path)["emits"]["member"]["fidelity"]["columns"]["left_on"]
    return column


def test_a_null_count_clear_of_the_noise_gives_the_share_it_states(tmp_path: Path) -> None:
    result = _init(tmp_path, _with_null_count("700", "300"), LIBRARY_CONTRACT)
    assert _left_on(tmp_path)["nullFraction"] == "0.3"
    states = {e["pointer"]: e["state"] for e in result.artifacts["ledger"]["statistics"]}
    assert states["/tables/member/columns/left_on/statistics/0"] == "used"
    _plans(tmp_path)


def test_a_null_count_inside_the_noise_draws_no_null_and_names_its_interval(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _with_null_count("990", "6"), LIBRARY_CONTRACT)
    floor = noise_floor("gaussian", Fraction("2.5"), 2)
    upper = (6 + floor) / 996
    assert _left_on(tmp_path)["nullFraction"] == "0"
    assert "The released null count stands inside the noise" in result.stdout
    assert f"the release allows 0 to {_share_text(upper)} of the rows" in result.stdout
    states = {e["pointer"]: e["state"] for e in result.artifacts["ledger"]["statistics"]}
    assert states["/tables/member/columns/left_on/statistics/0"] == "used"
    _plans(tmp_path)


#: The cliques' contract with ``channel``, a column both cliques hold, allowed to be NULL.
NULLABLE_CHANNEL = private_cliques.CONTRACT.replace(
    "type: \"enum('online','store')\", required: true",
    "type: \"enum('online','store')\", required: false",
)


def _clique_release(known: str, null: str) -> Dict[str, Any]:
    """The cliques' private release with a noisy count of ``channel``'s known and NULL rows."""
    profile = copy.deepcopy(private_cliques.EXAMPLE)
    profile["_schemaURL"] = SCHEMA_URL_1_5_0
    profile["tables"]["subscriber"]["columns"]["channel"]["statistics"] = [
        {
            "definition": "nullCount/2",
            "sampleSize": {"min": 10000, "max": 99999},
            "disclosure": "noisy",
            "levels": ["known", "null"],
            "noise": {"mechanism": "gaussian", "scale": "2.24", "privacyLoss": {"rho": "0.1"}},
            "groups": [{"group": "", "status": "F", "noisyCounts": [known, null]}],
        }
    ]
    return profile


def _channel(tmp_path: Path) -> Dict[str, Any]:
    column: Dict[str, Any] = _block(tmp_path)["emits"]["subscriber"]["fidelity"]["columns"][
        "channel"
    ]
    return column


def test_a_clique_column_whose_null_count_is_inside_the_noise_keeps_its_clique(
    tmp_path: Path,
) -> None:
    assert "required: false" in NULLABLE_CHANNEL
    result = _init(tmp_path, _clique_release("14000", "3"), NULLABLE_CHANNEL)
    channel = _channel(tmp_path)
    assert channel["given"] == "plan" and "nullFraction" not in channel
    assert "The released null count stands inside the noise" in result.stdout
    states = {e["pointer"]: e["state"] for e in result.artifacts["ledger"]["statistics"]}
    assert states["/tables/subscriber/cliques/1"] == "used"
    assert states["/tables/subscriber/columns/channel/statistics/0"] == "used"
    _plans(tmp_path)


def test_a_clique_column_sets_a_released_null_share_aside_and_keeps_its_clique(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _clique_release("13000", "1000"), NULLABLE_CHANNEL)
    channel = _channel(tmp_path)
    assert channel["given"] == "plan" and "nullFraction" not in channel
    entries = {e["pointer"]: e for e in result.artifacts["ledger"]["statistics"]}
    assert entries["/tables/subscriber/cliques/1"]["state"] == "used"
    held = entries["/tables/subscriber/columns/channel/statistics/0"]
    assert held["state"] != "used"
    assert "a clique column holds no null" in json.dumps(held)
    assert _share_text(Fraction(1000, 14000)) in held["note"]
    _plans(tmp_path)


def _strict(tmp_path: Path, profile: Dict[str, Any], allow: Iterable[str] = ()) -> Any:
    tmp_path.mkdir()
    (tmp_path / "c.fluid.yaml").write_text(NULLABLE_CHANNEL, encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(profile), encoding="utf-8")
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "c.fluid.yaml"),
        profile=str(tmp_path / "p.json"),
        strict=True,
        allow=tuple(allow),
    )


#: What the strict run of the cliques' release refuses besides a null count: its own gaps.
_CLIQUE_GAPS = (
    "/extensions/com.agentics.flux~11/emits/subscriber/rows",
    "/extensions/com.agentics.flux~11/emits/subscriber/fidelity/columns/subscriber_id",
    "/spec/timeBounds",
)


def test_strict_refuses_a_released_null_share_a_clique_sets_aside_naming_it(
    tmp_path: Path,
) -> None:
    pointer = "/tables/subscriber/columns/channel/statistics/0"
    refused = _strict(tmp_path / "a", _clique_release("13000", "1000"), _CLIQUE_GAPS)
    assert refused.exit_code != 0
    (line,) = [text for text in refused.stderr.splitlines() if pointer in text]
    assert line.lstrip().startswith(f"unused {pointer} (nullCount/2)")
    assert "a clique column holds no null" in line
    assert _share_text(Fraction(1000, 14000)) in line
    accepted = _strict(tmp_path / "b", _clique_release("13000", "1000"), _CLIQUE_GAPS + (pointer,))
    assert accepted.exit_code == 0, accepted.stderr
    # Inside the noise no share is drawn, so none is set aside: the assumption is a gap instead.
    inside = _strict(tmp_path / "c", _clique_release("14000", "3"), _CLIQUE_GAPS)
    assert f"unused {pointer}" not in inside.stderr
    assert "gap /extensions/com.agentics.flux~11/emits/subscriber/fidelity/columns/channel: " in (
        inside.stderr
    )
