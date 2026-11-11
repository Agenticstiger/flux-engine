# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 2, by kind: every generator family fits the distribution its column declares.

One expose carries one column per family (SEAM-FIDELITY-SPEC §1): categorical
from ``levels`` and from an equal-weight ``support``, bounded-normal on a
DECIMAL grid and on the integers, rate (Poisson for INT, exponential for
DECIMAL), uniform over an integer range and over a DECIMAL grid, a declared
histogram, ``nullFraction``, and identifiers. ``build_plan`` and
``generate_column`` draw it the way a run does: one path-derived stream per
cell, so the test covers the seed tree, the sampler and the generator's own
quantisation and clamping.

Each reference is built from the declaration alone with scipy and numpy, and
restates what ``generator/rows.py`` does to the draw:

* **bounded-normal** -- ``truncated_normal`` on the declared ``[range.min,
  range.max]``, rounded to the column's scale, clamped to the inward-rounded
  bounds. ``quantised_pmf`` is exactly that. The default mean is the range
  midpoint and the default stddev is a sixth of the range.
* **rate, INT** -- Poisson, with the search cap ``floor(mean + 10 sqrt(mean) +
  10)`` folding the tail onto the cap, then clamped into the declared range
  (mass below ``range.min`` lands on it, mass above ``range.max`` on it).
* **rate, DECIMAL** -- exponential rounded to the scale, then clamped into the
  range. The clamp mass lands on each bound's grid point.
* **uniform** -- uniform over the grid points of ``[min, max]``, both ends
  included. A histogram picks a bin by its normalised weight, then a grid point
  of the bin: half-open, one step below the next edge, except the last bin,
  closed at the top.
* **nullFraction** -- a Bernoulli first, on the same stream, so the non-null
  values keep the declared distribution.

Choice of test for the grids. Pearson's chi-square with thin cells pooled is
used wherever the grid is coarse enough that the quantisation can show. KS on
discrete data against a continuous CDF is only sensitive to the continuous
shape, so it (with Cramer-von Mises) is used only for the within-bin positions
of a histogram, where the grid is fine relative to the bin and the
discreteness error (at most ``1/(2m)`` for ``m`` grid points) is far below the
critical value. The uniform DECIMAL(12,3) column adds chi-square on the units
modulo 10 and 7, because 5000-point cells cannot see a sampler that visits
only some of the grid points.

Pass criterion: no statistic rejected by Benjamini-Yekutieli at ``q = 0.01``
across the whole battery (BY because the tests of one column share its
sample), asserted per case and once for the whole battery. A draw outside its
declared support is counted, logged and fails its case; it never aborts the
fixture. The identifier checks are exact and structural, not statistical.

Power (``test_the_battery_catches_an_injected_defect``). Each control feeds
the real draws to the module's own statistic against a deliberately wrong
reference, and requires a p-value at or below the battery's rank-one value
(``q / (m c(m))``, 8.0e-5 for the 31 tests). Measured, and asserted per tier:

* Pull-request size (15 000 draws) detects: the DECIMAL mean 2% high; the
  Poisson mean 2% high; 0.03 of categorical mass moved between two levels; the
  lower clamp mass dropped from an exponential column; swapped histogram bin
  weights; nullFraction 0.23 against 0.20; a uniform sampler snapped to every
  tenth grid point; the exponential rate 10% high.
* Only nightly size (150 000 draws) detects: values floored instead of rounded
  to the grid (a half-step shift); the midpoint mean 2% high on the default
  DECIMAL normal; the INT normal mean 2% high; 0.01 of categorical mass moved;
  nullFraction 0.21 against 0.20.
* Neither tier detects an exponential rate 2% high (p about 0.03 at nightly
  size): that is the limit of this chi-square at these sizes, stated rather
  than claimed away. A quarter-step error in a clamp edge cell moves too
  little mass (about 0.1% of the cell) to be seen at either size.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import math  # noqa: E402
import re  # noqa: E402
from collections import Counter  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from decimal import Decimal  # noqa: E402
from typing import Any, Callable, Dict, List, Sequence, Tuple  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from scipy import stats  # noqa: E402

from flux_engine.generator.rows import build_plan, generate_column  # noqa: E402
from tests.accuracy.gof.kernel_battery import rank_one_threshold  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.references import GridPmf, quantised_pmf, truncated_normal  # noqa: E402
from tests.accuracy.harness.stats import Battery, Q, pooled_chisquare  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.generator.conftest import (  # noqa: E402
    SIM_ID,
    H,
    expose_from_schema,
    fidelity,
    root_for,
)

FAMILY = "gof-kind"
CRITERION = "no statistic rejected by Benjamini-Yekutieli at q=0.01 across the generator battery"
ROW_LABEL = "gof-kind-generator"

# ---------------------------------------------------------------------------
# The declarations: one column per family
# ---------------------------------------------------------------------------

