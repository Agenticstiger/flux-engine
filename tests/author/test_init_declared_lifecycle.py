# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes a history's activation and end from the lifecycle a contract declares.

FLUID 0.7.7 declares on the natural entity of a model with a validity window how its versions
begin and end: ``activation``, a column that holds the entity's first start on every version,
``endMarker``, the value an ending version holds in a column, and ``currentFlag``, a column that
holds a value on each entity's last version. Pinned here, on the membership versions of a gym:

* ``activation`` is the entry's ``activation``, and in the run every version of an entity holds the
  entity's first start;
* ``endMarker`` is the entry's ``end`` as ``finalVersion``, and in the run every ended entity's last
  version holds the value, no earlier version does, and the ended share is the published one;
* ``currentFlag`` has no construct in the engine, so it is written nowhere and the ledger says so;
* a lifecycle that names a column the expose lacks, a column of another type, or a value the column
  does not declare writes what it did before, with the reason;
* a contract with no lifecycle gives the bundle it gave before.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages.plan import plan
from flux_engine.stages.run import run

from ._written import bundle_digests
from .test_history_date_pairs import TIERS, _block, _init, _profile
from .test_history_init import _levels, _transitions

pytestmark = pytest.mark.integration

STATUS = ["ACTIVE", "ENDED", "PAUSED"]

# A row of tier_version: version_id, pass_id, member_id, valid_from, valid_to, tier, status,
# enrolled_on, renewed_on
PASS, START, END, STATE, JOINED = 1, 3, 4, 6, 7

#: The membership versions of a gym: ``status`` is ACTIVE, PAUSED or ENDED, ``enrolled_on`` the day
#: a member joined.
GYM = TIERS.replace("channel", "status")


def _lifecycle(**members: Any) -> str:
    """``GYM`` whose natural entity declares *members* as its lifecycle."""
    lines = ["          lifecycle:"]
    for name, value in members.items():
        if isinstance(value, dict):
            lines.append(
                f"            {name}: {{ column: {value['column']}, value: {value['value']} }}"
            )
        else:
            lines.append(f"            {name}: {value}")
    contract = GYM.replace(
        "{ name: pass, type: natural, expr: pass_id }",
        "name: pass\n          type: natural\n          expr: pass_id\n" + "\n".join(lines),
    )
    assert contract != GYM
    return contract


def _gym_profile(*, moving: bool = True) -> Dict[str, Any]:
    """The profile of ``GYM``: status moves between ACTIVE and PAUSED, and ends on ENDED."""
    profile = _profile()
    table = profile["tables"]["tier_version"]
    table["columns"]["status"] = {
        "domain": "category",
        "statistics": [_levels(STATUS, [1100, 150, 600], 1850)],
    }
    del table["columns"]["channel"]
    history = table["history"]
    del history["columns"]["channel"]
    if moving:
        history["columns"]["status"] = {
            "initial": _levels(["ACTIVE", "PAUSED"], [700, 300], 1000),
            "transitions": _transitions(
                "status",
                STATUS,
                {"ACTIVE": [0, 150, 200], "PAUSED": [250, 0, 0]},
            ),
        }
    else:
        history["columns"]["status"] = {
            "initial": _levels(["ACTIVE", "PAUSED"], [700, 300], 1000),
            "transitions": _transitions(
                "status", STATUS, {"ACTIVE": [0, 0, 0], "PAUSED": [0, 0, 0]}
            ),
        }
    history["reasons"] = _levels(
        ["several", "status", "tier"], [50, 100 if moving else 0, 700], 850
    )
    return profile


def _entry(tmp_path: Path) -> Optional[Dict[str, Any]]:
    entries = _block(tmp_path).get("history")
    return dict(entries[0]) if entries else None


def _rows(tmp_path: Path) -> List[List[Any]]:
    profile = tmp_path / "tiers.profile.json"
    planned = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=profile,
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr
    outcome = run(tmp_path / "bundle", state_dir=tmp_path / "run", profile=profile)
    assert outcome.success, outcome.stderr
    (stream,) = (tmp_path / "run").glob("runs/*/emits/tier_version.ndjson")
    return [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines() if line]


def _entities(rows: List[List[Any]]) -> Dict[Any, List[List[Any]]]:
    found: Dict[Any, List[List[Any]]] = defaultdict(list)
    for row in rows:
        found[row[PASS]].append(row)
    return {key: sorted(versions, key=lambda row: row[START]) for key, versions in found.items()}


def _says(result: Any, fragment: str) -> bool:
    return any(fragment in line for line in result.stdout.splitlines())


