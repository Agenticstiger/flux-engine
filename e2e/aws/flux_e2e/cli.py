# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``python -m flux_e2e``: run the end-to-end accuracy test, or sweep its AWS resources.

A run walks these steps, each recorded in ``<work-root>/<run-id>/run.json`` so a
finished step is not repeated when the same run id is given again:

1. ``source``: DuckDB's tpcds extension writes the source; each in-scope table is
   copied to Parquet.
2. ``contract``: the FLUID 0.7.7 contract, one expose per table, with its keys.
3. ``profile``: ``fluid profile`` per table, merged into the one profile the
   Simulation pins.
4. ``generate``: FLUX in its sandbox, from the contract and the profile only.
5. ``publish``: both copies to S3, catalogued in Glue (needs the live flag).
6. ``judge``: the consumer queries and the key checks in Athena, on both copies.
7. ``sdmetrics``: SDMetrics, when ``--sdmetrics-python`` names a private interpreter.
8. ``report``: ``report.md`` and ``report.json``, and one line in ``history.jsonl``.

Results stay in the work root, which is private to the operator: nothing a run
measures belongs in the repository.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from flux_e2e import aws, generator, judges, owner, privacy, report, tpcds

STEPS = (
    "source",
    "contract",
    "profile",
    "generate",
    "privacy",
    "publish",
    "judge",
    "sdmetrics",
    "report",
)
_HERE = Path(__file__).resolve().parent
REPO = _HERE.parents[2]


