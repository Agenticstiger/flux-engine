# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Counted layouts end to end (ADR 0016 sections 1 and 2): invoices, their lines and their accounts.

The bundle is ``tests/fixtures/exact-counts`` with two ``counts`` entries: the lines of each invoice
(``invoice_line.invoice``) and the invoices of each account (``invoice.account``), each with a shape
of children per parent whose slot values total the child rows. What is pinned, on kernel 0.2.0:

* **The layout is the table.** Every parent holds the children the layout allots it, in consecutive
  rows, and the histogram of children per parent is the declared one, class for class.
* **Two paths, one answer.** The column path's rows equal the per-row path's, ``--jobs`` does not move
  the digests, and the shares of a ``--shard`` joined are the whole run's bytes.
* **Scale.** ``--scale 0.5`` and ``--scale 2`` plan and apply, and the counts still total the
  scaled child rows.
* **Kernel 0.1.0** refuses the key, naming the kernel.
"""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator import columns
from flux_engine.generator.counts import (
    class_counts,
    classes_of,
    count_key,
    counted_layout,
    slot_values,
)
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from tests.generator.test_rules_over_links import _by_both_paths

from .conftest import REPO_ROOT

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "exact-counts"
INVOICES, ACCOUNTS = 1200, 300
LINES_PER_INVOICE = {"levels": ["1", "3", "6"], "levelWeights": ["0.5", "0.3", "0.2"]}
#: Enough lines (8 760) for two worker processes under ``--jobs 4``.
MANY_LINES_PER_INVOICE = {"levels": ["3", "7", "12"], "levelWeights": ["0.3", "0.4", "0.3"]}
INVOICES_PER_ACCOUNT = {"levels": ["2", "4", "6"], "levelWeights": ["0.25", "0.5", "0.25"]}


def _flow(shape: Mapping[str, Sequence[str]]) -> str:
    keys = tuple(shape)
    return "{ " + ", ".join(f"{key}: {json.dumps(list(shape[key]))}" for key in keys) + " }"


def lines_of(shape: Mapping[str, Sequence[str]]) -> int:
    """The child rows whose total is the slot values of *shape* over the invoices."""
    return int(slot_values(INVOICES, classes_of(shape)).sum())


def bundle_with(
    tmp_path: Path,
    name: str,
    *,
    lines: Mapping[str, Sequence[str]] = LINES_PER_INVOICE,
    accounts: Mapping[str, Sequence[str]] = INVOICES_PER_ACCOUNT,
    counted: bool = True,
    minimum: Optional[int] = None,
    line_rows: Optional[int] = None,
    account_when: Optional[str] = None,
    account_minimum: Optional[int] = None,
    invoice_when: Optional[str] = None,
) -> Path:
    """A copy of ``tests/fixtures/exact-counts`` whose lines total *lines*' slots, with counts entries."""
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    path = target / "simulation.flux.yml"
    text = path.read_text("utf-8")
    total = lines_of(lines) if line_rows is None else line_rows
    for old, new in (
        ("        rows: 4000\n", f"        rows: {total}\n"),
        (
            "line_id: { range: { min: '1', max: '4000' } }",
            f"line_id: {{ range: {{ min: '1', max: '{total}' }} }}",
        ),
    ):
        assert text.count(old) == 1
        text = text.replace(old, new)
    floor = "" if minimum is None else f" minChildren: {minimum},"
    floor += "" if invoice_when is None else f" parentWhen: {json.dumps(invoice_when)},"
    account_floor = "" if account_minimum is None else f" minChildren: {account_minimum},"
    account_floor += "" if account_when is None else f" parentWhen: {json.dumps(account_when)},"
    block = (
        "    counts:\n"
        f"      - {{ emit: invoice_line, relationship: invoice,{floor} children: {_flow(lines)} }}\n"
        f"      - {{ emit: invoice, relationship: account,{account_floor} children: {_flow(accounts)} }}\n"
        if counted
        else ""
    )
    anchor = "  com.agentics.flux/1:\n"
    assert text.count(anchor) == 1
    path.write_text(text.replace(anchor, anchor + block), "utf-8")
    return target


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return bundle_with(tmp_path_factory.mktemp("counted"), "counted")


@pytest.fixture(scope="module")
def outcome(bundle: Path) -> Any:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def _table(outcome: Any, expose_id: str) -> List[Dict[str, Any]]:
    emit = outcome.emit(expose_id)
    return [dict(zip(list(emit.expose.column_names), row, strict=True)) for row in emit.rows]


