# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Write a FLUX Simulation from a FLUID contract and a profile, and nothing else.

This runs on the generator side, inside the sandbox, with only the contract and
the profile document beside it. It reads no record: every number it writes is a
published metric of the profile or a stated assumption, and the column report it
returns says which, per column.

The profile ``fluid profile`` writes today holds column metrics (count,
nullCount, distinctCount, sum, min, max and quartiles) after its output checks.
What this makes of them:

* ``rows`` is the table's published ``rowCount``.
* ``nullFraction`` is ``nullCount / rowCount`` when both are published.
* A number or date column becomes a histogram over its published order
  statistics: the edges are the distinct published values among min, the
  quartiles and max, and each bin carries the share of rows between its edges.
  A withheld min or max clips that tail onto the nearest published value.
  One published value makes a one-level column.
* A string column whose contract type is ``enum(...)`` draws its declared
  levels (``support: declared``) with equal weights: the profile publishes no
  level shares.
* Any other string column draws opaque levels, ``<column>-<n>``, as many as
  its published distinctCount (capped), with equal weights.
* A one-column integer primary key never has a published order statistic (each
  value is one row): it takes the widest published range of the foreign keys
  that reference it, or is assumed dense, ``1..rowCount``, when none does.
* Anything else the profile withheld is an assumption, named in the report.

Run inside the sandbox as a single file (``python -I bundle.py ...``), so it
imports the standard library, PyYAML, flux-spec's digest and flux-stats' enum
reader only.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml
from flux_stats.fidelity import declared_enum_values

#: The extension namespace FLUX reads its emit blocks and profile pin from.
NAMESPACE = "com.agentics.flux/1"
#: The column FLUX generates from each contract ``check`` that the owner writes as a
#: difference form (EXPR-SPEC-2, sections 1 and 6), by the invariant's id. The owner's
#: rules are ``flux_e2e.rules``; this file runs alone, so it names the one it needs.
GENERATES = {"date_of_key": "d_date"}
#: The FLUX-side rules (EXPR-SPEC-2, section 1) FLUX generates, by table: the columns of the
#: calendar that need a date part, date arithmetic or a concatenation, which FLUID SQL/1
#: leaves out. Each is written under its emit's ``rules``, when the expose has the columns
#: it names and the contract's rules do not already decide its column.
FLUX_RULES = (
    (
        "date_dim",
        {
            "id": "day_of_month",
            "language": "flux-expr/3",
            "derive": "d_dom",
            "expr": "EXTRACT(DAY FROM d_date)",
        },
        ("d_date",),
    ),
    (
        "date_dim",
        {
            "id": "same_day_last_year",
            "language": "flux-expr/3",
            "derive": "d_same_day_ly",
            "expr": "d_date_sk - DAYS_BETWEEN(ADD_YEARS(d_date, -1), d_date)",
        },
        ("d_date", "d_date_sk"),
    ),
    (
        "date_dim",
        {
            "id": "quarter_name",
            "language": "flux-expr/3",
            "derive": "d_quarter_name",
            "expr": "d_year || 'Q' || d_qoy",
        },
        ("d_year", "d_qoy"),
    ),
)
#: How many opaque levels a string column may draw, at most.
OPAQUE_LEVEL_CAP = 1000
#: The quantile levels ``fluid profile`` publishes, by their decimal spelling.
QUARTILES = ("0.25", "0.5", "0.75")
_EPOCH = date(1970, 1, 1)


@dataclass
class ColumnNote:
    """How one column was declared: from the profile, or by assumption."""

    column: str
    source: str  # "profile" | "contract" | "assumed"
    how: str


@dataclass
class EmitPlan:
    expose_id: str
    rows: int
    rows_source: str
    columns: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    notes: List[ColumnNote] = field(default_factory=list)


def _published(metric: Optional[Mapping[str, Any]]) -> bool:
    return bool(metric) and metric.get("status") == "F"


def _metric_value(metrics: Mapping[str, Any], name: str) -> Optional[Any]:
    metric = metrics.get(name)
    return metric.get("value") if _published(metric) else None


