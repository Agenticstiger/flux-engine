# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The calendar and clock rules, and the ticket's dependency, the owner writes into the contract (ADR 0011).

``date_dim`` is a calendar keyed by the Julian day, and ``time_dim`` a clock keyed by the
second of the day. Every attribute that FLUID SQL/1 can state from the key is a contract
rule: a ``derive`` for each attribute, and one difference-form ``check`` for the date, which
FLUX generates through the FLUX setting ``generates: d_date`` (EXPR-SPEC-2, sections 1 and
6). The owner counts each rule's violating rows on the source with forge-cli's own reading of
FLUID SQL/1 (``fluid_build.fluid_sql``), so a rule and its band mean what every other
consumer of the contract reads.

The rules follow dsdgen's calendar, quirks included:

* the day of the week counts from Sunday as 0, and dsdgen's own value differs from the key's
  on a few days at the two ends of the calendar, which the band counts;
* the quarter sequence steps at March, June, September and December, as dsdgen's
  ``month / 3`` does, and the weekend is Friday and Saturday;
* dsdgen puts April 1, July 1 and October 1 of a year not divisible by four in the quarter
  before. The quarter's rule states that, from the day of the month, so it holds on every row
  and needs no band.

The day of the month is a date part, which FLUID SQL/1 leaves out, so FLUX generates it from
a FLUX-side rule that ``bundle.py`` writes (EXPR-SPEC-2, section 1). The contract rules that
read it (the first day of the month, the quarter) are written here in FLUID SQL/1 and are set
after it. Two columns that need FLUX-side rules, the same day a year back and a quarter's
name, follow the key the same way; the last day of the month and the same day a quarter back
keep their own statistics, because dsdgen's own values for them are not a function of the date.

FLUID SQL/1 has no remainder and no floor, so ``ROUND((n - (d - 1) / 2) / d, 0)`` is
``n`` divided by ``d`` rounded down, for the whole numbers here: ``ROUND`` rounds a tie
away from zero, and the shift keeps every quotient off a tie.

A sale's ticket is a functional dependency (ADR 0011, step 2): every line of a ticket
carries the ticket's date, time, customer, demographics, address and store. The owner writes it
as a contract ``dependency`` and counts its violating rows on the source as EXPR-SPEC-2,
section 8 counts them (the g3 measure: a key's covered rows minus those that carry its most
common ``to`` tuple, a NULL being a value). A dependency no row violates carries no band. One
whose violating rows are its columns' own NULLs, as dsdgen's are, carries an upper bound in
``rows``, the count rounded up to a multiple of ``ROUND_BASE`` as a rule's band is, and an
integer, since a contract that declares keys cannot carry a YAML float. FLUX then puts the
columns' NULLs on NULL rows, at their declared null fractions, while every non-NULL value of a
key agrees. 1 to 9 violating rows leave the dependency out, as for any published count.

A band is a count of rows over a calendar or a clock, which hold no person. It still passes
the threshold and the rounding every other published count does: a rule that fewer than
the owner's minimum cell of rows violate is left out of the contract, and a band is rounded
up to a multiple of ``ROUND_BASE``, so the source still meets it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

#: A published count is rounded to a multiple of this (as ADR 0003's output checks).
ROUND_BASE = 5

#: The key of a day divided by 7, rounded down, shifted so that Sunday is 0.
_WEEKS = "ROUND((d_date_sk - 2) / 7, 0)"
#: The second of the day divided by 60, rounded down.
_MINUTES = "ROUND((t_time_sk - 29.5) / 60, 0)"


@dataclass(frozen=True)
class Rule:
    """One contract invariant: a ``derive`` of ``column``, or a ``check`` when it has none."""

    id: str
    table: str
    column: Optional[str]
    text: str
    #: The column FLUX generates a ``check`` into (its FLUX setting), when it has one.
    generates: Optional[str] = None
    #: Columns the text reads that no other rule needs, which the expose must have too.
    reads: Sequence[str] = ()

    def columns(self) -> Sequence[str]:
        """The columns the rule names as its target, and those it reads beyond them."""
        named = [name for name in (self.column, self.generates) if name is not None]
        return [*named, *self.reads]


