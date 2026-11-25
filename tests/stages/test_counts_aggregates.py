# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Parent aggregates over a counted relationship end to end (ADR 0016 section 5).

The bundle is ``tests/fixtures/exact-counts`` with a ``counts`` entry for the lines of each
invoice, whose shape has a class of invoices with no line, and a contract whose ``invoice``
declares summaries of its lines: a ``sum`` with ``ifNone``, a ``count``, a ``max``, a filtered
``count`` and ``sum``, and a ``sum`` over a nullable column. What is pinned, on kernel 0.2.0:

* **Held by construction.** Every invoice's summaries equal what its own lines hold, for all of
  them, an invoice without a line included.
* **Refusals.** A relationship with no ``counts`` entry names the key, kernel 0.1.0 refuses, a
  child that reads a summary through a link is a cycle, and a column that cannot hold the value
  is refused.
* **Paths.** The per-row path and the column path write the same rows, ``--jobs`` and ``--shard``
  do not move a byte or a digest, and ``--scale`` below and above 1 keeps every summary.
* **Digests.** The parent's plan names the child plan it sums, and a plan with no summary keeps
  its document.
"""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator import columns
from flux_engine.generator.counts import classes_of, slot_values
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from tests.bundles import repin_contract
from tests.generator.test_rules_over_links import _by_both_paths
from tests.stages.test_counts_run import _flow, _shares

from ._fleet import group_by
from .conftest import REPO_ROOT

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "exact-counts"
CONTRACT = "sales-chain.fluid.yml"
INVOICES = 1200
#: An invoice with no line, with one and with five: a 0 class for ``ifNone`` to answer.
LINES = {"levels": ["0", "1", "5"], "levelWeights": ["0.25", "0.35", "0.4"]}

_ISSUED = "        - { name: issued_on, type: DATE, required: true }\n"
_AMOUNT = '        - { name: amount, type: "DECIMAL(12,2)", required: true }\n'
_DISCOUNT = '        - { name: discount, type: "DECIMAL(10,2)", required: false }\n'
_TOTALS = """        - { name: total, type: "DECIMAL(14,2)", required: true }
        - { name: line_count, type: INT, required: true }
        - { name: max_qty, type: INT, required: false }
        - { name: big_lines, type: INT, required: true }
        - { name: big_total, type: "DECIMAL(14,2)", required: true }
        - { name: discount_total, type: "DECIMAL(14,2)", required: false }
"""
_SUMMARIES = """      invariants:
        - id: invoice_total
          derive: total
          aggregate: { function: sum, column: amount, from: invoice_line, via: invoice, ifNone: 0 }
        - id: invoice_line_count
          derive: line_count
          aggregate: { function: count, from: invoice_line, via: invoice }
        - id: invoice_max_qty
          derive: max_qty
          aggregate: { function: max, column: quantity, from: invoice_line, via: invoice }
        - id: invoice_big_lines
          derive: big_lines
          aggregate: { function: count, from: invoice_line, via: invoice, filter: "quantity >= 6" }
        - id: invoice_big_total
          derive: big_total
          aggregate:
            function: sum
            column: amount
            from: invoice_line
            via: invoice
            filter: "quantity >= 6"
            ifNone: 0
        - id: invoice_discount_total
          derive: discount_total
          aggregate: { function: sum, column: discount, from: invoice_line, via: invoice }