LEVELS = {"alpha": "0.5", "beta": "0.3", "gamma": "0.15", "delta": "0.05"}
SUPPORT = ["IE", "GB", "FR"]  # three equal weights: 1/3 has no finite decimal spelling

HIST_DEC_BINS = ["0", "0.05", "10", "20", "50", "100"]
HIST_DEC_WEIGHTS = ["0.01", "0.39", "0.3", "0.2", "0.1"]
HIST_INT_BINS = ["0", "5", "10", "50"]
HIST_INT_WEIGHTS = ["0.2", "0.3", "0.5"]

NULL_FRACTION = "0.2"
NULL_NORMAL_FRACTION = "0.35"

SCHEMA: List[Dict[str, Any]] = [
    {"name": "ident", "type": "STRING", "required": True, "semanticType": "identifier"},
    {"name": "ident_uuid", "type": "uuid", "required": True},
    {"name": "cat_levels", "type": "STRING"},
    {"name": "cat_support", "type": "STRING"},
    {"name": "norm_dec_declared", "type": "DECIMAL(10,2)"},
    {"name": "norm_dec_default", "type": "DECIMAL(10,2)"},
    {"name": "norm_int", "type": "INT"},
    {"name": "rate_int_free", "type": "INT"},
    {"name": "rate_int_clamped", "type": "INT"},
    {"name": "rate_dec_upper", "type": "DECIMAL(10,2)"},
    {"name": "rate_dec_both", "type": "DECIMAL(10,2)"},
    {"name": "uni_int", "type": "INT"},
    {"name": "uni_dec", "type": "DECIMAL(12,3)"},
    {"name": "hist_dec", "type": "DECIMAL(10,2)"},
    {"name": "hist_int", "type": "INT"},
    {"name": "null_cat", "type": "STRING"},
    {"name": "null_norm", "type": "DECIMAL(10,2)"},
]

COLUMNS: Dict[str, Dict[str, Any]] = {
    "cat_levels": {"levels": LEVELS},
    "cat_support": {"support": {"values": SUPPORT}},
    "norm_dec_declared": {
        "range": {"min": "0.00", "max": "1.00"},
        "mean": "0.70",
        "stddev": "0.12",
    },
    "norm_dec_default": {"range": {"min": "0.00", "max": "2.40"}},
    "norm_int": {
        "family": "bounded-normal",
        "range": {"min": "0", "max": "80"},
        "mean": "30",
        "stddev": "15",
    },
    "rate_int_free": {"family": "rate", "mean": "12.0"},
    "rate_int_clamped": {"family": "rate", "mean": "4.5", "range": {"min": "2", "max": "9"}},
    "rate_dec_upper": {"rate": "0.5", "range": {"min": "0.00", "max": "8.00"}},
    "rate_dec_both": {"rate": "0.8", "range": {"min": "0.50", "max": "6.00"}},
    "uni_int": {"range": {"min": "10", "max": "41"}},
    "uni_dec": {"family": "uniform", "range": {"min": "0.000", "max": "100.000"}},
    "hist_dec": {"bins": HIST_DEC_BINS, "binWeights": HIST_DEC_WEIGHTS},
    "hist_int": {"bins": HIST_INT_BINS, "binWeights": HIST_INT_WEIGHTS},
    "null_cat": {"levels": {"x": "0.6", "y": "0.4"}, "nullFraction": NULL_FRACTION},
    "null_norm": {
        "range": {"min": "0.00", "max": "10.00"},
        "mean": "4.00",
        "stddev": "2.00",
        "nullFraction": NULL_NORMAL_FRACTION,
    },
}

DRAWN = [name for name in COLUMNS]


# ---------------------------------------------------------------------------
# References, from the declaration alone
#
# A draw outside the declared support (an undeclared level, a count above the
# clamp, an off-grid value, a value in no bin) is an engine defect, not a
# reason to abort the module fixture: the helpers count it in ``viol`` under
# the case's name, leave it out of the statistic, and the case's test records
# the count and fails after it has written its row.
# ---------------------------------------------------------------------------

Violations = Counter


def _f(text: str) -> float:
    return float(Decimal(text))


def categorical_counts(
    values: Sequence[Any], weights: Dict[str, float], viol: Violations, case: str
) -> Tuple[np.ndarray, np.ndarray]:
    names = sorted(weights)
    index = {name: position for position, name in enumerate(names)}
    kept = [index[value] for value in values if value in index]
    viol[case] += len(values) - len(kept)
    observed = np.bincount(np.array(kept, dtype=np.int64), minlength=len(names))
    return observed, np.array([weights[name] for name in names], dtype=float)


def grid_normal_reference(mean: float, sd: float, lo: float, hi: float, step: float) -> GridPmf:
    """Truncated normal on ``[lo, hi]``, rounded to the grid and clamped to it."""
    return quantised_pmf(truncated_normal(mean, sd, lo, hi), lo, hi, step)


