# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Join one night's parts into the ``flux-accuracy-nightly/1`` artefact, and judge it.

The nightly workflow uploads its jobs' outputs as separate artifacts. This
reads them back from one directory and writes one JSON document:

* ``run``: the commit, the date, the base seed, the workflow run;
* ``families``: every measured case the result log recorded, per family,
  with a failure the confirmation pass cleared marked ``unconfirmed``;
* ``pytest``: the junit outcome counts, so a test that errored before it
  could record anything is still counted. junit files an expected failure
  (a strict xfail) under ``skipped``; the artefact counts it apart, as
  ``xfailed``;
* ``benchmarks``: pytest-benchmark's timings, with the ratio to the committed
  snapshot (``docs/accuracy/latest.json``);
* ``practrand``, ``determinism``, ``mutation``: each component's summary;
* ``verdict``: whether the night passed, and every reason it did not.

A component that did not run is recorded as missing, never as passing.

    python -m tests.accuracy.tools.artefact --parts <dir> ... --out accuracy-nightly.json
    python -m tests.accuracy.tools.artefact --verdict accuracy-nightly.json

Standard library only, so the joining job needs nothing beyond the engine.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ElementTree
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from tests.accuracy.report.render import FAMILY_TITLES

__all__ = [
    "ARTEFACT_SCHEMA",
    "BENCH_FAIL_RATIO",
    "BENCH_FLAG_RATIO",
    "MUTATION_FLOORS",
    "MUTATION_MAX_DROP",
    "MUTATION_TARGET",
    "build_artefact",
    "main",
]

ARTEFACT_SCHEMA = "flux-accuracy-nightly/1"

#: A benchmark this many times slower than the committed snapshot fails the night.
BENCH_FAIL_RATIO = 3.0
#: One this many times slower is flagged in the report.
BENCH_FLAG_RATIO = 1.5
#: The weekly mutation kill rate each module must keep: the first baseline
#: (mutmut 3.8.1, 2026-11-11, 2 525 mutants), rounded down to the nearest five
#: points. A ratchet, not an aspiration: raise a floor when its module's
#: survivors are killed. DESIGN.md has the baseline and why it sits where it
#: does.
MUTATION_FLOORS: Dict[str, float] = {
    "flux_engine.concordance.boundary.statistic": 0.90,
    "flux_engine.core.fixedpoint": 0.65,
    "flux_engine.core.lrm": 0.95,
    "flux_engine.core.rng": 0.75,
    "flux_engine.core.seedtree": 0.70,
}
#: Every family a nightly run must log at least one case for: a family that
#: logs nothing has not run, whatever the rest of the night says.
REQUIRED_FAMILIES = tuple(name for name, _ in FAMILY_TITLES)

#: The floor of a module added to the run before it has a baseline of its own.
MUTATION_TARGET = 0.75
#: The largest week-on-week drop in a module's kill rate, in absolute terms.
MUTATION_MAX_DROP = 0.05

_KILLED_STATUSES = frozenset({"killed", "timeout", "segfault", "caught by type check"})
#: A mutant no test reaches is not killed: it counts against the rate.
_COUNTED_STATUSES = _KILLED_STATUSES | {"survived", "suspicious", "no tests"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _results(parts: Path, directory: str = "results") -> List[Dict[str, Any]]:
    """The cases logged into result directories named *directory*.

    The nightly job's first pass logs into ``results/``; its confirmation
    pass, which re-runs only what failed, into ``confirm-results/``.
    """
    records: List[Dict[str, Any]] = []
    for path in sorted(parts.rglob("results-*.jsonl")):
        if path.parent.name != directory:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    records.sort(key=lambda record: (record["family"], record["case"], record["test"]))
    return records


def _families(records: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record["family"])].append(record)
    return {
        family: {
            "cases": len(entries),
            "passed": sum(1 for entry in entries if entry["passed"]),
            # A case logged by a strict-xfail test pins a known defect. It is
            # counted apart, never as a failure: its test's own outcome is in
            # the junit report, where an unexpected pass already fails.
            "failed": sum(
                1
                for entry in entries
                if not entry["passed"]
                and not entry.get("knownDefect")
                and not entry.get("unconfirmed")
            ),
            "unconfirmed": sum(1 for entry in entries if entry.get("unconfirmed")),
            "knownDefects": sum(1 for entry in entries if entry.get("knownDefect")),
            "results": list(entries),
        }
        for family, entries in sorted(grouped.items())
    }


