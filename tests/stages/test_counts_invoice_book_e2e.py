# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An invoice header whose total sums its lines, through the ``fluxsim`` command line.

The bundle is ``tests/fixtures/counts-invoices``: accounts, the invoices of each account that is not closed and
the lines of each invoice, laid out by two ``counts`` entries, with the invoice's ``total``,
``line_count`` and ``max_qty`` declared as contract aggregates of its lines. Every run here is
``fluxsim plan``, ``apply`` and ``verify`` in a subprocess, and every assertion reads the rows
the run wrote. What is pinned:

* **Verify passes and the aggregates hold.** Each aggregate holds on every invoice, recomputed
  from the output rows, and the rules lines of ``verify`` say so.
* **The layout.** No eligible account is below its minimum, no closed account has an invoice, no
  invoice is without a line, and the histogram of children per parent is the declared one, class
  for class.
* **The scorecard** names format 1-4-0 and has a ``childrenPerParent`` row within tolerance for
  each counted relationship.
* **Size.** The same holds at ``--scale 0.5``, under ``--jobs 4``, and the shares of
  ``--shard I/3`` joined are the whole run's bytes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

import pytest

from flux_engine.core.digest import digest_of_document
from flux_engine.stages import _parallel as parallel
from flux_engine.stages.layout import RunLayout
from flux_stats.profile import SCHEMA_URL_1_2_0

from .conftest import REPO_ROOT

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "counts-invoices"
NAMESPACE = "  com.agentics.flux/1:\n"
ACCOUNTS, INVOICES, LINES = 300, 1200, 3360
#: The declared shapes, level by level: the counts and the shares of the parents that have them.
INVOICE_LEVELS = {3: Decimal("0.25"), 5: Decimal("0.5"), 7: Decimal("0.25")}
LINE_LEVELS = {1: Decimal("0.4"), 3: Decimal("0.4"), 6: Decimal("0.2")}
SCORECARD_ROWS = ("children:invoice.account", "children:invoice_line.invoice")
EXPOSES = ("account", "invoice", "invoice_line")


def _run(args: Sequence[str], cwd: Path) -> Dict[str, Any]:
    """Run ``fluxsim -j <args>`` as a subprocess and return its JSON envelope."""
    argv = [sys.executable, "-m", "flux_engine.cli", "-j", *args]
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr or done.stdout
    envelope: Dict[str, Any] = json.loads(done.stdout)
    assert envelope["ok"], envelope
    return envelope


def _histogram_profile(
    parents: str, child: str, edges: List[str], counts: List[int]
) -> Dict[str, Any]:
    total = sum(counts)
    return {
        "parent": {"table": parents, "columns": [f"{parents}_id"]},
        "child": {"table": child, "columns": [f"{parents}_id"]},
        "statistics": [
            {
                "definition": "marginalHistogram/1",
                "bins": edges,
                "sampleSize": {"min": total, "max": total},
                "disclosure": "none",
                "groups": [{"group": "", "k": total, "binCounts": counts, "status": "F"}],
            }
        ],
    }


def _profile() -> Dict[str, Any]:
    def column(rows: int, mean: str) -> Dict[str, Any]:
        """A number column whose published statistic is a mean, as a fidelity property reads it."""
        total = int(Decimal(mean) * rows * (1 << 16))
        group = {"group": "", "k": rows, "Sx": str(total), "status": "F"}
        statistic = {
            "definition": "mean/1",
            "sampleSize": {"min": rows, "max": rows},
            "disclosure": "none",
            "groups": [group],
        }
        return {"domain": "number", "statistics": [statistic]}

    plain: Dict[str, Any] = {"domain": "number", "statistics": []}
    invoice_account = _histogram_profile(
        "account", "invoice", ["0", "1", "3", "4", "5", "6", "7", "8"], [60, 0, 60, 0, 120, 0, 60]
    )
    invoice_account["child"] = {"table": "invoice", "columns": ["account_id"]}
    lines = _histogram_profile("invoice", "invoice_line", ["1", "2", "4", "7"], [40, 40, 20])
    return {
        "_producer": "urn:example:test-profiler",
        "_schemaURL": SCHEMA_URL_1_2_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "account": {"columns": {"account_id": column(ACCOUNTS, "150.5")}},
            "invoice": {"columns": {"invoice_id": column(INVOICES, "600.5"), "account_id": plain}},
            "invoice_line": {"columns": {"invoice_id": plain, "amount": column(LINES, "450.5")}},
        },
        "relationships": [invoice_account, lines],
    }


