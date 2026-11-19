# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Compare determinism manifests: exit 0 iff every manifest's ``digests`` are identical.

Run as ``python -m tests.accuracy.determinism.compare [--expect N] a.json b.json [...]``.

Exit codes: 0 identical, 1 the manifests differ, 2 unusable input (unreadable, empty
digests, wrong number of manifests for ``--expect``).

Only the ``digests`` mapping is compared. ``environment`` is expected to differ between
cells (that is the point of a matrix). ``schema``, ``seed`` and ``profile`` are checked
separately: manifests built under different seeds or sizes are not comparable, so a mismatch is reported and
fails the comparison rather than being mistaken for a determinism failure.

On a difference, each differing key is printed with the cells (the manifest file names)
that disagree, grouped by the value they hold. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = ["ManifestError", "compare", "main"]

MISSING = "<missing>"


class ManifestError(ValueError):
    """An input that is not a usable manifest: unreadable, not JSON, or no digests."""


def _load(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ManifestError(f"{path}: cannot read a manifest: {error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("digests"), dict):
        raise ManifestError(f"{path}: not a determinism manifest (no 'digests' mapping)")
    if not document["digests"]:
        raise ManifestError(f"{path}: the manifest holds no digests, so it proves nothing")
    return document


def compare(manifests: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """The report lines for *manifests* (cell name -> manifest); empty when identical."""
    lines: List[str] = []
    for field in ("schema", "seed", "profile"):
        values = {name: manifest.get(field) for name, manifest in manifests.items()}
        if len({str(value) for value in values.values()}) > 1:
            lines.append(f"{field} differs between manifests, which are not comparable:")
            lines.extend(f"  {name}: {field} {value}" for name, value in values.items())
    keys = sorted({key for manifest in manifests.values() for key in manifest["digests"]})
    for key in keys:
        groups: Dict[str, List[str]] = {}
        for name, manifest in manifests.items():
            groups.setdefault(str(manifest["digests"].get(key, MISSING)), []).append(name)
        if len(groups) > 1:
            lines.append(f"DIFFERS {key}")
            for value, cells in sorted(groups.items(), key=lambda item: item[1]):
                lines.append(f"  {value}  <-  {', '.join(sorted(cells))}")
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("manifests", nargs="+", type=Path, help="two or more manifest files")
    parser.add_argument(
        "--expect",
        type=int,
        metavar="N",
        help="fail (exit 2) unless exactly N manifests are given, e.g. the matrix size",
    )
    args = parser.parse_args(argv)
    if args.expect is not None and len(args.manifests) != args.expect:
        print(
            f"expected {args.expect} manifests, got {len(args.manifests)}: a cell is missing "
            "or a stray file matched",
            file=sys.stderr,
        )
        return 2
    if len(args.manifests) < 2:
        parser.error("give at least two manifests to compare")
    names = [str(path) for path in args.manifests]
    if len(set(names)) != len(names):
        parser.error("each manifest must be a distinct path")
    try:
        manifests = {str(path): _load(path) for path in args.manifests}
    except ManifestError as error:
        print(error, file=sys.stderr)
        return 2
    lines = compare(manifests)
    if lines:
        print("\n".join(lines))
        print(f"{sum(1 for line in lines if line.startswith('DIFFERS'))} key(s) differ")
        return 1
    count = len(next(iter(manifests.values()))["digests"])
    print(f"identical: {len(manifests)} manifests, {count} digests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
