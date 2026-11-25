# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init --history FILE`` merges a declared history, its projections and the window into the bundle.

Pinned here, through ``run_init`` and the seam:

* the bundle written with a file that holds a parent, a tracked column, ``end: finalVersion`` on a
  carried status column, a snapshot, events and periods plans and runs;
* it holds the file's keys as given, and the counted layouts, exact allotments and register that
  fit them: no counts entry over a projected emit or the history's parent relationship, the
  statistics of those listed unused with their reasons, no ``allocation: exact`` on a history or a
  projected emit, the ended status's weight 0, and every register entry on a counted number;
* each refusal of the file names it, and a plan refusal is reported with its code, not answered by
  writing the bundle without the history.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.seam.counts import COUNTS_KEY
from flux_engine.seam.history import HISTORY_KEY
from flux_engine.seam.projections import EVENTS_KEY, PERIODS_KEY, SNAPSHOTS_KEY
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan

from .test_history_init import NAMESPACE, _bins, _levels, _namespace, _profile
from .test_init_from_profile import _simulation
from .test_projection_init import CONTRACT, _profile_with

pytestmark = pytest.mark.integration

WRITTEN = ("plan_current", "plan_change", "plan_month")
STATUS = ["active", "ended"]

HISTORY_FILE: Dict[str, Any] = {
    "timeBounds": {"start": "2024-01-01", "end": "2025-12-31"},
    "history": [
        {
            "emit": "plan_version",
            "entities": 1000,
            "parent": {"relationship": "account", "minChildren": 1},
            "versions": {"bins": ["1", "2", "4"], "binWeights": ["0.6", "0.4"]},
            "start": {"bins": ["2025-01-01", "2025-06-01"], "binWeights": ["1"]},
            "length": {
                "bins": ["30", "90", "365"],
                "binWeights": ["0.5", "0.5"],
                "closed": "lower",
            },
            "until": "2025-12-31",
            "chain": "nextStart",
            "tracked": [
                {
                    "column": "tier",
                    "initial": {
                        "levels": ["basic", "plus", "pro"],
                        "levelWeights": ["0.5", "0.3", "0.2"],
                    },
                    "transitions": {
                        "basic": {"plus": "0.6", "pro": "0.4"},
                        "plus": {"basic": "0.5", "pro": "0.5"},
                        "pro": {"basic": "0.5", "plus": "0.5"},
                    },
                }
            ],
            "end": {"mode": "finalVersion", "share": "0.2", "column": "status", "value": "ended"},
        }
    ],
    "snapshots": [{"emit": "plan_current", "of": "plan_version", "ended": "keep"}],
    "events": [
        {
            "emit": "plan_change",
            "of": "plan_version",
            "date": "recorded_on",
            "op": "op",
            "before": {"before_tier": "tier"},
            "after": {"after_tier": "tier"},
        }
    ],
    "periods": [
        {
            "emit": "plan_month",
            "of": "plan_version",
            "column": "month",
            "grain": "month",
            "match": "overlap",
            "kinds": [{"count": 1}],
        }
    ],
}


def _contract() -> str:
    """The projection fixture's contract, with a ``status`` column on the history and its snapshot."""
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    for expose in document["exposes"]:
        if expose["exposeId"] in ("plan_version", "plan_current"):
            expose["contract"]["schema"].append(
                {"name": "status", "type": "STRING", "required": True}
            )
            expose["semantics"].setdefault("dimensions", []).append(
                {"name": "status", "type": "categorical"}
            )
    return yaml.safe_dump(document, sort_keys=False)


def _profile_for_status() -> Dict[str, Any]:
    found = copy.deepcopy(_profile_with(*WRITTEN))
    status = {"domain": "category", "statistics": [_levels(STATUS, [1500, 350], 1850)]}
    found["tables"]["plan_version"]["columns"]["status"] = status
    found["tables"]["plan_current"]["columns"]["status"] = {
        "domain": "category",
        "statistics": [_levels(STATUS, [800, 200], 1000)],
    }
    # The profile publishes no history for the table: the file declares it.
    found["tables"]["plan_version"].pop("history", None)
    return found


