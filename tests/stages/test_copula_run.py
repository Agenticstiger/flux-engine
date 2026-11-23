# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copula's rows end to end (ADR 0035 decision 6): equivalence, jobs, shards, scale and tau.

The bundle is ``tests/fixtures/copula``: one table of accounts whose five number, date and
timestamp columns are histograms. Every run is on kernel 0.2.0. A copula column draws its value
from the row's copula stream and its own cell stream draws only its NULL draw, so each test holds
what that path promises: the column path equals the per-row path, the rows of a slice are the
whole run's, ``--jobs`` and ``--shard`` change no byte, ``--scale`` changes only the count, and the
empirical Kendall tau of drawn rows is the declared one.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Sequence

import numpy as np
import pytest

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator import columns
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from tests.generator.test_rules_over_links import _by_both_paths
from tests.seam.test_copula_block import bundle_with

pytestmark = pytest.mark.integration

FIVE = (
    "copula:\n"
    "  columns: [amount, balance, opened_at, closed_on, limit_raised]\n"
    "  rankCorrelations:\n"
    "    measure: spearman\n"
    "    pairs:\n"
    "      - { columns: [amount, balance], value: '0.6' }\n"
    "      - { columns: [balance, opened_at], value: '-0.4' }\n"
    "      - { columns: [opened_at, closed_on], value: '0.7' }\n"
    "      - { columns: [amount, limit_raised], value: '0.3' }\n"
)
TAU = (
    "copula:\n  columns: [amount, balance]\n  rankCorrelations:\n    measure: kendall\n"
    "    pairs: [ { columns: [amount, balance], value: '0.5' } ]\n"
)
NAMES = ["acct_id", "amount", "balance", "opened_at", "closed_on", "region", "limit_raised"]


def _with_rows(tmp_path: Path, copula: str, name: str, rows: int) -> Path:
    bundle = bundle_with(tmp_path, copula, name)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert text.count("        rows: 400\n") == 1
    simulation.write_text(text.replace("        rows: 400\n", f"        rows: {rows}\n"), "utf-8")
    return bundle


def _run(bundle: Path, **options: Any) -> Any:
    return pipeline.execute(bundle, kernel=KERNEL_V2, **options)


def _rows(outcome: Any) -> List[Sequence[Any]]:
    return [tuple(row) for row in outcome.emit("accounts").rows]


@pytest.mark.parametrize("chunk", [4096, 7])
def test_the_column_path_equals_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    bundle = _with_rows(tmp_path, FIVE, "paths", 300)
    by_columns, by_rows, taken = _by_both_paths(monkeypatch, lambda: _run(bundle))
    assert _rows(by_columns) == _rows(by_rows)
    assert taken and all(taken)


def test_the_column_path_equals_the_per_row_path_with_a_null_fraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _with_rows(tmp_path, FIVE, "nulls", 300)
    simulation = bundle / "simulation.flux.yml"
    old = "binWeights: ['0.2', '0.5', '0.3']"
    text = simulation.read_text("utf-8")
    simulation.write_text(text.replace(old, old + "\n              nullFraction: '0.25'"), "utf-8")
    by_columns, by_rows, taken = _by_both_paths(monkeypatch, lambda: _run(bundle))
    assert _rows(by_columns) == _rows(by_rows)
    assert taken and all(taken)
    nulls = sum(row[NAMES.index("balance")] is None for row in _rows(by_columns))
    assert 40 < nulls < 110


def test_every_value_is_on_its_columns_grid_inside_its_bins(tmp_path: Path) -> None:
    for row in _rows(_run(_with_rows(tmp_path, FIVE, "grid", 500))):
        by = dict(zip(NAMES, row, strict=True))
        assert Decimal("0") <= by["amount"] <= Decimal("1000") and by["amount"] == round(
            by["amount"], 2
        )
        assert isinstance(by["balance"], int) and 0 <= by["balance"] <= 500
        assert 1782864000000 <= by["opened_at"] <= 1790726400000
        assert isinstance(by["closed_on"], date) and date(2026, 10, 1) <= by["closed_on"] <= date(
            2026, 12, 31
        )
        assert 0 <= by["limit_raised"] <= 9


