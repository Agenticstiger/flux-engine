# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SDMetrics' multi-table QualityReport and DiagnosticReport, as a second judge.

SDMetrics is not a dependency of this harness and never becomes one: it requires
``copulas``, whose licence is the Business Source License 1.1. Install
``sdmetrics`` (and ``pyarrow``) into a private environment of your own and run
this file with that environment's interpreter, as a single file::

    <private-python> -I sdmetrics_judge.py --source DIR --synthetic DIR \
        --metadata metadata.json --out sdmetrics.json

It reads records (both copies), so it runs on the data owner's side.

Large tables that are no relationship's parent are sampled, with a fixed seed and
the same row budget on both sides, so that a run stays within minutes; a parent is
always read whole, so referential checks see every key. The report records what
was sampled.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


def _frame(path: Path, *, rows: Optional[int], seed: int) -> Any:
    import pandas as pd
    import pyarrow.parquet as pq

    files = sorted(path.glob("*.parquet")) if path.is_dir() else [path]
    table = pq.ParquetDataset([str(file) for file in files]).read()
    frame = table.to_pandas()
    for column in frame.columns:
        # SDMetrics reads numbers, not Decimal objects.
        if (
            frame[column].dtype == object
            and frame[column].map(lambda v: hasattr(v, "as_tuple")).any()
        ):
            frame[column] = pd.to_numeric(
                frame[column].map(lambda v: None if v is None else float(v))
            )
        elif (
            str(frame[column].dtype).startswith("date")
            or frame[column].map(lambda v: v.__class__.__name__ == "date").any()
        ):
            frame[column] = pd.to_datetime(frame[column])
    for column in frame.columns:
        # DuckDB writes an INTEGER as int32 and FLUX every integer as int64; compare values,
        # not physical widths.
        if str(frame[column].dtype).lower().startswith(("int", "uint")):
            frame[column] = frame[column].astype("Int64" if frame[column].isna().any() else "int64")
    if rows is not None and len(frame) > rows:
        frame = frame.sample(n=rows, random_state=seed).reset_index(drop=True)
    return frame


def _load(
    metadata: Mapping[str, Any], args: argparse.Namespace
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, int]]:
    """Both copies of every table, a table that is no parent sampled to the row budget."""
    parents = {
        relationship["parent_table_name"] for relationship in metadata.get("relationships", [])
    }
    real: Dict[str, Any] = {}
    synthetic: Dict[str, Any] = {}
    sampled: Dict[str, int] = {}
    for table in metadata["tables"]:
        budget = None if table in parents else args.max_rows
        source_path = args.source / f"{table}.parquet"
        source = source_path if source_path.exists() else args.source / table
        real[table] = _frame(source, rows=budget, seed=args.seed)
        synthetic[table] = _frame(args.synthetic / table, rows=budget, seed=args.seed)
        if budget is not None and budget in (len(real[table]), len(synthetic[table])):
            sampled[table] = budget
    return real, synthetic, sampled


def _report(
    report: Any, real: Mapping[str, Any], synthetic: Mapping[str, Any], metadata: Mapping[str, Any]
) -> Dict[str, Any]:
    """One report's score, its property scores, and each property's mean per table."""
    started = time.monotonic()
    report.generate(dict(real), dict(synthetic), dict(metadata), verbose=False)
    properties = {
        row["Property"]: _score(row["Score"]) for _, row in report.get_properties().iterrows()
    }
    tables: Dict[str, Dict[str, float]] = {}
    for name in properties:
        try:
            details = report.get_details(property_name=name)
        except Exception:  # some properties have no per-table breakdown
            continue
        if "Table" in details.columns and "Score" in details.columns:
            for table, group in details.groupby("Table"):
                tables.setdefault(str(table), {})[name] = _score(group["Score"].mean())
    return {
        "score": _score(report.get_score()),
        "properties": properties,
        "tables": tables,
        "seconds": round(time.monotonic() - started, 1),
    }


def _score(value: Any) -> Optional[float]:
    """A score as a float; NaN (a property SDMetrics could not compute) becomes null."""
    number = float(value)
    return None if number != number else number


def disjoint_sample(frame: Any, taken: Any, *, seed: int) -> Any:
    """Rows of *frame* outside *taken*, as many as *taken* holds; None when too few are left."""
    rest = frame.drop(taken.index)
    if len(rest) < len(taken):
        return None
    return rest.sample(n=len(taken), random_state=seed)


