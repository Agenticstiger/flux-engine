# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Render ``docs/accuracy/REPORT.md`` from a ``flux-accuracy-nightly/1`` artefact.

The page is generated, never edited: ``test_report_page.py`` re-renders it
from the committed artefact (``docs/accuracy/latest.json``) and fails when
the two differ. Every number on it comes out of the artefact; the only prose
here is the frame around the numbers and the "What was not tested" section.

    python -m tests.accuracy.report.render --artefact latest.json --out REPORT.md

Standard library only, and deterministic: the same artefact renders the same
bytes on any machine.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = ["FAMILY_TITLES", "NOT_TESTED", "main", "render"]

#: Display order and headings. A family the artefact carries that is not
#: listed here is rendered after these, under its own name.
FAMILY_TITLES: Tuple[Tuple[str, str], ...] = (
    ("fidelity-s", "1. Fidelity metrics on the profile S"),
    ("gof-kernel", "2. Goodness of fit: kernel samplers"),
    ("gof-power", "2. Goodness of fit: power against a 2% shift"),
    ("gof-numerics", "2. Goodness of fit: the decimal Φ and Φ⁻¹"),
    ("gof-kind", "2. Goodness of fit: declared distributions, by kind"),
    ("gate-roc", "3. The fidelity gate's own accuracy"),
    ("properties", "4. Properties and metamorphic relations"),
    ("streams", "5. Seed-tree stream quality"),
    ("journeys", "6. Journey conformance"),
    ("determinism", "7. Determinism"),
    ("recovery", "Recovery tests: calibrate and accuracy"),
)