"""
_DISCOUNT_DRAW = (
    "            amount: { range: { min: '1', max: '900' } }\n"
    "            discount: { range: { min: '1', max: '50' }, nullFraction: '0.4' }\n"
)


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text("utf-8")
    assert text.count(old) == 1, old
    path.write_text(text.replace(old, new, 1), "utf-8")


def line_rows(shape: Mapping[str, Sequence[str]]) -> int:
    """The child rows whose total is the slot values of *shape* over the invoices."""
    return int(slot_values(INVOICES, classes_of(shape)).sum())


def bundle_with(
    tmp_path: Path,
    name: str = "bundle",
    *,
    lines: Mapping[str, Sequence[str]] = LINES,
    counted: bool = True,
    totals: str = _TOTALS,
    summaries: str = _SUMMARIES,
    line_columns: str = "",
    line_invariants: str = "",
    parent_when: Optional[str] = None,
) -> Path:
    """A copy of ``tests/fixtures/exact-counts`` whose ``invoice`` summarises its lines.

    The invoice gains *totals* and the contract invariants *summaries*; the line gains a nullable
    ``discount`` and *line_columns*, and the counted relationship is declared unless *counted* is
    false.
    """
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    contract, simulation = target / CONTRACT, target / "simulation.flux.yml"
    _edit(contract, _ISSUED, _ISSUED + totals + summaries)
    _edit(contract, _AMOUNT, _AMOUNT + _DISCOUNT + line_columns + line_invariants)
    total = line_rows(lines)
    _edit(simulation, "        rows: 4000\n", f"        rows: {total}\n")
    _edit(
        simulation,
        "line_id: { range: { min: '1', max: '4000' } }",
        f"line_id: {{ range: {{ min: '1', max: '{total}' }} }}",
    )
    _edit(simulation, "            amount: { range: { min: '1', max: '900' } }\n", _DISCOUNT_DRAW)
    if counted:
        anchor = "  com.agentics.flux/1:\n"
        _edit(
            simulation,
            anchor,
            anchor
            + "    counts:\n"
            + "      - { emit: invoice_line, relationship: invoice, "
            + ("" if parent_when is None else f'parentWhen: "{parent_when}", ')
            + f"children: {_flow(lines)} }}\n",
        )
    repin_contract(contract, simulation, "retail.sales_chain")
    return target


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return bundle_with(tmp_path_factory.mktemp("aggregates"))


@pytest.fixture(scope="module")
def outcome(bundle: Path) -> Any:
    return pipeline.execute(bundle, kernel=KERNEL_V2)


def _table(outcome: Any, expose_id: str) -> List[Dict[str, Any]]:
    emit = outcome.emit(expose_id)
    return [dict(zip(list(emit.expose.column_names), row, strict=True)) for row in emit.rows]


def _lines_of(invoices: Sequence[Mapping[str, Any]], lines: Sequence[Mapping[str, Any]]) -> Any:
    return group_by(lines, "invoice_id", [row["invoice_id"] for row in invoices])


def _sum(values: Sequence[Optional[Decimal]]) -> Optional[Decimal]:
    present = [value for value in values if value is not None]
    return sum(present, Decimal(0)) if present else None


def _expected(children: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """What an invoice's summaries must be for its *children*, worked out from the rows alone."""
    big = [line for line in children if line["quantity"] >= 6]
    return {
        "total": _sum([line["amount"] for line in children]) or Decimal(0),
        "line_count": len(children),
        "max_qty": max((line["quantity"] for line in children), default=None),
        "big_lines": len(big),
        "big_total": _sum([line["amount"] for line in big]) or Decimal(0),
        "discount_total": _sum([line["discount"] for line in children]),
    }


def _check_summaries(
    invoices: Sequence[Mapping[str, Any]], lines: Sequence[Mapping[str, Any]]
) -> int:
    """Assert every invoice holds its summaries; return how many invoices were checked."""
    held = _lines_of(invoices, lines)
    for invoice in invoices:
        found = {name: invoice[name] for name in _expected([])}
        assert found == _expected(held[invoice["invoice_id"]]), invoice["invoice_id"]
    return len(invoices)


def test_every_invoice_holds_the_summaries_of_its_lines(outcome: Any) -> None:
    invoices, lines = _table(outcome, "invoice"), _table(outcome, "invoice_line")
    assert _check_summaries(invoices, lines) == INVOICES
    assert len(lines) == line_rows(LINES)


def test_an_invoice_with_no_line_takes_if_none_and_a_null_summary(outcome: Any) -> None:
    invoices, lines = _table(outcome, "invoice"), _table(outcome, "invoice_line")
    held = _lines_of(invoices, lines)
    childless = [row for row in invoices if not held[row["invoice_id"]]]
    assert len(childless) == 300
    for row in childless:
        assert row["total"] == Decimal("0.00") and row["big_total"] == Decimal("0.00")
        assert row["line_count"] == row["big_lines"] == 0
        assert row["max_qty"] is None and row["discount_total"] is None


def test_a_null_in_a_child_column_is_left_out_of_the_sum(outcome: Any) -> None:
    invoices, lines = _table(outcome, "invoice"), _table(outcome, "invoice_line")
    held = _lines_of(invoices, lines)
    mixed = [
        row
        for row in invoices
        if any(line["discount"] is None for line in held[row["invoice_id"]])
        and any(line["discount"] is not None for line in held[row["invoice_id"]])
    ]
    assert mixed, "some invoice holds a NULL discount and a value"
    for row in mixed:
        assert row["discount_total"] == _sum([line["discount"] for line in held[row["invoice_id"]]])
    nothing = [
        row
        for row in invoices
        if held[row["invoice_id"]]
        and all(line["discount"] is None for line in held[row["invoice_id"]])
    ]
    assert nothing and all(row["discount_total"] is None for row in nothing)


def test_the_filter_counts_only_the_lines_it_is_true_for(outcome: Any) -> None:
    invoices, lines = _table(outcome, "invoice"), _table(outcome, "invoice_line")
    held = _lines_of(invoices, lines)
    partial = [row for row in invoices if 0 < row["big_lines"] < len(held[row["invoice_id"]])]
    assert partial, "some invoice has lines the filter keeps and lines it drops"
    assert sum(row["big_lines"] for row in invoices) == sum(line["quantity"] >= 6 for line in lines)


