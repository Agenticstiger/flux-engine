# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A small, deterministic benchmark harness for the ``fluxsim`` command line.

It drives the CLI in subprocesses (``<python> -m flux_engine.cli ...``), so it needs only the
standard library and works against any source tree the same interpreter can run.

Timed cases (``run``, or ``plan --no-stage``) are repeated ``--repeats`` times for each
``--jobs`` value, each repeat in a fresh ``--state-dir``. For every repeat the harness records
wall seconds, the CPU seconds of the whole process tree, the peak resident set of the largest
process, the rows written and the ``--timings`` phases (``emit <x>`` and ``frame <x>`` are the
parallel phases, every other phase is serial). Both the CPU time and the peak resident set come
from ``wait4`` on the child (exact; unlike ``RUSAGE_CHILDREN`` it is not a high-water mark over
every earlier child) plus, for the pool workers a forkserver starts, a ``ps`` sampler on the
process group, because ``wait4`` never sees those workers (the CPU figure is approximate).

Every run also records its bit-exact fingerprint: the plan digest, the run digest, the sha256 of
``plan.json`` (without its ``generated_at`` stamp, the only field that varies) and of every file
under the run directory's ``emits/``. The fingerprint must not depend on ``--jobs``; the harness
exits 1 if it does. ``--save FILE`` writes everything as JSON; ``--against FILE`` re-runs and
compares each fingerprint with the saved one, prints every mismatch and exits 2 on any.

Digest-only cases (kernel 0.1.0, ``--scale 0.5``, ``apply --shard``) run at jobs 1 and 4 with no
timing. ``verify`` is timed separately: one ``run``, then ``fluxsim verify`` at each jobs value
(best of ``--repeats``).

Environment. ``PYTHONDONTWRITEBYTECODE`` makes every process, pool workers included, recompile
every module, which distorts timings. The subprocesses therefore run with it removed, after one
untimed warm-up per source tree so ``__pycache__`` exists; both values are recorded.

