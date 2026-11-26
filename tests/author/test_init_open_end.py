# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes a versioned table whose end column holds one value, such as an open end.

A table of versions whose end column is required holds a sentinel such as ``9999-12-31`` for every open
version. When every version is open the profile publishes that one value and nothing else for the
column, and every row of the column takes it.

Pinned here, through ``fluxsim init``, ``plan`` and ``apply`` on a lending library of regions,
stations, versions of a station's line and the readings taken on a line:

* an ordinary one-value date (``2025-10-01``) is written, and every row drawn holds exactly it;
* the same value as a timestamp (``9999-12-31T00:00:00``, a sentinel open end) is the same;
* a DATE that is ``9999-12-31`` cannot be written on every row yet: the table is left out and its
  statistics are listed unused with the reason, unless something the bundle writes needs it (a
  relationship, or a history file naming it), when ``init`` refuses before it writes anything,
  names the table and the column, and says what to change;
* the bundle of each that is written plans and applies, and the table that refers to the versions
  is written.
* a column that holds one date at or after 9999-01-01T00:00:00 gives the window nothing, and a date
  before it does; a column with a span that starts before it, from its published range or from its
  bins, keeps every span it has, even when another ends in the year 9999.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from flux_engine.author.bundle import AuthoredExpose, _spans_of
from flux_engine.author.columns import ColumnPlan
from flux_engine.author.window import WINDOW_END_LIMIT_MS, instant_ms
from flux_engine.cli import main
from flux_engine.cli._init import run_init
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.seam.contract import LogicalKind
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan

from .test_history_init import NAMESPACE, _bins, _levels, _metrics, _ms, _profile
from .test_init_history_file import HISTORY_FILE

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


def _contract(required_end: bool, end_type: str = "DATE") -> str:
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
                _column("valid_to", end_type, required_end),
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


def _profile_of(last_end: str, first_end: Optional[str] = None) -> Dict[str, Any]:
    """The profile of a world of 2 regions, 3 stations (rowCount withheld) and 20 versions of a line.

    The versions' end column publishes one date, *last_end*: every version of the table is open.
    With *first_end*, it publishes that date as its minimum and *last_end* as its maximum.
    """
    year, month, day = (int(part) for part in last_end.split("-"))
    ended = _ms(year, month, day)
    year, month, day = (int(part) for part in (first_end or last_end).split("-"))
    earliest = _ms(year, month, day)
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
                "valid_to": {"domain": "timestamp", "metrics": _metrics(earliest, ended, 20)},
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


def _init(
    tmp_path: Path,
    *,
    required_end: bool,
    last_end: str,
    first_end: Optional[str] = None,
    window: Optional[Tuple[str, str]] = None,
    end_type: str = "DATE",
    edges: Optional[List[str]] = None,
) -> Any:
    """Run ``init`` on the library; *edges* (dates) give the end column a published histogram."""
    found = _profile_of(last_end, first_end)
    if edges is not None:
        found["tables"]["line_version"]["columns"]["valid_to"]["statistics"] = [
            _bins(
                [_ms(*(int(part) for part in edge.split("-"))) for edge in edges],
                [10] * (len(edges) - 1),
                10 * (len(edges) - 1),
            )
        ]
    (tmp_path / "library.fluid.yml").write_text(_contract(required_end, end_type), encoding="utf-8")
    (tmp_path / "library.profile.json").write_text(json.dumps(found), encoding="utf-8")
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "library.fluid.yml"),
        profile=str(tmp_path / "library.profile.json"),
        window=window,
    )


def _emits(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    found: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]["emits"]
    return found


def _time_bounds(tmp_path: Path) -> Dict[str, str]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    found: Dict[str, str] = yaml.safe_load(text)["spec"]["timeBounds"]
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


