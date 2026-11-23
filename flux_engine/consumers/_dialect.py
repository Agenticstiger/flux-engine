# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Dialect spellings that mean something other than what DuckDB's text would mean (ADR 0028 section 2).

The engine reads a consumer query in the dialect its author wrote, and runs the DuckDB text that
sqlglot writes for the tree. Most spellings carry over. A few do not, and each is handled here, on
the tree and before the allow-list reads it, so that the allow-list stays closed and sees only
nodes it already lists:

* a weekday or week part is rewritten to the DuckDB expression that has the value the dialect gives
  it under the dialect's documented default settings, or refused (``FLX-3622``) when no exact
  rewrite is written, naming the dialect and the part;
* ``YEAR(0)`` in T-SQL, where an integer is a day count from 1900-01-01, is rewritten to that date;
* the nodes sqlglot writes for a T-SQL ``DATEDIFF`` operand (``TimeStrToTime``) and for a Snowflake
  ``LEN`` (a binary-aware ``Length``) are rewritten to a cast and a plain ``LENGTH``.

Every rewrite builds only nodes the allow-list lists (``DayOfWeek``, ``DayOfYear``, ``Add``,
``Mod``, ``IntDiv``, ``Cast``, ``Interval``, ``DateTrunc``), so none of them reads a file, the
environment or the clock. The rewrites copy an operand a few times, so an operand is capped.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.tokens import TokenType

from flux_engine.consumers import _allow
from flux_engine.consumers.errors import FLX_3622, FLX_3627
from flux_engine.core.errors import FluxError

__all__ = ["refuse_misread_spellings", "translate"]

#: The most nodes an operand may hold when a rewrite repeats it.
_MAX_OPERAND_NODES = 200

#: The first day of the week, as DuckDB's ``DAYOFWEEK`` numbers it (Sunday is 0).
_DAYS: Dict[str, int] = {
    "SUNDAY": 0,
    "MONDAY": 1,
    "TUESDAY": 2,
    "WEDNESDAY": 3,
    "THURSDAY": 4,
    "FRIDAY": 5,
    "SATURDAY": 6,
}

#: T-SQL's ``datetime`` holds days from 1753-01-01 to 9999-12-31, counted from 1900-01-01.
_TSQL_FIRST_DAY = -53_690
_TSQL_LAST_DAY = 2_958_463

#: A day count with more digits than this (leading zeros apart) is out of range and is not parsed.
_MAX_DAY_DIGITS = 40

#: Where the engine maps the parts of each dialect, for the refusal's message.
_MAPPED = "tsql, bigquery, snowflake and mysql"

_PART_TOKENS = frozenset({TokenType.VAR, TokenType.IDENTIFIER, TokenType.STRING})

#: The T-SQL date functions whose first argument is a part name.
_TSQL_PART_FUNCTIONS = frozenset(
    {"DATEPART", "DATENAME", "DATEADD", "DATEDIFF", "DATEDIFF_BIG", "DATETRUNC"}
)

#: Part abbreviations that sqlglot's T-SQL reader maps to another part than T-SQL's documentation
#: gives them, checked against sqlglot 30.33 and the DATEPART, DATEADD, DATEDIFF, DATENAME and
#: DATETRUNC pages. ``w`` is the weekday in T-SQL and is read as the week, or, in DATEADD, as a
#: week to add where T-SQL adds a day; ``y`` is the day of the year and is read as the year in
#: DATEADD, DATENAME and DATETRUNC (and passed through unread in DATEDIFF). ``m`` in DATETRUNC is
#: the month and is read as the minute; ``dy`` in DATENAME is the day of the year and is written
#: as the day of the month. Every other abbreviation in those tables reads as T-SQL means it, or
#: is refused later by the allow-list.
_TSQL_MISREAD: Dict[str, str] = {"w": "weekday", "y": "day of the year"}
_TSQL_MISREAD_IN: Dict[Tuple[str, str], str] = {
    ("DATETRUNC", "m"): "month",
    ("DATENAME", "dy"): "day of the year",
}