def _order_statistics(metrics: Mapping[str, Any]) -> List[Tuple[Fraction, Decimal]]:
    """The published (level, value) points among min, the quartiles and max."""
    points: List[Tuple[Fraction, Decimal]] = []
    low = _metric_value(metrics, "min")
    if low is not None:
        points.append((Fraction(0), Decimal(str(low))))
    quantiles = metrics.get("quantiles")
    if _published(quantiles):
        for level, value in sorted(
            quantiles.get("values", {}).items(), key=lambda item: Fraction(item[0])
        ):
            points.append((Fraction(level), Decimal(str(value))))
    high = _metric_value(metrics, "max")
    if high is not None:
        points.append((Fraction(1), Decimal(str(high))))
    return points


def histogram(
    points: Sequence[Tuple[Fraction, Decimal]],
) -> Optional[Tuple[List[Decimal], List[Fraction]]]:
    """Edges and bin weights from published order statistics, or None.

    The edges are the distinct values, in order. A bin's weight is the share of
    rows between its two edges; the mass at or below the first edge and above
    the last one (a withheld min or max, or a point mass on an edge) is folded
    into the first and last bins, so the weights sum to one.
    """
    if not points:
        return None
    # The highest level at which each distinct value is the order statistic.
    reach: Dict[Decimal, Fraction] = {}
    for level, value in points:
        reach[value] = max(level, reach.get(value, level))
    edges = sorted(reach)
    if len(edges) < 2:
        return None
    weights: List[Fraction] = []
    for low, high in pairwise(edges):
        weights.append(reach[high] - reach[low])
    weights[0] += reach[edges[0]]
    weights[-1] += 1 - reach[edges[-1]]
    return edges, weights


def _decimal_text(value: Fraction) -> str:
    """An exact decimal spelling of a share whose denominator divides 10^k."""
    text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _date_text(milliseconds: Decimal) -> str:
    # Whole days since the epoch: the generator reads a DATE bound as YYYY-MM-DD or
    # integer days, and the fidelity check reads a bound as a decimal or an RFC 3339
    # instant, so an integer is the one spelling both accept.
    return int(milliseconds) // 86_400_000  # type: ignore[return-value]


def _timestamp_text(milliseconds: Decimal) -> str:
    moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(milliseconds))
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _base_type(declared: str) -> str:
    return declared.split("(", 1)[0].strip().lower()


def _enum_levels(declared: str) -> List[str]:
    # The engine's own reader: a quoted level may hold a comma, a parenthesis or
    # a doubled quote, so the list is not split at every comma.
    return list(declared_enum_values(declared))


def _primary_columns(expose: Mapping[str, Any]) -> List[str]:
    semantics = expose.get("semantics") or {}
    return [
        entity.get("expr") or entity["name"]
        for entity in semantics.get("entities", [])
        if entity.get("type") == "primary"
    ]


def plan_emit(
    expose: Mapping[str, Any],
    table: Mapping[str, Any],
    referencing: Optional[Mapping[str, Sequence[Decimal]]] = None,
) -> EmitPlan:
    """The emit block for one expose, from its profile table."""
    expose_id = expose["exposeId"]
    row_count = table.get("rowCount")
    if isinstance(row_count, int) and row_count > 0:
        plan = EmitPlan(expose_id, row_count, "profile rowCount")
    else:
        plan = EmitPlan(expose_id, 10, "assumed: rowCount withheld, 10 rows")
    primary = _primary_columns(expose)
    profiled = table.get("columns", {})
    # A column a difference-form rule generates is solved from its key on every row, and
    # a declared draw would only give the engine a range to refuse a solved date with (a
    # placeholder day, when the profile withholds the column's order statistics). It
    # declares nothing, so a row the rule cannot solve takes the time-bounds default.
    solved = {setting["generates"]: rule for rule, setting in invariant_settings(expose).items()}
    for column in expose["contract"]["schema"]:
        name, declared = column["name"], str(column["type"])
        entry: Dict[str, Any] = {}
        if name in solved:
            plan.columns[name] = entry
            plan.notes.append(
                ColumnNote(name, "contract", f"generated by rule {solved[name]}: declares no draw")
            )
            continue
        metrics = (profiled.get(name) or {}).get("metrics", {})
        _null_fraction(metrics, plan.rows if isinstance(row_count, int) else None, entry)
        base = _base_type(declared)
        if base in ("string", "varchar", "char", "text", "enum"):
            note = _string_column(name, declared, metrics, entry)
        else:
            note = _ordered_column(
                name,
                declared,
                metrics,
                entry,
                is_key=[name] == primary,
                rows=plan.rows,
                referenced=(referencing or {}).get(name, ()),
            )
        plan.columns[name] = entry
        plan.notes.append(note)
    return plan