def _plans(tmp_path: Path) -> Path:
    """Plan and apply the bundle; the state directory holding the run."""
    state = tmp_path / "state"
    planned = plan(
        tmp_path / "bundle",
        state_dir=state,
        profile=tmp_path / "library.profile.json",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr
    run_id = str(planned.artifacts["run_id"])
    applied = apply(tmp_path / "bundle", state_dir=state, run_id=run_id, auto_plan=False)
    assert applied.success, applied.stderr
    return state / "runs" / run_id


def _drawn(run_dir: Path, table: str, column: str) -> List[Any]:
    """Every value of *column* in the rows the run drew for *table*."""
    attestation = json.loads(
        (run_dir / f"seam-attestation-{table}.json").read_text(encoding="utf-8")
    )
    lines = (run_dir / "emits" / f"{table}.ndjson").read_text(encoding="utf-8").splitlines()
    position = attestation["columns"].index(column)
    return [json.loads(line)[position] for line in lines if line]


def test_a_required_end_that_holds_the_last_day_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, required_end=True, last_end="9999-12-31")
    assert result.exit_code == 1
    message = " ".join(result.stderr.split())
    assert "line_version.valid_to" in message
    assert "publishes one value, the last the calendar holds, on every row" in message
    assert "cannot yet write a DATE of exactly 9999-12-31 on every row" in message
    assert "write its open end as NULL, or make the column a TIMESTAMP" in message
    assert not (tmp_path / "bundle").exists()


def test_the_refusal_of_a_value_init_cannot_draw_exactly_carries_a_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(tmp_path, required_end=True, last_end="9999-12-31")
    exit_code = main(
        [
            "init",
            str(tmp_path / "again"),
            "--contract",
            str(tmp_path / "library.fluid.yml"),
            "--profile",
            str(tmp_path / "library.profile.json"),
            "--json",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert envelope["error"]["code"] == "FLX-3125"
    message = " ".join(envelope["error"]["message"].split())
    assert "line_version.valid_to publishes one value, the last the calendar holds" in message
    assert not (tmp_path / "again").exists()


def test_the_refusal_of_a_one_value_timestamp_and_of_a_history_file_carry_the_code_too(
    tmp_path: Path,
) -> None:
    (tmp_path / "library.fluid.yml").write_text(_contract(True, "TIMESTAMP"), encoding="utf-8")
    found = _profile_of("9999-12-31")
    last = str(253_402_300_799_999)
    found["tables"]["line_version"]["columns"]["valid_to"]["metrics"] = _metrics(last, last, 20)
    (tmp_path / "library.profile.json").write_text(json.dumps(found), encoding="utf-8")
    refused = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "library.fluid.yml"),
        profile=str(tmp_path / "library.profile.json"),
    )
    assert refused.exit_code == 1 and refused.error_code == "FLX-3125"
    named = copy.deepcopy(HISTORY_FILE)
    named["history"][0].update({"emit": "line_version", "tracked": []})
    named["history"][0].pop("parent")
    for key in ("snapshots", "events", "periods"):
        named.pop(key, None)
    (tmp_path / "history.json").write_text(json.dumps(named), encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    by_file = _init_without_readings(other, "9999-12-31", history=str(tmp_path / "history.json"))
    assert by_file.exit_code == 1 and by_file.error_code == "FLX-3125"


def _init_without_readings(tmp_path: Path, last_end: str, history: Optional[str] = None) -> Any:
    """``init`` on the library with no table of readings: nothing refers to the versions."""
    document = yaml.safe_load(_contract(True))
    document["exposes"] = [e for e in document["exposes"] if e["exposeId"] != "reading"]
    found = _profile_of(last_end)
    found["tables"].pop("reading")
    (tmp_path / "library.fluid.yml").write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    (tmp_path / "library.profile.json").write_text(json.dumps(found), encoding="utf-8")
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "library.fluid.yml"),
        profile=str(tmp_path / "library.profile.json"),
        history=history,
    )


def test_a_table_nothing_refers_to_is_left_out_when_its_end_holds_only_the_last_day(
    tmp_path: Path,
) -> None:
    result = _init_without_readings(tmp_path, "9999-12-31")
    assert result.exit_code == 0, result.stderr
    emits = _emits(tmp_path)
    assert set(emits) == {"region", "station"}
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    unused = yaml.safe_load(text)["extensions"][NAMESPACE]["unused"]
    held = [
        entry
        for entry in unused
        if str(entry["statistic"]["pointer"]).startswith("/tables/line_version/")
    ]
    assert held
    for entry in held:
        reason = " ".join(str(entry["reason"]).split())
        assert reason.startswith("the expose was not written: line_version.valid_to publishes")
        assert "cannot yet write a DATE of exactly 9999-12-31 on every row" in reason
        assert "write its open end as NULL, or make the column a TIMESTAMP" in reason
    assert "line_version: not written" in " ".join(result.stdout.split())
    # The bundle that is written plans and applies.
    _plans(tmp_path)


