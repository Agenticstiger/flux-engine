# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation's history entry (ADR 0017 sections 1 and 2): read, checked, bound into ``H``.

The bundle is written here: a ``customer`` expose and a ``subscription_version`` expose whose
contract declares a validity window (a natural entity, a start and an end). A bundle that declares
no entry hashes as it always did; an entry moves ``H`` of its emit and neither ``G`` nor any other
emit's ``H``. Until the generator lands, ``plan`` reads a history and refuses to generate it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import pytest
import yaml

from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.seam.emit import seam_digests
from flux_engine.seam.history import (
    HISTORY_KEY,
    HistoryEntry,
    bound_into_h,
    history_entries_of,
    read_history_block,
)
from flux_engine.seam.resolve import (
    FLX_3105,
    load_bundle,
    load_documents_exact,
    resolve_all_emits,
)
from flux_engine.stages import pipeline
from tests.stages.test_counts_run import FIXTURE as EXACT_COUNTS

pytestmark = pytest.mark.unit

SIMULATION = "sim-subscriptions"
PRODUCT = "retail.subscriptions"
SIX = "sha256:" + "0" * 64

ENTRY: Dict[str, Any] = {
    "emit": "subscription_version",
    "entities": 100,
    "parent": {
        "relationship": "customer",
        "minChildren": 1,
        "children": {"levels": ["1", "2"], "levelWeights": ["0.5", "0.5"]},
    },
    "versions": {"bins": ["1", "2", "4"], "binWeights": ["0.6", "0.4"]},
    "start": {"bins": ["2026-07-01", "2026-08-01"], "binWeights": ["1"]},
    "length": {"bins": ["30", "90"], "binWeights": ["1"], "closed": "lower"},
    "until": "2026-09-30",
    "chain": "nextStart",
    "activation": "started_on",
    "tracked": [
        {
            "column": "plan",
            "transitions": {
                "basic": {"pro": "0.8", "team": "0.2"},
                "pro": {"basic": "1"},
                "team": {"pro": "1"},
            },
        }
    ],
    "end": {"mode": "finalVersion", "share": "0.1", "column": "status", "value": "cancelled"},
}


def _schema(stamp: str, *, end_required: bool, end_type: Optional[str]) -> List[Dict[str, Any]]:
    return [
        {"name": "version_id", "type": "INT", "required": True},
        {"name": "subscription_id", "type": "INT", "required": True},
        {"name": "customer_id", "type": "INT", "required": True},
        {"name": "plan", "type": "STRING", "required": True},
        {"name": "tier", "type": "STRING", "required": True},
        {"name": "status", "type": "STRING", "required": True},
        {"name": "region", "type": "STRING"},
        {"name": "seats", "type": "INT"},
        {"name": "started_on", "type": stamp, "required": True},
        {"name": "valid_from", "type": stamp, "required": True},
        {"name": "valid_to", "type": end_type or stamp, "required": end_required},
    ]


def contract(
    *,
    stamp: str = "DATE",
    window: bool = True,
    end_required: bool = False,
    end_type: Optional[str] = None,
    primary: str = "version_id",
) -> Dict[str, Any]:
    """The subscriptions contract: a customer, and its subscription versions with a validity window."""
    versioned: Dict[str, Any] = {
        "entities": [
            {"name": "version", "type": "primary", "expr": primary},
            {"name": "subscription", "type": "natural", "expr": "subscription_id"},
        ],
        "relationships": [
            {
                "name": "customer",
                "to": "customer",
                "fromColumns": ["customer_id"],
                "toColumns": ["customer_id"],
                "cardinality": "many_to_one",
                "joinType": "inner",
            }
        ],
    }
    if window:
        versioned["dimensions"] = [
            {
                "name": "valid_from",
                "type": "time",
                "typeParams": {"validityParams": {"isStart": True}},
            },
            {"name": "valid_to", "type": "time", "typeParams": {"validityParams": {"isEnd": True}}},
        ]
    binding = {"platform": "aws", "format": "glue_table", "location": {"database": "retail"}}
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": PRODUCT,
        "name": "Subscriptions",
        "metadata": {"owner": {"team": "sales"}},
        "exposes": [
            {
                "exposeId": "customer",
                "kind": "table",
                "binding": {**binding, "location": {"database": "retail", "table": "customer"}},
                "contract": {"schema": [{"name": "customer_id", "type": "INT", "required": True}]},
                "semantics": {
                    "entities": [{"name": "customer", "type": "primary", "expr": "customer_id"}]
                },
            },
            {
                "exposeId": "subscription_version",
                "kind": "table",
                "binding": {**binding, "location": {"database": "retail", "table": "sv"}},
                "contract": {
                    "schema": _schema(stamp, end_required=end_required, end_type=end_type)
                },
                "semantics": versioned,
            },
        ],
    }