def _write(tmp_path: Path, document: Any, name: str = "history.yml") -> Path:
    path = tmp_path / name
    path.write_text(
        document if isinstance(document, str) else yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _inputs(tmp_path: Path, profile: Optional[Dict[str, Any]] = None) -> None:
    (tmp_path / "plans.fluid.yml").write_text(_contract(), encoding="utf-8")
    (tmp_path / "plans.profile.json").write_text(
        json.dumps(_profile_for_status() if profile is None else profile), encoding="utf-8"
    )


def _init(tmp_path: Path, history: Any, **options: Any) -> Any:
    if not (tmp_path / "plans.fluid.yml").exists():
        _inputs(tmp_path)
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "plans.fluid.yml"),
        profile=str(tmp_path / "plans.profile.json"),
        history=str(history),
        **options,
    )


def _declared(tmp_path: Path, document: Any = HISTORY_FILE) -> Any:
    result = _init(tmp_path, _write(tmp_path, document))
    assert result.exit_code == 0, result.stderr
    return result


# --- what the bundle holds ------------------------------------------------------------------


def test_the_bundle_holds_the_files_keys_and_its_window(tmp_path: Path) -> None:
    result = _declared(tmp_path)
    namespace = _namespace(tmp_path)
    assert namespace[HISTORY_KEY] == HISTORY_FILE["history"]
    assert namespace[SNAPSHOTS_KEY] == HISTORY_FILE["snapshots"]
    assert namespace[EVENTS_KEY] == HISTORY_FILE["events"]
    assert namespace[PERIODS_KEY] == HISTORY_FILE["periods"]
    assert _simulation(tmp_path / "bundle")["spec"]["timeBounds"] == {
        "start": "2024-01-01T00:00:00.000Z",
        "end": "2025-12-31T00:00:00.000Z",
    }
    assert result.artifacts["historyFile"] == "history.yml"
    notes = {subject: sentence for subject, sentence in _notes(result)}
    for subject in (
        "plan_version: history",
        "plan_current: snapshots",
        "plan_change: events",
        "plan_month: periods",
    ):
        assert notes[subject] == "declared by --history history.yml"


def _notes(result: Any) -> List[Any]:
    return [(item["subject"], item["assumption"]) for item in result.artifacts["notes"]]


