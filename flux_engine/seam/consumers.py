# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The consumer queries a Simulation declares (ADR 0028 sections 1 and 8).

The people who read a dataset ask it questions in SQL. A Simulation declares those questions under
the top-level ``consumers`` key of its namespace, in the FLUX bundle and never in the FLUID contract:

.. code-block:: yaml

    extensions:
      com.agentics.flux/1:
        consumers:
          dialect: snowflake
          asOf: "2026-06-30"
          tables: { prod.analytics.subscriptions: subscriptions }
          queries:
            - { id: premium-renewed, sql: "SELECT * FROM subscriptions WHERE renewed", unit: subscriptions }

This module reads the shape of the block and nothing more: closed keys, bounds, names and exact
decimals. It parses no SQL (sqlglot is an optional dependency, and no generation reads it) and fits
nothing. **The block changes no generated row**: it is bound into no emit's ``H`` and ``G``
(:mod:`flux_engine.seam.emit` leaves it out of both), and it enters ``inputDigest`` through the
Simulation's bytes, as every key does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, FrozenSet, Iterable, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError, register
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.scorecard import _operand

__all__ = [
    "CONSUMERS_KEY",
    "DIALECTS",
    "Calibration",
    "CalibrationParameter",
    "ConsumerQuery",
    "ConsumerRegistry",
    "ConsumerView",
    "Ceilings",
    "ENGINE_EVALUATIONS",
    "ENGINE_MEMORY_BYTES",
    "ENGINE_SECONDS",
    "MOST_EVALUATIONS",
    "MOST_MEMORY_BYTES",
    "MOST_SECONDS",
    "check_consumer_documents",
    "raised_ceilings",
    "Limits",
    "check_consumer_columns",
    "read_consumers_block",
]

#: The top-level key of the namespace that declares consumer queries.
CONSUMERS_KEY = "consumers"

#: The sqlglot dialect names a registry may declare, written here so that this module imports no
#: sqlglot. A test checks each against ``sqlglot.dialects.Dialects`` when sqlglot is installed.
DIALECTS: Tuple[str, ...] = (
    "athena",
    "bigquery",
    "clickhouse",
    "databricks",
    "doris",
    "drill",
    "duckdb",
    "hive",
    "mysql",
    "oracle",
    "postgres",
    "presto",
    "redshift",
    "snowflake",
    "spark",
    "spark2",
    "sqlite",
    "starrocks",
    "tableau",
    "teradata",
    "trino",
    "tsql",
)

DEFAULT_DIALECT = "duckdb"
DEFAULT_SECONDS = 30
DEFAULT_MEMORY_BYTES = 1 << 30
DEFAULT_EVALUATIONS = 64

_MOST_PREFIXES = 64
_MOST_QUERIES = 1024
_MOST_VIEWS = 256
_MOST_SQL_BYTES = 65_536
_MOST_PARAMETERS = 256
_MOST_CALIBRATED = 64

#: What the engine lets a bundle ask for. A bundle's ``limits`` and ``calibration.evaluations`` may
#: only lower these: a bundle comes from someone else, and its text is no place to raise what the
#: machine spends. Raising them takes a command-line flag (:func:`raised_ceilings`).
ENGINE_SECONDS = 30
ENGINE_MEMORY_BYTES = 1 << 30
ENGINE_EVALUATIONS = 64

#: The most a raised ceiling may be.
MOST_SECONDS = 600
MOST_MEMORY_BYTES = 16 << 30
MOST_EVALUATIONS = 4096
_MOST_DECIMAL_DIGITS = 64

_BLOCK_KEYS = (
    "dialect",
    "asOf",
    "tables",
    "prefixes",
    "columns",
    "views",
    "queries",
    "limits",
    "calibration",
    "compiledWith",
)
_QUERY_KEYS = ("id", "sql", "unit", "groupBy", "heldOut", "tolerance", "parameters")
_VIEW_KEYS = ("name", "sql")
_LIMIT_KEYS = ("seconds", "memory")
_CALIBRATION_KEYS = ("parameters", "evaluations", "scale", "heldOutTolerance", "seed")
_CALIBRATION_PARAMETER_KEYS = ("document", "pointer", "min", "max", "step")
_COMPILED_WITH_KEYS = ("sqlglot",)