def _check_counted(
    outcome: Any,
    child: str,
    parent: str,
    key: str,
    shape: Mapping[str, Sequence[str]],
) -> List[int]:
    """Assert *child*'s rows are the layout of *shape* over *parent*'s, and return the counts."""
    children, parents = _table(outcome, child), _table(outcome, parent)
    relationship = {"invoice_line": "invoice", "invoice": "account"}[child]
    layout = counted_layout(
        count_key(outcome.root, outcome.emit(child).plan.sim_id, child, relationship),
        len(parents),
        len(children),
        shape=shape,
    )
    names = [row[key] for row in parents]
    assert len(set(names)) == len(names)
    assert [row[key] for row in children] == [
        names[layout.parent_of(i)] for i in range(len(children))
    ]
    for number, run_parent in enumerate(layout.counts.tolist()):
        run = layout.children_of(number)
        assert len(run) == run_parent
        assert {children[i][key] for i in run} <= {names[number]}
    return [int(count) for count in layout.counts]


def test_every_invoice_holds_the_lines_the_layout_allots_it(outcome: Any) -> None:
    counts = _check_counted(outcome, "invoice_line", "invoice", "invoice_id", LINES_PER_INVOICE)
    classes = classes_of(LINES_PER_INVOICE)
    histogram = [counts.count(level) for level in (1, 3, 6)]
    assert histogram == class_counts(INVOICES, classes) == [600, 360, 240]
    assert sum(counts) == len(_table(outcome, "invoice_line")) == lines_of(LINES_PER_INVOICE)
    assert min(counts) >= 1  # no invoice is without a line


def test_every_account_holds_the_invoices_the_layout_allots_it(outcome: Any) -> None:
    counts = _check_counted(outcome, "invoice", "account", "account_id", INVOICES_PER_ACCOUNT)
    histogram = [counts.count(level) for level in (2, 4, 6)]
    assert histogram == class_counts(ACCOUNTS, classes_of(INVOICES_PER_ACCOUNT)) == [75, 150, 75]
    assert sum(counts) == INVOICES


def _planned_emits(bundle: Path, state: Path) -> Dict[str, Any]:
    planned = plan(bundle, state_dir=state, kernel="0.2.0", stage=False)
    assert planned.success, planned.stderr
    layout = RunLayout.for_state_dir(state, str(planned.artifacts["run_id"]))
    emits: Dict[str, Any] = json.loads(layout.plan_path.read_text("utf-8"))["emits"]
    return emits


def test_the_plan_moves_the_digests_of_the_two_counted_emits_and_g_of_none(
    bundle: Path, tmp_path: Path
) -> None:
    counted = _planned_emits(bundle, tmp_path / "counted")
    plain = _planned_emits(bundle_with(tmp_path, "plain", counted=False), tmp_path / "plain")
    for expose_id in ("invoice", "invoice_line"):
        assert counted[expose_id]["H"] != plain[expose_id]["H"], expose_id
        assert counted[expose_id]["generator"]["generatorDigest"] != (
            plain[expose_id]["generator"]["generatorDigest"]
        ), expose_id
    for expose_id in counted:
        assert counted[expose_id]["G"] == plain[expose_id]["G"], expose_id
    for expose_id in ("product", "account"):
        assert counted[expose_id] == plain[expose_id], expose_id


def test_validate_reads_the_declaration(bundle: Path) -> None:
    assert validate(bundle).success


