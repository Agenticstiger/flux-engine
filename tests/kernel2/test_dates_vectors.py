# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The vector file of the dates stream (ADR 0024 section 6, KERNEL-SPEC-2 section 12).

``tests/vectors/kernel2-dates-vectors.json`` pins what the dates a lifetime's ``duration`` or a
``within`` entry sets draw on: the rule stream ``<row path> + ("rule", R)`` with ``R`` the canonical
JSON of ``["dates", <column>]``, for a DATE column and a TIMESTAMP column of a fixed root. For each
column it holds the key label, the key, and the counter and block of positions 0, 1 and 257 for rows
0, 1 and 2**40; for a hidden column it holds the cell path's key and block, the path no rule stream
shares. It also pins the duration draw: thresholds, the slot a ``uniform53`` picks and the value,
for a fixed histogram, including the ``open`` slot and an end past the window.

For a date within a linked row's lifetime it pins the ``bounded`` draw at position 1 over the
values of ``J`` (a DATE column and a TIMESTAMP column), and the draw of an offset histogram cut to
``J``: the thresholds and the values each bin keeps inside, and the value a ``uniform53`` picks.

Every value is computed here from ``tests/kernel2/reference.py``'s Philox and an independent
reading of the specification (a linear search over the slots, a ``range`` indexed by position),
never typed and never read from the engine, and the first test fails until the file matches.
The second holds the engine's own code to the file. Regenerate with::

    python -m tests.kernel2.test_dates_vectors