_ID = re.compile(r"[a-z][a-z0-9._-]{0,127}")
_PARAMETER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_MEMORY = re.compile(r"([1-9][0-9]{0,5})(MiB|GiB)")
_DECIMAL = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")
_POINTER = re.compile(r"(/([^~/]|~[01])*)*")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")

FLX_3601 = register(
    "FLX-3601",
    "the consumers block is not a mapping of the keys it carries",
    "A Simulation declares its consumers' questions under "
    "extensions['com.agentics.flux/1'].consumers, a mapping that carries dialect, asOf, tables, "
    "prefixes, columns, views, queries, limits, calibration and compiledWith (the sqlglot "
    "version that compiled the requirements fluxsim init applied) and nothing else. The block is "
    "read with the exact YAML reader's conventions: a key it does not carry is refused rather "
    "than ignored, since a declaration nothing reads goes missing without anyone noticing. "
    "Fix the block named in the message.",
)

FLX_3602 = register(
    "FLX-3602",
    "a consumers dialect is not one the engine reads",
    "The dialect names the SQL the consumers write in, and it is one of a closed list of sqlglot "
    "dialect names (duckdb when the block names none). The message lists them.",
)

FLX_3603 = register(
    "FLX-3603",
    "a consumers asOf is not a date",
    "asOf is the date that CURRENT_DATE, CURRENT_TIMESTAMP and NOW() read as, so a rate never "
    "depends on the day it runs. It is a full calendar date written YYYY-MM-DD, as a string.",
)

FLX_3604 = register(
    "FLX-3604",
    "a consumers table, prefix or column names nothing the bundle has",
    "tables maps each table name consumers write to an exposeId the Simulation emits, prefixes "
    "is a list of at most 64 qualifiers, and columns maps an exposeId to the column names "
    "consumers write there, each mapped to a column of that expose's contract. A name that "
    "resolves to nothing would leave a query reading a table or a column that does not exist.",
)

FLX_3605 = register(
    "FLX-3605",
    "a consumer query has a malformed or repeated id, or too many are declared",
    "A query's id is a lowercase letter followed by at most 127 lowercase letters, digits, '.', "
    "'-' and '_', and it names the query in the profile, the request and the scorecard, so it is "
    "unique across the queries. At most 1,024 queries are declared.",
)

FLX_3606 = register(
    "FLX-3606",
    "a consumer view is malformed, repeated or named like an expose",
    "A view is a mapping of a name and the SQL it stands for. Its name follows the grammar of a "
    "query id, is unique among the views and is not the exposeId of an emit, since a query that "
    "read that name could not tell the view from the table. At most 256 views are declared.",
)

FLX_3607 = register(
    "FLX-3607",
    "the SQL of a consumer query or view is not a usable text",
    "The sql of a query or a view is a non-empty string of at most 65,536 UTF-8 bytes that holds "
    "no NUL character. Whether it is a query the engine will run is decided when it is parsed.",
)

FLX_3608 = register(
    "FLX-3608",
    "a consumer query's unit or groupBy does not name what it counts",
    "unit is the exposeId or view whose rows a query's rate counts, and groupBy is a column of "
    "that unit, so a query that groups by a column also names its unit. The message names the "
    "query and the key.",
)

FLX_3609 = register(
    "FLX-3609",
    "a consumer query's tolerance or heldOut is malformed",
    "tolerance is an exact decimal from 0 to 1 written as a string, never a float, and heldOut "
    "is a boolean (false when absent). The same reading applies to calibration.heldOutTolerance.",
)

FLX_3610 = register(
    "FLX-3610",
    "a consumer query's parameters are malformed",
    "parameters maps at most 256 placeholder names, each of the form [A-Za-z_][A-Za-z0-9_]{0,63}, "
    "to a value written as a string, a whole number or a boolean. A float is refused, as every "
    "number in a bundle is exact.",
)

FLX_3611 = register(
    "FLX-3611",
    "consumers limits are malformed or past their bounds",
    "limits.seconds is a whole number from 1 to 600 (30 when absent) and limits.memory is a size "
    "such as 512MiB or 1GiB of at most 16GiB (1GiB when absent).",
)

FLX_3612 = register(
    "FLX-3612",
    "a consumers calibration declaration is malformed",
    "calibration declares at most 64 parameters, each with a document, an RFC 6901 pointer, an "
    "exact decimal min below max and a step above zero; evaluations from 1 to 4,096 (64 when "
    "absent); scale in (0, 1] (1 when absent); heldOutTolerance, an exact decimal from 0 to 1; "
    "and a seed, a whole number. Only the shape is read here: nothing is fitted.",
)