def test_with_no_pair_listed_each_margin_is_its_histograms(tmp_path: Path) -> None:
    empty = "copula:\n  columns: [amount, balance]\n  rankCorrelations: { measure: kendall, pairs: [] }\n"
    rows = _rows(_run(_with_rows(tmp_path, empty, "margins", 20000)))
    first = [row[NAMES.index("amount")] for row in rows]
    shares = [
        sum(1 for v in first if lo <= v < hi) / len(first) for lo, hi in ((0, 100), (100, 400))
    ]
    assert shares[0] == pytest.approx(0.5, abs=0.02) and shares[1] == pytest.approx(0.3, abs=0.02)
    second = [row[NAMES.index("balance")] for row in rows]
    assert sum(1 for v in second if v < 50) / len(second) == pytest.approx(0.2, abs=0.02)


def _kendall_tau(a: "np.ndarray", b: "np.ndarray") -> float:
    """``S / C(n, 2)``: the sum of ``sign(a_i - a_j) * sign(b_i - b_j)`` over pairs, ties adding 0."""
    n = a.shape[0]
    total = 0
    for start in range(0, n, 500):
        block = slice(start, min(n, start + 500))
        total += int(
            (np.sign(a[block, None] - a[None, :]) * np.sign(b[block, None] - b[None, :])).sum()
        )
    return total / 2 / (n * (n - 1) / 2)


def test_the_empirical_kendall_tau_of_drawn_rows_is_the_declared_one(tmp_path: Path) -> None:
    rows = _rows(_run(_with_rows(tmp_path, TAU, "tau", 20000)))
    amount = np.array([float(row[NAMES.index("amount")]) for row in rows])
    balance = np.array([float(row[NAMES.index("balance")]) for row in rows])
    assert _kendall_tau(amount, balance) == pytest.approx(0.5, abs=0.03)


@pytest.mark.parametrize("count", [3, 5])
def test_the_shards_joined_are_the_unsharded_run(tmp_path: Path, count: int) -> None:
    bundle = _with_rows(tmp_path, FIVE, "shards", 300)
    whole = _rows(_run(bundle))
    parts = [_rows(_run(bundle, shard=(i, count))) for i in range(1, count + 1)]
    assert [row for part in parts for row in part] == whole


@pytest.mark.parametrize("scale", ["0.25", "0.5", "2"])
def test_a_scaled_run_has_the_scaled_rows_on_either_path_and_replays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scale: str
) -> None:
    bundle = _with_rows(tmp_path, FIVE, "scales", 400)
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: _run(bundle, scale=Decimal(scale))
    )
    expected = int(400 * Decimal(scale))
    assert len(_rows(by_columns)) == expected
    assert _rows(by_columns) == _rows(by_rows) and taken and all(taken)
    assert _rows(_run(bundle, scale=Decimal(scale))) == _rows(by_columns)
    # A copula's rows depend on their ordinal and the run's root alone, so a shard of a scaled run
    # is that run's slice.
    first = _rows(_run(bundle, scale=Decimal(scale), shard=(1, 2)))
    assert first == _rows(by_columns)[: len(first)]


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_jobs_do_not_move_the_rows(tmp_path: Path) -> None:
    bundle = _with_rows(tmp_path, FIVE, "jobs", 12000)
    runs = [_run(bundle, jobs=jobs) for jobs in (1, 4)]
    assert runs[1].emit("accounts").processes > 1
    assert (
        runs[0].emit("accounts").writer.stream_bytes()
        == runs[1].emit("accounts").writer.stream_bytes()
    )
    assert (
        runs[0].emit("accounts").writer.output_digest()
        == runs[1].emit("accounts").writer.output_digest()
    )
