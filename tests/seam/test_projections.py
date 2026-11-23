# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation's projection entries (ADR 0027 sections 1 to 5): read, checked, bound into ``H``.

The bundle is written here: a ``customer`` expose, a ``subscription_version`` expose with a
validity window and a history, and one projected expose for each key. A bundle that declares no
entry hashes as it always did; an entry moves ``H`` of its own emit and neither ``G`` nor any
other emit's ``H``. Until the generators land, ``plan`` reads a projection and refuses to
generate it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

import pytest
import yaml

from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.seam.counts import CountsEntry
from flux_engine.seam.emit import seam_digests
from flux_engine.seam.history import HistoryEntry, read_history_block
from flux_engine.seam.projections import (
    EVENTS_KEY,
    PERIODS_KEY,
    PROJECTS_KEY,
    SNAPSHOTS_KEY,
    SnapshotEntry,
    bound_into_h,
    check_projected_emit,
    projection_keys_of,
    read_events_block,
    read_periods_block,
    read_snapshots_block,
    refuse_projection,
)
from flux_engine.seam.resolve import (
    FLX_3105,
    load_bundle,
    load_documents_exact,
    resolve_all_emits,
)
from tests.seam.test_history import ENTRY, PRODUCT, SIMULATION, _emits, contract, entry_with
from tests.stages.test_counts_run import FIXTURE as EXACT_COUNTS

pytestmark = pytest.mark.unit

SNAPSHOT: Dict[str, Any] = {"emit": "subscription_current", "of": "subscription_version"}
EVENT: Dict[str, Any] = {
    "emit": "subscription_change",
    "of": "subscription_version",
    "date": "recorded_at",
    "at": "changed_at",
    "lag": {"bins": ["0", "3", "7"], "binWeights": ["0.7", "0.3"]},
    "on": ["c", "u", "d"],
    "op": "op",
    "type": {
        "column": "kind",
        "values": {"c": "create", "d": "delete", "end": "cancel", "plan": "plan_change"},
    },
    "before": {"before_plan": "plan"},
    "after": {"after_plan": "plan"},
    "when": "plan <> 'team'",
}
PERIOD: Dict[str, Any] = {
    "emit": "subscription_month",
    "of": "subscription_version",
    "column": "month",
    "grain": "month",
    "match": "overlap",
    "kinds": [
        {"count": 2, "when": "plan = 'pro'", "overrides": {"label": "pro"}},
        {
            "count": {"levels": ["1", "3"], "levelWeights": ["0.5", "0.5"]},
            "when": "month IS NOT NULL AND previous.plan IS NULL",
            "overrides": {
                "quota": "5",
                "price": "9.50",
                "active": "true",
                "seen": "2026-07-01T00:00:00Z",
                "stamp_day": "2026-07-01",
            },
        },
    ],
}
EMITTED = (
    "customer",
    "subscription_version",
    "subscription_current",
    "subscription_change",
    "subscription_month",
)


def _binding(table: str) -> Dict[str, Any]:
    return {
        "platform": "aws",
        "format": "glue_table",
        "location": {"database": "retail", "table": table},
    }


def _current() -> Dict[str, Any]:
    """The snapshot's expose: columns of the history's emit, with the same names and types."""
    return {
        "exposeId": "subscription_current",
        "kind": "table",
        "binding": _binding("sc"),
        "contract": {
            "schema": [
                {"name": "subscription_id", "type": "INT", "required": True},
                {"name": "customer_id", "type": "INT", "required": True},
                {"name": "plan", "type": "STRING", "required": True},
                {"name": "status", "type": "STRING", "required": True},
                {"name": "valid_from", "type": "DATE", "required": True},
            ]
        },
        "semantics": {
            "entities": [{"name": "subscription", "type": "primary", "expr": "subscription_id"}]
        },
    }


def _change() -> Dict[str, Any]:
    """The events' expose: a key, a date and an instant, an op, a type and before and after columns."""
    columns = [
        ("event_id", "INT", True),
        ("subscription_id", "INT", True),
        ("op", "STRING", True),
        ("kind", "STRING", True),
        ("n", "INT", False),
        ("changed_at", "DATE", True),
        ("recorded_at", "DATE", True),
        ("recorded_ts", "TIMESTAMP", True),
        ("changed_ts", "TIMESTAMP", True),
        ("before_plan", "STRING", False),
        ("after_plan", "STRING", False),
        ("before_seats", "INT", False),
        ("required_before", "STRING", True),
        ("required_after", "STRING", True),
    ]
    return {
        "exposeId": "subscription_change",
        "kind": "table",
        "binding": _binding("sch"),
        "contract": {"schema": [{"name": n, "type": t, "required": r} for n, t, r in columns]},
        "semantics": {"entities": [{"name": "event", "type": "primary", "expr": "event_id"}]},
    }


def _month() -> Dict[str, Any]:
    """The periodic expose: a period column with a grain, columns of the history, and others."""
    columns = [
        ("period_id", "INT", True),
        ("subscription_id", "INT", True),
        ("month", "DATE", True),
        ("stamp_day", "DATE", False),
        ("valid_from", "DATE", False),
        ("plan", "STRING", True),
        ("label", "STRING", False),
        ("quota", "INT", False),
        ("price", "DECIMAL(10,2)", False),
        ("active", "BOOL", False),
        ("seen", "TIMESTAMP", False),
    ]
    return {
        "exposeId": "subscription_month",
        "kind": "table",
        "binding": _binding("sm"),
        "contract": {"schema": [{"name": n, "type": t, "required": r} for n, t, r in columns]},
        "semantics": {
            "entities": [{"name": "period", "type": "primary", "expr": "period_id"}],
            "dimensions": [
                {"name": "month", "type": "time", "typeParams": {"timeGranularity": "month"}},
                {"name": "stamp_day", "type": "time"},
            ],
        },
    }