FLX_3614 = register(
    "FLX-3614",
    "consumers limits.seconds is above the engine's ceiling",
    "A bundle may only lower the wall clock a query may take: limits.seconds is at most 30, the "
    "engine's ceiling, and a bundle that asks for more is refused by every command. The "
    "command-line flag --max-query-seconds raises the ceiling to at most 600 for the command that "
    "runs the queries, for a bundle that declares no limits.seconds of its own.",
)

FLX_3615 = register(
    "FLX-3615",
    "consumers limits.memory is above the engine's ceiling",
    "A bundle may only lower the memory a query may use: limits.memory is at most 1GiB, the "
    "engine's ceiling, and a bundle that asks for more is refused by every command. The "
    "command-line flag --max-query-memory raises the ceiling to at most 16GiB for the command that "
    "runs the queries, for a bundle that declares no limits.memory of its own.",
)

FLX_3616 = register(
    "FLX-3616",
    "consumers calibration.evaluations is above the engine's ceiling",
    "A bundle may only lower the number of runs a fit may make: calibration.evaluations is at "
    "most 64, the engine's ceiling, and a bundle that asks for more is refused by every command. "
    "The command-line flag --max-evaluations raises the ceiling to at most 4,096 for the command "
    "that fits, for a bundle that declares no evaluations of its own.",
)

FLX_3617 = register(
    "FLX-3617",
    "a consumers calibration parameter names a document outside the bundle's closed set",
    "calibration.parameters[].document names one of the files that make up the bundle: a "
    "top-level *.flux.yml, *.flux.yaml, *.fluid.yml or *.fluid.yaml file of the bundle "
    "directory, the closed set of files inputDigest covers (ADR 0006). A path, a file from "
    "another directory or a file the loader does not read is refused, since the overlay a fit "
    "writes may only change the bundle's own documents.",
)

FLX_3613 = register(
    "FLX-3613",
    "a consumers block holds more than the engine reads",
    "A block carries at most 64 prefixes, 1,024 queries, 256 views, 256 parameters in a query "
    "and 64 calibration parameters. Split the questions across Simulations.",
)


@dataclass(frozen=True)
class Ceilings:
    """The largest ``limits.seconds``, ``limits.memory`` and ``calibration.evaluations`` a bundle may ask for."""

    seconds: int = ENGINE_SECONDS
    memory: int = ENGINE_MEMORY_BYTES
    evaluations: int = ENGINE_EVALUATIONS


def raised_ceilings(
    seconds: Optional[int] = None,
    memory: Optional[int] = None,
    evaluations: Optional[int] = None,
) -> Ceilings:
    """The engine's ceilings with any of them raised, as ``--max-query-seconds``,
    ``--max-query-memory`` and ``--max-evaluations`` raise them.

    A raised ceiling is at least the engine's own and at most 600 seconds, 16 GiB and 4,096
    evaluations; a value outside that is a ``ValueError``, for the flag that passes it to refuse.
    """
    chosen = []
    for name, value, low, high in (
        ("seconds", seconds, ENGINE_SECONDS, MOST_SECONDS),
        ("memory", memory, ENGINE_MEMORY_BYTES, MOST_MEMORY_BYTES),
        ("evaluations", evaluations, ENGINE_EVALUATIONS, MOST_EVALUATIONS),
    ):
        if value is None:
            value = low
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"the {name} ceiling is a whole number from {low} to {high}")
        chosen.append(value)
    return Ceilings(*chosen)


@dataclass(frozen=True)
class ConsumerView:
    """A named query other queries read from."""

    name: str
    sql: str


