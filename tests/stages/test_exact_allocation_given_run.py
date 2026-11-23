# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Exact allocation of a column given an exact column end to end (ADR 0016 section 6).

KERNEL-SPEC-2 section 8.3: the rows of each parent level are the slots of that level's class, and
the column's own counts and permutation over those rows give each row its class, so each pair of a
parent level and a child level holds exactly its share. The bundle is ``tests/fixtures/exact-counts``:

* ``account.status`` is drawn given ``account.segment``, both exact (300 rows, three groups);
* ``product.list_price`` is a histogram drawn given ``product.category``, both exact (26 rows).

What is pinned, on kernel 0.2.0: the pair counts, the column path against the per-row path, a chunked
pass, ``--jobs``, ``--shard`` and ``--scale``, and the refusals of a parent that is not exact.
"""

from __future__ import annotations

import shutil
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.generator import columns
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from tests.generator.test_rules_over_links import _by_both_paths

from .conftest import REPO_ROOT

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "exact-counts"
SEGMENT = "segment: { levels: { retail: '0.6', trade: '0.3', public: '0.1' } }"
STATUS = "status: { levels: { active: '0.8', closed: '0.2' } }"
GIVEN = (
    "{ retail: { active: '0.9', closed: '0.1' }, trade: { active: '0.8', closed: '0.2' }, "
    "public: { active: '0.5', closed: '0.5' } }"
)
CATEGORY = "category: { levels: { tools: '0.4', toys: '0.3', books: '0.2', games: '0.1' } }"
LIST_PRICE = "list_price: { range: { min: '1', max: '500' } }"
EDGES = ["1", "100", "250", "500"]
BIN_GIVEN = (
    "{ books: ['0.6', '0.2', '0.2'], games: ['0.4', '0.3', '0.3'], "
    "tools: ['0.5', '0.3', '0.2'], toys: ['0.25', '0.25', '0.5'] }"
)


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


def levels_bundle(
    tmp_path: Path,
    name: str,
    *,
    parent: bool = True,
    child: bool = True,
    naccounts: Optional[int] = None,
    table: str = GIVEN,
) -> Path:
    """``account.status`` given ``account.segment``, each exact when asked."""
    segment = SEGMENT[: -len(" }")] + (", allocation: exact }" if parent else " }")
    status = f"status: {{ given: segment, levelWeightsGiven: {table}" + (
        ", allocation: exact }" if child else " }"
    )
    replacements = {SEGMENT: segment, STATUS: status}
    if naccounts is not None:
        replacements["        rows: 300\n"] = f"        rows: {naccounts}\n"
        replacements["account_id: { range: { min: '1', max: '300' } }"] = (
            f"account_id: {{ range: {{ min: '1', max: '{naccounts}' }} }}"
        )
    return _edited(tmp_path, name, replacements)


def bins_bundle(tmp_path: Path, name: str, *, parent: bool = True, nproducts: int = 26) -> Path:
    """``product.list_price`` as a histogram given ``product.category``, each exact when asked."""
    category = CATEGORY[: -len(" }")] + (", allocation: exact }" if parent else " }")
    price = (
        f"list_price: {{ range: {{ min: '1', max: '500' }}, bins: {EDGES}, given: category, "
        f"binWeightsGiven: {BIN_GIVEN}, allocation: exact }}"
    )
    replacements = {CATEGORY: category, LIST_PRICE: price}
    if nproducts != 26:
        replacements["        rows: 26\n"] = f"        rows: {nproducts}\n"
        replacements["product_id: { range: { min: '1', max: '26' } }"] = (
            f"product_id: {{ range: {{ min: '1', max: '{nproducts}' }} }}"
        )
    return _edited(tmp_path, name, replacements)


def _pairs(outcome: Any, expose: str, parent: str, child: str) -> Dict[Tuple[Any, Any], int]:
    emit = outcome.emit(expose)
    names = list(emit.expose.column_names)
    return dict(Counter((row[names.index(parent)], row[names.index(child)]) for row in emit.rows))


def _bin_of(price: Decimal) -> int:
    return sum(1 for edge in EDGES[1:-1] if price >= Decimal(edge))


def _statuses(outcome: Any) -> Dict[Tuple[Any, Any], int]:
    return _pairs(outcome, "account", "segment", "status")


def _prices(outcome: Any) -> Dict[Tuple[Any, Any], int]:
    emit = outcome.emit("product")
    names = list(emit.expose.column_names)
    return dict(
        Counter(
            (row[names.index("category")], _bin_of(row[names.index("list_price")]))
            for row in emit.rows
        )
    )


#: 300 accounts over 180 retail, 90 trade and 30 public rows; each group split by its own weights.
STATUS_COUNTS = {
    ("retail", "active"): 162,
    ("retail", "closed"): 18,
    ("trade", "active"): 72,
    ("trade", "closed"): 18,
    ("public", "active"): 15,
    ("public", "closed"): 15,
}
#: 26 products over books 5, games 3, tools 10 and toys 8 rows, each group split by its bin weights.
PRICE_COUNTS = {
    ("books", 0): 3,
    ("books", 1): 1,
    ("books", 2): 1,
    ("games", 0): 1,
    ("games", 1): 1,
    ("games", 2): 1,
    ("tools", 0): 5,
    ("tools", 1): 3,
    ("tools", 2): 2,
    ("toys", 0): 2,
    ("toys", 1): 2,
    ("toys", 2): 4,
}


def test_each_pair_of_a_parent_level_and_a_level_holds_its_share(tmp_path: Path) -> None:
    outcome = pipeline.execute(levels_bundle(tmp_path, "levels"), kernel=KERNEL_V2)
    assert _statuses(outcome) == STATUS_COUNTS


def test_each_pair_of_a_parent_level_and_a_bin_holds_its_share(tmp_path: Path) -> None:
    outcome = pipeline.execute(bins_bundle(tmp_path, "bins"), kernel=KERNEL_V2)
    assert _prices(outcome) == PRICE_COUNTS


@pytest.mark.parametrize("chunk", [4096, 7])
def test_the_column_path_equals_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    for target, expose in (
        (levels_bundle(tmp_path, "paths-levels"), "account"),
        (bins_bundle(tmp_path, "paths-bins", nproducts=300), "product"),
    ):
        by_columns, by_rows, taken = _by_both_paths(
            monkeypatch, lambda target=target: pipeline.execute(target, kernel=KERNEL_V2)
        )
        assert by_columns.emit(expose).rows == by_rows.emit(expose).rows, expose
        assert taken and all(taken)


@pytest.mark.parametrize("count", [3, 5])
def test_the_shares_joined_are_the_whole_runs_rows_and_counts(tmp_path: Path, count: int) -> None:
    for target, expose in (
        (levels_bundle(tmp_path, "shards-levels"), "account"),
        (bins_bundle(tmp_path, "shards-bins"), "product"),
    ):
        # Under a shard the counts are of the run's rows: the shares joined are the whole run.
        whole = pipeline.execute(target, kernel=KERNEL_V2)
        parts = [
            pipeline.execute(target, kernel=KERNEL_V2, shard=(i, count))
            for i in range(1, count + 1)
        ]
        joined = [tuple(row) for part in parts for row in part.emit(expose).rows]
        assert joined == [tuple(row) for row in whole.emit(expose).rows], expose


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_jobs_do_not_move_the_rows_or_the_counts(tmp_path: Path) -> None:
    many = levels_bundle(tmp_path, "many", naccounts=9000)
    runs = [pipeline.execute(many, kernel=KERNEL_V2, jobs=jobs) for jobs in (1, 4)]
    assert runs[1].emit("account").processes > 1
    assert (
        runs[0].emit("account").writer.stream_bytes()
        == runs[1].emit("account").writer.stream_bytes()
    )
    assert _statuses(runs[0]) == {
        ("retail", "active"): 4860,
        ("retail", "closed"): 540,
        ("trade", "active"): 2160,
        ("trade", "closed"): 540,
        ("public", "active"): 450,
        ("public", "closed"): 450,
    }


def test_a_scaled_run_is_exact_at_the_scaled_rows(tmp_path: Path) -> None:
    half = pipeline.execute(levels_bundle(tmp_path, "half"), kernel=KERNEL_V2, scale=Decimal("0.5"))
    assert _statuses(half) == {
        ("retail", "active"): 81,
        ("retail", "closed"): 9,
        ("trade", "active"): 36,
        ("trade", "closed"): 9,
        ("public", "active"): 8,
        ("public", "closed"): 7,
    }
    double = pipeline.execute(
        levels_bundle(tmp_path, "double"), kernel=KERNEL_V2, scale=Decimal("2")
    )
    assert sum(_statuses(double).values()) == 600
    small = pipeline.execute(bins_bundle(tmp_path, "small"), kernel=KERNEL_V2, scale=Decimal("0.5"))
    assert sum(_prices(small).values()) == 13


def _refused(target: Path, kernel: Any = KERNEL_V2) -> FluxError:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=kernel)
    return caught.value


def test_kernel_0_1_0_refuses_the_key_and_names_the_kernel(tmp_path: Path) -> None:
    refusal = _refused(levels_bundle(tmp_path, "old"), KERNEL_V1)
    assert refusal.code.code == "FLX-3105" and "0.2.0" in str(refusal)


def test_a_column_given_a_column_that_is_not_exact_is_refused(tmp_path: Path) -> None:
    refusal = _refused(levels_bundle(tmp_path, "plain-parent", parent=False))
    assert refusal.code.code == "FLX-3402" and "itself exact" in str(refusal)


def test_a_column_given_a_linked_rows_column_is_refused(tmp_path: Path) -> None:
    table = "".join(
        f"{level}: {{ paid: '1', open: '0', void: '0' }}, "
        for level in ("retail", "trade", "public")
    )
    old = "status: { levels: { paid: '0.7', open: '0.25', void: '0.05' } }"
    new = (
        f"status: {{ given: account.segment, levelWeightsGiven: {{ {table} }}, allocation: exact }}"
    )
    refusal = _refused(_edited(tmp_path, "linked", {old: new}))
    assert refusal.code.code == "FLX-3402" and "linked" in str(refusal)


NULLABLE_SEGMENT_DIGEST = "sha256:bef99c9f78b2be2eb20a8335c14512c1e65e9ea232a04889b7cefc45ce6a067c"


def test_a_column_given_an_exact_column_with_a_null_fraction_is_refused(tmp_path: Path) -> None:
    """A null has no level to condition on, so the exact path refuses as the drawn path does."""
    target = levels_bundle(tmp_path, "null-parent")
    contract = target / "sales-chain.fluid.yml"
    old = "{ name: segment, type: STRING, required: true }"
    assert contract.read_text("utf-8").count(old) == 1
    contract.write_text(
        contract.read_text("utf-8").replace(old, "{ name: segment, type: STRING }"), "utf-8"
    )
    simulation = target / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    declared = "segment: { levels: { retail: '0.6', trade: '0.3', public: '0.1' }, "
    assert text.count(declared) == 1
    simulation.write_text(
        text.replace(declared, declared + "nullFraction: '0.1', ").replace(
            "sha256:8ab49a74f25b18de2c0ec299e23606b3d6a9d6cbd0b7ff8ee2fedb1168bbb005",
            NULLABLE_SEGMENT_DIGEST,
        ),
        "utf-8",
    )
    refusal = _refused(target)
    assert refusal.code.code == "FLX-3402" and "nullFraction" in str(refusal)
