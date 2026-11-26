# The registry of consumer queries

A bundle declares its consumers' questions in the Simulation's `consumers` key, under the `com.agentics.flux/1` extensions. You write the registry as a file inside the bundle folder or at a path the user names, with the block's keys at the top level, and `fluxsim init --consumers FILE` embeds it in the Simulation. A bundle without the key is read, planned and drawn as before, byte for byte. Kernel 0.1.0 refuses the key (`FLX-3105`, naming the kernel). The page that shows a whole registry is
[check consumer queries](https://github.com/Agenticstiger/flux-engine/blob/v0.12.1/docs/how-to/check-consumer-queries.md);
copy its shape, and do not invent a key. The block is read with closed keys, so a key nothing reads is refused (`FLX-3601`, `FLX-3613`).

## The keys

| Key | Holds |
|---|---|
| `dialect` | the SQL dialect the consumers write in, from a closed list in [the specification](https://github.com/Agenticstiger/flux-engine/blob/v0.12.1/docs/spec/CONSUMER-QUERIES-SPEC.md); `duckdb` when absent |
| `asOf` | the date that `CURRENT_DATE`, `CURRENT_TIMESTAMP` and `NOW()` read as; a query that reads the clock is refused without it (`FLX-3629`) |
| `tables` | each table name the consumers write, mapped to an expose of the bundle |
| `prefixes` | qualifiers dropped from any other table name before it is matched |
| `columns` | per expose, each column name the consumers write, mapped to a column of the contract |
| `views` | named queries other queries read from, checked as queries are |
| `queries` | the questions |
| `limits` | `seconds` and `memory` for one query; a bundle may only lower the engine's ceilings |
| `calibration` | the parameters a fit may move (see the calibration reference of this skill) |

## A query

A query holds `id` and `sql`, and optionally:

- `unit`: the expose or view whose rows the query's rate counts. A query with no unit is checked for rows only.
- `groupBy`: a column of the unit, for a rate per group; it needs a unit.
- `heldOut`: `true` keeps the query out of every fit. Its target comes from a separate profile.
- `tolerance`: an exact decimal from 0 to 1, the largest gap between the run's rate and its target that still meets it. Write it as a string.
- `parameters`: the value text of each placeholder in the SQL. A placeholder with no value, or a value no placeholder uses, is refused (`FLX-3626`).

An `id` is a lowercase letter then lowercase letters, digits, `.`, `-` and `_`, at most 128 characters in all. It names the query in the profile, the request and the scorecard. The ceilings are in the specification (queries, views, SQL size, parameters); stay far below them.

The `limits` ceilings are 30 seconds and 1GiB per query. Raising one for a run takes `--max-query-seconds` (up to 600) or `--max-query-memory` (up to 16GiB) on `verify`, `ship` or `calibrate`, never a bundle value. Ask the user before you pass either one, and never for a bundle you have not read.

## What the SQL may be

Each query and view is parsed with sqlglot in the declared dialect, checked against closed lists, and rewritten before DuckDB sees it. It must be one statement that is a query: a `SELECT`, a set operation of them, or a non-recursive `WITH` before one. It may hold column references, literals, comparisons, boolean logic, arithmetic, `CASE`, `CAST` to a closed list of types, `IN`, `BETWEEN`, `LIKE`, `ILIKE`, `IS NULL`, joins, subqueries, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT` and windows over the listed functions, and functions that only read their arguments.

It may not hold:

- DDL, DML, `PRAGMA`, `SET`, `ATTACH`, `COPY`, `INSTALL`, `LOAD`, `EXPORT`, `CALL`, or anything read as a command;
- a table function (reading a file or a range), a function that reads the environment, a setting, the catalog or a random source, or a table named as a string or a path;
- a table that does not resolve, through `tables`, `prefixes`, `WITH` names and views, to an expose or a view;
- `LIMIT`, `OFFSET` or a window whose `ORDER BY` is not total: list a key the contract declares unique, or every column of the rows ordered (`FLX-3630`);
- a text past the size and nesting bounds.

A refusal names the query and the construct. The remedy is a plainer query that stays inside the lists, written with the user's agreement because the query is the consumers' question. Never widen the list, add a way to read a file, or move a literal out of a placeholder to get past a check.

A query that fits looks like this (the registry maps `sub_plan` and the table name for the consumers):

```sql
SELECT * FROM prod.analytics.subscriptions WHERE sub_plan = 'premium' AND renewed
```

## From a query to what the bundle must hold

Every query, held out or not, is read for what it needs the copy to hold, since a held-out question must return rows too. A predicate gives a requirement: a level (`plan = 'premium'`), a value, a pattern (`LIKE`), nulls (`IS NULL`) or a range (`BETWEEN`, `<`, `>=`). The results page says how `init` and `verify` report them: the results reference of this skill.

## What a profiler is owed

The profile request carries each calibration query's canonical text, its unit and grouping, and the sets of categorical columns the queries read together (a workload, holding no value). A held-out query is never in that list; it goes in a request of its own. The request holds no value of the data. A profiler treats each query as untrusted text and checks it again.