def refuse_misread_spellings(sql: str, dialect: str, label: str) -> None:
    """Refuse the spellings the parser reads as another part than the dialect does.

    Decided from the token stream, so a comment, a bracket or a quote between the parenthesis and
    the part does not hide it.
    """
    if dialect.lower() != "tsql":
        return
    try:
        tokens = Dialect.get_or_raise("tsql").tokenize(sql)
    except Exception:  # the caller has already tokenized the text; a failure here refuses later
        return
    for index in range(len(tokens) - 2):
        name = tokens[index].text.upper()
        if name not in _TSQL_PART_FUNCTIONS or tokens[index + 1].token_type != TokenType.L_PAREN:
            continue
        first = tokens[index + 2]
        if first.token_type not in _PART_TOKENS:
            continue
        part = first.text.lower()
        meaning = _TSQL_MISREAD.get(part) or _TSQL_MISREAD_IN.get((name, part))
        if meaning is not None:
            raise FluxError(
                FLX_3622,
                f"{label} writes the T-SQL part {first.text} in {name}, which the parser reads as "
                f"another part than T-SQL's {meaning} in the dialect tsql; write {meaning.replace(' ', '')}"
                " in full or use another part",
            )


# ---------------------------------------------------------------------------------------------
# Building DuckDB expressions
# ---------------------------------------------------------------------------------------------


def _num(value: int) -> exp.Expr:
    return exp.Literal.number(value)


def _paren(node: exp.Expr) -> exp.Expr:
    return exp.Paren(this=node)


def _dow(operand: exp.Expr) -> exp.Expr:
    """DuckDB's ``DAYOFWEEK``: Sunday 0 to Saturday 6."""
    return exp.DayOfWeek(this=operand.copy())


def _doy(operand: exp.Expr) -> exp.Expr:
    return exp.DayOfYear(this=operand.copy())


def _weekday_from_one(operand: exp.Expr) -> exp.Expr:
    """Sunday 1 to Saturday 7: T-SQL with ``DATEFIRST`` 7, BigQuery ``DAYOFWEEK`` and MySQL."""
    return _paren(exp.Add(this=_dow(operand), expression=_num(1)))


def _iso_weekday(operand: exp.Expr) -> exp.Expr:
    """Monday 1 to Sunday 7."""
    shifted = _paren(
        exp.Mod(this=_paren(exp.Add(this=_dow(operand), expression=_num(6))), expression=_num(7))
    )
    return _paren(exp.Add(this=shifted, expression=_num(1)))


def _week_from_first(operand: exp.Expr, first: int) -> exp.Expr:
    """Weeks that start on day *first*, the days before the year's first such day being week 0.

    The count of *first* days from 1 January to the date: ``(doy + 6 - days since the last first
    day) // 7``. BigQuery ``WEEK`` and ``WEEK(<day>)`` and MySQL ``WEEK`` in mode 0 are this.
    """
    since = _paren(
        exp.Mod(
            this=_paren(exp.Add(this=_dow(operand), expression=_num(7 - first))), expression=_num(7)
        )
    )
    top = exp.Sub(this=exp.Add(this=_doy(operand), expression=_num(6)), expression=since)
    return _paren(exp.IntDiv(this=_paren(top), expression=_num(7)))


def _tsql_week(operand: exp.Expr) -> exp.Expr:
    """T-SQL ``week`` with ``DATEFIRST`` 7: week 1 holds 1 January, and weeks start on Sunday."""
    day = _paren(exp.Sub(this=_doy(operand), expression=_num(1)))
    # The weekday of 1 January, Sunday 0: (weekday - day offset) mod 7, written without a negative.
    jan_first = _paren(
        exp.Mod(
            this=_paren(
                exp.Add(this=_dow(operand), expression=exp.Mul(this=_num(6), expression=day.copy()))
            ),
            expression=_num(7),
        )
    )
    quotient = _paren(
        exp.IntDiv(this=_paren(exp.Add(this=day, expression=jan_first)), expression=_num(7))
    )
    return _paren(exp.Add(this=quotient, expression=_num(1)))


def _iso_week(operand: exp.Expr) -> exp.Expr:
    return exp.WeekOfYear(this=operand.copy())