@dataclass(frozen=True)
class Dependency:
    """One contract ``dependency``: equal ``from_columns`` mean equal ``to_columns``."""

    id: str
    table: str
    from_columns: Sequence[str]
    to_columns: Sequence[str]

    def columns(self) -> Sequence[str]:
        return [*self.from_columns, *self.to_columns]


#: The ticket of a store sale: its date, time, customer, demographics, address and store.
DEPENDENCIES = (
    Dependency(
        "ticket",
        "store_sales",
        ("ss_ticket_number",),
        (
            "ss_sold_date_sk",
            "ss_sold_time_sk",
            "ss_customer_sk",
            "ss_cdemo_sk",
            "ss_hdemo_sk",
            "ss_addr_sk",
            "ss_store_sk",
        ),
    ),
)


RULES = (
    Rule(
        "date_of_key",
        "date_dim",
        None,
        "d_date_sk - 2440588 = DAYS_BETWEEN(DATE '1970-01-01', d_date)",
        generates="d_date",
    ),
    Rule("year_of_date", "date_dim", "d_year", "YEARS_BETWEEN(DATE '1900-01-01', d_date) + 1900"),
    Rule(
        "month_of_date",
        "date_dim",
        "d_moy",
        "MONTHS_BETWEEN(DATE '1900-01-01', d_date) - 12 * (d_year - 1900) + 1",
    ),
    Rule("month_sequence", "date_dim", "d_month_seq", "MONTHS_BETWEEN(DATE '1900-01-01', d_date)"),
    Rule(
        "quarter_sequence",
        "date_dim",
        "d_quarter_seq",
        "4 * (d_year - 1900) + ROUND((d_moy - 1) / 3, 0) + 1",
    ),
    Rule("week_sequence", "date_dim", "d_week_seq", "ROUND((d_date_sk - 2415018) / 7, 0)"),
    Rule("day_of_week", "date_dim", "d_dow", f"d_date_sk + 1 - 7 * {_WEEKS}"),
    Rule(
        "day_name",
        "date_dim",
        "d_day_name",
        "CASE WHEN d_dow = 0 THEN 'Sunday' WHEN d_dow = 1 THEN 'Monday' "
        "WHEN d_dow = 2 THEN 'Tuesday' WHEN d_dow = 3 THEN 'Wednesday' "
        "WHEN d_dow = 4 THEN 'Thursday' WHEN d_dow = 5 THEN 'Friday' ELSE 'Saturday' END",
    ),
    Rule("weekend", "date_dim", "d_weekend", "CASE WHEN d_dow IN (5, 6) THEN 'Y' ELSE 'N' END"),
    Rule(
        "first_day_of_month",
        "date_dim",
        "d_first_dom",
        "d_date_sk - d_dom + 1",
        reads=("d_dom",),
    ),
    Rule(
        "quarter_of_year",
        "date_dim",
        "d_qoy",
        "CASE WHEN d_dom = 1 AND d_moy IN (4, 7, 10) "
        "AND d_year - 4 * ROUND((d_year - 1.5) / 4, 0) <> 0 "
        "THEN ROUND((d_moy + 1) / 3, 0) - 1 ELSE ROUND((d_moy + 1) / 3, 0) END",
        reads=("d_dom", "d_moy", "d_year"),
    ),
    Rule("fiscal_year", "date_dim", "d_fy_year", "d_year"),
    Rule("fiscal_quarter_sequence", "date_dim", "d_fy_quarter_seq", "d_quarter_seq"),
    Rule("fiscal_week_sequence", "date_dim", "d_fy_week_seq", "d_week_seq"),
    Rule("time_of_key", "time_dim", "t_time", "t_time_sk"),
    Rule("hour_of_time", "time_dim", "t_hour", "ROUND((t_time_sk - 1799.5) / 3600, 0)"),
    Rule("minute_of_time", "time_dim", "t_minute", f"{_MINUTES} - 60 * t_hour"),
    Rule("second_of_time", "time_dim", "t_second", f"t_time_sk - 60 * {_MINUTES}"),
    Rule("half_of_day", "time_dim", "t_am_pm", "CASE WHEN t_hour < 12 THEN 'AM' ELSE 'PM' END"),
    Rule(
        "shift",
        "time_dim",
        "t_shift",
        "CASE WHEN t_hour BETWEEN 7 AND 14 THEN 'first' "
        "WHEN t_hour BETWEEN 15 AND 22 THEN 'second' ELSE 'third' END",
    ),
    Rule(
        "sub_shift",
        "time_dim",
        "t_sub_shift",
        "CASE WHEN t_hour < 6 THEN 'night' WHEN t_hour < 12 THEN 'morning' "
        "WHEN t_hour < 17 THEN 'afternoon' ELSE 'evening' END",
    ),
    Rule(
        "meal_time",
        "time_dim",
        "t_meal_time",
        "CASE WHEN t_hour BETWEEN 6 AND 9 THEN 'breakfast' "
        "WHEN t_hour BETWEEN 12 AND 14 THEN 'lunch' "
        "WHEN t_hour BETWEEN 17 AND 19 THEN 'dinner' END",
    ),
)


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _column_sql(relationship: Optional[str], name: str) -> str:
    # The rules read their own row only, so no relationship has an alias to read from.
    if relationship is not None:
        raise ValueError(f"a calendar or clock rule reads its own row, not {relationship}.{name}")
    return "t." + _quoted(name)