def exponential_reference(rate: float, lo: float, hi: float, step: float) -> GridPmf:
    return quantised_pmf(stats.expon(scale=1.0 / rate), lo, hi, step)


def grid_counts(pmf: GridPmf, values: Sequence[Any], viol: Violations, case: str) -> np.ndarray:
    raw = np.array([float(value) for value in values], dtype=float)
    index = np.rint((raw - pmf.lo) / pmf.step).astype(np.int64)
    inside = (index >= 0) & (index < len(pmf.probabilities))
    viol[case] += int((~inside).sum())
    return np.bincount(index[inside], minlength=len(pmf.probabilities))


def poisson_reference(mean: float, lo: int | None, hi: int | None) -> Tuple[int, np.ndarray]:
    """Poisson, tail folded onto the search cap, then clamped into ``[lo, hi]``.

    Returns the first support point and the probabilities from there.
    """
    cap = int(math.floor(mean + 10.0 * math.sqrt(mean) + 10.0))
    ks = np.arange(cap + 1)
    pmf = stats.poisson.pmf(ks, mean)
    pmf[-1] = stats.poisson.sf(cap - 1, mean)
    low = 0 if lo is None else lo
    high = cap if hi is None else min(hi, cap)
    clamped = np.zeros(high - low + 1)
    for k, mass in zip(ks, pmf, strict=True):
        clamped[min(max(int(k), low), high) - low] += mass
    return low, clamped


def poisson_counts(
    values: Sequence[Any], mean: float, lo: int | None, hi: int | None, viol: Violations, case: str
) -> Tuple[np.ndarray, np.ndarray]:
    first, probabilities = poisson_reference(mean, lo, hi)
    raw = np.asarray(values, dtype=np.int64) - first
    inside = (raw >= 0) & (raw < len(probabilities))
    viol[case] += int((~inside).sum())
    return np.bincount(raw[inside], minlength=len(probabilities)), probabilities


def grid_units(
    values: Sequence[Decimal], lo: Decimal, hi: Decimal, step: Decimal, viol: Violations, case: str
) -> Tuple[np.ndarray, int]:
    """Each value as an integer number of *step* above *lo*, and the grid's point count."""
    points = int((hi - lo) / step) + 1
    units = np.array([int((value - lo) / step) for value in values], dtype=np.int64)
    inside = (units >= 0) & (units < points)
    viol[case] += int((~inside).sum())
    return units[inside], points


def grouped(counts: np.ndarray, width: int) -> Tuple[np.ndarray, np.ndarray]:
    """Group a uniform grid's counts into runs of *width* points; the last run is the remainder."""
    total = len(counts)
    edges = list(range(0, total, width))
    observed = np.array([counts[start : start + width].sum() for start in edges])
    sizes = np.array([min(width, total - start) for start in edges], dtype=float)
    return observed, sizes / total


def residue_counts(units: np.ndarray, points: int, modulus: int) -> Tuple[np.ndarray, np.ndarray]:
    """Counts of ``units mod modulus`` and the share of grid points in each residue class."""
    observed = np.bincount(units % modulus, minlength=modulus)
    shares = np.array(
        [len(range(residue, points, modulus)) for residue in range(modulus)], dtype=float
    )
    return observed, shares / points


def histogram_bins(edges: Sequence[str], weights: Sequence[str], step: Decimal):
    """Per bin: ``(first point, last point, probability)``; the last bin is closed at the top."""
    total = sum(Decimal(weight) for weight in weights)
    bins = []
    for index, weight in enumerate(weights):
        low = Decimal(edges[index])
        top = Decimal(edges[index + 1])
        last = top if index == len(weights) - 1 else top - step
        bins.append((low, max(low, last), Decimal(weight) / total))
    return bins


def histogram_assign(
    values: Sequence[Any],
    bins: Sequence[Tuple[Decimal, Decimal, Decimal]],
    viol: Violations,
    case: str,
) -> Tuple[np.ndarray, List[List[Decimal]]]:
    counts = np.zeros(len(bins), dtype=np.int64)
    within: List[List[Decimal]] = [[] for _ in bins]
    for value in (Decimal(v) for v in values):
        for index, (low, top, _) in enumerate(bins):
            if low <= value <= top:
                counts[index] += 1
                within[index].append(value)
                break
        else:
            viol[case] += 1
    return counts, within


# ---------------------------------------------------------------------------
# The battery
# ---------------------------------------------------------------------------


class Measured:
    """What one column's tests measured, for the result log."""

    def __init__(self) -> None:
        self.cases: Dict[str, Dict[str, Any]] = {}

    def note(self, case: str, **metrics: Any) -> None:
        self.cases.setdefault(case, {}).update(metrics)


def _chi(battery: Battery, case: str, test: str, observed, probabilities) -> int:
    statistic, pvalue, cells = pooled_chisquare(observed, probabilities)
    battery.add(case=case, test=test, statistic=statistic, pvalue=pvalue, sample=case)
    return cells