def _week_start_trunc(operand: exp.Expr, first: int) -> exp.Expr:
    """``DATE_TRUNC`` to the week that starts on day *first*, as a date.

    DuckDB truncates to Monday, so the date is moved back to a Monday-start week and forward again.
    """
    offset = (first - 1) % 7
    unit = exp.Var(this="DAY")

    def days() -> exp.Expr:
        return exp.Interval(this=exp.Literal.string(str(offset)), unit=unit.copy())

    shifted = operand.copy() if offset == 0 else exp.Sub(this=operand.copy(), expression=days())
    truncated: exp.Expr = exp.DateTrunc(unit=exp.Literal.string("WEEK"), this=shifted)
    if offset:
        truncated = exp.Add(this=truncated, expression=days())
    return exp.Cast(this=_paren(truncated), to=exp.DataType.build("DATE"))


# ---------------------------------------------------------------------------------------------
# Which node is a weekday or a week part
# ---------------------------------------------------------------------------------------------

_NUMBERED_NAMES = frozenset({"WEEK", "WEEKS", "DAYOFWEEK", "DOW", "ISOWEEK", "WEEKISO"})
_UNIT_CLASSES: Tuple[Tuple[type, str], ...] = (
    (exp.Extract, "this"),
    (exp.DateTrunc, "unit"),
    (exp.TimestampTrunc, "unit"),
    (exp.DatetimeTrunc, "unit"),
)


def _unit_name(unit: object) -> str:
    return str(getattr(unit, "name", "") or unit or "").upper()


def _week_start_day(unit: object) -> Optional[int]:
    """The first day of ``WEEK(<day>)`` as DuckDB numbers it, or ``None`` when *unit* is no such unit."""
    if isinstance(unit, exp.WeekStart):
        return _DAYS.get(_unit_name(unit.this))
    return None


def _part_of(node: exp.Expr) -> Optional[str]:
    """The name of the weekday or week part *node* holds, or ``None``."""
    if isinstance(node, (exp.DayOfWeek, exp.Week, exp.DayOfWeekIso)):
        return type(node).__name__
    for cls, key in _UNIT_CLASSES:
        if isinstance(node, cls):
            unit = node.args.get(key)
            if isinstance(unit, exp.WeekStart):
                return "WEEK(" + _unit_name(unit.this) + ")"
            name = _unit_name(unit)
            if name in _NUMBERED_NAMES:
                return name
    return None


def _operand_of(node: exp.Expr) -> Optional[exp.Expr]:
    found = node.args.get("expression") if isinstance(node, exp.Extract) else node.args.get("this")
    return found if isinstance(found, exp.Expr) else None


# ---------------------------------------------------------------------------------------------
# The mapping for each dialect
# ---------------------------------------------------------------------------------------------

#: What a mapper returns: the replacement, or the node itself when it already means the same.
_Mapper = Callable[[exp.Expr], Optional[exp.Expr]]


def _unit(node: exp.Expr) -> str:
    return _unit_name(node.args.get("this")) if isinstance(node, exp.Extract) else ""


def _map_tsql(node: exp.Expr) -> Optional[exp.Expr]:
    # DATEFIRST is the session's; the default of U.S. English, 7 (Sunday), is what is read.
    if isinstance(node, exp.Extract):
        found = node.args["expression"]
        unit = _unit(node)
        if unit == "DAYOFWEEK":
            return _weekday_from_one(found)
        if unit == "WEEK":
            return _tsql_week(found)
        if unit == "WEEKISO":
            return _iso_week(found)
    return None


def _map_bigquery(node: exp.Expr) -> Optional[exp.Expr]:
    if isinstance(node, exp.Extract):
        found = node.args["expression"]
        unit = node.args.get("this")
        if _unit(node) == "DAYOFWEEK":
            return _weekday_from_one(found)
        if _unit(node) == "WEEK":
            return _week_from_first(found, 0)
        if _unit(node) == "ISOWEEK":
            return _iso_week(found)
        first = _week_start_day(unit)
        if first is not None:
            return _week_from_first(found, first)
    if type(node) is exp.DateTrunc:
        unit = node.args.get("unit")
        first = 1 if _unit_name(unit) == "ISOWEEK" else _week_start_day(unit)
        found = node.args.get("this")
        if first is not None and isinstance(found, exp.Expr):
            return _week_start_trunc(found, first)
    return None