def _fidelity(columns: Mapping[str, Any], target: str) -> Dict[str, Any]:
    return {
        "fidelity": {
            "encoding": {"fractionalBits": 16, "rounding": "floor"},
            "records": {"min": 1, "max": 1000000},
            "columns": dict(columns),
            "properties": [
                {
                    "property": "mean",
                    "column": target,
                    "tolerance": {"type": "absolute", "value": "1000"},
                }
            ],
            "budget": {"maxChecksPerState": 20, "maxStateAge": "P30D"},
            "failureAction": "withholdBundle",
        }
    }


def _emits(stamp: str) -> Dict[str, Any]:
    day = stamp == "DATE"
    low, high = (
        ("2026-07-01", "2026-09-30") if day else ("2026-07-01T00:00:00Z", "2026-09-30T00:00:00Z")
    )
    return {
        "customer": {
            "rows": 10,
            **_fidelity({"customer_id": {"range": {"min": "1", "max": "10"}}}, "customer_id"),
        },
        "subscription_version": {
            "rows": 300,
            **_fidelity(
                {
                    "version_id": {"range": {"min": "1", "max": "300"}},
                    "subscription_id": {"range": {"min": "1", "max": "100"}},
                    "plan": {"levels": {"basic": "0.5", "pro": "0.3", "team": "0.2"}},
                    "tier": {"levels": {"gold": "0.5", "silver": "0.5"}},
                    "status": {"levels": {"active": "1", "cancelled": "0"}},
                    "seats": {"range": {"min": "1", "max": "10"}},
                    "region": {"levels": {"north": "0.5", "south": "0.5"}, "nullFraction": "0.1"},
                    "started_on": {"range": {"min": low, "max": high}},
                    "valid_from": {"range": {"min": low, "max": high}},
                    "valid_to": {"range": {"min": low, "max": high}},
                },
                "version_id",
            ),
        },
    }


def bundle_with(
    tmp_path: Path,
    name: str,
    entries: Any = None,
    *,
    namespace: Optional[Mapping[str, Any]] = None,
    stamp: str = "DATE",
    columns: Optional[Mapping[str, Any]] = None,
    **contract_options: Any,
) -> Path:
    """A bundle of the two exposes, with *entries* as its ``history`` key and *namespace* beside it."""
    root = tmp_path / name
    root.mkdir()
    fluid = contract(stamp=stamp, **contract_options)
    (root / "subscriptions.fluid.yml").write_text(yaml.safe_dump(fluid, sort_keys=False), "utf-8")
    (document,) = load_documents_exact(root / "subscriptions.fluid.yml")
    digest = digest_of_document(document, subject=f"contractDigest:{PRODUCT}").value
    for fixture_name in ("world.flux.yml", "persona.flux.yml"):
        text = (EXACT_COUNTS / fixture_name).read_text("utf-8")
        (root / fixture_name).write_text(text, "utf-8")
    emits = _emits(stamp)
    emits["subscription_version"]["fidelity"]["columns"].update(columns or {})
    extensions: Dict[str, Any] = {"emits": emits, **(namespace or {})}
    if entries is not None:
        extensions[HISTORY_KEY] = entries
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
                for expose_id in ("customer", "subscription_version")
            ],
        },
        "extensions": {"com.agentics.flux/1": extensions},
    }
    (root / "simulation.flux.yml").write_text(yaml.safe_dump(simulation, sort_keys=False), "utf-8")
    return root


def resolved(bundle: Path) -> Dict[str, Any]:
    return {
        emit.expose_id: emit
        for emit in resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION)
    }


def _digests(bundle: Path) -> Dict[str, Tuple[str, str]]:
    found = {}
    for expose_id, emit in resolved(bundle).items():
        digests = seam_digests(
            emit.contract_document, emit.emit, emit.compile().column_names, emit.extension
        )
        found[expose_id] = (digests.h.value, digests.g.value)
    return found


