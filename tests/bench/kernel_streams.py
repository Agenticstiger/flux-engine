# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Prototype for ADR 0010: one stream per cell against counter-based words.

Kernel v1 builds a new ``Generator(PCG64(SeedSequence(e)))`` for every cell
(KERNEL-SPEC section 3), where ``e`` is the sha256 of the cell's seed-tree path.
A counter-based generator is a pure function of a key and a counter (Salmon,
Moraes, Dror and Shaw, "Parallel random numbers: as easy as 1, 2, 3", SC11).
Here the key comes once from a column's path without its row, and the row is
the counter, so the words of every row of a column come from one call.

Four ways to get ``--words`` words for each of ``--cells`` cells of one column,
and one part of the first:

* ``v1 per cell``: the engine's own ``CellStreams.stream_at``, as a run calls it.
* ``v1 hash floor``: the part of a v1 stream that stays one call per cell even
  if the seeding after it were batched in numpy: the sha256 of each cell's label,
  with the front every label of the column shares hashed once and copied, and the
  digests gathered into one array, as a batched seeding would take them.
* ``v2 per cell``: one numpy ``Philox`` object per cell. Its cost shows that the
  gain comes from asking for many cells at once, not from Philox itself.
* ``v2 column``: one numpy ``Philox`` object for the column. numpy steps its
  counter by one for each block of four words, so block ``r`` is row ``r``.
* ``v2 any rows``: Philox4x64-10 written out in numpy array arithmetic, which
  takes any set of rows in any order, as a worker holding a slice would.

Before timing, the script checks that ``v2 any rows`` agrees with numpy's
``Philox`` on random keys and counters, and with ``v2 column`` on the first rows.
Each round times every variant once, in turn, and prints the load average
beside it: other work on a shared machine moves timings by more than some of
the differences measured here. Run from the repository root::

    python -m tests.bench.kernel_streams --cells 1000000 --rounds 3

Nothing in the engine imports this module.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import statistics
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from numpy.random import Philox

from flux_engine.core.seedtree import SEED_TREE_VERSION, derive_root, stream_entropy_of_label
from flux_engine.generator.paths import CellStreams
from tests.kernel2.reference import COUNTER_SPACE as _COUNTER_SPACE
from tests.kernel2.reference import numpy_philox, philox4x64_10

SIM_ID = "bench"
EXPOSE_ID = "orders"
COLUMN = "amount"

_MASK64 = (1 << 64) - 1
_SEPARATOR = b"\x1f"

Key = Tuple[int, int]


def column_key(root_digest: bytes, column_label: bytes) -> Key:
    """A 128-bit Philox key for one column: the first 16 bytes of a sha256 of its path."""
    digest = hashlib.sha256(b"flux-seed/2\x00" + root_digest + b"\x00" + column_label).digest()
    return int.from_bytes(digest[:8], "big"), int.from_bytes(digest[8:16], "big")


def _counters(rows: "np.ndarray") -> "np.ndarray":
    counters = np.zeros((rows.size, 4), dtype=np.uint64)
    counters[:, 0] = rows
    return counters


def check(key: Key, cases: int = 200, rows: int = 10_000) -> None:
    """Raise ``AssertionError`` unless the three Philox routes give the same words."""
    material = Philox(key=np.array((1, 2), dtype=np.uint64)).random_raw(6 * cases)
    for case in range(cases):
        words = [int(word) for word in material[6 * case : 6 * case + 6]]
        case_key = (words[0], words[1])
        counter = words[2:]
        counter_int = sum(word << (64 * i) for i, word in enumerate(counter))
        expected = numpy_philox(case_key, counter_int).random_raw(4)
        got = philox4x64_10(np.array([counter], dtype=np.uint64), case_key)[0]
        assert np.array_equal(expected, got), f"case {case}: {expected} != {got}"
    column = numpy_philox(key, 0).random_raw(4 * rows).reshape(rows, 4)
    computed = philox4x64_10(_counters(np.arange(rows, dtype=np.uint64)), key)
    assert np.array_equal(column, computed), "the column call and the array function differ"


def v1_per_cell(cells: int, words: int, streams: CellStreams) -> int:
    folded = 0
    for row in range(cells):
        folded ^= streams.stream_at(row, COLUMN).next_raw_words(words)[-1]
    return folded