def test_kernel_0_1_0_refuses_the_key_and_names_the_kernel(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "0.2.0" in str(caught.value)


def test_a_bundle_with_no_counts_leaves_invoices_without_lines(tmp_path: Path) -> None:
    plain = pipeline.execute(bundle_with(tmp_path, "plain", counted=False), kernel=KERNEL_V2)
    held = {row["invoice_id"] for row in _table(plain, "invoice_line")}
    assert len(held) < INVOICES  # a pick leaves some invoices childless: the layout never does


@pytest.mark.parametrize("chunk", [4096, 500])
def test_the_column_paths_rows_are_the_per_row_paths_rows(
    monkeypatch: pytest.MonkeyPatch, bundle: Path, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(bundle, kernel=KERNEL_V2)
    )
    for expose_id in ("invoice", "invoice_line", "account", "product"):
        assert by_columns.emit(expose_id).rows == by_rows.emit(expose_id).rows, expose_id
    assert taken and all(taken)


def _shares(bundle: Path, count: int) -> Tuple[Any, List[Any]]:
    whole = pipeline.execute(bundle, kernel=KERNEL_V2)
    parts = [
        pipeline.execute(bundle, kernel=KERNEL_V2, shard=(i, count)) for i in range(1, count + 1)
    ]
    return whole, parts


@pytest.mark.parametrize("count", [3, 7])
def test_the_shares_joined_are_the_whole_runs_bytes(bundle: Path, count: int) -> None:
    whole, parts = _shares(bundle, count)
    for expose_id in ("invoice", "invoice_line"):
        joined = b"".join(part.emit(expose_id).writer.stream_bytes() for part in parts)
        assert joined == whole.emit(expose_id).writer.stream_bytes(), expose_id


def _digests(applied: Any) -> Dict[str, Any]:
    return {k: v for k, v in applied.artifacts.items() if "digest" in k.lower() and k != "run_id"}


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_jobs_do_not_move_the_digests_or_the_bytes(tmp_path: Path) -> None:
    many = bundle_with(tmp_path, "many", lines=MANY_LINES_PER_INVOICE)
    runs = [pipeline.execute(many, kernel=KERNEL_V2, jobs=jobs) for jobs in (1, 4)]
    assert runs[1].emit("invoice_line").processes > 1
    for expose_id in ("invoice", "invoice_line"):
        assert (
            runs[0].emit(expose_id).writer.stream_bytes()
            == runs[1].emit(expose_id).writer.stream_bytes()
        )
    found = []
    for jobs in (1, 4):
        state = tmp_path / f"state-{jobs}"
        planned = plan(many, state_dir=state, jobs=jobs, kernel="0.2.0")
        assert planned.success, planned.stderr
        applied = apply(many, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
        assert applied.success, applied.stderr
        found.append((planned.artifacts.get("plan_digest"), _digests(applied)))
    assert found[0] == found[1]


@pytest.mark.parametrize("scale", ["0.5", "2"])
def test_a_scaled_run_plans_and_its_counts_total_the_scaled_child_rows(
    bundle: Path, tmp_path: Path, scale: str
) -> None:
    planned = plan(bundle, state_dir=tmp_path / "state", kernel="0.2.0", scale=scale, stage=False)
    assert planned.success, planned.stderr
    run_id = str(planned.artifacts["run_id"])
    applied = apply(bundle, run_id=run_id, state_dir=tmp_path / "state")
    assert applied.success, applied.stderr
    written = RunLayout.for_state_dir(tmp_path / "state", run_id).emit_path("invoice_line")
    assert len(written.read_text("utf-8").splitlines()) == int(
        lines_of(LINES_PER_INVOICE) * Decimal(scale)
    )
    scaled = pipeline.execute(bundle, kernel=KERNEL_V2, scale=Decimal(scale))
    lines, invoices = _table(scaled, "invoice_line"), _table(scaled, "invoice")
    assert len(lines) == int(lines_of(LINES_PER_INVOICE) * Decimal(scale))
    assert len(invoices) == int(INVOICES * Decimal(scale))
    held: Dict[Any, int] = {}
    for row in lines:
        held[row["invoice_id"]] = held.get(row["invoice_id"], 0) + 1
    assert sum(held.values()) == len(lines) and set(held) == {r["invoice_id"] for r in invoices}
    assert max(held.values()) - min(held.values()) <= 6


# -- minChildren (ADR 0016 section 2, step 3) -------------------------------------------------

#: The class of none folds into the minimum: no invoice has fewer than m lines.
WITH_NONE = {"levels": ["0", "2", "4"], "levelWeights": ["0.2", "0.4", "0.4"]}


@pytest.mark.parametrize("minimum", [1, 2])
def test_every_invoice_has_at_least_the_minimum_and_the_counts_total_the_child_rows(
    tmp_path: Path, minimum: int
) -> None:
    target = bundle_with(tmp_path, f"min{minimum}", lines=WITH_NONE, minimum=minimum)
    outcome = pipeline.execute(target, kernel=KERNEL_V2)
    lines = _table(outcome, "invoice_line")
    held: Dict[Any, int] = {}
    for row in lines:
        held[row["invoice_id"]] = held.get(row["invoice_id"], 0) + 1
    invoices = [row["invoice_id"] for row in _table(outcome, "invoice")]
    assert len(lines) == lines_of(WITH_NONE) and sum(held.values()) == len(lines)
    assert min(held.get(key, 0) for key in invoices) >= minimum
    layout = counted_layout(
        count_key(
            outcome.root, outcome.emit("invoice_line").plan.sim_id, "invoice_line", "invoice"
        ),
        INVOICES,
        len(lines),
        shape=WITH_NONE,
        minimum=minimum,
    )
    assert [held.get(key, 0) for key in invoices] == layout.counts.tolist()


def test_the_column_path_equals_the_per_row_path_under_a_minimum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = bundle_with(tmp_path, "min-paths", lines=WITH_NONE, minimum=2)
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(target, kernel=KERNEL_V2)
    )
    assert by_columns.emit("invoice_line").rows == by_rows.emit("invoice_line").rows
    assert taken and all(taken)


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_a_minimum_does_not_move_the_digests_with_jobs(tmp_path: Path) -> None:
    many = bundle_with(tmp_path, "min-many", lines=MANY_LINES_PER_INVOICE, minimum=2)
    found = []
    for jobs in (1, 4):
        state = tmp_path / f"state-{jobs}"
        planned = plan(many, state_dir=state, jobs=jobs, kernel="0.2.0")
        assert planned.success, planned.stderr
        applied = apply(many, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
        assert applied.success, applied.stderr
        found.append((planned.artifacts.get("plan_digest"), _digests(applied)))
    assert found[0] == found[1]


def test_child_rows_that_cannot_give_every_parent_its_minimum_are_refused_at_plan(
    tmp_path: Path,
) -> None:
    short = bundle_with(
        tmp_path, "short", lines=LEVELS_THREE, minimum=3, line_rows=3 * INVOICES - 1
    )
    with pytest.raises(FluxError) as caught:
        pipeline.execute(short, kernel=KERNEL_V2)
    message = str(caught.value)
    assert caught.value.code.code == "FLX-3403"
    assert "N = 3599" in message and "m = 3" in message and "e = 1200" in message
    assert "--scale" not in message


LEVELS_THREE = {"levels": ["3", "4"], "levelWeights": ["1", "0"]}


def test_a_scale_that_leaves_too_few_child_rows_is_refused_and_names_the_scale(
    tmp_path: Path,
) -> None:
    exact = bundle_with(tmp_path, "exact", lines=LEVELS_THREE, minimum=3)
    assert pipeline.execute(exact, kernel=KERNEL_V2).emit("invoice_line").rows
    # 1200 invoices at 0.3338333 are 401 (400.6 rounds up) and 3600 lines are 1202 (1201.8): 1203 are needed.
    with pytest.raises(FluxError) as caught:
        pipeline.execute(exact, kernel=KERNEL_V2, scale=Decimal("0.3338333"))
    message = str(caught.value)
    assert caught.value.code.code == "FLX-3403"
    assert "N = 1202" in message and "e = 401" in message
    assert "--scale 0.3338333" in message and "too few child rows" in message
    assert pipeline.execute(exact, kernel=KERNEL_V2, scale=Decimal("0.5"))


def test_a_scale_does_not_name_itself_when_scale_one_leaves_too_few_child_rows_too(
    tmp_path: Path,
) -> None:
    short = bundle_with(
        tmp_path, "short-scaled", lines=LEVELS_THREE, minimum=3, line_rows=3 * INVOICES - 1
    )
    for scale in (Decimal(1), Decimal(2)):
        with pytest.raises(FluxError) as caught:
            pipeline.execute(short, kernel=KERNEL_V2, scale=scale)
        message = str(caught.value)
        assert caught.value.code.code == "FLX-3403" and "N - m * e < 0" in message
        # Raising the scale cannot help: the same shortfall is there at scale 1.
        assert "--scale" not in message and "too few child rows" not in message


@pytest.mark.parametrize("scale", ["0.5", "0.3338333", "0.9"])
def test_a_scale_below_one_does_not_name_itself_when_scale_one_leaves_too_few_child_rows_too(
    tmp_path: Path, scale: str
) -> None:
    short = bundle_with(
        tmp_path, "short-below", lines=LEVELS_THREE, minimum=3, line_rows=3 * INVOICES - 10
    )
    with pytest.raises(FluxError) as at_one:
        pipeline.execute(short, kernel=KERNEL_V2, scale=Decimal(1))
    with pytest.raises(FluxError) as caught:
        pipeline.execute(short, kernel=KERNEL_V2, scale=Decimal(scale))
    message = str(caught.value)
    assert caught.value.code.code == "FLX-3403" and "N - m * e < 0" in message
    # Raising the scale cannot help: the same shortfall is there at scale 1.
    assert "--scale" not in message and "too few child rows" not in message
    assert "--scale" not in str(at_one.value)


@pytest.mark.parametrize("shard", [None, (1, 3)])
def test_a_scale_below_one_names_itself_when_only_the_scale_leaves_too_few_child_rows(
    tmp_path: Path, shard: Optional[Tuple[int, int]]
) -> None:
    exact = bundle_with(tmp_path, "exact-below", lines=LEVELS_THREE, minimum=3)
    assert pipeline.execute(exact, kernel=KERNEL_V2, scale=Decimal(1))
    with pytest.raises(FluxError) as caught:
        pipeline.execute(exact, kernel=KERNEL_V2, scale=Decimal("0.3338333"), shard=shard)
    message = str(caught.value)
    assert "--scale 0.3338333" in message and "too few child rows" in message


def test_a_minimum_above_the_top_of_the_shape_is_refused_at_plan(tmp_path: Path) -> None:
    target = bundle_with(tmp_path, "top", lines=LINES_PER_INVOICE, minimum=7)
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3403" and "above the most children" in str(caught.value)


# -- parentWhen (ADR 0016 section 4) ----------------------------------------------------------

OPEN_ACCOUNTS = "status <> 'closed'"


def _conditioned(tmp_path: Path, name: str = "when", **extra: Any) -> Path:
    return bundle_with(tmp_path, name, account_when=OPEN_ACCOUNTS, **extra)


def _held(outcome: Any) -> Tuple[List[Dict[str, Any]], Dict[Any, int]]:
    accounts = _table(outcome, "account")
    counts: Dict[Any, int] = {row["account_id"]: 0 for row in accounts}
    for row in _table(outcome, "invoice"):
        counts[row["account_id"]] += 1
    return accounts, counts


@pytest.mark.parametrize("minimum", [None, 1, 2])
def test_no_closed_account_has_an_invoice_and_every_open_one_has_the_minimum(
    tmp_path: Path, minimum: Optional[int]
) -> None:
    outcome = pipeline.execute(_conditioned(tmp_path, account_minimum=minimum), kernel=KERNEL_V2)
    accounts, counts = _held(outcome)
    closed = [row["account_id"] for row in accounts if row["status"] == "closed"]
    opened = [row["account_id"] for row in accounts if row["status"] != "closed"]
    assert closed and opened
    assert all(counts[key] == 0 for key in closed)
    assert all(counts[key] >= (minimum or 0) for key in opened)
    assert sum(counts.values()) == len(_table(outcome, "invoice")) == INVOICES
    layout = counted_layout(
        count_key(outcome.root, outcome.emit("invoice").plan.sim_id, "invoice", "account"),
        ACCOUNTS,
        INVOICES,
        shape=INVOICES_PER_ACCOUNT,
        minimum=minimum or 0,
        eligible=[row for row, a in enumerate(accounts) if a["status"] != "closed"],
    )
    assert [counts[row["account_id"]] for row in accounts] == layout.counts.tolist()


def test_a_condition_changes_the_plan_digests_of_the_child_only(tmp_path: Path) -> None:
    plain = _planned_emits(bundle_with(tmp_path, "p"), tmp_path / "ps")
    when = _planned_emits(_conditioned(tmp_path), tmp_path / "ws")
    assert when["invoice"]["H"] != plain["invoice"]["H"]
    assert when["invoice"]["generator"]["generatorDigest"] != (
        plain["invoice"]["generator"]["generatorDigest"]
    )
    assert when["account"] == plain["account"]
    assert {e["G"] for e in when.values()} == {e["G"] for e in plain.values()}


def test_the_column_path_equals_the_per_row_path_under_a_condition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = _conditioned(tmp_path, account_minimum=1)
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(target, kernel=KERNEL_V2)
    )
    for expose_id in ("invoice", "invoice_line", "account"):
        assert by_columns.emit(expose_id).rows == by_rows.emit(expose_id).rows, expose_id
    assert taken and all(taken)


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_a_condition_does_not_move_the_digests_with_jobs_or_shards(tmp_path: Path) -> None:
    many = _conditioned(tmp_path, lines=MANY_LINES_PER_INVOICE)
    found = []
    for jobs in (1, 4):
        state = tmp_path / f"state-{jobs}"
        planned = plan(many, state_dir=state, jobs=jobs, kernel="0.2.0")
        assert planned.success, planned.stderr
        applied = apply(many, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
        assert applied.success, applied.stderr
        found.append((planned.artifacts.get("plan_digest"), _digests(applied)))
    assert found[0] == found[1]
    whole, parts = _shares(many, 3)
    for expose_id in ("invoice", "invoice_line"):
        joined = b"".join(part.emit(expose_id).writer.stream_bytes() for part in parts)
        assert joined == whole.emit(expose_id).writer.stream_bytes(), expose_id


def test_a_scaled_run_keeps_the_condition(tmp_path: Path) -> None:
    outcome = pipeline.execute(_conditioned(tmp_path), kernel=KERNEL_V2, scale=Decimal("0.5"))
    accounts, counts = _held(outcome)
    assert all(counts[r["account_id"]] == 0 for r in accounts if r["status"] == "closed")
    assert sum(counts.values()) == int(INVOICES * Decimal("0.5"))


def test_child_rows_too_few_for_the_open_accounts_are_refused_when_the_layout_is_worked_out(
    tmp_path: Path,
) -> None:
    target = _conditioned(tmp_path, account_minimum=6)  # 1200 invoices, about 240 open accounts
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2)
    message = str(caught.value)
    assert caught.value.code.code == "FLX-3403"
    assert "N = 1200" in message and "m = 6" in message and "e = " in message


def _without_shape(target: Path) -> None:
    """Make the invoices-per-account entry a minimum and a parentWhen alone, with no shape."""
    path = target / "simulation.flux.yml"
    text = path.read_text("utf-8")
    shape = f" children: {_flow(INVOICES_PER_ACCOUNT)} }}"
    assert text.count(shape) == 1
    path.write_text(text.replace(shape, " }"), "utf-8")


def test_a_scale_below_one_does_not_name_itself_for_the_open_accounts_when_scale_one_fails_too(
    tmp_path: Path,
) -> None:
    # The eligible accounts are known only when the layout is first worked out, in a scaled run too.
    target = _conditioned(tmp_path, account_minimum=6)  # refused at scale 1 as well
    _without_shape(target)
    scaled = Decimal("0.37")
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2, scale=scaled)
    message = str(caught.value)
    assert caught.value.code.code == "FLX-3403" and "N - m * e < 0" in message
    assert "--scale" not in message and "too few child rows" not in message
    assert message.count("FLX-3403") == 1
    # Through the CLI's plan, and through a shard.
    planned = plan(target, state_dir=tmp_path / "state", kernel="0.2.0", scale="0.37", stage=False)
    assert not planned.success and "--scale" not in planned.stderr
    with pytest.raises(FluxError) as shard:
        pipeline.execute(target, kernel=KERNEL_V2, scale=scaled, shard=(1, 3))
    assert "--scale" not in str(shard.value)