def test_the_bundle_plans_and_runs(tmp_path: Path) -> None:
    _declared(tmp_path)
    state = tmp_path / "state"
    planned = plan(
        tmp_path / "bundle",
        state_dir=state,
        profile=tmp_path / "plans.profile.json",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr
    applied = apply(tmp_path / "bundle", run_id=planned.artifacts["run_id"], state_dir=state)
    assert applied.success, applied.stderr


def test_no_counts_entry_lays_out_a_projected_emit_or_the_historys_parent(tmp_path: Path) -> None:
    from .test_history_init import _states

    _inputs(tmp_path, _with_relationships(_profile_for_status()))
    result = _declared(tmp_path)
    assert not _namespace(tmp_path).get(COUNTS_KEY)
    states = _states(result)
    assert states["/relationships/0/statistics/0"]["state"] == "unused"
    assert "parent lays its entities out over this relationship" in (
        states["/relationships/0/statistics/0"]["note"]
    )
    for index in (1, 2):
        state = states[f"/relationships/{index}/statistics/0"]
        assert state["state"] == "unused"
        assert "projected from the declared history, whose layout places its rows" in (
            state["note"]
        )


def _with_relationships(profile: Dict[str, Any]) -> Dict[str, Any]:
    """*profile* with the children per parent of the history's parent and of two projections."""
    found = copy.deepcopy(profile)
    found["relationships"] = [
        {
            "parent": {"table": parent, "columns": [column]},
            "child": {"table": child, "columns": [column]},
            "statistics": [_children_per_parent(rows)],
        }
        for parent, child, column, rows in (
            ("account", "plan_version", "account_id", 1000),
            ("plan_version", "plan_month", "plan_id", 1000),
            ("plan_version", "plan_change", "plan_id", 1000),
        )
    ]
    return found


def _children_per_parent(parents: int) -> Dict[str, Any]:
    edges = ["0", "1", "2", "4"]
    cells = [parents // 5, parents // 2, parents - parents // 5 - parents // 2]
    return _bins(edges, cells, parents)


def test_each_statistic_a_profile_publishes_of_the_history_is_unused_for_the_file(
    tmp_path: Path,
) -> None:
    from .test_history_init import HISTORY, _states

    profile = _profile_for_status()
    profile["tables"]["plan_version"]["history"] = _profile()["tables"]["plan_version"]["history"]
    _inputs(tmp_path, profile)
    result = _declared(tmp_path)
    states = _states(result)
    published = [pointer for pointer in states if pointer.startswith(HISTORY + "/")]
    assert published
    for pointer in published:
        assert states[pointer]["state"] == "unused"
        assert "--history declares the bundle's histories" in states[pointer]["note"]


def test_no_history_or_projected_column_asks_for_an_exact_allotment(tmp_path: Path) -> None:
    _declared(tmp_path)
    emits = _namespace(tmp_path)["emits"]
    for emit in ("plan_version", "plan_current", "plan_change", "plan_month"):
        for declaration in emits[emit]["fidelity"]["columns"].values():
            assert declaration.get("allocation") != "exact"


def test_the_ended_status_has_no_weight_on_a_carried_end_column(tmp_path: Path) -> None:
    _declared(tmp_path)
    columns = _namespace(tmp_path)["emits"]["plan_version"]["fidelity"]["columns"]
    assert columns["status"]["levels"] == {"active": "1", "ended": "0"}
    register = _namespace(tmp_path)["parameters"]
    reasons = [
        entry
        for entry in register
        if entry["pointer"].endswith("/plan_version/fidelity/columns/status/levels/ended")
    ]
    assert reasons and "last version only" in reasons[0]["reason"]


def test_every_register_entry_points_at_a_counted_number(tmp_path: Path) -> None:
    from flux_engine.seam.parameters import declared_numbers, parse_pointer
    from flux_stats.exact_yaml import load_documents

    _inputs(tmp_path, _with_relationships(_profile_for_status()))
    _declared(tmp_path)
    (document,) = load_documents(
        (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8"), name="sim"
    )
    numbers = declared_numbers(
        {document["id"]: document}, simulation_id=document["id"], namespace=NAMESPACE
    )
    under = {
        (number.document, number.tokens[:depth])
        for number in numbers
        for depth in range(len(number.tokens) + 1)
    }
    register = document["extensions"][NAMESPACE]["parameters"]
    assert register
    for entry in register:
        if entry["document"] != document["id"]:
            continue  # the World and the Persona carry one default each
        assert (entry["document"], parse_pointer(entry["pointer"])) in under, entry["pointer"]


def test_a_declared_history_cites_no_statistic_of_the_profiles_history(tmp_path: Path) -> None:
    _declared(tmp_path)
    planned = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "plans.profile.json",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr
    namespace = _simulation(tmp_path / "bundle")["extensions"][NAMESPACE]
    written = namespace[HISTORY_KEY]
    assert written
    register = namespace["parameters"]
    history_entries = [entry for entry in register if f"/{HISTORY_KEY}/" in entry["pointer"]]
    assert history_entries
    for entry in register:
        statistic = entry.get("statistic")
        if statistic is not None:
            assert "/history/" not in statistic["pointer"], entry
    for entry in history_entries:
        assert entry["source"] == "assumption"
        assert "declared by --history" in entry["reason"]


# --- what init refuses ----------------------------------------------------------------------


def _refused(tmp_path: Path, document: Any, *, code: int = 1, **options: Any) -> str:
    result = _init(tmp_path, _write(tmp_path, document), **options)
    assert result.exit_code == code, result.stdout
    assert not (tmp_path / "bundle").exists()
    return str(result.stderr)


def test_an_unknown_key_is_refused_by_name(tmp_path: Path) -> None:
    message = _refused(tmp_path, {**HISTORY_FILE, "versions": []})
    assert "history.yml" in message and "versions" in message


@pytest.mark.parametrize("document", [{"snapshots": []}, {"history": []}, {"history": "x"}])
def test_a_missing_or_empty_history_is_refused(tmp_path: Path, document: Dict[str, Any]) -> None:
    assert "history.yml" in _refused(tmp_path, document)


def test_an_emit_that_names_nothing_written_is_refused_naming_the_entry(tmp_path: Path) -> None:
    document = copy.deepcopy(HISTORY_FILE)
    document["history"][0]["emit"] = "nobody"
    assert "history[0].emit" in _refused(tmp_path, document)
    document = copy.deepcopy(HISTORY_FILE)
    document["history"][0]["emit"] = "plan_invoice"
    assert "history[0].emit" in _refused(tmp_path, document)
    document = copy.deepcopy(HISTORY_FILE)
    document["snapshots"][0]["of"] = "plan_current"
    assert "snapshots[0].of" in _refused(tmp_path, document)
    document = copy.deepcopy(HISTORY_FILE)
    document["events"][0]["emit"] = "plan_version"
    assert "events[0].emit" in _refused(tmp_path, document)


def test_the_window_is_given_once(tmp_path: Path) -> None:
    message = _refused(tmp_path, HISTORY_FILE, code=2, window=("2025-01-01", "2025-06-01"))
    assert "timeBounds" in message and "--window" in message


def test_the_window_of_the_file_is_checked_as_window_is(tmp_path: Path) -> None:
    document = {**HISTORY_FILE, "timeBounds": {"start": "2025-12-31", "end": "2025-01-01"}}
    assert "timeBounds" in _refused(tmp_path, document)


def test_a_file_that_is_not_a_regular_file_or_text_or_one_document_is_refused(
    tmp_path: Path,
) -> None:
    _inputs(tmp_path)
    directory = tmp_path / "dir.yml"
    directory.mkdir()
    for path in (directory, tmp_path / "missing.yml"):
        result = _init(tmp_path, path)
        assert result.exit_code == 1 and path.name in result.stderr
    binary = tmp_path / "binary.yml"
    binary.write_bytes(b"history: \xff\xfe\n")
    result = _init(tmp_path, binary)
    assert result.exit_code == 1 and "not UTF-8" in result.stderr
    two = _write(tmp_path, "history: [{emit: a}]\n---\nhistory: [{emit: b}]\n", "two.yml")
    result = _init(tmp_path, two)
    assert result.exit_code == 1 and "one document" in result.stderr


def test_a_file_past_the_size_bound_is_refused(tmp_path: Path) -> None:
    from flux_engine.seam.resolve import MAX_FILE_BYTES

    _inputs(tmp_path)
    big = tmp_path / "big.yml"
    big.write_text("history: []\n" + "# " + "x" * MAX_FILE_BYTES, encoding="utf-8")
    result = _init(tmp_path, big)
    assert result.exit_code == 1 and "big.yml" in result.stderr


def test_a_plan_refusal_is_reported_with_its_code_and_the_bundle_is_not_written(
    tmp_path: Path,
) -> None:
    document = copy.deepcopy(HISTORY_FILE)
    document["history"][0]["tracked"][0]["column"] = "channel"
    document["history"][0]["tracked"][0]["transitions"] = {}
    result = _init(tmp_path, _write(tmp_path, document))
    assert result.exit_code == 1
    assert "history.yml" in result.stderr and "FLX-" in result.stderr
    assert result.error_code and result.error_code.startswith("FLX-") or "FLX-" in result.stderr
    assert not (tmp_path / "bundle").exists()


def test_history_needs_a_contract_and_a_profile(tmp_path: Path) -> None:
    result = run_init(str(tmp_path / "bundle"), history=str(_write(tmp_path, HISTORY_FILE)))
    assert result.exit_code == 2 and "--history" in result.stderr


def test_each_overlay_bundle_takes_the_history(tmp_path: Path) -> None:
    _inputs(tmp_path)
    overlay = tmp_path / "bigger.yml"
    overlay.write_text(
        "name: bigger\npatch:\n  tables:\n    plan_invoice:\n      rowCount: 3600\n",
        encoding="utf-8",
    )
    result = _init(tmp_path, _write(tmp_path, HISTORY_FILE), overlays=(str(overlay),))
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "bigger" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    assert block[HISTORY_KEY] == HISTORY_FILE["history"]
    assert block["emits"]["plan_invoice"]["rows"] == 3600


def test_a_number_the_file_writes_unquoted_is_kept_exactly(tmp_path: Path) -> None:
    text = (
        yaml.safe_dump(HISTORY_FILE, sort_keys=False)
        .replace("'0.6'", "0.6")
        .replace("'0.4'", "0.4")
    )
    assert "binWeights:\n    - 0.6" in text
    result = _init(tmp_path, _write(tmp_path, text))
    assert result.exit_code == 0, result.stderr
    assert _namespace(tmp_path)[HISTORY_KEY][0]["versions"]["binWeights"] == ["0.6", "0.4"]


def test_every_counted_number_under_the_declared_keys_is_assumed(tmp_path: Path) -> None:
    from flux_engine.seam.parameters import declared_numbers, parse_pointer
    from flux_stats.exact_yaml import load_documents

    document = copy.deepcopy(HISTORY_FILE)
    document["history"][0]["parent"]["children"] = {
        "bins": ["1", "2", "4"],
        "binWeights": ["0.5", "0.5"],
    }
    document["events"][0]["lag"] = {"bins": ["0", "5", "30"], "binWeights": ["0.7", "0.3"]}
    _declared(tmp_path, document)
    (simulation,) = load_documents(
        (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8"), name="sim"
    )
    numbers = declared_numbers(
        {simulation["id"]: simulation}, simulation_id=simulation["id"], namespace=NAMESPACE
    )
    under = [
        number
        for number in numbers
        if number.tokens[2:3]
        in (("history",), ("snapshots",), ("events",), ("periods",), ("asOf",))
    ]
    reached = {"parent.minChildren": False, "parent.children": False, "lag": False}
    register = _namespace(tmp_path)["parameters"]
    for number in under:
        entries = [
            entry
            for entry in register
            if entry["document"] == simulation["id"]
            and number.tokens[: len(parse_pointer(entry["pointer"]))]
            == parse_pointer(entry["pointer"])
        ]
        assert entries, number.tokens
        deepest = max(entries, key=lambda entry: len(parse_pointer(entry["pointer"])))
        assert deepest["source"] == "assumption", number.tokens
        assert "declared by --history" in deepest["reason"], number.tokens
        tail = number.tokens[4:]
        reached["parent.minChildren"] |= tail[:2] == ("parent", "minChildren")
        reached["parent.children"] |= tail[:2] == ("parent", "children")
        reached["lag"] |= tail[:1] == ("lag",)
    assert all(reached.values()), reached


def test_a_contract_range_ending_in_year_9999_leaves_the_window_of_a_file_plannable(
    tmp_path: Path,
) -> None:
    document = yaml.safe_load(_contract())
    for expose in document["exposes"]:
        for column in expose["contract"]["schema"]:
            if column["name"] == "signed_on":
                column["validationRules"] = [
                    {"type": "range", "constraint": "between '1900-01-01' and '9999-12-31'"}
                ]
    (tmp_path / "plans.fluid.yml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / "plans.profile.json").write_text(
        json.dumps(_profile_for_status()), encoding="utf-8"
    )
    declared = {key: value for key, value in HISTORY_FILE.items() if key != "timeBounds"}
    result = _init(tmp_path, _write(tmp_path, declared))
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    assert int(yaml.safe_load(text)["spec"]["timeBounds"]["end"][:4]) < 9999
    planned = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "plans.profile.json",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr
