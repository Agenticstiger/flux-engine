# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's paths, keys and layouts held to ``kernel2-history-vectors.json`` (KERNEL-SPEC-2 section 8.4).

The vector file pins what section 8.4 says in numbers: each new path's key label, key, counter and
first block, and the layouts of chosen histories: ``c``, the first starts and rooms, and the counts
and offsets of the version layout ``V`` and of the entity layout ``A``. Every value is computed here
from the reference's own primitives (``tests/kernel2/reference.py``: Philox, ``permute/2``, the
slot values and the allotment), by code that does not call the engine, and the first test fails
until the file matches it. Regenerate it with::

    python -m tests.kernel2.test_history_vectors

The rest of the module asks the engine for each of them.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.core.seedtree import counter_stream, derive_root, key_label
from flux_engine.generator.counts import classes_of
from flux_engine.generator.events import lag_at, lags
from flux_engine.generator.history import (
    HistoryPlan,
    OuterInputs,
    chain_columns,
    drawn_length,
    ended_count,
    ended_key,
    entities_key,
    entity_chain,
    entity_states,
    first_start_at,
    fit_key,
    fit_lengths,
    largest_count,
    plan_history,
    reason_key,
    tracked_levels,
    tracked_spec,
    unit_of,
    versions_key,
)
from flux_engine.generator.paths import (
    ALLOCATE_PATH_SHAPE,
    CHANGE_PATH_SHAPE,
    COUNT_PATH_SHAPE,
    ENDED_PATH_SHAPE,
    ENTITIES_PATH_SHAPE,
    HISTORY_PATH_SHAPE,
    KIND_PATH_SHAPE,
    LAG_PATH_SHAPE,
    REASON_PATH_SHAPE,
    VERSIONS_FIT_PATH_SHAPE,
    VERSIONS_PATH_SHAPE,
    allocate_path,
    change_path,
    count_path,
    ended_path,
    entities_path,
    history_path,
    kind_path,
    lag_path,
    reason_path,
    versions_fit_path,
    versions_path,
)
from flux_engine.generator.periods import _runs, _slots, kind_key, period_grid
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.counts import ChildShape
from flux_engine.seam.history import (
    Histogram,
    HistoryEnd,
    HistoryEntry,
    HistoryParent,
    HistoryTracked,
)
from tests.kernel2 import reference as k2

pytestmark = pytest.mark.unit

VECTORS = Path(__file__).resolve().parents[1] / "vectors"
FILE = VECTORS / "kernel2-history-vectors.json"
REGENERATE = "python -m tests.kernel2.test_history_vectors"

SEAM = hashlib.sha256(b"kernel2 history vectors: H").digest()
SEED = 20261122
ROOT = k2.root(SEAM, SEED)
SIM = "s"
EXPOSE = "subscription_version"
TWO_53 = 1 << 53
SEPARATOR = b"\x1f"
EPOCH = date(1970, 1, 1)

#: name, the path, the positions of its ordinal parts, and whether its key is a layout key (32 bytes).
SHAPES: Tuple[Tuple[str, Tuple[Any, ...], Tuple[int, ...], bool], ...] = (
    ("entities", entities_path(SIM, EXPOSE, "customer"), (), True),
    ("versions", versions_path(SIM, EXPOSE), (), True),
    ("versions.fit", versions_fit_path(SIM, EXPOSE), (), True),
    ("history", history_path(SIM, EXPOSE, 41), (5,), False),
    ("change", change_path(SIM, EXPOSE, 41, 6), (5, 6), False),
    ("ended", ended_path(SIM, EXPOSE), (), True),
    ("reason", reason_path(SIM, EXPOSE), (), True),
    ("lag", lag_path(SIM, EXPOSE, 41, 6), (5, 6), False),
    ("kind", kind_path(SIM, EXPOSE, 2, 11), (), True),
)

BINS = ChildShape("bins", ("1", "2", "4", "9"), ("0.45", "0.35", "0.20"))
LEVELS = ChildShape("levels", ("1", "3"), ("0.5", "0.5"))
PARENT_LEVELS = ChildShape("levels", ("1", "2", "3"), ("0.5", "0.3", "0.2"))
LENGTH = Histogram(("30", "90"), ("1",))
DAY_MS = 86_400_000

#: Each case is a declared history and the sizes it is laid out at.
CASES: Tuple[Dict[str, Any], ...] = (
    {
        "name": "(a) bins of versions under a parent, a start histogram, DATE",
        "countRule": "rank",
        "kind": "DATE",
        "windowStart": "2023-01-01T00:00:00Z",
        "until": "2025-12-31",
        "entities": 10,
        "rows": 40,
        "versions": BINS.as_member(),
        "start": {
            "bins": ["2023-01-01", "2024-01-01", "2025-07-01"],
            "binWeights": ["0.55", "0.45"],
        },
        "end": None,
        "tracked": [],
        "reasons": None,
        "parentRows": 4,
        "parent": {"minChildren": 1, "children": PARENT_LEVELS.as_member()},
        "length": LENGTH.as_member(),
        "chain": "nextStart",
        "openEnd": None,
    },
    {
        "name": "(b) no shape, an even spread, no parent, DATE",
        "countRule": "fit",
        "kind": "DATE",
        "windowStart": "2023-01-01T00:00:00Z",
        "until": "2024-06-30",
        "entities": 8,
        "rows": 25,
        "versions": None,
        "start": None,
        "end": None,
        "tracked": [],
        "reasons": None,
        "parentRows": None,
        "parent": None,
        # Lengths that overrun the room of most entities: the chains are compressed.
        "length": {"bins": ["200", "700"], "binWeights": ["1"]},
        "chain": "dayBefore",
        "openEnd": "9999-12-31",
    },
    {
        "name": "(c) levels, closeLast, bins closed above, TIMESTAMP",
        "countRule": "fit",
        "kind": "TIMESTAMP",
        "windowStart": "2023-01-01T00:00:00Z",
        "until": "2024-03-01T00:00:00Z",
        "entities": 8,
        "rows": 24,
        "versions": LEVELS.as_member(),
        "start": {
            "bins": ["2023-01-01T00:00:00Z", "2023-06-01T00:00:00Z", "2024-02-01T00:00:00Z"],
            "binWeights": ["0.7", "0.3"],
            "closed": "upper",
        },
        "end": {"mode": "closeLast", "share": "0.25", "column": None, "value": None},
        "tracked": [],
        "reasons": None,
        "parentRows": 3,
        "parent": {"minChildren": 0, "children": None},
        "length": {"bins": ["30", "90", "365"], "binWeights": ["0.3", "0.7"], "closed": "upper"},
        "chain": "nextStart",
        "openEnd": None,
    },
    {
        "name": "(d) finalVersion, two tracked columns with reasons, DATE",
        "countRule": "rank",
        "kind": "DATE",
        "windowStart": "2023-01-01T00:00:00Z",
        "until": "2025-12-31",
        "entities": 12,
        "rows": 40,
        "versions": BINS.as_member(),
        "start": None,
        "end": {"mode": "finalVersion", "share": "0.5", "column": "status", "value": "gone"},
        "tracked": [
            {
                "column": "plan",
                "initial": {"levels": ["basic", "pro"], "levelWeights": ["0.75", "0.25"]},
                "transitions": {
                    "basic": {"pro": "0.8", "team": "0.2"},
                    "pro": {"basic": "0.5", "team": "0.5"},
                    "team": {"pro": "1"},
                },
                "levels": {"basic": "0.5", "pro": "0.3", "team": "0.2"},
            },
            {
                "column": "tier",
                "initial": None,
                "transitions": {"low": {"high": "1"}, "high": {"low": "1"}},
                "levels": {"low": "0.6", "high": "0.4"},
            },
        ],
        "reasons": {"levels": ["plan", "tier"], "levelWeights": ["0.7", "0.3"]},
        "parentRows": None,
        "parent": None,
        "length": LENGTH.as_member(),
        "chain": "nextStart",
        "openEnd": None,
    },
    {
        "name": "(e) finalVersion, one tracked column, a share of changes that move none, DATE",
        "countRule": "rank",
        "kind": "DATE",
        "windowStart": "2023-01-01T00:00:00Z",
        "until": "2025-12-31",
        "entities": 12,
        "rows": 40,
        "versions": BINS.as_member(),
        "start": None,
        "end": {"mode": "finalVersion", "share": "0.5", "column": "status", "value": "gone"},
        "tracked": [
            {
                "column": "plan",
                "initial": {"levels": ["basic", "pro"], "levelWeights": ["0.75", "0.25"]},
                "transitions": {
                    "basic": {"pro": "0.8", "team": "0.2"},
                    "pro": {"basic": "0.5", "team": "0.5"},
                    "team": {"pro": "1"},
                },
                "levels": {"basic": "0.5", "pro": "0.3", "team": "0.2"},
            },
        ],
        "reasons": None,
        "untracked": "0.6",
        "parentRows": None,
        "parent": None,
        "length": LENGTH.as_member(),
        "chain": "nextStart",
        "openEnd": None,
    },
)