NOT_TESTED: Tuple[str, ...] = (
    "**Simulation-based calibration.** It waits for the fitted calibrator. Today "
    "`calibrate` reads a declared scorecard and fits nothing, so there is no "
    "posterior to calibrate. The recovery tests above cover what the declared "
    "path does.",
    "**Drift-detection accuracy.** It waits for drift detection on S, which does not exist yet.",
    "**Joint structure in generated data.** Without `given`, the generator draws "
    "each column independently, and Section 1 measures the resulting shortfall in "
    "ContingencySimilarity and CorrelationSimilarity. With `given`, it draws only "
    "the links a plan declares, and two numeric columns are never linked directly, "
    "because a histogram column is a leaf. The fidelity check tests each declared "
    "link through a grouped property: a grouped `marginalHistogram` for a histogram "
    "drawn given a categorical, a grouped `levelHistogram` for a categorical drawn "
    "given a categorical. Nothing here claims joint fidelity beyond the declared links.",
    "**Privacy and re-identification resistance.** S is aggregate by "
    "construction. Its privacy properties are outside this program.",
    "**The agentic tier and the stochastic sweep executor.** Neither is built; "
    "the engine refuses both, and the main suite tests those refusals.",
    "**Real data.** Every record set here is synthetic, drawn from stated "
    "parameters (ADR 0003). A fit to an owner's real committed state is "
    "measured only by that owner's own fidelity check.",
)


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _number(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value == 0:
            return "0"
        if abs(value) >= 1e4 or abs(value) < 1e-3:
            return f"{value:.3g}"
        return f"{value:.4g}"
    return str(value)


def _measured(metrics: Mapping[str, Any], limit: int = 4) -> str:
    """A compact reading of a case's metrics: the scalars first, then a test table."""
    parts: List[str] = []
    tests = metrics.get("tests")
    if isinstance(tests, Mapping):
        for name, entry in tests.items():
            if isinstance(entry, Mapping) and "pvalue" in entry:
                parts.append(f"{name} p={_number(float(entry['pvalue']))}")
    for key, value in metrics.items():
        if len(parts) >= limit:
            break
        if key == "tests" or isinstance(value, (Mapping, list, tuple)):
            continue
        parts.append(f"{key}={_number(value)}")
    return ", ".join(parts[:limit]) if parts else "-"


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> List[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    return lines


def _family_section(family: str, title: str, summary: Mapping[str, Any]) -> List[str]:
    lines = [f"## {title}", ""]
    known = summary.get("knownDefects", 0)
    cleared = summary.get("unconfirmed", 0)
    notes = []
    if known:
        notes.append(f"{known} pin a known defect")
    if cleared:
        notes.append(f"{cleared} failed the first pass and were cleared at the confirmation seed")
    lines.append(
        f"{summary['passed']} of {summary['cases']} measured cases passed"
        + (f"; {'; '.join(notes)}." if notes else ".")
    )
    lines.append("")
    rows = []
    for entry in summary["results"]:
        rows.append(
            (
                entry["case"],
                _number(entry["n"]) if entry.get("n") is not None else "-",
                (
                    "known defect"
                    if entry.get("knownDefect")
                    else (
                        "pass"
                        if entry["passed"]
                        else ("cleared on confirmation" if entry.get("unconfirmed") else "**FAIL**")
                    )
                ),
                _measured(entry.get("metrics") or {}),
                entry["criterion"],
            )
        )
    lines.extend(_table(("Case", "n", "Result", "Measured", "Criterion"), rows))
    lines.append("")
    if family == "gate-roc":
        lines.extend(_gate_tables(summary))
    return lines


def _gate_tables(summary: Mapping[str, Any]) -> List[str]:
    lines: List[str] = []
    for entry in summary["results"]:
        metrics = entry.get("metrics") or {}
        withhold = metrics.get("withholdByFactor")
        if not isinstance(withhold, Mapping):
            continue
        series = entry["case"].removesuffix("-roc")
        lines.append(
            f"**{series}**: tolerance {_number(metrics.get('tolerance'))}, "
            f"{_number(metrics.get('committedRecords'))} committed and {_number(entry.get('n'))} "
            f"synthetic records per trial; ROC area {_number(metrics.get('auc'))}."
        )
        lines.append("")
        rows = []
        for factor, cell in sorted(withhold.items(), key=lambda item: float(item[0])):
            withheld, trials = cell["withheld"], cell["trials"]
            rows.append(
                (
                    f"{float(factor):g} × tolerance",
                    _number(cell.get("truth")),
                    f"{withheld} of {trials}",
                    _number(withheld / trials) if trials else "-",
                )
            )
        lines.extend(
            _table(
                ("Injected deviation", "Population deviation", "Withheld", "Withhold rate"), rows
            )
        )
        lines.append("")
        lines.append(
            f"False rejects at no deviation: {_number(metrics.get('falseRejectRateAtZero'))} "
            f"(95% upper bound {_number(metrics.get('falseRejectUpper95'))}). False accepts at twice "
            f"the tolerance: {_number(metrics.get('falseAcceptRateAtTwice'))} (95% upper bound "
            f"{_number(metrics.get('falseAcceptUpper95'))})."
        )
        lines.append("")
    return lines


def _practrand_section(practrand: Mapping[str, Any]) -> List[str]:
    lines = ["## 5. PractRand", ""]
    if not practrand:
        return lines + ["PractRand did not run in this artefact.", ""]
    rows = []
    for mode, summary in sorted(practrand.items()):
        evaluations = summary.get("evaluations") or {}
        noted = ", ".join(
            f"{name}: {count}" for name, count in sorted(evaluations.items()) if count
        )
        rows.append(
            (
                mode,
                _number(summary.get("maxLengthBytes")),
                summary.get("verdict"),
                noted or "no anomalies",
            )
        )
    lines.extend(_table(("Feed", "Bytes tested", "Verdict", "Anomalies by evaluation"), rows))
    lines.append("")
    return lines


def _determinism_section(determinism: Mapping[str, Any]) -> List[str]:
    lines = ["## 7. Determinism matrix", ""]
    cells = determinism.get("cells") or []
    if not cells:
        return lines + ["No determinism manifest is in this artefact.", ""]
    identical = determinism.get("identical")
    lines.append(
        f"{len(cells)} cells, {_number(determinism.get('digests'))} digests each: "
        + ("every cell's manifest is identical." if identical else "**the manifests differ.**")
    )
    lines.append("")
    rows = []
    for cell in cells:
        environment = cell.get("environment") or {}
        rows.append(
            (
                cell["cell"],
                environment.get("platform", "-"),
                environment.get("python", "-"),
                environment.get("numpy", "-"),
            )
        )
    lines.extend(_table(("Cell", "Platform", "Python", "NumPy"), rows))
    lines.append("")
    for difference in determinism.get("differences") or []:
        lines.append(f"- `{difference['key']}` differs.")
    if determinism.get("differences"):
        lines.append("")
    return lines


def _benchmark_section(benchmarks: Optional[Sequence[Mapping[str, Any]]]) -> List[str]:
    lines = ["## 8. Speed", ""]
    if not benchmarks:
        return lines + ["No benchmark is in this artefact.", ""]
    lines.append(
        "Median wall time per call on the nightly runner. A benchmark more than 3 times "
        "slower than the committed snapshot fails the night; more than 1.5 times is flagged."
    )
    lines.append("")
    rows = []
    for row in benchmarks:
        ratio = row.get("ratioToBaseline")
        rows.append(
            (
                row["name"],
                f"{row['medianSeconds'] * 1e6:,.1f} µs",
                _number(row.get("rounds")),
                f"{ratio:.2f}×" if ratio is not None else "-",
            )
        )
    lines.extend(_table(("Benchmark", "Median", "Rounds", "Against snapshot"), rows))
    lines.append("")
    return lines


def _mutation_section(mutation: Optional[Mapping[str, Any]]) -> List[str]:
    lines = ["## Mutation testing", ""]
    if not mutation or not mutation.get("modules"):
        return lines + [
            "Mutation testing runs weekly; this artefact is from a night without it.",
            "",
        ]
    lines.append(
        "Kill rate on covered lines. Each module keeps the floor set from its first "
        f"baseline, and none may drop more than {mutation['maxDrop']:.0%} in a week."
    )
    lines.append("")
    rows = []
    for name, entry in sorted(mutation["modules"].items()):
        counts = entry.get("counts") or {}
        rate = entry.get("killRate")
        floor = entry.get("floor", mutation.get("target"))
        rows.append(
            (
                name,
                f"{rate:.1%}" if rate is not None else "-",
                f"{floor:.0%}" if floor is not None else "-",
                ", ".join(f"{status}: {count}" for status, count in sorted(counts.items())),
            )
        )
    lines.extend(_table(("Module", "Kill rate", "Floor", "Mutants"), rows))
    lines.append("")
    return lines


#: The nightly workflow's page, below the repository. A link to one run does not last:
#: GitHub drops runs after its retention period, and a snapshot committed from another
#: repository names a run this one never had. The workflow's page is always there and
#: lists the latest runs.
NIGHTLY_WORKFLOW_PAGE = "actions/workflows/accuracy-nightly.yml"


def _workflow_link(run_url: str) -> str:
    """The nightly workflow's page in the repository *run_url* belongs to, or ``""``."""
    repository, found, _ = run_url.rpartition("/actions/runs/")
    return f"{repository}/{NIGHTLY_WORKFLOW_PAGE}" if found else ""


def render(artefact: Mapping[str, Any]) -> str:
    """The report page for *artefact*, as Markdown."""
    run = artefact.get("run") or {}
    verdict = artefact.get("verdict") or {}
    families: Mapping[str, Any] = artefact.get("families") or {}
    lines: List[str] = [
        "# Accuracy report",
        "",
        "<!-- Generated by tests/accuracy/report/render.py from docs/accuracy/latest.json."
        " Do not edit by hand; re-render instead. -->",
        "",
        "What each family measures, and the pass criterion every number below is held to, is "
        "in [DESIGN.md](DESIGN.md). Results are measured at the stated sizes and seeds, not "
        "established in general.",
        "",
    ]
    commit = str(run.get("commit") or "")[:12]
    workflow_link = _workflow_link(str(run.get("url") or ""))
    lines.append(
        f"Latest nightly run: {run.get('date', '-')}, commit `{commit or '-'}`, base seed "
        f"`{run.get('baseSeed', '-')}`, confirmation seed `{run.get('confirmSeed', '-')}`"
        + (f", [nightly workflow]({workflow_link})" if workflow_link else "")
        + "."
    )
    lines.append("")
    lines.append(f"Verdict: **{'passed' if verdict.get('passed') else 'failed'}**.")
    lines.append("")
    for heading, items in (
        ("Reasons", verdict.get("reasons") or []),
        ("Missing", verdict.get("missing") or []),
        ("Flagged", verdict.get("flagged") or []),
        (
            "Known defects, pinned as strict xfails until their fixes merge",
            verdict.get("knownDefects") or [],
        ),
    ):
        if items:
            lines.append(f"{heading}:")
            lines.append("")
            lines.extend(f"- {_cell(item)}" for item in items)
            lines.append("")

    pytest_counts = artefact.get("pytest")
    summary_rows = []
    order = [name for name, _ in FAMILY_TITLES] + sorted(
        set(families) - {n for n, _ in FAMILY_TITLES}
    )
    titles: Dict[str, str] = dict(FAMILY_TITLES)
    for family in order:
        if family in families:
            entry = families[family]
            summary_rows.append((titles.get(family, family), entry["cases"], entry["passed"]))
    lines.append("## Summary")
    lines.append("")
    if pytest_counts:
        lines.append(
            f"pytest: {_number(pytest_counts['tests'])} tests, {pytest_counts['failures']} failed, "
            f"{pytest_counts['errors']} errored, {pytest_counts['skipped']} skipped, "
            f"{pytest_counts['xfailed']} expected failures (strict xfails)."
        )
        lines.append("")
    lines.extend(_table(("Family", "Cases", "Passed"), summary_rows))
    lines.append("")

    for family in order:
        if family in families:
            lines.extend(_family_section(family, titles.get(family, family), families[family]))
    lines.extend(_practrand_section(artefact.get("practrand") or {}))
    lines.extend(_determinism_section(artefact.get("determinism") or {}))
    lines.extend(_benchmark_section(artefact.get("benchmarks")))
    lines.extend(_mutation_section(artefact.get("mutation")))
    lines.append("## What was not tested")
    lines.append("")
    lines.extend(f"- {item}" for item in NOT_TESTED)
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.accuracy.report.render", description=__doc__
    )
    parser.add_argument(
        "--artefact", type=Path, required=True, help="a flux-accuracy-nightly/1 file"
    )
    parser.add_argument("--out", type=Path, required=True, help="where to write the page")
    args = parser.parse_args(argv)
    artefact = json.loads(args.artefact.read_text(encoding="utf-8"))
    args.out.write_text(render(artefact), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
