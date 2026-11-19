# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``python -m flux_roundtrip``: run the round trip, judge it, print the targets.

A run walks these steps in a fresh work directory:

1. ``truth``: ``fluxsim run`` on the bundle under ``truth/``, which draws the truth's rows.
2. ``profile``: the rows to Parquet, then ``fluid profile`` per table at its default
   settings, joined into one profile.
3. ``init``: ``fluxsim init --contract C --profile P --json``.
4. ``run``: ``fluxsim run <bundle> --profile P --verify --json``.
5. ``judge``: the figures below, each against its target.

Nothing a run measures belongs in the repository except the one JSON line ``--record`` appends
to ``history.jsonl``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_roundtrip import judges, owner, truth

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
HISTORY = ROOT / "history.jsonl"

#: The targets' tolerances.
SHARE_TOLERANCE = 0.10
CRAMERS_V_TOLERANCE = 0.05
#: The pair of columns whose grouped shares and Cramér's V are judged: tier | segment.
GROUPED = ("organisation", "segment", "tier")


def forge_cli_pin() -> str:
    """The forge-cli commit the harness's pyproject pins."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"Agenticstiger/forge-cli@([0-9a-f]{40})", text)
    if match is None:
        raise RuntimeError("pyproject.toml pins no forge-cli commit")
    return match.group(1)


def engine_sha() -> str:
    """The engine's git commit, with ``+dirty`` when its source differs from that commit."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        sha = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain", "--", "flux_engine", "packages")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return sha + ("+dirty" if dirty else "")


def _say(message: str) -> None:
    print(message, flush=True)


def _rows(run_dir: Path, contract: Path) -> Dict[str, List[Dict[str, Any]]]:
    return {
        table: owner.read_emit(run_dir, table, [c["name"] for c in schema])
        for table, schema in owner.read_schema(contract).items()
    }


def _column(rows: Sequence[Mapping[str, Any]], name: str) -> List[Any]:
    return [row[name] for row in rows]


def _primary_keys(contract: Path) -> Dict[str, str]:
    import yaml

    document = yaml.safe_load(contract.read_text(encoding="utf-8"))
    keys = {}
    for expose in document["exposes"]:
        for entity in (expose.get("semantics") or {}).get("entities", []):
            if entity.get("type") == "primary":
                keys[expose["exposeId"]] = entity["expr"]
    return keys