def _model(table: str, schema: Sequence[Mapping[str, Any]]) -> Any:
    """The expose as forge-cli's FLUID SQL/1 checker sees it."""
    from fluid_build.fluid_sql import Column, Model, column_type
    from flux_stats.fidelity import declared_enum_values

    model = Model(name=table)
    for entry in schema:
        declared = str(entry["type"])
        enum = declared_enum_values(declared) if declared.lower().startswith("enum(") else ()
        model.columns[entry["name"]] = Column(
            name=entry["name"],
            declared=declared,
            type=column_type(declared),
            enum=tuple(enum),
            required=bool(entry.get("required")),
        )
    return model


def violating_rows(con: Any, table: str, schema: Sequence[Mapping[str, Any]], rule: Rule) -> int:
    """How many rows of *table* violate *rule*, as RFC-data-rules.md section 1.5 counts them.

    A ``derive`` row violates when its column ``IS DISTINCT FROM`` the value, and a
    ``check`` row when its predicate is not TRUE. forge-cli checks the text against the
    contract's own column types first, so a rule that does not fit its column fails here.
    """
    from fluid_build.fluid_sql import PREDICATE, VALUE, check, parse, render_duckdb

    model = _model(table, schema)
    node = parse(rule.text)
    if rule.column is None:
        checked = check(node, model, PREDICATE)
        holds = render_duckdb(node, checked, _column_sql, predicate=True)
        where = f"NOT ({holds})"
    else:
        checked = check(node, model, VALUE, derive=model.columns[rule.column])
        value = render_duckdb(node, checked, _column_sql)
        where = f"t.{_quoted(rule.column)} IS DISTINCT FROM ({value})"
    return int(con.execute(f"SELECT count(*) FROM {_quoted(table)} t WHERE {where}").fetchone()[0])