def _p_chi(observed, probabilities) -> float:
    return pooled_chisquare(observed, probabilities)[1]


def _null_case(
    battery: Battery, case: str, nulls: int, n: int, fraction: str, measured: Measured
) -> None:
    result = stats.binomtest(nulls, n, _f(fraction))
    battery.add(
        case=case,
        test="binomial-null",
        statistic=nulls / n,
        pvalue=result.pvalue,
        sample=case,
    )
    measured.note(case, nullRate=nulls / n, declared=fraction)


def _uniform_decimal_tests(
    battery: Battery, case: str, values: Sequence[Decimal], viol: Violations
) -> None:
    """Coarse bins (5000 units), then the fine grid: units mod 10 and units mod 7.

    The coarse bins alone cannot see a sampler that visits only some of the
    points (every tenth one would pass), which the residue classes do.
    """
    units, points = grid_units(
        values, Decimal("0.000"), Decimal("100.000"), Decimal("0.001"), viol, case
    )
    counts = np.bincount(units, minlength=points)
    observed, probabilities = grouped(counts, 5000)
    _chi(battery, case, "chi2", observed, probabilities)
    for modulus in (10, 7):
        observed, probabilities = residue_counts(units, points, modulus)
        _chi(battery, case, f"chi2-mod{modulus}", observed, probabilities)


def _histogram_case(
    battery: Battery,
    case: str,
    values: Sequence[Any],
    edges: Sequence[str],
    weights: Sequence[str],
    step_text: str,
    viol: Violations,
) -> None:
    step = Decimal(step_text)
    bins = histogram_bins(edges, weights, step)
    counts, within = histogram_assign(values, bins, viol, case)
    probabilities = np.array([float(p) for _, _, p in bins])
    _chi(battery, case, "chi2-bins", counts, probabilities)
    uniform = stats.uniform(0.0, 1.0).cdf
    for index, (low, top, _) in enumerate(bins):
        points = int((top - low) / step) + 1
        if not within[index]:
            continue
        units = np.array([int((value - low) / step) for value in within[index]])
        if points >= 200:
            positions = (units + 0.5) / points
            ks = stats.kstest(positions, uniform)
            battery.add(
                case=case,
                test=f"ks-within-bin{index}",
                statistic=ks.statistic,
                pvalue=ks.pvalue,
                sample=case,
            )
            cvm = stats.cramervonmises(positions, uniform)
            battery.add(
                case=case,
                test=f"cvm-within-bin{index}",
                statistic=cvm.statistic,
                pvalue=cvm.pvalue,
                sample=case,
            )
        elif len(units) >= 5 * points and points >= 2:
            _chi(
                battery,
                case,
                f"chi2-within-bin{index}",
                np.bincount(units, minlength=points),
                np.full(points, 1.0 / points),
            )


def run_battery(tier: Tier) -> Tuple[Battery, Measured, Dict[str, Any]]:
    n = int(tier.pick(pr=15_000, nightly=150_000))
    seed = tier.seed(ROW_LABEL, "root")
    expose = expose_from_schema(SCHEMA)
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=n,
        fidelity=fidelity(COLUMNS),
    )
    root = root_for(H, seed % (1 << 63))
    columns = {name: generate_column(plan, name, root=root) for name in DRAWN}
    battery = Battery(name="generator families", q=Q)
    measured = Measured()
    viol: Violations = Counter()
    sizes: Dict[str, int] = {}

    observed, probabilities = categorical_counts(
        columns["cat_levels"], {k: _f(v) for k, v in LEVELS.items()}, viol, "cat_levels"
    )
    _chi(battery, "cat_levels", "chi2", observed, probabilities)

    observed, probabilities = categorical_counts(
        columns["cat_support"],
        {name: 1.0 / len(SUPPORT) for name in SUPPORT},
        viol,
        "cat_support",
    )
    _chi(battery, "cat_support", "chi2", observed, probabilities)

    for case, pmf in NORMAL_REFERENCES.items():
        _chi(
            battery,
            case,
            "chi2",
            grid_counts(pmf, columns[case], viol, case),
            pmf.probabilities,
        )

    for case, (mean, lo, hi) in POISSON_REFERENCES.items():
        observed, probabilities = poisson_counts(columns[case], mean, lo, hi, viol, case)
        _chi(battery, case, "chi2", observed, probabilities)

    for case, pmf in EXPONENTIAL_REFERENCES.items():
        _chi(
            battery,
            case,
            "chi2",
            grid_counts(pmf, columns[case], viol, case),
            pmf.probabilities,
        )

    # uniform INT: every integer of the range, both ends included
    units, points = grid_units(
        [Decimal(v) for v in columns["uni_int"]],
        Decimal(10),
        Decimal(41),
        Decimal(1),
        viol,
        "uni_int",
    )
    _chi(
        battery,
        "uni_int",
        "chi2",
        np.bincount(units, minlength=points),
        np.full(points, 1.0 / points),
    )

    _uniform_decimal_tests(battery, "uni_dec", columns["uni_dec"], viol)

    _histogram_case(
        battery, "hist_dec", columns["hist_dec"], HIST_DEC_BINS, HIST_DEC_WEIGHTS, "0.01", viol
    )
    _histogram_case(
        battery, "hist_int", columns["hist_int"], HIST_INT_BINS, HIST_INT_WEIGHTS, "1", viol
    )

    # nullFraction: the null count, and the values that are not null
    nulls = sum(1 for value in columns["null_cat"] if value is None)
    _null_case(battery, "null_cat", nulls, n, NULL_FRACTION, measured)
    present = [value for value in columns["null_cat"] if value is not None]
    observed, probabilities = categorical_counts(present, {"x": 0.6, "y": 0.4}, viol, "null_cat")
    _chi(battery, "null_cat", "chi2-nonnull", observed, probabilities)

    nulls = sum(1 for value in columns["null_norm"] if value is None)
    _null_case(battery, "null_norm", nulls, n, NULL_NORMAL_FRACTION, measured)
    present = [value for value in columns["null_norm"] if value is not None]
    pmf = NULL_NORMAL_REFERENCE
    _chi(
        battery,
        "null_norm",
        "chi2",
        grid_counts(pmf, present, viol, "null_norm"),
        pmf.probabilities,
    )

    for name, values in columns.items():
        sizes[name] = sum(1 for value in values if value is not None)
    return (
        battery,
        measured,
        {
            "n": n,
            "seed": seed,
            "sizes": sizes,
            "columns": columns,
            "violations": viol,
        },
    )