#: The rank-coupling cases again under the fit: a recorded plan keeps (a), (d) and (e), a new plan takes these.
#: A case under the rank coupling carries no ``countRule`` in the file, as a recorded plan carries no ``versionCounts``.
CASES = (
    *CASES,
    *(
        {**case, "name": name, "countRule": "fit"}
        for case, name in (
            (CASES[0], "(f) as (a), each entity taking a count its room fits"),
            (CASES[3], "(g) as (d), each entity taking a count its room fits"),
        )
    ),
)


# ---------------------------------------------------------------------------
# The reference: keys, draws, layouts and first starts, without the engine
# ---------------------------------------------------------------------------


def _label(path: Sequence[Any], ordinals: Sequence[int]) -> bytes:
    """KERNEL-SPEC-2 section 2's key label: the parts joined by the separator, ordinals empty."""
    return SEPARATOR.join(
        b"" if i in ordinals else str(part).encode() for i, part in enumerate(path)
    )


def _digest(path: Sequence[Any], ordinals: Sequence[int]) -> bytes:
    material = k2.SEED_TREE_VERSION.encode() + k2.NUL + ROOT + k2.NUL + _label(path, ordinals)
    return hashlib.sha256(material).digest()


def _stream_key(path: Sequence[Any], ordinals: Sequence[int]) -> Tuple[int, int]:
    digest = _digest(path, ordinals)
    return int.from_bytes(digest[:8], "big"), int.from_bytes(digest[8:16], "big")


def _unit(text: str, kind: str) -> int:
    if kind == "DATE":
        return (date.fromisoformat(text) - EPOCH).days
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()) * 1000


def _reference_layout(
    key: bytes,
    parents: int,
    child_rows: int,
    minimum: int,
    shape: Optional[Dict[str, Any]],
    expected: Optional[Sequence[Fraction]],
) -> Dict[str, Any]:
    """Section 8.1's counts and offsets of every parent row, from the reference's pieces."""
    sigma = [k2.permute(key, parents, rank) for rank in range(parents)]
    slots = k2.slot_values(parents, shape) if shape is not None and parents else None
    x = k2._expected_counts(sigma, slots, None if expected is None else list(expected))
    spare = child_rows - minimum * parents
    assert spare >= 0
    counts = [minimum + a for a in k2._allot(spare, x, minimum)] if parents else []
    return {
        "sigma": sigma,
        "counts": counts,
        "offsets": [sum(counts[:p]) for p in range(parents)],
    }


def _reference_fit(
    case: Dict[str, Any], starts: Sequence[int], until: int, c: int
) -> Dict[str, Any]:
    """KERNEL-SPEC-2 section 8.4's fit, as the text says it and by the slowest route: no heap, no sort.

    The multiset is the layout's, the capacity of an entity is the versions its own drawn lengths
    fit, the priority is ``permute/2`` under the fit key, and each count, from the largest, takes the
    entity it is dealt to by a scan of every entity.
    """
    entities, rows = case["entities"], case["rows"]
    key_v = _digest(versions_path(SIM, EXPOSE), ())
    multiset = sorted(
        _reference_layout(key_v, entities, rows, 1, case["versions"], None)["counts"], reverse=True
    )
    closing = 1 if _closes(case) else 0
    capacity = []
    for entity in range(entities):
        bound = until - starts[entity] - 1 - closing
        total = fitted = 0
        for k in range(1, c):
            total += _drawn_length(case, entity, k - 1)
            if total > bound:
                break
            fitted = k
        capacity.append(1 + fitted)
    key_fit = _digest(versions_fit_path(SIM, EXPOSE), ())
    priority = [k2.permute(key_fit, entities, entity) for entity in range(entities)]
    counts = [0] * entities
    for count in multiset:
        free = [e for e in range(entities) if counts[e] == 0]
        fits = [e for e in free if capacity[e] >= count]
        pool, order = (
            (fits, lambda e: priority[e]) if fits else (free, lambda e: (-capacity[e], priority[e]))
        )
        counts[min(pool, key=order)] = count
    offsets = [sum(counts[:p]) for p in range(entities)]
    return {
        "key": key_fit.hex(),
        "priority": priority,
        "capacity": capacity,
        "counts": counts,
        "offsets": offsets,
    }


