# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Two histories, a snapshot and an as-of reader, and a window that grows, through the ``fluxsim`` command line.

The bundle is ``tests/fixtures/history-window``: ``subscription_version``, a history on DATE columns with a
shape of versions, a start histogram and lengths long enough that the chains of its busiest entities are
compressed to fit, ending entities with a final version; ``device_version``, a history on TIMESTAMP columns
with versions spread evenly, the default first start and ending entities with a closed last version; the
``invoice`` rows that read a subscription as of a date after the histories' ``until``; and
``subscription_current``, the snapshot of the subscriptions. Every run is ``fluxsim plan``, ``apply`` and
``verify`` in a subprocess, and every assertion reads the rows the run wrote. What is pinned:

* **The lines.** Every ``history:*`` and ``snapshot:*`` line of ``verify`` passes on every entity, and the
  ``asOf`` line counts every invoice as reading a version.
* **The chains.** A chain whose drawn lengths overrun its room is compressed, and one that fits is not; a DATE
  and a TIMESTAMP chain have no gap and no overlap; both end modes end the share of entities they declare.
* **The window.** Under two windows with the same ``until`` the histories, the snapshot and the reader are the
  same bytes and have the same output digests, while ``planDigest`` differs (ADR 0017 section 10).
* **Size.** ``--jobs 1`` and ``--jobs 4`` make one run, ``--scale 0.5`` keeps every guarantee, and the shares of
  ``--shard I/3`` joined are the whole run's bytes.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

import pytest
import yaml

from flux_engine.generator.history import ended_count
from flux_engine.stages import _parallel as parallel
from flux_engine.stages.layout import RunLayout

from .conftest import REPO_ROOT

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "history-window"
WINDOW_END = 'end: "2027-01-01T00:00:00Z"'
LONG_WINDOW_END = "2030-06-30T00:00:00Z"
UNTIL = date(2025, 12, 31)
SUBSCRIPTIONS, DEVICES, INVOICES = "subscription_version", "device_version", "invoice"
SNAPSHOT = "subscription_current"
HISTORIES = (SUBSCRIPTIONS, DEVICES)
#: The emits the window must not move: every one of them is made from a history or reads one.
HELD = (SUBSCRIPTIONS, DEVICES, INVOICES, SNAPSHOT)
EXPOSES = ("customer", SUBSCRIPTIONS, DEVICES, INVOICES, SNAPSHOT)
#: Every emit of the fixture, the events and the periodic rows of ADR 0027 included.
ALL_EXPOSES = (*EXPOSES, "subscription_change", "subscription_month")
#: The shortest length a subscription's version is drawn at, in days: a closed one shorter was compressed.
SHORTEST_LENGTH = 200
AMOUNTS = {"basic": "19.00", "pro": "49.00", "team": "99.00"}
LINES = {
    SUBSCRIPTIONS: {"history:no_gaps_no_overlap", "history:one_current", "history:ended_is_last"},
    DEVICES: {"history:no_gaps_no_overlap", "history:one_current", "history:ended_is_last"},
    SNAPSHOT: {"snapshot:equals_last_version"},
}


def _bundle(tmp_path: Path, name: str = "bundle", *, window_end: Optional[str] = None) -> Path:
    """A copy of the fixture; with *window_end* its window is that long."""
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    if window_end is not None:
        path = target / "simulation.flux.yml"
        text = path.read_text("utf-8")
        assert text.count(WINDOW_END) == 1
        path.write_text(text.replace(WINDOW_END, f'end: "{window_end}"'), "utf-8")
    return target


def _cli(args: Sequence[str], cwd: Path) -> Dict[str, Any]:
    """Run ``fluxsim -j <args>`` as a subprocess and return its JSON envelope."""
    argv = [sys.executable, "-m", "flux_engine.cli", "-j", *args]
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr or done.stdout
    envelope: Dict[str, Any] = json.loads(done.stdout)
    assert envelope["ok"], envelope
    return envelope


class Run(NamedTuple):
    bundle: Path
    state: Path
    layout: RunLayout


def _plan(bundle: Path, state: Path, *options: str) -> RunLayout:
    planned = _cli(
        ["plan", str(bundle), "--state-dir", str(state), "--kernel", "0.2.0", *options],
        state.parent,
    )
    return RunLayout.for_state_dir(state, str(planned["artifacts"]["run_id"]))


def _apply(bundle: Path, layout: RunLayout, state: Path, *options: str) -> None:
    arguments = ["apply", str(bundle), "--state-dir", str(state), "--run-id", layout.run_id]
    _cli([*arguments, *options], state.parent)