# The references of the continuous and count columns, from their declarations.
NORMAL_REFERENCES: Dict[str, GridPmf] = {
    # DECIMAL(10,2), declared mean and stddev
    "norm_dec_declared": grid_normal_reference(0.70, 0.12, 0.0, 1.0, 0.01),
    # ... the defaults: midpoint mean, range / 6 stddev
    "norm_dec_default": grid_normal_reference(1.2, 2.4 / 6.0, 0.0, 2.4, 0.01),
    # INT, scale 0, truncated at the declared 0 (a half-cell at the bound)
    "norm_int": grid_normal_reference(30.0, 15.0, 0.0, 80.0, 1.0),
}
POISSON_REFERENCES: Dict[str, Tuple[float, int | None, int | None]] = {
    "rate_int_free": (12.0, None, None),
    "rate_int_clamped": (4.5, 2, 9),
}
EXPONENTIAL_REFERENCES: Dict[str, GridPmf] = {
    "rate_dec_upper": exponential_reference(0.5, 0.0, 8.0, 0.01),
    "rate_dec_both": exponential_reference(0.8, 0.5, 6.0, 0.01),
}
NULL_NORMAL_REFERENCE = grid_normal_reference(4.0, 2.0, 0.0, 10.0, 0.01)


# ---------------------------------------------------------------------------
# Power: injected defects, run through the module's own statistics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Control:
    """A defect injected into the reference (or the data) and the statistic that must see it.

    ``p`` returns the p-value of the module's own test for the case against the
    defective reference. "Caught" means that p-value is at or below the battery's
    rank-one critical value, at which the real battery rejects whatever else is
    in it. ``nightly_only`` marks defects the pull-request size cannot see.
    """

    name: str
    defect: str
    p: Callable[[Dict[str, Any]], float]
    nightly_only: bool = False


def _grid_p(case: str, pmf: GridPmf) -> Callable[[Dict[str, Any]], float]:
    return lambda columns: _p_chi(
        grid_counts(pmf, columns[case], Counter(), case), pmf.probabilities
    )


def _levels_p(case: str, weights: Dict[str, float]) -> Callable[[Dict[str, Any]], float]:
    return lambda columns: _p_chi(*categorical_counts(columns[case], weights, Counter(), case))


def _poisson_p(case: str, mean: float, lo, hi) -> Callable[[Dict[str, Any]], float]:
    return lambda columns: _p_chi(*poisson_counts(columns[case], mean, lo, hi, Counter(), case))


def _null_p(case: str, fraction: float) -> Callable[[Dict[str, Any]], float]:
    def p(columns: Dict[str, Any]) -> float:
        values = columns[case]
        nulls = sum(1 for value in values if value is None)
        return float(stats.binomtest(nulls, len(values), fraction).pvalue)

    return p


def _no_lower_clamp_mass() -> GridPmf:
    base = exponential_reference(0.8, 0.5, 6.0, 0.01)
    expon = stats.expon(scale=1.0 / 0.8)
    probabilities = base.probabilities.copy()
    probabilities[0] = expon.cdf(0.505) - expon.cdf(0.495)  # one cell's mass, not everything below
    return GridPmf(lo=base.lo, step=base.step, probabilities=probabilities / probabilities.sum())


