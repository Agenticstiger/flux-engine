# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes the end share of a contract whose entities end by a marked version.

A contract that declares ``lifecycle.endMarker`` ends an entity with a final version that holds
the value, never by closing its last one. A profile of such a table therefore publishes
``lastVersion.closed`` as 0, and the share of entities that end is read from the marker column's
history instead (ADR 0017, amendment of 2026-11-27):

* the entities that begin on the marker plus every published change into it, when each of those
  cells is published;
* else the rows the column's own histogram puts on the marker, one for each ended entity;
* else no end is written, and the ledger says why.

The share is of every entity, so the rows of a withheld ``lastVersion`` are not the entities.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

import tests.author.test_history_date_pairs as tiers
from flux_engine.cli._init import run_init
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages.plan import plan
from flux_engine.stages.run import run

from .test_history_date_pairs import _block, _init
from .test_history_init import _levels, _states, _transitions
from .test_init_declared_lifecycle import (
    STATE,
    _entities,
    _gym_profile,
    _lifecycle,
    _rows,
)
from .test_init_history_without_lengths import ACTIVATED
from .test_init_history_without_lengths import _profile as _withheld_profile
from .test_init_noisy_release_1_5 import _released_tiers

pytestmark = pytest.mark.integration

STATUS = ["ACTIVE", "ENDED", "PAUSED"]
MARKER = {"endMarker": {"column": "status", "value": "ENDED"}}
HISTORY = "/tables/tier_version/history"


