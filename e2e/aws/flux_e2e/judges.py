# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The judges that read both copies: consumer queries, key integrity, FLUX's own verify.

Every number here is a rate or a share, so the two copies are compared on their
shape and never on their absolute values:

* **Queries.** The same SQL runs in Athena on the source database and on the
  synthetic one. A result's *measure* is its last numeric column and its *key* is
  every other column. ``aggregateError`` is the relative error of the measure's
  total (of every numeric column, for a one-row result). ``shareDistance`` is the
  total variation distance between the two sides' shares of the measure by key,
  and ``keyOverlap`` the Jaccard overlap of their keys. The comparison follows
  TabQueryBench's query-workload idea (ratios of a synthetic answer to the real
  one), carried over to several tables.
* **Keys.** Primary-key uniqueness and the orphan rate of every foreign key, on
  both copies: the source side is the control.
* **FLUX verify.** The scorecard and the shape flags ``fluxsim verify --json``
  gives against the pinned profile, read as FLUX wrote them.
"""

from __future__ import annotations

import datetime
import math
import numbers
import re
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_e2e import tpcds
from flux_e2e.aws import QueryResult

QUERY_DIR = Path(__file__).resolve().parent.parent / "queries" / "athena-tpcds"
_SUFFIX = "[ TABLE_SUFFIX ]"


# ---- queries ----------------------------------------------------------------------

KEY, MEASURE = "key", "measure"


def load_queries(tables: Sequence[str]) -> List[Tuple[str, str]]:
    """The vendored queries that read only *tables*, as (name, sql), in query order."""
    import sqlglot
    from sqlglot import expressions

    scope = set(tables)
    chosen = []
    for path in sorted(QUERY_DIR.glob("q*.sql"), key=_query_order):
        sql = path.read_text(encoding="utf-8").replace(_SUFFIX, "")
        tree = sqlglot.parse_one(sql, read="trino")
        ctes = {cte.alias_or_name for cte in tree.find_all(expressions.CTE)}
        reads = {table.name for table in tree.find_all(expressions.Table)} - ctes
        if reads <= scope:
            chosen.append((path.stem, sql))
    return chosen


def _query_order(path: Path) -> Tuple[int, str]:
    match = re.match(r"q(\d+)(.*)", path.stem)
    return (int(match.group(1)), match.group(2)) if match else (10**6, path.stem)


def query_roles(sql: str) -> Optional[List[Optional[str]]]:
    """Each output column's role, read from the query's outermost SELECT.

    An aggregate (or a window over one) is a measure; a ranking window or a column
    a GROUP BY names is a key; a plain column of a subquery or CTE takes the role it
    has there, and an expression takes the role of the columns it reads. Anything
    else is unknown (None) and is decided by its values and name. A ``SELECT *``
    over a real table gives no roles at all.
    """
    import sqlglot
    from sqlglot import expressions

    tree = sqlglot.parse_one(sql, read="trino")
    ctes = {cte.alias_or_name: cte.this for cte in tree.find_all(expressions.CTE)}
    roles = _select_roles(tree, ctes, depth=0)
    return None if roles is None else [role for _, role in roles]


_RANKING = ("Rank", "DenseRank", "RowNumber", "PercentRank", "Ntile", "CumeDist")

Roles = List[Tuple[str, Optional[str]]]


def _select_roles(node: Any, ctes: Mapping[str, Any], *, depth: int) -> Optional[Roles]:
    from sqlglot import expressions

    while isinstance(node, (expressions.Union, expressions.Except, expressions.Intersect)):
        node = node.left
    if not isinstance(node, expressions.Select) or depth > 8:
        return None
    sources: Dict[str, Optional[Roles]] = {}
    order: List[Optional[Roles]] = []
    head = node.args.get("from_") or node.args.get("from")
    for source in ([head.this] if head else []) + [
        join.this for join in node.args.get("joins") or []
    ]:
        inner: Optional[Roles] = None
        if isinstance(source, expressions.Subquery):
            inner = _select_roles(source.this, ctes, depth=depth + 1)
        elif isinstance(source, expressions.Table) and source.name in ctes:
            inner = _select_roles(ctes[source.name], ctes, depth=depth + 1)
        sources[source.alias_or_name] = inner
        order.append(inner)
    group = node.args.get("group")
    grouped = set()
    if group is not None:
        grouped = {column.name for column in group.find_all(expressions.Column)}
        grouped |= {part.sql() for part in group.expressions}
    out: Roles = []
    for projection in node.expressions:
        if isinstance(projection, expressions.Star):
            if not order or any(inner is None for inner in order):
                return None
            for inner in order:
                out.extend(inner or [])
            continue
        out.append((projection.alias_or_name, _role(projection.unalias(), sources, grouped)))
    return out


def _role(expression: Any, sources: Mapping[str, Optional[Roles]], grouped: set) -> Optional[str]:
    from sqlglot import expressions

    if isinstance(expression, expressions.Window) and type(expression.this).__name__ in _RANKING:
        return KEY
    if expression.find(expressions.AggFunc) is not None:
        return MEASURE
    if expression.sql() in grouped:
        return KEY
    if isinstance(expression, expressions.Column):
        if expression.name in grouped:
            return KEY
        return _lookup(sources, expression)
    found = [_lookup(sources, column) for column in expression.find_all(expressions.Column)]
    if MEASURE in found:
        return MEASURE
    if found and all(role == KEY for role in found):
        return KEY
    return None


def _lookup(sources: Mapping[str, Optional[Roles]], column: Any) -> Optional[str]:
    """The role a column has in the subquery or CTE it comes from, if any."""
    candidates = [sources.get(column.table)] if column.table else list(sources.values())
    for inner in candidates:
        for name, role in inner or []:
            if name == column.name:
                return role
    return None


#: Athena errors that say the run, not the synthetic data, failed.
_HARNESS_ERROR = re.compile(
    r"Throttl|Rate exceeded|TooManyRequests|BytesScannedCutoff|bytes scanned|AccessDenied|"
    r"TABLE_NOT_FOUND|SCHEMA_NOT_FOUND|EntityNotFound|InternalError|ServiceUnavailable",
    re.IGNORECASE,
)

#: An unresolved integer column with one of these names is an identifier or a calendar
#: part, so a key rather than a quantity.
_KEY_NAME = re.compile(r"(_sk|_id|_number)$|(^|_)(year|moy|qoy|dom|dow|hour|minute)$")


def _number(value: Any) -> Optional[float]:
    """A finite number as a float, or None. Athena's integers arrive as numpy scalars."""
    if value is None or isinstance(value, bool) or not isinstance(value, (numbers.Real, Decimal)):
        return None
    if type(value).__name__ == "bool_":
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _numeric_columns(result: QueryResult) -> List[int]:
    columns = []
    for index in range(len(result.columns)):
        values = [row[index] for row in result.rows if row[index] is not None]
        if values and all(_number(value) is not None for value in values):
            columns.append(index)
    return columns