@dataclass(frozen=True)
class ConsumerQuery:
    """One question: its id, text and what its rate counts."""

    id: str
    sql: str
    unit: Optional[str] = None
    group_by: Optional[str] = None
    held_out: bool = False
    tolerance: Optional[Fraction] = None
    declared_tolerance: Optional[str] = None
    #: ``(name, value)`` pairs sorted by name; a value is a string, a whole number or a boolean.
    parameters: Tuple[Tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class Limits:
    """What one query may spend: wall-clock seconds and DuckDB memory in bytes."""

    seconds: int = DEFAULT_SECONDS
    memory: int = DEFAULT_MEMORY_BYTES
    #: The keys (``seconds``, ``memory``) the bundle declared, so that a command whose flags raise the
    #: runtime ceilings can tell a value the bundle chose from a default it did not.
    declared: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CalibrationParameter:
    """A value a fit may move: a document, an RFC 6901 pointer into it and its bounds."""

    document: str
    pointer: str
    minimum: Fraction
    maximum: Fraction
    step: Fraction
    declared: Tuple[str, str, str] = ("", "", "")


@dataclass(frozen=True)
class Calibration:
    """The calibration declaration (shape only)."""

    parameters: Tuple[CalibrationParameter, ...] = ()
    evaluations: int = DEFAULT_EVALUATIONS
    scale: Fraction = Fraction(1)
    declared_scale: str = "1"
    held_out_tolerance: Optional[Fraction] = None
    declared_held_out_tolerance: Optional[str] = None
    seed: Optional[int] = None
    #: Whether the block gave ``evaluations``. When it did not, a fit runs to the ceiling in force
    #: (the engine's, or what ``--max-evaluations`` raised it to), and ``evaluations`` holds the
    #: default.
    evaluations_declared: bool = False


@dataclass(frozen=True)
class ConsumerRegistry:
    """The ``consumers`` block as read."""

    dialect: str = DEFAULT_DIALECT
    as_of: Optional[str] = None
    #: ``(table name, exposeId)`` pairs sorted by name.
    tables: Tuple[Tuple[str, str], ...] = ()
    prefixes: Tuple[str, ...] = ()
    #: ``(exposeId, ((written, contract column), ...))`` sorted by exposeId, then by written name.
    columns: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...] = ()
    views: Tuple[ConsumerView, ...] = ()
    queries: Tuple[ConsumerQuery, ...] = ()
    limits: Limits = Limits()
    calibration: Calibration = Calibration()
    #: The sqlglot version that compiled the requirements ``fluxsim init`` applied
    #: (``compiledWith: {sqlglot: <version>}``), or ``None`` when none were recorded.
    compiled_sqlglot: Optional[str] = None


def _mapping(value: Any, at: str, keys: Sequence[str], code: ErrorCode) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FluxError(code, f"{at} is a mapping of {list(keys)}")
    unknown = sorted(str(key) for key in value if str(key) not in keys)
    if unknown:
        raise FluxError(code, f"{at} declares {unknown}; it carries {list(keys)}")
    return value


def _text(value: Any, at: str, code: ErrorCode) -> str:
    if not isinstance(value, str) or not value:
        raise FluxError(code, f"{at} is a non-empty string")
    return nfc(value)


