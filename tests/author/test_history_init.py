# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes a ``history`` entry from a table's published history (ADR 0017 section 13).

Pinned here, through ``fluxsim init`` and the seam:

* the entry written from a profile with a history: ``entities`` and ``versions``, ``start`` and
  ``length``, ``until``, ``chain``, ``tracked`` with its ``initial`` and ``transitions``,
  ``reasons`` and ``end``, and a declared range for each date column of the emit;
* the bar a ``chain`` level must reach, 90 % of consecutive versions, and no entry below it;
* every published member is used or recorded as unused, with its reason;
* a profile without history members writes no history, and the bundle is byte for byte what
  ``init`` wrote before;
* a written bundle resolves through the seam, and a history that the plan refuses is not written.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.author.columns import ColumnPlan
from flux_engine.author.history import CHAIN_HOLDS_AT_LEAST, HistorySource, write_history
from flux_engine.cli._init import run_init
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.history import history_entries_of
from flux_engine.seam.keys import compile_keys

from ._written import bundle_digests, resolved_emits

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
CONTRACT = Path(__file__).resolve().parents[1] / "fixtures" / "history-init" / "plans.fluid.yml"
SAMPLE = {"min": 1000, "max": 9999}
TIER = ["basic", "plus", "pro"]


def _ms(year: int, month: int, day: int) -> str:
    return str((date(year, month, day) - date(1970, 1, 1)).days * 86_400_000)


def _metrics(low: str, high: str, count: int) -> Dict[str, Any]:
    def metric(definition: str, value: Any) -> Dict[str, Any]:
        return {
            "definition": definition,
            "status": "F",
            "sampleSize": SAMPLE,
            "disclosure": "acro-default",
            "value": value,
        }

    return {
        "count": metric("count/1", count),
        "min": metric("min/1", low),
        "max": metric("max/1", high),
    }


def _statistic(
    definition: str, key: str, edges: List[str], counts: List[Any], k: int, **more: Any
) -> Dict[str, Any]:
    group: Dict[str, Any] = {"group": more.pop("group", ""), "k": k, key: counts, "status": "F"}
    if "cellStatus" in more:
        group["cellStatus"] = more.pop("cellStatus")
    node = {
        "definition": definition,
        "bins" if key == "binCounts" else "levels": edges,
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "groups": [group],
    }
    return {**node, **more}


def _bins(edges: List[str], counts: List[Any], k: int, **more: Any) -> Dict[str, Any]:
    return _statistic("marginalHistogram/1", "binCounts", edges, counts, k, **more)


def _levels(levels: List[str], counts: List[Any], k: int, **more: Any) -> Dict[str, Any]:
    return _statistic("levelHistogram/1", "levelCounts", levels, counts, k, **more)


def _transitions(column: str, levels: List[str], rows: Dict[str, List[int]]) -> Dict[str, Any]:
    node = _levels(levels, [0] * len(levels), 1)
    node["groupBy"] = column
    node["groups"] = [
        {"group": name, "k": sum(counts), "levelCounts": counts, "status": "F"}
        for name, counts in rows.items()
    ]
    return node


def _history() -> Dict[str, Any]:
    still = _transitions("channel", ["app", "web"], {"app": [0, 0], "web": [0, 0]})
    return {
        "entity": ["plan_id"],
        "start": "valid_from",
        "end": "valid_to",
        "versionsPerEntity": _bins(["1", "2", "3", "4"], [400, 350, 250], 1000),
        "firstStart": _bins(
            [_ms(2025, 1, 1), _ms(2025, 4, 1), _ms(2025, 7, 1), _ms(2025, 9, 1)],
            [420, 330, 250],
            1000,
        ),
        "versionLength": _bins(["1", "30", "90", "365"], [250, 350, 250], 850),
        "chain": _levels(["dayBefore", "gap", "nextStart", "overlap"], [20, 10, 800, 20], 850),
        "lastVersion": _levels(["closed", "open"], [150, 850], 1000),
        "columns": {
            "tier": {
                "initial": _levels(TIER, [500, 350, 150], 1000),
                "transitions": _transitions(
                    "tier",
                    TIER,
                    {"basic": [0, 300, 100], "plus": [100, 0, 200], "pro": [40, 60, 0]},
                ),
            },
            "channel": {"initial": _levels(["app", "web"], [600, 400], 1000), "transitions": still},
        },
        "reasons": _levels(["channel", "several", "tier"], [0, 50, 800], 850),
    }