def _key(value: Any) -> str:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    number = _number(value)
    if number is not None:
        return repr(round(number, 6))
    return str(value)


def relative_error(source: float, synthetic: float) -> float:
    """``|synthetic - source| / |source|``; a non-zero answer to a zero one scores 1."""
    if source == 0:
        return 0.0 if synthetic == 0 else 1.0
    return abs(synthetic - source) / abs(source)


def _roles(
    source: QueryResult, synthetic: QueryResult, declared: Optional[Sequence[Optional[str]]]
) -> Tuple[List[int], List[int]]:
    """Key and measure column indexes: the declared role, else numbers are measures."""
    width = len(source.columns)

    def empty(side: QueryResult, index: int) -> bool:
        return all(row[index] is None for row in side.rows)

    # A column numeric on one side and NULL on every row of the other (an aggregate
    # over no matching rows) is still a number, and NULL counts as zero.
    left, right = set(_numeric_columns(source)), set(_numeric_columns(synthetic))
    numeric = {
        index
        for index in range(width)
        if (index in left or empty(source, index))
        and (index in right or empty(synthetic, index))
        and (index in left or index in right)
    }
    roles = list(declared) if declared and len(declared) == width else [None] * width
    keys, measures = [], []
    for index, role in enumerate(roles):
        if role == MEASURE and index in numeric:
            measures.append(index)
        elif role == KEY or index not in numeric:
            keys.append(index)
        elif _KEY_NAME.search(source.columns[index].lower()):
            keys.append(index)
        else:
            measures.append(index)
    return keys, measures