def entry_with(**changes: Any) -> Dict[str, Any]:
    """The valid entry with *changes* applied; a value of ``None`` removes the key."""
    entry = copy.deepcopy(ENTRY)
    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return entry


def _stamped() -> Dict[str, Any]:
    """The valid entry for a TIMESTAMP window."""
    return entry_with(
        until="2026-09-30T00:00:00Z",
        start={"bins": ["2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z"], "binWeights": ["1"]},
    )


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Tuple[str, str]]:
    return _digests(bundle_with(tmp_path_factory.mktemp("plain"), "plain"))


# --- a valid entry reads -----------------------------------------------------------------------


def test_a_valid_entry_reads(tmp_path: Path) -> None:
    emit = resolved(bundle_with(tmp_path, "valid", [ENTRY]))["subscription_version"]
    (entry,) = history_entries_of(emit.extension, "subscription_version")
    assert isinstance(entry, HistoryEntry)
    assert (entry.emit, entry.entities, entry.until, entry.chain) == (
        "subscription_version",
        100,
        "2026-09-30",
        "nextStart",
    )
    assert entry.parent is not None and entry.parent.relationship == "customer"
    assert entry.parent.min_children == 1
    assert entry.versions is not None and entry.versions.kind == "bins"
    assert entry.start is not None and entry.start.edges == ("2026-07-01", "2026-08-01")
    assert entry.length.closed == "lower"
    assert entry.activation == "started_on"
    assert [tracked.column for tracked in entry.tracked] == ["plan"]
    assert entry.end is not None and (entry.end.mode, entry.end.value) == (
        "finalVersion",
        "cancelled",
    )


def test_the_smallest_entry_reads(tmp_path: Path) -> None:
    smallest = {
        "emit": "subscription_version",
        "entities": 5,
        "length": {"bins": ["1", "30"], "binWeights": ["1"]},
        "until": "2026-09-30",
    }
    emit = resolved(bundle_with(tmp_path, "small", [smallest]))["subscription_version"]
    (entry,) = history_entries_of(emit.extension, "subscription_version")
    assert entry.parent is None and entry.tracked == () and entry.chain == "nextStart"


def test_a_timestamp_window_reads_and_takes_dayBefore_never(tmp_path: Path) -> None:
    ok = bundle_with(tmp_path, "stamped", [_stamped()], stamp="TIMESTAMP")
    assert "subscription_version" in resolved(ok)
    entry = _stamped()
    entry["chain"] = "dayBefore"
    with pytest.raises(FluxError, match="dayBefore needs DATE") as caught:
        resolved(bundle_with(tmp_path, "daybefore", [entry], stamp="TIMESTAMP"))
    assert caught.value.code.code == "FLX-3403"


def test_an_open_end_that_reads_as_the_end_columns_type_reads(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, "open", [entry_with(openEnd="9999-12-31")], end_required=True)
    assert "subscription_version" in resolved(bundle)


@pytest.mark.parametrize(
    ("name", "changes"),
    [
        # Past the window, where no version ends: the reading dbt's dbt_valid_to_current gives.
        ("after", {"openEnd": "2026-10-01"}),
        # Under dayBefore a closed version ends the day before a start, so before until.
        ("daybefore", {"openEnd": "2026-09-30", "chain": "dayBefore"}),
    ],
)
def test_an_open_end_no_closed_version_can_hold_reads(
    tmp_path: Path, name: str, changes: Dict[str, Any]
) -> None:
    bundle = bundle_with(tmp_path, name, [entry_with(**changes)], end_required=True)
    assert "subscription_version" in resolved(bundle)


def test_the_member_round_trips_through_the_extension(tmp_path: Path) -> None:
    emit = resolved(bundle_with(tmp_path, "trip", [ENTRY]))["subscription_version"]
    (entry,) = history_entries_of(emit.extension, "subscription_version")
    assert bound_into_h((entry,), "subscription_version") == entry.as_member()
    assert emit.extension is not None and emit.extension[HISTORY_KEY] == entry.as_member()
    assert "emit" not in entry.as_member()