def judge(
    contract: Path,
    truth_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    synthetic_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    init: Mapping[str, Any],
    verify: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> Dict[str, Any]:
    """Every figure the round trip reports, for one truth and one synthetic copy."""
    schema = owner.read_schema(contract)
    primary = _primary_keys(contract)
    figures: Dict[str, Any] = {"scorecard": judges.scorecard_figures(verify)}
    marginals: Dict[str, Any] = {}
    for table, columns in schema.items():
        used = [(c["name"], c["type"]) for c in columns if c["name"] != primary.get(table)]
        marginals[table] = judges.k_marginal(truth_rows[table], synthetic_rows[table], used)
    pairs = [d for m in marginals.values() for d in m["subsets"].values()]
    figures["kMarginal"] = {
        "k": 2,
        "bins": judges.BINS,
        "score": 1.0 - sum(pairs) / len(pairs) if pairs else 0.0,
        "tables": {t: round(m["score"], 6) for t, m in marginals.items()},
    }
    relationships: Dict[str, Any] = {}
    for child, fk, parent, key in truth.RELATIONSHIPS:
        name = f"{child}.{fk}"
        relationships[name] = {
            side: judges.relationship_figures(_column(rows[parent], key), _column(rows[child], fk))
            for side, rows in (("truth", truth_rows), ("synthetic", synthetic_rows))
        }
    figures["relationships"] = relationships
    table, group, value = GROUPED
    figures["grouped"] = {
        f"{value}|{group}": judges.grouped_figures(
            truth_rows[table], synthetic_rows[table], group, value
        )
    }
    figures["derives"] = judges.derives_hold(synthetic_rows["invoice"])
    figures["unusedStatistics"] = judges.unused_statistics(init)
    figures["constantFlags"] = judges.constant_flags(verify, profile)
    return figures


def targets(figures: Mapping[str, Any]) -> List[Tuple[str, bool, str]]:
    """Each target as ``(name, passed, the numbers)``."""
    out: List[Tuple[str, bool, str]] = []
    flags = figures["constantFlags"]
    out.append(
        (
            "no CONSTANT shape flag on a column the profile shows 2+ levels or bins for",
            not flags,
            "flagged: " + (", ".join(flags) if flags else "none"),
        )
    )
    for name, pair in figures["relationships"].items():
        t, s = pair["truth"], pair["synthetic"]
        childless = abs(t["childlessShare"] - s["childlessShare"])
        top = abs(t["topShare"] - s["topShare"])
        out.append(
            (
                f"childless share within {SHARE_TOLERANCE:.2f} ({name})",
                childless <= SHARE_TOLERANCE,
                f"truth {t['childlessShare']:.3f}, synthetic {s['childlessShare']:.3f}, "
                f"difference {childless:.3f}",
            )
        )
        out.append(
            (
                f"top 1% share within {SHARE_TOLERANCE:.2f} ({name})",
                top <= SHARE_TOLERANCE,
                f"truth {t['topShare']:.3f}, synthetic {s['topShare']:.3f}, difference {top:.3f}",
            )
        )
    for name, grouped in figures["grouped"].items():
        out.append(
            (
                f"grouped shares {name} within {SHARE_TOLERANCE:.2f}",
                grouped["maxShareDifference"] <= SHARE_TOLERANCE,
                f"max difference {grouped['maxShareDifference']:.3f}",
            )
        )
        out.append(
            (
                f"Cramer's V of {name} within {CRAMERS_V_TOLERANCE:.2f}",
                grouped["cramersVDifference"] <= CRAMERS_V_TOLERANCE,
                f"truth {grouped['cramersVTruth']:.3f}, synthetic "
                f"{grouped['cramersVSynthetic']:.3f}, difference "
                f"{grouped['cramersVDifference']:.3f}",
            )
        )
    derives = figures["derives"]
    out.append(
        (
            "contract derives hold on 100% of rows",
            derives["bothShare"] == 1.0,
            f"vat {derives['vatShare']:.4f}, gross {derives['grossShare']:.4f}, "
            f"both {derives['bothShare']:.4f}",
        )
    )
    unused = figures["unusedStatistics"]
    out.append(("0 unused statistics", unused == 0, f"unused: {unused}"))
    scorecard = figures["scorecard"]
    out.append(
        (
            "the scorecard states its coverage",
            scorecard["coverageStated"],
            f"coverage: {scorecard.get('coverage', 'not stated')}",
        )
    )
    return out


def print_report(figures: Mapping[str, Any], checks: Sequence[Tuple[str, bool, str]]) -> None:
    card = figures["scorecard"]
    overall = card["overall"]
    _say(f"scorecard overall: {'not measured' if overall is None else f'{overall:.4f}'}")
    coverage = card.get("coverage")
    if coverage:
        _say(
            f"scorecard coverage: columns {coverage['columnsCovered']} of {coverage['columnsOf']}, "
            f"relationships {coverage['relationshipsCovered']} of {coverage['relationshipsOf']}"
        )
    k = figures["kMarginal"]
    _say(
        f"k-marginal (k={k['k']}, {k['bins']} bins): {k['score']:.4f}  "
        + "  ".join(f"{t} {s:.4f}" for t, s in k["tables"].items())
    )
    for name, pair in figures["relationships"].items():
        t, s = pair["truth"], pair["synthetic"]
        _say(
            f"{name}: childless {t['childlessShare']:.3f} -> {s['childlessShare']:.3f}, "
            f"top 1% {t['topShare']:.3f} -> {s['topShare']:.3f}, "
            f"most children {t['mostChildren']} -> {s['mostChildren']}"
        )
    _say("")
    for name, passed, numbers in checks:
        _say(f"{'PASS' if passed else 'FAIL'}  {name}: {numbers}")


def record(figures: Mapping[str, Any], checks: Sequence[Tuple[str, bool, str]]) -> Dict[str, Any]:
    """Append one JSON line to ``history.jsonl`` and return it."""
    line = {
        "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
        "engineSha": engine_sha(),
        "forgeCliPin": forge_cli_pin(),
        "figures": figures,
        "targets": {name: passed for name, passed, _ in checks},
    }
    with HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
    return line


def run(work: Path, *, keep_going: bool = False) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The round trip: returns the figures and the checks' inputs."""
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    contract_name = truth.CONTRACT

    started = time.monotonic()
    _say("truth: fluxsim run on the truth bundle")
    truth_bundle = work / "truth"
    shutil.copytree(truth.TRUTH_DIR, truth_bundle)
    envelope = truth.fluxsim(["run", "."], cwd=truth_bundle)
    (work / "truth-run.json").write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    if envelope["_exit"] != 0:
        raise RuntimeError(f"the truth run failed: {envelope.get('error')}")
    truth_run = truth_bundle / envelope["artifacts"]["apply"]["run_dir"]
    contract = truth_bundle / contract_name
    truth_rows = _rows(truth_run, contract)

    _say("profile: Parquet, then fluid profile per table")
    owner.export_parquet(truth_run, contract, work / "parquet", work / "scratch")
    documents = owner.profile_tables(
        owner.fluid_executable(), contract, work / "parquet", list(truth.TABLES), work / "profiles"
    )
    profile_path = work / "profile.json"
    owner.merge_profiles(documents, profile_path)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))

    _say("init: fluxsim init --contract --profile")
    bundle = work / "bundle"
    init = truth.fluxsim(
        ["init", str(bundle), "--contract", str(contract), "--profile", str(profile_path)],
        cwd=work,
    )
    (work / "init.json").write_text(json.dumps(init, indent=2), encoding="utf-8")
    if init["_exit"] != 0:
        raise RuntimeError(f"init failed: {init.get('error')}")

    _say("run: fluxsim run --profile --verify")
    result = truth.fluxsim(["run", ".", "--profile", str(profile_path), "--verify"], cwd=bundle)
    (work / "run.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    artifacts = result.get("artifacts") or {}
    if "apply" not in artifacts or "run_dir" not in (artifacts["apply"] or {}):
        raise RuntimeError(f"the round-trip run wrote no rows: {result.get('error')}")
    if "verify" not in artifacts:
        raise RuntimeError(f"verify did not run: {result.get('error')}")
    synthetic_rows = _rows(bundle / artifacts["apply"]["run_dir"], contract)

    _say("judge")
    figures = judge(contract, truth_rows, synthetic_rows, init, artifacts["verify"], profile)
    _say(f"({time.monotonic() - started:.0f}s)\n")
    return figures, result


def default_work_root() -> Path:
    """Where a run's files go: under the user's data directory, never in the repository."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "flux-roundtrip"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m flux_roundtrip",
        description="A round trip against a generated truth: generate, profile, init, run, judge.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="run the round trip and print the targets")
    run_parser.add_argument(
        "--work-root", type=Path, default=default_work_root(), help="where the run's files go"
    )
    run_parser.add_argument(
        "--record", action="store_true", help="append one line to history.jsonl"
    )
    run_parser.add_argument(
        "--report-only", action="store_true", help="exit 0 even when a target fails"
    )
    commands.add_parser("repin", help="pin the truth Simulation to its contract's digest again")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "repin":
        _say(truth.repin(truth.TRUTH_DIR))
        return 0
    figures, _ = run(args.work_root)
    checks = targets(figures)
    print_report(figures, checks)
    if args.record:
        record(figures, checks)
        _say(f"\nrecorded one line in {HISTORY.name}")
    failed = [name for name, passed, _ in checks if not passed]
    if failed and not args.report_only:
        _say(f"\n{len(failed)} target(s) failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