def _string_column(
    name: str, declared: str, metrics: Mapping[str, Any], entry: Dict[str, Any]
) -> ColumnNote:
    levels = _enum_levels(declared)
    if levels:
        entry["support"] = "declared"
        entry["maxCardinality"] = len(levels)
        return ColumnNote(name, "contract", f"{len(levels)} declared levels, equal weights")
    distinct = _metric_value(metrics, "distinctCount")
    if isinstance(distinct, int) and distinct > 0:
        drawn = min(distinct, OPAQUE_LEVEL_CAP)
        capped = " (capped)" if drawn < distinct else ""
        source, how = "profile", f"{drawn} opaque levels from distinctCount {distinct}{capped}"
    else:
        drawn = 1
        source, how = "assumed", "distinctCount withheld: one opaque level"
    width = len(str(drawn))
    values = [f"{name}-{index:0{width}d}" for index in range(1, drawn + 1)]
    entry["support"] = {"values": values}
    entry["maxCardinality"] = drawn
    return ColumnNote(name, source, how)


def _null_fraction(metrics: Mapping[str, Any], rows: Optional[int], entry: Dict[str, Any]) -> None:
    """``nullCount`` over the column's rows: ``count + nullCount``, else ``rowCount``."""
    count, nulls = _metric_value(metrics, "count"), _metric_value(metrics, "nullCount")
    if not isinstance(nulls, int):
        return
    total = count + nulls if isinstance(count, int) else rows
    if not total:
        return
    share = min(Fraction(nulls, total), Fraction(1))
    if share:
        # Rounded counts give shares like 5/7; FLUX reads exact decimals.
        entry["nullFraction"] = _decimal_text(Fraction(round(share * 10**6), 10**6))


def _scale(declared: str) -> int:
    """The decimal places a value of this type carries: DECIMAL(p,s) gives s, an integer 0."""
    if "(" in declared and "," in declared:
        return int(declared[declared.rindex(",") + 1 : declared.rindex(")")].strip())
    return 0


def _published_mean(metrics: Mapping[str, Any]) -> Optional[Decimal]:
    total, count = _metric_value(metrics, "sum"), _metric_value(metrics, "count")
    if total is None or not isinstance(count, int) or count <= 0:
        return None
    return Decimal(str(total)) / count