def test_a_scale_does_not_name_itself_when_scale_one_leaves_too_few_rows_for_the_open_accounts(
    tmp_path: Path,
) -> None:
    target = _conditioned(tmp_path, account_minimum=6)  # refused at scale 1: 1200 for ~240 open
    _without_shape(target)
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2, scale=Decimal(2))
    message = str(caught.value)
    assert caught.value.code.code == "FLX-3403" and "N - m * e < 0" in message
    assert "--scale" not in message and "too few child rows" not in message


def test_too_few_child_rows_for_the_open_accounts_do_not_name_a_scale_at_scale_one(
    tmp_path: Path,
) -> None:
    target = _conditioned(tmp_path, account_minimum=6)
    for scale in (None, Decimal(1)):
        with pytest.raises(FluxError) as caught:
            pipeline.execute(target, kernel=KERNEL_V2, scale=scale)
        assert "N - m * e < 0" in str(caught.value) and "--scale" not in str(caught.value)


def test_a_condition_no_account_meets_is_refused_when_the_layout_is_worked_out(
    tmp_path: Path,
) -> None:
    target = bundle_with(tmp_path, "none", account_when="status = 'nowhere'")
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3403" and "e = 0" in str(caught.value)


def test_a_condition_that_reads_a_link_is_refused_and_names_parent_when(tmp_path: Path) -> None:
    target = bundle_with(tmp_path, "link", invoice_when="account.status = 'active'")
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2)
    assert caught.value.code.code in ("FLX-3403", "FLX-6210")
    assert "parentWhen" in str(caught.value)


def test_a_condition_that_is_not_a_predicate_is_refused(tmp_path: Path) -> None:
    target = bundle_with(tmp_path, "number", account_when="account_id + 1")
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-6206"


def test_kernel_0_1_0_refuses_the_condition(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(_conditioned(tmp_path), kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "0.2.0" in str(caught.value)