def test_a_declared_activation_is_written_and_every_version_holds_the_first_start(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _gym_profile(), _lifecycle(activation="enrolled_on"))
    assert result.exit_code == 0, result.stderr
    entry = _entry(tmp_path)
    assert entry is not None and entry["activation"] == "enrolled_on"
    entities = _entities(_rows(tmp_path))
    assert entities
    for versions in entities.values():
        assert {row[JOINED] for row in versions} == {versions[0][START]}


def test_an_end_marker_is_written_as_a_final_version_and_sits_on_the_last_version_only(
    tmp_path: Path,
) -> None:
    result = _init(
        tmp_path, _gym_profile(), _lifecycle(endMarker={"column": "status", "value": "ENDED"})
    )
    assert result.exit_code == 0, result.stderr
    entry = _entry(tmp_path)
    assert entry is not None
    assert entry["end"]["mode"] == "finalVersion"
    assert (entry["end"]["column"], entry["end"]["value"]) == ("status", "ENDED")
    assert entry["end"]["share"] == "0.15"
    # The column moves between ACTIVE and PAUSED; only the ending version holds ENDED.
    tracked = {item["column"]: item for item in entry["tracked"]}
    assert set(tracked["status"]["transitions"]) == {"ACTIVE", "PAUSED"}
    assert tracked["status"]["initial"]["levels"] == ["ACTIVE", "PAUSED"]
    entities = _entities(_rows(tmp_path))
    ended = 0
    for versions in entities.values():
        marked = [row[STATE] == "ENDED" for row in versions]
        assert not any(marked[:-1])
        ended += marked[-1]
    # The profile closes 15 % of the entities' last versions.
    assert ended == 0.15 * len(entities)


def test_an_end_marker_on_a_column_that_does_not_move_is_written(tmp_path: Path) -> None:
    result = _init(
        tmp_path,
        _gym_profile(moving=False),
        _lifecycle(endMarker={"column": "status", "value": "ENDED"}),
    )
    assert result.exit_code == 0, result.stderr
    entry = _entry(tmp_path)
    assert entry is not None and entry["end"]["mode"] == "finalVersion"
    entities = _entities(_rows(tmp_path))
    for versions in entities.values():
        assert not any(row[STATE] == "ENDED" for row in versions[:-1])


def test_a_lifecycle_with_all_three_members_writes_the_two_the_engine_has(tmp_path: Path) -> None:
    contract = _lifecycle(
        activation="enrolled_on",
        endMarker={"column": "status", "value": "ENDED"},
        currentFlag={"column": "tier", "value": "pro"},
    )
    result = _init(tmp_path, _gym_profile(), contract)
    assert result.exit_code == 0, result.stderr
    entry = _entry(tmp_path)
    assert entry is not None
    assert entry["activation"] == "enrolled_on" and entry["end"]["mode"] == "finalVersion"
    assert _says(result, "currentFlag")
    assert _rows(tmp_path)


#: The run's file of ``GYM`` with no lifecycle, as the base wrote it before the lifecycle was read,
#: with the share of changes that move no tracked column, cited to the column's transitions.
SIMULATION_WITHOUT_LIFECYCLE = "f5ba978ecf499a56c68bf0db24e80787437edda6be234c573a12e9f4d5bd4015"


def test_a_contract_without_a_lifecycle_gives_the_bundle_it_gave_before(tmp_path: Path) -> None:
    result = _init(tmp_path, _gym_profile(), GYM)
    assert result.exit_code == 0, result.stderr
    assert bundle_digests(tmp_path)["simulation.flux.yml"] == SIMULATION_WITHOUT_LIFECYCLE
    entry = _entry(tmp_path)
    assert entry is not None and "activation" not in entry
    assert entry["end"] == {"mode": "closeLast", "share": "0.15"}
    assert not _says(result, "lifecycle")


@pytest.mark.parametrize(
    ("members", "fragment"),
    [
        ({"activation": "joined_on"}, "joined_on"),
        ({"activation": "tier"}, "tier"),
        ({"endMarker": {"column": "left_on", "value": "ENDED"}}, "left_on"),
        ({"endMarker": {"column": "status", "value": "GONE"}}, "GONE"),
    ],
    ids=[
        "no such column",
        "a column of another type",
        "an end column the expose lacks",
        "no such level",
    ],
)
def test_a_lifecycle_the_expose_cannot_hold_keeps_what_was_written_before(
    tmp_path: Path, members: Dict[str, Any], fragment: str
) -> None:
    result = _init(tmp_path, _gym_profile(), _lifecycle(**members))
    assert result.exit_code == 0, result.stderr
    entry = _entry(tmp_path)
    assert entry is not None and "activation" not in entry
    assert entry["end"]["mode"] == "closeLast"
    assert _says(result, fragment)
    assert _rows(tmp_path)
