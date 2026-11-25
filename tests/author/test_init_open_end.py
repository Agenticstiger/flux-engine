# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes a versioned table whose end column holds one value, the last day a date can hold.

A table of versions whose end column is required holds a sentinel such as ``9999-12-31`` for every open
version. When every version is open the profile publishes that one date and nothing else for the
column, and the column is spanned by the day after it. No day follows ``9999-12-31``, so the plan of
the column raised ``OverflowError`` and the whole table was left out of the bundle; a table that
refers to it was then refused (``FLX-3403``), and ``init`` exited 1 on a world that is fine with a NULL
open end.

Pinned here, through ``fluxsim init`` and ``plan`` on a lending library of regions, stations, versions
of a station's line and the readings taken on a line:

* a required end column holding ``9999-12-31`` writes the versioned table, spanned by the day that
  ends there, and says so in the register;
* the same world with a NULL open end (an optional end column, an ordinary last date) is written as
  it always was, spanned by the day that starts there;
* the bundle of either plans, and the table that refers to the versions is written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages.plan import plan

from .test_history_init import NAMESPACE, _levels, _metrics, _ms, _profile

pytestmark = pytest.mark.integration


def _expose(
    expose_id: str,
    schema: List[Dict[str, Any]],
    entities: List[Dict[str, Any]],
    relationships: List[Dict[str, Any]],
    dimensions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "local",
            "format": "parquet",
            "location": {"path": f"./out/{expose_id}.parquet"},
        },
        "contract": {"schema": schema},
        "semantics": {
            "entities": entities,
            "relationships": relationships,
            "dimensions": dimensions,
        },
    }


def _column(name: str, kind: str = "INT", required: bool = True) -> Dict[str, Any]:
    return {"name": name, "type": kind, "required": required}


def _to_one(name: str, target: str, column: str) -> Dict[str, Any]:
    return {
        "name": name,
        "to": target,
        "fromColumns": [column],
        "toColumns": [column],
        "cardinality": "many_to_one",
    }


def _contract(required_end: bool) -> str:
    """Regions hold stations; a line's versions (a validity window) hold a station; readings name a line."""
    exposes = [
        _expose(
            "region", [_column("region_id")], [_entity("region", "primary", "region_id")], [], []
        ),
        _expose(
            "station",
            [_column("station_id"), _column("region_id")],
            [_entity("station", "primary", "station_id")],
            [_to_one("region", "region", "region_id")],
            [],
        ),
        _expose(
            "line_version",
            [
                _column("version_id"),
                _column("line_id"),
                _column("station_id"),
                _column("valid_from", "DATE"),
                _column("valid_to", "DATE", required_end),
                _column("tier", "STRING"),
            ],
            [
                _entity("line_version", "primary", "version_id"),
                _entity("line", "natural", "line_id"),
            ],
            [_to_one("station", "station", "station_id")],
            [
                {
                    "name": "valid_from",
                    "type": "time",
                    "typeParams": {"validityParams": {"isStart": True}},
                },
                {
                    "name": "valid_to",
                    "type": "time",
                    "typeParams": {"validityParams": {"isEnd": True}},
                },
                {"name": "tier", "type": "categorical"},
            ],
        ),
        _expose(
            "reading",
            [_column("reading_id"), _column("line_id")],
            [_entity("reading", "primary", "reading_id")],
            [_to_one("line", "line_version", "line_id")],
            [],
        ),
    ]
    document = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "lending.library",
        "name": "Library",
        "metadata": {"owner": {"team": "library-team"}},
        "exposes": exposes,
    }
    return yaml.safe_dump(document, sort_keys=False)


def _entity(name: str, kind: str, column: str) -> Dict[str, Any]:
    return {"name": name, "type": kind, "expr": column}


def _number(high: int, count: int) -> Dict[str, Any]:
    return {"domain": "number", "metrics": _metrics("1", str(high), count)}


def _profile_of(last_end: str) -> Dict[str, Any]:
    """The profile of a world of 2 regions, 3 stations (rowCount withheld) and 20 versions of a line.

    The versions' end column publishes one date, *last_end*: every version of the table is open.
    """
    year, month, day = (int(part) for part in last_end.split("-"))
    ended = _ms(year, month, day)
    found = _profile(history=False)
    found["disclosureRules"]["acro-default"]["minimumCell"] = 5
    found["tables"] = {
        "region": {"rowCount": 2, "columns": {"region_id": _number(2, 2)}},
        # Three rows are below the minimum cell: the profile withholds the count of the stations.
        "station": {
            "columns": {"station_id": {"domain": "number"}, "region_id": {"domain": "number"}}
        },
        "line_version": {
            "rowCount": 20,
            "columns": {
                "version_id": _number(20, 20),
                "line_id": _number(20, 20),
                "station_id": {"domain": "number"},
                "valid_from": {
                    "domain": "timestamp",
                    "metrics": _metrics(_ms(2025, 1, 1), _ms(2025, 9, 1), 20),
                },
                "valid_to": {"domain": "timestamp", "metrics": _metrics(ended, ended, 20)},
                "tier": {
                    "domain": "category",
                    "statistics": [_levels(["basic", "plus", "pro"], [10, 6, 4], 20)],
                },
            },
        },
        "reading": {
            "columns": {"reading_id": {"domain": "number"}, "line_id": {"domain": "number"}}
        },
    }
    return found


def _init(tmp_path: Path, *, required_end: bool, last_end: str) -> Any:
    (tmp_path / "library.fluid.yml").write_text(_contract(required_end), encoding="utf-8")
    (tmp_path / "library.profile.json").write_text(
        json.dumps(_profile_of(last_end)), encoding="utf-8"
    )
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "library.fluid.yml"),
        profile=str(tmp_path / "library.profile.json"),
    )


def _emits(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    found: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]["emits"]
    return found


def _reasons(tmp_path: Path) -> List[str]:
    """The reason of every assumption the Simulation's register states for the end column."""
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            pointer = str(node.get("pointer", ""))
            if node.get("source") == "assumption" and pointer.endswith(
                "line_version/fidelity/columns/valid_to/binWeights"
            ):
                found.append(" ".join(str(node["reason"]).split()))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(yaml.safe_load(text))
    return found


def _plans(tmp_path: Path) -> None:
    planned = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "library.profile.json",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr


def test_a_required_end_that_holds_the_last_day_is_spanned_by_the_day_that_ends_there(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, required_end=True, last_end="9999-12-31")
    assert result.exit_code == 0, result.stderr
    emits = _emits(tmp_path)
    assert {"region", "station", "line_version", "reading"} <= set(emits)
    ended = emits["line_version"]["fidelity"]["columns"]["valid_to"]
    assert ended["bins"] == ["9999-12-30", "9999-12-31"]
    assert ended["binWeights"] == ["1"]
    assert any("the day that ends there" in reason for reason in _reasons(tmp_path))
    _plans(tmp_path)


def test_a_null_open_end_is_written_as_it_always_was(tmp_path: Path) -> None:
    result = _init(tmp_path, required_end=False, last_end="2025-10-01")
    assert result.exit_code == 0, result.stderr
    emits = _emits(tmp_path)
    assert {"region", "station", "line_version", "reading"} <= set(emits)
    ended = emits["line_version"]["fidelity"]["columns"]["valid_to"]
    assert ended["bins"] == ["2025-10-01", "2025-10-02"]
    assert any("the day that starts there" in reason for reason in _reasons(tmp_path))
    _plans(tmp_path)