def _sql(value: Any, at: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FluxError(FLX_3607, f"{at}.sql is a non-empty string")
    if "\x00" in value:
        raise FluxError(FLX_3607, f"{at}.sql holds a NUL character")
    if len(value.encode("utf-8", errors="surrogatepass")) > _MOST_SQL_BYTES:
        raise FluxError(FLX_3607, f"{at}.sql is longer than {_MOST_SQL_BYTES} UTF-8 bytes")
    return value


def _whole(value: Any, at: str, low: int, high: Optional[int], code: ErrorCode) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FluxError(code, f"{at} is a whole number")
    if value < low or (high is not None and value > high):
        bound = f"from {low} to {high}" if high is not None else f"at least {low}"
        raise FluxError(code, f"{at} is {bound}")
    return value


def _signed_decimal(value: Any, at: str, code: ErrorCode) -> Tuple[Fraction, str]:
    """An exact decimal of any sign, written as a string or a whole number, never a float."""
    if isinstance(value, bool) or isinstance(value, float):
        raise FluxError(code, f"{at} is an exact decimal, never a float or a bool")
    if isinstance(value, int):
        text = str(value) if -(10**30) < value < 10**30 else ""
    elif isinstance(value, str):
        text = value
    else:
        text = ""
    if not text or len(text) > _MOST_DECIMAL_DIGITS or not _DECIMAL.fullmatch(text):
        raise FluxError(code, f'{at} is an exact decimal written as a string, such as "0.25"')
    return Fraction(text), text


def _date(value: Any, at: str) -> str:
    match = _DATE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise FluxError(FLX_3603, f'{at} is a full date written YYYY-MM-DD, such as "2026-06-30"')
    year, month, day = (int(part) for part in match.groups())
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    longest = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if year < 1 or not 1 <= month <= 12 or not 1 <= day <= longest[month - 1]:
        raise FluxError(FLX_3603, f"{at} is {value!r}, which is not a calendar date")
    return str(value)


def _tables(raw: Any, emits: FrozenSet[str], at: str) -> Tuple[Tuple[str, str], ...]:
    if not isinstance(raw, Mapping):
        raise FluxError(FLX_3604, f"{at} is a mapping of table names to exposeIds")
    found = {}
    for name, target in raw.items():
        written = _text(name, f"{at} (a table name)", FLX_3604)
        expose = _text(target, f"{at}.{written}", FLX_3604)
        if expose not in emits:
            raise FluxError(
                FLX_3604,
                f"{at}.{written} names {expose!r}, which this Simulation does not emit "
                f"(it emits {sorted(emits)})",
            )
        if written in found:
            raise FluxError(FLX_3604, f"{at} names the table {written!r} twice")
        found[written] = expose
    return tuple(sorted(found.items()))


def _prefixes(raw: Any, at: str) -> Tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise FluxError(FLX_3604, f"{at} is a list of qualifiers")
    if len(raw) > _MOST_PREFIXES:
        raise FluxError(FLX_3613, f"{at} holds more than {_MOST_PREFIXES} prefixes")
    return tuple(_text(item, f"{at}[{index}]", FLX_3604) for index, item in enumerate(raw))


def _columns(
    raw: Any, emits: FrozenSet[str], at: str
) -> Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]:
    if not isinstance(raw, Mapping):
        raise FluxError(FLX_3604, f"{at} is a mapping of exposeIds to column maps")
    found = {}
    for key, mapping in raw.items():
        expose = _text(key, f"{at} (an exposeId)", FLX_3604)
        if expose not in emits:
            raise FluxError(
                FLX_3604,
                f"{at}.{expose} names an expose this Simulation does not emit "
                f"(it emits {sorted(emits)})",
            )
        if not isinstance(mapping, Mapping):
            raise FluxError(FLX_3604, f"{at}.{expose} is a mapping of written names to columns")
        pairs = {}
        for written, column in mapping.items():
            name = _text(written, f"{at}.{expose} (a written column name)", FLX_3604)
            if name in pairs:
                raise FluxError(FLX_3604, f"{at}.{expose} names the column {name!r} twice")
            pairs[name] = _text(column, f"{at}.{expose}.{name}", FLX_3604)
        found[expose] = tuple(sorted(pairs.items()))
    return tuple(sorted(found.items()))


