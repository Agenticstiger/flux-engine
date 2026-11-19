# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Prototype for ADR 0010: a truncated normal from basic IEEE 754 operations.

Kernel v1 maps one 53-bit uniform through the inverse normal CDF in the
``decimal`` module (KERNEL-SPEC section 4: Acklam's approximation, one Halley
step, ``Decimal.exp``, ``.ln`` and ``.sqrt``). This prototype maps the same
uniform through Wichura's AS241 (PPND16, Applied Statistics 37(3), 1988), with
the coefficients CPython's ``statistics.NormalDist.inv_cdf`` uses, in binary64.

It uses only addition, subtraction, multiplication, division and square root,
which IEEE 754 requires to be correctly rounded, and ``frexp``, which is exact,
in one fixed order. The tails' logarithm follows the method of fdlibm's
``e_log.c`` (Sun Microsystems, 1993) on those operations, so no libm function is
called and the result cannot depend on the platform's libm or on which SIMD loop
numpy picks. Φ at the two bounds is still worked out once per declaration in
``decimal``, with the engine's own ``normal_cdf``, and then rounded to binary64. The
transform itself lives in ``tests/kernel2/reference.py``, the reference for KERNEL-SPEC-2;
this module measures it.
A draw in the upper half of the interval is mapped through the upper tail's own
probability, ``Φ(-b) + (1 - u) * mass``, never through one minus a number close
to one: in binary64 that subtraction loses most of a far tail's digits.

For each declared interval the script reports:

* speed: v1's ``truncated_normal`` per draw, the prototype per draw in plain
  Python floats, and the prototype over an array in numpy, in interleaved
  rounds with the load average beside each;
* error: the largest distance from the value v1 computes before it rounds, in
  steps of the statistics' 16-fractional-bit grid (KERNEL-SPEC section 12); how
  many draws round to another value at the declared scale than v1's do; how many
  of those land in another step of the grid;
* sameness: whether numpy and plain Python give the same bits for every input,
  a digest of those bits for comparison with other machines, and how often
  numpy's own ``log`` differs from ``math.log`` here.

Run from the repository root::

    python -m tests.bench.kernel_normal --draws 200000 --rounds 3

Nothing in the engine imports this module.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import platform
import statistics
import sys
import time
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from numpy.random import Philox

from flux_engine.core.rng import normal_cdf, normal_ppf, truncated_normal
from tests.kernel2.reference import (
    TWO_POW_53,
    TWO_POW_MINUS_53,
    Interval,
    OneWord,
    Plan,
    bits,
    log_basic,
    plan_of,
)
from tests.kernel2.reference import truncated_normal as draw_v2
from tests.kernel2.reference import truncated_normal_scalar as draw_v2_scalar

#: The statistics grid: values are compared as multiples of 2**-16 (KERNEL-SPEC section 12).
GRID_STEPS_PER_UNIT = 1 << 16
_PRECISION = 50
_TWO_POW_53 = TWO_POW_53
_TWO_POW_MINUS_53 = TWO_POW_MINUS_53

#: The declarations the prototype measures, from the plain middle to the deep tail.
INTERVALS: Tuple[Interval, ...] = (
    Interval("standard [-8, 8]", Decimal(0), Decimal(1), Decimal(-8), Decimal(8), 6),
    Interval("middle DECIMAL(10,2)", Decimal(50), Decimal(10), Decimal(20), Decimal(80), 2),
    Interval("upper tail [3, 10]", Decimal(0), Decimal(1), Decimal(3), Decimal(10), 6),
    Interval("deep lower tail [-12, -6]", Decimal(0), Decimal(1), Decimal(-12), Decimal(-6), 6),
    Interval("INT, mean 1000", Decimal(1000), Decimal(300), Decimal(0), Decimal(5000), 0),
)


def reference_z(u53: int, plan: Plan) -> Decimal:
    """The standardised value v1 rounds from: its own ``normal_ppf`` at 50 digits."""
    with localcontext() as context:
        context.prec = _PRECISION
        u = Decimal(u53) / _TWO_POW_53
        if plan.mirror:
            u = 1 - u
        target = plan.cdf_a + u * plan.mass
        if plan.degenerate or target <= 0:
            z = plan.a
        elif target >= 1:
            z = plan.b
        else:
            z = normal_ppf(target, precision=_PRECISION)
        return -z if plan.mirror else z