def test_an_untracked_share_reads_binds_into_h_and_moves_only_its_own_emit(tmp_path: Path) -> None:
    emit_id = str(ENTRY["emit"])
    held = entry_with(untracked="0.6")
    emit = resolved(bundle_with(tmp_path, "share", [held]))[emit_id]
    (entry,) = history_entries_of(emit.extension, emit_id)
    assert entry.untracked == "0.6"
    assert entry.as_member()["untracked"] == "0.6"
    assert emit.extension is not None and emit.extension[HISTORY_KEY]["untracked"] == "0.6"
    # The key is bound into the digest of the emit, which it moves.
    assert _digests(bundle_with(tmp_path, "with-share", [held])) != _digests(
        bundle_with(tmp_path, "plain", [ENTRY])
    )


def test_an_entry_without_the_key_binds_none_into_h(tmp_path: Path) -> None:
    emit_id = str(ENTRY["emit"])
    emit = resolved(bundle_with(tmp_path, "plain", [ENTRY]))[emit_id]
    (entry,) = history_entries_of(emit.extension, emit_id)
    assert entry.untracked is None
    assert "untracked" not in entry.as_member()


def test_an_untracked_share_beside_several_tracked_columns_and_reasons_reads(
    tmp_path: Path,
) -> None:
    emit_id = str(ENTRY["emit"])
    tracked = [
        ENTRY["tracked"][0],
        {"column": "tier", "transitions": {"gold": {"silver": "1"}, "silver": {"gold": "1"}}},
    ]
    held = entry_with(
        tracked=tracked,
        reasons={"levels": ["plan", "tier"], "levelWeights": ["0.5", "0.5"]},
        untracked="0.25",
    )
    emit = resolved(bundle_with(tmp_path, "several", [held]))[emit_id]
    (entry,) = history_entries_of(emit.extension, emit_id)
    assert entry.untracked == "0.25"