def test_a_history_file_that_names_the_table_is_refused_with_the_reason_it_is_left_out(
    tmp_path: Path,
) -> None:
    named = copy.deepcopy(HISTORY_FILE)
    named["history"][0].update({"emit": "line_version", "tracked": []})
    named["history"][0].pop("parent")
    for key in ("snapshots", "events", "periods"):
        named.pop(key, None)
    (tmp_path / "history.json").write_text(json.dumps(named), encoding="utf-8")
    result = _init_without_readings(tmp_path, "9999-12-31", history=str(tmp_path / "history.json"))
    assert result.exit_code == 1
    assert "line_version.valid_to publishes one value" in " ".join(result.stderr.split())
    assert not (tmp_path / "bundle").exists()


def test_a_table_something_refers_to_is_still_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    # The readings name a line of the versions: left out, plan would refuse them (FLX-3403).
    result = _init(tmp_path, required_end=True, last_end="9999-12-31")
    assert result.exit_code == 1
    assert "line_version.valid_to publishes one value" in " ".join(result.stderr.split())
    assert not (tmp_path / "bundle").exists()


def test_a_required_timestamp_end_at_the_start_of_the_last_day_is_drawn_exactly(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, required_end=True, last_end="9999-12-31", end_type="TIMESTAMP")
    assert result.exit_code == 0, result.stderr
    emits = _emits(tmp_path)
    assert {"region", "station", "line_version", "reading"} <= set(emits)
    ended = emits["line_version"]["fidelity"]["columns"]["valid_to"]
    assert ended["bins"] == [
        "9999-12-31T00:00:00.000Z",
        "9999-12-31T00:00:00.001Z",
        "9999-12-31T00:00:00.002Z",
    ]
    assert ended["binWeights"] == ["1", "0"]
    assert any("every row takes it" in reason for reason in _reasons(tmp_path))
    drawn = _drawn(_plans(tmp_path), "line_version", "valid_to")
    assert len(drawn) == 20
    assert set(drawn) == {drawn[0]}
    assert drawn[0] == "9999-12-31T00:00:00.000Z"


def test_a_timestamp_end_within_two_milliseconds_of_the_last_instant_is_refused(
    tmp_path: Path,
) -> None:
    (tmp_path / "library.fluid.yml").write_text(_contract(True, "TIMESTAMP"), encoding="utf-8")
    found = _profile_of("9999-12-31")
    last = str(253_402_300_799_999)
    found["tables"]["line_version"]["columns"]["valid_to"]["metrics"] = _metrics(last, last, 20)
    (tmp_path / "library.profile.json").write_text(json.dumps(found), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "library.fluid.yml"),
        profile=str(tmp_path / "library.profile.json"),
    )
    assert result.exit_code == 1
    message = " ".join(result.stderr.split())
    assert "line_version.valid_to" in message
    assert "publishes one value, the last the calendar holds, on every row" in message
    assert "write its open end as NULL" in message
    assert not (tmp_path / "bundle").exists()


def test_an_end_column_that_holds_only_the_last_day_gives_the_window_nothing(
    tmp_path: Path,
) -> None:
    # The window is the versions' start span: a run reaching the year 9999 would spread every
    # date drawn across the window over eight thousand years.
    result = _init(tmp_path, required_end=True, last_end="9999-12-31", end_type="TIMESTAMP")
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["spec"]["timeBounds"] == {
        "start": "2025-01-01T00:00:00.000Z",
        "end": "2025-09-01T00:00:00.000Z",
    }


def test_a_window_given_beside_an_end_column_in_the_year_9999_is_not_refused_for_it(
    tmp_path: Path,
) -> None:
    # Two end dates, both in the year 9999, give the column a range. The window init works out
    # leaves that range out, so a window given with --window is not refused for leaving it out.
    result = _init(
        tmp_path,
        required_end=True,
        last_end="9999-12-31",
        first_end="9999-12-30",
        window=("2025-01-01", "2025-12-31"),
    )
    assert result.exit_code == 0, result.stderr
    _plans(tmp_path)