def draw_v1(u53: int, plan: Plan) -> Decimal:
    interval = plan.interval
    return truncated_normal(
        OneWord(u53),  # type: ignore[arg-type]
        mean=interval.mean,
        stddev=interval.stddev,
        lo=interval.lo,
        hi=interval.hi,
        scale=interval.scale,
    )


# ---------------------------------------------------------------------------
# Inputs, measurements, report
# ---------------------------------------------------------------------------


def uniforms(draws: int) -> "np.ndarray":
    """*draws* uniform53 values from a fixed Philox key, then the edges and every power of two."""
    raw = Philox(key=np.array((5, 6), dtype=np.uint64)).random_raw(draws) >> np.uint64(11)
    edges = [0, 1, 2, 3, _TWO_POW_53 - 1, _TWO_POW_53 - 2, _TWO_POW_53 // 2]
    edges += [1 << k for k in range(53)] + [_TWO_POW_53 - (1 << k) for k in range(53)]
    return np.concatenate((raw, np.array(edges, dtype=np.uint64)))


@dataclass
class Accuracy:
    largest_grid_error: float
    at_u53: int
    scale_mismatches: int
    grid_mismatches: int
    largest_unit_difference: int


def _grid_step(value: Decimal) -> int:
    return int((value * GRID_STEPS_PER_UNIT).to_integral_value(rounding=ROUND_FLOOR))


def accuracy(u53: "np.ndarray", plan: Plan) -> Accuracy:
    z, units = draw_v2(u53, plan)
    largest = Decimal(0)
    at = 0
    scale_mismatches = grid_mismatches = largest_units = 0
    stddev = plan.interval.stddev
    for index, raw in enumerate(u53.tolist()):
        with localcontext() as context:
            context.prec = _PRECISION
            error = abs(Decimal(float(z[index])) - reference_z(raw, plan)) * stddev
        if error > largest:
            largest, at = error, raw
        v1_value = draw_v1(raw, plan)
        v2_value = Decimal(int(units[index])).scaleb(-plan.interval.scale)
        if v1_value != v2_value:
            scale_mismatches += 1
            difference = int(abs(v1_value - v2_value).scaleb(plan.interval.scale))
            largest_units = max(largest_units, difference)
            if _grid_step(v1_value) != _grid_step(v2_value):
                grid_mismatches += 1
    return Accuracy(
        float(largest * GRID_STEPS_PER_UNIT), at, scale_mismatches, grid_mismatches, largest_units
    )


def bits_digest(u53: "np.ndarray", plans: Sequence[Plan]) -> str:
    """sha256 over the binary64 bits of ``z`` for every input under every plan, numpy route."""
    digest = hashlib.sha256()
    for plan in plans:
        digest.update(draw_v2(u53, plan)[0].astype("<f8").tobytes())
    return digest.hexdigest()


def scalar_mismatches(u53: "np.ndarray", plan: Plan) -> int:
    """How many inputs give other bits, or another value, in plain Python than in numpy."""
    z, units = draw_v2(u53, plan)
    mismatches = 0
    for index, raw in enumerate(u53.tolist()):
        z_scalar, units_scalar = draw_v2_scalar(raw, plan)
        if bits(z_scalar) != bits(float(z[index])) or units_scalar != int(units[index]):
            mismatches += 1
    return mismatches


#: Probabilities to probe for order: AS241's region edges (0.075 and its mirror, and the
#: tail's r = 5 edge at exp(-25)), the switch between the two tails, and a plain central one.
MONOTONICITY_PROBES = (
    Decimal("0.075"),
    Decimal("0.2"),
    Decimal("0.5"),
    Decimal("0.925"),
    Decimal("1.3887943864964020e-11"),
)


@dataclass
class Order:
    pairs: int
    backward: int
    largest_ulps: float
    reach: int
    boundary_windows: int
    backward_at_scale: int


#: The farthest apart, in uniform53 steps, two inputs are compared for order.
_MAX_REACH = 16


def _reachable(plan: Plan, probe: Decimal) -> bool:
    return bool(plan.cdf_a < probe < plan.cdf_a + plan.mass)


def _window(plan: Plan, probe: Decimal, window: int) -> "np.ndarray":
    """*window* consecutive uniform53 values around the one that maps to *probe*."""
    with localcontext() as context:
        context.prec = _PRECISION
        fraction = (probe - plan.cdf_a) / plan.mass
        centre = int((1 - fraction if plan.mirror else fraction) * _TWO_POW_53)
    start = max(0, min(centre - window // 2, _TWO_POW_53 - window))
    return np.arange(start, start + window, dtype=np.uint64)


def _boundary_probe(plan: Plan, probe: Decimal) -> Decimal:
    """The probability, in the plan's frame, at which the value crosses the rounding
    boundary of the declared scale nearest above the value *probe* maps to."""
    interval = plan.interval
    with localcontext() as context:
        context.prec = _PRECISION
        z = normal_ppf(probe, precision=_PRECISION)
        value = interval.mean + interval.stddev * (-z if plan.mirror else z)
        step = Decimal(1).scaleb(-interval.scale)
        boundary = (value / step).to_integral_value(rounding=ROUND_FLOOR) * step + step / 2
        z_boundary = (boundary - interval.mean) / interval.stddev
        return normal_cdf(-z_boundary if plan.mirror else z_boundary, precision=_PRECISION)


def monotonicity(plan: Plan, window: int) -> Order:
    """Order over *window* neighbouring uniforms around each probe the interval reaches.

    Around the probes themselves: how many steps of z go backward, by how many units in
    the last place at most, and the farthest apart (in uniform53 steps) two inputs are
    whose values come out in the wrong order. Around the rounding boundary of the
    declared scale nearest each probe: how many steps go backward after rounding.
    """
    order = Order(0, 0, 0.0, 0, 0, 0)
    for probe in MONOTONICITY_PROBES:
        if not _reachable(plan, probe):
            continue
        z, _units = draw_v2(_window(plan, probe, window), plan)
        steps = np.diff(z)
        backward = steps < 0
        spacing = np.spacing(np.maximum(np.abs(z[:-1]), np.abs(z[1:])))
        order.pairs += steps.size
        order.backward += int(np.count_nonzero(backward))
        if backward.any():
            order.largest_ulps = max(
                order.largest_ulps, float((np.abs(steps) / spacing)[backward].max())
            )
        for distance in range(1, _MAX_REACH + 1):
            if np.any(z[distance:] < z[:-distance]):
                order.reach = max(order.reach, distance)
        boundary = _boundary_probe(plan, probe)
        if _reachable(plan, boundary):
            _z, units = draw_v2(_window(plan, boundary, window), plan)
            order.boundary_windows += 1
            order.backward_at_scale += int(np.count_nonzero(np.diff(units) < 0))
    return order


def log_disagreements(count: int) -> Tuple[int, int, int]:
    """Over *count* points in (0, 0.5]: numpy log vs math.log differences, and the
    largest distance of ``log_basic`` from ``math.log`` in units in the last place."""
    x = (Philox(key=np.array((7, 8), dtype=np.uint64)).random_raw(count) >> np.uint64(11)).astype(
        np.float64
    ) * _TWO_POW_MINUS_53
    x = np.concatenate((x * 0.5, np.ldexp(1.0, -np.arange(1, 1000))))
    x = x[x > 0]
    libm = np.array([math.log(value) for value in x.tolist()])
    numpy_differs = int(np.count_nonzero(np.log(x) != libm))
    ours = log_basic(x)
    ulps = np.abs(ours.view(np.int64) - libm.view(np.int64))
    return numpy_differs, int(ulps.max()), int(np.count_nonzero(ulps))


def _v1_batch(inputs: Sequence[int], plan: Plan) -> List[Decimal]:
    return [draw_v1(raw, plan) for raw in inputs]


def _v2_scalar_batch(inputs: Sequence[int], plan: Plan) -> List[Tuple[float, int]]:
    return [draw_v2_scalar(raw, plan) for raw in inputs]


def _load() -> str:
    try:
        one, five, fifteen = os.getloadavg()
    except (AttributeError, OSError):  # Windows has no load average
        return "n/a"
    return f"{one:.2f} {five:.2f} {fifteen:.2f}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--draws", type=int, default=200_000, help="random inputs per interval")
    parser.add_argument("--timed", type=int, default=10_000, help="draws per timed v1 call")
    parser.add_argument("--vector", type=int, default=1_000_000, help="draws per numpy call")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--window", type=int, default=40_000, help="neighbours per order probe")
    args = parser.parse_args(argv)

    plans = [plan_of(interval) for interval in INTERVALS]
    inputs = uniforms(args.draws)
    vector_inputs = uniforms(args.vector)
    timed_inputs = inputs[: args.timed].tolist()
    timed_count = len(timed_inputs)
    print(
        f"python {platform.python_version()}, numpy {np.__version__}, "
        f"{platform.system()} {platform.machine()}; {inputs.size:,} inputs per interval"
    )

    times: Dict[Tuple[str, str], List[float]] = {}
    for round_index in range(1, args.rounds + 1):
        for plan in plans:
            runs: Tuple[Tuple[str, Callable[..., object], Tuple[object, ...], int], ...] = (
                ("v1 decimal", _v1_batch, (timed_inputs, plan), timed_count),
                ("v2 python", _v2_scalar_batch, (timed_inputs, plan), timed_count),
                ("v2 numpy", draw_v2, (vector_inputs, plan), vector_inputs.size),
            )
            for name, function, arguments, count in runs:
                load = _load()
                started = time.perf_counter()
                function(*arguments)
                elapsed = time.perf_counter() - started
                times.setdefault((plan.interval.name, name), []).append(elapsed / count)
                print(
                    f"round {round_index}  {plan.interval.name:<26} {name:<10}"
                    f" {elapsed / count * 1e9:10.1f} ns/draw  load {load}"
                )

    print("\ninterval / variant                      best ns/draw  median ns/draw  v1 best / best")
    for (interval_name, name), observed in times.items():
        v1_best = min(times[(interval_name, "v1 decimal")])
        label = f"{interval_name} / {name}"
        print(
            f"{label:<39} {min(observed) * 1e9:12.1f} {statistics.median(observed) * 1e9:15.1f}"
            f" {v1_best / min(observed):15.1f}x"
        )

    print("\nper interval: largest error in grid steps (at u53); draws at another value at the")
    print(
        "declared scale than v1; of those, draws in another grid step; largest difference in units"
    )
    for plan in plans:
        result = accuracy(inputs, plan)
        print(
            f"{plan.interval.name:<26} {result.largest_grid_error:10.3e} ({result.at_u53})"
            f"  {result.scale_mismatches}  {result.grid_mismatches}"
            f"  {result.largest_unit_difference}"
        )

    print("\nper interval: neighbouring uniforms around AS241's edges and the tail switch;")
    print("backward steps of z, the largest in ulps, the farthest reach in uniform53 steps;")
    print("then windows centred on rounding boundaries: backward steps at the declared scale")
    for plan in plans:
        order = monotonicity(plan, args.window)
        print(
            f"{plan.interval.name:<26} {order.backward} of {order.pairs:,}"
            f"  {order.largest_ulps:.1f}  {order.reach}"
            f"  | {order.boundary_windows} windows: {order.backward_at_scale}"
        )

    mismatches = sum(scalar_mismatches(inputs, plan) for plan in plans)
    numpy_log, largest_ulps, ulp_count = log_disagreements(args.draws)
    print(f"\nnumpy and plain Python differ on {mismatches} of {inputs.size * len(plans):,} draws")
    print(f"digest of z over every interval: {bits_digest(inputs, plans)}")
    print(
        f"np.log differs from math.log on {numpy_log} points; log_basic is within"
        f" {largest_ulps} ulp of math.log and differs on {ulp_count} points"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