def _ordered_column(
    name: str,
    declared: str,
    metrics: Mapping[str, Any],
    entry: Dict[str, Any],
    *,
    is_key: bool,
    rows: int,
    referenced: Sequence[Decimal] = (),
) -> ColumnNote:
    """A number, date or timestamp column, from the best of what the profile published."""
    base = _base_type(declared)
    temporal = base == "date" or base.startswith("timestamp") or base == "datetime"
    integer = base in ("bigint", "int", "integer", "smallint", "tinyint", "long")

    def spell(value: Decimal) -> Any:
        if base == "date":
            return _date_text(value)
        if temporal:
            return _timestamp_text(value)
        return format(value, "f")

    points = _order_statistics(metrics)
    shaped = histogram(points)
    if shaped is not None:
        edges, weights = shaped
        entry["range"] = {"min": spell(edges[0]), "max": spell(edges[-1])}
        entry["bins"] = [spell(edge) for edge in edges]
        entry["binWeights"] = [_decimal_text(weight) for weight in weights]
        clipped = [] if any(level == 0 for level, _ in points) else ["min"]
        clipped += [] if any(level == 1 for level, _ in points) else ["max"]
        how = f"histogram over {len(edges)} published order statistics"
        if clipped:
            how += f", {' and '.join(clipped)} withheld: tail clipped"
        return ColumnNote(name, "profile", how)
    if is_key and integer:
        return _key_range(name, entry, rows, referenced)
    if points and temporal:
        # A date or timestamp has no categorical family: one value is a one-day range.
        day = Decimal(86_400_000)
        entry["range"] = {"min": spell(points[0][1]), "max": spell(points[0][1] + day)}
        return ColumnNote(name, "profile", "one published value: a one-day range")
    mean = None if temporal else _published_mean(metrics)
    if mean is not None:
        return _around_mean(
            name,
            entry,
            mean,
            points,
            scale=_scale(declared),
            integer=integer,
            distinct=_metric_value(metrics, "distinctCount"),
        )
    if points:
        entry["levels"] = {spell(points[0][1]): "1"}
        return ColumnNote(name, "profile", "one published value: a single level")
    if temporal:
        entry["range"] = {"min": spell(Decimal(0)), "max": spell(Decimal(86_400_000))}
        return ColumnNote(name, "assumed", "no published order statistic: a placeholder day")
    entry["levels"] = {"0": "1"}
    return ColumnNote(name, "assumed", "no published order statistic: a single placeholder level")


def _key_range(
    name: str, entry: Dict[str, Any], rows: int, referenced: Sequence[Decimal]
) -> ColumnNote:
    """A unique integer key, which never has a published order statistic (each value is one row).

    It starts where the foreign keys referencing it start and is at least rowCount
    values wide, so it can hold a value per row; with no referencing key it is
    assumed dense from 1.
    """
    if referenced:
        low, high = min(referenced), max(referenced)
        high = max(high, low + rows - 1)
        entry["range"] = {"min": format(low, "f"), "max": format(high, "f")}
        return ColumnNote(
            name,
            "assumed",
            "key range: from the foreign keys referencing it, at least rowCount wide",
        )
    entry["range"] = {"min": "1", "max": str(rows)}
    return ColumnNote(name, "assumed", "key with no published order statistic: dense 1..rowCount")


def _around_mean(
    name: str,
    entry: Dict[str, Any],
    mean: Decimal,
    points: Sequence[Tuple[Fraction, Decimal]],
    *,
    scale: int,
    integer: bool,
    distinct: Optional[int],
) -> ColumnNote:
    """Too few order statistics for a histogram, but ``sum / count`` is published.

    A published min (or max) and the mean give a uniform range with that end and
    that mean. With neither, a uniform range centred on the mean holds as many
    values as the published distinctCount; with no distinct count either, every
    row takes the mean. Each keeps a query's totals and averages; the spread is an
    assumption.
    """
    step = Decimal(1) if integer else Decimal(1).scaleb(-scale)
    level = {point: value for point, value in points}
    if Fraction(0) in level and level[Fraction(0)] < mean:
        low, high = level[Fraction(0)], 2 * mean - level[Fraction(0)]
        how = "uniform range with the published min and mean (sum / count)"
    elif Fraction(1) in level and level[Fraction(1)] > mean:
        low, high = 2 * mean - level[Fraction(1)], level[Fraction(1)]
        how = "uniform range with the published max and mean (sum / count)"
    elif isinstance(distinct, int) and distinct > 1:
        half = step * (distinct - 1) / 2
        low, high = mean - half, mean + half
        how = "uniform range around the published mean, as many values as distinctCount"
    else:
        low = high = mean
        how = "uniform range around the published mean"
    low = low.quantize(step, rounding="ROUND_FLOOR")
    high = high.quantize(step, rounding="ROUND_CEILING")
    if high > low:
        entry["range"] = {"min": format(low, "f"), "max": format(high, "f")}
        return ColumnNote(name, "assumed", how)
    entry["levels"] = {format(mean.quantize(step, rounding="ROUND_HALF_EVEN"), "f"): "1"}
    return ColumnNote(name, "assumed", "the published mean (sum / count) on every row")


