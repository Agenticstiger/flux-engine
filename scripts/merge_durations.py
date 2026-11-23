#!/usr/bin/env python3

# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Merge the per-shard duration files of one CI run into one pytest-split file.

Each shard of the fast subset, run with ``--store-durations --clean-durations``,
writes the durations of the tests it ran and nothing else, so the shards' files
are disjoint and together hold every test of the run. This joins them, refuses
a test that two shards both timed (the shards were not one split), and writes
the result sorted by test id, as pytest-split writes its own file.

pytest-split only uses these numbers to balance the groups. A stale or odd file
can make the groups uneven; it cannot drop or repeat a test, which is what
``make fast-partition`` proves.

Usage: merge_durations.py OUTPUT SHARD_FILE [SHARD_FILE ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Sequence


def merge(paths: Sequence[Path]) -> Dict[str, float]:
    """The union of the shards' durations; a test in two shards is an error."""
    merged: Dict[str, float] = {}
    for path in paths:
        durations = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(durations, dict):
            raise ValueError(f"{path}: not a JSON object of test id to seconds")
        for test, seconds in durations.items():
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds < 0:
                raise ValueError(f"{path}: {test!r} has no usable duration: {seconds!r}")
            if test in merged:
                raise ValueError(f"{path}: {test!r} was timed by more than one shard")
            merged[test] = float(seconds)
    if not merged:
        raise ValueError("no durations in any shard file")
    return dict(sorted(merged.items()))


def main(argv: Sequence[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    output = Path(argv[0])
    merged = merge([Path(name) for name in argv[1:]])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(merged, indent=4) + "\n", encoding="utf-8")
    print(f"merged {len(argv) - 1} shard files: {len(merged)} tests, {sum(merged.values()):.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