def test_an_empty_block_or_a_null_declares_nothing(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    assert _digests(bundle_with(tmp_path, "empty", [])) == plain
    assert _digests(bundle_with(tmp_path, "none", None, namespace={HISTORY_KEY: None})) == plain


# --- every refusal -----------------------------------------------------------------------------

Change = Callable[[], Dict[str, Any]]

UNKNOWN_KEYS: List[Tuple[str, Change]] = [
    ("an entry", lambda: entry_with(bogus=1)),
    ("the parent", lambda: entry_with(parent={"relationship": "customer", "bogus": 1})),
    ("versions", lambda: entry_with(versions={"levels": ["1"], "levelWeights": ["1"], "bogus": 1})),
    ("start", lambda: entry_with(start={**ENTRY["start"], "bogus": 1})),
    ("length", lambda: entry_with(length={**ENTRY["length"], "bogus": 1})),
    ("a tracked column", lambda: entry_with(tracked=[{**ENTRY["tracked"][0], "bogus": 1}])),
    ("end", lambda: entry_with(end={**ENTRY["end"], "bogus": 1})),
    (
        "reasons",
        lambda: entry_with(reasons={"levels": ["plan"], "levelWeights": ["1"], "bogus": 1}),
    ),
]


@pytest.mark.parametrize(("where", "change"), UNKNOWN_KEYS, ids=[name for name, _ in UNKNOWN_KEYS])
def test_an_unknown_key_at_any_level_is_flx_3105(
    tmp_path: Path, where: str, change: Change
) -> None:
    with pytest.raises(FluxError, match="bogus") as caught:
        resolved(bundle_with(tmp_path, "unknown", [change()]))
    assert caught.value.code is FLX_3105
    assert "history[0]" in str(caught.value)


@pytest.mark.parametrize("spelling", ["mean", "given", "states", "otherwise", "open"])
def test_a_length_spelled_another_way_is_flx_3105(tmp_path: Path, spelling: str) -> None:
    length = {**ENTRY["length"], spelling: "0.1"}
    with pytest.raises(FluxError, match=f"length.{spelling}") as caught:
        resolved(bundle_with(tmp_path, "spelled", [entry_with(length=length)]))
    assert caught.value.code is FLX_3105
    assert "history[0]" in str(caught.value)


def test_a_block_that_is_not_a_list_is_flx_3105(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        resolved(bundle_with(tmp_path, "mapping", dict(ENTRY)))
    assert caught.value.code is FLX_3105


REFUSED: List[Tuple[str, Change, str]] = [
    ("an unemitted emit", lambda: entry_with(emit="nothing"), "does not emit"),
    ("entities of zero", lambda: entry_with(entities=0), "entities is a whole number above zero"),
    ("entities negative", lambda: entry_with(entities=-3), "entities is a whole number above zero"),
    ("entities a fraction", lambda: entry_with(entities="1.5"), "entities is a whole number"),
    ("entities missing", lambda: entry_with(entities=None), "entities is a whole number"),
    (
        "a parent that is not a relationship",
        lambda: entry_with(parent={"relationship": "nope"}),
        "not a to-one relationship",
    ),
    (
        "a versions class of zero",
        lambda: entry_with(versions={"levels": ["0", "2"], "levelWeights": ["1", "1"]}),
        "below 1",
    ),
    (
        "a versions bin of zero",
        lambda: entry_with(versions={"bins": ["0", "3"], "binWeights": ["1"]}),
        "below 1",
    ),
    (
        "a versions shape with no weights",
        lambda: entry_with(versions={"levels": ["1", "2"]}),
        "versions",
    ),
    (
        "a versions class that is not a count",
        lambda: entry_with(versions={"levels": ["x"], "levelWeights": ["1"]}),
        "whole number",
    ),
    ("an until missing", lambda: entry_with(until=None), "until is required"),
    (
        "an until that does not parse",
        lambda: entry_with(until="tomorrow"),
        "until 'tomorrow' is not a DATE",
    ),
    ("an until after the window", lambda: entry_with(until="2026-10-01"), "after timeBounds.end"),
    (
        "an until at the window's start",
        lambda: entry_with(until="2026-07-01"),
        "not after timeBounds.start",
    ),
    (
        "a start bin before the window",
        lambda: entry_with(start={"bins": ["2026-06-01", "2026-08-01"], "binWeights": ["1"]}),
        "outside",
    ),
    (
        "a start bin after until",
        lambda: entry_with(
            until="2026-09-01", start={"bins": ["2026-07-01", "2026-09-15"], "binWeights": ["1"]}
        ),
        "outside",
    ),
    (
        "a length bin below a day",
        lambda: entry_with(length={"bins": ["0", "30"], "binWeights": ["1"]}),
        "below one day",
    ),
    ("a length missing", lambda: entry_with(length=None), "length is required"),
    ("a chain of another name", lambda: entry_with(chain="weekly"), "chain is one of"),
    (
        "an openEnd that is not the type",
        lambda: entry_with(openEnd="never"),
        "openEnd 'never' is not a DATE",
    ),
    (
        "an activation of another type",
        lambda: entry_with(activation="seats"),
        "activation names 'seats'",
    ),
    (
        "an activation that is a key",
        lambda: entry_with(activation="version_id"),
        "a key, a validity column",
    ),
    (
        "an activation that is a validity column",
        lambda: entry_with(activation="valid_from"),
        "a key, a validity column",
    ),
    (
        "an activation that is the parent's key",
        lambda: entry_with(activation="customer_id"),
        "a key, a validity column",
    ),
    (
        "a tracked column with no levels",
        lambda: entry_with(tracked=[{"column": "seats", "transitions": {"a": {"b": "1"}}}]),
        "not categorical",
    ),
    (
        "a tracked column with a nullFraction",
        lambda: entry_with(
            tracked=[
                {
                    "column": "region",
                    "transitions": {"north": {"south": "1"}, "south": {"north": "1"}},
                }
            ]
        ),
        "nullFraction",
    ),
    (
        "a tracked column that is a key",
        lambda: entry_with(tracked=[{"column": "version_id", "transitions": {"1": {"2": "1"}}}]),
        "a key, a validity column",
    ),
    (
        "a tracked column the emit lacks",
        lambda: entry_with(tracked=[{"column": "ghost", "transitions": {"1": {"2": "1"}}}]),
        "does not declare",
    ),
    (
        "a transition to an unknown level",
        lambda: entry_with(tracked=[{"column": "plan", "transitions": {"basic": {"gold": "1"}}}]),
        "level 'gold'",
    ),
    (
        "a transition from an unknown level",
        lambda: entry_with(tracked=[{"column": "plan", "transitions": {"gold": {"basic": "1"}}}]),
        "level 'gold'",
    ),
    (
        "a level moving to itself",
        lambda: entry_with(tracked=[{"column": "plan", "transitions": {"basic": {"basic": "1"}}}]),
        "to itself",
    ),
    (
        "a transition row that totals zero",
        lambda: entry_with(
            tracked=[{"column": "plan", "transitions": {"basic": {"pro": "0", "team": "0"}}}]
        ),
        "total zero",
    ),
    (
        "an initial with an unknown level",
        lambda: entry_with(
            tracked=[
                {
                    "column": "plan",
                    "initial": {"levels": ["gold"], "levelWeights": ["1"]},
                    "transitions": {"basic": {"pro": "1"}},
                }
            ]
        ),
        "level 'gold'",
    ),
    (
        "two tracked columns and no reasons",
        lambda: entry_with(
            tracked=[
                ENTRY["tracked"][0],
                {
                    "column": "tier",
                    "transitions": {"gold": {"silver": "1"}, "silver": {"gold": "1"}},
                },
            ]
        ),
        "reasons is required",
    ),
    (
        "reasons naming an untracked column",
        lambda: entry_with(reasons={"levels": ["status"], "levelWeights": ["1"]}),
        "not a tracked column",
    ),
    (
        "an untracked share of zero",
        lambda: entry_with(untracked="0"),
        "untracked is a decimal above 0 and below 1",
    ),
    (
        "an untracked share of one",
        lambda: entry_with(untracked="1"),
        "untracked is a decimal above 0 and below 1",
    ),
    (
        "an untracked share above one",
        lambda: entry_with(untracked="1.5"),
        "untracked is a decimal above 0 and below 1",
    ),
    (
        "an untracked share that is no number",
        lambda: entry_with(untracked="most"),
        "untracked is a decimal above 0 and below 1",
    ),
    (
        "an untracked share beside no tracked column",
        lambda: entry_with(tracked=None, untracked="0.5", end=None),
        "untracked needs a tracked column",
    ),
    (
        "an end mode of another name",
        lambda: entry_with(end={**ENTRY["end"], "mode": "stop"}),
        "end.mode is one of",
    ),
    (
        "a finalVersion with no column",
        lambda: entry_with(end={"mode": "finalVersion", "share": "0.1"}),
        "finalVersion",
    ),
    (
        "an end share above one",
        lambda: entry_with(end={**ENTRY["end"], "share": "1.5"}),
        "end.share is a decimal from 0 to 1",
    ),
    (
        "an end share below zero",
        lambda: entry_with(end={**ENTRY["end"], "share": "-0.1"}),
        "end.share is a decimal from 0 to 1",
    ),
    (
        "an end value that is not a level",
        lambda: entry_with(end={**ENTRY["end"], "value": "gone"}),
        "not a level of 'status'",
    ),
    (
        "an end column that is a key",
        lambda: entry_with(end={**ENTRY["end"], "column": "version_id"}),
        "a key, a validity column",
    ),
    (
        "an end column with no levels",
        lambda: entry_with(end={**ENTRY["end"], "column": "seats"}),
        "not categorical",
    ),
    (
        "an end value that initial reaches",
        lambda: entry_with(
            tracked=[
                {
                    "column": "status",
                    "initial": {"levels": ["active", "cancelled"], "levelWeights": ["1", "1"]},
                    "transitions": {"active": {"cancelled": "1"}},
                }
            ]
        ),
        "above zero",
    ),
    (
        "an end value that the column's own weights reach",
        lambda: entry_with(
            tracked=[{"column": "status", "transitions": {"active": {"cancelled": "1"}}}]
        ),
        "above zero",
    ),
    (
        "an end value that a transition reaches",
        lambda: entry_with(
            tracked=[
                {
                    "column": "status",
                    "initial": {"levels": ["active"], "levelWeights": ["1"]},
                    "transitions": {"active": {"cancelled": "1"}},
                }
            ]
        ),
        "above zero",
    ),
]


#: The open end's refusals (ADR 0017, the amendment of 2026-11-25): an ``until`` in its year, and an
#: ``openEnd`` a closed version could hold.
OPEN_END_REFUSED: List[Tuple[str, Change, str]] = [
    (
        "an until in the year 9999",
        lambda: entry_with(until="9999-12-31"),
        "until '9999-12-31' is in the year 9999",
    ),
    (
        "an openEnd before until",
        lambda: entry_with(openEnd="2026-09-01"),
        "openEnd '2026-09-01' is before until '2026-09-30'",
    ),
    (
        "an openEnd inside the window under nextStart",
        lambda: entry_with(openEnd="2026-09-30"),
        "openEnd '2026-09-30' is inside timeBounds",
    ),
]


@pytest.mark.parametrize(
    ("change", "fragment"),
    [(c, f) for _, c, f in REFUSED + OPEN_END_REFUSED],
    ids=[name for name, _, _ in REFUSED + OPEN_END_REFUSED],
)
def test_a_refused_entry_is_flx_3403_naming_the_entry_and_the_key(
    tmp_path: Path, change: Change, fragment: str
) -> None:
    with pytest.raises(FluxError) as caught:
        resolved(bundle_with(tmp_path, "refused", [change()]))
    assert caught.value.code.code == "FLX-3403", str(caught.value)
    assert fragment in str(caught.value), str(caught.value)
    assert "history[0]" in str(caught.value) or "history (subscription_version)" in str(
        caught.value
    )


def test_a_primary_entity_that_is_the_natural_entity_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FluxError, match="share a key") as caught:
        resolved(bundle_with(tmp_path, "same", [ENTRY], primary="subscription_id"))
    assert caught.value.code.code == "FLX-3403"
    assert "history (subscription_version)" in str(caught.value)


def test_a_reachable_level_with_no_transitions_row_is_refused(tmp_path: Path) -> None:
    tracked = [{"column": "plan", "transitions": {"basic": {"pro": "1"}, "pro": {"basic": "1"}}}]
    with pytest.raises(FluxError, match="no row for the level 'team'") as caught:
        resolved(bundle_with(tmp_path, "norow", [entry_with(tracked=tracked)]))
    assert caught.value.code.code == "FLX-3403"


def test_a_level_a_chain_cannot_reach_needs_no_row(tmp_path: Path) -> None:
    tracked = [
        {
            "column": "plan",
            "initial": {"levels": ["basic", "pro"], "levelWeights": ["1", "1"]},
            "transitions": {"basic": {"pro": "1"}, "pro": {"basic": "1"}},
        }
    ]
    resolved(bundle_with(tmp_path, "unreached", [entry_with(tracked=tracked)]))


def test_a_column_drawn_given_a_tracked_column_is_refused(tmp_path: Path) -> None:
    columns = {"tier": {"levels": {"gold": "0.5", "silver": "0.5"}, "given": ["plan"]}}
    with pytest.raises(FluxError, match="'tier' is drawn given it") as caught:
        resolved(bundle_with(tmp_path, "given", [ENTRY], columns=columns))
    assert caught.value.code.code == "FLX-3403"


def test_an_end_value_a_carried_end_column_can_draw_is_refused(tmp_path: Path) -> None:
    entry = entry_with(end={**ENTRY["end"], "column": "tier", "value": "gold"})
    with pytest.raises(FluxError, match="statistics of 'tier'") as caught:
        resolved(bundle_with(tmp_path, "carried", [entry]))
    assert caught.value.code.code == "FLX-3403"


def test_a_second_entry_for_one_emit_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FluxError, match="second entry") as caught:
        resolved(bundle_with(tmp_path, "twice", [ENTRY, ENTRY]))
    assert caught.value.code.code == "FLX-3403" and "history[1]" in str(caught.value)