def compare_query(
    source: QueryResult,
    synthetic: QueryResult,
    roles: Optional[Sequence[Optional[str]]] = None,
) -> Dict[str, Any]:
    """One query's comparison, as rates and shares.

    ``aggregateError`` is the median, over the measures, of the relative error of
    each measure's total over the answer's rows. ``shareDistance`` is the median,
    over the measures, of the total variation distance between the two sides'
    shares of the measure by key; ``keyOverlap`` is the Jaccard overlap of the
    keys. When no key is shared the shares cannot be compared value by value, and
    the query is marked ``keysDisjoint``.
    """
    out: Dict[str, Any] = {
        "rowsSource": len(source.rows),
        "rowsSynthetic": len(synthetic.rows),
        "bytesScanned": source.bytes_scanned + synthetic.bytes_scanned,
    }
    if source.error:
        out.update(status="error source", error=source.error)
        return out
    if synthetic.error and _HARNESS_ERROR.search(synthetic.error):
        # Throttling, the scan cutoff, permissions or a missing table: not the data's doing.
        out.update(status="error harness", error=synthetic.error)
        return out
    if synthetic.error:
        # The synthetic data made a consumer query fail: a measurement, not a harness fault.
        out.update(status="error synthetic", error=synthetic.error)
        return out
    if not source.rows:
        out["status"] = "empty source"
        return out
    if not synthetic.rows:
        out.update(status="empty synthetic", aggregateError=1.0, shareDistance=1.0, keyOverlap=0.0)
        return out
    out["status"] = "compared"
    keys, measures = _roles(source, synthetic, roles)
    out["keys"] = [source.columns[index] for index in keys]
    out["measures"] = [source.columns[index] for index in measures]
    if measures:
        errors = [
            relative_error(
                *(sum(_number(row[m]) or 0.0 for row in side.rows) for side in (source, synthetic))
            )
            for m in measures
        ]
        out["aggregateError"] = median(errors)
    if keys:
        out["keyOverlap"] = _overlap(source, synthetic, keys)
        out["keysDisjoint"] = out["keyOverlap"] == 0.0
        if measures:
            out["shareDistance"] = median(
                _share_distance(source, synthetic, keys, measure) for measure in measures
            )
    return out


def _shares(result: QueryResult, keys: Sequence[int], measure: int) -> Dict[Tuple[str, ...], float]:
    totals: Dict[Tuple[str, ...], float] = {}
    for row in result.rows:
        key = tuple(_key(row[index]) for index in keys)
        totals[key] = totals.get(key, 0.0) + abs(_number(row[measure]) or 0.0)
    whole = sum(totals.values())
    return {key: value / whole for key, value in totals.items()} if whole else {}


def _share_distance(
    source: QueryResult, synthetic: QueryResult, keys: Sequence[int], measure: int
) -> float:
    left, right = _shares(source, keys, measure), _shares(synthetic, keys, measure)
    if not left or not right:
        # A side with no mass has no distribution: equal when both are empty, else the worst.
        return 0.0 if not left and not right else 1.0
    return 0.5 * sum(
        abs(left.get(key, 0.0) - right.get(key, 0.0)) for key in set(left) | set(right)
    )