def _run(
    tmp_path: Path,
    bundle: Path,
    name: str = "state",
    *,
    scale: Optional[str] = None,
    jobs: Optional[int] = None,
) -> Run:
    """Plan and apply *bundle* under kernel 0.2.0, at *scale* and under *jobs* when they are given."""
    state = tmp_path / name
    layout = _plan(bundle, state, *([] if scale is None else ["--scale", scale]))
    _apply(bundle, layout, state, *([] if jobs is None else ["--jobs", str(jobs)]))
    return Run(bundle, state, layout)


def _verify(run: Run) -> Dict[str, Any]:
    arguments = [
        "verify",
        str(run.bundle),
        "--state-dir",
        str(run.state),
        "--run-id",
        run.layout.run_id,
    ]
    return _cli(arguments, run.state.parent)


def _rows(layout: RunLayout, expose_id: str) -> List[Dict[str, Any]]:
    names = json.loads(layout.seam_attestation_path(expose_id).read_text("utf-8"))["columns"]
    return [
        dict(zip(names, json.loads(line), strict=True))
        for line in layout.emit_path(expose_id).read_text("utf-8").splitlines()
    ]


def _by_entity(rows: Sequence[Dict[str, Any]], key: str) -> Dict[Any, List[Dict[str, Any]]]:
    held: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows:
        held.setdefault(row[key], []).append(row)
    return held


def _digests(layout: RunLayout) -> Dict[str, Any]:
    document: Dict[str, Any] = json.loads(layout.run_digest_path.read_text("utf-8"))
    return document