def _forge_shaped(*, ended_rows: int = 150) -> Dict[str, Any]:
    """A library-style release of the gym's memberships whose status ends on ``ENDED``.

    1000 entities hold 1850 versions. As a profile of such a contract publishes them, no entity
    closes its last version (``closed`` is 0), and 20 entities begin on ``ENDED``, 120 move from
    ``ACTIVE`` to it and 60 from ``PAUSED``: 200 entities end. The column's own histogram holds
    *ended_rows* rows on ``ENDED``; a real release agrees with the cells above (200), and a
    test sets another number to show which of the two the entry reads.
    """
    profile = _gym_profile()
    table = profile["tables"]["tier_version"]
    rest = 1850 - ended_rows
    table["columns"]["status"]["statistics"] = [
        _levels(STATUS, [rest * 2 // 3, ended_rows, rest - rest * 2 // 3], 1850)
    ]
    history = table["history"]
    history["lastVersion"] = _levels(["closed", "open"], [0, 1000], 1000)
    history["columns"]["status"] = {
        "initial": _levels(STATUS, [650, 20, 330], 1000),
        "transitions": _transitions(
            "status",
            STATUS,
            {"ACTIVE": [0, 120, 150], "ENDED": [0, 0, 0], "PAUSED": [200, 60, 0]},
        ),
    }
    return profile


def _entry(tmp_path: Path) -> Dict[str, Any]:
    (entry,) = _block(tmp_path)["history"]
    return dict(entry)


def _ended_entities(tmp_path: Path) -> int:
    entities = _entities(_rows(tmp_path))
    ended = 0
    for versions in entities.values():
        marked = [row[STATE] == "ENDED" for row in versions]
        assert not any(marked[:-1])
        ended += marked[-1]
    return ended


def test_a_profile_that_closes_no_last_version_still_ends_the_entities_that_hold_the_marker(
    tmp_path: Path,
) -> None:
    # The histogram holds 150 marked rows, the cells 200 ended entities: the cells are read.
    result = _init(tmp_path, _forge_shaped(), _lifecycle(**MARKER))
    assert result.exit_code == 0, result.stderr
    entry = _entry(tmp_path)
    assert entry["end"] == {
        "mode": "finalVersion",
        "share": "0.2",
        "column": "status",
        "value": "ENDED",
    }
    # The column keeps moving between the levels an entity holds before it ends.
    tracked = {item["column"]: item for item in entry["tracked"]}
    assert set(tracked["status"]["transitions"]) == {"ACTIVE", "PAUSED"}
    assert _ended_entities(tmp_path) == 200


def test_the_ledger_says_which_members_gave_the_share_and_that_the_last_versions_did_not(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _forge_shaped(), _lifecycle(**MARKER))
    states = _states(result)
    last = states[f"{HISTORY}/lastVersion"]
    assert last["state"] == "unused"
    assert "end marker" in last["note"]
    assert not any(ch.isdigit() for ch in last["note"]), last["note"]
    for member in ("initial", "transitions"):
        assert states[f"{HISTORY}/columns/status/{member}"]["state"] == "used"


@pytest.mark.parametrize("case", ["initial", "transitions", "group withheld", "group missing"])
def test_a_withheld_cell_into_the_marker_gives_the_share_to_the_columns_histogram(
    tmp_path: Path, case: str
) -> None:
    profile = _forge_shaped(ended_rows=150)
    columns = profile["tables"]["tier_version"]["history"]["columns"]["status"]
    if case in ("initial", "transitions"):
        group = columns[case]["groups"][0 if case == "initial" else 2]
        # The marker's cell: the first value of ENDED, or the move into it from PAUSED.
        at = 1
        counts = list(group["levelCounts"])
        group["cellStatus"] = ["F" if i != at else "C" for i in range(len(counts))]
        group["levelCounts"] = [c if i != at else None for i, c in enumerate(counts)]
    elif case == "group withheld":
        columns["transitions"]["groups"][2] = {"group": "PAUSED", "status": "C"}
    else:
        del columns["transitions"]["groups"][2]
    _init(tmp_path, profile, _lifecycle(**MARKER))
    # 150 marked rows are 150 ended entities of 1000.
    assert _entry(tmp_path)["end"]["share"] == "0.15"
    assert _entry(tmp_path)["end"]["mode"] == "finalVersion"


def test_a_histogram_that_puts_more_rows_than_entities_on_the_marker_caps_the_share_at_one(
    tmp_path: Path,
) -> None:
    # 1800 marked rows over 1000 entities, and the column is not tracked: the histogram's share.
    profile = _forge_shaped(ended_rows=1800)
    history = profile["tables"]["tier_version"]["history"]
    del history["columns"]["status"]
    reasons = history["reasons"]
    reasons["levels"] = ["several", "tier"]
    group = reasons["groups"][0]
    group["levelCounts"], group["k"] = [50, 700], 750
    result = _init(tmp_path, profile, _lifecycle(**MARKER))
    assert result.exit_code == 0, result.stderr
    assert _entry(tmp_path)["end"]["share"] == "1"


def test_a_profile_that_ends_every_entity_caps_the_share_at_one(tmp_path: Path) -> None:
    profile = _forge_shaped()
    history = profile["tables"]["tier_version"]["history"]
    history["columns"]["status"]["initial"] = _levels(STATUS, [0, 1000, 0], 1000)
    _init(tmp_path, profile, _lifecycle(**MARKER))
    assert _entry(tmp_path)["end"]["share"] == "1"


def _release_without_a_marker_cell() -> Dict[str, Any]:
    profile = _forge_shaped()
    history = profile["tables"]["tier_version"]["history"]
    history["columns"]["status"]["initial"] = _levels(STATUS, [700, 0, 300], 1000)
    history["columns"]["status"]["transitions"] = _transitions(
        "status", STATUS, {"ACTIVE": [0, 0, 150], "ENDED": [0, 0, 0], "PAUSED": [200, 0, 0]}
    )
    profile["tables"]["tier_version"]["columns"]["status"]["statistics"] = [
        _levels(STATUS, [1100, 0, 750], 1850)
    ]
    return profile


def test_a_share_of_no_entity_is_not_written_and_the_run_says_why(tmp_path: Path) -> None:
    result = _init(tmp_path, _release_without_a_marker_cell(), _lifecycle(**MARKER))
    assert result.exit_code == 0, result.stderr
    assert "end" not in _entry(tmp_path)
    assert "no entity ends by the marker" in result.stdout


def test_a_table_with_no_end_marker_keeps_the_end_it_had(tmp_path: Path) -> None:
    # No lifecycle: the closed share of last versions is the end share, and none closed gives none.
    plain = tiers.TIERS.replace("channel", "status")
    (tmp_path / "none").mkdir()
    _init(tmp_path / "none", _forge_shaped(), plain)
    assert "end" not in _entry(tmp_path / "none")
    closing = _forge_shaped()
    closing["tables"]["tier_version"]["history"]["lastVersion"] = _levels(
        ["closed", "open"], [150, 850], 1000
    )
    (tmp_path / "some").mkdir()
    _init(tmp_path / "some", closing, plain)
    assert _entry(tmp_path / "some")["end"] == {"mode": "closeLast", "share": "0.15"}


def _marked_contract() -> str:
    """``ACTIVATED`` whose natural entity ends on the value ``web`` of ``channel``."""
    return ACTIVATED.replace(
        "lifecycle: { activation: enrolled_on }",
        "lifecycle: { activation: enrolled_on, endMarker: { column: channel, value: web } }",
    )


def test_a_withheld_last_version_ends_the_entities_not_the_rows(tmp_path: Path) -> None:
    # 1800 versions of 1000 entities, 270 of them marked: 270 entities end, 27 % of 1000. The
    # rows' share is 15 %, and each ended entity holds the marker on one version alone.
    profile = _withheld_profile(rows=1800, entities=1000, open_cell=1800)
    version = profile["tables"]["tier_version"]
    version["columns"]["channel"]["statistics"] = [_levels(["app", "web"], [1530, 270], 1800)]
    channel = version["history"]["columns"]["channel"]
    channel["initial"]["groups"] = [{"group": "", "status": "C"}]
    channel["transitions"]["groups"] = [
        {"group": "app", "status": "C"},
        {"group": "web", "status": "C"},
    ]
    (tmp_path / "tiers.fluid.yml").write_text(_marked_contract(), encoding="utf-8")
    (tmp_path / "tiers.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "tiers.fluid.yml"),
        profile=str(tmp_path / "tiers.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    end = _entry(tmp_path)["end"]
    assert (end["mode"], end["share"]) == ("finalVersion", "0.27")


def test_a_noisy_release_reads_the_cells_that_stand_clear_of_the_noise(tmp_path: Path) -> None:
    # The 20 entities that begin on the marker lie inside the noise of a first value (about 37),
    # so only the 120 and the 60 that move into it count: 180 of 1000.
    result = _init(tmp_path, _released_tiers(profile=_forge_shaped()), _lifecycle(**MARKER))
    assert result.exit_code == 0, result.stderr
    end: Optional[Dict[str, Any]] = _entry(tmp_path).get("end")
    assert end is not None
    assert (end["mode"], end["column"], end["value"]) == ("finalVersion", "status", "ENDED")
    assert end["share"] == "0.18"


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "end-marker-init"


def test_a_profile_a_profiler_wrote_ends_the_share_of_entities_its_cells_count(
    tmp_path: Path,
) -> None:
    # The fixture is a release of 1000 memberships' versions as the profiler writes it: no last
    # version is closed, 100 entities begin on ENDED, 75 and 50 move into it from the other two
    # levels (every count rounded to 5), and its column's histogram puts 230 rows on it.
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(FIXTURE / "gym.fluid.yml"),
        profile=str(FIXTURE / "gym.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    (entry,) = yaml.safe_load((tmp_path / "bundle" / "simulation.flux.yml").read_text("utf-8"))[
        "extensions"
    ]["com.agentics.flux/1"]["history"]
    assert entry["end"] == {
        "mode": "finalVersion",
        "share": "0.225",
        "column": "status",
        "value": "ENDED",
    }
    planned = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=FIXTURE / "gym.profile.json",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr
    outcome = run(
        tmp_path / "bundle", state_dir=tmp_path / "run", profile=FIXTURE / "gym.profile.json"
    )
    assert outcome.success, outcome.stderr
    (stream,) = (tmp_path / "run").glob("runs/*/emits/tier_version.ndjson")
    versions: Dict[Any, List[List[Any]]] = {}
    for line in stream.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        versions.setdefault(row[1], []).append(row)
    ended = 0
    for held in versions.values():
        marked = [row[5] == "ENDED" for row in sorted(held, key=lambda row: row[3])]
        assert not any(marked[:-1])
        ended += marked[-1]
    assert (ended, len(versions)) == (225, 1000)