def _largest_count(entities: int, rows: int, shape: Optional[Dict[str, Any]]) -> int:
    """``c``: ranks hold the slots in reverse order, so ties go to the larger slot."""
    if shape is None:
        return -(-rows // entities)
    reversed_hook = [Fraction(entities - 1 - rank) for rank in range(entities)]
    return max(_reference_layout(bytes(32), entities, rows, 1, shape, reversed_hook)["counts"])


def _start_bins(
    histogram: Dict[str, Any], kind: str, first: int, last: int, scale: int = 0
) -> List[Tuple[Fraction, int, int]]:
    """EXPR-SPEC-2 section 6's bins inside ``[first, last]``: weight kept, first value, values inside.

    With a *scale* the edges are whole days times it (a length), else values of *kind* (a start).
    """
    edges = [int(text) * scale if scale else _unit(text, kind) for text in histogram["bins"]]
    upper = histogram.get("closed") == "upper"
    kept = []
    for k, text in enumerate(histogram["binWeights"]):
        if upper:
            low, high = edges[k] + (1 if k else 0), edges[k + 1]
        else:
            low, high = edges[k], edges[k + 1] - (0 if k == len(edges) - 2 else 1)
        inside = min(high, last) - max(low, first) + 1
        weight = Fraction(text)
        if weight > 0 and inside > 0:
            kept.append((weight * Fraction(inside, high - low + 1), max(low, first), inside))
    return kept


def _pick(u: int, bins: List[Tuple[Fraction, int, int]]) -> int:
    """The value one uniform53 *u* picks from *bins*."""
    total = sum(weight for weight, _first, _count in bins)
    running, thresholds = Fraction(0), []
    for weight, _first, _count in bins:
        running += weight
        thresholds.append(int(TWO_53 * running / total))
    index = next(i for i, threshold in enumerate(thresholds) if u < threshold)
    before = thresholds[index - 1] if index else 0
    return bins[index][1] + (u - before) * bins[index][2] // (thresholds[index] - before)


def _first_start(entity: int, case: Dict[str, Any], first: int, last: int) -> int:
    path = history_path(SIM, EXPOSE, entity)
    key = _stream_key(path, (5,))
    bins = [] if case["start"] is None else _start_bins(case["start"], case["kind"], first, last)
    if not bins:
        drawn = k2.bounded(lambda a: k2.philox_block((entity, 0, 0, a), key)[0], last - first + 1)
        return first + drawn
    return _pick(k2.philox_block((entity, 0, 0, 0), key)[0] >> 11, bins)


def _drawn_length(case: Dict[str, Any], entity: int, version: int) -> int:
    """Version *version*'s length: one uniform53 at position 0 of the change path, through ``length``."""
    key = _stream_key(change_path(SIM, EXPOSE, entity, version), (5, 6))
    scale = DAY_MS if case["kind"] == "TIMESTAMP" else 1
    bins = _start_bins(case["length"], case["kind"], 1, 1 << 62, scale)
    return _pick(k2.philox_block((entity, version, 0, 0), key)[0] >> 11, bins)


def _fit(drawn: Sequence[int], room: int) -> List[int]:
    """Section 4 item 4 by exact rationals: one unit each, the rest by largest remainder."""
    if sum(drawn) <= room - 1:
        return list(drawn)
    spare = room - 1 - len(drawn)
    quotas = [Fraction(spare * (d - 1), sum(d - 1 for d in drawn)) for d in drawn]
    shares = [int(q) for q in quotas]
    order = sorted(range(len(drawn)), key=lambda j: (-(quotas[j] - shares[j]), j))
    for j in order[: spare - sum(shares)]:
        shares[j] += 1
    return [1 + share for share in shares]


def _closes(case: Dict[str, Any]) -> bool:
    """Whether the case's ``end`` closes the last version of an ended entity."""
    return case["end"] is not None and case["end"]["mode"] == "closeLast"


def _chain_vector(
    case: Dict[str, Any], entity: int, count: int, first: int, until: int, closes: bool
) -> Any:
    """One entity's lengths, fit, starts and ends, by the reference.

    *closes* says the entity's last version is closed too, with its own drawn length.
    """
    drawn = [_drawn_length(case, entity, j) for j in range(count)]
    closed = drawn if closes else drawn[: count - 1]
    fitted = _fit(closed, until - first)
    starts = [first]
    for length in fitted:
        starts.append(starts[-1] + length)
    step = 1 if case["chain"] == "dayBefore" else 0
    sentinel = None if case["openEnd"] is None else _unit(case["openEnd"], case["kind"])
    ends = [start - step for start in starts[1:]]
    if closes:
        starts = starts[:-1]
    else:
        ends.append(sentinel)
    return {
        "entity": entity,
        "versions": count,
        "closedLast": closes,
        "drawn": drawn,
        "compressed": sum(closed) > until - first - 1,
        "fitted": fitted,
        "starts": starts,
        "ends": ends,
    }


# -- how a history ends (ADR 0017 section 5), by the reference ----------------------------------


def _reference_ended(case: Dict[str, Any], counts: Sequence[int]) -> Dict[str, Any]:
    """Which entities end: ``n = min(LRM(E, [share, 1 - share])[0], E')`` and ``permute/2(K_end, E', r) < n``."""
    end = case["end"]
    eligible = [
        e
        for e, held in enumerate(counts)
        if (end["mode"] == "closeLast" and end["column"] is None) or held >= 2
    ]
    share = Fraction(end["share"])
    of_all = k2._lrm(len(counts), [share, 1 - share])[0] if counts else 0
    count = min(of_all, len(eligible))
    key = _digest(ended_path(SIM, EXPOSE), ())
    ended = sorted(
        eligible[rank]
        for rank in range(len(eligible))
        if k2.permute(key, len(eligible), rank) < count
    )
    return {"key": key.hex(), "eligible": len(eligible), "count": count, "ended": ended}


# -- tracked columns (ADR 0017 section 6), by the reference -------------------------------------


def _levels_of(weights: Dict[str, str]) -> Dict[str, Decimal]:
    return {level: Decimal(weight) for level, weight in weights.items()}


def _initial_levels(tracked: Dict[str, Any]) -> Dict[str, Decimal]:
    initial = tracked["initial"]
    if initial is None:
        return _levels_of(tracked["levels"])
    return _levels_of(dict(zip(initial["levels"], initial["levelWeights"], strict=True)))


def _state_draw(entity: int, version: int, column: int) -> int:
    """The uniform53 at position ``256 + column`` of the version's ``change`` path."""
    key = _stream_key(change_path(SIM, EXPOSE, entity, version), (5, 6))
    return k2.philox_block((entity, version, 256 + column, 0), key)[0] >> 11


def _reasons_member(case: Dict[str, Any]) -> Dict[str, Any]:
    """The case's ``reasons``; one tracked column with none has itself as the one reason."""
    if case["reasons"] is not None:
        return case["reasons"]  # type: ignore[no-any-return]
    return {"levels": [case["tracked"][0]["column"]], "levelWeights": ["1"]}


def _reference_reasons(
    case: Dict[str, Any], counts: Sequence[int], ended: Sequence[int]
) -> Dict[str, Any]:
    """The ordinary changes, their classes and the class of each: section 6's slots, class by class."""
    forced = case["end"] is not None and case["end"]["column"] is not None
    per_entity = [held - 1 - (1 if forced and e in ended else 0) for e, held in enumerate(counts)]
    offsets = [sum(per_entity[:e]) for e in range(len(counts) + 1)]
    total = offsets[-1]
    names = [tracked["column"] for tracked in case["tracked"]]
    reasons = _reasons_member(case)
    weights = [Fraction(text) for text in reasons["levelWeights"]]
    weights = [weight / sum(weights) for weight in weights]
    untracked = case.get("untracked")
    if untracked is not None:
        weights = [weight * (1 - Fraction(untracked)) for weight in weights] + [Fraction(untracked)]
    class_counts = k2._lrm(total, weights)
    key = _digest(reason_path(SIM, EXPOSE), ())
    starts = [sum(class_counts[:k]) for k in range(len(class_counts))]
    classes = []
    for change in range(total):
        slot = k2.permute(key, total, change)
        found = max(k for k, begin in enumerate(starts) if begin <= slot and class_counts[k])
        classes.append(
            names.index(reasons["levels"][found]) if found < len(reasons["levels"]) else len(names)
        )
    return {
        "key": key.hex(),
        "changeOffsets": offsets,
        "total": total,
        "classCounts": class_counts,
        "columnOfChange": classes,
    }


def _reference_states(
    case: Dict[str, Any],
    entity: int,
    count: int,
    forced_ending: bool,
    offset: int,
    columns: Sequence[int],
) -> Dict[str, Any]:
    """Entity *entity*'s tracked levels on each version, with the draws that made them."""
    tracked = case["tracked"]
    state = [
        k2.categorical(_state_draw(entity, 0, place), _initial_levels(held))
        for place, held in enumerate(tracked)
    ]
    states = [list(state)]
    draws = [[_state_draw(entity, 0, place) for place in range(len(tracked))]]
    for version in range(1, count):
        used: List[Optional[int]] = [None] * len(tracked)
        if not (forced_ending and version == count - 1):
            place = columns[offset + version - 1]
            if place < len(tracked):
                used[place] = _state_draw(entity, version, place)
                row = _levels_of(tracked[place]["transitions"][state[place]])
                state[place] = k2.categorical(used[place], row)  # type: ignore[arg-type]
        states.append(list(state))
        draws.append(used)  # type: ignore[arg-type]
    return {"entity": entity, "states": states, "draws": draws}


# -- change events (ADR 0027 section 4), by the reference -----------------------------------------

#: The lag histograms the vectors draw through: one that meets small intervals, one that does not.
LAG_NEAR = {"bins": ["0", "1", "4", "20"], "binWeights": ["0.5", "0.3", "0.2"]}
LAG_FAR = {"bins": ["100", "200"], "binWeights": ["1"]}
#: Each lag draw: the histogram, the kind of the date column, the entity, the version and the room.
LAG_DRAWS: Tuple[Tuple[str, str, int, int, int], ...] = (
    ("near", "DATE", 0, 0, 0),
    ("near", "DATE", 7, 3, 3),
    ("near", "DATE", 41, 6, 30),
    ("near", "DATE", 3, 9, 400),
    ("near", "TIMESTAMP", 7, 3, 3),
    ("near", "TIMESTAMP", 41, 6, 30),
    ("far", "DATE", 7, 3, 4),
    ("far", "DATE", 41, 6, 400),
    ("far", "TIMESTAMP", 3, 9, 2),
)


def _reference_events(
    case: Dict[str, Any], counts: Sequence[int], ended: Sequence[int]
) -> Dict[str, Any]:
    """The events of the history: a ``c`` per entity, a ``u`` per later version, a ``d`` per ended entity.

    Under ``finalVersion`` the change into the final version is the ``d`` and takes the place of
    its ``u``; under ``closeLast`` the ``d`` is one more event after the last version's own.
    """
    final = case["end"] is not None and case["end"]["mode"] == "finalVersion"
    created = len(counts)
    updated = sum(counts) - created - (len(ended) if final else 0)
    deleted = len(ended)
    return {
        "c": created,
        "u": updated,
        "d": deleted,
        "total": created + updated + deleted,
        "closingVersions": [counts[e] for e in ended] if _closes(case) else [],
    }


def _reference_lag(name: str, kind: str, entity: int, version: int, hi: int) -> Dict[str, Any]:
    """One lag: the uniform53 at position 0 of its path, through the bins inside ``[0, hi]`` or a bounded draw."""
    histogram = LAG_NEAR if name == "near" else LAG_FAR
    scale = DAY_MS if kind == "TIMESTAMP" else 1
    room = hi * scale
    key = _stream_key(lag_path(SIM, EXPOSE, entity, version), (5, 6))
    bins = _start_bins(histogram, kind, 0, room, scale)
    if bins:
        drawn = k2.philox_block((entity, version, 0, 0), key)[0] >> 11
        value = _pick(drawn, bins)
    else:
        drawn = None
        value = k2.bounded(lambda a: k2.philox_block((entity, version, 0, a), key)[0], room + 1)
    return {
        "histogram": name,
        "kind": kind,
        "entity": entity,
        "version": version,
        "room": room,
        "uniform53": drawn,
        "bounded": not bins,
        "lag": value,
    }


# -- periodic rows (ADR 0027 section 5), by the reference ---------------------------------------

#: Each periods vector: the window, the grain and match, the versions (start day, end day or None
#: for an open one, excluded) and the kinds' shapes, written from the ADR and not from the engine.
PERIOD_CASES: Tuple[Dict[str, Any], ...] = (
    {
        "name": "months from a 31st, overlap",
        "window": ["2023-01-31", "2023-07-15"],
        "grain": "month",
        "match": "overlap",
        "versions": [
            ["2023-01-31", "2023-03-31"],
            ["2023-03-31", None],
            ["2023-02-10", "2023-02-28"],
        ],
        "kinds": [{"levels": ["0", "1", "3"], "levelWeights": ["0.2", "0.5", "0.3"]}],
    },
    {
        "name": "quarters, start",
        "window": ["2023-01-01", "2024-01-01"],
        "grain": "quarter",
        "match": "start",
        "versions": [
            ["2023-01-01", "2023-04-01"],
            ["2023-04-01", None],
            ["2023-05-20", "2023-12-31"],
        ],
        "kinds": [{"bins": ["1", "3", "6"], "binWeights": ["0.6", "0.4"]}],
    },
    {
        "name": "weeks, end",
        "window": ["2023-01-02", "2023-02-20"],
        "grain": "week",
        "match": "end",
        "versions": [["2023-01-02", "2023-01-20"], ["2023-01-20", None]],
        "kinds": [],
    },
)


def _day(text: str) -> int:
    return (date.fromisoformat(text) - EPOCH).days


def _period_days(case: Dict[str, Any]) -> List[int]:
    """Each period's first day: counted from the anchor, a month clamped to the month's last day."""
    anchor = date.fromisoformat(case["window"][0])
    stop = _day(case["window"][1])
    months = {"month": 1, "quarter": 3, "year": 12}
    found: List[int] = []
    while True:
        position = len(found)
        if case["grain"] == "week":
            begins = (anchor - EPOCH).days + 7 * position
        else:
            total = anchor.year * 12 + anchor.month - 1 + months[case["grain"]] * position
            year, month = divmod(total, 12)
            month += 1
            last = calendar.monthrange(year, month)[1]
            begins = (date(year, month, min(anchor.day, last)) - EPOCH).days
        if begins >= stop:
            return found
        found.append(begins)


def _reference_pairs(
    match: str, days: List[int], ends: List[int], spans: List[List[int]]
) -> List[List[int]]:
    """For each period, the versions that hold in it under the *match* mode."""
    pairs: List[List[int]] = []
    for begins, closes in zip(days, ends, strict=True):
        held = []
        for row, (valid_from, valid_to) in enumerate(spans):
            if match == "start":
                holds = valid_from <= begins < valid_to
            elif match == "end":
                holds = valid_from < closes <= valid_to
            else:
                holds = valid_from < closes and valid_to > begins
            if holds:
                held.append(row)
        pairs.append(held)
    return pairs


def _reference_allotments(case: Dict[str, Any], pairs: List[List[int]]) -> List[List[List[int]]]:
    """The allotment of each kind in each period, over the pairs the period holds."""
    allotments = []
    for index, shape in enumerate(case["kinds"]):
        per_period = []
        for period, held in enumerate(pairs):
            number = len(held)
            if not number:
                per_period.append([])
                continue
            key = _digest(kind_path(SIM, EXPOSE, index, period), ())
            slots = k2.slot_values(number, shape)
            per_period.append([slots[k2.permute(key, number, rank)] for rank in range(number)])
        allotments.append(per_period)
    return allotments


def _reference_periods(case: Dict[str, Any]) -> Dict[str, Any]:
    """The periods, the pairs of each and the allotment of each kind in each period."""
    days = _period_days(case)
    stop = _day(case["window"][1])
    ends = [min(days[i + 1], stop) if i + 1 < len(days) else stop for i in range(len(days))]
    spans = [[_day(a), stop if b is None else _day(b)] for a, b in case["versions"]]
    pairs = _reference_pairs(case["match"], days, ends, spans)
    allotments = _reference_allotments(case, pairs)
    rows = (
        sum(len(held) for held in pairs)
        if not case["kinds"]
        else sum(sum(per_period_kind) for kind in allotments for per_period_kind in kind)
    )
    return {
        "periodDays": days,
        "periodEnds": ends,
        "validity": spans,
        "pairs": pairs,
        "kindKeys": [
            [_digest(kind_path(SIM, EXPOSE, k, p), ()).hex() for p in range(len(days))]
            for k in range(len(case["kinds"]))
        ],
        "allotments": allotments,
        "rows": rows,
    }


def _case_vector(case: Dict[str, Any]) -> Dict[str, Any]:
    kind, entities, rows = case["kind"], case["entities"], case["rows"]
    day = 86_400_000
    window = _unit(case["windowStart"], "TIMESTAMP")
    first = window // day if kind == "DATE" else window
    until = _unit(case["until"], kind)
    c = _largest_count(entities, rows, case["versions"])
    reserved = c + (1 if _closes(case) else 0)
    last = until - reserved - 1
    assert first <= last, case["name"]
    starts = [_first_start(e, case, first, last) for e in range(entities)]
    rooms = [until - a for a in starts]
    key_v = _digest(versions_path(SIM, EXPOSE), ())
    coupled = None if case["versions"] is None else [Fraction(room) for room in rooms]
    versions = _reference_layout(key_v, entities, rows, 1, case["versions"], coupled)
    fitted = None
    if case["versions"] is not None and case["countRule"] == "fit":
        fitted = _reference_fit(case, starts, until, c)
        versions = {**versions, "counts": fitted["counts"], "offsets": fitted["offsets"]}
    vector: Dict[str, Any] = {
        "name": case["name"],
        **({"countRule": "fit"} if case["countRule"] == "fit" else {}),
        "kind": kind,
        "windowStart": case["windowStart"],
        "until": case["until"],
        "entities": entities,
        "rows": rows,
        "versions": case["versions"],
        "start": case["start"],
        "end": case["end"],
        "tracked": case["tracked"],
        "reasons": case["reasons"],
        **({"untracked": case["untracked"]} if "untracked" in case else {}),
        "parentRows": case["parentRows"],
        "parent": case["parent"],
        "length": case["length"],
        "chain": case["chain"],
        "openEnd": case["openEnd"],
        "c": c,
        "j": [first, last],
        "untilUnits": until,
        "firstStarts": starts,
        "rooms": rooms,
        "versionKey": key_v.hex(),
        "versionSigma": versions["sigma"],
        "versionCounts": versions["counts"],
        "versionOffsets": versions["offsets"],
    }
    if fitted is not None:
        vector.update(
            fitKey=fitted["key"], fitPriority=fitted["priority"], fitCapacity=fitted["capacity"]
        )
    counts = versions["counts"]
    ended = None if case["end"] is None else _reference_ended(case, counts)
    chosen_set = {0, 1, counts.index(max(counts)), entities - 1}
    if ended is not None and ended["ended"]:
        chosen_set.update(ended["ended"][:2])
    chosen = sorted(chosen_set)
    flagged = [] if ended is None else ended["ended"]
    vector["chains"] = [
        _chain_vector(
            case, entity, counts[entity], starts[entity], until, _closes(case) and entity in flagged
        )
        for entity in chosen
    ]
    if ended is not None:
        vector["ended"] = ended
    vector["events"] = _reference_events(case, counts, flagged)
    if case["tracked"]:
        _add_tracked(vector, case, counts, flagged, chosen)
    if case["parent"] is not None:
        key_a = _digest(entities_path(SIM, EXPOSE, "customer"), ())
        parent = case["parent"]
        outer = _reference_layout(
            key_a, case["parentRows"], entities, parent["minChildren"], parent["children"], None
        )
        vector.update(
            entityKey=key_a.hex(),
            entitySigma=outer["sigma"],
            entityCounts=outer["counts"],
            entityOffsets=outer["offsets"],
        )
    return vector


def _add_tracked(
    vector: Dict[str, Any],
    case: Dict[str, Any],
    counts: Sequence[int],
    flagged: Sequence[int],
    chosen: Sequence[int],
) -> None:
    """The reasons of the ordinary changes and the tracked levels of the chosen entities."""
    forced = case["end"] is not None and case["end"]["column"] is not None
    reasons = _reference_reasons(case, counts, flagged)
    vector["reasons"] = {**_reasons_member(case), **reasons}
    vector["states"] = [
        _reference_states(
            case,
            entity,
            counts[entity],
            forced and entity in flagged,
            reasons["changeOffsets"][entity],
            reasons["columnOfChange"],
        )
        for entity in chosen
    ]


def _path_vector(
    name: str, path: Tuple[Any, ...], ordinals: Tuple[int, ...], layout: bool
) -> Dict[str, Any]:
    digest = _digest(path, ordinals)
    ordered = [int(path[i]) for i in ordinals]
    counter = (ordered + [0, 0])[:2] + [0, 0]
    key = (int.from_bytes(digest[:8], "big"), int.from_bytes(digest[8:16], "big"))
    return {
        "name": name,
        "path": [part if isinstance(part, int) else str(part) for part in path],
        "ordinalPositions": list(ordinals),
        "keyLabel": _label(path, ordinals).hex(),
        "ordinals": ordered,
        **({"layoutKey": digest.hex()} if layout else {"key": digest[:16].hex()}),
        "counter": [format(word, "016x") for word in counter],
        "block": [format(word, "016x") for word in k2.philox_block(counter, key)],
    }


def history_vectors() -> Dict[str, Any]:
    return {
        "description": (
            "KERNEL-SPEC-2 section 8.4's paths and layouts: the key label, key, counter and first "
            "block of each of the six paths of a history's emit and of an event's lag, and the layouts of chosen "
            "histories, namely c, the first starts and rooms, and the counts and offsets of the "
            "entity layout and the version layout, and the lengths, fits, starts and ends of the "
            "chains of chosen entities, then ADR 0027's lag path and, for each history, the number of its "
            "change events by op, chosen event lags, and, for chosen windows, the periods, the pairs of each period and each kind's allotment. Every value is computed by "
            "tests/kernel2/test_history_vectors.py from tests/kernel2/reference.py, never typed; "
            "keys and words are hex, other integers decimal."
        ),
        "regenerate": REGENERATE,
        "seam": SEAM.hex(),
        "seed": str(SEED),
        "root": ROOT.hex(),
        "paths": [_path_vector(*shape) for shape in SHAPES],
        "layouts": [_case_vector(case) for case in CASES],
        "lagHistograms": {"near": LAG_NEAR, "far": LAG_FAR},
        "lags": [_reference_lag(*draw) for draw in LAG_DRAWS],
        "periods": [{**case, **_reference_periods(case)} for case in PERIOD_CASES],
    }


def _render(vectors: Dict[str, Any]) -> str:
    return json.dumps(vectors, indent=2) + "\n"


# ---------------------------------------------------------------------------
# The engine held to the file
# ---------------------------------------------------------------------------

STORED: Dict[str, Any] = (
    json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else history_vectors()
)


def _root() -> Any:
    return derive_root(
        bytes.fromhex(STORED["seam"]),
        int(STORED["seed"]),
        seed_tree_version=kernel2.SEED_TREE_VERSION,
    )


def test_the_vector_file_is_current() -> None:
    assert FILE.read_text(encoding="utf-8") == _render(history_vectors()), f"run {REGENERATE}"


def test_the_engine_root_is_the_vector_root() -> None:
    assert _root().digest.hex() == STORED["root"]


SHAPE_OF = {
    "entities": ENTITIES_PATH_SHAPE,
    "versions": VERSIONS_PATH_SHAPE,
    "versions.fit": VERSIONS_FIT_PATH_SHAPE,
    "history": HISTORY_PATH_SHAPE,
    "change": CHANGE_PATH_SHAPE,
    "ended": ENDED_PATH_SHAPE,
    "reason": REASON_PATH_SHAPE,
    "lag": LAG_PATH_SHAPE,
    "kind": KIND_PATH_SHAPE,
}


@pytest.mark.parametrize("vector", STORED["paths"], ids=lambda vector: vector["name"])
def test_every_path_has_its_vector_key_label_key_counter_and_block(vector: Dict[str, Any]) -> None:
    path = tuple(vector["path"])
    shape = SHAPE_OF[vector["name"]]
    label, ordinals = key_label(path, shape)
    assert label.hex() == vector["keyLabel"]
    assert list(ordinals[: len(vector["ordinals"])]) == vector["ordinals"]
    counter = tuple(int(word, 16) for word in vector["counter"])
    assert counter == (*ordinals, 0, 0)
    key_value = kernel2.key_of(_root().domain + b"\x00" + _root().digest + b"\x00" + label)
    block = kernel2.philox_block(counter, key_value)
    assert [format(word, "016x") for word in block] == vector["block"]
    stored = vector.get("layoutKey") or vector["key"]
    assert (len(stored) // 2, bool(vector.get("layoutKey"))) in ((32, True), (16, False))
    assert key_value[0].to_bytes(8, "big").hex() == stored[:16]


def test_the_layout_keys_are_the_engines_keys() -> None:
    by_name = {vector["name"]: vector for vector in STORED["paths"]}
    root = _root()
    assert entities_key(root, SIM, EXPOSE, "customer").hex() == by_name["entities"]["layoutKey"]
    assert versions_key(root, SIM, EXPOSE).hex() == by_name["versions"]["layoutKey"]
    assert fit_key(root, SIM, EXPOSE).hex() == by_name["versions.fit"]["layoutKey"]
    assert ended_key(root, SIM, EXPOSE).hex() == by_name["ended"]["layoutKey"]
    assert reason_key(root, SIM, EXPOSE).hex() == by_name["reason"]["layoutKey"]
    assert kind_key(root, SIM, EXPOSE, 2, 11).hex() == by_name["kind"]["layoutKey"]
    for name in ("entities", "versions", "versions.fit", "ended", "reason", "kind"):
        key = bytes.fromhex(by_name[name]["layoutKey"])
        assert len(key) == 32


def test_every_shape_has_a_key_label_of_its_own() -> None:
    labels = {vector["keyLabel"] for vector in STORED["paths"]}
    assert len(labels) == len(SHAPES)
    others = {
        key_label(count_path(SIM, EXPOSE, "customer"), COUNT_PATH_SHAPE)[0].hex(),
        key_label(allocate_path(SIM, EXPOSE, "plan"), ALLOCATE_PATH_SHAPE)[0].hex(),
    }
    assert not labels & others
    # Two paths of one shape with other ordinals share a label and differ in the counter.
    first = key_label(history_path(SIM, EXPOSE, 1), HISTORY_PATH_SHAPE)
    second = key_label(history_path(SIM, EXPOSE, 2), HISTORY_PATH_SHAPE)
    assert first[0] == second[0] and first[1] != second[1]
    assert key_label(change_path(SIM, EXPOSE, 4, 5), CHANGE_PATH_SHAPE)[1] == (4, 5)


def _entry(case: Dict[str, Any]) -> HistoryEntry:
    versions = case["versions"]
    start = case["start"]
    parent = case["parent"]
    return HistoryEntry(
        emit=EXPOSE,
        entities=case["entities"],
        length=Histogram(
            tuple(case["length"]["bins"]),
            tuple(case["length"]["binWeights"]),
            case["length"].get("closed"),
        ),
        until=case["until"],
        chain=case["chain"],
        open_end=case["openEnd"],
        parent=(
            None
            if parent is None
            else HistoryParent(
                "customer",
                min_children=parent["minChildren"],
                children=(
                    None
                    if parent["children"] is None
                    else ChildShape(
                        "levels",
                        tuple(parent["children"]["levels"]),
                        tuple(parent["children"]["levelWeights"]),
                    )
                ),
            )
        ),
        versions=(
            None
            if versions is None
            else ChildShape(
                "levels" if "levels" in versions else "bins",
                tuple(versions.get("levels", versions.get("bins", ()))),
                tuple(versions.get("levelWeights", versions.get("binWeights", ()))),
            )
        ),
        start=(
            None
            if start is None
            else Histogram(tuple(start["bins"]), tuple(start["binWeights"]), start.get("closed"))
        ),
        end=_end_of(case),
        tracked=tuple(_tracked_of(held) for held in case["tracked"]),
        reasons=_reasons_of(case),
        untracked=case.get("untracked"),
        version_counts=case.get("countRule", "rank"),
    )


def _end_of(case: Dict[str, Any]) -> Optional[HistoryEnd]:
    end = case["end"]
    return (
        None if end is None else HistoryEnd(end["mode"], end["share"], end["column"], end["value"])
    )


def _tracked_of(held: Dict[str, Any]) -> HistoryTracked:
    initial = held["initial"]
    return HistoryTracked(
        column=held["column"],
        transitions=tuple(
            (level, tuple(row.items())) for level, row in held["transitions"].items()
        ),
        initial=(
            None
            if initial is None
            else ChildShape("levels", tuple(initial["levels"]), tuple(initial["levelWeights"]))
        ),
    )


def _reasons_of(case: Dict[str, Any]) -> Optional[ChildShape]:
    reasons = case["reasons"]
    if reasons is None:
        return None
    return ChildShape("levels", tuple(reasons["levels"]), tuple(reasons["levelWeights"]))


def _planned(vector: Dict[str, Any]) -> HistoryPlan:
    parent = vector["parent"]
    return plan_history(
        _root(),
        SIM,
        EXPOSE,
        _entry(vector),
        kind=LogicalKind.DATE if vector["kind"] == "DATE" else LogicalKind.TIMESTAMP,
        rows=vector["rows"],
        entities=vector["entities"],
        time_bounds={"start": vector["windowStart"], "end": "2099-01-01T00:00:00Z"},
        outer=(
            None
            if parent is None
            else OuterInputs(
                "customer",
                vector["parentRows"],
                shape=_entry(vector).parent.children if _entry(vector).parent else None,
                minimum=parent["minChildren"],
            )
        ),
    )


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    from flux_engine.generator import history

    monkeypatch.setattr(history, "_PLANS", {})
    monkeypatch.setattr(history, "_OUTERS", {})


@pytest.mark.parametrize("vector", STORED["layouts"], ids=lambda vector: vector["name"])
def test_the_largest_count_and_the_first_starts_are_the_vectors(vector: Dict[str, Any]) -> None:
    entry = _entry(vector)
    assert largest_count(vector["entities"], vector["rows"], entry.versions) == vector["c"]
    plan = _planned(vector)
    assert plan.c == vector["c"]
    assert plan.first_starts.tolist() == vector["firstStarts"]
    assert plan.rooms.tolist() == vector["rooms"]
    assert plan.until == vector["untilUnits"] == unit_of(vector["until"], _kind(vector))
    kind = _kind(vector)
    for entity in (0, vector["entities"] - 1):
        assert (
            first_start_at(
                _root(),
                SIM,
                EXPOSE,
                entry,
                kind,
                entity,
                c=vector["c"],
                time_bounds={"start": vector["windowStart"], "end": "never read"},
            )
            == vector["firstStarts"][entity]
        )


def _kind(vector: Dict[str, Any]) -> LogicalKind:
    return LogicalKind.DATE if vector["kind"] == "DATE" else LogicalKind.TIMESTAMP


@pytest.mark.parametrize(
    "vector",
    [v for v in STORED["layouts"] if "fitKey" in v],
    ids=lambda vector: vector["name"],
)
def test_the_fit_is_the_vectors(vector: Dict[str, Any]) -> None:
    """The capacities, the priorities and the counts of the fit, as the slow reference works them out."""
    plan = _planned(vector)
    root = _root()
    assert fit_key(root, SIM, EXPOSE).hex() == vector["fitKey"]
    assert plan.versions.counts.tolist() == vector["versionCounts"]
    assert sorted(vector["versionCounts"]) == sorted(
        _reference_layout(
            bytes.fromhex(vector["versionKey"]),
            vector["entities"],
            vector["rows"],
            1,
            vector["versions"],
            None,
        )["counts"]
    )
    assert sorted(vector["fitPriority"]) == list(range(vector["entities"]))
    assert all(1 <= held <= vector["c"] for held in vector["fitCapacity"])
    capacity = vector["fitCapacity"]
    # An entity holds more than its capacity only where no entity with room was left for the count.
    for entity, count in enumerate(vector["versionCounts"]):
        if count > capacity[entity]:
            assert not [
                e
                for e, held in enumerate(vector["versionCounts"])
                if e != entity and held < count and capacity[e] >= count
            ]


@pytest.mark.parametrize("vector", STORED["layouts"], ids=lambda vector: vector["name"])
def test_the_version_layout_is_the_vectors(vector: Dict[str, Any]) -> None:
    plan = _planned(vector)
    assert versions_key(_root(), SIM, EXPOSE).hex() == vector["versionKey"]
    assert plan.versions.counts.tolist() == vector["versionCounts"]
    assert plan.versions.offsets.tolist() == vector["versionOffsets"]
    assert sum(vector["versionCounts"]) == vector["rows"]
    assert min(vector["versionCounts"]) >= 1 and max(vector["versionCounts"]) <= vector["c"]
    for row in (0, vector["rows"] - 1):
        entity = plan.entity_of(row)
        offsets = vector["versionOffsets"]
        assert offsets[entity] <= row < offsets[entity] + vector["versionCounts"][entity]


@pytest.mark.parametrize(
    "vector", [v for v in STORED["layouts"] if v["parent"] is not None], ids=lambda v: v["name"]
)
def test_the_entity_layout_is_the_vectors(vector: Dict[str, Any]) -> None:
    plan = _planned(vector)
    assert plan.per_parent is not None
    assert plan.per_parent.counts.tolist() == vector["entityCounts"]
    assert plan.per_parent.offsets.tolist() == vector["entityOffsets"]
    assert sum(vector["entityCounts"]) == vector["entities"]
    for entity in range(vector["entities"]):
        parent = plan.parent_of_entity(entity)
        assert parent is not None
        start = vector["entityOffsets"][parent]
        assert start <= entity < start + vector["entityCounts"][parent]


@pytest.mark.parametrize("vector", STORED["layouts"], ids=lambda vector: vector["name"])
def test_every_entity_has_room_for_its_chain(vector: Dict[str, Any]) -> None:
    reserved = vector["c"] + (1 if _closes(vector) else 0)
    assert min(vector["rooms"]) >= reserved + 1
    first, last = vector["j"]
    assert all(first <= start <= last for start in vector["firstStarts"])


def test_the_most_room_takes_the_most_versions_in_the_vectors() -> None:
    vector = STORED["layouts"][0]
    sigma = vector["versionSigma"]
    order = sorted(range(vector["entities"]), key=lambda e: (vector["rooms"][e], sigma[e]))
    counts = [vector["versionCounts"][e] for e in order]
    assert counts == sorted(counts) and counts[0] < counts[-1]


def test_without_a_shape_the_vector_spreads_evenly() -> None:
    vector = STORED["layouts"][1]
    assert sorted(vector["versionCounts"]) == [3] * 7 + [4]
    assert vector["c"] == 4


@pytest.mark.parametrize("vector", STORED["layouts"], ids=lambda vector: vector["name"])
def test_the_lengths_fits_starts_and_ends_are_the_vectors(vector: Dict[str, Any]) -> None:
    entry, kind, plan = _entry(vector), _kind(vector), _planned(vector)
    assert any(chain["compressed"] for chain in vector["chains"]) or vector["name"][1] != "b"
    for chain in vector["chains"]:
        entity = chain["entity"]
        drawn = [
            drawn_length(_root(), SIM, EXPOSE, entry, kind, entity, version)
            for version in range(chain["versions"])
        ]
        assert drawn == chain["drawn"]
        room = plan.until - int(plan.first_starts[entity])
        closed = drawn if chain["closedLast"] else drawn[:-1]
        assert fit_lengths(closed, room) == chain["fitted"]
        found = entity_chain(_root(), SIM, EXPOSE, entry, kind, plan, entity)
        assert list(found.starts) == chain["starts"]
        sentinel = None if entry.open_end is None else unit_of(entry.open_end, kind)
        assert [sentinel if end is None else end for end in found.ends] == chain["ends"]
        # The column path writes the same rows.
        first = int(plan.versions.offsets[entity])
        rows = np.arange(first, first + chain["versions"], dtype=np.int64)
        starts, ends, opened = chain_columns(_root(), SIM, EXPOSE, entry, kind, plan, rows)
        assert starts.tolist() == chain["starts"]
        last = [] if chain["closedLast"] else [True]
        assert opened.tolist() == [False] * (chain["versions"] - len(last)) + last
        assert (
            ends.tolist()[: chain["versions"] - len(last)]
            == chain["ends"][: chain["versions"] - len(last)]
        )


@pytest.mark.parametrize(
    "vector", [v for v in STORED["layouts"] if "ended" in v], ids=lambda v: v["name"]
)
def test_the_ended_entities_are_the_vectors(vector: Dict[str, Any]) -> None:
    plan = _planned(vector)
    ended = vector["ended"]
    assert ended_key(_root(), SIM, EXPOSE).hex() == ended["key"]
    assert plan.ended is not None
    assert np.nonzero(plan.ended)[0].tolist() == ended["ended"]
    assert len(ended["ended"]) == ended["count"] <= ended["eligible"]
    counts = vector["versionCounts"]
    assert ended_count(len(counts), ended["eligible"], vector["end"]["share"]) == ended["count"]
    # Only an eligible entity ends.
    if vector["end"]["mode"] == "finalVersion" or vector["end"]["column"] is not None:
        assert all(counts[entity] >= 2 for entity in ended["ended"])


@pytest.mark.parametrize(
    "vector", [v for v in STORED["layouts"] if v["tracked"]], ids=lambda v: v["name"]
)
def test_the_reasons_are_the_vectors(vector: Dict[str, Any]) -> None:
    plan = _planned(vector)
    reasons = vector["reasons"]
    assert reason_key(_root(), SIM, EXPOSE).hex() == reasons["key"]
    assert plan.changes is not None and plan.reasons is not None
    assert plan.changes.tolist() == reasons["changeOffsets"]
    assert plan.reasons.total == reasons["total"] == sum(reasons["classCounts"])
    classes = [plan.reasons.column_of(change) for change in range(reasons["total"])]
    assert classes == reasons["columnOfChange"]
    assert plan.reasons.columns_of(np.arange(reasons["total"], dtype=np.int64)).tolist() == classes
    # Each tracked column makes its share of the changes exactly.
    names = [held["column"] for held in vector["tracked"]]
    held_by = reasons["classCounts"]
    for level, held in zip(reasons["levels"], held_by, strict=False):
        assert classes.count(names.index(level)) == held
    if "untracked" in vector:
        # The class after the tracked ones moves no column, and takes its share of the changes.
        assert classes.count(len(names)) == held_by[-1]
        assert held_by[-1] == round(vector["reasons"]["total"] * float(vector["untracked"]))


@pytest.mark.parametrize(
    "vector", [v for v in STORED["layouts"] if v["tracked"]], ids=lambda v: v["name"]
)
def test_the_tracked_values_are_the_vectors(vector: Dict[str, Any]) -> None:
    plan = _planned(vector)
    specs = [
        tracked_spec(_tracked_of(held), _levels_of(held["levels"])) for held in vector["tracked"]
    ]
    for found in vector["states"]:
        entity = found["entity"]
        # Every draw is the one at position 256 + c of its version's change path.
        for version, drawn in enumerate(found["draws"]):
            for place, value in enumerate(drawn):
                if value is not None:
                    stream = counter_stream(
                        _root(), change_path(SIM, EXPOSE, entity, version), CHANGE_PATH_SHAPE
                    )
                    assert stream.block(256 + place)[0] >> 11 == value
        states = entity_states(_root(), SIM, EXPOSE, specs, plan, entity)
        assert [list(state) for state in states] == found["states"]
        first = int(plan.versions.offsets[entity])
        rows = np.arange(first, first + len(states), dtype=np.int64)
        columns = tracked_levels(_root(), SIM, EXPOSE, specs, plan, rows)
        for place, column in enumerate(columns):
            assert column.tolist() == [state[place] for state in found["states"]]
        # A version differs from its previous one: one reason, and no move to itself.
        for before, after in zip(found["states"], found["states"][1:], strict=False):
            assert before != after or vector["end"] is not None or "untracked" in vector


@pytest.mark.parametrize("vector", STORED["layouts"], ids=lambda vector: vector["name"])
def test_the_event_counts_are_the_vectors(vector: Dict[str, Any]) -> None:
    plan = _planned(vector)
    events = vector["events"]
    ended = 0 if plan.ended is None else int(plan.ended.sum())
    final = vector["end"] is not None and vector["end"]["mode"] == "finalVersion"
    assert events["c"] == plan.entities
    assert events["d"] == ended
    assert events["u"] == plan.rows - plan.entities - (ended if final else 0)
    assert events["total"] == events["c"] + events["u"] + events["d"]
    if _closes(vector):
        assert events["total"] == plan.rows + ended
        assert events["closingVersions"] == [
            int(plan.versions.counts[e]) for e in np.nonzero(plan.ended)[0].tolist()  # type: ignore[arg-type]
        ]
    else:
        assert events["closingVersions"] == []


@pytest.mark.parametrize(
    "vector",
    STORED["lags"],
    ids=lambda v: f"{v['histogram']}-{v['kind']}-{v['entity']}-{v['version']}",
)
def test_the_lags_are_the_vectors(vector: Dict[str, Any]) -> None:
    raw = STORED["lagHistograms"][vector["histogram"]]
    histogram = Histogram(tuple(raw["bins"]), tuple(raw["binWeights"]))
    kind = LogicalKind.DATE if vector["kind"] == "DATE" else LogicalKind.TIMESTAMP
    args = (_root(), SIM, EXPOSE, histogram, kind)
    entity, version, room = vector["entity"], vector["version"], vector["room"]
    assert lag_at(*args, entity, version, room) == vector["lag"]
    found = lags(
        *args,
        np.array([entity], dtype=np.int64),
        np.array([version], dtype=np.int64),
        np.array([room], dtype=np.int64),
    )
    assert found.tolist() == [vector["lag"]]
    assert 0 <= vector["lag"] <= room
    stream = counter_stream(_root(), lag_path(SIM, EXPOSE, entity, version), LAG_PATH_SHAPE)
    if vector["uniform53"] is not None:
        assert stream.block(0)[0] >> 11 == vector["uniform53"]
    assert vector["bounded"] == (vector["uniform53"] is None)


@pytest.mark.parametrize("vector", STORED["periods"], ids=lambda v: v["name"])
def test_the_periods_the_pairs_and_the_allotments_are_the_vectors(vector: Dict[str, Any]) -> None:
    start, stop = (_day(text) * DAY_MS for text in vector["window"])
    days, begins, ends = period_grid(vector["grain"], (start, stop))
    assert [(day - EPOCH).days for day in days] == vector["periodDays"]
    assert (ends // DAY_MS).tolist() == vector["periodEnds"]
    valid_from = np.array([span[0] * DAY_MS for span in vector["validity"]], dtype=np.int64)
    valid_to = np.array([span[1] * DAY_MS for span in vector["validity"]], dtype=np.int64)
    first, last = _runs(vector["match"], begins, ends, valid_from, valid_to)
    for period, held in enumerate(vector["pairs"]):
        found = [row for row in range(len(first)) if first[row] <= period <= last[row]]
        assert found == held
    for kind, shape in enumerate(vector["kinds"]):
        classes = classes_of(shape)
        for period, held in enumerate(vector["pairs"]):
            assert (
                kind_key(_root(), SIM, EXPOSE, kind, period).hex()
                == vector["kindKeys"][kind][period]
            )
            number = len(held)
            if not number:
                continue
            slot = kernel2.permute2_array(
                kind_key(_root(), SIM, EXPOSE, kind, period),
                number,
                np.arange(number, dtype=np.int64),
            )
            taken = [int(_slots(classes, number)[index]) for index in slot]
            assert taken == vector["allotments"][kind][period]
    assert vector["rows"] == (
        sum(len(held) for held in vector["pairs"])
        if not vector["kinds"]
        else sum(sum(taken) for kind in vector["allotments"] for taken in kind)
    )


def test_a_far_histogram_draws_a_bounded_lag_and_a_near_one_a_binned_lag() -> None:
    assert any(v["bounded"] for v in STORED["lags"]) and not all(
        v["bounded"] for v in STORED["lags"]
    )
    assert all(v["histogram"] == "far" for v in STORED["lags"] if v["bounded"])


def test_a_chain_is_compressed_where_its_lengths_overrun_its_room() -> None:
    compressed = [
        chain for vector in STORED["layouts"] for chain in vector["chains"] if chain["compressed"]
    ]
    assert compressed
    for chain in compressed:
        assert all(length >= 1 for length in chain["fitted"])


if __name__ == "__main__":  # pragma: no cover - regenerates the vector file
    FILE.write_text(_render(history_vectors()), encoding="utf-8")
    sys.stdout.write(f"wrote {FILE}\n")