def test_an_end_column_that_runs_from_2025_to_the_last_day_keeps_its_span_below_the_year_9999(
    tmp_path: Path,
) -> None:
    # Versions that close in 2025 and versions that stay open on 9999-12-31: the range starts in
    # 2025, so the column's span is not a year-9999 span, and the contract declares no open end,
    # so the window holds the span as far as the year 9999 begins and not the year's own values.
    result = _init(tmp_path, required_end=True, last_end="9999-12-31", first_end="2025-10-01")
    assert result.exit_code == 0, result.stderr
    assert _time_bounds(tmp_path) == {
        "start": "2025-01-01T00:00:00.000Z",
        "end": "2025-10-01T00:00:00.000Z",
    }
    _plans(tmp_path)


def test_a_value_exactly_at_the_start_of_the_year_9999_gives_the_window_nothing(
    tmp_path: Path,
) -> None:
    # 9999-01-01T00:00:00 is the first instant a window may not reach: the column is left out.
    result = _init(tmp_path, required_end=True, last_end="9999-01-01")
    assert result.exit_code == 0, result.stderr
    ended = _emits(tmp_path)["line_version"]["fidelity"]["columns"]["valid_to"]
    assert ended["bins"] == ["9999-01-01", "9999-01-02", "9999-01-03"]
    assert _time_bounds(tmp_path) == {
        "start": "2025-01-01T00:00:00.000Z",
        "end": "2025-09-01T00:00:00.000Z",
    }
    _plans(tmp_path)


def test_a_value_just_before_the_year_9999_is_a_span_the_window_holds(tmp_path: Path) -> None:
    # The last day of 9998 is a date the window can hold: the column keeps its span, which ends
    # the day after its one value. The edge that closes the value's bin carries no weight and
    # reaches no further.
    result = _init(tmp_path, required_end=False, last_end="9998-12-31")
    assert result.exit_code == 0, result.stderr
    ended = _emits(tmp_path)["line_version"]["fidelity"]["columns"]["valid_to"]
    assert ended["bins"] == ["9998-12-31", "9999-01-01", "9999-01-02"]
    assert ended["binWeights"] == ["1", "0"]
    # No value is in the year 9999: the edge that starts it only closes a bin, and no note says so.
    assert "values in the year 9999" not in result.stdout
    assert _time_bounds(tmp_path) == {
        "start": "2025-01-01T00:00:00.000Z",
        "end": "9999-01-01T00:00:00.000Z",
    }
    _plans(tmp_path)


def test_an_ordinary_one_value_date_is_drawn_exactly(tmp_path: Path) -> None:
    result = _init(tmp_path, required_end=False, last_end="2025-10-01")
    assert result.exit_code == 0, result.stderr
    emits = _emits(tmp_path)
    assert {"region", "station", "line_version", "reading"} <= set(emits)
    ended = emits["line_version"]["fidelity"]["columns"]["valid_to"]
    assert ended["bins"] == ["2025-10-01", "2025-10-02", "2025-10-03"]
    assert ended["binWeights"] == ["1", "0"]
    assert any("every row takes it" in reason for reason in _reasons(tmp_path))
    drawn = _drawn(_plans(tmp_path), "line_version", "valid_to")
    assert len(drawn) == 20
    assert set(drawn) == {"2025-10-01"}


# -- the spans a column gives the window -------------------------------------------------------------


def _spans(
    time_span: Optional[Tuple[int, int]],
    bins: Optional[List[str]] = None,
    weights: Optional[List[str]] = None,
) -> List[Tuple[int, int]]:
    """The spans :func:`_spans_of` gives the window for a date column with a published range and bins."""
    column = ColumnPlan("opened_on", time_span=time_span)
    if bins is not None:
        column.declaration["bins"] = bins
    if weights is not None:
        column.declaration["binWeights"] = weights
    expose = AuthoredExpose("visit", kinds={"opened_on": LogicalKind.DATE})
    found, _, _ = _spans_of(expose, column, None)
    return found


def _at(text: str) -> int:
    moment = instant_ms(text)
    assert moment is not None
    return moment


def test_a_column_with_a_span_in_2025_keeps_every_span_when_another_is_in_the_year_9999() -> None:
    # The published range runs from 2025 to the last day; its bins are the last two days. Only
    # the span that starts in 2025 is a span a window can hold, and the column keeps both.
    ranged = (_at("2025-10-01"), _at("9999-12-31"))
    binned = (_at("9999-12-30"), _at("9999-12-31"))
    assert _spans(ranged, ["9999-12-30", "9999-12-31"]) == [ranged, binned]
    # The same, with the span in 2025 held by the bins and the published range in the year 9999.
    assert _spans(binned, ["2025-10-01", "9999-12-31"]) == [binned, ranged]