def _default_work_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "flux-e2e-aws"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m flux_e2e",
        description="Run FLUX's end-to-end accuracy test on AWS, or sweep its AWS resources.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run the end-to-end test (steps 1 to 8), then sweep")
    run.add_argument("--work-root", type=Path, default=_default_work_root())
    run.add_argument("--run-id", help="reuse a run's directory and skip its finished steps")
    run.add_argument("--scale-factor", type=float, default=1.0)
    run.add_argument("--scope", choices=sorted(tpcds.SCOPES), default="store")
    run.add_argument("--seed", type=int, default=42)
    run.add_argument(
        "--holdout",
        type=float,
        default=0.0,
        help="share of customers held out of the source, with their rows (the privacy control set)",
    )
    run.add_argument("--holdout-seed", type=int, default=17, help="seed of the holdout split")
    run.add_argument(
        "--anonymeter-python", type=Path, help="a private interpreter with anonymeter installed"
    )
    run.add_argument("--privacy-attacks", type=int, default=2000, help="targets per privacy attack")
    run.add_argument("--no-aws", action="store_true", help="skip publishing and the Athena judges")
    run.add_argument(
        "--jobs", type=int, default=min(8, os.cpu_count() or 1), help="FLUX worker processes"
    )
    run.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    run.add_argument("--aws-profile", default=os.environ.get("AWS_PROFILE"))
    run.add_argument(
        "--sdmetrics-python", type=Path, help="a private interpreter with sdmetrics installed"
    )
    run.add_argument("--sdmetrics-max-rows", type=int, default=300_000)
    run.add_argument("--until", choices=STEPS, help="stop after this step")
    run.add_argument("--keep", action="store_true", help="do not sweep AWS at the end")
    run.add_argument(
        "--allow-unconfined",
        action="store_true",
        help="run the generator under env -i alone where sandbox-exec is missing",
    )
    run.add_argument(
        "--label", default="", help="a note for the score history, such as the change under test"
    )

    sweep = commands.add_parser("sweep", help="delete every AWS resource under the harness prefix")
    sweep.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    sweep.add_argument("--aws-profile", default=os.environ.get("AWS_PROFILE"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (aws.NotLive, ValueError) as refusal:
        parser.exit(2, f"python -m flux_e2e: error: {refusal}\n")
    except RuntimeError as failure:
        parser.exit(1, f"python -m flux_e2e: failed: {failure}\n")


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "sweep":
        outcome = aws.sweep(aws.session(args.aws_profile, args.region))
        print(json.dumps(outcome, indent=2))
        return 1 if aws.left_over(outcome) else 0
    return _run(args)


class Run:
    """One run's directory and record."""

    def __init__(self, root: Path, run_id: str, commit: str) -> None:
        self.work_root = root
        self.dir = root / run_id
        self.id = run_id
        self.commit = commit
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir.chmod(0o700)
        self.path = self.dir / "run.json"
        self.record: Dict[str, Any] = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {"runId": run_id}
        )
        self.record.setdefault("steps", {})

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.record, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )

    def step(self, name: str, work: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        done = self.record["steps"]
        if name in done:
            print(f"[{name}] done earlier, skipped")
            return done[name]
        print(f"[{name}] ...", flush=True)
        started = time.monotonic()
        outcome = work()
        outcome["seconds"] = round(time.monotonic() - started, 1)
        outcome["harnessCommit"] = self.commit
        done[name] = outcome
        self.save()
        print(f"[{name}] {outcome['seconds']}s", flush=True)
        return outcome


def _git(*argv: str, cwd: Path = REPO) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


#: Settings a resumed run must share with the run it resumes: steps already done used them.
_LOCKED = ("scope", "scaleFactor", "seed", "region", "holdout", "holdoutSeed")
#: What a run recorded before a setting existed is that setting's default.
_BEFORE = {"holdout": 0.0, "holdoutSeed": 17}
_ENGINE_PATHS = ("flux_engine", "packages", "pyproject.toml", "uv.lock")


def _config(args: argparse.Namespace, tables: List[str]) -> Dict[str, Any]:
    return {
        "scope": args.scope,
        "tables": tables,
        "scaleFactor": args.scale_factor,
        "seed": args.seed,
        "holdout": args.holdout,
        "holdoutSeed": args.holdout_seed,
        "region": args.region,
        "label": args.label,
        "startedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "harnessCommit": _git("rev-parse", "HEAD"),
        # The last commit that changed what the generator side installs.
        "engineCommit": _git("log", "-1", "--format=%H", "--", *_ENGINE_PATHS),
        "engineDirty": bool(_git("status", "--porcelain", "--", *_ENGINE_PATHS)),
    }


@dataclass(frozen=True)
class _Paths:
    """Where a run keeps its files."""

    dir: Path

    @property
    def source_db(self) -> Path:
        return self.dir / "source" / "tpcds.duckdb"

    @property
    def parquet(self) -> Path:
        return self.dir / "source" / "parquet"

    @property
    def contract(self) -> Path:
        return self.dir / "owner" / "tpcds.fluid.yaml"

    @property
    def profile(self) -> Path:
        return self.dir / "owner" / "source.profile.json"

    @property
    def gen_root(self) -> Path:
        return self.dir / "generator"


def _open_run(args: argparse.Namespace) -> Tuple[Run, Path, List[str]]:
    """The run's directory and record, refusing settings that differ from a resumed run's."""
    run_id = aws.check_run_id(
        args.run_id or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    )
    if args.anonymeter_python and not 0 < args.holdout < 1:
        raise ValueError(
            "the privacy judge needs a control set: pass --holdout 0.1 (any share in (0, 1))"
        )
    work_root = args.work_root.expanduser().resolve()
    run = Run(work_root, run_id, _git("rev-parse", "HEAD"))
    tables = list(tpcds.SCOPES[args.scope])
    fresh = _config(args, tables)
    recorded = run.record.setdefault("config", fresh)
    changed = [key for key in _LOCKED if recorded.get(key, _BEFORE.get(key)) != fresh[key]]
    if changed:
        raise ValueError(
            f"run {run_id} was started with a different {', '.join(changed)}; "
            "give the same settings or start a new run id"
        )
    run.save()
    return run, work_root, tables


def _build_steps(
    run: Run, args: argparse.Namespace, tables: List[str], paths: _Paths
) -> List[Tuple[str, Callable[[], Dict[str, Any]]]]:
    """The owner's and the generator's steps, in order."""
    return [
        ("source", lambda: _source(args, paths.source_db, tables, paths.parquet)),
        (
            "contract",
            lambda: owner.write_contract(
                paths.source_db, tables, paths.contract, region=args.region
            ),
        ),
        ("profile", lambda: _profile(run, paths.contract, paths.parquet, tables, paths.profile)),
        ("generate", lambda: _generate(run, args, paths.contract, paths.profile, paths.gen_root)),
    ]


def _run(args: argparse.Namespace) -> int:
    run, work_root, tables = _open_run(args)
    paths = _Paths(run.dir)
    left_over = False
    try:
        for name, work in _build_steps(run, args, tables, paths):
            run.step(name, work)
            if args.until == name:
                return 0
        if args.anonymeter_python:
            run.step("privacy", lambda: _privacy(run, args, paths.parquet, paths.gen_root))
        if args.until == "privacy":
            return 0
        if not args.no_aws and "judge" not in run.record["steps"]:
            aws.require_live()
            left_over = _aws_steps(
                run, args, aws.Names(run.id), tables, paths.parquet, paths.gen_root
            )
        if args.sdmetrics_python and args.until not in ("publish", "judge"):
            run.step(
                "sdmetrics",
                lambda: _sdmetrics(
                    run, args, tables, paths.contract, paths.parquet, paths.gen_root
                ),
            )
        if args.until in ("publish", "judge", "sdmetrics"):
            return 1 if left_over else 0
    finally:
        print(f"work directory: {run.dir}")
    report.write(run.dir, run.record, history=work_root / "history.jsonl")
    print(f"report: {run.dir / 'report.md'}")
    return 1 if left_over else 0


def _source(
    args: argparse.Namespace, database: Path, tables: List[str], parquet: Path
) -> Dict[str, Any]:
    """dsdgen, then the holdout split when one is asked for, then the Parquet copies."""
    outcome = owner.generate_source(database, args.scale_factor)
    if args.holdout:
        outcome["holdout"] = privacy.split_holdout(database, args.holdout, args.holdout_seed)
        privacy.export_holdout(database, parquet.parent / "holdout")
    outcome["rows"] = owner.export_parquet(database, tables, parquet)
    return outcome


def _privacy(run: Run, args: argparse.Namespace, parquet: Path, gen_root: Path) -> Dict[str, Any]:
    """The person views of the three sides, Anonymeter's attacks, and SDMetrics' distances."""
    folder = run.dir / "privacy"
    holdout = parquet.parent / "holdout"
    dims = {
        table: parquet / f"{table}.parquet"
        for table in ("customer_address", "customer_demographics")
    }
    sides = {
        "ori": {table: parquet / f"{table}.parquet" for table in privacy.PERSON_TABLES},
        "syn": {table: generator.output_files(gen_root, table) for table in privacy.PERSON_TABLES},
        "control": {
            **dims,
            "customer": holdout / "customer.parquet",
            "store_sales": holdout / "store_sales.parquet",
        },
    }
    rows = {
        side: privacy.person_view(sources, folder / f"{side}.parquet")
        for side, sources in sides.items()
    }
    spec = privacy.spec(args.privacy_attacks, args.seed)
    (folder / "spec.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    views = [f"--{side}={folder / f'{side}.parquet'}" for side in sides]
    _private(
        args.anonymeter_python,
        "anonymeter_judge.py",
        [*views, f"--spec={folder / 'spec.json'}", f"--out={folder / 'anonymeter.json'}"],
        folder,
    )
    outcome: Dict[str, Any] = {
        "rows": rows,
        "spec": spec,
        "anonymeter": json.loads((folder / "anonymeter.json").read_text(encoding="utf-8")),
    }
    if args.sdmetrics_python:
        # Optional: a failure here is recorded and keeps the attacks' result.
        try:
            _private(
                args.sdmetrics_python,
                "sdmetrics_judge.py",
                ["privacy", *views, f"--out={folder / 'distance.json'}"],
                folder,
            )
            outcome["distance"] = json.loads((folder / "distance.json").read_text(encoding="utf-8"))
        except RuntimeError as error:
            outcome["distanceError"] = str(error)[:500]
    return outcome


def _private(python: Path, script: str, argv: List[str], home: Path) -> None:
    """Run a judge file under an operator's private interpreter, with no inherited environment."""
    try:
        done = subprocess.run(
            [str(python), "-I", str(_HERE / script), *argv],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8"},
        )
    except OSError as error:
        raise RuntimeError(f"{script} could not start under {python}: {error}") from error
    if done.returncode != 0:
        tail = [line[:300] for line in (done.stderr or done.stdout).strip().splitlines()[-3:]]
        raise RuntimeError(f"{script} failed ({done.returncode}): {' | '.join(tail)}")


def _aws_steps(
    run: Run,
    args: argparse.Namespace,
    names: aws.Names,
    tables: List[str],
    parquet: Path,
    gen_root: Path,
) -> bool:
    """Publish both copies and judge them, then sweep; True when the sweep left something."""
    boto = aws.session(args.aws_profile, args.region)
    left_over = False
    try:
        run.step("publish", lambda: _publish(boto, names, tables, parquet, gen_root))
        if args.until != "publish":
            run.step(
                "judge",
                lambda: _judge(
                    lambda: aws.session(args.aws_profile, args.region), names, tables, run
                ),
            )
    finally:
        if not args.keep:
            left_over = _sweep(run, boto)
    return left_over


def _sweep(run: Run, boto: Any) -> bool:
    """Sweep the prefix and record it; a swept run publishes again when resumed."""
    try:
        outcome = aws.sweep(boto)
    except Exception as error:  # recorded; an exception already in flight keeps propagating
        outcome = {"errors": [f"sweep: {type(error).__name__}: {error}"], "remaining": {}}
    run.record["sweep"] = outcome
    if "publish" in run.record["steps"]:
        run.record["published"] = run.record["steps"].pop("publish")
    run.save()
    if aws.left_over(outcome):
        print(
            f"the sweep left resources behind or failed: {json.dumps(outcome, default=str)}",
            file=sys.stderr,
        )
        return True
    return False


def _profile(
    run: "Run", contract: Path, parquet: Path, tables: List[str], profile: Path
) -> Dict[str, Any]:
    fluid = Path(sys.executable).with_name("fluid")
    documents = owner.profile_tables(
        fluid, contract, parquet, tables, run.dir / "owner" / "profiles"
    )
    digest = owner.merge_profiles(documents, profile)
    published = owner.read_profile_metrics(json.loads(profile.read_text(encoding="utf-8")))
    return {"digest": digest, **published, "fluid": _fluid_version(fluid)}


def _generate(
    run: "Run", args: argparse.Namespace, contract: Path, profile: Path, gen_root: Path
) -> Dict[str, Any]:
    venv = run.dir / "generator-venv"
    installed = generator.build_venv(REPO, venv)
    record = generator.run_generator(
        gen_root,
        venv,
        contract,
        profile,
        work_root=run.work_root,
        seed=args.seed,
        jobs=args.jobs,
        allow_unconfined=args.allow_unconfined,
    )
    return {
        "runId": record["runId"],
        "sandboxExec": record["sandboxExec"],
        "distributions": installed["distributions"],
        "commands": [
            {key: command[key] for key in ("step", "argv", "env", "exitCode", "seconds")}
            for command in record["commands"]
        ],
        "verify": judges.read_verify(record["verify"]),
        "digests": record["digests"],
        "authoring": generator.authoring_notes(gen_root),
    }


def _publish(
    boto: Any, names: aws.Names, tables: List[str], parquet: Path, gen_root: Path
) -> Dict[str, Any]:
    aws.create(boto, names)
    published: Dict[str, Any] = {"bucket": names.bucket, "workgroup": names.workgroup, "tables": {}}
    for table in tables:
        source_types = aws.publish(boto, names, "source", table, [parquet / f"{table}.parquet"])
        synthetic_files = generator.output_files(gen_root, table)
        synthetic_types = aws.publish(boto, names, "synthetic", table, synthetic_files)
        published["tables"][table] = {
            "typesMatch": source_types == synthetic_types,
            "typeDifferences": {
                column: [source_types.get(column), synthetic_types.get(column)]
                for column in sorted(set(source_types) | set(synthetic_types))
                if source_types.get(column) != synthetic_types.get(column)
            },
        }
    return published


def _judge(
    sessions: Callable[[], Any], names: aws.Names, tables: List[str], run: Run
) -> Dict[str, Any]:
    queries = judges.load_queries(tables)
    primary = run.record["steps"]["contract"]["primaryKeys"]
    key_jobs = judges.key_sql(primary, tables)
    jobs = [
        (name, side, sql) for name, sql in queries + key_jobs for side in ("source", "synthetic")
    ]
    results = aws.run_all(sessions, names, jobs)
    per_query = {
        name: judges.compare_query(
            results[(name, "source")], results[(name, "synthetic")], judges.query_roles(sql)
        )
        for name, sql in queries
    }
    keys = {
        side: judges.read_keys({name: results[(name, side)] for name, _ in key_jobs})
        for side in ("source", "synthetic")
    }
    outcome = {
        "queries": per_query,
        "querySummary": judges.summarise_queries(per_query),
        "keys": keys,
        "bytesScanned": sum(result.bytes_scanned for result in results.values()),
    }
    failed = judges.harness_failures(per_query, keys)
    if failed:
        # Not a measurement: the source side or a key check broke. Kept for diagnosis.
        run.record["failedJudge"] = outcome
        run.save()
        raise RuntimeError(f"the judge could not run {len(failed)} checks: {', '.join(failed[:5])}")
    return outcome


def _fluid_version(fluid: Path) -> str:
    return subprocess.run([str(fluid), "--version"], capture_output=True, text=True).stdout.strip()


def _sdmetrics(
    run: Run,
    args: argparse.Namespace,
    tables: List[str],
    contract: Path,
    parquet: Path,
    gen_root: Path,
) -> Dict[str, Any]:
    metadata = report.sdmetrics_metadata(contract, tables)
    path = run.dir / "judges" / "sdmetrics-metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    out = run.dir / "judges" / "sdmetrics.json"
    for table in tables:
        generator.output_files(gen_root, table)  # refuses a link before SDMetrics reads them
    subprocess.run(
        [
            str(args.sdmetrics_python),
            "-I",
            str(_HERE / "sdmetrics_judge.py"),
            "--source",
            str(parquet),
            "--synthetic",
            str(gen_root / "parquet"),
            "--metadata",
            str(path),
            "--out",
            str(out),
            "--max-rows",
            str(args.sdmetrics_max_rows),
        ],
        check=True,
        # It reads records and needs no credentials: none of the operator's environment.
        env={"PATH": "/usr/bin:/bin", "HOME": str(path.parent), "LANG": "C.UTF-8"},
    )
    return json.loads(out.read_text(encoding="utf-8"))
