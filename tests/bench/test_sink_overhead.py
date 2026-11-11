# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How much a sink adds to a run: Parquet and ADBC-DuckDB against generation.

``make bench`` runs this module with pytest-benchmark (BSD-2-Clause, in the
``accuracy`` dependency group). It generates one stream of
``FLUX_BENCH_ROWS`` rows (20,000 unless set) from the small telco fixture
with its declared row count raised, times the generation once, then times
delivering that stream to each sink over several rounds. Each sink's
``extra_info`` records its time as a share of the generation time, which
is the number that says whether a sink is worth worrying about.

Marked ``slow`` and skipped without pytest-benchmark, so the fast suite
never runs it and CI's slow shard, which installs ``.[dev]`` alone, skips
it. Under pytest-xdist pytest-benchmark switches itself off and each sink
runs once, untimed; ``make bench`` runs it in one process to time it.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

pytest.importorskip("pytest_benchmark", reason="needs pytest-benchmark (the accuracy group)")
pytest.importorskip("pyarrow")
pytest.importorskip("adbc_driver_manager")
pytest.importorskip("adbc_driver_duckdb")

from flux_engine.sinks.registry import open_target  # noqa: E402
from flux_engine.stages import pipeline  # noqa: E402
from flux_engine.stages.deliver import deliver  # noqa: E402

pytestmark = pytest.mark.slow

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"
_ROWS = int(os.environ.get("FLUX_BENCH_ROWS", "20000"))
_ROUNDS = 5

_TIMES: Dict[str, float] = {}


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("bench") / "bundle"
    shutil.copytree(_FIXTURE, target)
    simulation = target / "simulation.flux.yml"
    text, count = re.subn(r"(?m)^(\s+rows:) 64\b", rf"\1 {_ROWS}", simulation.read_text())
    assert count == 1, "the fixture's declared row count moved"
    simulation.write_text(text)
    return target


@pytest.fixture(scope="module")
def result(bundle: Path) -> Any:
    started = time.perf_counter()
    executed = pipeline.execute(bundle)
    _TIMES["generation"] = time.perf_counter() - started
    assert executed.emits[0].result.rows == _ROWS
    return executed


def _share(benchmark: Any, name: str) -> None:
    if benchmark.stats is None:
        # pytest-benchmark is off (under xdist, or --benchmark-disable): the
        # sink ran once, untimed, which still checks that it works.
        return
    mean = benchmark.stats.stats.mean
    benchmark.extra_info["rows"] = _ROWS
    benchmark.extra_info["rows_per_second"] = round(_ROWS / mean)
    benchmark.extra_info["share_of_generation_pct"] = round(100 * mean / _TIMES["generation"], 2)
    _TIMES[name] = mean


def test_generation(benchmark: Any, bundle: Path) -> None:
    benchmark.pedantic(pipeline.execute, args=(bundle,), rounds=1, iterations=1)
    benchmark.extra_info["rows"] = _ROWS


def test_parquet_sink(benchmark: Any, result: Any, tmp_path: Path) -> None:
    target = open_target(f"parquet:{tmp_path / 'parquet'}")
    benchmark.pedantic(
        deliver, args=(result,), kwargs={"run_id": "bench", "target": target}, rounds=_ROUNDS
    )
    _share(benchmark, "parquet")


@pytest.fixture
def duckdb_uri(tmp_path: Path) -> Iterator[str]:
    yield str(tmp_path / "bench.duckdb")


def test_adbc_duckdb_sink(benchmark: Any, result: Any, duckdb_uri: str) -> None:
    target = open_target("adbc:duckdb", {"uri": duckdb_uri})
    # Every round after the first replays the slice: a delete and an insert,
    # the cost a re-run pays.
    benchmark.pedantic(
        deliver, args=(result,), kwargs={"run_id": "bench", "target": target}, rounds=_ROUNDS
    )
    _share(benchmark, "adbc-duckdb")