def _overlap(source: QueryResult, synthetic: QueryResult, keys: Sequence[int]) -> float:
    left = {tuple(_key(row[i]) for i in keys) for row in source.rows}
    right = {tuple(_key(row[i]) for i in keys) for row in synthetic.rows}
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def summarise_queries(per_query: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """Counts and medians over the queries; disjoint keys are counted, not averaged in."""
    entries = list(per_query.values())
    errors = [
        entry["aggregateError"] for entry in entries if entry.get("aggregateError") is not None
    ]
    shared = [
        entry["shareDistance"]
        for entry in entries
        if entry.get("shareDistance") is not None and not entry.get("keysDisjoint")
    ]
    statuses: Dict[str, int] = {}
    for entry in entries:
        statuses[entry["status"]] = statuses.get(entry["status"], 0) + 1
    return {
        "queries": len(entries),
        "statuses": statuses,
        "aggregateErrorMedian": median(errors) if errors else None,
        "aggregateWithin10Percent": sum(1 for error in errors if error <= 0.10),
        "aggregateCompared": len(errors),
        "shareDistanceMedian": median(shared) if shared else None,
        "shareCompared": len(shared),
        "keysDisjoint": sum(1 for entry in entries if entry.get("keysDisjoint")),
    }


def harness_failures(
    per_query: Mapping[str, Mapping[str, Any]], keys: Mapping[str, Any]
) -> List[str]:
    """What says the judge itself failed: a source query or any key check that errored."""
    failed = [
        name
        for name, entry in per_query.items()
        if entry["status"] in ("error source", "error harness")
    ]
    for side, found in keys.items():
        for kind in ("primaryKeys", "foreignKeys"):
            failed += [f"{side} {name}" for name, entry in found[kind].items() if "error" in entry]
    return failed


# ---- keys -------------------------------------------------------------------------


def key_sql(
    primary_keys: Mapping[str, Sequence[str]], tables: Sequence[str]
) -> List[Tuple[str, str]]:
    """(name, sql) for every primary key and foreign key among *tables*."""
    jobs: List[Tuple[str, str]] = []
    for table in tables:
        columns = list(primary_keys.get(table) or ())
        if not columns:
            continue
        listed = ", ".join(columns)
        present = " AND ".join(f"{column} IS NOT NULL" for column in columns)
        jobs.append(
            (
                f"pk:{table}",
                f"SELECT (SELECT count(*) FROM {table}) AS rows_total, "
                f"(SELECT count(*) FROM (SELECT DISTINCT {listed} FROM {table} WHERE {present})) AS keys_distinct, "
                f"(SELECT count(*) FROM {table} WHERE NOT ({present})) AS keys_null",
            )
        )
    for fk in tpcds.foreign_keys(tables):
        on = " AND ".join(
            f"c.{mine} = p.{theirs}"
            for mine, theirs in zip(fk.columns, fk.target_columns, strict=True)
        )
        present = " AND ".join(f"c.{column} IS NOT NULL" for column in fk.columns)
        jobs.append(
            (
                f"fk:{fk.name}",
                f"SELECT count(*) AS references_total, count_if(p.{fk.target_columns[0]} IS NULL) AS orphans "
                f"FROM {fk.table} c LEFT JOIN (SELECT DISTINCT {', '.join(fk.target_columns)} "
                f"FROM {fk.target}) p ON {on} WHERE {present}",
            )
        )
    return jobs


def read_keys(results: Mapping[str, QueryResult]) -> Dict[str, Any]:
    """Uniqueness and orphan rates from the key queries of one side."""
    out: Dict[str, Any] = {"primaryKeys": {}, "foreignKeys": {}}
    for name, result in results.items():
        if result.error or not result.rows:
            out["primaryKeys" if name.startswith("pk:") else "foreignKeys"][name[3:]] = {
                "error": result.error
            }
            continue
        row = dict(zip(result.columns, result.rows[0], strict=True))
        if name.startswith("pk:"):
            total, distinct = int(row["rows_total"]), int(row["keys_distinct"])
            out["primaryKeys"][name[3:]] = {
                "rows": total,
                "distinctKeys": distinct,
                "nullKeys": int(row["keys_null"]),
                "duplicateRate": (1 - distinct / total) if total else 0.0,
            }
        else:
            total, orphans = int(row["references_total"]), int(row["orphans"])
            out["foreignKeys"][name[3:]] = {
                "references": total,
                "orphans": orphans,
                "orphanRate": orphans / total if total else 0.0,
            }
    rates = [entry["orphanRate"] for entry in out["foreignKeys"].values() if "orphanRate" in entry]
    duplicates = [
        entry["duplicateRate"] for entry in out["primaryKeys"].values() if "duplicateRate" in entry
    ]
    out["summary"] = {
        "foreignKeys": len(rates),
        "orphanRateMedian": median(rates) if rates else None,
        "orphanRateMax": max(rates) if rates else None,
        "foreignKeysWithoutOrphans": sum(1 for rate in rates if rate == 0),
        "primaryKeys": len(duplicates),
        "primaryKeysUnique": sum(1 for rate in duplicates if rate == 0),
        "duplicateRateMedian": median(duplicates) if duplicates else None,
    }
    return out


# ---- FLUX verify ------------------------------------------------------------------


def _count(names: Sequence[Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for name in names:
        counts[str(name)] = counts.get(str(name), 0) + 1
    return counts


def _shape_flags(shape: Mapping[str, Any]) -> Tuple[Dict[str, int], int]:
    """Every flag's alert name, counted, and how many columns verify could not read."""
    columns = [column for table in shape.get("tables", []) for column in table.get("columns", [])]
    unread = sum(1 for column in columns if column.get("status") not in (None, "read", "checked"))
    alerts = [flag.get("alert") for column in columns for flag in column.get("flags", [])]
    return _count(alerts), unread


def read_verify(document: Mapping[str, Any]) -> Dict[str, Any]:
    """The scorecard, fidelity outcomes and shape flags from ``fluxsim verify --json``."""
    artifacts = document.get("artifacts") or {}
    scorecard = ((artifacts.get("scorecard") or {}).get("document")) or {}
    shape = scorecard.get("shapeFlags") or {}
    flags, unread = _shape_flags(shape)
    fidelity = artifacts.get("fidelity") or {}
    refusals = [
        refusal.get("code") for entry in fidelity.values() for refusal in entry.get("refusals", [])
    ]
    overall = scorecard.get("overall") or {}
    return {
        "exitCode": document.get("exitCode"),
        "rowsConform": (artifacts.get("rowConformance") or {}).get("ok"),
        "scorecardStatus": overall.get("status"),
        "scorecardScore": overall.get("score"),
        "dimensions": {
            entry.get("dimension"): entry.get("status") for entry in scorecard.get("dimensions", [])
        },
        "fidelityStatus": {table: entry.get("status") for table, entry in fidelity.items()},
        "refusalCodes": _count(refusals),
        "shapeTests": shape.get("tests"),
        "shapeFlags": flags,
        "shapeColumnsUnread": unread,
    }
