#!/usr/bin/env python3

# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Check that the CI shards of a test selection are exactly that selection.

``make fast-partition`` and ``make slow-partition`` collect a whole selection
into ``<whole>.txt``, then each group of each split CI uses into
``<prefix>-<ways>-<n>.txt``, all in one scratch directory. This compares the
node ids. For every split, the groups together must be the whole collection:
no test in two groups, none in no group, and no empty group.

It compares collected node-id lists, not exit codes, so a pytest that failed
to collect would leave a short list and fail here rather than pass.

Usage: check_split_partition.py SCRATCH_DIR WHOLE PREFIX WAYS [WAYS ...]

For example ``build/ci fast-all fast-group 4 8`` reads ``fast-all.txt`` and
``fast-group-4-1.txt`` to ``fast-group-8-8.txt``.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import List, Sequence


def node_ids(path: Path) -> List[str]:
    """The node ids in a ``pytest --collect-only -q`` listing, in order."""
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if "::" in line]


def check_split(scratch: Path, prefix: str, ways: int, every: Sequence[str]) -> str:
    """Raise AssertionError unless the ``ways`` groups partition ``every``."""
    groups = [node_ids(scratch / f"{prefix}-{ways}-{n}.txt") for n in range(1, ways + 1)]
    sizes = [len(group) for group in groups]
    assert all(groups), f"{ways}-way split has an empty group: {sizes}"
    counts = Counter(node for group in groups for node in group)
    repeated = sorted(node for node, count in counts.items() if count > 1)
    assert not repeated, f"{ways}-way split: selected by more than one group: {repeated}"
    whole = set(every)
    missing, extra = sorted(whole - set(counts)), sorted(set(counts) - whole)
    assert not missing and not extra, f"{ways}-way split: missing {missing} / extra {extra}"
    return (
        f"{prefix} partitioned exactly {ways} ways: {len(every)} tests "
        f"across {ways} groups ({', '.join(str(size) for size in sizes)})"
    )


def main(argv: Sequence[str]) -> int:
    if len(argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    scratch, whole, prefix = Path(argv[0]), argv[1], argv[2]
    splits = sorted({int(way) for way in argv[3:]})
    every = node_ids(scratch / f"{whole}.txt")
    assert every, f"{whole} collected nothing"
    assert len(set(every)) == len(every), f"{whole} repeats a node id"
    for ways in splits:
        print(check_split(scratch, prefix, ways, every))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
