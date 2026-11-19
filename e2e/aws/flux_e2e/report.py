# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The run's report (``report.md``, ``report.json``) and its line in the score history.

Both stay in the operator's work root. The history holds one JSON line per run with
the headline numbers, so a later run after a change to FLUX reads against the
earlier ones.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import yaml

from flux_e2e import tpcds


def sdmetrics_metadata(contract: Path, tables: Sequence[str]) -> Dict[str, Any]:
    """SDMetrics' multi-table metadata, from the contract's columns and keys.

    SDMetrics keys a table by one column, so a table whose primary key has several
    columns gets none, and a relationship over several columns is left out.
    """
    document = yaml.safe_load(contract.read_text(encoding="utf-8"))
    out: Dict[str, Any] = {"tables": {}, "relationships": []}
    primary: Dict[str, str] = {}
    for expose in document["exposes"]:
        table = expose["exposeId"]
        if table not in tables:
            continue
        entities = (expose.get("semantics") or {}).get("entities", [])
        key = next(
            (entity.get("expr") for entity in entities if entity.get("type") == "primary"), None
        )
        foreign = {
            column
            for relationship in (expose.get("semantics") or {}).get("relationships", [])
            for column in relationship["fromColumns"]
        }
        columns: Dict[str, Any] = {}
        for column in expose["contract"]["schema"]:
            name, declared = column["name"], str(column["type"]).lower()
            if name == key or name in foreign:
                columns[name] = {"sdtype": "id"}
            elif declared.startswith(("decimal", "bigint", "int")):
                columns[name] = {"sdtype": "numerical"}
            elif declared.startswith("date"):
                columns[name] = {"sdtype": "datetime", "datetime_format": "%Y-%m-%d"}
            else:
                columns[name] = {"sdtype": "categorical"}
        entry: Dict[str, Any] = {"columns": columns}
        if key:
            entry["primary_key"] = key
            primary[table] = key
        out["tables"][table] = entry
    for fk in tpcds.foreign_keys(tables):
        if len(fk.columns) == 1 and primary.get(fk.target) == fk.target_columns[0]:
            out["relationships"].append(
                {
                    "parent_table_name": fk.target,
                    "parent_primary_key": fk.target_columns[0],
                    "child_table_name": fk.table,
                    "child_foreign_key": fk.columns[0],
                }
            )
    return out


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _num(value: Optional[float], digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def headline(record: Mapping[str, Any]) -> Dict[str, Any]:
    """The numbers the score history keeps for this run."""
    steps = record.get("steps", {})
    judge = steps.get("judge", {})
    keys = (judge.get("keys") or {}).get("synthetic", {}).get("summary", {})
    control = (judge.get("keys") or {}).get("source", {}).get("summary", {})
    generate = steps.get("generate", {})
    verify = generate.get("verify", {})
    sdm = steps.get("sdmetrics", {})
    columns = [
        note for table in (generate.get("authoring") or {}).values() for note in table["columns"]
    ]
    summary = judge.get("querySummary") or {}
    return {
        "runId": record.get("runId"),
        "label": record.get("config", {}).get("label"),
        "startedAt": record.get("config", {}).get("startedAt"),
        "engineCommit": record.get("config", {}).get("engineCommit"),
        "bundleDigest": (generate.get("digests") or {}).get("bundle"),
        "outputDigest": (generate.get("digests") or {}).get("outputs"),
        "scaleFactor": record.get("config", {}).get("scaleFactor"),
        "scope": record.get("config", {}).get("scope"),
        "duckdb": steps.get("source", {}).get("duckdb"),
        "columnsFromProfile": sum(1 for note in columns if note["source"] == "profile"),
        "columnsFromContract": sum(1 for note in columns if note["source"] == "contract"),
        "columnsAssumed": sum(1 for note in columns if note["source"] == "assumed"),
        "verifyScorecard": verify.get("scorecardStatus"),
        "verifyScore": verify.get("scorecardScore"),
        "shapeFlags": sum((verify.get("shapeFlags") or {}).values()),
        "queryAggregateErrorMedian": summary.get("aggregateErrorMedian"),
        "queryShareDistanceMedian": summary.get("shareDistanceMedian"),
        "queriesWithin10Percent": summary.get("aggregateWithin10Percent"),
        "queriesCompared": summary.get("aggregateCompared"),
        "queriesKeysDisjoint": summary.get("keysDisjoint"),
        "queriesErrorSynthetic": (summary.get("statuses") or {}).get("error synthetic", 0),
        "queriesEmptySynthetic": (summary.get("statuses") or {}).get("empty synthetic", 0),
        "orphanRateMedian": keys.get("orphanRateMedian"),
        "foreignKeysWithoutOrphans": keys.get("foreignKeysWithoutOrphans"),
        "foreignKeys": keys.get("foreignKeys"),
        "sourceOrphanRateMax": control.get("orphanRateMax"),
        "primaryKeysUnique": keys.get("primaryKeysUnique"),
        "primaryKeys": keys.get("primaryKeys"),
        "sdmetricsQuality": (sdm.get("quality") or {}).get("score"),
        "sdmetricsDiagnostic": (sdm.get("diagnostic") or {}).get("score"),
        "holdout": record.get("config", {}).get("holdout"),
        **_privacy_headline(steps.get("privacy") or {}),
    }


def write(run_dir: Path, record: Mapping[str, Any], *, history: Path) -> None:
    head = headline(record)
    (run_dir / "report.json").write_text(
        json.dumps({"headline": head, "record": record}, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "report.md").write_text(render(record, head), encoding="utf-8")
    # One line per run: a report written again for the same run replaces its line.
    lines = history.read_text(encoding="utf-8").splitlines() if history.exists() else []
    kept = [line for line in lines if line and json.loads(line).get("runId") != head["runId"]]
    kept.append(json.dumps(head, sort_keys=True, default=str))
    staged = history.with_name(history.name + ".tmp")
    staged.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.replace(staged, history)


def render(record: Mapping[str, Any], head: Mapping[str, Any]) -> str:
    sections = [
        _run_section(record),
        ["## Headline", ""] + [f"- {key}: {value}" for key, value in head.items()],
        _boundary_section(record),
        _verify_section(record),
        _query_section(record),
        _keys_section(record),
        _sdmetrics_section(record),
        _privacy_section(record),
        _authoring_section(record),
        _publish_section(record),
    ]
    return "\n\n".join("\n".join(section) for section in sections) + "\n"


_STEP_ORDER = (
    "source",
    "contract",
    "profile",
    "generate",
    "privacy",
    "publish",
    "judge",
    "sdmetrics",
)


def _run_section(record: Mapping[str, Any]) -> List[str]:
    config, steps = record.get("config", {}), record.get("steps", {})
    source, profile = steps.get("source", {}), steps.get("profile", {})
    return [
        f"# FLUX end-to-end accuracy run {record.get('runId')}",
        "",
        "A TPC-DS-shaped source generated with DuckDB's tpcds extension; not a TPC-DS benchmark result.",
        "",
        "## Run",
        "",
        f"- scope `{config.get('scope')}` ({len(config.get('tables', []))} tables), "
        f"scale factor {config.get('scaleFactor')}, seed {config.get('seed')}, region {config.get('region')}",
        f"- flux-engine `{config.get('engineCommit')}`{' (dirty)' if config.get('engineDirty') else ''}; "
        f"DuckDB {source.get('duckdb')}, tpcds extension {source.get('tpcdsExtension')}",
        f"- profiler: {profile.get('fluid')}; profile {profile.get('digest')}: "
        f"{profile.get('metricsPublished')} metrics published, {profile.get('metricsWithheld')} withheld",
        "- step seconds: "
        + ", ".join(
            f"{name} {steps[name].get('seconds')}" for name in _STEP_ORDER if name in steps
        ),
    ]


def _boundary_section(record: Mapping[str, Any]) -> List[str]:
    generate = record.get("steps", {}).get("generate", {})
    lines = [
        "## Boundary",
        "",
        f"- sandbox-exec confinement: {generate.get('sandboxExec')}",
        "- generator environment: " + ", ".join(generate.get("distributions", [])),
    ]
    for command in generate.get("commands", []):
        lines.append(
            f"- `{command['step']}` exit {command['exitCode']} in {command['seconds']}s: "
            f"`{' '.join(command['argv'])}`"
        )
    if generate.get("commands"):
        lines.append(
            f"- environment: `{json.dumps(generate['commands'][0]['env'], sort_keys=True)}`"
        )
    digests = generate.get("digests") or {}
    lines.append(f"- bundle {digests.get('bundle')}; outputs {digests.get('outputs')}")
    return lines


_VERIFY_KEYS = (
    "exitCode",
    "rowsConform",
    "scorecardStatus",
    "scorecardScore",
    "dimensions",
    "fidelityStatus",
    "refusalCodes",
    "shapeTests",
    "shapeFlags",
    "shapeColumnsUnread",
)


def _verify_section(record: Mapping[str, Any]) -> List[str]:
    verify = record.get("steps", {}).get("generate", {}).get("verify", {})
    return ["## Judge 1: FLUX verify against the pinned profile", ""] + [
        f"- {key}: {verify.get(key)}" for key in _VERIFY_KEYS
    ]


def _query_section(record: Mapping[str, Any]) -> List[str]:
    judge = record.get("steps", {}).get("judge", {})
    lines = [
        "## Judge 2: consumer queries in Athena, as rates",
        "",
        f"- summary: {json.dumps(judge.get('querySummary'), sort_keys=True)}",
        f"- bytes scanned (both sides, all judge queries): {judge.get('bytesScanned')}",
        "",
        "| query | status | rows src | rows syn | aggregate error | share distance | key overlap |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, entry in (judge.get("queries") or {}).items():
        lines.append(
            f"| {name} | {entry.get('status')} | {entry.get('rowsSource')} | "
            f"{entry.get('rowsSynthetic')} | {_pct(entry.get('aggregateError'))} | "
            f"{_num(entry.get('shareDistance'))} | {_num(entry.get('keyOverlap'))} |"
        )
    return lines


def _keys_section(record: Mapping[str, Any]) -> List[str]:
    keys = record.get("steps", {}).get("judge", {}).get("keys") or {}
    source, synthetic = keys.get("source", {}), keys.get("synthetic", {})
    lines = [
        "## Judge 3: key integrity (source is the control)",
        "",
        "| primary key | rows src | duplicate rate src | rows syn | duplicate rate syn |",
        "|---|---|---|---|---|",
    ]
    for table, entry in (synthetic.get("primaryKeys") or {}).items():
        control = (source.get("primaryKeys") or {}).get(table, {})
        lines.append(
            f"| {table} | {control.get('rows')} | {_pct(control.get('duplicateRate'))} | "
            f"{entry.get('rows')} | {_pct(entry.get('duplicateRate'))} |"
        )
    lines += [
        "",
        "| foreign key | orphan rate src | orphan rate syn | references syn |",
        "|---|---|---|---|",
    ]
    for name, entry in (synthetic.get("foreignKeys") or {}).items():
        control = (source.get("foreignKeys") or {}).get(name, {})
        lines.append(
            f"| {name} | {_pct(control.get('orphanRate'))} | {_pct(entry.get('orphanRate'))} | "
            f"{entry.get('references')} |"
        )
    return lines


def _scores(scores: Mapping[str, Any]) -> str:
    return ", ".join(f"{key} {_num(value)}" for key, value in scores.items())


def _privacy_headline(step: Mapping[str, Any]) -> Dict[str, Any]:
    """Per criterion, the highest risk any of its attacks found, with its interval."""
    best = (step.get("anonymeter") or {}).get("headline") or {}
    out: Dict[str, Any] = {}
    for criterion, key in (
        ("singlingOut", "privacySinglingOut"),
        ("linkability", "privacyLinkability"),
        ("inference", "privacyInference"),
    ):
        entry = best.get(criterion)
        out[key] = (
            None
            if entry is None
            else {
                "risk": entry.get("risk"),
                "ci": entry.get("ci"),
                "attack": entry.get("attack"),
                "beatsBaseline": entry.get("beatsBaseline"),
                "of": entry.get("of"),
                "failed": entry.get("failed"),
                "verbatimCopy": (entry.get("verbatimCopy") or {}).get("risk"),
            }
        )
    # Every attack's own risk too, so that a run with more attacks than an earlier
    # one can still be compared with it attack by attack.
    attacks = (step.get("anonymeter") or {}).get("attacks") or {}
    out["privacyAttackRisks"] = {
        name: outcome.get("risk") for name, outcome in sorted(attacks.items())
    } or None
    distance = step.get("distance") or {}
    out["privacyDistanceOverfitting"] = (distance.get("overfitting") or {}).get("score")
    out["privacyDistanceNull"] = (distance.get("overfittingNull") or {}).get("score")
    return out


def _rate_text(rate: Any) -> str:
    return "n/a" if not rate else f"{100 * rate['value']:.1f}% +/- {100 * rate['error']:.1f}"


def _privacy_section(record: Mapping[str, Any]) -> List[str]:
    lines = ["## Judge 5: privacy (Anonymeter, private environment)", ""]
    step = record.get("steps", {}).get("privacy")
    if not step:
        return lines + ["- not run (no --anonymeter-python, or no --holdout)"]
    anonymeter = step.get("anonymeter") or {}
    return (
        lines
        + _privacy_setup(record, step)
        + _attack_table(anonymeter)
        + _privacy_headline_lines(anonymeter)
        + _distance_lines(step)
    )


def _privacy_setup(record: Mapping[str, Any], step: Mapping[str, Any]) -> List[str]:
    """What was attacked, and how: the holdout, the sizes and the columns."""
    holdout = record.get("steps", {}).get("source", {}).get("holdout") or {}
    spec = step.get("spec") or {}
    anonymeter = step.get("anonymeter") or {}
    return [
        "- The source is TPC-DS-shaped data generated with DuckDB's tpcds extension and holds no "
        "real people: this measures the method, not a real customer base.",
        f"- holdout: {holdout.get('fraction')} of {holdout.get('customers')} customers "
        f"(seed {holdout.get('seed')}); moved {holdout.get('heldOut')}",
        "- addresses and demographics stay in the source (other customers and their sales "
        "refer to them); each held-out person's link to them leaves. Addresses only held-out "
        f"people live at: {holdout.get('addressesOfHeldOutOnly', 'not counted')}",
        f"- person rows: {step.get('rows')}; Anonymeter {anonymeter.get('anonymeter')}; "
        f"targets per attack: {anonymeter.get('attacksUsed')}",
        _sampling_line(anonymeter),
        "- verbatim copy: the same attack with the built-from rows standing in for the "
        "synthetic copy, the risk a generator that memorised its input would score here",
        "- Anonymeter draws some randomness of its own (the linkability baseline, the singling "
        "out queries), so a rerun with the same seed varies a little",
        f"- auxiliary columns: {', '.join(spec.get('auxiliary', []))}",
        f"- secret columns: {', '.join(spec.get('secrets', {}))}",
        f"- linkage sets: {spec.get('linkageSets')}; wide: {spec.get('wideLinkageSets', 'not run')}",
        "",
    ]


def _sampling_line(anonymeter: Mapping[str, Any]) -> str:
    for key, side in (("oriSampledTo", "built-from"), ("controlSampledTo", "held-out")):
        if anonymeter.get(key):
            return (
                f"- the {side} rows were sampled to {anonymeter[key]} so both real sides are "
                "attacked at one size"
            )
    return "- both real sides had the same size"


def _attack_table(anonymeter: Mapping[str, Any]) -> List[str]:
    """One row per attack: its risk, its success rates and its verbatim copy's risk."""
    lines = [
        "| attack | criterion | risk | 95% interval | main | baseline | control | beats baseline "
        "| verbatim copy |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    criteria = anonymeter.get("criteria") or {}
    for name, outcome in (anonymeter.get("attacks") or {}).items():
        criterion = criteria.get(name.split(".", 1)[0], "")
        if "risk" not in outcome:
            lines.append(f"| {name} | {criterion} | error | {outcome.get('error')} | | | | | |")
            continue
        low, high = outcome["ci"]
        verbatim = outcome.get("verbatimCopy") or {}
        copied = (
            f"error: {verbatim['error']}" if "error" in verbatim else _num(verbatim.get("risk"))
        )
        lines.append(
            f"| {name} | {criterion} | {_num(outcome['risk'])} | {_num(low)} to {_num(high)} | "
            f"{_rate_text(outcome.get('attack'))} | {_rate_text(outcome.get('baseline'))} | "
            f"{_rate_text(outcome.get('control'))} | {outcome.get('beatsBaseline')} | {copied} |"
        )
    return lines


def _privacy_headline_lines(anonymeter: Mapping[str, Any]) -> List[str]:
    """Each criterion's headline risk, and how many of its attacks could not run."""
    lines = [
        "",
        "Headline per criterion: the highest risk among its attacks that beat their baseline "
        "(or the highest of all when none did); the interval is that attack's own, not adjusted "
        "for choosing the highest.",
        "",
    ]
    criteria = anonymeter.get("criteria") or {}
    for criterion, entry in sorted((anonymeter.get("headline") or {}).items()):
        name = criteria.get(criterion, criterion)
        failed = f"{entry.get('failed')} of {entry.get('of')} attacks could not run"
        if entry.get("risk") is None:
            lines.append(f"- {name}: no risk measured; {failed}")
            continue
        low, high = entry["ci"]
        lines.append(
            f"- {name}: risk {_num(entry['risk'])} ({_num(low)} to {_num(high)}) from "
            f"{entry['attack']}"
            + ("" if entry["beatsBaseline"] else ", which did not beat its baseline")
            + f"; {failed}"
        )
    return lines


def _null_line(null: Optional[Mapping[str, Any]]) -> str:
    """The overfitting metric between two disjoint samples of built-from people."""
    if not null or "score" not in null:
        return f"- null split: not measured ({(null or {}).get('skipped', 'not run')})"
    return (
        "- null split, the same metric between two disjoint samples of built-from people: "
        f"{_num(null['score'])} ({null.get('synthetic_data_percentages')}). Nobody in either "
        "sample was held out, so the score above says something about memorisation only "
        "where it falls below this one"
    )


def _distance_lines(step: Mapping[str, Any]) -> List[str]:
    """SDMetrics' distance metrics on the same person views, when they ran."""
    lines: List[str] = []
    if step.get("distanceError"):
        lines.append(f"- SDMetrics distance metrics failed: {step['distanceError']}")
    distance = step.get("distance")
    if distance:
        overfitting, baseline = distance.get("overfitting") or {}, distance.get("baseline") or {}
        lines += [
            "",
            f"- SDMetrics DCROverfittingProtection: {_num(overfitting.get('score'))} "
            f"({overfitting.get('synthetic_data_percentages')}, training sampled to "
            f"{distance.get('overfittingTrainingRows')} rows)",
            _null_line(distance.get("overfittingNull")),
            f"- SDMetrics DCRBaselineProtection: {_num(baseline.get('score'))} "
            f"({baseline.get('median_DCR_to_real_data')})",
        ]
    return lines


def _sdmetrics_section(record: Mapping[str, Any]) -> List[str]:
    lines = ["## Judge 4: SDMetrics (private environment)", ""]
    sdm = record.get("steps", {}).get("sdmetrics")
    if not sdm:
        return lines + ["- not run (no --sdmetrics-python)"]
    lines.append(f"- SDMetrics {sdm.get('sdmetrics')}, sampled: {sdm.get('sampled')}")
    for name in ("quality", "diagnostic"):
        part = sdm.get(name) or {}
        lines.append(
            f"- {name}: score {_num(part.get('score'))}; {_scores(part.get('properties') or {})}"
        )
        for table, scores in sorted((part.get("tables") or {}).items()):
            lines.append(f"  - {table}: {_scores(scores)}")
    return lines


def _authoring_section(record: Mapping[str, Any]) -> List[str]:
    lines = ["## How each column was declared", ""]
    authoring = record.get("steps", {}).get("generate", {}).get("authoring") or {}
    for table, entry in authoring.items():
        counts: Dict[str, int] = {}
        for note in entry["columns"]:
            counts[note["source"]] = counts.get(note["source"], 0) + 1
        lines.append(f"- {table}: rows {entry['rows']} ({entry['rowsSource']}); columns {counts}")
    return lines


def _publish_section(record: Mapping[str, Any]) -> List[str]:
    published = record.get("published") or record.get("steps", {}).get("publish", {})
    tables = published.get("tables") or {}
    mismatched = {
        table: entry["typeDifferences"]
        for table, entry in tables.items()
        if not entry.get("typesMatch")
    }
    sweep = record.get("sweep")
    return [
        "## Publishing and sweep",
        "",
        f"- Glue type differences between the copies: {mismatched or 'none'}",
        f"- sweep: {json.dumps(sweep, sort_keys=True) if sweep else 'not run'}",
    ]