def dependency_violating_rows(con: Any, dependency: Dependency) -> int:
    """How many rows of the dependency's table violate it, as EXPR-SPEC-2 section 8 counts them.

    The rows with a NULL in a ``from`` column are not covered. For each key, the violating rows
    are its covered rows minus those that carry its most common ``to`` tuple, and a NULL in a
    ``to`` column is a value (the g3 measure of approximate dependencies). ``GROUP BY`` puts
    NULLs in one group, so the tuples are compared as the check compares them.
    """
    table = _quoted(dependency.table)
    sources = ", ".join(_quoted(name) for name in dependency.from_columns)
    targets = ", ".join(_quoted(name) for name in dependency.to_columns)
    covered = " AND ".join(f"{_quoted(name)} IS NOT NULL" for name in dependency.from_columns)
    sql = (
        f"WITH tuples AS (SELECT {sources}, {targets}, count(*) AS n FROM {table} "
        f"WHERE {covered} GROUP BY ALL), "
        f"tops AS (SELECT max(n) AS top FROM tuples GROUP BY {sources}) "
        "SELECT coalesce((SELECT sum(n) FROM tuples), 0) - coalesce((SELECT sum(top) FROM tops), 0)"
    )
    return int(con.execute(sql).fetchone()[0])


def dependency_covered_rows(con: Any, dependency: Dependency) -> int:
    """How many rows of the dependency's table it covers: those with no NULL in a ``from`` column."""
    covered = " AND ".join(f"{_quoted(name)} IS NOT NULL" for name in dependency.from_columns)
    sql = f"SELECT count(*) FROM {_quoted(dependency.table)} WHERE {covered}"
    return int(con.execute(sql).fetchone()[0])


def _rounded_up(count: int) -> int:
    return -(-count // ROUND_BASE) * ROUND_BASE


def contract_invariants(
    con: Any, table: str, schema: Sequence[Mapping[str, Any]], *, minimum_cell: int
) -> List[Dict[str, Any]]:
    """The invariants the owner writes for *table*, each with the band the source needs.

    A rule whose columns the expose does not have is left out, and so is one that between 1
    and ``minimum_cell - 1`` rows violate, since a band that small would describe single
    rows. A rule every row holds carries no band, which reads as no violating row. A dependency
    is written with no band when no row violates it, and with an upper bound in rows when
    some do, unless 1 to ``minimum_cell - 1`` rows do.
    """
    names = {entry["name"] for entry in schema}
    entries = [
        _rule_entry(con, table, schema, rule, minimum_cell)
        for rule in RULES
        if rule.table == table and set(rule.columns()) <= names
    ] + [
        _dependency_entry(con, dependency, minimum_cell)
        for dependency in DEPENDENCIES
        if dependency.table == table and set(dependency.columns()) <= names
    ]
    return [entry for entry in entries if entry is not None]


def _rule_entry(
    con: Any, table: str, schema: Sequence[Mapping[str, Any]], rule: Rule, minimum_cell: int
) -> Optional[Dict[str, Any]]:
    """*rule* as the owner writes it, with a ``rows`` band when some rows violate it."""
    count = violating_rows(con, table, schema, rule)
    if 0 < count < minimum_cell:
        return None
    entry: Dict[str, Any] = {"id": rule.id}
    if rule.column is None:
        entry["check"] = rule.text
    else:
        entry["derive"] = rule.column
        entry["expr"] = rule.text
    if count:
        entry["mustBeLessOrEqualTo"] = _rounded_up(count)
        entry["unit"] = "rows"
    return entry


def _dependency_entry(
    con: Any, dependency: Dependency, minimum_cell: int
) -> Optional[Dict[str, Any]]:
    """*dependency* as the owner writes it, with a ``rows`` band when some rows violate it."""
    count = dependency_violating_rows(con, dependency)
    if 0 < count < minimum_cell:
        return None
    entry: Dict[str, Any] = {
        "id": dependency.id,
        "dependency": {"from": list(dependency.from_columns), "to": list(dependency.to_columns)},
    }
    if count:
        entry["mustBeLessOrEqualTo"] = _rounded_up(count)
        entry["unit"] = "rows"
    return entry