def test_only_the_summing_plan_carries_sums_in_the_plan_file(bundle: Path, tmp_path: Path) -> None:
    emits = _planned(bundle, tmp_path / "summed")
    assert emits["invoice"]["generator"]["generatorDigest"].startswith("sha256:")
    for expose_id in ("product", "account", "invoice_line"):
        assert "sums" not in json.dumps(emits[expose_id]), expose_id


def _planned(bundle: Path, state: Path) -> Dict[str, Any]:
    planned = plan(bundle, state_dir=state, kernel="0.2.0", stage=False)
    assert planned.success, planned.stderr
    layout = RunLayout.for_state_dir(state, str(planned.artifacts["run_id"]))
    emits: Dict[str, Any] = json.loads(layout.plan_path.read_text("utf-8"))["emits"]
    return emits


def test_the_summed_plans_digest_is_in_the_parents_document(outcome: Any) -> None:
    parent, child = outcome.emit("invoice").plan, outcome.emit("invoice_line").plan
    assert parent.as_document()["sums"] == [
        {
            "expose": "invoice_line",
            "relationship": "invoice",
            "generatorDigest": parent.sums[0][2].digest().value,
        }
    ]
    assert "sums" not in child.as_document()
    # The child reads the parent as it was before its sums were attached.
    assert all(not read.sums for _, read in child.reads)


def test_a_moved_child_moves_the_parents_digest(bundle: Path, tmp_path: Path) -> None:
    moved = bundle_with(tmp_path, "moved")
    _edit(
        moved / "simulation.flux.yml",
        "quantity: { range: { min: '1', max: '10' } }",
        "quantity: { range: { min: '1', max: '11' } }",
    )
    before, after = _planned(bundle, tmp_path / "a"), _planned(moved, tmp_path / "b")
    assert before["invoice"]["generator"]["generatorDigest"] != (
        after["invoice"]["generator"]["generatorDigest"]
    )


def test_validate_reads_the_declaration(bundle: Path) -> None:
    assert validate(bundle).success


def _refused(bundle: Path, kernel: str = KERNEL_V2) -> FluxError:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=kernel)
    return caught.value


def test_a_relationship_with_no_counts_entry_is_refused_and_names_the_key(tmp_path: Path) -> None:
    refused = _refused(bundle_with(tmp_path, counted=False))
    assert refused.code.code == "FLX-6210" and "counts" in str(refused)
    assert "invoice_line.invoice" in str(refused)


def test_kernel_0_1_0_refuses_a_summary(bundle: Path) -> None:
    refused = _refused(bundle, KERNEL_V1)
    assert refused.code.code == "FLX-6210" and "0.2.0" in str(refused)


def _copy_of(column: str, parent_column: str) -> Dict[str, str]:
    """The line's *column*, derived as a copy of the invoice's *parent_column* through its link."""
    return {
        "line_columns": f'        - {{ name: {column}, type: "DECIMAL(14,2)", required: false }}\n',
        "line_invariants": "      invariants:\n"
        f"        - id: copy_{column}\n          derive: {column}\n"
        f"          expr: invoice.{parent_column}\n",
    }


def test_a_child_that_copies_the_total_back_is_a_cycle(tmp_path: Path) -> None:
    refused = _refused(bundle_with(tmp_path, **_copy_of("invoice_total", "total")))
    assert refused.code.code == "FLX-6208" and "invoice.total" in str(refused)


def test_a_child_that_reads_a_column_computed_from_a_total_is_a_cycle(tmp_path: Path) -> None:
    bundle = bundle_with(
        tmp_path,
        totals=_TOTALS
        + '        - { name: double_total, type: "DECIMAL(14,2)", required: true }\n',
        summaries=_SUMMARIES
        + '        - id: doubled\n          derive: double_total\n          expr: "ROUND(total * 2, 2)"\n',
        **_copy_of("twice", "double_total"),
    )
    refused = _refused(bundle)
    assert refused.code.code == "FLX-6208" and "invoice.double_total" in str(refused)


def test_a_child_that_reads_an_unrelated_parent_column_is_not_a_cycle(tmp_path: Path) -> None:
    bundle = bundle_with(
        tmp_path,
        line_columns="        - { name: issued, type: DATE, required: false }\n",
        line_invariants="      invariants:\n        - id: copy_issued\n          derive: issued\n"
        "          expr: invoice.issued_on\n",
    )
    outcome = pipeline.execute(bundle, kernel=KERNEL_V2)
    invoices = _table(outcome, "invoice")
    lines = _table(outcome, "invoice_line")
    by_id = {row["invoice_id"]: row for row in invoices}
    assert lines and all(line["issued"] == by_id[line["invoice_id"]]["issued_on"] for line in lines)
    assert _check_summaries(invoices, lines) == INVOICES