def _junit(parts: Path, filename: str = "junit.xml") -> Optional[Dict[str, Any]]:
    paths = sorted(parts.rglob(filename))
    if not paths:
        return None
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    xfailed = 0
    failing: List[str] = []
    passing: List[str] = []
    for path in paths:
        root = ElementTree.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        for suite in suites:
            for key in totals:
                totals[key] += int(suite.get(key, "0"))
            for case in suite.iter("testcase"):
                node = f"{case.get('classname', '')}::{case.get('name', '')}"
                skipped = case.find("skipped")
                if case.find("failure") is not None or case.find("error") is not None:
                    failing.append(node)
                elif skipped is None:
                    passing.append(node)
                elif skipped.get("type") == "pytest.xfail":
                    xfailed += 1
    totals["skipped"] -= xfailed
    return {**totals, "xfailed": xfailed, "failing": failing, "passing": passing}


def _benchmarks(
    parts: Path, baseline: Optional[Mapping[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    paths = sorted(parts.rglob("bench.json"))
    if not paths:
        return None
    previous = {
        entry["name"]: entry["medianSeconds"]
        for entry in (baseline or {}).get("benchmarks") or []
        if entry.get("medianSeconds")
    }
    rows: List[Dict[str, Any]] = []
    for path in paths:
        for bench in _read_json(path).get("benchmarks", []):
            bench_stats = bench.get("stats", {})
            median = float(bench_stats.get("median", 0.0))
            ratio = median / previous[bench["name"]] if bench["name"] in previous else None
            rows.append(
                {
                    "name": bench["name"],
                    "group": bench.get("group"),
                    "meanSeconds": float(bench_stats.get("mean", 0.0)),
                    "medianSeconds": median,
                    "stddevSeconds": float(bench_stats.get("stddev", 0.0)),
                    "rounds": int(bench_stats.get("rounds", 0)),
                    "ratioToBaseline": ratio,
                }
            )
    return sorted(rows, key=lambda row: row["name"])


def _practrand(parts: Path) -> Dict[str, Any]:
    found: Dict[str, Any] = {}
    for path in sorted(parts.rglob("practrand-*.json")):
        mode = path.stem.removeprefix("practrand-")
        summary = _read_json(path)
        found[mode] = {
            "verdict": summary.get("verdict"),
            "maxLengthBytes": summary.get("max_length_bytes"),
            "evaluations": summary.get("evaluations"),
            "anomalies": summary.get("anomalies", [])[:50],
        }
    return found


def _determinism(parts: Path) -> Dict[str, Any]:
    manifests = {
        path.stem.removeprefix("manifest-"): _read_json(path)
        for path in sorted(parts.rglob("manifest-*.json"))
    }
    if not manifests:
        return {"cells": [], "identical": None, "differences": []}
    reference_name, reference = next(iter(manifests.items()))
    differences = []
    keys = sorted(set().union(*(set(m.get("digests", {})) for m in manifests.values())))
    for key in keys:
        values = {
            name: manifest.get("digests", {}).get(key) for name, manifest in manifests.items()
        }
        if len(set(values.values())) > 1:
            differences.append({"key": key, "cells": values})
    return {
        "cells": [
            {
                "cell": name,
                "environment": manifest.get("environment", {}),
                "digests": len(manifest.get("digests", {})),
            }
            for name, manifest in manifests.items()
        ],
        "reference": reference_name,
        "digests": len(reference.get("digests", {})),
        "identical": not differences,
        "differences": differences[:20],
    }


_RESULT_LINE = re.compile(r"^\s*(?P<name>[\w.]+__mutmut_\d+):\s*(?P<status>[\w ]+?)\s*$")


def _module_of(mutant: str) -> str:
    # ``flux_engine.core.lrm.x_largest_remainder__mutmut_3`` -> ``flux_engine.core.lrm``
    head = mutant.split("__mutmut_")[0]
    return head.rsplit(".", 1)[0]


def _mutation(parts: Path, baseline: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    stats_paths = sorted(parts.rglob("mutation.json"))
    results_paths = sorted(parts.rglob("mutation-results.txt"))
    if not stats_paths and not results_paths:
        return None
    totals = _read_json(stats_paths[0]) if stats_paths else {}
    modules: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for path in results_paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _RESULT_LINE.match(line)
            if match:
                modules[_module_of(match.group("name"))][match.group("status").strip()] += 1
    previous = {
        name: entry.get("killRate")
        for name, entry in ((baseline or {}).get("mutation") or {}).get("modules", {}).items()
    }
    rates: Dict[str, Dict[str, Any]] = {}
    for name, counts in sorted(modules.items()):
        counted = sum(count for status, count in counts.items() if status in _COUNTED_STATUSES)
        killed = sum(count for status, count in counts.items() if status in _KILLED_STATUSES)
        rate = killed / counted if counted else None
        rates[name] = {
            "counts": dict(counts),
            "killRate": rate,
            "floor": MUTATION_FLOORS.get(name, MUTATION_TARGET),
            "previousKillRate": previous.get(name),
        }
    return {
        "totals": totals,
        "modules": rates,
        "target": MUTATION_TARGET,
        "maxDrop": MUTATION_MAX_DROP,
    }


def _verdict(
    artefact: Mapping[str, Any], *, weekly: bool, jobs: Mapping[str, Any]
) -> Dict[str, Any]:
    reasons: List[str] = []
    missing: List[str] = []
    flagged: List[str] = []

    for name, job in sorted(jobs.items()):
        result = (job or {}).get("result")
        if result not in ("success", "skipped") and not (name == "mutation" and not weekly):
            reasons.append(f"job {name} ended {result}")

    pytest_counts = artefact.get("pytest")
    confirmation = artefact.get("confirmation") or {}
    cleared = set(confirmation.get("passing") or [])
    if pytest_counts is None:
        missing.append("pytest junit report")
    else:
        # A first-pass failure the confirmation pass cleared, at a second
        # fresh seed, is a flag, not a red night (DESIGN.md, "Controlling
        # false alarms"); one it repeated, or that was never re-run, is a
        # failure.
        confirmed = [node for node in pytest_counts["failing"] if node not in cleared]
        unconfirmed = [node for node in pytest_counts["failing"] if node in cleared]
        if confirmed:
            reasons.append(
                f"pytest: {len(confirmed)} test(s) failed or errored and were not cleared "
                f"by the confirmation pass ({', '.join(confirmed[:10])})"
            )
        if unconfirmed:
            flagged.append(
                f"{len(unconfirmed)} first-pass failure(s) cleared at the confirmation seed: "
                + ", ".join(unconfirmed[:10])
            )
    run = artefact.get("run") or {}
    expected_seed = {"results": run.get("baseSeed"), "confirm-results": run.get("confirmSeed")}
    stale = sorted(
        {
            f"{entry['test']} (tier {entry.get('tier')}, baseSeed {entry.get('baseSeed')})"
            for summary in artefact["families"].values()
            for entry in summary["results"]
            if entry.get("tier") != run.get("tier")
            or entry.get("baseSeed") != expected_seed["results"]
        }
        | {
            f"{entry['test']} (confirmation, tier {entry.get('tier')}, "
            f"baseSeed {entry.get('baseSeed')})"
            for entry in confirmation.get("results") or []
            if entry.get("tier") != run.get("tier")
            or entry.get("baseSeed") != expected_seed["confirm-results"]
        }
    )
    if stale:
        # Every case must come from this night's tier and seeds, or the
        # artefact's seeds would not reproduce the numbers in it.
        reasons.append(
            f"{len(stale)} logged case(s) are from another tier or seed: " + ", ".join(stale[:5])
        )
    for family, summary in artefact["families"].items():
        if summary["failed"]:
            failing = [
                entry["case"]
                for entry in summary["results"]
                if not entry["passed"]
                and not entry.get("knownDefect")
                and not entry.get("unconfirmed")
            ]
            reasons.append(
                f"{family}: {summary['failed']} case(s) failed ({', '.join(failing[:10])})"
            )
    for family in REQUIRED_FAMILIES:
        if family not in artefact["families"]:
            missing.append(f"family {family} logged no case")
    if not artefact["families"]:
        missing.append("measured cases (the result log is empty)")

    practrand = artefact["practrand"]
    for mode in ("first-words", "interleave"):
        if mode not in practrand:
            missing.append(f"PractRand {mode}")
        elif practrand[mode]["verdict"] != "pass":
            reasons.append(f"PractRand {mode}: {practrand[mode]['verdict']}")

    determinism = artefact["determinism"]
    if len(determinism["cells"]) < 2:
        missing.append("determinism manifests (fewer than two cells)")
    elif not determinism["identical"]:
        reasons.append(
            f"determinism: {len(determinism['differences'])} digest(s) differ across cells"
        )

    benchmarks = artefact["benchmarks"]
    if benchmarks is None:
        missing.append("benchmarks")
    else:
        for row in benchmarks:
            ratio = row["ratioToBaseline"]
            if ratio is not None and ratio > BENCH_FAIL_RATIO:
                reasons.append(f"benchmark {row['name']}: {ratio:.2f}x the committed snapshot")
            elif ratio is not None and ratio > BENCH_FLAG_RATIO:
                flagged.append(f"benchmark {row['name']}: {ratio:.2f}x the committed snapshot")

    mutation = artefact["mutation"]
    if weekly:
        if mutation is None or not mutation["modules"]:
            missing.append("mutation score")
        else:
            for name in sorted(set(MUTATION_FLOORS) - set(mutation["modules"])):
                missing.append(f"mutation score for {name}")
            for name, entry in mutation["modules"].items():
                rate, previous = entry["killRate"], entry["previousKillRate"]
                unchecked = int(entry.get("counts", {}).get("not checked", 0))
                if unchecked:
                    # A run cut short leaves mutants unchecked; a rate over the
                    # rest would judge a sample, not the module.
                    missing.append(f"mutation run incomplete for {name}: {unchecked} not checked")
                    continue
                if rate is None:
                    missing.append(f"mutation score for {name}")
                    continue
                floor = float(entry.get("floor", MUTATION_TARGET))
                if rate < floor:
                    reasons.append(
                        f"mutation {name}: kill rate {rate:.1%} below its floor {floor:.0%}"
                    )
                if previous is not None and previous - rate > MUTATION_MAX_DROP:
                    reasons.append(
                        f"mutation {name}: kill rate fell {previous - rate:.1%} in a week"
                    )

    known = sorted(
        {
            str(entry["knownDefect"])
            for summary in artefact["families"].values()
            for entry in summary["results"]
            if entry.get("knownDefect")
        }
    )
    return {
        "passed": not reasons and not missing,
        "reasons": reasons,
        "missing": missing,
        "flagged": flagged,
        "knownDefects": known,
    }


def build_artefact(
    *,
    parts: Path,
    run: Mapping[str, Any],
    jobs: Mapping[str, Any],
    baseline: Optional[Mapping[str, Any]],
    weekly: bool,
) -> Dict[str, Any]:
    """The joined artefact for the parts under *parts*."""
    first = _results(parts, "results")
    confirm = _results(parts, "confirm-results")
    confirm_junit = _junit(parts, "confirm-junit.xml")
    cleared = {(entry["test"], entry["case"]) for entry in confirm if entry.get("passed") is True}
    for entry in first:
        if (
            not entry["passed"]
            and not entry.get("knownDefect")
            and (entry["test"], entry["case"]) in cleared
        ):
            entry["unconfirmed"] = True
    artefact: Dict[str, Any] = {
        "schema": ARTEFACT_SCHEMA,
        "run": {**dict(run), "weekly": weekly},
        "families": _families(first),
        "pytest": _junit(parts),
        # Present only on a night whose first pass failed: the re-run of the
        # failures at the confirmation seed.
        "confirmation": (
            {
                "results": confirm,
                "passing": (confirm_junit or {}).get("passing", []),
                "failing": (confirm_junit or {}).get("failing", []),
            }
            if confirm or confirm_junit
            else None
        ),
        "benchmarks": _benchmarks(parts, baseline),
        "practrand": _practrand(parts),
        "determinism": _determinism(parts),
        "mutation": _mutation(parts, baseline),
        "jobs": {name: {"result": (job or {}).get("result")} for name, job in sorted(jobs.items())},
    }
    artefact["verdict"] = _verdict(artefact, weekly=weekly, jobs=jobs)
    return artefact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.accuracy.tools.artefact", description=__doc__
    )
    parser.add_argument(
        "--verdict", metavar="ARTEFACT", help="exit 0 iff ARTEFACT passed; print why not"
    )
    parser.add_argument(
        "--parts", type=Path, help="directory the night's artifacts were downloaded to"
    )
    parser.add_argument("--base-seed", help="the night's base seed")
    parser.add_argument("--confirm-seed", help="the seed the confirmation pass re-ran at")
    parser.add_argument("--date", help="the night's UTC date")
    parser.add_argument("--weekly", default="false", help="'true' when the weekly extras ran")
    parser.add_argument("--commit", help="the commit the night ran on")
    parser.add_argument("--run-url", help="the workflow run")
    parser.add_argument("--jobs", default="{}", help="the workflow's needs context, as JSON")
    parser.add_argument("--baseline", type=Path, help="the committed snapshot to compare against")
    parser.add_argument("--out", type=Path, help="where to write the artefact")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.verdict:
        verdict = _read_json(Path(args.verdict))["verdict"]
        for line in verdict["reasons"] + [f"missing: {item}" for item in verdict["missing"]]:
            print(line, file=sys.stderr)
        for line in verdict["flagged"]:
            print(f"flagged: {line}", file=sys.stderr)
        print("passed" if verdict["passed"] else "failed")
        return 0 if verdict["passed"] else 1
    if args.parts is None or args.out is None:
        _parser().error("--parts and --out are required unless --verdict is given")
    baseline = _read_json(args.baseline) if args.baseline and args.baseline.is_file() else None
    run: Dict[str, Any] = {
        "tier": "nightly",
        # Reduced the way tests/accuracy/harness/tier.py reduces it, so the
        # run's seed and every logged case's seed are one number.
        "baseSeed": int(args.base_seed) % (1 << 63) if args.base_seed else None,
        "confirmSeed": int(args.confirm_seed) % (1 << 63) if args.confirm_seed else None,
        "date": args.date,
        "commit": args.commit,
        "url": args.run_url,
    }
    artefact = build_artefact(
        parts=args.parts,
        run=run,
        jobs=json.loads(args.jobs),
        baseline=baseline,
        weekly=args.weekly == "true",
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artefact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: {'passed' if artefact['verdict']['passed'] else 'failed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