def _ledger() -> Dict[str, Any]:
    """An expose whose relationship has the periodic emit as its parent."""
    return {
        "exposeId": "month_ledger",
        "kind": "table",
        "binding": _binding("ml"),
        "contract": {
            "schema": [
                {"name": "ledger_id", "type": "INT", "required": True},
                {"name": "period_id", "type": "INT", "required": True},
            ]
        },
        "semantics": {
            "entities": [{"name": "ledger", "type": "primary", "expr": "ledger_id"}],
            "relationships": [
                {
                    "name": "period",
                    "to": "subscription_month",
                    "fromColumns": ["period_id"],
                    "toColumns": ["period_id"],
                    "cardinality": "many_to_one",
                    "joinType": "inner",
                }
            ],
        },
    }


def bundle(
    tmp_path: Path,
    name: str,
    *,
    snapshots: Any = None,
    events: Any = None,
    periods: Any = None,
    ledger: bool = False,
    history: Any = (ENTRY,),
    current: Optional[Dict[str, Any]] = None,
    projected_rows: Optional[int] = None,
    namespace: Optional[Mapping[str, Any]] = None,
) -> Path:
    """A bundle of the three exposes, with *snapshots* as its ``snapshots`` key."""
    root = tmp_path / name
    root.mkdir()
    fluid = contract()
    fluid["exposes"].append(current if current is not None else _current())
    fluid["exposes"].append(_change())
    fluid["exposes"].append(_month())
    if ledger:
        fluid["exposes"].append(_ledger())
    (root / "subscriptions.fluid.yml").write_text(yaml.safe_dump(fluid, sort_keys=False), "utf-8")
    (document,) = load_documents_exact(root / "subscriptions.fluid.yml")
    digest = digest_of_document(document, subject=f"contractDigest:{PRODUCT}").value
    for fixture_name in ("world.flux.yml", "persona.flux.yml"):
        text = (EXACT_COUNTS / fixture_name).read_text("utf-8")
        (root / fixture_name).write_text(text, "utf-8")
    emits = _emits("DATE")
    if projected_rows is not None:
        emits["subscription_current"] = {"rows": projected_rows}
    extensions: Dict[str, Any] = {"emits": emits, **(namespace or {})}
    if history:
        extensions["history"] = list(history)
    if snapshots is not None:
        extensions[SNAPSHOTS_KEY] = snapshots
    if events is not None:
        extensions[EVENTS_KEY] = events
    if periods is not None:
        extensions[PERIODS_KEY] = periods
    simulation = {
        "fluxVersion": "0.5.0",
        "kind": "Simulation",
        "id": SIMULATION,
        "name": "Subscriptions",
        "metadata": {"owner": {"team": "sales"}},
        "spec": {
            "worldRef": "world-sales",
            "fidelity": "deterministic",
            "seed": 42,
            "timeBounds": {"start": "2026-07-01T00:00:00Z", "end": "2026-09-30T23:59:59Z"},
            "personaRefs": ["persona-sales"],
            "emits": [
                {
                    "productRef": PRODUCT,
                    "exposeId": expose_id,
                    "fluidVersion": "0.7.7",
                    "provenance": {"contractDigest": digest},
                }
                for expose_id in (*EMITTED, *(("month_ledger",) if ledger else ()))
            ],
        },
        "extensions": {"com.agentics.flux/1": extensions},
    }
    (root / "simulation.flux.yml").write_text(yaml.safe_dump(simulation, sort_keys=False), "utf-8")
    return root


def resolved(path: Path) -> Dict[str, Any]:
    return {
        emit.expose_id: emit
        for emit in resolve_all_emits(load_bundle(path), simulation_id=SIMULATION)
    }


def digests(path: Path) -> Dict[str, Tuple[str, str]]:
    found = {}
    for expose_id, emit in resolved(path).items():
        pair = seam_digests(
            emit.contract_document, emit.emit, emit.compile().column_names, emit.extension
        )
        found[expose_id] = (pair.h.value, pair.g.value)
    return found


def refused(path: Path, code: str) -> str:
    with pytest.raises(FluxError) as caught:
        resolved(path)
    assert caught.value.code.code == code
    return str(caught.value)


def current_without(*names: str) -> Dict[str, Any]:
    expose = _current()
    schema = expose["contract"]["schema"]
    expose["contract"]["schema"] = [column for column in schema if column["name"] not in names]
    return expose


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Tuple[str, str]]:
    return digests(bundle(tmp_path_factory.mktemp("plain"), "plain"))


# --- snapshots: reads ----------------------------------------------------------------------


def test_a_snapshot_reads_and_defaults_ended_to_keep(tmp_path: Path) -> None:
    emits = resolved(bundle(tmp_path, "keep", snapshots=[SNAPSHOT]))
    extension = emits["subscription_current"].extension or {}
    assert extension[SNAPSHOTS_KEY] == {"of": "subscription_version", "ended": "keep"}
    assert projection_keys_of(extension) == (SNAPSHOTS_KEY,)


def test_ended_drop_reads(tmp_path: Path) -> None:
    emits = resolved(bundle(tmp_path, "drop", snapshots=[{**SNAPSHOT, "ended": "drop"}]))
    assert (emits["subscription_current"].extension or {})[SNAPSHOTS_KEY]["ended"] == "drop"