def test_an_emit_with_no_validity_window_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FluxError, match="no validity window") as caught:
        resolved(bundle_with(tmp_path, "flat", [ENTRY], window=False))
    assert caught.value.code.code == "FLX-3403"


def test_a_start_and_an_end_of_different_types_are_refused(tmp_path: Path) -> None:
    with pytest.raises(FluxError, match="both DATE or both TIMESTAMP") as caught:
        resolved(bundle_with(tmp_path, "mixed", [ENTRY], end_type="TIMESTAMP"))
    assert caught.value.code.code == "FLX-3403"


def test_an_end_column_the_contract_requires_needs_an_open_end(tmp_path: Path) -> None:
    with pytest.raises(FluxError, match="openEnd is required") as caught:
        resolved(bundle_with(tmp_path, "required", [ENTRY], end_required=True))
    assert caught.value.code.code == "FLX-3403"


def test_a_parent_that_a_counts_entry_also_names_is_refused(tmp_path: Path) -> None:
    counts = [
        {
            "emit": "subscription_version",
            "relationship": "customer",
            "children": {"levels": ["1", "2"], "levelWeights": ["0.5", "0.5"]},
        }
    ]
    with pytest.raises(FluxError, match="counts entry") as caught:
        resolved(bundle_with(tmp_path, "counted", [ENTRY], namespace={"counts": counts}))
    assert caught.value.code.code == "FLX-3403"