class Bundle(NamedTuple):
    path: Path
    profile: Path


def _bundle(tmp_path: Path) -> Bundle:
    """A copy of the committed fixture that pins the profile the scorecard compares with."""
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE, target)
    profile = tmp_path / "profile.json"
    document = _profile()
    profile.write_text(json.dumps(document), encoding="utf-8")
    digest = digest_of_document(document, subject="p").value
    simulation = target / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert text.count(NAMESPACE) == 1
    simulation.write_text(
        text.replace(NAMESPACE, NAMESPACE + f"    profile:\n      digest: {digest}\n"), "utf-8"
    )
    return Bundle(target, profile)


class Run(NamedTuple):
    bundle: Bundle
    state: Path
    layout: RunLayout
    verified: Dict[str, Any]
    tables: Dict[str, List[Dict[str, Any]]]


def _read(layout: RunLayout, expose_id: str) -> List[Dict[str, Any]]:
    names = json.loads(layout.seam_attestation_path(expose_id).read_text("utf-8"))["columns"]
    return [
        dict(zip(names, json.loads(line), strict=True))
        for line in layout.emit_path(expose_id).read_text("utf-8").splitlines()
    ]


def _plan_apply_verify(
    tmp_path: Path,
    *,
    scale: Optional[str] = None,
    jobs: Optional[int] = None,
    bundle: Optional[Bundle] = None,
) -> Run:
    held = bundle or _bundle(tmp_path)
    state = tmp_path / ".flux"
    options: List[str] = ["--state-dir", str(state)]
    options += [] if scale is None else ["--scale", scale]
    options += [] if jobs is None else ["--jobs", str(jobs)]
    planned = _run(["plan", str(held.path), *options, "--profile", str(held.profile)], tmp_path)
    run_id = str(planned["artifacts"]["run_id"])
    apply_options = ["--state-dir", str(state), "--run-id", run_id]
    apply_options += [] if jobs is None else ["--jobs", str(jobs)]
    _run(["apply", str(held.path), *apply_options], tmp_path)
    verified = _run(
        [
            "verify",
            str(held.path),
            "--state-dir",
            str(state),
            "--run-id",
            run_id,
            "--profile",
            str(held.profile),
            "--allow-declared-baseline-drift",
        ],
        tmp_path,
    )
    layout = RunLayout.for_state_dir(state, run_id)
    return Run(held, state, layout, verified, {name: _read(layout, name) for name in EXPOSES})


def _children(parents: List[Dict[str, Any]], children: List[Dict[str, Any]], key: str) -> Counter:
    held: Counter = Counter({row[key]: 0 for row in parents})
    held.update(row[key] for row in children)
    return held


def _check_run(run: Run, *, rows: Dict[str, int]) -> None:
    """Assert everything the layout and the aggregates promise of *run*, from its rows alone."""
    accounts, invoices, lines = (run.tables[name] for name in EXPOSES)
    assert {name: len(run.tables[name]) for name in EXPOSES} == rows
    assert run.verified["ok"] and run.verified["exitCode"] == 0

    # Every aggregate, recomputed from the rows of the lines, holds on every invoice.
    held: Dict[Any, List[Dict[str, Any]]] = {row["invoice_id"]: [] for row in invoices}
    for line in lines:
        held[line["invoice_id"]].append(line)
    for invoice in invoices:
        mine = held[invoice["invoice_id"]]
        assert Decimal(invoice["total"]) == sum((Decimal(r["amount"]) for r in mine), Decimal(0))
        assert invoice["line_count"] == len(mine)
        assert invoice["max_qty"] == max(r["quantity"] for r in mine)

    # No eligible parent is below its minimum, and no parent outside the condition has a child.
    per_account = _children(accounts, invoices, "account_id")
    eligible = [row["account_id"] for row in accounts if row["status"] != "closed"]
    closed = [row["account_id"] for row in accounts if row["status"] == "closed"]
    assert eligible and closed
    assert min(per_account[key] for key in eligible) >= 1
    assert all(per_account[key] == 0 for key in closed)
    assert min(len(mine) for mine in held.values()) >= 1

    # The histogram of children per parent is the declared one.
    seen_lines = Counter(len(mine) for mine in held.values())
    seen_accounts = Counter(per_account[key] for key in eligible)
    seen_lines = Counter(len(mine) for mine in held.values())
    assert dict(seen_lines) == _largest_remainder(LINE_LEVELS, len(invoices))
    assert dict(seen_accounts) == _largest_remainder(INVOICE_LEVELS, len(eligible))