def test_a_snapshot_emit_needs_no_rows(tmp_path: Path) -> None:
    emits = resolved(bundle(tmp_path, "norows", snapshots=[SNAPSHOT]))
    assert "rows" not in (emits["subscription_current"].extension or {})


def test_the_reader_returns_the_entries_sorted_by_emit() -> None:
    block = [
        {"emit": "subscription_current", "of": "subscription_version"},
        {"emit": "customer", "of": "subscription_version"},
    ]
    entries = read_snapshots_block(
        block, emits=frozenset(EMITTED), where="here", code=FLX_3105, refusal=FLX_3105
    )
    assert [entry.emit for entry in entries] == ["customer", "subscription_current"]
    assert bound_into_h(entries, "customer") == {"of": "subscription_version", "ended": "keep"}
    assert bound_into_h(entries, "subscription_version") is None


def test_an_empty_block_or_a_null_declares_nothing(tmp_path: Path) -> None:
    for name, block in (("empty", []), ("null", None)):
        emits = resolved(bundle(tmp_path, name, snapshots=block))
        assert SNAPSHOTS_KEY not in (emits["subscription_current"].extension or {})


# --- snapshots: refusals -------------------------------------------------------------------


def test_an_unknown_key_is_flx_3105(tmp_path: Path) -> None:
    text = refused(bundle(tmp_path, "unknown", snapshots=[{**SNAPSHOT, "mode": "x"}]), "FLX-3105")
    assert "snapshots[0]" in text and "mode" in text


def test_a_block_that_is_not_a_list_is_flx_3105(tmp_path: Path) -> None:
    refused(bundle(tmp_path, "scalar", snapshots={"emit": "customer"}), "FLX-3105")


@pytest.mark.parametrize(
    ("name", "entry", "needle"),
    [
        ("ended", {**SNAPSHOT, "ended": "gone"}, "ended"),
        ("noemit", {"emit": "ghost", "of": "subscription_version"}, "does not emit"),
        ("noof", {"emit": "subscription_current", "of": "ghost"}, ".of"),
        ("self", {"emit": "subscription_current", "of": "subscription_current"}, "itself"),
        ("nohistory", {"emit": "subscription_current", "of": "customer"}, "no history"),
        ("empty", {"emit": "subscription_current", "of": ""}, ".of"),
    ],
)
def test_a_refused_entry_is_flx_3403_naming_the_entry_and_the_key(
    tmp_path: Path, name: str, entry: Dict[str, Any], needle: str
) -> None:
    text = refused(bundle(tmp_path, name, snapshots=[entry]), "FLX-3403")
    assert needle in text


def test_two_entries_for_one_emit_are_refused(tmp_path: Path) -> None:
    text = refused(bundle(tmp_path, "twice", snapshots=[SNAPSHOT, SNAPSHOT]), "FLX-3403")
    assert "second entry" in text


def test_a_declared_rows_on_a_projected_emit_is_refused_naming_the_entry(tmp_path: Path) -> None:
    text = refused(bundle(tmp_path, "rows", snapshots=[SNAPSHOT], projected_rows=5), "FLX-3403")
    assert "snapshots (subscription_current)" in text and "rows" in text


def test_a_column_the_history_lacks_is_refused(tmp_path: Path) -> None:
    extra = _current()
    extra["contract"]["schema"].append({"name": "nickname", "type": "STRING", "required": False})
    text = refused(bundle(tmp_path, "lacks", snapshots=[SNAPSHOT], current=extra), "FLX-3403")
    assert "nickname" in text


def test_a_column_of_another_type_is_refused(tmp_path: Path) -> None:
    other = _current()
    other["contract"]["schema"][3] = {"name": "status", "type": "INT", "required": True}
    text = refused(bundle(tmp_path, "typed", snapshots=[SNAPSHOT], current=other), "FLX-3403")
    assert "status" in text and "type" in text


def _held(**changes: Any) -> Dict[str, HistoryEntry]:
    entries = read_history_block(
        [entry_with(**changes)],
        emits=frozenset({"subscription_version"}),
        where="here",
        code=FLX_3105,
        refusal=FLX_3105,
    )
    return {entry.emit: entry for entry in entries}


def _checked(entry: SnapshotEntry, histories: Any, counts: Any = (), extension: Any = None) -> Any:
    return check_projected_emit(
        SNAPSHOTS_KEY,
        entry,
        histories=histories,
        counts=counts,
        extension=extension,
        refusal=FLX_3105,
    )


def test_a_projected_emit_with_a_history_a_counts_entry_or_a_reference_is_refused() -> None:
    entry = SnapshotEntry("subscription_current", "subscription_version")
    held = _held()["subscription_version"]
    assert _checked(entry, [held]) is held
    own = HistoryEntry(**{**held.__dict__, "emit": "subscription_current"})
    with pytest.raises(FluxError, match="a history of its own"):
        _checked(entry, [held, own])
    counted = CountsEntry(emit="subscription_current", relationship="customer")
    with pytest.raises(FluxError, match="a counts entry"):
        _checked(entry, [held], [counted])
    with pytest.raises(FluxError, match="a reference block"):
        _checked(entry, [held], extension={"reference": True})


# --- H, G and the plan ---------------------------------------------------------------------