@pytest.mark.parametrize("link", [{"when": "plan = 'pro'"}, {"weight": "customer_id"}])
def test_a_parent_that_a_links_entry_conditions_or_weighs_is_refused(
    tmp_path: Path, link: Mapping[str, str]
) -> None:
    links = [{"emit": "subscription_version", "relationship": "customer", **link}]
    with pytest.raises(FluxError, match="links entry") as caught:
        resolved(bundle_with(tmp_path, "linked", [ENTRY], namespace={"links": links}))
    assert caught.value.code.code == "FLX-3403"


def test_a_links_entry_with_sentinels_alone_leaves_the_parent_alone(tmp_path: Path) -> None:
    links = [
        {
            "emit": "subscription_version",
            "relationship": "customer",
            "sentinels": {"customer_id": "0"},
        }
    ]
    assert "subscription_version" in resolved(
        bundle_with(tmp_path, "sentinel", [ENTRY], namespace={"links": links})
    )


def test_the_reader_alone_names_the_entry_and_the_key() -> None:
    with pytest.raises(FluxError) as caught:
        read_history_block(
            [entry_with(entities=0)],
            emits=frozenset({"subscription_version"}),
            where="here",
            code=FLX_3105,
            refusal=FLX_3105,
        )
    assert "here[0] (emit 'subscription_version').entities" in str(caught.value)