def _swapped_hist_weights_p(columns: Dict[str, Any]) -> float:
    weights = list(HIST_DEC_WEIGHTS)
    weights[1], weights[2] = weights[2], weights[1]  # 0.39 <-> 0.3
    bins = histogram_bins(HIST_DEC_BINS, weights, Decimal("0.01"))
    counts, _ = histogram_assign(
        columns["hist_dec"],
        histogram_bins(HIST_DEC_BINS, HIST_DEC_WEIGHTS, Decimal("0.01")),
        Counter(),
        "hist_dec",
    )
    return _p_chi(counts, np.array([float(p) for _, _, p in bins]))


def _every_tenth_point_p(columns: Dict[str, Any]) -> float:
    """A sampler that only ever returns every tenth grid point, through the module's own tests."""
    step = Decimal("0.001")
    snapped = [(Decimal(v) / step // 10 * 10) * step for v in columns["uni_dec"]]
    battery = Battery(name="control", q=Q)
    _uniform_decimal_tests(battery, "uni_dec", snapped, Counter())
    return min(entry.pvalue for entry in battery.entries)


def _levels(alpha: float, beta: float) -> Dict[str, float]:
    weights = {k: _f(v) for k, v in LEVELS.items()}
    weights["alpha"], weights["beta"] = alpha, beta
    return weights


CONTROLS: List[Control] = [
    Control(
        "norm_dec_declared-mean+2%",
        "the declared mean 0.70 read 2% high",
        _grid_p("norm_dec_declared", grid_normal_reference(0.714, 0.12, 0.0, 1.0, 0.01)),
    ),
    Control(
        "rate_int_free-poisson-mean+2%",
        "the Poisson mean 12 read 2% high",
        _poisson_p("rate_int_free", 12.0 * 1.02, None, None),
    ),
    Control(
        "cat_levels-3pt-moved",
        "0.03 of probability moved from alpha to beta",
        _levels_p("cat_levels", _levels(0.47, 0.33)),
    ),
    Control(
        "rate_dec_both-lower-clamp-mass-dropped",
        "values below range.min not clamped onto it: the lower cell holds one cell's mass",
        _grid_p("rate_dec_both", _no_lower_clamp_mass()),
    ),
    Control(
        "hist_dec-bin-weights-swapped",
        "bin weights 0.39 and 0.30 swapped",
        _swapped_hist_weights_p,
    ),
    Control(
        "null_cat-fraction-0.23",
        "nullFraction 0.23 where 0.20 is declared",
        _null_p("null_cat", 0.23),
    ),
    Control(
        "uni_dec-every-tenth-point",
        "a uniform sampler snapped to every tenth grid point",
        _every_tenth_point_p,
    ),
    Control(
        "rate_dec_both-rate+10%",
        "the exponential rate 0.8 read 10% high",
        _grid_p("rate_dec_both", exponential_reference(0.8 * 1.10, 0.5, 6.0, 0.01)),
    ),
    Control(
        "norm_dec_declared-floor-not-round",
        "values floored to the grid instead of rounded (a half-step shift)",
        _grid_p("norm_dec_declared", grid_normal_reference(0.70 - 0.005, 0.12, 0.0, 1.0, 0.01)),
        nightly_only=True,
    ),
    Control(
        "norm_dec_default-mean+2%",
        "the midpoint mean 1.2 read 2% high",
        _grid_p("norm_dec_default", grid_normal_reference(1.224, 2.4 / 6.0, 0.0, 2.4, 0.01)),
        nightly_only=True,
    ),
    Control(
        "norm_int-mean+2%",
        "the declared INT mean 30 read 2% high",
        _grid_p("norm_int", grid_normal_reference(30.6, 15.0, 0.0, 80.0, 1.0)),
        nightly_only=True,
    ),
    Control(
        "cat_levels-1pt-moved",
        "0.01 of probability moved from alpha to beta",
        _levels_p("cat_levels", _levels(0.49, 0.31)),
        nightly_only=True,
    ),
    Control(
        "null_cat-fraction-0.21",
        "nullFraction 0.21 where 0.20 is declared",
        _null_p("null_cat", 0.21),
        nightly_only=True,
    ),
]


# ---------------------------------------------------------------------------
# Fixtures and tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def run(tier: Tier) -> Tuple[Battery, Measured, Dict[str, Any]]:
    return run_battery(tier)


CASES = [
    "cat_levels",
    "cat_support",
    "norm_dec_declared",
    "norm_dec_default",
    "norm_int",
    "rate_int_free",
    "rate_int_clamped",
    "rate_dec_upper",
    "rate_dec_both",
    "uni_int",
    "uni_dec",
    "hist_dec",
    "hist_int",
    "null_cat",
    "null_norm",
]


@pytest.mark.parametrize("name", CASES)
def test_generator_family_fits_its_declaration(
    name: str,
    run: Tuple[Battery, Measured, Dict[str, Any]],
    tier: Tier,
    accuracy_log: ResultLog,
) -> None:
    battery, measured, info = run
    decisions = [d for d in battery.decisions() if d.entry.case == name]
    assert decisions, f"no test was measured for {name}"
    rejected = [d for d in decisions if d.rejected]
    violations = int(info["violations"][name])
    accuracy_log.record(
        family=FAMILY,
        case=f"generator/{name}",
        criterion=CRITERION,
        passed=not rejected and violations == 0,
        seed=info["seed"],
        n=info["sizes"][name],
        metrics={
            "method": battery.method,
            "violations": violations,
            "tests": {
                d.entry.test: {
                    "statistic": d.entry.statistic,
                    "pvalue": d.entry.pvalue,
                    "adjusted": d.adjusted,
                }
                for d in decisions
            },
            **measured.cases.get(name, {}),
        },
    )
    assert violations == 0, f"{name}: {violations} draws fell outside the declared support"
    assert battery.method == "by"
    assert not rejected, battery.summary()


def test_the_whole_battery_is_clean_and_every_case_is_parametrised(
    run: Tuple[Battery, Measured, Dict[str, Any]], accuracy_log: ResultLog
) -> None:
    """A case added to the battery but missing from ``CASES`` would be adjusted in, never asserted."""
    battery, _, info = run
    in_battery = {entry.case for entry in battery.entries}
    rejected = battery.rejected()
    unparametrised = sorted(in_battery - set(CASES))
    unmeasured = sorted(set(CASES) - in_battery)
    passed = (
        not rejected
        and not unparametrised
        and not unmeasured
        and not sum(info["violations"].values())
    )
    accuracy_log.record(
        family=FAMILY,
        case="generator/battery",
        criterion=CRITERION,
        passed=passed,
        seed=info["seed"],
        n=info["n"],
        metrics={
            "tests": len(battery.entries),
            "rejected": [f"{d.entry.case}/{d.entry.test}" for d in rejected],
            "unparametrised": unparametrised,
            "unmeasured": unmeasured,
            "violations": {k: v for k, v in info["violations"].items() if v},
        },
    )
    assert not unparametrised, f"cases in the battery with no test of their own: {unparametrised}"
    assert not unmeasured, f"parametrised cases with no statistic: {unmeasured}"
    assert not sum(info["violations"].values()), dict(info["violations"])
    assert not rejected, battery.summary()


@pytest.mark.parametrize("control", CONTROLS, ids=[c.name for c in CONTROLS])
def test_the_battery_catches_an_injected_defect(
    control: Control,
    run: Tuple[Battery, Measured, Dict[str, Any]],
    tier: Tier,
    accuracy_log: ResultLog,
) -> None:
    if control.nightly_only:
        tier.only_nightly(f"the pull-request size cannot see: {control.defect}")
    battery, _, info = run
    threshold = rank_one_threshold(Q, len(battery.entries), battery.method)
    pvalue = control.p(info["columns"])
    caught = pvalue <= threshold
    accuracy_log.record(
        family=FAMILY,
        case=f"generator/power/{control.name}",
        criterion="the module's own statistic rejects the defect at the battery's rank-one value",
        passed=caught,
        seed=info["seed"],
        n=info["n"],
        metrics={"defect": control.defect, "pvalue": pvalue, "threshold": threshold},
    )
    assert caught, f"{control.defect}: p={pvalue:.3g} above the rank-one value {threshold:.3g}"


def test_every_draw_lies_inside_its_declared_range_at_its_scale(
    run: Tuple[Battery, Measured, Dict[str, Any]], tier: Tier, accuracy_log: ResultLog
) -> None:
    """The structural half of the clamping claim, exact rather than statistical."""
    _, _, info = run
    columns = info["columns"]
    bounds = {
        "norm_dec_declared": ("0.00", "1.00", 2),
        "norm_dec_default": ("0.00", "2.40", 2),
        "rate_dec_upper": ("0.00", "8.00", 2),
        "rate_dec_both": ("0.50", "6.00", 2),
        "uni_dec": ("0.000", "100.000", 3),
        "null_norm": ("0.00", "10.00", 2),
        "norm_int": ("0", "80", 0),
        "rate_int_clamped": ("2", "9", 0),
        "uni_int": ("10", "41", 0),
    }
    outside = 0
    for name, (lo, hi, scale) in bounds.items():
        for value in columns[name]:
            if value is None:
                continue
            number = Decimal(value)
            if not Decimal(lo) <= number <= Decimal(hi):
                outside += 1
            elif scale and -number.as_tuple().exponent != scale:
                outside += 1
    accuracy_log.record(
        family=FAMILY,
        case="generator/ranges",
        criterion="no draw outside its declared range or off its declared scale",
        passed=outside == 0,
        seed=info["seed"],
        n=info["n"],
        metrics={"outside": outside},
    )
    assert outside == 0


def test_the_uniform_range_reaches_both_declared_ends(
    run: Tuple[Battery, Measured, Dict[str, Any]], tier: Tier, accuracy_log: ResultLog
) -> None:
    _, _, info = run
    ints = set(info["columns"]["uni_int"])
    decs = [Decimal(v) for v in info["columns"]["uni_dec"]]
    reached = {10, 41} <= ints and min(decs) < Decimal("0.100") and max(decs) > Decimal("99.900")
    accuracy_log.record(
        family=FAMILY,
        case="generator/uniform-ends",
        criterion="the integer range reaches both of its declared ends",
        passed=reached,
        seed=info["seed"],
        n=info["n"],
        metrics={"min": str(min(decs)), "max": str(max(decs))},
    )
    assert reached


# ---------------------------------------------------------------------------
# Identifiers: derived, never drawn
# ---------------------------------------------------------------------------

HEX = re.compile(r"^[0-9a-f]+$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-8[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def test_identifier_columns_are_distinct_and_never_random(
    tier: Tier, accuracy_log: ResultLog
) -> None:
    n = int(tier.pick(pr=15_000, nightly=150_000))
    seed = tier.seed(ROW_LABEL, "identifiers")
    expose = expose_from_schema(
        [
            {"name": "ident", "type": "STRING", "required": True, "semanticType": "identifier"},
            {"name": "ident_uuid", "type": "uuid", "required": True},
            {"name": "drawn", "type": "INT"},
        ]
    )
    plan = build_plan(
        expose,
        sim_id=SIM_ID,
        seam_digest=H,
        rows=n,
        fidelity=fidelity({"drawn": {"range": {"min": "0", "max": "1000000"}}}),
    )
    first = {
        name: generate_column(plan, name, root=root_for(H, seed % (1 << 63)))
        for name in ("ident", "ident_uuid", "drawn")
    }
    again = {
        name: generate_column(plan, name, root=root_for(H, seed % (1 << 63)))
        for name in ("ident", "ident_uuid", "drawn")
    }
    reseeded = {
        name: generate_column(plan, name, root=root_for(H, (seed + 1) % (1 << 63)))
        for name in ("ident", "ident_uuid", "drawn")
    }

    distinct = all(len(set(first[name])) == n for name in ("ident", "ident_uuid"))
    shaped = all(len(v) == 32 and HEX.match(v) for v in first["ident"]) and all(
        UUID.match(v) for v in first["ident_uuid"]
    )
    repeatable = first == again
    # The seed is not in the pre-image of an identifier; it is in every drawn value's.
    seed_free = (
        first["ident"] == reseeded["ident"] and first["ident_uuid"] == reseeded["ident_uuid"]
    )
    seed_moves_draws = first["drawn"] != reseeded["drawn"]
    passed = distinct and shaped and repeatable and seed_free and seed_moves_draws
    accuracy_log.record(
        family=FAMILY,
        case="generator/identifiers",
        criterion=(
            "identifiers are pairwise distinct, well formed, identical across runs and seeds, "
            "while a drawn column moves with the seed"
        ),
        passed=passed,
        seed=seed,
        n=n,
        metrics={
            "distinct": distinct,
            "shaped": shaped,
            "repeatable": repeatable,
            "seedFree": seed_free,
            "seedMovesDraws": seed_moves_draws,
        },
    )
    assert distinct, "two rows share an identifier"
    assert shaped, "an identifier is not 32 hex characters (or an RFC 9562 version-8 UUID)"
    assert repeatable, "an identifier changed between two runs of one plan"
    assert seed_free, "an identifier depends on the run seed"
    assert seed_moves_draws, "a drawn column did not move with the seed (the control failed)"


def test_a_helper_assumption_the_plan_picked_the_families_we_meant() -> None:
    """Fail loudly if a declaration silently resolved to another family."""
    expose = expose_from_schema(SCHEMA)
    plan = build_plan(expose, sim_id=SIM_ID, seam_digest=H, rows=1, fidelity=fidelity(COLUMNS))
    picked = {
        name: (plan.column_plan(name).family.value if plan.column_plan(name).family else None)
        for name in [c["name"] for c in SCHEMA]
    }
    assert picked["cat_levels"] == "categorical"
    assert picked["cat_support"] == "categorical"
    assert picked["norm_dec_declared"] == "bounded-normal"
    assert picked["norm_dec_default"] == "bounded-normal"
    assert picked["rate_int_free"] == "rate"
    assert picked["rate_dec_both"] == "rate"
    assert picked["uni_int"] == "uniform/empirical"
    assert picked["uni_dec"] == "uniform/empirical"
    assert picked["hist_dec"] == "uniform/empirical"
    assert picked["ident"] is None and picked["ident_uuid"] is None
    assert picked["norm_int"] == "bounded-normal"