def test_an_entry_moves_h_of_its_emit_and_neither_g_nor_another_emit(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    moved = digests(bundle(tmp_path, "snap", snapshots=[SNAPSHOT]))
    assert moved["subscription_current"][0] != plain["subscription_current"][0]
    assert moved["subscription_current"][1] == plain["subscription_current"][1]
    assert moved["subscription_version"] == plain["subscription_version"]
    assert moved["customer"] == plain["customer"]


def test_the_member_is_bound_only_to_the_emit_that_has_an_entry(tmp_path: Path) -> None:
    emits = resolved(bundle(tmp_path, "bound", snapshots=[SNAPSHOT]))
    assert PROJECTS_KEY in (emits["subscription_current"].extension or {})
    for other in ("customer", "subscription_version"):
        assert PROJECTS_KEY not in (emits[other].extension or {})
        assert SNAPSHOTS_KEY not in (emits[other].extension or {})


def test_the_projects_member_holds_the_history_emit_and_its_entry_and_no_plan(
    tmp_path: Path,
) -> None:
    emits = resolved(bundle(tmp_path, "projects", snapshots=[SNAPSHOT]))
    extension = emits["subscription_current"].extension or {}
    projects = extension[PROJECTS_KEY]
    assert set(projects) == {"emit", "entry"}
    assert projects["emit"] == "subscription_version"
    assert projects["entry"] == emits["subscription_version"].extension
    assert "history" in projects["entry"]
    text = json.dumps(extension, default=str)
    for plan_word in ("rowsFrom", "planDigest", "generatorDigest", "timeBounds"):
        assert plan_word not in text


def test_a_changed_history_moves_the_projection_and_a_changed_entry_moves_it_again(
    tmp_path: Path,
) -> None:
    first = digests(bundle(tmp_path, "one", snapshots=[SNAPSHOT]))
    second = digests(
        bundle(tmp_path, "two", snapshots=[SNAPSHOT], history=[entry_with(entities=101)])
    )
    third = digests(bundle(tmp_path, "three", snapshots=[{**SNAPSHOT, "ended": "drop"}]))
    assert first["subscription_current"][0] != second["subscription_current"][0]
    assert first["subscription_current"][0] != third["subscription_current"][0]
    assert first["subscription_current"][1] == second["subscription_current"][1]
    assert first["subscription_current"][1] == third["subscription_current"][1]


def test_a_bundle_without_the_key_keeps_every_digest(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    assert digests(bundle(tmp_path, "again", snapshots=None)) == plain
    assert digests(bundle(tmp_path, "blank", snapshots=[])) == plain


def test_kernel_0_1_0_refuses_the_key_and_names_the_kernel(tmp_path: Path) -> None:
    extension = resolved(bundle(tmp_path, "k1", snapshots=[SNAPSHOT]))[
        "subscription_current"
    ].extension
    with pytest.raises(FluxError) as caught:
        refuse_projection(extension, counter_based=False, where="sim", target="x")
    assert caught.value.code is FLX_3105
    assert "0.2.0" in str(caught.value) and "0.1.0" in str(caught.value)
    assert "snapshots" in str(caught.value)


def test_a_declared_snapshot_is_generated_so_the_plan_does_not_refuse_it(tmp_path: Path) -> None:
    extension = resolved(bundle(tmp_path, "k2", snapshots=[SNAPSHOT]))[
        "subscription_current"
    ].extension
    refuse_projection(extension, counter_based=True, where="sim", target="x")


def test_an_emit_with_no_entry_is_not_refused() -> None:
    refuse_projection(None, counter_based=False, where="sim", target="x")
    refuse_projection({"rows": 3}, counter_based=True, where="sim", target="x")


def test_the_entry_is_a_deep_copy_of_its_history_entry(tmp_path: Path) -> None:
    emits = resolved(bundle(tmp_path, "copy", snapshots=[SNAPSHOT]))
    projects = (emits["subscription_current"].extension or {})[PROJECTS_KEY]
    projects["entry"]["history"]["entities"] = 1
    assert (emits["subscription_version"].extension or {})["history"]["entities"] == 100
    assert copy.deepcopy(projects) == projects


# --- events: reads -------------------------------------------------------------------------


def event_with(**changes: Any) -> Dict[str, Any]:
    """The valid events entry with *changes* applied; a value of ``None`` removes the key."""
    entry = copy.deepcopy(EVENT)
    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return entry


def events_member(tmp_path: Path, name: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    emits = resolved(bundle(tmp_path, name, events=[entry]))
    return (emits["subscription_change"].extension or {})[EVENTS_KEY]


def test_a_valid_entry_reads_and_the_member_holds_it(tmp_path: Path) -> None:
    member = events_member(tmp_path, "valid", EVENT)
    expected = {key: value for key, value in EVENT.items() if key != "emit"}
    assert member == expected


def test_the_smallest_entry_reads_with_all_three_ops(tmp_path: Path) -> None:
    member = events_member(
        tmp_path, "small", {"emit": "subscription_change", "of": EVENT["of"], "date": "recorded_at"}
    )
    assert member == {"of": "subscription_version", "date": "recorded_at", "on": ["c", "u", "d"]}


def test_a_timestamp_date_with_a_timestamp_at_reads(tmp_path: Path) -> None:
    entry = event_with(date="recorded_ts", at="changed_ts")
    assert events_member(tmp_path, "stamps", entry)["at"] == "changed_ts"


def test_a_kept_subset_needs_only_its_texts(tmp_path: Path) -> None:
    values = {"plan": "plan_change", "end": "cancel"}
    member = events_member(
        tmp_path, "updates", event_with(on=["u"], type={"column": "kind", "values": values})
    )
    assert member["on"] == ["u"]
    assert member["type"]["values"] == values


def test_on_is_written_in_the_order_c_u_d(tmp_path: Path) -> None:
    member = events_member(tmp_path, "order", event_with(on=["d", "c"], before=None))
    assert member["on"] == ["c", "d"]


def test_a_required_before_column_is_read_when_c_is_not_kept(tmp_path: Path) -> None:
    entry = event_with(
        on=["u", "d"],
        before={"required_before": "plan"},
        type={"column": "kind", "values": {"d": "delete", "end": "cancel", "plan": "x"}},
    )
    assert events_member(tmp_path, "nocreate", entry)["before"] == {"required_before": "plan"}


def test_a_when_over_the_version_and_its_previous_reads(tmp_path: Path) -> None:
    text = "plan <> previous.plan AND valid_from IS NOT NULL AND started_on IS NOT NULL"
    assert events_member(tmp_path, "when", event_with(when=text))["when"] == text


def test_the_reader_alone_returns_the_entries_sorted() -> None:
    entries = read_events_block(
        [EVENT], emits=frozenset(EMITTED), where="here", code=FLX_3105, refusal=FLX_3105
    )
    assert entries[0].on == ("c", "u", "d")
    assert entries[0].lag is not None and entries[0].lag.edges == ("0", "3", "7")
    assert bound_into_h(entries, "subscription_change") == entries[0].as_member()


# --- events: refusals ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        event_with(mode="x"),
        event_with(lag={"bins": ["0", "3"], "binWeights": ["1"], "mean": "2"}),
        event_with(lag={"bins": ["0", "3"], "binWeights": ["1"], "open": "0.1"}),
        event_with(type={"column": "kind", "values": {}, "other": 1}),
    ],
)
def test_an_unknown_key_at_any_level_is_flx_3105(tmp_path: Path, entry: Dict[str, Any]) -> None:
    refused(bundle(tmp_path, "unknown", events=[entry]), "FLX-3105")


@pytest.mark.parametrize(
    ("name", "entry", "needle"),
    [
        ("nodate", event_with(date=None), "date is required"),
        ("datecolumn", event_with(date="ghost"), "date"),
        ("atcolumn", event_with(at="ghost"), "at"),
        ("datetype", event_with(date="op", at=None, lag=None), "DATE or TIMESTAMP"),
        ("onetype", event_with(date="recorded_ts"), "one type"),
        ("lagshape", event_with(lag=["1"]), "lag"),
        ("lagfraction", event_with(lag={"bins": ["0", "1.5"], "binWeights": ["1"]}), "whole"),
        ("lagnegative", event_with(lag={"bins": ["-1", "3"], "binWeights": ["1"]}), "whole"),
        ("lagdescends", event_with(lag={"bins": ["3", "1"], "binWeights": ["1"]}), "ascend"),
        ("lagweights", event_with(lag={"bins": ["0", "3"], "binWeights": ["1", "1"]}), "weights"),
        ("onname", event_with(on=["c", "x"]), ".on"),
        ("onempty", event_with(on=[]), ".on"),
        ("ontwice", event_with(on=["c", "c"]), ".on"),
        ("onscalar", event_with(on="c"), ".on"),
        ("opcolumn", event_with(op="ghost"), "op"),
        ("optype", event_with(op="n"), "text column"),
        ("nokind", event_with(type={"column": "kind"}), "values"),
        ("typecolumn", event_with(type={"column": "ghost", "values": {"c": "x"}}), "type.column"),
        ("typetext", event_with(type={"column": "n", "values": {"c": "x"}}), "text column"),
        ("typeempty", event_with(type={"column": "kind", "values": {}}), "values"),
        ("typenotext", event_with(type={"column": "kind", "values": {"c": 1}}), "values"),
        ("beforeshape", event_with(before=["x"]), "before"),
        ("beforecolumn", event_with(before={"ghost": "plan"}), "before"),
        ("beforehistory", event_with(before={"before_plan": "ghost"}), "before"),
        ("aftercolumn", event_with(after={"ghost": "plan"}), "after"),
        ("afterhistory", event_with(after={"after_plan": "ghost"}), "after"),
        ("beforerequired", event_with(before={"required_before": "plan"}), "keep 'c'"),
        ("afterrequired", event_with(after={"required_after": "plan"}), "keep 'd'"),
    ],
)
def test_a_refused_events_entry_is_flx_3403_naming_the_entry_and_the_key(
    tmp_path: Path, name: str, entry: Dict[str, Any], needle: str
) -> None:
    text = refused(bundle(tmp_path, name, events=[entry]), "FLX-3403")
    assert needle in text
    assert "events" in text


@pytest.mark.parametrize("missing", ["c", "d", "end", "plan"])
def test_every_kept_op_reason_and_the_end_need_a_text(tmp_path: Path, missing: str) -> None:
    values = {key: value for key, value in EVENT["type"]["values"].items() if key != missing}
    entry = event_with(type={"column": "kind", "values": values})
    text = refused(bundle(tmp_path, f"missing-{missing}", events=[entry]), "FLX-3403")
    assert f"no text for {missing!r}" in text


def test_a_text_for_a_dropped_op_is_not_needed(tmp_path: Path) -> None:
    values = {"c": "create", "end": "cancel", "plan": "plan_change"}
    entry = event_with(on=["c", "u"], type={"column": "kind", "values": values})
    assert events_member(tmp_path, "nodelete", entry)["on"] == ["c", "u"]


def test_a_text_for_a_column_that_is_not_tracked_is_refused(tmp_path: Path) -> None:
    values = {**EVENT["type"]["values"], "tier": "tier_change"}
    entry = event_with(type={"column": "kind", "values": values})
    assert "'tier'" in refused(bundle(tmp_path, "stray", events=[entry]), "FLX-3403")


@pytest.mark.parametrize(
    "text",
    [
        "tier = 'gold'",
        "status = 'active'",
        "customer.customer_id = 1",
        "plan <> 'basic' AND seats > 1",
        "previous.tier = 'gold'",
    ],
)
def test_a_when_that_reads_anything_but_the_versions_columns_is_refused(
    tmp_path: Path, text: str
) -> None:
    found = refused(bundle(tmp_path, "when", events=[event_with(when=text)]), "FLX-3403")
    assert "events (subscription_change).when" in found


def test_a_when_that_does_not_parse_is_refused_as_any_rule_is(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        resolved(bundle(tmp_path, "parse", events=[event_with(when="plan <>")]))
    assert caught.value.code.code.startswith("FLX-61")


def test_a_snapshot_and_an_events_entry_for_one_emit_are_refused(tmp_path: Path) -> None:
    both = bundle(
        tmp_path,
        "both",
        snapshots=[{"emit": "subscription_change", "of": "subscription_version"}],
        events=[EVENT],
    )
    assert "second projection entry" in refused(both, "FLX-3403")


def test_two_projections_of_different_emits_read_together(tmp_path: Path) -> None:
    emits = resolved(bundle(tmp_path, "both-ok", snapshots=[SNAPSHOT], events=[EVENT]))
    assert SNAPSHOTS_KEY in (emits["subscription_current"].extension or {})
    assert EVENTS_KEY in (emits["subscription_change"].extension or {})
    assert EVENTS_KEY not in (emits["subscription_current"].extension or {})


def test_a_declared_rows_on_an_events_emit_is_refused(tmp_path: Path) -> None:
    path = bundle(tmp_path, "rows", events=[EVENT], namespace=None)
    simulation = yaml.safe_load((path / "simulation.flux.yml").read_text("utf-8"))
    simulation["extensions"]["com.agentics.flux/1"]["emits"]["subscription_change"] = {"rows": 9}
    (path / "simulation.flux.yml").write_text(yaml.safe_dump(simulation, sort_keys=False), "utf-8")
    assert "events (subscription_change)" in refused(path, "FLX-3403")


# --- events: H, G and the plan -------------------------------------------------------------


def test_an_events_entry_moves_h_of_its_emit_and_neither_g_nor_another_emit(
    tmp_path: Path,
) -> None:
    before = digests(bundle(tmp_path, "plain"))
    moved = digests(bundle(tmp_path, "events", events=[EVENT]))
    assert moved["subscription_change"][0] != before["subscription_change"][0]
    assert moved["subscription_change"][1] == before["subscription_change"][1]
    for other in ("customer", "subscription_version", "subscription_current"):
        assert moved[other] == before[other]


def test_an_edit_of_the_entry_or_the_history_moves_h_again(tmp_path: Path) -> None:
    first = digests(bundle(tmp_path, "one", events=[EVENT]))["subscription_change"]
    lag = {"bins": ["0", "4"], "binWeights": ["1"]}
    second = digests(bundle(tmp_path, "two", events=[event_with(lag=lag)]))["subscription_change"]
    third = digests(bundle(tmp_path, "three", events=[EVENT], history=[entry_with(entities=101)]))[
        "subscription_change"
    ]
    assert len({first[0], second[0], third[0]}) == 3
    assert first[1] == second[1] == third[1]


def test_the_events_member_holds_the_history_and_no_plan(tmp_path: Path) -> None:
    emits = resolved(bundle(tmp_path, "projects", events=[EVENT]))
    extension = emits["subscription_change"].extension or {}
    assert set(extension) == {EVENTS_KEY, PROJECTS_KEY}
    assert extension[PROJECTS_KEY]["entry"] == emits["subscription_version"].extension
    text = json.dumps(extension, default=str)
    for plan_word in ("rowsFrom", "planDigest", "generatorDigest"):
        assert plan_word not in text


def test_kernel_0_1_0_refuses_the_events_key_and_names_the_kernel(tmp_path: Path) -> None:
    extension = resolved(bundle(tmp_path, "k1", events=[EVENT]))["subscription_change"].extension
    with pytest.raises(FluxError) as caught:
        refuse_projection(extension, counter_based=False, where="sim", target="subscription_change")
    assert caught.value.code is FLX_3105
    assert "events" in str(caught.value) and "kernel 0.1.0" in str(caught.value)


def test_a_declared_events_entry_is_generated_on_kernel_0_2_0(tmp_path: Path) -> None:
    extension = resolved(bundle(tmp_path, "k2", events=[EVENT]))["subscription_change"].extension
    refuse_projection(extension, counter_based=True, where="sim", target="subscription_change")


# --- periods: reads ------------------------------------------------------------------------


def period_with(**changes: Any) -> Dict[str, Any]:
    """The valid periods entry with *changes* applied; a value of ``None`` removes the key."""
    entry = copy.deepcopy(PERIOD)
    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return entry


def periods_member(tmp_path: Path, name: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    emits = resolved(bundle(tmp_path, name, periods=[entry]))
    return (emits["subscription_month"].extension or {})[PERIODS_KEY]


def test_a_valid_entry_reads_and_the_member_holds_it_with_every_default_written(
    tmp_path: Path,
) -> None:
    member = periods_member(tmp_path, "valid", PERIOD)
    assert member == {key: value for key, value in PERIOD.items() if key != "emit"}


def test_the_smallest_entry_takes_its_grain_from_the_columns_time_granularity(
    tmp_path: Path,
) -> None:
    entry = {"emit": "subscription_month", "of": "subscription_version", "column": "month"}
    assert periods_member(tmp_path, "small", entry) == {
        "of": "subscription_version",
        "column": "month",
        "grain": "month",
        "match": "overlap",
    }


def test_a_declared_grain_and_the_contracts_default_bind_alike(tmp_path: Path) -> None:
    plain = {"emit": "subscription_month", "of": "subscription_version", "column": "month"}
    declared = digests(bundle(tmp_path, "declared", periods=[{**plain, "grain": "month"}]))
    implied = digests(bundle(tmp_path, "implied", periods=[plain]))
    assert declared == implied


@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
@pytest.mark.parametrize("match", ["overlap", "start", "end"])
def test_every_grain_and_match_reads(tmp_path: Path, grain: str, match: str) -> None:
    member = periods_member(tmp_path, f"{grain}-{match}", period_with(grain=grain, match=match))
    assert (member["grain"], member["match"]) == (grain, match)


def test_a_grain_with_no_time_granularity_to_default_to_reads_when_declared(
    tmp_path: Path,
) -> None:
    member = periods_member(
        tmp_path, "stamp", period_with(column="stamp_day", grain="week", kinds=None)
    )
    assert member["grain"] == "week"


def test_a_count_of_zero_and_a_levels_count_read(tmp_path: Path) -> None:
    kinds = [{"count": 0}, {"count": {"levels": ["0", "2"], "levelWeights": ["1", "3"]}}]
    assert periods_member(tmp_path, "counts", period_with(kinds=kinds))["kinds"] == kinds


def test_a_kind_with_no_count_holds_one_row_per_pair(tmp_path: Path) -> None:
    member = periods_member(tmp_path, "onerow", period_with(kinds=[{"overrides": {"label": "x"}}]))
    assert member["kinds"] == [{"count": 1, "overrides": {"label": "x"}}]


def test_the_reader_alone_returns_the_entries() -> None:
    entries = read_periods_block(
        [PERIOD], emits=frozenset(EMITTED), where="here", code=FLX_3105, refusal=FLX_3105
    )
    assert entries[0].match == "overlap" and len(entries[0].kinds) == 2
    assert bound_into_h(entries, "subscription_month") == entries[0].as_member()


# --- periods: refusals ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        period_with(mode="x"),
        period_with(kinds=[{"count": 1, "rows": 2}]),
        period_with(kinds=[{"count": {"levels": ["1"], "levelWeights": ["1"], "x": 1}}]),
    ],
)
def test_an_unknown_periods_key_at_any_level_is_flx_3105(
    tmp_path: Path, entry: Dict[str, Any]
) -> None:
    refused(bundle(tmp_path, "unknown", periods=[entry]), "FLX-3105")


@pytest.mark.parametrize(
    ("name", "entry", "needle"),
    [
        ("nocolumn", period_with(column=None), "column"),
        ("column", period_with(column="ghost"), "column"),
        ("notdate", period_with(column="label"), "not a DATE"),
        ("history", period_with(column="valid_from"), "also a column"),
        ("grain", period_with(grain="hour"), "grain"),
        ("nograin", period_with(column="stamp_day", grain=None), "timeGranularity"),
        ("match", period_with(match="close"), "match"),
        ("kinds", period_with(kinds=[]), "kinds"),
        ("kindsscalar", period_with(kinds={"count": 1}), "kinds"),
        ("kind", period_with(kinds=[3]), "kinds[0]"),
        ("negative", period_with(kinds=[{"count": -1}]), "count"),
        ("fraction", period_with(kinds=[{"count": "1.5"}]), "count"),
        ("bool", period_with(kinds=[{"count": True}]), "count"),
        (
            "levels",
            period_with(kinds=[{"count": {"levels": ["a"], "levelWeights": ["1"]}}]),
            "whole",
        ),
        (
            "weights",
            period_with(kinds=[{"count": {"levels": ["1", "2"], "levelWeights": ["1"]}}]),
            "weights",
        ),
        (
            "bins",
            period_with(kinds=[{"count": {"bins": ["1", "2"], "binWeights": ["1"]}}]),
            "count",
        ),
        ("overcolumn", period_with(kinds=[{"overrides": {"ghost": "x"}}]), "overrides"),
        (
            "overperiod",
            period_with(kinds=[{"overrides": {"month": "2026-07-01"}}]),
            "period column",
        ),
        ("overint", period_with(kinds=[{"overrides": {"quota": "x"}}]), "quota"),
        ("overscale", period_with(kinds=[{"overrides": {"price": "9.555"}}]), "price"),
        ("overbool", period_with(kinds=[{"overrides": {"active": "yes"}}]), "active"),
        ("overstamp", period_with(kinds=[{"overrides": {"seen": "2026-07-01"}}]), "seen"),
        ("overday", period_with(kinds=[{"overrides": {"stamp_day": "2026/07/01"}}]), "stamp_day"),
        ("overempty", period_with(kinds=[{"overrides": {}}]), "overrides"),
        ("whenlabel", period_with(kinds=[{"when": "label = 'x'"}]), "label"),
        ("whenprev", period_with(kinds=[{"when": "previous.month IS NULL"}]), "month"),
        ("whentier", period_with(kinds=[{"when": "tier = 'gold'"}]), "tier"),
        ("whenlink", period_with(kinds=[{"when": "customer.customer_id = 1"}]), "customer_id"),
    ],
)
def test_a_refused_periods_entry_is_flx_3403_naming_the_entry_and_the_key(
    tmp_path: Path, name: str, entry: Dict[str, Any], needle: str
) -> None:
    text = refused(bundle(tmp_path, name, periods=[entry]), "FLX-3403")
    assert needle in text
    assert "periods" in text


def test_a_kind_when_that_does_not_parse_is_refused_as_any_rule_is(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        resolved(bundle(tmp_path, "parse", periods=[period_with(kinds=[{"when": "plan <>"}])]))
    assert caught.value.code.code.startswith("FLX-61")


def test_a_relationship_whose_parent_is_a_periodic_emit_is_refused(tmp_path: Path) -> None:
    text = refused(bundle(tmp_path, "parent", periods=[PERIOD], ledger=True), "FLX-3403")
    assert "month_ledger" in text and "subscription_month" in text and "periodic" in text


def test_the_same_relationship_is_read_when_no_periods_entry_names_its_parent(
    tmp_path: Path,
) -> None:
    emits = resolved(bundle(tmp_path, "unparented", snapshots=[SNAPSHOT], ledger=True))
    assert "month_ledger" in emits
    emits = resolved(bundle(tmp_path, "unparented-events", events=[EVENT], ledger=True))
    assert "month_ledger" in emits


def test_a_declared_rows_on_a_periodic_emit_is_refused(tmp_path: Path) -> None:
    path = bundle(tmp_path, "rows", periods=[PERIOD])
    simulation = yaml.safe_load((path / "simulation.flux.yml").read_text("utf-8"))
    simulation["extensions"]["com.agentics.flux/1"]["emits"]["subscription_month"] = {"rows": 9}
    (path / "simulation.flux.yml").write_text(yaml.safe_dump(simulation, sort_keys=False), "utf-8")
    assert "periods (subscription_month)" in refused(path, "FLX-3403")


def test_a_snapshot_and_a_periods_entry_for_one_emit_are_refused(tmp_path: Path) -> None:
    both = bundle(
        tmp_path,
        "both",
        snapshots=[{"emit": "subscription_month", "of": "subscription_version"}],
        periods=[PERIOD],
    )
    assert "second projection entry" in refused(both, "FLX-3403")


def test_a_periods_entry_may_not_name_an_of_with_no_history(tmp_path: Path) -> None:
    text = refused(bundle(tmp_path, "nohistory", periods=[period_with(of="customer")]), "FLX-3403")
    assert "no history" in text or "declares no history" in text


# --- periods: H, G and the plan ------------------------------------------------------------


def test_a_periods_entry_moves_h_of_its_emit_and_neither_g_nor_another_emit(
    tmp_path: Path,
) -> None:
    before = digests(bundle(tmp_path, "plain"))
    moved = digests(bundle(tmp_path, "periods", periods=[PERIOD]))
    assert moved["subscription_month"][0] != before["subscription_month"][0]
    assert moved["subscription_month"][1] == before["subscription_month"][1]
    for other in ("customer", "subscription_version", "subscription_current"):
        assert moved[other] == before[other]


def test_an_edit_of_the_periods_entry_or_the_history_moves_h_again(tmp_path: Path) -> None:
    first = digests(bundle(tmp_path, "one", periods=[PERIOD]))["subscription_month"]
    second = digests(bundle(tmp_path, "two", periods=[period_with(match="end")]))[
        "subscription_month"
    ]
    third = digests(
        bundle(tmp_path, "three", periods=[PERIOD], history=[entry_with(entities=101)])
    )["subscription_month"]
    assert len({first[0], second[0], third[0]}) == 3
    assert first[1] == second[1] == third[1]


def test_the_three_keys_bind_together_without_touching_each_others_emits(
    tmp_path: Path,
) -> None:
    emits = resolved(
        bundle(tmp_path, "all", snapshots=[SNAPSHOT], events=[EVENT], periods=[PERIOD])
    )
    for expose_id, key in (
        ("subscription_current", SNAPSHOTS_KEY),
        ("subscription_change", EVENTS_KEY),
        ("subscription_month", PERIODS_KEY),
    ):
        extension = emits[expose_id].extension or {}
        assert set(extension) == {key, PROJECTS_KEY}
        assert extension[PROJECTS_KEY]["emit"] == "subscription_version"
    assert set(emits["subscription_version"].extension or {}) == {
        "history",
        "fidelity",
        "rows",
    }


def test_the_periods_member_holds_no_plan(tmp_path: Path) -> None:
    extension = resolved(bundle(tmp_path, "plan", periods=[PERIOD]))["subscription_month"].extension
    text = json.dumps(extension, default=str)
    for plan_word in ("rowsFrom", "planDigest", "generatorDigest"):
        assert plan_word not in text


def test_kernel_0_1_0_refuses_the_periods_key_and_names_the_kernel(tmp_path: Path) -> None:
    extension = resolved(bundle(tmp_path, "k1", periods=[PERIOD]))["subscription_month"].extension
    with pytest.raises(FluxError) as caught:
        refuse_projection(extension, counter_based=False, where="sim", target="subscription_month")
    assert caught.value.code is FLX_3105
    assert "periods" in str(caught.value) and "kernel 0.1.0" in str(caught.value)


def test_a_declared_periods_entry_is_generated_on_kernel_0_2_0(tmp_path: Path) -> None:
    extension = resolved(bundle(tmp_path, "k2", periods=[PERIOD]))["subscription_month"].extension
    refuse_projection(extension, counter_based=True, where="sim", target="subscription_month")