def test_a_sum_whose_scale_the_column_cannot_hold_is_refused_at_plan_time(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, name="scale")
    _edit(
        bundle / CONTRACT,
        '{ name: total, type: "DECIMAL(14,2)"',
        '{ name: total, type: "DECIMAL(14,0)"',
    )
    repin_contract(bundle / CONTRACT, bundle / "simulation.flux.yml", "retail.sales_chain")
    assert _refused(bundle).code.code == "FLX-6110"


def test_a_sum_that_overflows_its_column_is_refused_when_the_row_is_made(tmp_path: Path) -> None:
    bundle = bundle_with(tmp_path, name="overflow")
    _edit(
        bundle / CONTRACT,
        '{ name: total, type: "DECIMAL(14,2)"',
        '{ name: total, type: "DECIMAL(5,2)"',
    )
    repin_contract(bundle / CONTRACT, bundle / "simulation.flux.yml", "retail.sales_chain")
    refused = _refused(bundle)
    assert refused.code.code == "FLX-6112" and "total" in str(refused)


def test_a_filter_that_reads_another_row_is_refused(tmp_path: Path) -> None:
    bundle = bundle_with(
        tmp_path,
        summaries=_SUMMARIES.replace(
            'filter: "quantity >= 6" }', 'filter: "product.list_price > 100" }'
        ),
    )
    refused = _refused(bundle)
    assert refused.code.code == "FLX-6210" and "another row" in str(refused)


def test_a_column_the_child_does_not_hold_is_refused(tmp_path: Path) -> None:
    bundle = bundle_with(
        tmp_path, summaries=_SUMMARIES.replace("column: amount,", "column: cost,", 1)
    )
    assert _refused(bundle).code.code == "FLX-6113"


def test_the_per_row_path_writes_the_column_paths_rows(
    monkeypatch: pytest.MonkeyPatch, bundle: Path
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", 500)
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(bundle, kernel=KERNEL_V2)
    )
    for expose_id in ("invoice", "invoice_line"):
        assert by_columns.emit(expose_id).rows == by_rows.emit(expose_id).rows, expose_id
    assert taken and all(taken)


@pytest.mark.parametrize("count", [3, 7])
def test_the_shares_joined_are_the_whole_runs_bytes(bundle: Path, count: int) -> None:
    whole, parts = _shares(bundle, count)
    joined = b"".join(part.emit("invoice").writer.stream_bytes() for part in parts)
    assert joined == whole.emit("invoice").writer.stream_bytes()


def _digests(applied: Any) -> Dict[str, Any]:
    return {k: v for k, v in applied.artifacts.items() if "digest" in k.lower() and k != "run_id"}


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_jobs_do_not_move_the_digests_or_the_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(parallel, "MIN_ROWS_PER_JOB", 200)  # the invoices split across workers
    many = bundle_with(tmp_path, "many")
    runs = [pipeline.execute(many, kernel=KERNEL_V2, jobs=jobs) for jobs in (1, 4)]
    assert runs[1].emit("invoice").processes > 1
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
def test_a_scaled_run_holds_every_summary(bundle: Path, scale: str) -> None:
    scaled = pipeline.execute(bundle, kernel=KERNEL_V2, scale=Decimal(scale))
    invoices, lines = _table(scaled, "invoice"), _table(scaled, "invoice_line")
    assert len(invoices) == int(INVOICES * Decimal(scale))
    assert len(lines) == int(line_rows(LINES) * Decimal(scale))
    assert _check_summaries(invoices, lines) == len(invoices)
    assert any(not row["line_count"] for row in invoices)


def test_a_summary_over_a_conditioned_relationship_holds_and_ineligible_parents_have_none(
    tmp_path: Path,
) -> None:
    target = bundle_with(tmp_path, "conditioned", parent_when="status <> 'void'")
    outcome = pipeline.execute(target, kernel=KERNEL_V2)
    invoices, lines = _table(outcome, "invoice"), _table(outcome, "invoice_line")
    assert _check_summaries(invoices, lines) == INVOICES
    held = _lines_of(invoices, lines)
    void = [row for row in invoices if row["status"] == "void"]
    assert void and all(not held[row["invoice_id"]] for row in void)
    for row in void:
        assert row["line_count"] == 0 and row["total"] == Decimal("0.00")
        assert row["max_qty"] is None and row["discount_total"] is None
    assert any(held[row["invoice_id"]] for row in invoices if row["status"] != "void")


def test_a_condition_that_reads_a_column_a_summary_sets_is_refused(tmp_path: Path) -> None:
    target = bundle_with(tmp_path, "cycle", parent_when="line_count > 0")
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-3403" and "parentWhen" in str(caught.value)