Usage (from the repository root, with the repository's interpreter)::

    .venv/bin/python tools/perfbench/perfbench.py --list
    .venv/bin/python tools/perfbench/perfbench.py --cases counts,links --jobs 1,4 --repeats 3
    .venv/bin/python tools/perfbench/perfbench.py --save base.json --md base.md
    .venv/bin/python tools/perfbench/perfbench.py --against base.json --repeats 1
    .venv/bin/python tools/perfbench/perfbench.py --tree /path/to/other/tree
    .venv/bin/python tools/perfbench/perfbench.py --tree /a --tree-b /b   # ABAB, ratio B/A
    .venv/bin/python tools/perfbench/perfbench.py --profile links

``--tree PATH`` sets ``PYTHONPATH=PATH:PATH/packages/flux-stats/src`` so the interpreter runs
that tree's code; the harness checks where ``flux_engine`` and ``flux_stats`` resolve and refuses
to go on if they do not point into the tree. Inputs always come from this repository's
fixtures, so two trees see the same bundles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import pstats
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
TEMPORAL_FILES = ("billing.fluid.yml", "persona.flux.yml", "simulation.flux.yml", "world.flux.yml")
PARALLEL_PREFIXES = ("emit ", "frame ")
TIMING_LINE = re.compile(r"^\s{2}(\S.*?)\s+(\d+\.\d+)\s*$")
MIB = 1024 * 1024


@dataclass(frozen=True)
class Case:
    """One timed benchmark: a bundle, a command and a scale."""

    name: str
    bundle: str
    command: str  # "run" or "plan-nostage"
    scale: float
    extra: Tuple[str, ...] = ()
    copy_files: Tuple[str, ...] = ()  # when set, only these files of the bundle are copied


CASES: Dict[str, Case] = {
    c.name: c
    for c in (
        Case("counts", "tests/fixtures/counts-invoices", "run", 30),
        Case("links", "tests/fixtures/related-star", "run", 100),
        Case("links-plan", "tests/fixtures/related-star", "plan-nostage", 100),
        Case("temporal", "tests/fixtures/temporal", "run", 100, copy_files=TEMPORAL_FILES),
        Case("history", "tests/fixtures/history-window", "run", 15),
        Case(
            "consumers",
            "tests/fixtures/consumers/bundle",
            "run",
            10,
            extra=("--profile", "tests/fixtures/consumers/subscriptions.profile.json"),
        ),
        Case("large", "e2e/roundtrip/truth", "run", 16),
    )
}
KERNEL_BUNDLES = ("links", "large")  # --scale is refused on kernel 0.1.0, so declared sizes
HALF_BUNDLES = ("counts", "links")
SHARD_BUNDLE = "counts"
VERIFY_BUNDLES = (("counts", 1.0), ("links", 1.0), ("history", 1.0))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def canonical_plan_sha(path: Path) -> str:
    """sha256 of plan.json with its wall-clock ``generated_at`` stamp removed."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.pop("generated_at", None)
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_timings(text: str) -> Dict[str, float]:
    """The phases of the ``timings (seconds...`` block, in order."""
    phases: Dict[str, float] = {}
    inside = False
    for line in text.splitlines():
        if line.startswith("timings ("):
            inside = True
            continue
        if not inside:
            continue
        match = TIMING_LINE.match(line)
        if match:
            phases[match.group(1)] = float(match.group(2))
    return phases


def serial_seconds(phases: Dict[str, float]) -> Optional[float]:
    if not phases:
        return None
    return round(
        sum(v for k, v in phases.items() if k != "total" and not k.startswith(PARALLEL_PREFIXES)),
        4,
    )


class Tree:
    """A source tree (or the venv's own install) the subprocesses run."""

    def __init__(self, path: Optional[str], python: str, label: str) -> None:
        self.python = python
        self.label = label
        self.path = str(Path(path).resolve()) if path else None
        self.env = dict(os.environ)
        self.env.pop("PYTHONDONTWRITEBYTECODE", None)
        if self.path:
            self.env["PYTHONPATH"] = os.pathsep.join(
                [self.path, os.path.join(self.path, "packages", "flux-stats", "src")]
            )
        self.warmed = False

    def check(self) -> Tuple[str, str]:
        code = "import flux_engine, flux_stats; print(flux_engine.__file__); print(flux_stats.__file__)"
        out = subprocess.run(
            [self.python, "-P", "-c", code],
            env=self.env,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        files = out.stdout.split()
        if out.returncode != 0 or len(files) != 2:
            sys.exit(
                f"cannot import flux_engine and flux_stats for tree {self.label}: {out.stderr.strip()}"
            )
        if self.path and not all(f.startswith(self.path + os.sep) for f in files):
            sys.exit(
                f"tree {self.path} is not what runs: flux_engine={files[0]} flux_stats={files[1]}"
            )
        return files[0], files[1]


@dataclass
class Exec:
    wall: float
    cpu: float
    rss: int
    rc: int
    out: str
    err: str


def _ps_seconds(text: str) -> float:
    """``ps`` CPU time, ``[[dd-]hh:]mm:ss[.cc]``, as seconds."""
    days, _, clock = text.rpartition("-")
    total = 0.0
    for part in clock.split(":"):
        total = total * 60 + float(part)
    return total + (float(days) * 86400 if days else 0.0)


class GroupSampler(threading.Thread):
    """Samples ``ps`` for the child's process group while the child runs.

    The engine's workers come from a forkserver, so they are not children of the process the
    harness waits for: ``wait4`` never reports their CPU time or memory. The sampler sees them
    (they share the process group) and keeps, per process, the largest CPU time and resident set
    it saw; a worker's last fraction of a second before it exits can be missed.
    """

    def __init__(self, pgid: int) -> None:
        super().__init__(daemon=True)
        self.pgid = pgid
        self.halt = threading.Event()
        self.cpu: Dict[int, float] = {}
        self.rss: Dict[int, int] = {}

    def run(self) -> None:
        while not self.halt.wait(0.2):
            out = subprocess.run(
                ["ps", "-axo", "pid=,pgid=,rss=,time="], capture_output=True, text=True
            ).stdout
            for line in out.splitlines():
                f = line.split()
                if len(f) == 4 and int(f[1]) == self.pgid:
                    pid = int(f[0])
                    self.cpu[pid] = max(self.cpu.get(pid, 0.0), _ps_seconds(f[3]))
                    self.rss[pid] = max(self.rss.get(pid, 0), int(f[2]) * 1024)


def execute(tree: Tree, args: Sequence[str], workdir: Path) -> Exec:
    """Run ``fluxsim`` once; wall by ``perf_counter``, CPU and peak RSS of the whole tree.

    The child's own CPU time and peak resident set come from ``wait4`` (exact); every other
    process of its group (the pool workers) comes from the sampler. The CPU time is the child's
    plus the workers' (approximate), the peak is the largest single process.
    """
    cmd = [tree.python, "-m", "flux_engine.cli", *args]
    with tempfile.TemporaryFile("w+") as so, tempfile.TemporaryFile("w+") as se:
        start = time.perf_counter()
        proc = subprocess.Popen(
            cmd, env=tree.env, cwd=workdir, stdout=so, stderr=se, start_new_session=True
        )
        sampler = GroupSampler(proc.pid)
        sampler.start()
        _, status, usage = os.wait4(proc.pid, 0)
        wall = time.perf_counter() - start
        sampler.halt.set()
        proc.returncode = os.waitstatus_to_exitcode(status)
        so.seek(0)
        se.seek(0)
        own_rss = usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss * 1024
        workers = [p for p in sampler.cpu if p != proc.pid]
        cpu = usage.ru_utime + usage.ru_stime + sum(sampler.cpu[p] for p in workers)
        rss = max([own_rss] + [sampler.rss[p] for p in workers])
        return Exec(wall, cpu, rss, proc.returncode, so.read(), se.read())


def stage_bundle(case: Case, scratch: Path) -> Path:
    src = ROOT / case.bundle
    if not case.copy_files:
        return src
    dest = scratch / ("bundle-" + case.name)
    if not dest.exists():
        dest.mkdir(parents=True)
        for name in case.copy_files:
            shutil.copy2(src / name, dest / name)
    return dest


def run_dir_of(state: Path) -> Path:
    runs = sorted((state / "runs").iterdir())
    if len(runs) != 1:
        raise RuntimeError(f"expected one run directory in {state}, found {len(runs)}")
    return runs[0]


def fingerprint(run_dir: Path, shards: bool = False) -> Dict[str, Any]:
    """The bit-exact record of a run directory."""
    fp: Dict[str, Any] = {}
    plan = run_dir / "plan.json"
    if plan.exists():
        doc = json.loads(plan.read_text(encoding="utf-8"))
        fp["planDigest"] = doc.get("planDigest")
        fp["plan.json"] = canonical_plan_sha(plan)
    receipt = run_dir / "receipt.json"
    if receipt.exists():
        fp["runDigest"] = json.loads(receipt.read_text(encoding="utf-8")).get("runDigest")
    emits = run_dir / "emits"
    if emits.is_dir():
        for f in sorted(emits.iterdir()):
            if f.is_file() and (".shard-" in f.name) == shards:
                fp["emits/" + f.name] = sha256_file(f)
    return fp


def count_rows(run_dir: Path) -> int:
    receipt = run_dir / "receipt.json"
    if receipt.exists():
        emits = json.loads(receipt.read_text(encoding="utf-8")).get("emits", {})
        return sum(int(e.get("rows", 0)) for e in emits.values())
    doc = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    return sum(int(e.get("generator", {}).get("rows", 0)) for e in doc.get("emits", {}).values())


def case_args(
    case: Case, bundle: Path, jobs: int, state: Path, scale: Optional[float] = None
) -> List[str]:
    verb = "run" if case.command == "run" else "plan"
    flags = [] if case.command == "run" else ["--no-stage"]
    s = case.scale if scale is None else scale
    return [verb, str(bundle), *flags, "--scale", f"{s:g}", "--timings", "--jobs", str(jobs),
            "--state-dir", str(state), *(str(ROOT / x) if (ROOT / x).exists() else x for x in case.extra)]  # fmt: skip


def run_case_once(tree: Tree, case: Case, bundle: Path, jobs: int, scratch: Path) -> Dict[str, Any]:
    state = Path(tempfile.mkdtemp(prefix="state-", dir=scratch))
    try:
        res = execute(tree, case_args(case, bundle, jobs, state), scratch)
        if res.rc != 0:
            raise RuntimeError(f"{case.name} jobs={jobs} exited {res.rc}: {res.err.strip()[-400:]}")
        run_dir = run_dir_of(state)
        phases = parse_timings(res.out)
        rows = count_rows(run_dir)
        return {
            "wall": round(res.wall, 4),
            "cpu": round(res.cpu, 4),
            "rss_bytes": res.rss,
            "rows": rows,
            "rows_per_s": round(rows / res.wall, 1) if res.wall else None,
            "serial_s": serial_seconds(phases),
            "phases": phases,
            "fingerprint": fingerprint(run_dir),
        }
    finally:
        shutil.rmtree(state, ignore_errors=True)


def warm_up(tree: Tree, scratch: Path) -> None:
    """One untimed pass per tree so ``__pycache__`` is written before anything is timed."""
    if tree.warmed:
        return
    tree.warmed = True
    state = Path(tempfile.mkdtemp(prefix="warm-", dir=scratch))
    try:
        bundle = str(ROOT / CASES["counts"].bundle)
        a = ["--state-dir", str(state)]
        execute(tree, ["run", bundle, "--scale", "1", "--jobs", "2", *a], scratch)
        run_id = run_dir_of(state).name
        execute(tree, ["verify", bundle, "--run-id", run_id, "--jobs", "2", *a], scratch)
        k = Path(tempfile.mkdtemp(prefix="warm-k-", dir=scratch))
        execute(tree, ["plan", bundle, "--kernel", "0.1.0", "--state-dir", str(k)], scratch)
        execute(
            tree, ["plan", bundle, "--scale", "1", "--no-stage", "--state-dir", str(k)], scratch
        )
        shutil.rmtree(k, ignore_errors=True)
    finally:
        shutil.rmtree(state, ignore_errors=True)


def load() -> List[float]:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return []


def summarise(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    walls = [r["wall"] for r in runs]
    best = min(runs, key=lambda r: r["wall"])
    return {
        "best_wall": min(walls),
        "median_wall": round(statistics.median(walls), 4),
        "rows": best["rows"],
        "rows_per_s": round(best["rows"] / min(walls), 1),
        "serial_s": best["serial_s"],
        "cpu_s": best["cpu"],
        "peak_rss_mib": round(max(r["rss_bytes"] for r in runs) / MIB, 1),
    }


@dataclass
class Results:
    timed: Dict[str, Any] = field(default_factory=dict)
    digests: Dict[str, Any] = field(default_factory=dict)
    verify: Dict[str, Any] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)
    mismatches: List[str] = field(default_factory=list)


def check_same(res: Results, what: str, fps: Dict[str, Dict[str, Any]]) -> None:
    """Every entry of ``fps`` (keyed by jobs or tree) must hold the same fingerprint."""
    items = list(fps.items())
    ref_key, ref = items[0]
    for key, fp in items[1:]:
        for name in sorted(set(ref) | set(fp)):
            if ref.get(name) != fp.get(name):
                res.problems.append(f"{what}: {name} differs between {ref_key} and {key}")


def _run_trees(
    trees: List[Tree], case: Case, jobs: List[int], repeats: int, bundle: Path, scratch: Path
) -> Dict[Tuple[str, int], List[Dict[str, Any]]]:
    runs: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    for j in jobs:
        for _ in range(repeats):
            for tree in trees:  # ABAB: the trees alternate run by run
                runs.setdefault((tree.label, j), []).append(
                    run_case_once(tree, case, bundle, j, scratch)
                )
    return runs


def _tree_entry(
    res: Results, case: Case, tree: Tree, jobs: List[int], runs: Dict[Tuple[str, int], Any]
) -> Dict[str, Any]:
    per_jobs: Dict[str, Any] = {}
    fps: Dict[str, Dict[str, Any]] = {}
    for j in jobs:
        rs = runs[(tree.label, j)]
        per_jobs[str(j)] = {**summarise(rs), "runs": rs}
        for k, r in enumerate(rs):
            fps[f"jobs={j}#{k}"] = r["fingerprint"]
    check_same(res, f"{case.name} [{tree.label}]", fps)
    return {"jobs": per_jobs, "fingerprint": next(iter(fps.values()))}


def _compare_trees(
    res: Results, case: Case, trees: List[Tree], entry: Dict[str, Any], jobs: List[int]
) -> None:
    a, b = (entry["trees"][t.label] for t in trees)
    check_same(res, f"{case.name} (tree {trees[0].label} vs {trees[1].label})",
               {trees[0].label: a["fingerprint"], trees[1].label: b["fingerprint"]})  # fmt: skip
    entry["ratio_b_over_a"] = {
        str(j): {
            "best": round(b["jobs"][str(j)]["best_wall"] / a["jobs"][str(j)]["best_wall"], 3),
            "median": round(b["jobs"][str(j)]["median_wall"] / a["jobs"][str(j)]["median_wall"], 3),
        }
        for j in jobs
    }  # fmt: skip


def timed_cases(
    res: Results, trees: List[Tree], cases: List[Case], jobs: List[int], repeats: int, scratch: Path
) -> None:
    for case in cases:
        bundle = stage_bundle(case, scratch)
        before = load()
        runs = _run_trees(trees, case, jobs, repeats, bundle, scratch)
        entry: Dict[str, Any] = {
            "case": asdict_case(case),
            "load_before": before,
            "load_after": load(),
            "trees": {t.label: _tree_entry(res, case, t, jobs, runs) for t in trees},
        }
        if len(trees) == 2:
            _compare_trees(res, case, trees, entry, jobs)
        res.timed[case.name] = entry
        res.digests["timed:" + case.name] = entry["trees"][trees[0].label]["fingerprint"]
        print(f"done {case.name}  load {before} -> {entry['load_after']}", file=sys.stderr)


def asdict_case(case: Case) -> Dict[str, Any]:
    return {
        "bundle": case.bundle,
        "command": case.command,
        "scale": case.scale,
        "extra": list(case.extra),
    }


def _digest_plans() -> List[Tuple[str, List[str], bool]]:
    plans: List[Tuple[str, List[str], bool]] = []
    for name in KERNEL_BUNDLES:
        plans.append((f"kernel-0.1.0:{name}", ["--kernel", "0.1.0"], False))
    for name in HALF_BUNDLES:
        plans.append((f"scale-0.5:{name}", ["--scale", "0.5"], False))
    plans.append((f"shards:{SHARD_BUNDLE}", ["--scale", "1"], True))
    return plans


def _digest_once(
    tree: Tree, label: str, flags: List[str], sharded: bool, bundle: Path, j: int, scratch: Path
) -> Dict[str, Any]:
    """Plan and apply *bundle* at *j* jobs in a fresh state directory; its fingerprint."""
    state = Path(tempfile.mkdtemp(prefix="state-", dir=scratch))
    try:
        base = ["--state-dir", str(state), "--jobs", str(j)]
        plan = execute(tree, ["plan", str(bundle), *flags, *base], scratch)
        if plan.rc != 0:
            raise RuntimeError(f"{label} plan failed: {plan.err.strip()[-300:]}")
        run_id = run_dir_of(state).name
        for shard in ("1/2", "2/2") if sharded else (None,):
            extra = ["--shard", shard] if shard else []
            done = execute(tree, ["apply", str(bundle), "--run-id", run_id, *extra, *base], scratch)
            if done.rc != 0:
                raise RuntimeError(f"{label} apply failed: {done.err.strip()[-300:]}")
        fp: Dict[str, Any] = dict(fingerprint(run_dir_of(state)))
        if sharded:
            fp.update(fingerprint(run_dir_of(state), shards=True))
        return fp
    finally:
        shutil.rmtree(state, ignore_errors=True)


def digest_cases(res: Results, tree: Tree, scratch: Path) -> None:
    """Kernel 0.1.0, scale 0.5 and shard fingerprints at jobs 1 and 4."""
    for label, flags, sharded in _digest_plans():
        bundle = stage_bundle(CASES[label.split(":")[1]], scratch)
        fps = {
            f"jobs={j}": _digest_once(tree, label, flags, sharded, bundle, j, scratch)
            for j in (1, 4)
        }
        check_same(res, label, fps)
        res.digests[label] = fps["jobs=1"]
        print(f"done {label}", file=sys.stderr)


def verify_timing(
    res: Results, tree: Tree, jobs: List[int], repeats: int, scratch: Path, labelled: bool = False
) -> None:
    """Time ``verify`` of each bundle on *tree*; with *labelled* each row is keyed by the tree."""
    for name, scale in VERIFY_BUNDLES:
        case = CASES[name]
        bundle = stage_bundle(case, scratch)
        state = Path(tempfile.mkdtemp(prefix="state-", dir=scratch))
        try:
            made = execute(
                tree,
                ["run", str(bundle), "--scale", f"{scale:g}", "--state-dir", str(state)],
                scratch,
            )
            if made.rc != 0:
                raise RuntimeError(f"verify setup {name} failed: {made.err.strip()[-300:]}")
            run_id = run_dir_of(state).name
            row: Dict[str, Any] = {}
            for j in jobs:
                best = None
                rc = 0
                for _ in range(repeats):
                    r = execute(tree, ["verify", str(bundle), "--run-id", run_id, "--jobs", str(j),
                                       "--state-dir", str(state)], scratch)  # fmt: skip
                    rc = r.rc
                    best = r.wall if best is None else min(best, r.wall)
                row[str(j)] = {"best_wall": round(best or 0.0, 4), "exit": rc}
            res.verify[f"{name} [{tree.label}]" if labelled else name] = {
                "scale": scale,
                "jobs": row,
            }
        finally:
            shutil.rmtree(state, ignore_errors=True)
        print(f"done verify {name}", file=sys.stderr)


def markdown(res: Results, trees: List[Tree], jobs: List[int]) -> str:
    head = "| case | tree | rows | jobs | best wall s | median wall s | rows/s | speedup vs 1 | serial s | CPU s | peak RSS MiB |"
    lines = [head, "|" + "---|" * 11]
    for name, entry in res.timed.items():
        for tree in trees:
            per = entry["trees"][tree.label]["jobs"]
            base = per[str(jobs[0])]["best_wall"]
            for j in jobs:
                s = per[str(j)]
                serial = "" if s["serial_s"] is None else f"{s['serial_s']:.2f}"
                lines.append(
                    f"| {name} | {tree.label} | {s['rows']} | {j} | {s['best_wall']:.2f} | {s['median_wall']:.2f} "
                    f"| {s['rows_per_s']:.0f} | {base / s['best_wall']:.2f}x | {serial} | {s['cpu_s']:.2f} "
                    f"| {s['peak_rss_mib']:.0f} |"
                )
        if "ratio_b_over_a" in entry:
            ratios = ", ".join(f"jobs {j}: {v['best']:.3f} (median {v['median']:.3f})"
                               for j, v in entry["ratio_b_over_a"].items())  # fmt: skip
            lines.append(f"| {name} | B/A wall | | | | | | | | | {ratios} |")
    if res.verify:
        lines += _verify_table(res)
    return "\n".join(lines) + "\n"


def _verify_table(res: Results) -> List[str]:
    vjobs = list(next(iter(res.verify.values()))["jobs"])
    lines = ["", "verify, best wall seconds", "", "| bundle | " + " | ".join(f"jobs {j}" for j in vjobs) + " |",
             "|" + "---|" * (len(vjobs) + 1)]  # fmt: skip
    for name, v in res.verify.items():
        lines.append(
            f"| {name} | " + " | ".join(f"{v['jobs'][j]['best_wall']:.2f}" for j in vjobs) + " |"
        )
    return lines


def compare_against(res: Results, path: Path) -> None:
    saved = json.loads(path.read_text(encoding="utf-8"))["digests"]
    for key, fp in res.digests.items():
        old = saved.get(key)
        if old is None:
            res.mismatches.append(f"{key}: not in the baseline")
            continue
        for name in sorted(set(old) | set(fp)):
            if old.get(name) != fp.get(name):
                res.mismatches.append(f"{key}: {name}: baseline {old.get(name)} now {fp.get(name)}")


def profile_case(tree: Tree, case: Case, scratch: Path, out_dir: Optional[Path]) -> str:
    bundle = stage_bundle(case, scratch)
    state = Path(tempfile.mkdtemp(prefix="state-", dir=scratch))
    prof = scratch / f"{case.name}.prof"
    try:
        cmd = [tree.python, "-m", "cProfile", "-o", str(prof), "-m", "flux_engine.cli",
               *case_args(case, bundle, 1, state)]  # fmt: skip
        proc = subprocess.run(cmd, env=tree.env, cwd=scratch, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"profile {case.name} exited {proc.returncode}: {proc.stderr[-300:]}"
            )
        stats = pstats.Stats(str(prof))
    finally:
        shutil.rmtree(state, ignore_errors=True)
    raw: Dict[Any, Any] = stats.stats  # type: ignore[attr-defined]
    total = sum(v[2] for v in raw.values()) or 1.0
    top = sorted(raw.items(), key=lambda kv: kv[1][2], reverse=True)[:30]
    out = [f"profile {case.name} (jobs 1, scale {case.scale:g}), total tottime {total:.2f} s", "",
           "top 30 functions by tottime", f"{'tottime':>9} {'cumtime':>9} {'calls':>9}  function"]  # fmt: skip
    for (fname, line, func), (_, nc, tt, ct, _) in top:
        out.append(f"{tt:9.3f} {ct:9.3f} {nc:9d}  {short_path(fname)}:{line}({func})")
    roll: Dict[str, float] = {}
    for (fname, _, _), (_, _, tt, _, _) in raw.items():
        key = short_path(fname) if not fname.startswith(("~", "<")) else "<builtins and C>"
        roll[key] = roll.get(key, 0.0) + tt
    out += ["", "per-module rollup (tottime share)"]
    for key, tt in sorted(roll.items(), key=lambda kv: kv[1], reverse=True)[:25]:
        out.append(f"{100 * tt / total:6.1f}%  {tt:8.3f}  {key}")
    text = "\n".join(out) + "\n"
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"profile-{case.name}.txt").write_text(text, encoding="utf-8")
    return text


def short_path(fname: str) -> str:
    for marker in ("/flux_engine/", "/flux_stats/", "/site-packages/"):
        if marker in fname:
            head, _, tail = fname.partition(marker)
            return (marker.strip("/") + "/" + tail) if marker != "/site-packages/" else tail
    return fname


#: Every option as ``(flag, argparse keywords)``.
_OPTIONS: Tuple[Tuple[str, Dict[str, Any]], ...] = (
    ("--cases", {"default": "all", "help": "comma list of case names, or all"}),
    ("--jobs", {"default": "1,2,4,8,16"}),
    ("--repeats", {"type": int, "default": 3}),
    ("--scale", {"type": float, "help": "override every timed case's scale"}),
    ("--tree", {"help": "source tree to run (default: the interpreter's own install)"}),
    ("--tree-b", {"help": "second tree, alternated with --tree run by run (ABAB)"}),
    ("--python", {"default": sys.executable, "help": "interpreter that runs fluxsim"}),
    ("--save", {"help": "write every result as JSON"}),
    ("--md", {"help": "also write the Markdown table to this file"}),
    ("--against", {"help": "compare every digest with this saved JSON; exit 2 on a mismatch"}),
    ("--profile", {"help": "comma list of cases (or all) to run under cProfile at jobs 1"}),
    ("--profile-dir", {"help": "write profile-<case>.txt files here as well"}),
    (
        "--no-digest-cases",
        {"action": "store_true", "help": "skip kernel, half-scale and shard cases"},
    ),
    ("--no-verify", {"action": "store_true", "help": "skip the verify timing"}),
    ("--list", {"action": "store_true", "help": "list the built-in cases and exit"}),
)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Benchmark the fluxsim CLI.")
    for flag, keywords in _OPTIONS:
        ap.add_argument(flag, **keywords)
    return ap


def _profile_all(
    ap: argparse.ArgumentParser,
    args: argparse.Namespace,
    names: List[str],
    tree: Tree,
    scratch: Path,
) -> None:
    wanted = names if args.profile == "all" else args.profile.split(",")
    for n in wanted:
        if n not in CASES:
            ap.error(f"unknown profile case {n}")
        warm_up(tree, scratch)
        out_dir = Path(args.profile_dir) if args.profile_dir else None
        print(profile_case(tree, CASES[n], scratch, out_dir))


def _measure(
    res: Results,
    args: argparse.Namespace,
    trees: List[Tree],
    cases: List[Case],
    jobs: List[int],
    scratch: Path,
) -> None:
    for t in trees:
        warm_up(t, scratch)
    timed_cases(res, trees, cases, jobs, args.repeats, scratch)
    if not args.no_digest_cases:
        digest_cases(res, trees[0], scratch)
    if not args.no_verify:
        for t in trees:
            verify_timing(res, t, [1, 2, 4, 8, 16], args.repeats, scratch, labelled=len(trees) > 1)


def _report(
    res: Results,
    args: argparse.Namespace,
    trees: List[Tree],
    jobs: List[int],
    modules: Dict[str, Tuple[str, str]],
    seen: Optional[str],
    started: float,
) -> Dict[str, Any]:
    return {
        "machine": {
            "cpu_count": os.cpu_count(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "PYTHONDONTWRITEBYTECODE_seen": seen,
            "PYTHONDONTWRITEBYTECODE_used": None,
            "trees": {t.label: t.path for t in trees},
            "modules": modules,
            "wall_seconds": round(time.time() - started, 1),
        },
        "jobs": jobs,
        "repeats": args.repeats,
        "timed": res.timed,
        "verify": res.verify,
        "digests": res.digests,
        "problems": res.problems,
        "mismatches": res.mismatches,
    }


def _verdict(res: Results, two_trees: bool) -> int:
    for p in res.problems:
        print("NOT BIT-EXACT:", p, file=sys.stderr)
    for m in res.mismatches:
        print("MISMATCH:", m, file=sys.stderr)
    if not res.problems and not res.mismatches:
        print(
            "bit-exact: every fingerprint is identical across jobs"
            + (" and trees" if two_trees else ""),
            file=sys.stderr,
        )
    return 2 if res.mismatches else (1 if res.problems else 0)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = _parser()
    args = ap.parse_args(argv)

    if args.list:
        for c in CASES.values():
            print(f"{c.name:12} {c.command:13} scale {c.scale:<6g} {c.bundle}")
        return 0
    names = list(CASES) if args.cases == "all" else args.cases.split(",")
    unknown = [n for n in names if n not in CASES]
    if unknown:
        ap.error(f"unknown cases {unknown}; known: {', '.join(CASES)}")
    cases = [Case(**{**CASES[n].__dict__, "scale": args.scale or CASES[n].scale}) for n in names]
    jobs = [int(x) for x in args.jobs.split(",")]

    trees = [Tree(args.tree, args.python, "A")]
    if args.tree_b:
        trees.append(Tree(args.tree_b, args.python, "B"))
    modules = {t.label: t.check() for t in trees}
    seen = os.environ.get("PYTHONDONTWRITEBYTECODE")
    scratch = Path(tempfile.mkdtemp(prefix="perfbench-"))
    res = Results()
    started = time.time()
    try:
        if args.profile:
            _profile_all(ap, args, names, trees[0], scratch)
            return 0
        _measure(res, args, trees, cases, jobs, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    table = markdown(res, trees, jobs)
    print(table)
    if args.md:
        Path(args.md).write_text(table, encoding="utf-8")
    if args.against:
        compare_against(res, Path(args.against))
    if args.save:
        report = _report(res, args, trees, jobs, modules, seen, started)
        Path(args.save).write_text(
            json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    return _verdict(res, bool(args.tree_b))


if __name__ == "__main__":
    sys.exit(main())
