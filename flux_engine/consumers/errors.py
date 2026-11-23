# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The error codes of the consumer SQL check (ADR 0028 section 2).

Kept apart from :mod:`flux_engine.consumers.check` so that ``fluxsim explain`` and the docs
generator can load the catalog without sqlglot installed. This module imports nothing but the
engine's own error catalog.

Every refusal names the query or view and the construct that was refused, and never a value of
the data: a consumer's text is a question, and the checker never sees a row.
"""

from __future__ import annotations

from flux_engine.core.errors import register

__all__ = [
    "FLX_3618",
    "FLX_3619",
    "FLX_3620",
    "FLX_3621",
    "FLX_3622",
    "FLX_3623",
    "FLX_3624",
    "FLX_3625",
    "FLX_3626",
    "FLX_3627",
    "FLX_3628",
    "FLX_3629",
    "FLX_3630",
    "FLX_3631",
    "FLX_3632",
    "FLX_3633",
    "FLX_3634",
    "FLX_3635",
    "FLX_3636",
    "FLX_3637",
    "FLX_3713",
    "FLX_3714",
    "FLX_3715",
]

FLX_3618 = register(
    "FLX-3618",
    "a consumer query or view does not parse",
    "The text is read by sqlglot in the dialect the consumers block declares, and it did not "
    "tokenize or parse there, or it holds no statement at all. The message names the query or "
    "view and sqlglot's own reading of the first problem. Fix the text, or declare the dialect "
    "it is written in.",
)

FLX_3619 = register(
    "FLX-3619",
    "a consumer query or view holds more than one statement",
    "A query or a view is exactly one statement. A text that holds two, such as a SELECT followed "
    "by a DROP, is refused whole, so that no later statement is ever read as part of the first. "
    "Split the text into separate queries.",
)

FLX_3620 = register(
    "FLX-3620",
    "a consumer query or view is not a query",
    "Only a SELECT, a set operation of them (UNION, INTERSECT, EXCEPT), or WITH before one is "
    "run. DDL, DML, PRAGMA, SET, ATTACH, COPY, INSTALL, LOAD, EXPORT, CALL, and anything sqlglot "
    "reads as a command, are refused. The message names the kind of statement found.",
)

FLX_3621 = register(
    "FLX-3621",
    "a consumer query or view uses an expression the engine does not allow",
    "Every node of the parsed text must be on a closed list of expression classes: column "
    "references, literals, comparisons, boolean logic, arithmetic, CASE, CAST to a closed list of "
    "types, IN, BETWEEN, LIKE, ILIKE, IS, joins with ON, subqueries, WITH that is not recursive, "
    "set operations, GROUP BY, HAVING, ORDER BY, LIMIT, OFFSET, DISTINCT, and windows with "
    "PARTITION BY and ORDER BY. A node outside the list, or a modifier of an allowed node that the "
    "list does not name (a frame, USING, NATURAL, a star modifier, WITH RECURSIVE), is refused. "
    "The message names the node. Rewrite the question with the allowed forms.",
)

FLX_3622 = register(
    "FLX-3622",
    "a consumer query or view calls a function the engine does not allow",
    "Only functions on a closed list that read nothing but their arguments are run: count, sum, "
    "avg, min, max, stddev and variance, lower, upper, trim, length, substring, concat, replace, "
    "abs, round, floor, ceil, trunc, coalesce, nullif, date_trunc, extract and the date parts, date and "
    "timestamp arithmetic, and the window functions row_number, rank and dense_rank. A weekday or "
    "week part (DAYOFWEEK, DOW, WEEK) is mapped to the DuckDB value for tsql, bigquery, snowflake "
    "and mysql under each dialect's default settings, and refused in any other dialect with no "
    "exact mapping. A number given to a date function is refused unless T-SQL reads it as days "
    "after 1900-01-01. A function "
    "that reads a file, the environment, a setting, the catalog, a sequence, a random source or the "
    "clock is refused, and so is any function the list does not name. The current time is read "
    "only as CURRENT_DATE, CURRENT_TIMESTAMP or NOW(), which become the registry's asOf.",
)

FLX_3623 = register(
    "FLX-3623",
    "a consumer query or view reads a table function, a string or a path",
    "A table is named by an identifier that resolves to an expose of the bundle or to a view. A "
    "table function (read_csv, read_parquet, glob, range, generate_series), a table written as a "
    "string or as a path, and a stage or a file reference, are refused, since each would read "
    "something that is not a table the bundle declares.",
)

FLX_3624 = register(
    "FLX-3624",
    "a consumer query or view names a table that does not resolve",
    "A table name resolves, in order, through the registry's tables map, through a qualifier in "
    "prefixes that is dropped, as an exposeId, and as a WITH name or a declared view. A name that "
    "matches none of them, or a table qualified by more parts than a rule can resolve, is "
    "refused. The message names the table as written and lists what the bundle exposes.",
)

FLX_3625 = register(
    "FLX-3625",
    "a consumer query or view names a table that resolves to more than one",
    "Unquoted names compare without regard to case and quoted names exactly. A name that matches "
    "two exposes, an expose and a WITH name or view that shadows it, or an expose that differs "
    "from another only in case (DuckDB cannot tell two such tables apart), is refused rather than "
    "guessed. Quote the name, rename the WITH entry, or map the table in tables.",
)

FLX_3626 = register(
    "FLX-3626",
    "the placeholders of a consumer query do not match its parameters",
    "Each placeholder is named (:name, $name, @name or %(name)s, by dialect) and takes the value "
    "the query's parameters give it, as a string, a whole number or a boolean written into the "
    "tree and never into the text. A placeholder with no value, a positional placeholder (?, %s, "
    "$1), a parameter that no placeholder uses, and a placeholder in a view are refused.",
)

FLX_3627 = register(
    "FLX-3627",
    "a consumer query or view is larger than the engine reads",
    "A text holds at most 20,000 tokens and brackets nested at most 64 deep, both counted on "
    "sqlglot's tokens before the parser runs, and its parsed tree at most 10,000 nodes and 64 "
    "levels. A long chain of AND or OR nests one level per term: group the terms in brackets or "
    "use IN. The columns its stars become and the projections its positional GROUP BY keys copy "
    "add at most 100,000 nodes between them, counted before sqlglot writes any of them out: name "
    "the columns instead of repeating a star, and group by the expressions. The message names "
    "the limit that was passed.",
)

FLX_3628 = register(
    "FLX-3628",
    "a consumer query or view nests deeper than sqlglot can read",
    "sqlglot raised a RecursionError while reading or checking the text. The text is refused "
    "with this code rather than letting the error escape. Flatten the nesting of the text.",
)

FLX_3629 = register(
    "FLX-3629",
    "a consumer query or view reads the current time and the registry declares no asOf",
    "CURRENT_DATE, CURRENT_TIMESTAMP, NOW() and their dialect spellings read as the registry's "
    "asOf date, or its midnight, so that a rate never depends on the day it is counted. Declare "
    "asOf in the consumers block, or take the date from a parameter.",
)

FLX_3630 = register(
    "FLX-3630",
    "a consumer query or view limits or numbers rows in an order that is not total",
    "LIMIT, OFFSET and window functions that depend on an order are run only when the ORDER BY "
    "lists a key the contract declares unique for the table it orders, or every column of the rows "
    "it orders, so that the rows a query keeps or numbers never depend on how ties fall. A "
    "window's ORDER BY is read within its PARTITION BY. Add the key to the ORDER BY.",
)

FLX_3631 = register(
    "FLX-3631",
    "consumer views read each other in a cycle",
    "Views become WITH entries in dependency order, so a view may not read itself, directly or "
    "through other views. The message names the views left in the cycle.",
)

FLX_3632 = register(
    "FLX-3632",
    "a consumer query's unit or groupBy does not fit its rows",
    "groupBy must be a column of the rows the query returns, after a star is expanded, and of "
    "its unit: an exposeId gives SELECT * over that expose, and a view gives its own checked "
    "text. Both name a column as the contract does, since a name the registry's columns map lists "
    "is read as the contract's name. The message names the query and the column.",
)

FLX_3633 = register(
    "FLX-3633",
    "a consumer query or view names a column that does not resolve",
    "sqlglot qualified the text against the columns the contract gives each expose and could not "
    "resolve a column, found one that two tables share, or found a star it could not expand. A "
    "column is written as the contract names it, or as the registry's columns map names it. A "
    "column qualified by a schema or a catalog is refused: qualify it by its table or its alias.",
)

FLX_3634 = register(
    "FLX-3634",
    "the canonical text of a consumer query does not pass the checks again",
    "The canonical text is parsed again in the duckdb dialect and passes every check of ADR 0028 "
    "section 2 once more, with the same tables, so that the tree that was checked and the text "
    "DuckDB runs are one statement. A canonical text that fails any of them, or reads other "
    "tables, is refused. The message names the check that failed.",
)

FLX_3635 = register(
    "FLX-3635",
    "the registry and the contract give the SQL check an unusable table or column",
    "The check needs each expose of the bundle with its columns and their SQL types. A logical "
    "type DuckDB cannot hold (a decimal of more than 38 digits), two exposes of one name, two "
    "columns that differ only in case (DuckDB cannot tell them apart), a name in the registry's "
    "columns that stands for two columns or is also the name of another column, or a registry that "
    "names an expose or a column the schema lacks, is refused.",
)

FLX_3636 = register(
    "FLX-3636",
    "the SQL check could not read a consumer query or view",
    "sqlglot or the check raised an error that no other code names while reading, qualifying or "
    "writing the text. The text is refused rather than run on a reading that did not finish. The "
    "message names the error's type. A text that reaches this code is worth reporting with the text "
    "that provoked it.",
)

FLX_3637 = register(
    "FLX-3637",
    "the consumers file given to init cannot be read as a consumers block",
    "fluxsim init --consumers FILE reads a YAML file that holds one mapping, the keys of a "
    "consumers block (dialect, asOf, tables, prefixes, columns, views, queries, limits, "
    "calibration), with the exact YAML reader the engine reads a bundle with. A file that cannot "
    "be opened, is not UTF-8 text or YAML, is not a regular file, passes the reader's bounds, or "
    "holds anything but one mapping is refused, and nothing is written. The block's own keys are "
    "checked by the registry reader, which names them with FLX-3601 to FLX-3617.",
)

FLX_3713 = register(
    "FLX-3713",
    "a consumer query reads a projection alias of its own SELECT",
    "A column that a SELECT writes in a later projection, WHERE, GROUP BY, HAVING or QUALIFY, or "
    "inside an expression of its ORDER BY, is the alias of one of its own projections. Reading an "
    "alias there makes the checker write the alias's expression in its place, and a chain of "
    "aliases that read each other doubles the text at every step, so the text is refused before "
    "it is expanded. Write the expression out. An ORDER BY key that is just an output column's "
    "name (ORDER BY n DESC) is accepted.",
)

FLX_3714 = register(
    "FLX-3714",
    "a consumer query holds a LIKE pattern of more than 512 characters",
    "A LIKE or ILIKE pattern is written into the text that counts the rows meeting a requirement "
    "and is matched at init, so its length is bounded at 512 characters. Shorten the pattern.",
)

FLX_3715 = register(
    "FLX-3715",
    "a consumer query or registry derives more requirements than the engine reads",
    "Each literal a query compares a column with (each member of an IN list, each bound, each "
    "pattern) is one requirement the copy must hold, and each is counted on the run. A query "
    "derives at most 256 requirements and a registry at most 10,000, and past either the registry "
    "is refused before any count is built. The message names the query. Shorten the IN list, or "
    "split the query.",
)