def _distances(frames: Mapping[str, Any], *, rows: int, iterations: int) -> Dict[str, Any]:
    """SDMetrics' distance-to-closest-record protection on the person views.

    ``DCROverfittingProtection`` asks whether synthetic rows sit closer to the built-from
    people than to the held-out ones, and runs again with a disjoint sample of built-from
    people in their place: the null split it is read against. ``DCRBaselineProtection``
    compares the synthetic rows' distance to the real ones with that of random rows.
    """
    import numpy as np
    from sdmetrics.single_table.privacy import DCRBaselineProtection, DCROverfittingProtection

    columns = {
        name: {"sdtype": "numerical" if str(dtype).startswith(("float", "int")) else "categorical"}
        for name, dtype in frames["ori"].dtypes.items()
    }
    metadata = {"tables": {"person": {"columns": columns}}}
    common = {"metadata": metadata, "table_name": "person", "num_rows_subsample": rows}
    out: Dict[str, Any] = {"rows": rows}
    # The metric reads "closer to training" against a 50/50 split, so the training
    # side is sampled down to the holdout's size first.
    training = frames["ori"].sample(
        n=min(len(frames["ori"]), len(frames["control"])), random_state=0
    )
    out["overfittingTrainingRows"] = len(training)

    def overfitting(validation: Any) -> Any:
        # SDMetrics samples with numpy's global generator. Seeding it gives every
        # call the same synthetic and training samples, so two calls differ only in
        # their validation side.
        np.random.seed(0)
        return DCROverfittingProtection.compute_breakdown(
            real_training_data=training,
            synthetic_data=frames["syn"],
            real_validation_data=validation,
            num_iterations=iterations,
            **common,
        )

    out["overfitting"] = overfitting(frames["control"])
    # The null split: a second, disjoint sample of built-from people in place of the
    # held-out ones. Nobody in it was held out, so whatever lean it shows is the
    # metric's own (SDMetrics scales each column by each reference set's range).
    others = disjoint_sample(frames["ori"], training, seed=1)
    out["overfittingNull"] = (
        overfitting(others)
        if others is not None
        else {"skipped": f"fewer than {len(training)} built-from rows outside the training sample"}
    )
    # This metric subsamples its real reference to *rows* as well, which hides some
    # memorisation from it (SDMetrics issue 934). The overfitting metric's references
    # are used whole while the holdout has at most *rows* people.
    np.random.seed(0)
    out["baseline"] = DCRBaselineProtection.compute_breakdown(
        real_data=frames["ori"],
        synthetic_data=frames["syn"],
        num_iterations=iterations,
        **common,
    )
    return out


def privacy(argv: Sequence[str]) -> int:
    """The ``privacy`` command: the distance metrics of three person views, as JSON."""
    import pandas as pd
    import sdmetrics

    parser = argparse.ArgumentParser(prog="sdmetrics_judge.py privacy")
    for side in ("ori", "syn", "control"):
        parser.add_argument(f"--{side}", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--rows", type=int, default=10_000, help="rows sampled per iteration")
    parser.add_argument("--iterations", type=int, default=3)
    args = parser.parse_args(argv)
    frames = {side: pd.read_parquet(getattr(args, side)) for side in ("ori", "syn", "control")}
    smallest = min(len(frame) for frame in frames.values())
    if smallest == 0:
        print("sdmetrics_judge privacy: a person view is empty", file=sys.stderr)
        return 2
    started = time.monotonic()
    # SDMetrics samples without replacement, so no sample may exceed the smallest side.
    out = _distances(frames, rows=min(args.rows, smallest), iterations=args.iterations)
    out["sdmetrics"] = sdmetrics.__version__
    out["seconds"] = round(time.monotonic() - started, 1)
    args.out.write_text(
        json.dumps(out, indent=2, sort_keys=True, allow_nan=False, default=float) + "\n",
        encoding="utf-8",
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["privacy"]:
        return privacy(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source", required=True, type=Path, help="one <table>.parquet or <table>/ per table"
    )
    parser.add_argument("--synthetic", required=True, type=Path)
    parser.add_argument(
        "--metadata", required=True, type=Path, help="SDMetrics multi-table metadata JSON"
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-rows", type=int, default=300_000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(arguments)

    import sdmetrics
    from sdmetrics.reports.multi_table import DiagnosticReport, QualityReport

    metadata: Mapping[str, Any] = json.loads(args.metadata.read_text(encoding="utf-8"))
    real, synthetic, sampled = _load(metadata, args)
    out: Dict[str, Any] = {
        "sdmetrics": sdmetrics.__version__,
        "sampled": sampled,
        "maxRows": args.max_rows,
        "quality": _report(QualityReport(), real, synthetic, metadata),
        "diagnostic": _report(DiagnosticReport(), real, synthetic, metadata),
    }
    args.out.write_text(
        json.dumps(out, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