def _profile(*, history: bool = True) -> Dict[str, Any]:
    """The profile of ``plans.fluid.yml``, with the history of ``plan_version`` when asked."""
    plan_version: Dict[str, Any] = {
        "rowCount": 1850,
        "columns": {
            "version_id": {"domain": "number", "metrics": _metrics("1", "1850", 1850)},
            "plan_id": {"domain": "number", "metrics": _metrics("1", "1000", 1850)},
            "account_id": {"domain": "number", "metrics": _metrics("1", "1000", 1850)},
            "valid_from": {
                "domain": "timestamp",
                "metrics": _metrics(_ms(2025, 1, 1), _ms(2025, 9, 1), 1850),
            },
            "valid_to": {
                "domain": "timestamp",
                "metrics": _metrics(_ms(2025, 2, 1), _ms(2025, 10, 1), 1850),
            },
            "tier": {
                "domain": "category",
                "statistics": [_levels(TIER, [1000, 600, 250], 1850)],
            },
            "channel": {
                "domain": "category",
                "statistics": [_levels(["app", "web"], [1100, 750], 1850)],
            },
            "signed_on": {
                "domain": "timestamp",
                "metrics": _metrics(_ms(2024, 6, 1), _ms(2025, 6, 1), 1850),
            },
        },
    }
    if history:
        plan_version["history"] = _history()
    return {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile/1-3-0/FluxProfile.json",
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {
            "acro-default": {
                "dominance": {"k": "0.90", "n": 2},
                "minimumCell": 10,
                "pRatio": "0.10",
                "roundingBase": 5,
                "zerosDisclosive": True,
            }
        },
        "tables": {
            "account": {
                "rowCount": 1000,
                "columns": {
                    "account_id": {"domain": "number", "metrics": _metrics("1", "1000", 1000)}
                },
            },
            "plan_version": plan_version,
        },
    }


