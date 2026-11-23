# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Kernel 0.2.0's refusals and bounds that the vector files do not reach.

The Poisson table a draw reads keeps only the thresholds a uniform53 can tell apart, so its
size follows the spread of the distribution and not its mean; it gives the draw of the
full table at every threshold's edge. Non-finite declarations are refused before any memo
sees them, a 2**64th draw on one path is refused, a Philox counter that is not four 64-bit
words, or a key that is not two, is refused, and the subnormal check runs before a
binary64 declaration's first draw on any path into the kernel, not only the stage one.
"""

from __future__ import annotations

import bisect
import math
import random
from decimal import Decimal
from pathlib import Path
from typing import Any, Tuple

import pytest

from flux_engine.core import kernel2, rng
from flux_engine.core.errors import FluxError
from flux_engine.core.kernel2 import FLX_5050, CounterStream
from flux_engine.core.seedtree import SeedTree, derive_root

MEANS = ("0.5", "1", "3", "40", "700", "2500")


@pytest.mark.parametrize("mean", MEANS)
def test_the_table_a_draw_reads_gives_the_full_tables_draw(mean: str) -> None:
    value = Decimal(mean)
    thresholds = kernel2.poisson_thresholds(value)
    top = (1 << 53) - 1
    probes = {0, 1, top}
    for threshold in thresholds:
        probes.update(t for t in (threshold - 1, threshold, threshold + 1) if 0 <= t <= top)
    chooser = random.Random(f"poisson {mean}")
    probes.update(chooser.randrange(1 << 53) for _ in range(500))
    for u53 in sorted(probes):
        assert kernel2.poisson_of(u53, value) == bisect.bisect_right(thresholds, u53), u53


def test_the_table_of_a_large_mean_follows_its_spread() -> None:
    mean = Decimal(100_000)
    table = kernel2._poisson_table(mean)
    cap = rng.poisson_cap(mean)
    assert len(table.window) < 20 * math.sqrt(100_000)
    assert table.start > 0 and table.stop < cap
    thresholds = kernel2.poisson_thresholds(mean)
    for u53 in (0, 1, 2, thresholds[table.start], thresholds[table.stop - 1], (1 << 53) - 1):
        assert kernel2.poisson_of(u53, mean) == bisect.bisect_right(thresholds, u53)


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_a_declaration_that_is_not_finite_is_refused(bad: str) -> None:
    value = Decimal(bad)
    one = Decimal(1)
    refusals = (
        lambda: kernel2.normal_plan(value, one, Decimal(-1), one, 6),
        lambda: kernel2.normal_plan(Decimal(0), value, Decimal(-1), one, 6),
        lambda: kernel2.normal_plan(Decimal(0), one, value, one, 6),
        lambda: kernel2.exponential_units(0, value, 6),
        lambda: kernel2.poisson_of(0, value),
        lambda: kernel2.poisson_thresholds(value),
    )
    for refusal in refusals:
        with pytest.raises(FluxError) as refused:
            refusal()
        assert refused.value.code.code == "FLX-5021"


def test_a_two_to_the_sixty_fourth_draw_on_one_path_is_refused() -> None:
    derive_root(b"H", 1)  # the seed tree registers FLX-2202
    stream = CounterStream((1, 2), (0, 0))
    stream._position = 1 << 64
    with pytest.raises(FluxError) as refused:
        rng.uniform53(stream)
    assert refused.value.code.code == "FLX-2202"


def test_the_subnormal_check_runs_before_a_binary64_declarations_first_draw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def flushing() -> None:
        raise FluxError(FLX_5050, "flushed")

    monkeypatch.setattr(kernel2, "require_subnormals", flushing)
    kernel2.clear_memos()
    try:
        for draw in (
            lambda: kernel2.normal_plan(Decimal(0), Decimal(1), Decimal(-1), Decimal(1), 6),
            lambda: kernel2.exponential_units(0, Decimal(2), 6),
        ):
            with pytest.raises(FluxError) as refused:
                draw()
            assert refused.value.code.code == "FLX-5050"
    finally:
        kernel2.clear_memos()


def test_a_tree_that_asks_for_a_path_off_the_table_is_refused() -> None:
    tree = SeedTree(root=derive_root(b"H", 1, seed_tree_version=kernel2.SEED_TREE_VERSION))
    with pytest.raises(FluxError) as refused:
        tree._new_stream(("not", "a", "shape"), b"not\x1fa\x1fshape")
    assert refused.value.code.code == "FLX-2201"


# ---------------------------------------------------------------------------
# FLX-5050: the subnormal check itself, and every place a run asks it
# ---------------------------------------------------------------------------


class _Flushing:
    """A module that delegates to *real*, but whose ``ldexp`` returns 0 as a flushed half would."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    @staticmethod
    def ldexp(_mantissa: float, _exponent: int) -> float:
        return 0.0