# --- H, G and the plan -------------------------------------------------------------------------


def test_an_entry_moves_h_of_its_emit_and_neither_g_nor_another_emit(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    moved = _digests(bundle_with(tmp_path, "history", [ENTRY]))
    assert moved["subscription_version"][0] != plain["subscription_version"][0]
    assert moved["subscription_version"][1] == plain["subscription_version"][1]
    assert moved["customer"] == plain["customer"]


def test_an_edit_of_the_entry_moves_h_again_and_never_g(tmp_path: Path) -> None:
    first = _digests(bundle_with(tmp_path, "one", [ENTRY]))
    second = _digests(bundle_with(tmp_path, "two", [entry_with(entities=101)]))
    assert first["subscription_version"][0] != second["subscription_version"][0]
    assert first["subscription_version"][1] == second["subscription_version"][1]


def test_the_member_is_bound_only_to_the_emit_that_has_an_entry(tmp_path: Path) -> None:
    emits = resolved(bundle_with(tmp_path, "bound", [ENTRY]))
    assert HISTORY_KEY in (emits["subscription_version"].extension or {})
    assert HISTORY_KEY not in (emits["customer"].extension or {})


def test_a_bundle_without_the_key_keeps_its_plan_digest(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, "plain")
    assert _digests(bundle) == _digests(bundle_with(tmp_path, "again", None))
    first = pipeline.execute(bundle, kernel=KERNEL_V2)
    second = pipeline.execute(bundle_with(tmp_path, "third", []), kernel=KERNEL_V2)
    assert [emit.digests.h.value for emit in first.emits] == [
        emit.digests.h.value for emit in second.emits
    ]


def test_kernel_0_1_0_refuses_the_key_and_names_the_kernel(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle_with(tmp_path, "k1", [ENTRY]), kernel=KERNEL_V1)
    assert caught.value.code is FLX_3105
    assert "0.2.0" in str(caught.value) and "0.1.0" in str(caught.value)


def test_a_declared_history_generates_its_chain(tmp_path: Path) -> None:
    result = pipeline.execute(bundle_with(tmp_path, "k2", [ENTRY]), kernel=KERNEL_V2)
    emit = next(e for e in result.emits if e.expose.expose_id == "subscription_version")
    assert emit.plan is not None and emit.plan.relations is not None
    assert emit.plan.relations.driver("valid_from") is not None