def _declared_span(entry: Mapping[str, Any]) -> List[Decimal]:
    """The numbers a column's emit entry is declared over: its range's ends, or its levels."""
    span = []
    for value in [*(entry.get("range") or {}).values(), *(entry.get("levels") or {})]:
        try:
            span.append(Decimal(str(value)))
        except InvalidOperation:
            continue  # a date or timestamp spelled as text names no integer key
    return span


def referencing_ranges(
    contract: Mapping[str, Any], tables: Mapping[str, Any]
) -> Dict[str, Dict[str, List[Decimal]]]:
    """Per target table and key column, the values of the foreign keys referencing it.

    Only one-column relationships count. Each referencing column gives the values its
    own emit entry is declared over, by ``plan_emit``'s rules, so every key it draws
    lies inside the range the parent's key is given. A column with nothing published
    is a placeholder and gives nothing: it says nothing about where the keys lie, and
    a placeholder far from the other keys would stretch the range they share. A child
    plan here has no references of its own; that would matter only for a one-column
    primary key that is also a foreign key, and no TPC-DS table here has one.
    """
    out: Dict[str, Dict[str, List[Decimal]]] = {}
    plans: Dict[str, EmitPlan] = {}
    for expose in contract["exposes"]:
        for relationship in (expose.get("semantics") or {}).get("relationships", []):
            if len(relationship["fromColumns"]) != 1:
                continue
            child = expose["exposeId"]
            columns = tables.get(child, {}).get("columns", {})
            metrics = (columns.get(relationship["fromColumns"][0]) or {}).get("metrics", {})
            if not _order_statistics(metrics) and _published_mean(metrics) is None:
                continue  # nothing published: a placeholder
            if child not in plans:
                plans[child] = plan_emit(expose, tables.get(child, {}))
            entry = plans[child].columns.get(relationship["fromColumns"][0], {})
            target = out.setdefault(relationship["to"], {})
            target.setdefault(relationship["toColumns"][0], []).extend(_declared_span(entry))
    return out


def _property_for(plan: EmitPlan) -> Dict[str, Any]:
    """FLUX needs one fidelity property per block: the first ranged column's mean.

    The profile ``fluid profile`` writes holds no statistic a property can be
    scored against, so verify reports the property as refused (FLX-5037). It is
    declared because a fidelity block needs one.
    """
    for name, entry in plan.columns.items():
        if "range" in entry and "T" not in str(entry["range"]["min"]):
            low, high = Decimal(str(entry["range"]["min"])), Decimal(str(entry["range"]["max"]))
            return {
                "property": "mean",
                "column": name,
                "tolerance": {"type": "absolute", "value": format((high - low) / 10, "f")},
            }
    for name, entry in plan.columns.items():
        if "support" in entry:
            return {"property": "support", "column": name, "tolerance": {"type": "count", "max": 0}}
    raise ValueError(f"{plan.expose_id}: no column can carry a fidelity property")


def invariant_settings(expose: Mapping[str, Any]) -> Dict[str, Dict[str, str]]:
    """The FLUX settings of the expose's rules: the column each difference form generates.

    Only an invariant the contract carries gets one, and only when the expose has the
    column, so a rule the owner left out leaves its column to its own statistics.
    """
    block = expose.get("contract") or {}
    columns = {column.get("name") for column in block.get("schema") or ()}
    declared = {
        entry.get("id") for entry in block.get("invariants") or () if isinstance(entry, dict)
    }
    return {
        rule: {"generates": column}
        for rule, column in GENERATES.items()
        if rule in declared and column in columns
    }


