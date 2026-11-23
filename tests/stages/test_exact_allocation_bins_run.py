# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Exact allocation of bins end to end (ADR 0016 section 6, KERNEL-SPEC-2 section 8.3).

The bundle is ``tests/fixtures/exact-counts`` with ``allocation: exact`` on ``product.list_price``,
drawn from three bins over the 26 rows of the table. What is pinned, on kernel 0.2.0:

* **Exact counts.** Each bin holds exactly its Largest Remainder count, a ``nullFraction`` adds a
  NULL class that is exact too, and ``--scale`` is exact at the scaled rows.
* **Inside a bin.** The value is the grid draw the column always made, at the index it had.
* **Two paths, one answer.** The column path's rows equal the per-row path's, a chunked pass equals
  one pass, ``--jobs`` does not move the digests, and the shares of ``--shard`` joined are the run.
* **Refusals.** Kernel 0.1.0 names the kernel; a histogram with no weights is refused.
"""

from __future__ import annotations

import shutil
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.core.seedtree import derive_root
from flux_engine.generator import columns, rows
from flux_engine.generator.paths import CellStreams
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from tests.generator.test_rules_over_links import _by_both_paths

from .conftest import REPO_ROOT

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "exact-counts"
LIST_PRICE = "list_price: { range: { min: '1', max: '500' } }"
PRODUCT_ID = "product_id: { range: { min: '1', max: '26' } }"
EDGES = ["1", "100", "250", "500"]
CONTRACT_DIGEST = "sha256:8ab49a74f25b18de2c0ec299e23606b3d6a9d6cbd0b7ff8ee2fedb1168bbb005"
NULLABLE_CONTRACT_DIGEST = "sha256:1995566e8fbe9f77474e7fc2b689bfc69440bff57855fc1c979d798e5e751d66"


def _edited(tmp_path: Path, name: str, replacements: Dict[str, str]) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    path = target / "simulation.flux.yml"
    text = path.read_text("utf-8")
    for old, new in replacements.items():
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    path.write_text(text, "utf-8")
    return target


def bundle_with(
    tmp_path: Path,
    name: str,
    *,
    allocation: Optional[str] = "exact",
    weights: Optional[str] = "['0.5', '0.3', '0.2']",
    null: Optional[str] = None,
    nrows: Optional[int] = None,
) -> Path:
    """A copy of the fixture whose ``product.list_price`` is a histogram declaring *allocation*."""
    declared = f", nullFraction: '{null}'" if null else ""
    spelled = f", allocation: {allocation}" if allocation else ""
    given = f", binWeights: {weights}" if weights else ""
    column = f"list_price: {{ range: {{ min: '1', max: '500' }}, bins: {EDGES}{given}{declared}{spelled} }}"
    replacements = {LIST_PRICE: column}
    if nrows is not None:
        replacements["        rows: 26\n"] = f"        rows: {nrows}\n"
        replacements[PRODUCT_ID] = f"product_id: {{ range: {{ min: '1', max: '{nrows}' }} }}"
    target = _edited(tmp_path, name, replacements)
    if null:
        contract = target / "sales-chain.fluid.yml"
        old = '{ name: list_price, type: "DECIMAL(10,2)", required: true }'
        text = contract.read_text("utf-8")
        assert text.count(old) == 1
        contract.write_text(
            text.replace(old, '{ name: list_price, type: "DECIMAL(10,2)" }'), "utf-8"
        )
        simulation = target / "simulation.flux.yml"
        simulation.write_text(
            simulation.read_text("utf-8").replace(CONTRACT_DIGEST, NULLABLE_CONTRACT_DIGEST),
            "utf-8",
        )
    return target


def _prices(outcome: Any) -> List[Optional[Decimal]]:
    emit = outcome.emit("product")
    position = list(emit.expose.column_names).index("list_price")
    return [row[position] for row in emit.rows]


def _bin_of(price: Optional[Decimal]) -> Optional[int]:
    if price is None:
        return None
    return sum(1 for edge in EDGES[1:-1] if price >= Decimal(edge))


def _bins(outcome: Any) -> Dict[Optional[int], int]:
    return dict(Counter(_bin_of(price) for price in _prices(outcome)))


#: Over 26 rows the weights 0.5, 0.3 and 0.2 are 13, 7.8 and 5.2: the floors total 25 and the bin
#: of the largest remainder, the second, takes the spare row.
PLAIN_COUNTS = {0: 13, 1: 8, 2: 5}
#: With a nullFraction of 0.1 the weights are 0.45, 0.27, 0.18 and 0.1: 11.7, 7.02, 4.68 and 2.6.
#: The floors total 24; the first and the third bin take the two spare rows.
NULLED_COUNTS = {0: 12, 1: 7, 2: 5, None: 2}


def test_each_bin_holds_exactly_its_largest_remainder_count(tmp_path: Path) -> None:
    outcome = pipeline.execute(bundle_with(tmp_path, "exact"), kernel=KERNEL_V2)
    assert _bins(outcome) == PLAIN_COUNTS


def test_the_null_class_is_exact_too(tmp_path: Path) -> None:
    outcome = pipeline.execute(bundle_with(tmp_path, "null", null="0.1"), kernel=KERNEL_V2)
    assert _bins(outcome) == NULLED_COUNTS


def test_without_allocation_the_counts_are_drawn_and_not_these(tmp_path: Path) -> None:
    plain = pipeline.execute(bundle_with(tmp_path, "plain", allocation=None), kernel=KERNEL_V2)
    assert _bins(plain) != PLAIN_COUNTS
    assert "allocation" not in str(plain.emit("product").plan.as_document())


@pytest.mark.parametrize("null", [None, "0.1"])
def test_the_column_path_equals_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, null: Optional[str]
) -> None:
    target = bundle_with(tmp_path, "paths", null=null, nrows=300)
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(target, kernel=KERNEL_V2)
    )
    assert by_columns.emit("product").rows == by_rows.emit("product").rows
    assert taken and all(taken)


@pytest.mark.parametrize("chunk", [4096, 7])
def test_a_chunked_pass_gives_the_rows_of_one_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, chunk: int
) -> None:
    target = bundle_with(tmp_path, "chunks", null="0.1", nrows=300)
    whole = pipeline.execute(target, kernel=KERNEL_V2)
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    assert pipeline.execute(target, kernel=KERNEL_V2).emit("product").rows == (
        whole.emit("product").rows
    )


def test_the_value_inside_a_bin_is_the_draw_the_column_always_made(tmp_path: Path) -> None:
    """Forcing the bin a plain draw chose changes nothing: the grid draw keeps its index."""
    outcome = pipeline.execute(bundle_with(tmp_path, "inside", nrows=50), kernel=KERNEL_V2)
    plan = outcome.emit("product").plan
    column_plan = replace(plan.column_plan("list_price"), allocated_rows=None)
    root = derive_root(b"\x01" * 32, 7, seed_tree_version=kernel2.SEED_TREE_VERSION)
    streams = CellStreams(root, plan.sim_id, plan.expose.expose_id)
    for row in range(20):
        plain = rows._draw_value(column_plan, streams.stream_at(row, "list_price"))
        forced = rows._draw_value(
            column_plan, streams.stream_at(row, "list_price"), None, _bin_of(plain)
        )
        assert forced == plain
        other = rows._draw_value(
            column_plan, streams.stream_at(row, "list_price"), None, (_bin_of(plain) + 1) % 3
        )
        assert _bin_of(other) == (_bin_of(plain) + 1) % 3


@pytest.mark.parametrize("count", [3, 5])
def test_the_shares_joined_are_the_whole_runs_rows_and_counts(tmp_path: Path, count: int) -> None:
    target = bundle_with(tmp_path, "shards", null="0.1", nrows=300)
    whole = pipeline.execute(target, kernel=KERNEL_V2)
    parts = [
        pipeline.execute(target, kernel=KERNEL_V2, shard=(i, count)) for i in range(1, count + 1)
    ]
    joined = [tuple(row) for part in parts for row in part.emit("product").rows]
    assert joined == [tuple(row) for row in whole.emit("product").rows]
    assert _bins(whole) == {0: 135, 1: 81, 2: 54, None: 30}


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_jobs_do_not_move_the_rows_or_the_counts(tmp_path: Path) -> None:
    many = bundle_with(tmp_path, "many", null="0.1", nrows=9000)
    runs = [pipeline.execute(many, kernel=KERNEL_V2, jobs=jobs) for jobs in (1, 4)]
    assert runs[1].emit("product").processes > 1
    assert (
        runs[0].emit("product").writer.stream_bytes()
        == runs[1].emit("product").writer.stream_bytes()
    )
    assert _bins(runs[0]) == {0: 4050, 1: 2430, 2: 1620, None: 900}


@pytest.mark.parametrize(
    "scale, expected",
    [("0.5", {0: 6, 1: 4, 2: 3}), ("2", {0: 26, 1: 16, 2: 10})],
)
def test_a_scaled_run_is_exact_at_the_scaled_rows(
    tmp_path: Path, scale: str, expected: Dict[int, int]
) -> None:
    outcome = pipeline.execute(
        bundle_with(tmp_path, "scaled"), kernel=KERNEL_V2, scale=Decimal(scale)
    )
    assert _bins(outcome) == expected


def _refused(target: Path, kernel: Any = KERNEL_V2) -> FluxError:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=kernel)
    return caught.value


def test_kernel_0_1_0_refuses_the_key_and_names_the_kernel(tmp_path: Path) -> None:
    refusal = _refused(bundle_with(tmp_path, "old"), KERNEL_V1)
    assert refusal.code.code == "FLX-3105" and "0.2.0" in str(refusal)


def test_a_histogram_with_no_weights_is_refused(tmp_path: Path) -> None:
    refusal = _refused(bundle_with(tmp_path, "unweighted", weights=None))
    assert refusal.code.code == "FLX-3402" and "binWeights" in str(refusal)