def hash_floor_parts(streams: CellStreams) -> Tuple[Any, bytes]:
    """sha256 fed with the front every label of the column shares, and the bytes after the row.

    A v1 stream's entropy is ``sha256(domain + NUL + root + NUL + label)``, and the labels
    of one column differ only in the row's digits.
    """
    head, _row, tail = streams.label(0, COLUMN).rsplit(_SEPARATOR, 2)
    front = hashlib.sha256(
        SEED_TREE_VERSION.encode("utf-8") + b"\x00" + streams.root.digest + b"\x00" + head
    )
    front.update(_SEPARATOR)
    return front, _SEPARATOR + tail


def check_hash_floor(streams: CellStreams, rows: int = 100) -> None:
    """Raise ``AssertionError`` unless the copied hash gives v1's own stream entropy."""
    front, back = hash_floor_parts(streams)
    for row in range(rows):
        digest = front.copy()
        digest.update(b"%d" % row + back)
        expected = stream_entropy_of_label(streams.root, streams.label(row, COLUMN))
        assert int.from_bytes(digest.digest(), "big") == expected, f"row {row}"


def v1_hash_floor(cells: int, streams: CellStreams) -> int:
    front, back = hash_floor_parts(streams)
    digests: List[bytes] = []
    append = digests.append
    for row in range(cells):
        digest = front.copy()
        digest.update(b"%d" % row + back)
        append(digest.digest())
    words = np.frombuffer(b"".join(digests), dtype=">u4")
    return int(words[-1])


def v2_per_cell(cells: int, words: int, key: Key) -> int:
    key_words = np.array(key, dtype=np.uint64)
    folded = 0
    for row in range(cells):
        bit_generator = Philox(key=key_words, counter=(row - 1) % _COUNTER_SPACE)
        folded ^= int(bit_generator.random_raw(words)[-1])
    return folded


def v2_column(cells: int, words: int, key: Key) -> int:
    blocks = numpy_philox(key, 0).random_raw(4 * cells).reshape(cells, 4)
    return int(np.bitwise_xor.reduce(blocks[:, words - 1]))


def v2_any_rows(rows: "np.ndarray", words: int, key: Key) -> int:
    blocks = philox4x64_10(_counters(rows), key)
    return int(np.bitwise_xor.reduce(blocks[:, words - 1]))


def _load() -> str:
    try:
        one, five, fifteen = os.getloadavg()
    except (AttributeError, OSError):  # Windows has no load average
        return "n/a"
    return f"{one:.2f} {five:.2f} {fifteen:.2f}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cells", type=int, default=1_000_000)
    parser.add_argument("--words", type=int, default=2, choices=(1, 2, 3, 4))
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args(argv)

    root = derive_root(hashlib.sha256(b"kernel-streams prototype").digest(), 20261118)
    streams = CellStreams(root, SIM_ID, EXPOSE_ID)
    head, _row, tail = streams.label(0, COLUMN).rsplit(b"\x1f", 2)
    key = column_key(root.digest, head + b"\x1f" + tail)
    check(key)
    check_hash_floor(streams)
    shuffled = np.argsort(numpy_philox((3, 4), 0).random_raw(args.cells)).astype(np.uint64)

    variants: Dict[str, Callable[[], int]] = {
        "v1 per cell": lambda: v1_per_cell(args.cells, args.words, streams),
        "v1 hash floor": lambda: v1_hash_floor(args.cells, streams),
        "v2 per cell": lambda: v2_per_cell(args.cells, args.words, key),
        "v2 column": lambda: v2_column(args.cells, args.words, key),
        "v2 any rows": lambda: v2_any_rows(shuffled, args.words, key),
    }
    print(
        f"python {platform.python_version()}, numpy {np.__version__}, "
        f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs; "
        f"{args.cells:,} cells, {args.words} words each; checks passed"
    )
    times: Dict[str, List[float]] = {name: [] for name in variants}
    for round_index in range(1, args.rounds + 1):
        for name, run in variants.items():
            load = _load()
            started = time.perf_counter()
            run()
            elapsed = time.perf_counter() - started
            times[name].append(elapsed)
            per_cell = elapsed / args.cells * 1e9
            print(
                f"round {round_index}  {name:<12} {elapsed:9.3f} s {per_cell:9.1f} ns/cell"
                f"  load {load}"
            )
    baseline = min(times["v1 per cell"])
    print("variant       best ns/cell  median ns/cell  v1 best / best")
    for name, observed in times.items():
        best = min(observed) / args.cells * 1e9
        median = statistics.median(observed) / args.cells * 1e9
        print(f"{name:<12} {best:13.1f} {median:15.1f} {baseline / min(observed):15.1f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