def _init(tmp_path: Path, profile: Dict[str, Any], contract: Optional[str] = None) -> Any:
    (tmp_path / "plans.fluid.yml").write_text(
        CONTRACT.read_text(encoding="utf-8") if contract is None else contract, encoding="utf-8"
    )
    (tmp_path / "plans.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "plans.fluid.yml"),
        profile=str(tmp_path / "plans.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _namespace(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    found: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return found


def _entries(tmp_path: Path) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = _namespace(tmp_path).get("history", [])
    return found


def _states(result: Any) -> Dict[str, Dict[str, Any]]:
    return {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}


HISTORY = "/tables/plan_version/history"


def _two_columns(profile: Dict[str, Any]) -> Dict[str, Any]:
    """*profile* whose channel also moves, so two columns are tracked and ``reasons`` is read."""
    found = copy.deepcopy(profile)
    columns = found["tables"]["plan_version"]["history"]["columns"]
    columns["channel"]["transitions"] = _transitions(
        "channel", ["app", "web"], {"app": [0, 60], "web": [40, 0]}
    )
    history = found["tables"]["plan_version"]["history"]
    history["reasons"] = _levels(["channel", "several", "tier"], [100, 50, 700], 850)
    return found


def test_the_entry_is_written_from_the_published_history(tmp_path: Path) -> None:
    _init(tmp_path, _profile())
    assert _entries(tmp_path) == [
        {
            "emit": "plan_version",
            "entities": 1000,
            "versions": {"bins": ["1", "2", "3", "4"], "binWeights": ["0.4", "0.35", "0.25"]},
            "start": {
                "bins": ["2025-01-01", "2025-04-01", "2025-07-01", "2025-09-01"],
                "binWeights": ["0.42", "0.33", "0.25"],
            },
            "length": {
                "bins": ["1", "30", "90", "365"],
                "binWeights": ["0.294117647", "0.411764706", "0.294117647"],
                "closed": "lower",
            },
            "until": "2025-10-01",
            "chain": "nextStart",
            "parent": {"relationship": "account"},
            "tracked": [
                {
                    "column": "tier",
                    "initial": {"levels": TIER, "levelWeights": ["0.5", "0.35", "0.15"]},
                    "transitions": {
                        "basic": {"plus": "0.75", "pro": "0.25"},
                        "plus": {"basic": "0.333333333", "pro": "0.666666667"},
                        "pro": {"basic": "0.4", "plus": "0.6"},
                    },
                }
            ],
            # 800 moves of the tier among 850 changes: the other 50 move no tracked column.
            "untracked": "0.058823529",
            "end": {"mode": "closeLast", "share": "0.15"},
        }
    ]


def test_until_is_the_window_end_and_no_entry_writes_a_final_version_or_an_as_of(
    tmp_path: Path,
) -> None:
    _init(tmp_path, _profile())
    simulation = yaml.safe_load((tmp_path / "bundle" / "simulation.flux.yml").read_text("utf-8"))
    assert simulation["spec"]["timeBounds"]["end"] == "2025-10-01T00:00:00.000Z"
    entry = _entries(tmp_path)[0]
    assert entry["until"] == "2025-10-01"
    assert entry["end"]["mode"] == "closeLast"
    assert "asOf" not in _namespace(tmp_path)
    assert not {"openEnd", "activation"} & set(entry)


def test_two_tracked_columns_write_the_reasons_without_several(tmp_path: Path) -> None:
    result = _init(tmp_path, _two_columns(_profile()))
    entry = _entries(tmp_path)[0]
    assert [item["column"] for item in entry["tracked"]] == ["channel", "tier"]
    assert entry["tracked"][0]["transitions"] == {"app": {"web": "1"}, "web": {"app": "1"}}
    # 100 changes move the channel and 700 the tier; the 50 that move several are not written.
    assert entry["reasons"] == {"levels": ["channel", "tier"], "levelWeights": ["0.125", "0.875"]}
    states = _states(result)
    assert states[f"{HISTORY}/reasons"]["state"] == "used"
    assert states[f"{HISTORY}/columns/channel/transitions"]["state"] == "used"


def test_a_column_that_never_moves_is_carried_and_its_statistics_are_unused(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _profile())
    assert [item["column"] for item in _entries(tmp_path)[0]["tracked"]] == ["tier"]
    states = _states(result)
    for key in ("initial", "transitions"):
        state = states[f"{HISTORY}/columns/channel/{key}"]
        assert state["state"] == "unused"
        assert "no published change moves the column" in state["note"]
    assert states[f"{HISTORY}/columns/tier/initial"]["state"] == "used"


def _chain(profile: Dict[str, Any], counts: List[int]) -> Dict[str, Any]:
    found = copy.deepcopy(profile)
    found["tables"]["plan_version"]["history"]["chain"] = _levels(
        ["dayBefore", "gap", "nextStart", "overlap"], counts, sum(counts)
    )
    return found


def test_the_bar_is_ninety_percent_of_consecutive_versions(tmp_path: Path) -> None:
    assert CHAIN_HOLDS_AT_LEAST == Fraction(9, 10)
    cases = {
        "exactly": ([0, 50, 450, 0], "nextStart"),  # 450 of 500 is 90 %: it holds
        "dayBefore": ([470, 10, 20, 0], "dayBefore"),
        "just under": ([0, 51, 449, 0], None),  # 89.8 %
        "split": ([260, 0, 240, 0], None),  # neither holds
    }
    for name, (counts, expected) in cases.items():
        folder = tmp_path / name.replace(" ", "-")
        folder.mkdir()
        result = _init(folder, _chain(_profile(), counts))
        entries = _entries(folder)
        if expected is None:
            assert entries == [], name
            state = _states(result)[f"{HISTORY}/chain"]
            assert state["state"] == "unused"
            assert "the versions do not chain" in state["note"]
        else:
            assert entries[0]["chain"] == expected, name
            assert _states(result)[f"{HISTORY}/chain"]["state"] == "used"


def test_no_entry_leaves_every_published_member_unused_with_the_reason(tmp_path: Path) -> None:
    result = _init(tmp_path, _chain(_profile(), [0, 51, 449, 0]))
    states = _states(result)
    published = [pointer for pointer in states if pointer.startswith(HISTORY + "/")]
    assert len(published) == 10
    for pointer in published:
        assert states[pointer]["state"] == "unused", pointer
        assert "the versions do not chain" in states[pointer]["note"]
    # No member is read, so the register names the history whole, as the reader needs.
    register = _namespace(tmp_path)["unused"]
    pointers = {item["statistic"]["pointer"] for item in register}
    assert HISTORY in pointers
    assert not [pointer for pointer in pointers if pointer.startswith(HISTORY + "/")]


def test_every_published_member_is_used_or_unused_with_a_reason(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile())
    states = _states(result)
    published = [pointer for pointer in states if pointer.startswith(HISTORY + "/")]
    assert len(published) == 10
    for pointer in published:
        assert states[pointer]["state"] in ("used", "unused"), pointer
        assert states[pointer]["note"], pointer
        assert (
            "no rule of fluxsim init reads this statistic of a history"
            not in states[pointer]["note"]
        ), pointer
    unused = {pointer for pointer in published if states[pointer]["state"] == "unused"}
    assert unused == {
        f"{HISTORY}/reasons",
        f"{HISTORY}/columns/channel/initial",
        f"{HISTORY}/columns/channel/transitions",
    }
    # The tier's moves give the share of changes it makes, so the histogram is not read.
    assert "its moves give the share of changes" in states[f"{HISTORY}/reasons"]["note"]


def test_no_closed_last_version_writes_no_end(tmp_path: Path) -> None:
    profile = _profile()
    profile["tables"]["plan_version"]["history"]["lastVersion"] = _levels(
        ["closed", "open"], [0, 1000], 1000
    )
    result = _init(tmp_path, profile)
    assert "end" not in _entries(tmp_path)[0]
    state = _states(result)[f"{HISTORY}/lastVersion"]
    assert state["state"] == "used"
    assert "writes no end" in state["note"]


def test_first_starts_outside_the_window_are_left_unused_and_the_entry_keeps_the_rest(
    tmp_path: Path,
) -> None:
    profile = _profile()
    history = profile["tables"]["plan_version"]["history"]
    history["firstStart"] = _bins(
        [_ms(2025, 1, 1), _ms(2025, 4, 1), _ms(2026, 6, 1)], [600, 400], 1000
    )
    result = _init(tmp_path, profile)
    entry = _entries(tmp_path)[0]
    assert "start" not in entry and "length" in entry
    state = _states(result)[f"{HISTORY}/firstStart"]
    assert state["state"] == "unused"
    assert "outside [timeBounds.start, until]" in state["note"]


def test_a_length_bin_below_one_day_is_raised_to_one(tmp_path: Path) -> None:
    profile = _profile()
    profile["tables"]["plan_version"]["history"]["versionLength"] = _bins(
        ["0", "30", "90", "365"], [250, 350, 250], 850
    )
    result = _init(tmp_path, profile)
    assert _entries(tmp_path)[0]["length"]["bins"] == ["1", "30", "90", "365"]
    assert "raised to one day" in result.stdout


def test_a_date_column_with_no_published_range_gets_the_window_as_its_range(
    tmp_path: Path,
) -> None:
    profile = _profile()
    del profile["tables"]["plan_version"]["columns"]["signed_on"]["metrics"]
    _init(tmp_path, profile)
    columns = _namespace(tmp_path)["emits"]["plan_version"]["fidelity"]["columns"]
    window = yaml.safe_load((tmp_path / "bundle" / "simulation.flux.yml").read_text("utf-8"))[
        "spec"
    ]["timeBounds"]
    assert columns["signed_on"]["range"] == {
        "min": window["start"][:10],
        "max": window["end"][:10],
    }
    # A column with a published range keeps it, and the history's own columns are not given one.
    assert columns["valid_from"]["range"] == {"min": "2025-01-01", "max": "2025-09-01"}


def test_a_profile_without_a_history_writes_no_history_and_the_bytes_it_always_did(
    tmp_path: Path,
) -> None:
    _init(tmp_path, _profile(history=False))
    assert "history" not in _namespace(tmp_path)
    digests = bundle_digests(tmp_path)
    # Pinned from the files `fluxsim init` wrote for this contract and profile before histories,
    # but for the check of `account`, which the profile has no statistic to score: it is left out,
    # and for the header, which no longer names the profile's file.
    assert digests == WITHOUT_HISTORY


#: sha256 of each file of the bundle written for the profile with no history.
WITHOUT_HISTORY: Dict[str, str] = {
    "persona.flux.yml": "74f6062c0e15e70d2689583ad345103c7189b244886c3280c244573b49677755",
    "plans.fluid.yml": "a36ebfc4005a034a13b34c5ab0397ba8192c66a10d5616fced07c2fd1dd29d27",
    "simulation.flux.yml": "a9b19c6e6d779786df3d174b8ab2477604182941065696fd9fc477796f2dbafc",
    "world.flux.yml": "20a3e0be50c0216bb8ef8ac07cdc72b0100a3cf4bddccbb91b7841fcc96b88cc",
}


def test_a_written_bundle_resolves_through_the_seam(tmp_path: Path) -> None:
    _init(tmp_path, _profile())
    emits = resolved_emits(tmp_path)
    entries = history_entries_of(emits["plan_version"].extension, "plan_version")
    assert [entry.entities for entry in entries] == [1000]
    assert entries[0].chain == "nextStart" and entries[0].until == "2025-10-01"
    assert [tracked.column for tracked in entries[0].tracked] == ["tier"]
    assert history_entries_of(emits["account"].extension, "account") == ()


def _source(contract: str, table: Dict[str, Any]) -> HistorySource:
    document = yaml.safe_load(contract)
    expose = next(item for item in document["exposes"] if item["exposeId"] == "plan_version")
    compiled = compile_expose(expose)
    keys = compile_keys(document)
    assert keys is not None
    plans = {
        "tier": ColumnPlan("tier", {"levels": {"basic": "0.5", "plus": "0.3", "pro": "0.2"}}),
        "channel": ColumnPlan("channel", {"levels": {"app": "0.6", "web": "0.4"}}),
    }
    return HistorySource(
        expose_id="plan_version",
        table=table,
        rows=1850,
        plans=plans,
        kinds={column.name: column.logical_type.kind for column in compiled.columns},
        contract_expose=expose,
        keys=keys.expose("plan_version"),
        window=("2024-06-01T00:00:00.000Z", "2025-10-01T00:00:00.000Z"),
        minimum_cell=10,
        fractional_bits=16,
    )


def test_no_parent_is_written_when_a_relationship_is_on_the_natural_entity() -> None:
    # A to-one relationship whose fromColumns are the natural entity's column would make the natural
    # key a foreign key, which the generator refuses, so init writes no parent for it.
    contract = CONTRACT.read_text(encoding="utf-8").replace("expr: plan_id }", "expr: account_id }")
    table = _profile()["tables"]["plan_version"]
    table["history"]["entity"] = ["account_id"]
    written = write_history(_source(contract, table))
    assert written.entry is not None
    assert "parent" not in written.entry


def test_a_history_the_plan_refuses_is_not_written(tmp_path: Path) -> None:
    # The natural entity is also the foreign key of a relationship, and a history lays the natural
    # entity out over its entities on its own, so the plan refuses the entry.
    contract = CONTRACT.read_text(encoding="utf-8").replace("expr: plan_id }", "expr: account_id }")
    profile = _profile()
    profile["tables"]["plan_version"]["history"]["entity"] = ["account_id"]
    result = _init(tmp_path, profile, contract)
    assert _entries(tmp_path) == []
    state = _states(result)[f"{HISTORY}/versionsPerEntity"]
    assert state["state"] == "unused"
    assert "refused, so none is written" in state["note"]


def _overlaid(tmp_path: Path, rows: Dict[str, int]) -> Any:
    """``init`` with an overlay that sets the ``rowCount`` of each table in *rows*."""
    (tmp_path / "plans.fluid.yml").write_text(
        CONTRACT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "plans.profile.json").write_text(json.dumps(_profile()), encoding="utf-8")
    patch = {"tables": {name: {"rowCount": count} for name, count in rows.items()}}
    overlay_file = tmp_path / "rows.yml"
    overlay_file.write_text(json.dumps({"name": "rows", "patch": patch}), encoding="utf-8")
    return run_init(
        str(tmp_path / "bundles"),
        contract=str(tmp_path / "plans.fluid.yml"),
        profile=str(tmp_path / "plans.profile.json"),
        overlays=(str(overlay_file),),
    )


def test_an_overlay_that_changes_the_rows_of_a_table_with_a_history_is_refused(
    tmp_path: Path,
) -> None:
    """More rows would make each chain longer and add no entity: init says so and writes nothing."""
    result = _overlaid(tmp_path, {"plan_version": 3700})
    assert (result.exit_code, result.error_code) == (1, "FLX-3105")
    assert "rowCount of table 'plan_version'" in result.stderr
    assert "publishes a history" in result.stderr and "history.entities" in result.stderr
    assert not (tmp_path / "bundles").exists() or not list((tmp_path / "bundles").iterdir())


def test_an_overlay_may_change_a_table_without_a_history_or_restate_the_rows_of_one_with(
    tmp_path: Path,
) -> None:
    result = _overlaid(tmp_path, {"account": 1200, "plan_version": 1850})
    assert result.exit_code == 0, result.stderr
