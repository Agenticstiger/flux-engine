# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Exact allocation of levels end to end (ADR 0016 section 6, KERNEL-SPEC-2 section 8.3).

The bundle is ``tests/fixtures/exact-counts`` with ``allocation: exact`` on ``product.category``,
which holds 26 rows over four levels. What is pinned, on kernel 0.2.0:

* **Exact counts.** Each level holds exactly its Largest Remainder count, a ``nullFraction`` adds a
  NULL class that is exact too, and ``--scale`` is exact at the scaled rows.
* **Two paths, one answer.** The column path's rows equal the per-row path's, ``--jobs`` does not
  move the digests, and a different seed moves which rows take which level and no count.
* **Refusals.** Kernel 0.1.0 names the kernel; a key
  column, a column of a family that cannot be allotted and a value other than ``exact`` are refused.
* **Additive.** A plan that declares no ``allocation`` has the ``generatorDigest`` it always had.
"""

from __future__ import annotations

import shutil
from collections import Counter
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
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from tests.generator.test_rules_over_links import _by_both_paths

from .conftest import REPO_ROOT

pytestmark = pytest.mark.integration

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "exact-counts"
CATEGORY = "category: { levels: { tools: '0.4', toys: '0.3', books: '0.2', games: '0.1' } }"
PRODUCT_ID = "product_id: { range: { min: '1', max: '26' } }"
LIST_PRICE = "list_price: { range: { min: '1', max: '500' } }"
#: What a product plan that declares no ``allocation`` has always digested to.
#: The contract's digest as the fixture pins it, and as it is once ``category`` may be NULL.
CONTRACT_DIGEST = "sha256:8ab49a74f25b18de2c0ec299e23606b3d6a9d6cbd0b7ff8ee2fedb1168bbb005"
NULLABLE_CONTRACT_DIGEST = "sha256:b46ee8697c76e39d0ac6751e9c2af1327ccfb57ec141977bdc9e19e1c67293d7"
PLAIN_PRODUCT_DIGEST = "sha256:bc9f7cd786dd2a2169e6f5d8d4c1494a5ccceccc3dae3359defdc9b27af56b8f"


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


def _nullable_category(target: Path) -> None:
    """Let ``product.category`` be NULL: the contract marks it required, which a nullFraction refuses."""
    contract = target / "sales-chain.fluid.yml"
    old = "{ name: category, type: STRING, required: true }"
    text = contract.read_text("utf-8")
    assert text.count(old) == 1
    contract.write_text(text.replace(old, "{ name: category, type: STRING }"), "utf-8")
    simulation = target / "simulation.flux.yml"
    simulation.write_text(
        simulation.read_text("utf-8").replace(CONTRACT_DIGEST, NULLABLE_CONTRACT_DIGEST), "utf-8"
    )


def bundle_with(
    tmp_path: Path,
    name: str,
    *,
    allocation: Optional[str] = "exact",
    null: Optional[str] = None,
    rows: Optional[int] = None,
    seed: Optional[int] = None,
) -> Path:
    """A copy of the fixture whose ``product.category`` declares *allocation*."""
    declared = f", nullFraction: '{null}'" if null else ""
    spelled = f", allocation: {allocation}" if allocation else ""
    levels = CATEGORY[: -len(" }")] + f"{declared}{spelled} }}"
    replacements = {CATEGORY: levels}
    if rows is not None:
        replacements["        rows: 26\n"] = f"        rows: {rows}\n"
        replacements[PRODUCT_ID] = f"product_id: {{ range: {{ min: '1', max: '{rows}' }} }}"
    if seed is not None:
        replacements["  seed: 42\n"] = f"  seed: {seed}\n"
    target = _edited(tmp_path, name, replacements)
    if null:
        _nullable_category(target)
    return target


def _categories(outcome: Any) -> List[Optional[str]]:
    emit = outcome.emit("product")
    position = list(emit.expose.column_names).index("category")
    return [row[position] for row in emit.rows]


#: Over 26 rows, the weights books 0.2, games 0.1, tools 0.4, toys 0.3 (NFC order) are 5.2, 2.6,
#: 10.4 and 7.8: the floors total 24 and games and toys have the largest remainders.
PLAIN_COUNTS = {"books": 5, "games": 3, "tools": 10, "toys": 8}
#: With a nullFraction of 0.1 the weights are 0.18, 0.09, 0.36, 0.27 and 0.1 for NULL: 4.68, 2.34,
#: 9.36, 7.02 and 2.6 over 26 rows. The floors total 24; books and NULL take the two spare rows.
NULLED_COUNTS = {"books": 5, "games": 2, "tools": 9, "toys": 7, None: 3}


def test_each_level_holds_exactly_its_largest_remainder_count(tmp_path: Path) -> None:
    outcome = pipeline.execute(bundle_with(tmp_path, "exact"), kernel=KERNEL_V2)
    assert dict(Counter(_categories(outcome))) == PLAIN_COUNTS


def test_the_null_class_is_exact_too(tmp_path: Path) -> None:
    outcome = pipeline.execute(bundle_with(tmp_path, "null", null="0.1"), kernel=KERNEL_V2)
    assert dict(Counter(_categories(outcome))) == NULLED_COUNTS


def test_without_allocation_the_counts_are_drawn_and_not_these(tmp_path: Path) -> None:
    plain = pipeline.execute(bundle_with(tmp_path, "plain", allocation=None), kernel=KERNEL_V2)
    assert dict(Counter(_categories(plain))) != PLAIN_COUNTS


@pytest.mark.parametrize("null", [None, "0.1"])
def test_the_column_path_equals_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, null: Optional[str]
) -> None:
    target = bundle_with(tmp_path, "paths", null=null, rows=300)
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(target, kernel=KERNEL_V2)
    )
    assert by_columns.emit("product").rows == by_rows.emit("product").rows
    assert taken and all(taken)


@pytest.mark.parametrize("chunk", [4096, 7])
def test_a_chunked_pass_gives_the_rows_of_one_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, chunk: int
) -> None:
    target = bundle_with(tmp_path, "chunks", null="0.1", rows=300)
    whole = pipeline.execute(target, kernel=KERNEL_V2)
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    assert pipeline.execute(target, kernel=KERNEL_V2).emit("product").rows == (
        whole.emit("product").rows
    )


def _digests(applied: Any) -> Dict[str, Any]:
    return {k: v for k, v in applied.artifacts.items() if "digest" in k.lower() and k != "run_id"}


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_jobs_do_not_move_the_digests_or_the_counts(tmp_path: Path) -> None:
    many = bundle_with(tmp_path, "many", null="0.1", rows=9000)
    runs = [pipeline.execute(many, kernel=KERNEL_V2, jobs=jobs) for jobs in (1, 4)]
    assert runs[1].emit("product").processes > 1
    assert (
        runs[0].emit("product").writer.stream_bytes()
        == runs[1].emit("product").writer.stream_bytes()
    )
    counts = Counter(_categories(runs[0]))
    assert counts == {"books": 1620, "games": 810, "tools": 3240, "toys": 2430, None: 900}
    found = []
    for jobs in (1, 4):
        state = tmp_path / f"state-{jobs}"
        planned = plan(many, state_dir=state, jobs=jobs, kernel="0.2.0")
        assert planned.success, planned.stderr
        applied = apply(many, state_dir=state, run_id=str(planned.artifacts["run_id"]), jobs=jobs)
        assert applied.success, applied.stderr
        found.append((planned.artifacts.get("plan_digest"), _digests(applied)))
    assert found[0] == found[1]


@pytest.mark.parametrize(
    "scale, expected",
    [
        ("0.5", {"books": 3, "games": 1, "tools": 5, "toys": 4}),
        ("2", {"books": 10, "games": 5, "tools": 21, "toys": 16}),
    ],
)
def test_a_scaled_run_is_exact_at_the_scaled_rows(
    tmp_path: Path, scale: str, expected: Dict[str, int]
) -> None:
    outcome = pipeline.execute(
        bundle_with(tmp_path, "scaled"), kernel=KERNEL_V2, scale=Decimal(scale)
    )
    assert dict(Counter(_categories(outcome))) == expected


def test_a_different_seed_moves_the_rows_and_no_count(tmp_path: Path) -> None:
    first = pipeline.execute(bundle_with(tmp_path, "s1", rows=300), kernel=KERNEL_V2)
    second = pipeline.execute(bundle_with(tmp_path, "s2", rows=300, seed=7), kernel=KERNEL_V2)
    assert _categories(first) != _categories(second)
    assert Counter(_categories(first)) == Counter(_categories(second))


def test_a_plan_that_declares_no_allocation_keeps_its_generator_digest(tmp_path: Path) -> None:
    plain = pipeline.execute(bundle_with(tmp_path, "plain", allocation=None), kernel=KERNEL_V2)
    exact = pipeline.execute(bundle_with(tmp_path, "exact"), kernel=KERNEL_V2)
    assert plain.emit("product").plan.digest().value == PLAIN_PRODUCT_DIGEST
    assert exact.emit("product").plan.digest().value != PLAIN_PRODUCT_DIGEST
    assert "allocation" not in str(plain.emit("product").plan.as_document())


def _refused(target: Path, kernel: Any = KERNEL_V2) -> FluxError:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=kernel)
    return caught.value


def test_kernel_0_1_0_refuses_the_key_and_names_the_kernel(tmp_path: Path) -> None:
    refusal = _refused(bundle_with(tmp_path, "old"), KERNEL_V1)
    assert refusal.code.code == "FLX-3105" and "0.2.0" in str(refusal)


def test_a_value_other_than_exact_is_refused(tmp_path: Path) -> None:
    refusal = _refused(bundle_with(tmp_path, "even", allocation="even"))
    assert refusal.code.code in ("FLX-3105", "FLX-5030") and "even" in str(refusal)


def test_a_key_column_and_a_family_that_cannot_be_allotted_are_refused(tmp_path: Path) -> None:
    for name, old, new in (
        ("key", PRODUCT_ID, "product_id: { range: { min: '1', max: '26' }, allocation: exact }"),
        ("range", LIST_PRICE, "list_price: { range: { min: '1', max: '500' }, allocation: exact }"),
    ):
        refusal = _refused(_edited(tmp_path, name, {old: new}))
        assert refusal.code.code == "FLX-3402", name


@pytest.mark.parametrize("count", [3, 5])
def test_the_shares_joined_are_the_whole_runs_rows_and_counts(tmp_path: Path, count: int) -> None:
    target = bundle_with(tmp_path, "shards", null="0.1", rows=300)
    whole = pipeline.execute(target, kernel=KERNEL_V2)
    parts = [
        pipeline.execute(target, kernel=KERNEL_V2, shard=(i, count)) for i in range(1, count + 1)
    ]
    joined = [tuple(row) for part in parts for row in part.emit("product").rows]
    assert joined == [tuple(row) for row in whole.emit("product").rows]
    assert Counter(_categories(whole)) == {
        "books": 54,
        "games": 27,
        "tools": 108,
        "toys": 81,
        None: 30,
    }


def test_a_column_given_an_exact_column_follows_the_parents_allocated_level(tmp_path: Path) -> None:
    """A column that is not exact reads the level its parent holds, not a level the parent drew."""
    table = (
        "{ retail: { active: '1', closed: '0' }, trade: { active: '0', closed: '1' }, "
        "public: { active: '1', closed: '0' } }"
    )
    target = _edited(
        tmp_path,
        "follow",
        {
            "segment: { levels: { retail: '0.6', trade: '0.3', public: '0.1' } }": (
                "segment: { levels: { retail: '0.6', trade: '0.3', public: '0.1' }, "
                "allocation: exact }"
            ),
            "status: { levels: { active: '0.8', closed: '0.2' } }": (
                f"status: {{ given: segment, levelWeightsGiven: {table} }}"
            ),
        },
    )
    outcome = pipeline.execute(target, kernel=KERNEL_V2)
    emit = outcome.emit("account")
    names = list(emit.expose.column_names)
    held = Counter((row[names.index("segment")], row[names.index("status")]) for row in emit.rows)
    assert dict(held) == {
        ("retail", "active"): 180,
        ("trade", "closed"): 90,
        ("public", "active"): 30,
    }


def test_the_given_child_of_an_exact_column_is_the_same_on_both_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    table = "{ retail: { active: '0.7', closed: '0.3' }, trade: { active: '0.5', closed: '0.5' }, public: { active: '0.1', closed: '0.9' } }"
    target = _edited(
        tmp_path,
        "follow-paths",
        {
            "segment: { levels: { retail: '0.6', trade: '0.3', public: '0.1' } }": (
                "segment: { levels: { retail: '0.6', trade: '0.3', public: '0.1' }, "
                "allocation: exact }"
            ),
            "status: { levels: { active: '0.8', closed: '0.2' } }": (
                f"status: {{ given: segment, levelWeightsGiven: {table} }}"
            ),
        },
    )
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: pipeline.execute(target, kernel=KERNEL_V2)
    )
    assert by_columns.emit("account").rows == by_rows.emit("account").rows
    assert taken and all(taken)


class _Spying(CellStreams):
    """The generator's cell streams, keeping each stream a cell was handed."""

    seen: List[Any]

    def stream_at(self, row: int, column: str) -> Any:
        stream = super().stream_at(row, column)
        self.seen.append(stream)
        return stream