def _views(raw: Any, emits: FrozenSet[str], at: str) -> Tuple[ConsumerView, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise FluxError(FLX_3606, f"{at} is a list of {{name, sql}} entries")
    if len(raw) > _MOST_VIEWS:
        raise FluxError(FLX_3613, f"{at} holds more than {_MOST_VIEWS} views")
    found = {}
    for index, entry in enumerate(raw):
        here = f"{at}[{index}]"
        mapping = _mapping(entry, here, _VIEW_KEYS, FLX_3606)
        name = mapping.get("name")
        if not isinstance(name, str) or not _ID.fullmatch(nfc(name)):
            raise FluxError(
                FLX_3606,
                f"{here}.name is a lowercase letter then at most 127 lowercase letters, digits, "
                "'.', '-' and '_'",
            )
        name = nfc(name)
        if name in found:
            raise FluxError(FLX_3606, f"{here} declares the view {name!r} twice")
        if name in emits:
            raise FluxError(FLX_3606, f"{here}.name {name!r} is the exposeId of an emit")
        found[name] = ConsumerView(name=name, sql=_sql(mapping.get("sql"), here))
    return tuple(found[name] for name in sorted(found))


def _parameters(raw: Any, at: str) -> Tuple[Tuple[str, Any], ...]:
    if not isinstance(raw, Mapping):
        raise FluxError(FLX_3610, f"{at} is a mapping of placeholder names to values")
    if len(raw) > _MOST_PARAMETERS:
        raise FluxError(FLX_3613, f"{at} holds more than {_MOST_PARAMETERS} parameters")
    found = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _PARAMETER_NAME.fullmatch(name):
            raise FluxError(
                FLX_3610, f"{at} names {name!r}; a name is [A-Za-z_][A-Za-z0-9_]{{0,63}}"
            )
        if isinstance(value, float) or not isinstance(value, (str, int, bool)):
            raise FluxError(FLX_3610, f"{at}.{name} is a string, a whole number or a boolean")
        found[name] = value
    return tuple(sorted(found.items()))


def _query(entry: Any, at: str, emits: FrozenSet[str], views: FrozenSet[str]) -> ConsumerQuery:
    mapping = _mapping(entry, at, _QUERY_KEYS, FLX_3605)
    raw_id = mapping.get("id")
    if not isinstance(raw_id, str) or not _ID.fullmatch(nfc(raw_id)):
        raise FluxError(
            FLX_3605,
            f"{at}.id is a lowercase letter then at most 127 lowercase letters, digits, "
            "'.', '-' and '_'",
        )
    name = nfc(raw_id)
    where = f"{at} ({name})"
    sql = _sql(mapping.get("sql"), where)
    unit = None
    if "unit" in mapping:
        unit = _text(mapping["unit"], f"{where}.unit", FLX_3608)
        if unit not in emits and unit not in views:
            raise FluxError(
                FLX_3608,
                f"{where}.unit names {unit!r}, which is neither an emit nor a view "
                f"(emits {sorted(emits)}, views {sorted(views)})",
            )
    group_by = None
    if "groupBy" in mapping:
        group_by = _text(mapping["groupBy"], f"{where}.groupBy", FLX_3608)
        if unit is None:
            raise FluxError(FLX_3608, f"{where}.groupBy needs a unit")
    held_out = mapping.get("heldOut", False)
    if not isinstance(held_out, bool):
        raise FluxError(FLX_3609, f"{where}.heldOut is a boolean")
    tolerance: Optional[Fraction] = None
    declared: Optional[str] = None
    if "tolerance" in mapping:
        tolerance, declared = _operand(mapping["tolerance"], f"{where}.tolerance", FLX_3609)
    parameters = (
        _parameters(mapping["parameters"], f"{where}.parameters")
        if ("parameters" in mapping)
        else ()
    )
    return ConsumerQuery(
        id=name,
        sql=sql,
        unit=unit,
        group_by=group_by,
        held_out=held_out,
        tolerance=tolerance,
        declared_tolerance=declared,
        parameters=parameters,
    )


def _limits(raw: Any, at: str, ceilings: Ceilings) -> Limits:
    mapping = _mapping(raw, at, _LIMIT_KEYS, FLX_3611)
    seconds = min(DEFAULT_SECONDS, ceilings.seconds)
    memory = min(DEFAULT_MEMORY_BYTES, ceilings.memory)
    if "seconds" in mapping:
        seconds = _whole(mapping["seconds"], f"{at}.seconds", 1, None, FLX_3611)
        if seconds > ceilings.seconds:
            raise FluxError(
                FLX_3614, f"{at}.seconds is {seconds}; the ceiling is {ceilings.seconds} seconds"
            )
    if "memory" in mapping:
        text = mapping["memory"]
        match = _MEMORY.fullmatch(text) if isinstance(text, str) else None
        if match is None:
            raise FluxError(FLX_3611, f"{at}.memory is a size such as 512MiB or 1GiB")
        memory = int(match.group(1)) << (20 if match.group(2) == "MiB" else 30)
        if memory > ceilings.memory:
            raise FluxError(
                FLX_3615, f"{at}.memory is {text}; the ceiling is {ceilings.memory >> 20}MiB"
            )
    return Limits(
        seconds=seconds,
        memory=memory,
        declared=tuple(key for key in _LIMIT_KEYS if key in mapping),
    )


def _parameters_listed(mapping: Mapping[str, Any], at: str) -> Tuple[CalibrationParameter, ...]:
    """The ``parameters`` list of a ``calibration`` block, read one by one."""
    if "parameters" not in mapping:
        return ()
    listed = mapping["parameters"]
    if not isinstance(listed, Sequence) or isinstance(listed, (str, bytes)):
        raise FluxError(FLX_3612, f"{at}.parameters is a list of parameters")
    if len(listed) > _MOST_CALIBRATED:
        raise FluxError(FLX_3613, f"{at}.parameters holds more than {_MOST_CALIBRATED}")
    return tuple(
        _calibration_parameter(item, f"{at}.parameters[{index}]")
        for index, item in enumerate(listed)
    )


def _evaluations(mapping: Mapping[str, Any], at: str, ceilings: Ceilings) -> int:
    """The evaluation budget a ``calibration`` block asks for, within the ceiling."""
    evaluations = min(DEFAULT_EVALUATIONS, ceilings.evaluations)
    if "evaluations" in mapping:
        evaluations = _whole(mapping["evaluations"], f"{at}.evaluations", 1, None, FLX_3612)
        if evaluations > ceilings.evaluations:
            raise FluxError(
                FLX_3616,
                f"{at}.evaluations is {evaluations}; the ceiling is {ceilings.evaluations}",
            )
    return evaluations


def _calibration_scale(mapping: Mapping[str, Any], at: str) -> Tuple[Fraction, str]:
    """The ``scale`` of a ``calibration`` block and the text it was written as."""
    if "scale" not in mapping:
        return Fraction(1), "1"
    scale, declared_scale = _signed_decimal(mapping["scale"], f"{at}.scale", FLX_3612)
    if not 0 < scale <= 1:
        raise FluxError(FLX_3612, f"{at}.scale is above 0 and at most 1")
    return scale, declared_scale


def _calibration_seed(mapping: Mapping[str, Any], at: str) -> Optional[int]:
    """The ``seed`` of a ``calibration`` block, or ``None``."""
    if "seed" not in mapping:
        return None
    seed = mapping["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise FluxError(FLX_3612, f"{at}.seed is a whole number")
    return seed


def _calibration(raw: Any, at: str, ceilings: Ceilings) -> Calibration:
    mapping = _mapping(raw, at, _CALIBRATION_KEYS, FLX_3612)
    parameters = _parameters_listed(mapping, at)
    evaluations = _evaluations(mapping, at, ceilings)
    scale, declared_scale = _calibration_scale(mapping, at)
    tolerance: Optional[Fraction] = None
    declared_tolerance: Optional[str] = None
    if "heldOutTolerance" in mapping:
        tolerance, declared_tolerance = _operand(
            mapping["heldOutTolerance"], f"{at}.heldOutTolerance", FLX_3609
        )
    return Calibration(
        parameters=parameters,
        evaluations=evaluations,
        scale=scale,
        declared_scale=declared_scale,
        held_out_tolerance=tolerance,
        declared_held_out_tolerance=declared_tolerance,
        seed=_calibration_seed(mapping, at),
        evaluations_declared="evaluations" in mapping,
    )


def _calibration_parameter(raw: Any, at: str) -> CalibrationParameter:
    mapping = _mapping(raw, at, _CALIBRATION_PARAMETER_KEYS, FLX_3612)
    for key in _CALIBRATION_PARAMETER_KEYS:
        if key not in mapping:
            raise FluxError(FLX_3612, f"{at} needs {key}")
    document = mapping["document"]
    if not isinstance(document, str) or not document:
        raise FluxError(FLX_3612, f"{at}.document is a non-empty file name")
    pointer = mapping["pointer"]
    if (
        not isinstance(pointer, str)
        or not pointer.startswith("/")
        or not _POINTER.fullmatch(pointer)
    ):
        raise FluxError(FLX_3612, f"{at}.pointer is an RFC 6901 JSON Pointer, such as /a/b")
    low, low_text = _signed_decimal(mapping["min"], f"{at}.min", FLX_3612)
    high, high_text = _signed_decimal(mapping["max"], f"{at}.max", FLX_3612)
    step, step_text = _signed_decimal(mapping["step"], f"{at}.step", FLX_3612)
    if not low < high:
        raise FluxError(FLX_3612, f"{at}.min is below max")
    if not step > 0:
        raise FluxError(FLX_3612, f"{at}.step is above zero")
    return CalibrationParameter(
        document=document,
        pointer=pointer,
        minimum=low,
        maximum=high,
        step=step,
        declared=(low_text, high_text, step_text),
    )


def _compiled_with(raw: Any, at: str) -> str:
    """The sqlglot version ``fluxsim init`` recorded beside the requirements it applied."""
    mapping = _mapping(raw, at, _COMPILED_WITH_KEYS, FLX_3601)
    version = mapping.get("sqlglot")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise FluxError(
            FLX_3601, f'{at}.sqlglot is a version written as a string, such as "30.33.0"'
        )
    return version


def _dialect(mapping: Mapping[str, Any], where: str) -> str:
    """The ``dialect`` of a ``consumers`` block, or the default."""
    if "dialect" not in mapping:
        return DEFAULT_DIALECT
    dialect = mapping["dialect"]
    if not isinstance(dialect, str) or dialect not in DIALECTS:
        raise FluxError(FLX_3602, f"{where}.dialect is {dialect!r}; it is one of {list(DIALECTS)}")
    return dialect


def _queries_listed(
    mapping: Mapping[str, Any], emits: FrozenSet[str], names: FrozenSet[str], where: str
) -> Tuple[ConsumerQuery, ...]:
    """The ``queries`` list of a ``consumers`` block, read one by one, each id once."""
    if "queries" not in mapping:
        return ()
    listed = mapping["queries"]
    if not isinstance(listed, Sequence) or isinstance(listed, (str, bytes)):
        raise FluxError(FLX_3605, f"{where}.queries is a list of queries")
    if len(listed) > _MOST_QUERIES:
        raise FluxError(FLX_3613, f"{where}.queries holds more than {_MOST_QUERIES} queries")
    queries = []
    seen = set()
    for index, entry in enumerate(listed):
        query = _query(entry, f"{where}.queries[{index}]", emits, names)
        if query.id in seen:
            raise FluxError(
                FLX_3605, f"{where}.queries[{index}] declares the id {query.id!r} twice"
            )
        seen.add(query.id)
        queries.append(query)
    return tuple(queries)


def read_consumers_block(
    block: Any,
    *,
    emits: FrozenSet[str],
    where: str,
    ceilings: Optional[Ceilings] = None,
) -> ConsumerRegistry:
    """Read a ``consumers`` block, or refuse it with an FLX-36nn code naming where.

    *emits* is every ``exposeId`` the Simulation emits. Every rule of ADR 0028 section 1 is read
    here except the one that needs a contract: that each ``columns`` target is a column of the
    expose (:func:`check_consumer_columns`).
    """
    ceilings = ceilings if ceilings is not None else Ceilings()
    mapping = _mapping(block, where, _BLOCK_KEYS, FLX_3601)
    dialect = _dialect(mapping, where)
    as_of = _date(mapping["asOf"], f"{where}.asOf") if "asOf" in mapping else None
    tables = _tables(mapping["tables"], emits, f"{where}.tables") if "tables" in mapping else ()
    prefixes = _prefixes(mapping["prefixes"], f"{where}.prefixes") if "prefixes" in mapping else ()
    columns = (
        _columns(mapping["columns"], emits, f"{where}.columns") if "columns" in mapping else ()
    )
    views = _views(mapping["views"], emits, f"{where}.views") if "views" in mapping else ()
    names = frozenset(view.name for view in views)
    queries = _queries_listed(mapping, emits, names, where)
    limits = (
        _limits(mapping["limits"], f"{where}.limits", ceilings) if "limits" in mapping else Limits()
    )
    calibration = (
        _calibration(mapping["calibration"], f"{where}.calibration", ceilings)
        if "calibration" in mapping
        else Calibration()
    )
    compiled = (
        _compiled_with(mapping["compiledWith"], f"{where}.compiledWith")
        if "compiledWith" in mapping
        else None
    )
    return ConsumerRegistry(
        dialect=dialect,
        as_of=as_of,
        tables=tables,
        prefixes=prefixes,
        columns=columns,
        views=views,
        queries=queries,
        limits=limits,
        calibration=calibration,
        compiled_sqlglot=compiled,
    )


def check_consumer_documents(
    registry: ConsumerRegistry, *, documents: Iterable[str], where: str
) -> None:
    """Refuse (FLX-3617) a calibration parameter whose document is not one of *documents*.

    *documents* is the file names of the bundle's closed set that the loader read as documents
    (:attr:`flux_engine.seam.resolve.LoadedBundle.flux_file_names` and ``fluid_file_names``).
    """
    known = frozenset(documents)
    for index, parameter in enumerate(registry.calibration.parameters):
        if parameter.document not in known:
            raise FluxError(
                FLX_3617,
                f"{where}: consumers.calibration.parameters[{index}].document is "
                f"{parameter.document!r}; the bundle's documents are {sorted(known)}",
            )


def check_consumer_columns(
    registry: ConsumerRegistry, *, expose_id: str, expose: Mapping[str, Any], where: str
) -> None:
    """Refuse a ``columns`` target that is not a column of *expose*'s contract (FLX-3604)."""
    wanted = nfc(expose_id)
    for key, pairs in registry.columns:
        if key != wanted:
            continue
        known = frozenset(compile_expose(expose).column_names)
        for written, column in pairs:
            if column not in known:
                raise FluxError(
                    FLX_3604,
                    f"{where}: consumers.columns.{key}.{written} maps to {column!r}, which is "
                    f"not a column of the contract (it has {sorted(known)})",
                )