For the seasonal rates of ADR 0025 section 2 it pins the thinning positions of the same two columns: the
test at position 2 (proposal 0's), the value of proposal 1 at position 3 and the test of proposal 127 at
position 256, and a thinning run over a fixed intensity, the proposals tried and the one accepted. The
reference intensity is written out here with ``datetime``, never read from the engine.

Existing vector files are not touched, and the entries of this file that exist stay as they are.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.core.seedtree import derive_root
from flux_engine.generator.dates import (
    DAY_MS,
    Duration,
    cut_table,
    draw_within,
    duration_table,
    offset_table,
    unit_of,
)
from flux_engine.generator.paths import CellStreams, dates_rule_text
from flux_engine.seam.dates import Histogram
from tests.kernel2 import reference as k2

pytestmark = pytest.mark.unit

DATES = Path(__file__).resolve().parents[1] / "vectors" / "kernel2-dates-vectors.json"
REGENERATE = "python -m tests.kernel2.test_dates_vectors"

SEAM = hashlib.sha256(b"kernel2 dates vectors: H").digest()
SEED = 20261122
ROOT = k2.root(SEAM, SEED)
SIM, EXPOSE = "s", "customer"
ROWS = (0, 1, 2**40)
POSITIONS = (0, 1, 257)
COLUMNS = (("ended_on", "DATE"), ("ended_at", "TIMESTAMP"))
HIDDEN_COLUMN = "churned_on"

#: Weights 5, 3, 1 and an open weight 1: total 10, so a bin and the open slot each hold a tenth or more.
HISTOGRAM = {
    "bins": ["0", "7", "30", "90"],
    "binWeights": ["5", "3", "1"],
    "open": "1",
}
UPPER_HISTOGRAM = {**HISTOGRAM, "closed": "upper"}
TWO_POW_53 = 1 << 53


def _hex(word: int) -> str:
    return f"{word:016x}"


def _block_hex(block: Sequence[int]) -> List[str]:
    return [_hex(word) for word in block]


# ---------------------------------------------------------------------------
# The streams
# ---------------------------------------------------------------------------


def _key_of_label(label: bytes) -> k2.Key:
    material = k2.SEED_TREE_VERSION.encode("utf-8") + k2.NUL + ROOT + k2.NUL + label
    return k2._key_of(hashlib.sha256(material).digest())


def _rule_label(column: str) -> bytes:
    """``("sim", s, "gen", exposeId, "row", <row>, "rule", R)``'s key label: the row part empty."""
    rule = json.dumps(["dates", column], separators=(",", ":"))
    parts = ["sim", SIM, "gen", EXPOSE, "row", "", "rule", rule]
    return k2.SEPARATOR.join(part.encode("utf-8") for part in parts)


def _draws(key: k2.Key, positions: Sequence[int] = POSITIONS) -> List[Dict[str, Any]]:
    found = []
    for row in ROWS:
        draws = []
        for position in positions:
            counter = (row, 0, position, 0)
            block = k2.philox_block(counter, key)
            draws.append(
                {
                    "position": position,
                    "counter": [str(word) for word in counter],
                    "block": _block_hex(block),
                    "uniform53": str(k2.uniform53(block[0])),
                }
            )
        found.append({"row": str(row), "draws": draws})
    return found


def _stream_vectors() -> List[Dict[str, Any]]:
    vectors = []
    for column, kind in COLUMNS:
        label = _rule_label(column)
        key = _key_of_label(label)
        vectors.append(
            {
                "column": column,
                "type": kind,
                "rule": json.dumps(["dates", column], separators=(",", ":")),
                "keyLabel": label.hex(),
                "key": _block_hex(key),
                "rows": _draws(key),
            }
        )
    return vectors


def _cell_vector() -> Dict[str, Any]:
    """A hidden column draws on the cell path of its own name, which no rule stream shares."""
    path = ("sim", SIM, "gen", EXPOSE, "row", 0, HIDDEN_COLUMN)
    label, _ordinals = k2.split_path(path)
    key = k2.stream_key(ROOT, path)
    rows = []
    for row in ROWS:
        block = k2.philox_block((row, 0, 0, 0), key)
        rows.append({"row": str(row), "block": _block_hex(block)})
    return {"column": HIDDEN_COLUMN, "keyLabel": label.hex(), "key": _block_hex(key), "rows": rows}


# ---------------------------------------------------------------------------
# The duration draw, read from the specification and not from the engine
# ---------------------------------------------------------------------------


def _slots(histogram: Dict[str, Any]) -> Tuple[List[Fraction], List[Tuple[int, int]]]:
    """The weight of each slot (the open slot last) and the whole days each bin holds."""
    edges = [int(edge) for edge in histogram["bins"]]
    weights = [Fraction(weight) for weight in histogram["binWeights"]]
    if "open" in histogram:
        weights.append(Fraction(histogram["open"]))
    upper = histogram.get("closed", "lower") == "upper"
    days = []
    for n in range(len(edges) - 1):
        if upper:
            days.append((edges[n] if n == 0 else edges[n] + 1, edges[n + 1]))
        else:
            days.append((edges[n], edges[n + 1] if n == len(edges) - 2 else edges[n + 1] - 1))
    return weights, days


def _thresholds(weights: Sequence[Fraction]) -> List[int]:
    total = sum(weights, Fraction(0))
    cuts, running = [], Fraction(0)
    for weight in weights:
        running += weight
        cuts.append((running * TWO_POW_53) // total)
    return cuts


def _reference_end(histogram: Dict[str, Any], kind: str, start: int, u: int) -> Optional[int]:
    weights, days = _slots(histogram)
    cuts = _thresholds(weights)
    unit = 1 if kind == "DATE" else DAY_MS
    previous = 0
    for slot, cut in enumerate(cuts):
        if u < cut:
            if slot == len(days):
                return None
            first, last = days[slot]
            values = range(start + first * unit, start + (last + 1) * unit)
            return values[(u - previous) * len(values) // (cut - previous)]
        previous = cut
    raise AssertionError("u is below 2**53, so a slot holds it")


def _probes(histogram: Dict[str, Any]) -> List[int]:
    cuts = _thresholds(_slots(histogram)[0])
    found = {0, TWO_POW_53 - 1, 12_345_678_901_234}
    for cut in cuts[:-1]:
        found.update({cut - 1, cut})
    found.add((cuts[0] + cuts[1]) // 2)
    return sorted(found)


def _text(value: Optional[int]) -> Optional[str]:
    return None if value is None else str(value)


def _past_window(end: Optional[int], window: Optional[Tuple[int, int]], past: str) -> Optional[int]:
    if end is not None and window is not None and end > window[1] and past == "open":
        return None
    return end


#: 2025-12-31 in epoch days, and 2025-12-31T23:59:59Z in epoch milliseconds: the window's last day
#: and instant. A start 20 days before them puts the 30-day bin's ends past the window.
LAST_DAY = 20_453
LAST_MS = 1_767_225_599_000


def _duration_vectors() -> List[Dict[str, Any]]:
    vectors = []
    for name, histogram, kind, start, window in (
        ("a DATE end, lower closed", HISTOGRAM, "DATE", 20_000, None),
        ("a DATE end, upper closed", UPPER_HISTOGRAM, "DATE", 20_000, None),
        ("a TIMESTAMP end, lower closed", HISTOGRAM, "TIMESTAMP", 1_700_000_000_000, None),
        ("a DATE end near the window", HISTOGRAM, "DATE", LAST_DAY - 20, (19_000, LAST_DAY)),
        (
            "a TIMESTAMP end near the window",
            HISTOGRAM,
            "TIMESTAMP",
            LAST_MS - 20 * DAY_MS,
            (1_735_689_600_000, LAST_MS),
        ),
    ):
        cases = []
        for u in _probes(histogram):
            end = _reference_end(histogram, kind, start, u)
            cases.append(
                {
                    "uniform53": str(u),
                    "end": _text(end),
                    "open": _text(_past_window(end, window, "open")),
                    "kept": _text(_past_window(end, window, "kept")),
                }
            )
        thresholds = _thresholds(_slots(histogram)[0])
        vectors.append(
            {
                "name": name,
                "type": kind,
                "histogram": histogram,
                "start": str(start),
                "window": None if window is None else [str(window[0]), str(window[1])],
                "thresholds": [str(cut) for cut in thresholds],
                "cases": cases,
            }
        )
    return vectors


# ---------------------------------------------------------------------------
# A date within a linked row's lifetime: the bounded draw and the cut histogram
# ---------------------------------------------------------------------------

#: ``J`` of a DATE column and of a TIMESTAMP column: a month, and a single value.
BOUNDED_INTERVALS = {
    "DATE": ((20_089, 20_119), (20_453, 20_453)),
    "TIMESTAMP": (
        (1_735_689_600_000, 1_735_689_600_000 + 31 * DAY_MS - 1),
        (1_767_225_599_000, 1_767_225_599_000),
    ),
}


def _bounded_vectors() -> List[Dict[str, Any]]:
    """The ``bounded`` draw at position 1 of each date column's stream, over the values of ``J``."""
    vectors = []
    for column, kind in COLUMNS:
        key = _key_of_label(_rule_label(column))
        rows = []
        for row in ROWS:
            cases = []
            for low, high in BOUNDED_INTERVALS[kind]:
                n = high - low + 1
                drawn = k2.bounded(
                    lambda attempt, row=row, key=key: k2.philox_block((row, 0, 1, attempt), key)[0],
                    n,
                )
                cases.append(
                    {"low": str(low), "high": str(high), "n": str(n), "value": str(low + drawn)}
                )
            rows.append({"row": str(row), "cases": cases})
        vectors.append({"column": column, "type": kind, "position": 1, "rows": rows})
    return vectors


#: An exponential dwell (ADR 0025 section 3): a mean of 20 days, a quarter of the rows with no end,
#: and a start for each column's grain.
DWELL_MEAN, DWELL_OPEN = Fraction(20), Fraction(1, 4)
DWELL_STARTS = {"DATE": 20_000, "TIMESTAMP": 1_700_000_000_000}


def _reference_dwell(u: int, kind: str) -> Optional[int]:
    """Whole units after the start, or ``None`` for no end: the open share, then section 5.4's exponential."""
    cut = DWELL_OPEN.numerator * TWO_POW_53 // DWELL_OPEN.denominator
    if u < cut:
        return None
    v = (u - cut) * TWO_POW_53 // (TWO_POW_53 - cut)
    unit = 1 if kind == "DATE" else DAY_MS
    rate = Decimal(float(Fraction(1) / (DWELL_MEAN * unit)))
    return int(k2.exponential(np.array([v], dtype=np.uint64), rate, 0)[0])


def _dwell_case(u: int, kind: str) -> Dict[str, Any]:
    units = _reference_dwell(u, kind)
    return {
        "uniform53": str(u),
        "units": _text(units),
        "end": _text(None if units is None else DWELL_STARTS[kind] + units),
    }


def _dwell_vectors() -> List[Dict[str, Any]]:
    """The exponential dwell draw at position 1 of each date column's stream, and at its edges."""
    cut = DWELL_OPEN.numerator * TWO_POW_53 // DWELL_OPEN.denominator
    probes = sorted({0, cut - 1, cut, cut + 1, 12_345_678_901_234, TWO_POW_53 - 1})
    vectors = []
    for column, kind in COLUMNS:
        key = _key_of_label(_rule_label(column))
        rows = []
        for row in ROWS:
            u = k2.uniform53(k2.philox_block((row, 0, 1, 0), key)[0])
            rows.append({"row": str(row), **_dwell_case(u, kind)})
        vectors.append(
            {
                "column": column,
                "type": kind,
                "mean": str(DWELL_MEAN),
                "open": "0.25",
                "start": str(DWELL_STARTS[kind]),
                "position": 1,
                "rows": rows,
                "probes": [_dwell_case(u, kind) for u in probes],
            }
        )
    return vectors


HOLD_TARGETS = ("0.5", "12.5", "50", "99.9")


def _hold_vectors() -> List[Dict[str, Any]]:
    """Position 0: ``bernoulli(1 - t / 100)``, the integer comparison ``u < floor(p * 2**53)``."""
    vectors = []
    for column, kind in COLUMNS:
        key = _key_of_label(_rule_label(column))
        rows = []
        for row in ROWS:
            u = k2.uniform53(k2.philox_block((row, 0, 0, 0), key)[0])
            cases = []
            for target in HOLD_TARGETS:
                threshold = (Fraction(100) - Fraction(target)) / 100 * TWO_POW_53 // 1
                cases.append(
                    {"target": target, "threshold": str(int(threshold)), "holds": u < threshold}
                )
            rows.append({"row": str(row), "uniform53": str(u), "cases": cases})
        vectors.append({"column": column, "type": kind, "position": 0, "rows": rows})
    return vectors


#: The declared range a candidate is drawn uniformly over, for a DATE and a TIMESTAMP column.
CANDIDATE_RANGES = {"DATE": (19_000, 20_453), "TIMESTAMP": (1_735_689_600_000, 1_767_225_599_000)}
#: Candidates 0 and 3 of a column whose draw is one call (``m = 1``): positions 257 and 260.
CANDIDATE_INDEXES = (0, 3)


def _candidate_vectors() -> List[Dict[str, Any]]:
    """Candidate ``j`` of a uniform draw over the declared range: one ``bounded`` at ``257 + j * m``."""
    vectors = []
    for column, kind in COLUMNS:
        key = _key_of_label(_rule_label(column))
        low, high = CANDIDATE_RANGES[kind]
        rows = []
        for row in ROWS:
            cases = []
            for j in CANDIDATE_INDEXES:
                position = 257 + j
                drawn = k2.bounded(
                    lambda attempt, row=row, key=key, position=position: k2.philox_block(
                        (row, 0, position, attempt), key
                    )[0],
                    high - low + 1,
                )
                cases.append({"candidate": j, "position": position, "value": str(low + drawn)})
            rows.append({"row": str(row), "cases": cases})
        vectors.append(
            {
                "column": column,
                "type": kind,
                "m": 1,
                "range": [str(low), str(high)],
                "rows": rows,
            }
        )
    return vectors


def _bin_span(edges: List[int], n: int, upper: bool) -> Tuple[int, int]:
    """The first and the last whole day offset bin *n* holds, from the specification."""
    if upper:
        return (edges[n] if n == 0 else edges[n] + 1), edges[n + 1]
    return edges[n], (edges[n + 1] if n == len(edges) - 2 else edges[n + 1] - 1)


def _cut_slots(
    histogram: Dict[str, Any], kind: str, start: int, interval: Tuple[int, Optional[int]]
) -> List[Tuple[Fraction, "range"]]:
    """The weight each bin keeps inside the interval and the values it keeps, from the specification.

    A bin's day ``d`` holds the values whose offset from the start is ``d`` whole days, so a bin
    holds the ``range`` of values from its first day to the end of its last. It keeps its weight
    times its values inside over all its values.
    """
    unit = 1 if kind == "DATE" else DAY_MS
    edges = [int(edge) for edge in histogram["bins"]]
    upper = histogram.get("closed", "lower") == "upper"
    low, high = interval
    kept = []
    for n, weight in enumerate(histogram["binWeights"]):
        first, last = _bin_span(edges, n, upper)
        whole = range(start + first * unit, start + (last + 1) * unit)
        inside = range(
            max(whole.start, low), (whole.stop if high is None else min(whole.stop, high + 1))
        )
        if len(inside) == 0 or Fraction(weight) == 0:
            continue
        kept.append((Fraction(weight) * Fraction(len(inside), len(whole)), inside))
    return kept


def _cut_value(slots: Sequence[Tuple[Fraction, "range"]], u: int) -> int:
    cuts = _thresholds([weight for weight, _ in slots])
    previous = 0
    for (_, values), cut in zip(slots, cuts, strict=True):
        if u < cut:
            return values[(u - previous) * len(values) // (cut - previous)]
        previous = cut
    raise AssertionError("u is below 2**53, so a bin holds it")


#: A fixed histogram of weights 1 and 3 (lower closed), and the cases that cut it to an interval.
CUT_HISTOGRAM = {"bins": ["0", "10", "30"], "binWeights": ["1", "3"]}
CUT_UPPER = {"bins": ["0", "7", "30", "90"], "binWeights": ["5", "0", "1"], "closed": "upper"}
HOUR = 3_600_000


def _cut_vectors() -> List[Dict[str, Any]]:
    vectors = []
    for name, histogram, kind, start, interval in (
        ("a DATE column, a bin cut at both ends", CUT_HISTOGRAM, "DATE", 20_000, (20_005, 20_020)),
        ("a DATE column, upper closed, a zero weight", CUT_UPPER, "DATE", 20_000, (20_000, 20_100)),
        (
            "a TIMESTAMP column counting from an hour past midnight",
            CUT_HISTOGRAM,
            "TIMESTAMP",
            1_700_000_000_000 + HOUR,
            (1_700_000_000_000 + HOUR + 9 * DAY_MS, 1_700_000_000_000 + HOUR + 12 * DAY_MS - 1),
        ),
        (
            "a TIMESTAMP column of a DATE parent",
            CUT_HISTOGRAM,
            "TIMESTAMP",
            20_000 * DAY_MS,
            (20_003 * DAY_MS + 1_000, 20_009 * DAY_MS + 5_000),
        ),
        (
            "a DATE column, an interval with no upper end",
            CUT_HISTOGRAM,
            "DATE",
            20_000,
            (20_020, None),
        ),
    ):
        slots = _cut_slots(histogram, kind, start, interval)
        thresholds = _thresholds([weight for weight, _ in slots])
        probes = {0, TWO_POW_53 - 1}
        for cut in thresholds[:-1]:
            probes.update({cut - 1, cut})
        probes.add(TWO_POW_53 // 2)
        vectors.append(
            {
                "name": name,
                "type": kind,
                "histogram": histogram,
                "parentStart": str(start),
                "interval": [str(interval[0]), None if interval[1] is None else str(interval[1])],
                "thresholds": [str(cut) for cut in thresholds],
                "spans": [[str(values[0]), str(values[-1])] for _, values in slots],
                "cases": [
                    {"uniform53": str(u), "value": str(_cut_value(slots, u))}
                    for u in sorted(probes)
                ],
            }
        )
    return vectors


# ---------------------------------------------------------------------------
# Seasonal rates: the thinning positions and a thinning run (ADR 0025 section 2)
# ---------------------------------------------------------------------------

#: The positions the thinning of ADR 0025 takes: the test of proposal 0, the value of proposal 1 and
#: the test of proposal 127 (``2 + 2k``, ``1 + 2k``, ``2 + 2k``).
THINNING_POSITIONS = (2, 3, 256)
PROPOSALS = 128
#: The intensity of a DATE column and of a TIMESTAMP column: weights as the entry writes them.
THINNING_RATES = {
    "DATE": {"dayOfWeek": ["1", "0", "0", "0", "0", "0", "0"]},
    "TIMESTAMP": {
        "month": ["2"] + ["1"] * 11,
        "dayOfWeek": ["1", "1", "1", "1", "1", "0", "0"],
        "hourOfWeek": ["4" if 9 <= hour < 17 else "1" for _day in range(7) for hour in range(24)],
    },
}


def _reference_lambda(rates: Dict[str, List[str]], kind: str, value: int) -> Fraction:
    """``lambda(t)``: the month, weekday and hour of week of *t* in UTC, read with ``datetime``."""
    if kind == "DATE":
        moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=value)
    else:
        moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=value)
    found = Fraction(1)
    if "month" in rates:
        found *= Fraction(rates["month"][moment.month - 1])
    if "dayOfWeek" in rates:
        found *= Fraction(rates["dayOfWeek"][moment.weekday()])
    if "hourOfWeek" in rates:
        found *= Fraction(rates["hourOfWeek"][moment.weekday() * 24 + moment.hour])
    return found


def _reference_lambda_max(rates: Dict[str, List[str]]) -> Fraction:
    found = Fraction(1)
    for weights in rates.values():
        found *= max(Fraction(weight) for weight in weights)
    return found


def _thinning_position_vectors() -> List[Dict[str, Any]]:
    vectors = []
    for column, kind in COLUMNS:
        key = _key_of_label(_rule_label(column))
        low, high = BOUNDED_INTERVALS[kind][0]
        n = high - low + 1
        rows = _draws(key, THINNING_POSITIONS)
        for held in rows:
            row = int(held["row"])
            drawn = k2.bounded(
                lambda attempt, row=row, key=key: k2.philox_block((row, 0, 3, attempt), key)[0], n
            )
            held["proposal1"] = {
                "position": 3,
                "low": str(low),
                "n": str(n),
                "value": str(low + drawn),
            }
        vectors.append({"column": column, "type": kind, "rows": rows})
    return vectors


def _thinning_run(key: k2.Key, row: int, kind: str) -> Dict[str, Any]:
    """Thin one row of a column over ``J``: proposal ``k`` at ``1 + 2k``, its test at ``2 + 2k``."""
    rates = THINNING_RATES[kind]
    low, high = BOUNDED_INTERVALS[kind][0]
    n = high - low + 1
    peak = _reference_lambda_max(rates)
    tried = []
    accepted: Optional[int] = None
    for k in range(PROPOSALS):
        drawn = k2.bounded(
            lambda attempt, k=k: k2.philox_block((row, 0, 1 + 2 * k, attempt), key)[0], n
        )
        value = low + drawn
        a = k2.uniform53(k2.philox_block((row, 0, 2 + 2 * k, 0), key)[0])
        found = _reference_lambda(rates, kind, value)
        holds = a * peak < found * TWO_POW_53
        tried.append(
            {
                "proposal": k,
                "value": str(value),
                "uniform53": str(a),
                "lambda": f"{found.numerator}/{found.denominator}",
                "accepted": holds,
            }
        )
        if holds:
            accepted = k
            break
    return {
        "row": str(row),
        "accepted": accepted,
        "fellBack": accepted is None,
        "value": tried[0 if accepted is None else accepted]["value"],
        "proposals": tried,
    }


def _thinning_run_vectors() -> List[Dict[str, Any]]:
    vectors = []
    for column, kind in COLUMNS:
        key = _key_of_label(_rule_label(column))
        peak = _reference_lambda_max(THINNING_RATES[kind])
        low, high = BOUNDED_INTERVALS[kind][0]
        vectors.append(
            {
                "column": column,
                "type": kind,
                "rates": THINNING_RATES[kind],
                "lambdaMax": f"{peak.numerator}/{peak.denominator}",
                "interval": [str(low), str(high)],
                "rows": [_thinning_run(key, row, kind) for row in ROWS],
            }
        )
    return vectors


def thinning_vectors() -> Dict[str, Any]:
    return {
        "proposals": PROPOSALS,
        "positions": _thinning_position_vectors(),
        "runs": _thinning_run_vectors(),
    }


def dates_vectors() -> Dict[str, Any]:
    return {
        "description": (
            'The dates stream of ADR 0024 section 6: the rule stream <row path> + ("rule", R) '
            "of a date column that a lifetime's duration or a within entry sets, with R the "
            'canonical JSON of ["dates", <column>], and the duration draw on integer thresholds. '
            "Every block word is 16 hex digits, every integer a decimal string. Computed by "
            "tests/kernel2/test_dates_vectors.py from tests/kernel2/reference.py, never typed."
        ),
        "regenerate": REGENERATE,
        "kernelVersion": k2.KERNEL_VERSION,
        "root": {"seam": SEAM.hex(), "seed": SEED, "root": ROOT.hex()},
        "simId": SIM,
        "exposeId": EXPOSE,
        "streams": _stream_vectors(),
        "hiddenColumnCell": _cell_vector(),
        "durations": _duration_vectors(),
        "withinBounded": _bounded_vectors(),
        "withinCuts": _cut_vectors(),
        "holds": _hold_vectors(),
        "candidates": _candidate_vectors(),
        "dwells": _dwell_vectors(),
        "thinning": thinning_vectors(),
    }


def _render(vectors: Dict[str, Any]) -> str:
    return json.dumps(vectors, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_the_vector_file_is_current() -> None:
    assert DATES.read_text(encoding="utf-8") == _render(dates_vectors()), f"run {REGENERATE}"


def _engine_streams() -> CellStreams:
    root = derive_root(SEAM, SEED, seed_tree_version=kernel2.SEED_TREE_VERSION)
    assert root.digest == ROOT
    return CellStreams(root, SIM, EXPOSE)


def test_the_engine_derives_the_pinned_keys_and_blocks() -> None:
    streams = _engine_streams()
    document = json.loads(DATES.read_text(encoding="utf-8"))
    for vector in document["streams"]:
        column = vector["column"]
        assert dates_rule_text(column) == vector["rule"]
        label, key = streams.dates_key(column)
        assert label.hex() == vector["keyLabel"]
        assert _block_hex(key) == vector["key"]
        for held in vector["rows"]:
            for draw in held["draws"]:
                stream = streams.dates_stream_at(int(held["row"]), column, draw["position"])
                assert [str(word) for word in (int(held["row"]), 0, draw["position"], 0)] == draw[
                    "counter"
                ]
                assert _block_hex(stream.block(draw["position"])) == draw["block"]
                assert (
                    str(stream.uniform53()) == draw["uniform53"]
                )  # the first call takes the position


def test_a_hidden_column_draws_on_its_cell_path_and_no_rule_stream_shares_it() -> None:
    streams = _engine_streams()
    document = json.loads(DATES.read_text(encoding="utf-8"))
    cell = document["hiddenColumnCell"]
    assert streams.key_label(cell["column"]).hex() == cell["keyLabel"]
    assert _block_hex(streams.column_key(cell["column"])) == cell["key"]
    for held in cell["rows"]:
        stream = streams.stream_at(int(held["row"]), cell["column"])
        assert isinstance(stream, kernel2.CounterStream)
        assert _block_hex(stream.block(0)) == held["block"]
    rule_keys = {vector["key"][0] + vector["key"][1] for vector in document["streams"]}
    assert cell["key"][0] + cell["key"][1] not in rule_keys
    assert cell["keyLabel"] not in {vector["keyLabel"] for vector in document["streams"]}


def test_the_engines_duration_draw_is_the_pinned_one() -> None:
    document = json.loads(DATES.read_text(encoding="utf-8"))
    seen_open = seen_past = False
    for vector in document["durations"]:
        histogram = Histogram(
            bins=tuple(vector["histogram"]["bins"]),
            bin_weights=tuple(vector["histogram"]["binWeights"]),
            closed=vector["histogram"].get("closed"),
            open=vector["histogram"].get("open"),
        )
        assert [str(cut) for cut in duration_table(histogram).cuts] == vector["thresholds"]
        window = None if vector["window"] is None else tuple(int(v) for v in vector["window"])
        for case in vector["cases"]:
            u, start = int(case["uniform53"]), int(vector["start"])
            for past in ("open", "kept"):
                duration = Duration("s", "e", vector["type"], histogram, past)
                expected = case[past]
                got = duration.end_of(start, u, window)  # type: ignore[arg-type]
                assert (None if got is None else str(got)) == expected
            seen_open |= case["end"] is None
            seen_past |= case["end"] is not None and case["open"] is None
    assert seen_open and seen_past  # the file holds an open slot and an end past the window


def test_the_engines_bounded_draw_within_a_lifetime_is_the_pinned_one() -> None:
    streams = _engine_streams()
    document = json.loads(DATES.read_text(encoding="utf-8"))
    for vector in document["withinBounded"]:
        for held in vector["rows"]:
            for case in held["cases"]:
                interval = (int(case["low"]), int(case["high"]))
                stream = streams.dates_stream_at(int(held["row"]), vector["column"], 1)
                value, fell_back = draw_within(interval, None, False, stream)
                assert str(value) == case["value"] and not fell_back
                assert interval[1] - interval[0] + 1 == int(case["n"])


def test_the_engines_cut_histogram_draw_is_the_pinned_one() -> None:
    document = json.loads(DATES.read_text(encoding="utf-8"))
    assert len(document["withinCuts"]) == 5
    for vector in document["withinCuts"]:
        histogram = Histogram(
            bins=tuple(vector["histogram"]["bins"]),
            bin_weights=tuple(vector["histogram"]["binWeights"]),
            closed=vector["histogram"].get("closed"),
        )
        low, high = vector["interval"]
        interval = (int(low), None if high is None else int(high))
        cut = cut_table(
            offset_table(histogram),
            int(vector["parentStart"]),
            interval,
            unit_of(vector["type"]),
        )
        assert cut is not None
        assert [str(value) for value in cut.cuts] == vector["thresholds"]
        assert [[str(a), str(b)] for a, b in cut.spans] == vector["spans"]
        for case in vector["cases"]:
            assert str(cut.value(int(case["uniform53"]))) == case["value"]


def test_the_engines_hold_draw_is_the_pinned_one() -> None:
    from decimal import localcontext

    from flux_engine.core.rng import bernoulli, kernel_context
    from flux_engine.generator.dates import holding_probability

    streams = _engine_streams()
    document = json.loads(DATES.read_text(encoding="utf-8"))
    for vector in document["holds"]:
        for held in vector["rows"]:
            for case in held["cases"]:
                stream = streams.dates_stream_at(int(held["row"]), vector["column"], 0)
                with localcontext(kernel_context()):
                    holds = bernoulli(stream, holding_probability(case["target"]))
                assert holds is case["holds"]


def test_the_engines_candidate_draw_is_the_pinned_one() -> None:
    streams = _engine_streams()
    document = json.loads(DATES.read_text(encoding="utf-8"))
    for vector in document["candidates"]:
        low, high = (int(bound) for bound in vector["range"])
        for held in vector["rows"]:
            for case in held["cases"]:
                assert case["position"] == 257 + case["candidate"] * vector["m"]
                stream = streams.dates_stream_at(
                    int(held["row"]), vector["column"], case["position"]
                )
                assert str(low + stream.bounded(high - low + 1)) == case["value"]


def test_the_engines_exponential_dwell_draw_is_the_pinned_one() -> None:
    from flux_engine.generator.dwell import ExponentialDwell, draw_dwell

    streams = _engine_streams()
    document = json.loads(DATES.read_text(encoding="utf-8"))
    assert [vector["type"] for vector in document["dwells"]] == ["DATE", "TIMESTAMP"]
    for vector in document["dwells"]:
        dwell = ExponentialDwell(mean=Fraction(vector["mean"]), open=Fraction(vector["open"]))
        unit = unit_of(vector["type"])
        for held in vector["rows"]:
            stream = streams.dates_stream_at(int(held["row"]), vector["column"], 1)
            u = stream.uniform53()
            assert str(u) == held["uniform53"]
            units = draw_dwell(dwell, u, unit)
            assert (None if units is None else str(units)) == held["units"]
            assert (None if units is None else str(int(vector["start"]) + units)) == held["end"]
        for case in vector["probes"]:
            units = draw_dwell(dwell, int(case["uniform53"]), unit)
            assert (None if units is None else str(units)) == case["units"]
        assert any(case["units"] is None for case in vector["probes"])
        assert any(case["units"] is not None for case in vector["probes"])


def test_the_engines_thinning_positions_are_the_pinned_ones() -> None:
    streams = _engine_streams()
    document = json.loads(DATES.read_text(encoding="utf-8"))["thinning"]
    assert document["proposals"] == PROPOSALS
    for vector in document["positions"]:
        for held in vector["rows"]:
            row = int(held["row"])
            for draw in held["draws"]:
                stream = streams.dates_stream_at(row, vector["column"], draw["position"])
                assert _block_hex(stream.block(draw["position"])) == draw["block"]
                assert str(stream.uniform53()) == draw["uniform53"]
            proposal = held["proposal1"]
            stream = streams.dates_stream_at(row, vector["column"], proposal["position"])
            assert (
                str(int(proposal["low"]) + stream.bounded(int(proposal["n"]))) == proposal["value"]
            )
    assert [draw["position"] for draw in document["positions"][0]["rows"][0]["draws"]] == [
        2,
        3,
        256,
    ]


def _check_proposals(rates: Any, held: Dict[str, Any]) -> None:
    """Each pinned proposal's intensity and verdict, and the first accepted one."""
    for case in held["proposals"]:
        lam = rates.lam(int(case["value"]))
        assert f"{lam.numerator}/{lam.denominator}" == case["lambda"]
        assert rates.accepts(int(case["uniform53"]), int(case["value"])) is case["accepted"]
    accepted = [case["proposal"] for case in held["proposals"] if case["accepted"]]
    assert (accepted[0] if accepted else None) == held["accepted"]


def test_the_engines_thinning_run_is_the_pinned_one() -> None:
    from flux_engine.generator.rates import Rates, thin

    streams = _engine_streams()
    document = json.loads(DATES.read_text(encoding="utf-8"))["thinning"]
    seen_later = False
    for vector in document["runs"]:
        given = vector["rates"]
        rates = Rates(
            vector["column"],
            vector["type"],
            month=tuple(given["month"]) if "month" in given else None,
            day_of_week=tuple(given["dayOfWeek"]) if "dayOfWeek" in given else None,
            hour_of_week=tuple(given["hourOfWeek"]) if "hourOfWeek" in given else None,
        )
        peak = rates.intensity.maximum
        assert f"{peak.numerator}/{peak.denominator}" == vector["lambdaMax"]
        low, high = (int(bound) for bound in vector["interval"])
        for held in vector["rows"]:
            stream = streams.dates_stream_at(int(held["row"]), vector["column"], 1)
            first = low + stream.bounded(high - low + 1)
            value, fell_back = thin(
                rates,
                stream,
                first,
                lambda stream=stream, low=low, high=high: low + stream.bounded(high - low + 1),
            )
            assert str(value) == held["value"] and fell_back is held["fellBack"]
            _check_proposals(rates, held)
            seen_later |= bool(held["accepted"])
    assert seen_later  # a row is accepted after proposal 0, so the later positions are pinned too


if __name__ == "__main__":  # pragma: no cover - regenerates the vector file
    DATES.write_text(_render(dates_vectors()), encoding="utf-8")
    sys.stdout.write(f"wrote {DATES}\n")