def flux_side_rules(expose: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The FLUX-side rules of the expose, as its emit declares them under ``rules``.

    A rule is written only when the expose has its column and the columns its text reads, and
    when no contract rule decides that column (a ``derive`` of it, or a difference form that
    generates it), so a column the owner left to its own statistics keeps them.
    """
    block = expose.get("contract") or {}
    columns = {column.get("name") for column in block.get("schema") or ()}
    decided = {
        entry.get("derive")
        for entry in block.get("invariants") or ()
        if isinstance(entry, dict) and entry.get("derive")
    } | {setting["generates"] for setting in invariant_settings(expose).values()}
    return [
        dict(rule)
        for table, rule, reads in FLUX_RULES
        if table == expose.get("exposeId")
        and {rule["derive"], *reads} <= columns
        and rule["derive"] not in decided
    ]


def simulation(
    contract: Mapping[str, Any],
    contract_digest: str,
    profile_digest: str,
    plans: Sequence[EmitPlan],
    *,
    seed: int,
    starter: Mapping[str, Any],
) -> Dict[str, Any]:
    """The Simulation document: the starter's world, and one generator emit per plan."""
    exposes = {expose["exposeId"]: expose for expose in contract.get("exposes", ())}
    emits: Dict[str, Any] = {}
    for plan in plans:
        emits[plan.expose_id] = {
            "rows": plan.rows,
            "fidelity": {
                "encoding": {"fractionalBits": 16, "rounding": "floor"},
                "records": {"min": 1, "max": 1_000_000_000},
                "columns": plan.columns,
                "properties": [_property_for(plan)],
                "budget": {"maxChecksPerState": 20, "maxStateAge": "P30D"},
                "failureAction": "withholdBundle",
            },
        }
        settings = invariant_settings(exposes.get(plan.expose_id, {}))
        if settings:
            emits[plan.expose_id]["invariants"] = settings
        flux_rules = flux_side_rules(exposes.get(plan.expose_id, {}))
        if flux_rules:
            emits[plan.expose_id]["rules"] = flux_rules
    spec = dict(starter["spec"])
    spec["seed"] = seed
    spec["emits"] = [
        {
            "productRef": contract["id"],
            "exposeId": plan.expose_id,
            "fluidVersion": contract["fluidVersion"],
            "provenance": {"contractDigest": contract_digest},
        }
        for plan in plans
    ]
    return {
        "fluxVersion": starter["fluxVersion"],
        "kind": "Simulation",
        "id": "sim-tpcds-copy",
        "name": "Synthetic copy of a TPC-DS-shaped source",
        "metadata": {"owner": {"team": "flux-e2e"}},
        "extensions": {NAMESPACE: {"profile": {"digest": profile_digest}, "emits": emits}},
        "spec": spec,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument(
        "--bundle", required=True, type=Path, help="a `fluxsim init` starter to fill"
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--notes", required=True, type=Path, help="where the column report goes")
    args = parser.parse_args(argv)

    import rfc8785
    from flux_spec.validate import canonical_digest

    contract = yaml.safe_load(args.contract.read_text(encoding="utf-8"))
    profile_document = json.loads(args.profile.read_text(encoding="utf-8"))
    profile_digest = (
        "sha256:" + __import__("hashlib").sha256(rfc8785.dumps(profile_document)).hexdigest()
    )
    tables = profile_document["tables"]
    referencing = referencing_ranges(contract, tables)
    plans = [
        plan_emit(expose, tables[expose["exposeId"]], referencing.get(expose["exposeId"]))
        for expose in contract["exposes"]
    ]

    starter_path = args.bundle / "simulation.flux.yml"
    starter = yaml.safe_load(starter_path.read_text(encoding="utf-8"))
    for stale in args.bundle.glob("*.fluid.y*ml"):
        stale.unlink()
    (args.bundle / args.contract.name).write_text(
        args.contract.read_text(encoding="utf-8"), encoding="utf-8"
    )
    document = simulation(
        contract,
        "sha256:" + canonical_digest(contract),
        profile_digest,
        plans,
        seed=args.seed,
        starter=starter,
    )
    starter_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    report = {
        plan.expose_id: {
            "rows": plan.rows,
            "rowsSource": plan.rows_source,
            "columns": [note.__dict__ for note in plan.notes],
        }
        for plan in plans
    }
    args.notes.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