def _map_snowflake(node: exp.Expr) -> Optional[exp.Expr]:
    # With WEEK_START 0 and WEEK_OF_YEAR_POLICY 0, the defaults: Sunday is 0 in DAYOFWEEK, weeks are
    # ISO weeks, and DATE_TRUNC to the week goes to Monday. Each is what DuckDB's text computes.
    if isinstance(node, (exp.DayOfWeek, exp.Week)):
        return node
    if isinstance(node, exp.DayOfWeekIso) and isinstance(node.args.get("this"), exp.Expr):
        return _iso_weekday(node.args["this"])
    if isinstance(node, exp.Extract) and _unit(node) in ("DAYOFWEEK", "WEEK"):
        return node
    if isinstance(node, exp.Extract) and _unit(node) == "WEEKISO":
        return _iso_week(node.args["expression"])
    if isinstance(node, exp.TimestampTrunc) and _unit_name(node.args.get("unit")) == "WEEK":
        return node
    return None


def _map_mysql(node: exp.Expr) -> Optional[exp.Expr]:
    found = node.args.get("this")
    if not isinstance(found, exp.Expr):
        return None
    if isinstance(node, exp.DayOfWeek):
        return _weekday_from_one(node.args["this"])
    if isinstance(node, exp.Week):
        # WEEK(x) with no mode reads default_week_format, 0 unless the server sets it.
        mode = node.args.get("mode")
        if mode is None or _mode_is(mode, "0"):
            return _week_from_first(found, 0)
        if _mode_is(mode, "3"):
            return _iso_week(found)
    return None


def _mode_is(mode: exp.Expr, value: str) -> bool:
    return isinstance(mode, exp.Literal) and not mode.is_string and mode.name == value


_MAPPERS: Dict[str, _Mapper] = {
    "tsql": _map_tsql,
    "bigquery": _map_bigquery,
    "snowflake": _map_snowflake,
    "mysql": _map_mysql,
}


def _refusal(label: str, dialect: str, part: str, node: exp.Expr) -> FluxError:
    mode = node.args.get("mode")
    if isinstance(node, exp.Week) and isinstance(mode, exp.Literal):
        part = f"WEEK with mode {mode.name}"
    return FluxError(
        FLX_3622,
        f"{label} uses the weekday or week part {part} in the dialect {dialect}, which has no exact "
        f"DuckDB mapping here (the engine maps them for {_MAPPED}, under each dialect's default "
        "settings, and for duckdb as it stands); write the query in the duckdb dialect, or use "
        "another part",
    )


def _check_size(operand: exp.Expr, label: str) -> None:
    size = 0
    for _ in operand.walk():
        size += 1
        if size > _MAX_OPERAND_NODES:
            raise FluxError(
                FLX_3627,
                f"{label} gives a weekday or week part an operand of more than {_MAX_OPERAND_NODES} "
                "expression nodes; the part is rewritten for DuckDB by repeating its operand",
            )


# ---------------------------------------------------------------------------------------------
# Other rewrites
# ---------------------------------------------------------------------------------------------


def _day_count(node: exp.Expr) -> Optional[int]:
    """The whole number a literal (or a negated one) holds, else ``None``."""
    sign = 1
    if isinstance(node, exp.Neg):
        sign, node = -1, node.this
    if isinstance(node, exp.Literal) and not node.is_string and node.name.isdigit():
        if len(node.name.lstrip("0")) > _MAX_DAY_DIGITS:
            return sign * (_TSQL_LAST_DAY + 1)
        return sign * int(node.name)
    return None


def _is_number(node: exp.Expr) -> bool:
    inner = node.this if isinstance(node, exp.Neg) else node
    return isinstance(inner, exp.Literal) and not inner.is_string