def _lines(verified: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Every rules line of a verify envelope, by emit and by rule."""
    return {
        expose_id: {entry["rule"]: entry for entry in entries}
        for expose_id, entries in verified["artifacts"]["rules"]["emits"].items()
    }


def _check_lines(verified: Dict[str, Any], *, entities: Dict[str, int], invoices: int) -> None:
    """Every history and snapshot line passes on every entity, and the reader reads a version on every row."""
    assert verified["exitCode"] == 0
    found = _lines(verified)
    for expose_id, rules in LINES.items():
        assert {
            rule for rule in found[expose_id] if rule.split(":")[0] in ("history", "snapshot")
        } == rules
        for rule in rules:
            entry = found[expose_id][rule]
            assert entry["met"] is True and entry["enforced"] is True, (expose_id, rule)
            assert entry["satisfied"] == entry["total"] == entities[expose_id], (expose_id, rule)
    reader = found[INVOICES]["asOf:subscription.invoice_date"]
    assert reader["enforced"] is False and reader["satisfied"] == reader["total"] == invoices
    rule = found[INVOICES]["amount_of_plan"]
    assert rule["met"] is True and rule["satisfied"] == rule["total"] == invoices


def _day(text: str) -> date:
    return date.fromisoformat(text)


def _instant(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture(scope="module")
def whole(tmp_path_factory: pytest.TempPathFactory) -> Run:
    tmp_path = tmp_path_factory.mktemp("whole")
    return _run(tmp_path, _bundle(tmp_path))


# --- the lines ---------------------------------------------------------------------------------


def test_every_history_and_snapshot_line_passes(whole: Run) -> None:
    sizes = {name: len(_rows(whole.layout, name)) for name in EXPOSES}
    assert sizes == {"customer": 50, SUBSCRIPTIONS: 144, DEVICES: 90, INVOICES: 600, SNAPSHOT: 60}
    _check_lines(
        _verify(whole), entities={SUBSCRIPTIONS: 60, DEVICES: 30, SNAPSHOT: 60}, invoices=600
    )


# --- the chains --------------------------------------------------------------------------------


def test_the_subscriptions_that_overrun_their_room_are_compressed_and_the_others_are_not(
    whole: Run,
) -> None:
    held = _by_entity(_rows(whole.layout, SUBSCRIPTIONS), "subscription_id")
    assert len(held) == 60
    compressed, kept = [], []
    for versions in held.values():
        closed = [
            (_day(row["valid_to"]) - _day(row["valid_from"])).days
            for row in versions
            if row["valid_to"]
        ]
        # Every drawn length is at least the shortest bin's edge, so a shorter closed one was compressed.
        (compressed if any(days < SHORTEST_LENGTH for days in closed) else kept).append(versions)
    assert compressed and kept
    for versions in compressed:
        # A compressed chain fills its room: its last version starts the day before until.
        assert _day(versions[-1]["valid_from"]) == UNTIL - timedelta(days=1)
    # Seven closed versions of at least 200 days always overrun the room, so every busiest entity is compressed.
    assert {len(versions) for versions in held.values()} == {1, 2, 3, 8}
    assert all(len(versions) < 8 for versions in kept)
    assert any(len(versions) == 8 for versions in compressed)
    assert {len(versions) for versions in kept} >= {1, 2}


def test_a_date_chain_and_a_timestamp_chain_have_no_gap_and_no_overlap(whole: Run) -> None:
    subscriptions = _by_entity(_rows(whole.layout, SUBSCRIPTIONS), "subscription_id")
    for versions in subscriptions.values():
        assert [row["valid_to"] for row in versions[:-1]] == [
            row["valid_from"] for row in versions[1:]
        ]
        assert all(len(row["valid_from"]) == 10 for row in versions)  # a DATE
        assert all(_day(row["valid_from"]) < UNTIL for row in versions)
    devices = _by_entity(_rows(whole.layout, DEVICES), "device_id")
    for versions in devices.values():
        closed = [row for row in versions if row["valid_to"] is not None]
        for before, after in zip(versions, versions[1:], strict=False):
            assert before["valid_to"] == after["valid_from"]
        assert len(closed) >= len(versions) - 1
        assert all(len(row["valid_from"]) == len("2023-05-24T15:27:09.887Z") for row in versions)
        assert all(_instant(row["valid_from"]) < datetime(2025, 12, 31) for row in versions)
    # The default first start is spread over the window's part before until, to the millisecond.
    firsts = sorted(_instant(versions[0]["valid_from"]) for versions in devices.values())
    assert firsts[0].year == 2023 and firsts[-1] > datetime(2025, 1, 1)
    assert any(first.microsecond for first in firsts)


def test_both_end_modes_end_the_share_of_entities_they_declare(whole: Run) -> None:
    subscriptions = _by_entity(_rows(whole.layout, SUBSCRIPTIONS), "subscription_id")
    eligible = sum(1 for versions in subscriptions.values() if len(versions) >= 2)
    ended = [
        versions for versions in subscriptions.values() if versions[-1]["status"] == "cancelled"
    ]
    assert len(ended) == ended_count(60, eligible, "0.25") > 0
    # A final version stays open, so every subscription has one open version, its last.
    assert all(versions[-1]["valid_to"] is None for versions in subscriptions.values())
    assert all(row["status"] == "active" for versions in ended for row in versions[:-1])
    devices = _by_entity(_rows(whole.layout, DEVICES), "device_id")
    retired = [versions for versions in devices.values() if versions[-1]["status"] == "retired"]
    assert len(retired) == ended_count(len(devices), len(devices), "0.3") > 0
    # A closed last version has an end, and the others stay open.
    assert all(versions[-1]["valid_to"] is not None for versions in retired)
    others = [versions for versions in devices.values() if versions[-1]["status"] != "retired"]
    assert others and all(versions[-1]["valid_to"] is None for versions in others)


# --- the reader and the snapshot ---------------------------------------------------------------


def test_an_invoice_dated_after_until_reads_the_version_valid_on_its_date(whole: Run) -> None:
    subscriptions = _by_entity(_rows(whole.layout, SUBSCRIPTIONS), "subscription_id")
    seen = set()
    for invoice in _rows(whole.layout, INVOICES):
        assert _day(invoice["invoice_date"]) > UNTIL
        version = subscriptions[invoice["subscription_id"]][
            -1
        ]  # open: the only one valid after until
        assert version["valid_to"] is None
        assert invoice["amount"] == AMOUNTS[version["plan"]]
        seen.add(version["plan"])
    assert seen == set(AMOUNTS)


def test_the_snapshot_is_the_last_version_of_each_subscription(whole: Run) -> None:
    subscriptions = _by_entity(_rows(whole.layout, SUBSCRIPTIONS), "subscription_id")
    snapshot = _rows(whole.layout, SNAPSHOT)
    assert [row["subscription_id"] for row in snapshot] == sorted(subscriptions)
    for row in snapshot:
        last = subscriptions[row["subscription_id"]][-1]
        assert {name: last[name] for name in row} == row


# --- the window --------------------------------------------------------------------------------


def test_two_windows_with_one_until_give_the_same_bytes_and_digests_and_two_plan_digests(
    tmp_path: Path, whole: Run
) -> None:
    wide = _run(tmp_path, _bundle(tmp_path, "wide", window_end=LONG_WINDOW_END), "wide-state")
    for expose_id in HELD:
        narrow_bytes = whole.layout.emit_path(expose_id).read_bytes()
        assert narrow_bytes and wide.layout.emit_path(expose_id).read_bytes() == narrow_bytes
        assert _digests(wide.layout)["components"]["emits"][expose_id] == (
            _digests(whole.layout)["components"]["emits"][expose_id]
        )
    assert _digests(wide.layout)["planDigest"] != _digests(whole.layout)["planDigest"]
    assert wide.layout.run_id != whole.layout.run_id
    _check_lines(
        _verify(wide), entities={SUBSCRIPTIONS: 60, DEVICES: 30, SNAPSHOT: 60}, invoices=600
    )


# --- jobs, scale, shard ------------------------------------------------------------------------


def _assert_same_run(first: Run, second: Run, expose_ids: Sequence[str] = ALL_EXPOSES) -> None:
    for expose_id in expose_ids:
        assert (
            first.layout.emit_path(expose_id).read_bytes()
            == second.layout.emit_path(expose_id).read_bytes()
        )
    assert first.layout.run_digest_path.read_bytes() == second.layout.run_digest_path.read_bytes()


def test_one_job_and_four_jobs_make_the_same_run(tmp_path: Path, whole: Run) -> None:
    four = _run(tmp_path, _bundle(tmp_path, "four"), "four-state", jobs=4)
    _assert_same_run(whole, four)
    _check_lines(
        _verify(four), entities={SUBSCRIPTIONS: 60, DEVICES: 30, SNAPSHOT: 60}, invoices=600
    )


def _grown(tmp_path: Path, name: str, factor: int) -> Path:
    """A copy of the fixture whose entities, rows and key ranges are *factor* times as many."""
    bundle = _bundle(tmp_path, name)
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    namespace = document["extensions"]["com.agentics.flux/1"]
    for entry in namespace["history"]:
        entry["entities"] *= factor
    for emit in namespace["emits"].values():
        if "rows" in emit:  # a projected emit declares none
            emit["rows"] *= factor
        for name, column in emit["fidelity"]["columns"].items():
            span = column.get("range")
            if name.endswith(("_id", "_sk")) and span is not None and int(span["max"]) < 10**6:
                span["max"] = str(int(span["max"]) * factor)
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return bundle


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_four_jobs_split_a_large_run_into_the_bytes_one_job_writes(tmp_path: Path) -> None:
    factor = 60  # 8640 versions of 3600 subscriptions: more than two jobs' share of rows
    assert 144 * factor >= 2 * parallel.MIN_ROWS_PER_JOB
    one = _run(tmp_path, _grown(tmp_path, "one", factor), "one-state", jobs=1)
    four = _run(tmp_path, _grown(tmp_path, "four", factor), "four-state", jobs=4)
    assert len(_rows(four.layout, SNAPSHOT)) == 60 * factor
    _assert_same_run(one, four)


def test_a_half_scale_run_keeps_every_guarantee(tmp_path: Path) -> None:
    run = _run(tmp_path, _bundle(tmp_path), "half", scale="0.5")
    sizes = {name: len(_rows(run.layout, name)) for name in EXPOSES}
    assert sizes == {"customer": 25, SUBSCRIPTIONS: 72, DEVICES: 45, INVOICES: 300, SNAPSHOT: 30}
    _check_lines(
        _verify(run), entities={SUBSCRIPTIONS: 30, DEVICES: 15, SNAPSHOT: 30}, invoices=300
    )


def test_a_small_scale_that_leaves_one_version_per_entity_still_plans(tmp_path: Path) -> None:
    """At ``--scale 0.015`` every device has one version, so no entity can end, and none ends.

    The key ranges are widened first, since ``--scale`` scales a key's span too and these
    spans would otherwise empty before the history is planned.
    """
    bundle = _bundle(tmp_path)
    path = bundle / "simulation.flux.yml"
    text = re.sub(
        r"max: '(\d+)'",
        lambda found: f"max: '{int(found.group(1)) * 1000}'",
        path.read_text("utf-8"),
    )
    path.write_text(text, "utf-8")
    run = _run(tmp_path, bundle, "tiny", scale="0.015")
    assert len(_rows(run.layout, DEVICES)) == 1
    _verify(run)


def test_the_three_shares_joined_are_the_whole_runs_bytes(whole: Run) -> None:
    count = 3
    for index in range(1, count + 1):
        _apply(whole.bundle, whole.layout, whole.state, "--shard", f"{index}/{count}")
    for expose_id in ALL_EXPOSES:
        shares = [
            whole.layout.shard_emit_path(expose_id, index, count) for index in range(1, count + 1)
        ]
        assert (
            b"".join(share.read_bytes() for share in shares)
            == whole.layout.emit_path(expose_id).read_bytes()
        )
