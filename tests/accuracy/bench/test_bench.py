# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Speed: the kernel samplers, stream derivation, row generation, S and the fidelity check.

Two groups of tests share one table of targets:

* ``test_bench_*`` time each target with pytest-benchmark. They run only when
  ``FLUX_ACCURACY_BENCH=1`` and not under pytest-xdist (a benchmark in a parallel worker
  measures the machine's other workers); the nightly workflow runs them once, alone, with
  ``--benchmark-json``. They never slow the pull-request tier.
* ``test_bench_targets_run_once`` calls every target once, with no ``benchmark`` fixture, in
  every tier, so that a renamed engine function or a changed signature breaks the pull
  request and not the nightly artefact.

Each target is a zero-argument function over inputs built beforehand, so what is timed is the
named operation and not its set-up. Sizes are in the target's name.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("pytest_benchmark")

import copy  # noqa: E402
import os  # noqa: E402
import random  # noqa: E402
from decimal import Decimal  # noqa: E402
from functools import lru_cache  # noqa: E402
from typing import Any, Callable, Dict, List, Tuple  # noqa: E402

import pytest  # noqa: E402

from flux_engine.concordance.boundary.statistic import compute_statistic  # noqa: E402
from flux_engine.concordance.verifier import ClearStatistic  # noqa: E402
from flux_engine.core.rng import (  # noqa: E402
    DrawStream,
    bernoulli,
    bounded_uint,
    categorical,
    exponential,
    poisson,
    truncated_normal,
    uniform53,
)
from flux_engine.core.seedtree import derive_root, stream_entropy  # noqa: E402
from flux_engine.generator.rows import generate_rows  # noqa: E402
from tests.accuracy.determinism.manifest import small_fixture_plan  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.concordance.conftest import (  # noqa: E402
    build_committed,
    build_compiled,
    vectors_document,
)

BENCH_ENV = "FLUX_ACCURACY_BENCH"
DRAWS = 500
STREAMS = 1000
GENERATOR_ROWS = 1000
STATISTIC_ROWS = 5000

D = Decimal

Target = Callable[[], Any]


def _skip_unless_benchmarking() -> None:
    if os.environ.get(BENCH_ENV, "").strip() != "1":
        pytest.skip(f"set {BENCH_ENV}=1 to run the benchmarks (they run alone, not under xdist)")
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.skip("benchmarks cannot run under pytest-xdist")


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _entropies() -> Tuple[int, ...]:
    root = derive_root(b"\x01" * 32, 20261111)
    return tuple(
        stream_entropy(root, ("sim", "bench", "gen", "x", "row", row, "value"))
        for row in range(STREAMS)
    )


def _sampler(draw: Callable[[DrawStream], Any]) -> Target:
    """*draw* on :data:`DRAWS` fresh sibling streams: how a run draws, one stream per value."""
    entropies = _entropies()[:DRAWS]

    def target() -> Any:
        last = None
        for entropy in entropies:
            last = draw(DrawStream.from_entropy(entropy))
        return last

    return target


def _stream_entropy_target() -> Target:
    root = derive_root(b"\x02" * 32, 7)
    paths = [("sim", "bench", "gen", "x", "row", row, "value") for row in range(STREAMS)]

    def target() -> Any:
        last = 0
        for path in paths:
            last = stream_entropy(root, path)
        return last

    return target


def _draw_stream_target() -> Target:
    entropies = _entropies()

    def target() -> Any:
        last = None
        for entropy in entropies:
            last = DrawStream.from_entropy(entropy)
        return last

    return target


def _generator_target() -> Target:
    plan, seam = small_fixture_plan(GENERATOR_ROWS)
    root = derive_root(seam, 20261111)

    def target() -> Any:
        return generate_rows(plan, root=root)

    return target


def _fidelity_inputs() -> Tuple[Any, Any, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The published vectors' contract and block, with a committed state and D' of 5,000 rows."""
    document = vectors_document()
    document = copy.deepcopy(document)
    document["extension"]["fidelity"]["records"] = {"min": 4, "max": 100000}
    document["emit"]["rows"] = STATISTIC_ROWS

    def rows(seed: int) -> List[Dict[str, Any]]:
        rng = random.Random(seed)
        return [
            {
                "subscriber_id": f"s{i}",
                "aborted_at": f"2026-08-{1 + i % 28:02d}T{i % 24:02d}:00:00.000Z",
                "amount": str(rng.randint(1, 8)),
                "attempts": rng.randint(0, 16),
                "market": rng.choice(["ie", "uk"]),
            }
            for i in range(STATISTIC_ROWS)
        ]

    committed_rows = rows(1)
    document["committedSlice"] = committed_rows
    compiled = build_compiled(document)
    committed = build_committed(document, compiled)
    return compiled, committed, committed_rows, rows(2)


def _statistic_target() -> Target:
    compiled, _, committed_rows, _ = _fidelity_inputs()

    def target() -> Any:
        return compute_statistic(committed_rows, compiled)

    return target


def _check_target() -> Target:
    compiled, committed, _, synthetic = _fidelity_inputs()

    def target() -> Any:
        evaluation = ClearStatistic(
            compiled=compiled, committed=committed, synthetic_rows=synthetic
        ).evaluate()
        assert not evaluation.refusals
        return evaluation

    return target


#: name -> (benchmark group, builder of the target). Builders run outside the timed region.
TARGETS: Dict[str, Tuple[str, Callable[[], Target]]] = {
    f"sampler-uniform53-x{DRAWS}": ("kernel", lambda: _sampler(uniform53)),
    f"sampler-bernoulli-x{DRAWS}": ("kernel", lambda: _sampler(lambda s: bernoulli(s, D("0.3")))),
    f"sampler-categorical-x{DRAWS}": (
        "kernel",
        lambda: _sampler(lambda s: categorical(s, {"a": D("0.5"), "b": D("0.3"), "c": D("0.2")})),
    ),
    f"sampler-bounded_uint-x{DRAWS}": (
        "kernel",
        lambda: _sampler(lambda s: bounded_uint(s, 1_000_003)),
    ),
    f"sampler-truncated_normal-x{DRAWS}": (
        "kernel",
        lambda: _sampler(
            lambda s: truncated_normal(s, mean=D(50), stddev=D(15), lo=D(1), hi=D(10000), scale=2)
        ),
    ),
    f"sampler-exponential-x{DRAWS}": (
        "kernel",
        lambda: _sampler(lambda s: exponential(s, rate=D(2))),
    ),
    f"sampler-poisson-x{DRAWS}": ("kernel", lambda: _sampler(lambda s: poisson(s, mean=D(40)))),
    f"stream-entropy-x{STREAMS}": ("streams", _stream_entropy_target),
    f"stream-from-entropy-x{STREAMS}": ("streams", _draw_stream_target),
    f"generator-rows-x{GENERATOR_ROWS}": ("generator", _generator_target),
    f"fidelity-compute-statistic-x{STATISTIC_ROWS}": ("fidelity", _statistic_target),
    f"fidelity-check-evaluate-x{STATISTIC_ROWS}": ("fidelity", _check_target),
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_bench_targets_run_once(name: str) -> None:
    """Every target still runs, and does the work it names (every tier, no benchmark fixture)."""
    _, build = TARGETS[name]
    result = build()()
    assert result is not None
    if name.startswith("generator-rows"):
        assert len(result) == GENERATOR_ROWS
    if name.startswith("fidelity-compute-statistic"):
        assert result.record_count == STATISTIC_ROWS
    if name.startswith("fidelity-check-evaluate"):
        assert result.synthetic_rows == STATISTIC_ROWS and result.outcomes


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_bench(name: str, benchmark: Any, tier: Tier) -> None:
    _skip_unless_benchmarking()
    group, build = TARGETS[name]
    target = build()
    benchmark.group = group
    benchmark.extra_info.update({"target": name, "tier": tier.name, "baseSeed": tier.base_seed})
    benchmark(target)