@pytest.mark.parametrize("null, draws", [(None, 1), ("0.1", 2)])
def test_a_cell_makes_its_draws_and_leaves_them_unread(
    tmp_path: Path, null: Optional[str], draws: int
) -> None:
    """The null draw and the class draw are made, so a later draw on the stream is the word it was."""
    outcome = pipeline.execute(bundle_with(tmp_path, "draws", null=null), kernel=KERNEL_V2)
    plan = outcome.emit("product").plan
    root = derive_root(b"\x01" * 32, 7, seed_tree_version=kernel2.SEED_TREE_VERSION)
    streams = _Spying(root, plan.sim_id, plan.expose.expose_id)
    streams.seen = []
    for row in range(5):
        rows._cell(
            plan.column_plan("category"), streams, plan.sim_id, "product", row, plan.seam_digest
        )
    assert [stream.position for stream in streams.seen] == [draws] * 5


def test_a_categorical_column_declared_by_a_support_only_takes_equal_shares(tmp_path: Path) -> None:
    """With no weights, the classes are the declared support's levels at equal weights."""
    target = _edited(
        tmp_path,
        "support",
        {
            CATEGORY: (
                "category: { support: { values: ['tools', 'toys', 'books', 'games'] }, "
                "maxCardinality: 4, allocation: exact }"
            )
        },
    )
    outcome = pipeline.execute(target, kernel=KERNEL_V2)
    # 26 rows over four equal classes are 6.5 each: the largest remainders go to the lower classes
    # in NFC order, books and games.
    assert dict(Counter(_categories(outcome))) == {"books": 7, "games": 7, "tools": 6, "toys": 6}