def test_the_check_refuses_when_plain_arithmetic_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kernel2, "math", _Flushing(math))
    with pytest.raises(FluxError) as refused:
        kernel2.require_subnormals()
    assert refused.value.code.code == "FLX-5050"


def test_the_check_refuses_when_the_array_path_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    import numpy as np

    class _Arrays:
        def __getattr__(self, name: str) -> Any:
            return getattr(np, name)

        @staticmethod
        def array(_values: Any) -> Any:
            return np.zeros(1) * 2.0

    monkeypatch.setattr(kernel2, "np", _Arrays())
    with pytest.raises(FluxError) as refused:
        kernel2.require_subnormals()
    assert refused.value.code.code == "FLX-5050"


def _flushing_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def flushing() -> None:
        raise FluxError(FLX_5050, "flushed")

    monkeypatch.setattr(kernel2, "require_subnormals", flushing)


SMALL = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"


def test_a_kernel_0_2_0_run_is_refused_in_a_flushing_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from flux_engine.core.kernels import KERNEL_V2
    from flux_engine.stages import pipeline
    from flux_engine.stages.plan import plan

    _flushing_process(monkeypatch)

    def loading(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the bundle was loaded before the subnormal check")

    # Refused before the bundle is read, so before any draw at all.
    monkeypatch.setattr(pipeline, "load_bundle", loading)
    with pytest.raises(FluxError) as refused:
        pipeline.execute(SMALL, kernel=KERNEL_V2)
    assert refused.value.code.code == "FLX-5050"
    monkeypatch.undo()
    _flushing_process(monkeypatch)
    planned = plan(SMALL, state_dir=tmp_path / ".flux", kernel="0.2.0")
    assert planned.error_code == "FLX-5050"
    # Kernel 0.1.0's draws are decimal, so the same process runs it.
    assert plan(SMALL, state_dir=tmp_path / ".flux", kernel="0.1.0").success


def test_a_worker_refuses_a_kernel_0_2_0_slice_in_a_flushing_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flux_engine.core.kernels import KERNEL_V2
    from flux_engine.stages import _parallel, pipeline

    result = pipeline.execute(SMALL, kernel=KERNEL_V2)
    outcome = next(emit for emit in result.emits if emit.plan is not None)
    monkeypatch.setitem(
        _parallel._WORKER, "emits", {outcome.expose.expose_id: (outcome.plan, outcome.expose)}
    )
    monkeypatch.setitem(_parallel._WORKER, "root", result.root)
    _flushing_process(monkeypatch)

    def generating(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the slice was generated before the subnormal check")

    monkeypatch.setattr(_parallel, "generate_row_slice", generating)
    sliced = _parallel._run_slice((outcome.expose.expose_id, 0, 4, False))
    assert sliced.generation_refusal is not None
    assert sliced.generation_refusal[0] == "FLX-5050"


# ---------------------------------------------------------------------------
# The samplers hand a counter stream's draw to kernel 0.2.0's rules
# ---------------------------------------------------------------------------


class _Marked(CounterStream):
    """A counter stream whose three transforms answer with markers, so a dispatch shows."""

    __slots__ = ()

    def binary64_truncated_normal(self, u53: int, **_: Any) -> Decimal:
        return Decimal("-111")

    def binary64_exponential(self, u53: int, **_: Any) -> Decimal:
        return Decimal("-222")

    def threshold_poisson(self, u53: int, **_: Any) -> int:
        return -333


def test_the_three_transforms_reach_kernel_0_2_0s_rules() -> None:
    stream = _Marked((1, 2), (0, 0))
    one = Decimal(1)
    drawn = rng.truncated_normal(stream, mean=Decimal(0), stddev=one, lo=-one, hi=one, scale=6)
    assert drawn == Decimal("-111")
    assert rng.exponential(stream, rate=Decimal(2), scale=6) == Decimal("-222")
    assert rng.poisson(stream, mean=Decimal(3)) == -333
    # Each took one draw index, as kernel 0.1.0's sampler takes one uniform.
    assert stream.position == 3
    # A zero stddev and a zero mean keep kernel 0.1.0's rules, and still take their draw.
    zero = Decimal(0)
    assert rng.truncated_normal(stream, mean=one, stddev=zero, lo=zero, hi=one * 2, scale=0) == one
    assert rng.poisson(stream, mean=zero) == 0
    assert stream.position == 5


def _v2_stream() -> CounterStream:
    from flux_engine.core.seedtree import counter_stream, match_shape

    root = derive_root(b"refusals: H", 3, seed_tree_version=kernel2.SEED_TREE_VERSION)
    path = ("world", "w", "cohorts")
    shape = match_shape(path)
    assert shape is not None
    return counter_stream(root, path, shape)


@pytest.mark.parametrize(
    ("mean", "stddev", "lo", "hi", "scale"),
    [
        ("0", "1e-11", "-1e-10", "1e-10", 23),  # scale above 22, its units below 2**52
        ("0", "1", "37", "38", 6),  # less than 2**-969 of the distribution
        ("0", "1", "-1e30", "1e30", 6),  # bounds beyond 2**52 units
        ("0", "1e-1000", "-1", "1", 6),  # a stddev below 2**-900
        ("1e-300", "1", "-1", "1", 6),  # a mean other than zero below 2**-900
    ],
)
def test_kernel_0_2_0s_own_normal_refusals(
    mean: str, stddev: str, lo: str, hi: str, scale: int
) -> None:
    with pytest.raises(FluxError) as refused:
        rng.truncated_normal(
            _v2_stream(),
            mean=Decimal(mean),
            stddev=Decimal(stddev),
            lo=Decimal(lo),
            hi=Decimal(hi),
            scale=scale,
        )
    assert refused.value.code.code == "FLX-5021"


@pytest.mark.parametrize(
    ("rate", "scale"),
    [("1e12", 23), ("1e-300", 6), ("1e-12", 6)],
    ids=["scale-23", "rate-below-2**-900", "units-beyond-2**52"],
)
def test_kernel_0_2_0s_own_exponential_refusals(rate: str, scale: int) -> None:
    with pytest.raises(FluxError) as refused:
        rng.exponential(_v2_stream(), rate=Decimal(rate), scale=scale)
    assert refused.value.code.code == "FLX-5021"


def test_a_plan_with_no_value_at_its_scale_is_refused() -> None:
    with pytest.raises(FluxError) as refused:
        kernel2.normal_plan(Decimal(0), Decimal(1), Decimal("1e-7"), Decimal("2e-7"), 6)
    assert refused.value.code.code == "FLX-5021"


# ---------------------------------------------------------------------------
# Bounds no Philox word reaches
# ---------------------------------------------------------------------------


class _OneWord(CounterStream):
    """Every attempt of every draw reads the same word 0."""

    __slots__ = ("_word",)

    def __init__(self, word: int) -> None:
        super().__init__((0, 0), (0, 0))
        self._word = word

    def block(self, draw: int, attempt: int = 0) -> Tuple[int, int, int, int]:
        return self._word, 0, 0, 0


def test_lemires_test_accepts_a_product_equal_to_its_threshold() -> None:
    # n = 2**63: the threshold 2**64 mod n is 0, and an even word's low product is 0 too.
    assert rng.bounded_uint(_OneWord(2), 1 << 63) == 1


def test_an_ordinal_beyond_64_bits_is_refused() -> None:
    from flux_engine.core.seedtree import counter_stream, match_shape

    root = derive_root(b"ordinals: H", 3, seed_tree_version=kernel2.SEED_TREE_VERSION)
    largest = ("world", "w", "market", (1 << 64) - 1)
    shape = match_shape(largest)
    assert shape is not None
    counter_stream(root, largest, shape)
    with pytest.raises(FluxError) as refused:
        counter_stream(root, ("world", "w", "market", 1 << 64), shape)
    assert refused.value.code.code == "FLX-2202"


@pytest.mark.parametrize(
    ("counter", "key"),
    [
        ((1, 2, 3), (5, 6)),
        ((1, 2, 3, 4, 5), (5, 6)),
        ((1, 2, 3, 1 << 64), (5, 6)),
        ((1, 2, 3, -1), (5, 6)),
        ((1, 2, 3, 4), (1 << 64, 6)),
        ((1, 2, 3, 4), (5, -1)),
    ],
    ids=[
        "three-words",
        "five-words",
        "a-word-past-64-bits",
        "a-negative-word",
        "a-wide-key",
        "a-negative-key",
    ],
)
def test_philox_block_refuses_a_counter_not_of_four_64_bit_words(
    counter: Tuple[int, ...], key: Tuple[int, int]
) -> None:
    with pytest.raises(FluxError) as refused:
        kernel2.philox_block(counter, key)
    assert refused.value.code.code == "FLX-5021"