def test_a_column_whose_every_span_starts_in_the_year_9999_gives_the_window_nothing() -> None:
    assert _spans((_at("9999-12-30"), _at("9999-12-31")), ["9999-12-30", "9999-12-31"]) == []
    assert _spans(None, ["9999-12-30", "9999-12-31"]) == []
    assert _spans((_at("9999-12-30"), _at("9999-12-31"))) == []


def test_a_span_that_starts_exactly_at_9999_01_01_gives_the_window_nothing() -> None:
    limit = (WINDOW_END_LIMIT_MS, WINDOW_END_LIMIT_MS + 86_400_000)
    assert _at("9999-01-01T00:00:00") == WINDOW_END_LIMIT_MS
    assert _spans(limit) == []
    assert _spans(None, ["9999-01-01", "9999-01-02"]) == []
    # One millisecond earlier, the span is one a window can hold.
    before = (WINDOW_END_LIMIT_MS - 1, WINDOW_END_LIMIT_MS + 86_400_000)
    assert _spans(before) == [before]
    assert _spans(None, ["9998-12-31T23:59:59.999Z", "9999-01-02"]) == [
        (WINDOW_END_LIMIT_MS - 1, WINDOW_END_LIMIT_MS + 86_400_000)
    ]


def test_a_bin_that_carries_no_weight_at_either_end_gives_the_window_nothing() -> None:
    # The edge that closes a one-value date's bin reaches a day further and draws nothing.
    edges = ["2025-10-01", "2025-10-02", "2025-10-03"]
    assert _spans(None, edges, ["1", "0"]) == [(_at("2025-10-01"), _at("2025-10-02"))]
    assert _spans(None, edges, ["0", "1"]) == [(_at("2025-10-02"), _at("2025-10-03"))]
    # Without weights, or with none positive, every edge counts.
    assert _spans(None, edges) == [(_at("2025-10-01"), _at("2025-10-03"))]
    assert _spans(None, edges, ["0", "0"]) == [(_at("2025-10-01"), _at("2025-10-03"))]


def test_a_bin_that_carries_no_weight_between_weighted_bins_stays_in_the_span() -> None:
    edges = ["2025-10-01", "2025-10-02", "2025-10-03", "2025-10-04", "2025-10-05"]
    assert _spans(None, edges, ["0", "1", "0", "2"]) == [(_at("2025-10-02"), _at("2025-10-05"))]
    assert _spans(None, edges, ["1", "0", "0", "2"]) == [(_at("2025-10-01"), _at("2025-10-05"))]


def test_a_one_value_date_ends_the_window_where_the_one_value_does(tmp_path: Path) -> None:
    # Versions that all end on 2025-10-01, after the last start (2025-09-01): the window's end is
    # the day after the value, and not the edge that closes the value's bin, two days after it.
    result = _init(tmp_path, required_end=False, last_end="2025-10-01")
    assert result.exit_code == 0, result.stderr
    ended = _emits(tmp_path)["line_version"]["fidelity"]["columns"]["valid_to"]
    assert ended["bins"] == ["2025-10-01", "2025-10-02", "2025-10-03"]
    assert _time_bounds(tmp_path) == {
        "start": "2025-01-01T00:00:00.000Z",
        "end": "2025-10-02T00:00:00.000Z",
    }


def test_a_span_wholly_in_the_year_9999_beside_another_is_cut_and_noted_for_an_undeclared_end() -> (
    None
):
    column = ColumnPlan("opened_on", time_span=(_at("2025-01-01"), _at("2026-01-01")))
    column.declaration["bins"] = ["9999-02-01", "9999-03-01"]
    expose = AuthoredExpose("visit", kinds={"opened_on": LogicalKind.DATE}, index=0)
    contract = {
        "exposes": [
            {
                "contract": {"schema": []},
                "semantics": {
                    "dimensions": [
                        {
                            "name": "opened_on",
                            "type": "time",
                            "typeParams": {"validityParams": {"isEnd": True}},
                        }
                    ]
                },
            }
        ]
    }
    found, _, noted = _spans_of(expose, column, contract)
    assert found == [(_at("2025-01-01"), _at("2026-01-01"))]
    assert noted