def _largest_remainder(levels: Dict[int, Decimal], parents: int) -> Dict[int, int]:
    """Parents per class by the largest remainder of *parents* over the class shares."""
    exact = {level: share * parents for level, share in levels.items()}
    counts = {level: int(value) for level, value in exact.items()}
    spare = parents - sum(counts.values())
    for level in sorted(exact, key=lambda each: (-(exact[each] - counts[each]), each))[:spare]:
        counts[level] += 1
    return counts


def _check_rules_and_scorecard(run: Run) -> None:
    artifacts = run.verified["artifacts"]
    rules = {
        expose_id: {entry["rule"]: entry for entry in entries}
        for expose_id, entries in artifacts["rules"]["emits"].items()
    }
    for rule in ("invoice_total", "invoice_line_count", "invoice_max_qty"):
        entry = rules["invoice"][rule]
        assert (entry["satisfied"], entry["total"], entry["share"]) == (
            len(run.tables["invoice"]),
            len(run.tables["invoice"]),
            "1",
        )
        assert entry["met"] is True and entry["enforced"] is True
    for expose_id, rule in (
        ("invoice", "counts:account.minChildren"),
        ("invoice", "counts:account.parentWhen"),
        ("invoice_line", "counts:invoice.minChildren"),
    ):
        entry = rules[expose_id][rule]
        assert entry["met"] is True and entry["satisfied"] == entry["total"] > 0, rule

    document = artifacts["scorecard"]["document"]
    assert document["_schemaURL"].endswith("/scorecard/1-4-0/FluxScorecard.json")
    found = {
        row["propertyId"]: row
        for table in document["tables"]
        for row in table["properties"]
        if row["propertyId"] in SCORECARD_ROWS
    }
    assert set(found) == set(SCORECARD_ROWS)
    for row in found.values():
        assert row["property"] == "childrenPerParent" and row["outcome"] == "within", row


@pytest.fixture(scope="module")
def whole(tmp_path_factory: pytest.TempPathFactory) -> Run:
    return _plan_apply_verify(tmp_path_factory.mktemp("whole"))


def test_verify_passes_and_every_aggregate_holds_on_every_invoice(whole: Run) -> None:
    _check_run(whole, rows={"account": ACCOUNTS, "invoice": INVOICES, "invoice_line": LINES})
    _check_rules_and_scorecard(whole)


def test_a_half_scale_run_keeps_every_guarantee(tmp_path: Path) -> None:
    run = _plan_apply_verify(tmp_path, scale="0.5")
    _check_run(
        run, rows={"account": ACCOUNTS // 2, "invoice": INVOICES // 2, "invoice_line": LINES // 2}
    )
    _check_rules_and_scorecard(run)


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_jobs_four_writes_the_whole_runs_bytes_and_keeps_every_guarantee(
    whole: Run, tmp_path: Path
) -> None:
    run = _plan_apply_verify(tmp_path, jobs=4)
    _check_run(run, rows={"account": ACCOUNTS, "invoice": INVOICES, "invoice_line": LINES})
    _check_rules_and_scorecard(run)
    for expose_id in EXPOSES:
        assert (
            run.layout.emit_path(expose_id).read_bytes()
            == whole.layout.emit_path(expose_id).read_bytes()
        )
    assert run.layout.run_digest_path.read_bytes() == whole.layout.run_digest_path.read_bytes()


def test_the_three_shares_joined_are_the_whole_runs_bytes(whole: Run) -> None:
    for index in (1, 2, 3):
        _run(
            [
                "apply",
                str(whole.bundle.path),
                "--state-dir",
                str(whole.state),
                "--run-id",
                whole.layout.run_id,
                "--shard",
                f"{index}/3",
            ],
            whole.state.parent,
        )
    for expose_id in EXPOSES:
        shares = [
            whole.layout.emit_path(expose_id).with_name(f"{expose_id}.shard-{index}-of-3.ndjson")
            for index in (1, 2, 3)
        ]
        joined = b"".join(share.read_bytes() for share in shares)
        assert joined == whole.layout.emit_path(expose_id).read_bytes(), expose_id