def _days_after_1900(operand: exp.Expr, name: str, label: str) -> exp.Expr:
    """The date T-SQL reads the number *operand* as (days after 1900-01-01), or a refusal."""
    days = _day_count(operand)
    if days is None or not _TSQL_FIRST_DAY <= days <= _TSQL_LAST_DAY:
        raise FluxError(
            FLX_3622,
            f"{label} gives {name} a number where a date is meant; only T-SQL reads a whole number "
            f"from {_TSQL_FIRST_DAY} to {_TSQL_LAST_DAY} as days after 1900-01-01, and the engine "
            "refuses any other at compile time rather than fail when it runs",
        )
    base = exp.Cast(this=exp.Literal.string("1900-01-01"), to=exp.DataType.build("DATE"))
    step = exp.Interval(this=exp.Literal.string(str(days)), unit=exp.Var(this="DAY"))
    return exp.Cast(this=_paren(exp.Add(this=base, expression=step)), to=exp.DataType.build("DATE"))


def _rewrite_to_date(node: exp.Expr, label: str) -> Optional[exp.Expr]:
    """``TsOrDsToDate`` of a number: T-SQL's day count from 1900-01-01, else refused."""
    operand = node.args.get("this")
    if not isinstance(operand, exp.Expr) or not _is_number(operand):
        return None
    default = node.args.get("default_date")
    name = describe_parent(node.parent, node)
    if isinstance(default, exp.Literal) and default.name == "1900-01-01":
        return _days_after_1900(operand, name, label)
    raise FluxError(
        FLX_3622,
        f"{label} gives {name} a number where a date is meant; only T-SQL reads a number as days "
        "after 1900-01-01, and the engine refuses it at compile time rather than fail when it runs",
    )


def describe_parent(parent: Optional[exp.Expr], node: exp.Expr) -> str:
    return _allow.describe(parent if parent is not None else node)


def _rewrite_time_str(node: exp.Expr) -> Optional[exp.Expr]:
    found = node.args.get("this")
    if not isinstance(found, exp.Expr) or set(k for k, v in node.args.items() if v) != {"this"}:
        return None
    return exp.Cast(this=found, to=exp.DataType.build("TIMESTAMP"))


def _rewrite_length(node: exp.Length) -> None:
    """Snowflake ``LEN`` and ``LENGTH``: characters for text. sqlglot writes a ``TYPEOF`` branch for bytes."""
    found = node.args.get("this")
    if (
        node.args.get("binary") is True
        and isinstance(found, exp.Expr)
        and not node.args.get("encoding")
    ):
        node.set("binary", None)
        node.set("this", exp.Cast(this=found, to=exp.DataType.build("TEXT")))


def _translate_special(node: exp.Expr, label: str) -> bool:
    """Rewrite the nodes that are the same in every dialect; whether *node* was one of them."""
    if isinstance(node, exp.TsOrDsToDate):
        replacement = _rewrite_to_date(node, label)
        if replacement is not None:
            node.replace(replacement)
        return True
    if type(node) is exp.TimeStrToTime:
        replacement = _rewrite_time_str(node)
        if replacement is not None:
            node.replace(replacement)
        return True
    if type(node) is exp.Length:
        _rewrite_length(node)
        return True
    return False


def _translate_node(node: exp.Expr, dialect: str, mapper: Optional[_Mapper], label: str) -> None:
    if _translate_special(node, label):
        return
    if dialect == "tsql" and isinstance(node, exp.Extract):
        found = node.args.get("expression")
        if isinstance(found, exp.Expr) and _is_number(found):
            node.set("expression", _days_after_1900(found, describe_parent(None, node), label))
    if dialect == "duckdb":
        return
    part = _part_of(node)
    if part is None:
        return
    operand = _operand_of(node)
    if mapper is None or operand is None:
        raise _refusal(label, dialect, part, node)
    found = mapper(node)
    if found is None:
        raise _refusal(label, dialect, part, node)
    if found is not node:
        # A rewrite repeats its operand, so the operand's size is capped; one more level of
        # nesting than the cap allows is built before it is refused, which is bounded.
        _check_size(operand, label)
        node.replace(found)


def translate(tree: exp.Expr, *, dialect: str, label: str) -> None:
    """Rewrite *tree* in place; see the module's docstring. Raises ``FLX-3622`` for what is refused.

    The nodes are taken innermost first, so that an operand a rewrite repeats is already rewritten.
    """
    dialect = dialect.lower()
    mapper = _MAPPERS.get(dialect)
    pending: List[exp.Expr] = list(tree.walk())
    for node in reversed(pending):
        _translate_node(node, dialect, mapper, label)
