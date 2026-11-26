# Consumer queries: normative specification

The people who read a dataset ask it questions in SQL. A Simulation declares those questions, the engine
checks them, counts them on a run in a locked DuckDB, compiles what they need the copy to hold, reports
their rates beside the profile's, and can fit declared numbers to them. The design is
[ADR 0028](../adr/0028-consumer-queries-run-locked-compile-requirements-and-calibrate.md). This page fixes
the rules; where it and the ADR differ in a detail, the code and this page are the truth and the
differences are listed in section 11.

A query's text is the consumers' question. Its literals come from the consumers and never from the data,
and nothing on this path prints or logs a value of a run: the child process returns whole numbers, and the
levels of a grouped count.

The words must, must not and may are used as in RFC 2119. Errors are named by their codes; `fluxsim explain
FLX-nnnn` prints each.

## 1. The `consumers` block

The block is `extensions['com.agentics.flux/1'].consumers` of a Simulation, a key no earlier engine reads
(ADR 0018 section 1). A bundle without it reads, plans and draws as before, byte for byte. The block is
read with the exact YAML reader's conventions: closed keys, no float, and bounds. The block changes no
generated row: it is bound into no emit's `H` or `G` and enters `inputDigest` through the Simulation's
bytes, as every key does. Kernel `0.1.0` refuses the key with `FLX-3105`, naming the kernel.

| Key | Holds | Refusal |
|---|---|---|
| `dialect` | A sqlglot dialect from the closed list below. `duckdb` when absent. | `FLX-3602` |
| `asOf` | A calendar date written `YYYY-MM-DD`, as a string. What `CURRENT_DATE`, `CURRENT_TIMESTAMP` and `NOW()` read as. | `FLX-3603`, and `FLX-3629` for a clock read with no `asOf` |
| `tables` | A mapping from the table names consumers write to an `exposeId` of the Simulation. | `FLX-3604` |
| `prefixes` | At most 64 qualifiers that are dropped from a table name before it is matched. | `FLX-3604`, `FLX-3613` |
| `columns` | Per `exposeId`, a mapping from the column names consumers write to columns of the contract. | `FLX-3604` |
| `views` | A list of `{ name, sql }`: named queries that other queries read, checked as queries are. At most 256. | `FLX-3606`, `FLX-3607` |
| `queries` | The questions. At most 1,024. | `FLX-3605` |
| `limits` | `seconds` and `memory` (section 3). | `FLX-3611`, `FLX-3614`, `FLX-3615` |
| `calibration` | The numbers a fit may move (section 8). | `FLX-3612`, `FLX-3616`, `FLX-3617` |
| `compiledWith` | `{ sqlglot: <version> }`, written by `fluxsim init`: the sqlglot version that compiled the requirements it applied (section 5). | `FLX-3601` |

No other key is read, and one that is present is refused (`FLX-3601`). The dialect list is `athena`,
`bigquery`, `clickhouse`, `databricks`, `doris`, `drill`, `duckdb`, `hive`, `mysql`, `oracle`, `postgres`,
`presto`, `redshift`, `snowflake`, `spark`, `spark2`, `sqlite`, `starrocks`, `tableau`, `teradata`, `trino`
and `tsql`.

**A query** holds `id` and `sql`, and optionally `unit`, `groupBy`, `heldOut`, `tolerance` and
`parameters`.

- `id` is a lowercase letter followed by at most 127 lowercase letters, digits, `.`, `-` and `_`. It names
  the query in the profile, the request and the scorecard, and is unique across the queries
  (`FLX-3605`).
- `sql` is a non-empty string of at most 65,536 UTF-8 bytes with no NUL character (`FLX-3607`). A view's
  `sql` follows the same rule, and a view's `name` follows the grammar of an `id` and is not the `exposeId`
  of an emit (`FLX-3606`).
- `unit` is the `exposeId` or view whose rows the query's rate counts. `groupBy` is a column of the unit
  and needs a unit (`FLX-3608`).
- `heldOut` is a boolean, `false` when absent. `tolerance` is an exact decimal from 0 to 1 written as a
  string, the largest gap between the run's rate and its target that still meets it (`FLX-3609`).
- `parameters` maps at most 256 placeholder names, each `[A-Za-z_][A-Za-z0-9_]{0,63}`, to the value of the
  placeholder: a string, a whole number or a boolean. A float is refused (`FLX-3610`).

The calibration queries are the ones with `heldOut: false`. A held-out query takes part in no fit and has
its own targets (section 9).

## 2. The SQL check

Each view and query is parsed with sqlglot in the declared dialect, checked, and rewritten before DuckDB
sees any of it. `validate`, `plan`, `init`, `verify`, `calibrate` and `ship` refuse the bundle, naming the
query and the construct. The check is sqlglot only and runs no SQL; without the `sql` extra the bundle is
not refused and the queries are reported as not checked, with the command that installs the extra.

### 2.1 Bounds and statements

1. **Bounded read.** sqlglot's tokenizer runs first. A text of more than 20,000 tokens, or with brackets
   nested more than 64 deep, is refused before the parser runs (`FLX-3627`).
2. **One statement.** The text holds exactly one statement (`FLX-3619`), and it is a `SELECT`, a set
   operation of them (`UNION`, `INTERSECT`, `EXCEPT`) or `WITH` before one (`FLX-3620`). DDL, DML, `PRAGMA`,
   `SET`, `ATTACH`, `COPY`, `INSTALL`, `LOAD`, `EXPORT`, `CALL` and anything sqlglot reads as a command are
   refused. A text that does not parse, or holds no statement, is `FLX-3618`.
3. **Tree caps.** More than 10,000 expression nodes, or more than 64 levels, is refused (`FLX-3627`). The
   walk is iterative and counts as it goes. A `RecursionError` inside sqlglot is refused with `FLX-3628`
   and never raised, and any other error nobody named is `FLX-3636`.
4. **No alias read in its own `SELECT`.** A column that a later projection, `WHERE`, `GROUP BY`, `HAVING`, or
   an expression of the `ORDER BY` reads, and that is the alias of an earlier projection of the same
   `SELECT`, is refused before sqlglot qualifies the text (`FLX-3713`), because writing the alias's
   expression in its place doubles the tree at each step of a chain. Write the expression out. An `ORDER BY`
   key that is just an output column's name stays accepted, and so does an alias that only repeats its own
   column's name (`plan AS plan`). The check also tells sqlglot not to expand aliases.
5. **Pattern length.** A `LIKE` or `ILIKE` pattern of more than 512 characters is refused (`FLX-3714`),
   whether it is written in the text or bound by a placeholder.
6. **Written-out size.** sqlglot writes each star as the columns of its sources and each positional
   `GROUP BY` key as a copy of the projection it names. Before it does, the check counts the nodes these
   add, scope by scope from the innermost, so that a star over a `WITH` entry or a subquery reads the
   columns that entry returns: three for each column a star becomes, and the nodes of each projection a
   key copies, counted after the stars before it. More than 100,000 between them is refused (`FLX-3627`),
   because the canonical tree may hold no more. Name the columns instead of repeating a star, and group
   by the expressions.

### 2.2 The allowlist

Every node of the parsed tree passes only when its exact class is on a closed list: the test is
`type(node) in list`, never `isinstance`, so a subclass sqlglot adds later is refused until someone lists
it. Each argument a node carries must be on the list of its class, and each scalar argument must hold a
value that list names. A clause nobody listed (`QUALIFY`, a `WINDOW` clause, a frame, `TABLESAMPLE`, a
lock, a hint, `USING`, `NATURAL` and `ASOF` joins, `PIVOT`, time travel, `GROUP BY ALL`, `ROLLUP`, `CUBE`,
`GROUPING SETS`, `DISTINCT ON`, a star modifier, `WITH RECURSIVE`) is refused with `FLX-3621`. There is no
denylist to forget a name from.

The names are those of sqlglot's `exp` module, as `flux_engine/consumers/_allow.py` lists them. A test
compares the two lists below with that module, so this page cannot drift from the code.

**Expression classes** (a node outside this list is `FLX-3621`):

<!-- allowlist: expressions -->
`Select`, `Union`, `Intersect`, `Except`, `Subquery`, `With`, `CTE`, `From`, `Where`, `Group`, `Having`,
`Order`, `Ordered`, `Limit`, `Offset`, `Distinct`, `Join`, `Window`, `Table`, `TableAlias`, `Alias`,
`Column`, `Identifier`, `Star`, `Literal`, `Boolean`, `Null`, `Var`, `Paren`, `Tuple`, `Interval`,
`DataType`, `DataTypeParam`, `And`, `Or`, `Not`, `EQ`, `NEQ`, `GT`, `GTE`, `LT`, `LTE`, `Add`, `Sub`,
`Mul`, `Div`, `Mod`, `IntDiv`, `Neg`, `DPipe`, `In`, `Between`, `Like`, `ILike`, `Is`, `Case`, `If`,
`Coalesce`, `Nullif`, `Cast`, `Exists`
<!-- end allowlist -->

**Function classes**: aggregates (which may also be window functions), the ranking functions (which are
allowed only over a window), and the string, number and date functions that read only their arguments. A
call outside this list is `FLX-3622`:

<!-- allowlist: functions -->
`Count`, `Sum`, `Avg`, `Min`, `Max`, `Stddev`, `StddevPop`, `StddevSamp`, `Variance`, `VariancePop`,
`RowNumber`, `Rank`, `DenseRank`, `Lower`, `Upper`, `Trim`, `Length`, `Substring`, `Concat`, `Replace`,
`Abs`, `Round`, `Floor`, `Ceil`, `Trunc`, `Extract`, `DateTrunc`, `TimestampTrunc`, `DatetimeTrunc`, `DateAdd`,
`DateSub`, `DateDiff`, `TsOrDsAdd`, `TsOrDsDiff`, `TsOrDsToDate`, `TimestampAdd`, `TimestampSub`, `TimestampDiff`,
`DatetimeAdd`, `DatetimeSub`, `DatetimeDiff`, `Year`, `Month`, `Day`, `Quarter`, `Week`, `WeekOfYear`,
`DayOfWeek`, `DayOfMonth`, `DayOfYear`, `Hour`, `Minute`, `Second`
<!-- end allowlist -->

Two of these classes are ones sqlglot writes itself when it reads a dialect's own spelling, so a legal
query reaches them without naming them: `TsOrDsToDate` for `YEAR(x)`, `MONTH(x)` and `DAY(x)` in MySQL,
T-SQL, Spark, Hive and Databricks, and `Trunc` for integer division in PostgreSQL, Redshift, T-SQL, SQLite,
Trino and Drill. Their arguments are checked as closely as their neighbours': `TsOrDsToDate` carries its
operand and, from T-SQL, the one default date `'1900-01-01'`; `Trunc` carries its operand and a numeric
literal digit count. The operand is a column, a literal or a scalar expression of them, never a query, a
table, a table function or a function that is not listed; any other argument is refused. The depth and node
caps are unchanged.

A weekday or week part is mapped, before the allow-list reads the tree, to the DuckDB expression that has the
value the dialect gives it under the dialect's documented default settings. The mapping is built only from
`DayOfWeek`, `DayOfYear`, `WeekOfYear`, `DateTrunc`, `Cast`, `Interval` and the arithmetic operators, all on the
lists above, so it widens nothing. The settings read are `DATEFIRST` 7 for T-SQL, `default_week_format` 0 for
MySQL, and `WEEK_START` 0 with `WEEK_OF_YEAR_POLICY` 0 for Snowflake; a query run against a session that
changes them counts differently from the engine.

| Dialect | Part | Value |
| --- | --- | --- |
| T-SQL | `DATEPART(weekday, x)`, `dw` | Sunday 1 to Saturday 7 |
| T-SQL | `DATEPART(week, x)`, `wk`, `ww` | week 1 holds 1 January, weeks start on Sunday |
| T-SQL | `DATEPART(iso_week, x)` | ISO 8601 week |
| BigQuery | `EXTRACT(DAYOFWEEK FROM x)` | Sunday 1 to Saturday 7 |
| BigQuery | `EXTRACT(WEEK FROM x)`, `WEEK(<day>)` | weeks start on the day (Sunday by default); days before the year's first such day are week 0 |
| BigQuery | `EXTRACT(ISOWEEK FROM x)` | ISO 8601 week |
| BigQuery | `DATE_TRUNC(x, WEEK)`, `WEEK(<day>)`, `ISOWEEK` | the date on or before `x` that starts the week, as a date |
| Snowflake | `DAYOFWEEK`, `DAYOFWEEKISO`, `WEEK`, `WEEKOFYEAR`, `WEEKISO`, `DATE_TRUNC('week', x)` | Sunday 0 to Saturday 6; Monday 1 to Sunday 7; ISO 8601 week; the Monday on or before `x` |
| MySQL | `DAYOFWEEK(x)` | Sunday 1 to Saturday 7 |
| MySQL | `WEEK(x)`, `WEEK(x, 0)`, `WEEK(x, 3)`, `WEEKOFYEAR(x)` | mode 0 (Sunday weeks, week 0 before the first Sunday); ISO 8601 week |

Every other weekday or week part (T-SQL `DATETRUNC(week, x)`, the part `w`, which the parser reads as the week,
BigQuery `TIMESTAMP_TRUNC` and `DATETIME_TRUNC` to a week, MySQL `WEEK` in any other mode, and any part in another
dialect other than `duckdb`) is refused (`FLX-3622`), and the message names the dialect and the part. The
`duckdb` dialect keeps its own numbering. The T-SQL date parts that the query parser reads differently from T-SQL
(`w` and `y` in every date function, `m` in `DATETRUNC`, `dy` in `DATENAME`) are refused with `FLX-3622` however
they are quoted or commented. A rewrite repeats its operand, so the operand holds at most 200 nodes
(`FLX-3627` beyond that).

An integer given to a date function means that many days after 1900-01-01 in T-SQL, so `YEAR(0)` and
`DATEPART(year, 0)` are 1900. The engine writes that date for a whole-number literal from -53690 to 2958463, the
range of the T-SQL `datetime`. A number given to a date function in any other dialect, a T-SQL number outside
that range, and a decimal one are refused when the query is compiled (`FLX-3622`), not when it runs. An integer
column given to a date function is not rewritten and fails when it runs, and that run is refused.

T-SQL `DATEDIFF(unit, x, GETDATE())` reads the clock as `CURRENT_TIMESTAMP` does: it becomes the `asOf` literal at
midnight, and is refused without `asOf` (`FLX-3629`). Snowflake `LEN` and `LENGTH` are the length in characters of
the text.

A function that reads a file, the environment, a setting, the catalog, a sequence, a random source or the
clock is refused, and so is a table function (`read_csv`, `read_parquet`, `glob`, `range`,
`generate_series`; `FLX-3623`).

**Types a `CAST` may name**:

<!-- allowlist: cast-types -->
`BIGINT`, `BOOLEAN`, `CHAR`, `DATE`, `DECIMAL`, `INT`, `NCHAR`, `NVARCHAR`, `SMALLINT`, `TEXT`,
`TIMESTAMP`, `TIMESTAMPNTZ`, `TINYINT`, `VARCHAR`
<!-- end allowlist -->

**Date parts** that `EXTRACT`, `DATE_TRUNC` and `INTERVAL` may name:

<!-- allowlist: date-parts -->
`DAY`, `DAYOFWEEK`, `DAYOFYEAR`, `DAYS`, `DOW`, `DOY`, `HOUR`, `HOURS`, `MINUTE`, `MINUTES`, `MONTH`,
`MONTHS`, `QUARTER`, `QUARTERS`, `SECOND`, `SECONDS`, `WEEK`, `WEEKS`, `YEAR`, `YEARS`
<!-- end allowlist -->

Other rules of the list:

- A `JOIN` carries `ON` and a side of `LEFT`, `RIGHT` or `FULL` or a kind of `INNER`, `OUTER` or `CROSS`.
- A `Window` holds an aggregate or a ranking function, `PARTITION BY` and `ORDER BY`, and no `DISTINCT`
  and no frame. A ranking function without `OVER` is refused.
- `LIMIT` and `OFFSET` take a whole number or a placeholder that is bound to one.
- A table is a name of at most three parts. A table given as a string, as a path or as a function is
  `FLX-3623`.

### 2.3 Placeholders and the clock

A placeholder (`:name`, `$name`, `@name` or `%(name)s`, by dialect) takes the value of the query's
`parameters` of that name. Each becomes a typed literal in the tree, never text spliced into SQL: a string,
a whole number, an exact decimal, a date, a timestamp or a boolean, from the value's spelling and the column
it is compared with. A placeholder with no value, a positional placeholder (`?`, `%s`, `$1`), a parameter
that no placeholder uses, and a placeholder in a view are refused (`FLX-3626`).

`CURRENT_DATE`, `CURRENT_TIMESTAMP`, `NOW()` and their dialect spellings are replaced by the `asOf` literal,
at midnight for a timestamp, so a rate never depends on the day it is counted. A query that reads the clock
when the block declares no `asOf` is refused (`FLX-3629`).

### 2.4 Tables and columns

A table name of up to three parts resolves, in this order, by the first rule that matches:

1. the `tables` map, matched on all its parts;
2. a qualifier listed in `prefixes` is dropped, and the rest is matched against `tables` and then as an
   `exposeId`;
3. a name of one part, as an `exposeId`;
4. a name of one part, as a `WITH` entry the text defines before it, or a declared view.

Unquoted parts compare without regard to case (folded with `str.lower`, as sqlglot folds them) and quoted
parts exactly. A name that matches nothing, or has more than three parts, is `FLX-3624`. A name that
matches two exposes, an expose that a `WITH` entry or a view would shadow, and an expose that has a twin
differing only in case (DuckDB cannot tell them apart, quoted or not) are `FLX-3625`: the check refuses
rather than guesses. Views become `WITH` entries in dependency order ahead of the text's own; a cycle is
`FLX-3631`, and a text that with its views holds more than 100,000 nodes is `FLX-3627`.

The `columns` map gives, per expose, the names consumers write. A name it lists is read as the contract's
name wherever a column name is written, so the canonical text and every star are the contract's. A listed
name stands for one column and is not the name of another (`FLX-3635`). sqlglot then qualifies the tree
against the columns of the exposes and expands stars; a column that does not resolve, is shared by two
tables, or is qualified by a schema or a catalog is `FLX-3633`. The schema the check reads is the exposes of
the bundle with their SQL types; a type DuckDB cannot hold, two exposes of one name, and two columns that
differ only in case are `FLX-3635`.

`unit` and `groupBy` are checked against the rows: an `exposeId` gives `SELECT *` over that expose, a view
gives its own checked text, and `groupBy` is a column of the query's rows and of its unit (`FLX-3632`).

### 2.5 Total orders

`LIMIT`, `OFFSET` and a window function whose result depends on an order are accepted only when the order
is total, so the rows a query keeps or numbers never depend on how ties fall (`FLX-3630`). An order is total
when it lists a key the contract declares unique for each table it orders (every table of the `FROM` and
its joins), or every column of the rows it orders, or every expression the query groups by. A query that
aggregates with no `GROUP BY` returns one row and needs no order. A window's `ORDER BY` is read within its
`PARTITION BY`.

### 2.6 The rewrite and the canonical text

The rewrite works on the parsed tree, never on the text: table names become `exposeId`s, columns take their
contract names, views become `WITH` entries, placeholders become typed literals, and the clock becomes the
`asOf` literal. sqlglot then writes the tree as one DuckDB text with its comments dropped
(`sql(dialect="duckdb", comments=False)`): the query's **canonical text**.

The canonical text is parsed again in the `duckdb` dialect and passes every check of this section once
more, with larger caps for a text whose stars are expanded and whose names are qualified (200,000 tokens,
brackets nested 96 deep, 100,000 nodes, 96 levels), and with no placeholder, clock or unknown function left.
It must read the tables the first tree read. The tree returned is that second tree, so the tree that was
checked and the text DuckDB runs are one statement. A canonical text that fails any check is `FLX-3634`.

The query's digest is `sha256:` and the lowercase hex of the UTF-8 canonical text. The scorecard and the
profile request carry it. Every requirement and every rate is recorded with the sqlglot and DuckDB versions
that produced it, and one made under other versions is compared only as `versionsDiffer`, never silently.

## 3. The runner

`verify`, `ship` and each calibration start one child process and stop it when they are done. The parent
never imports DuckDB. The libraries are the `sql` extra: `sqlglot==30.33.0`, `duckdb>=2.0.1,<2.1` and
`pyarrow>=18`; without them the queries are not measured (`FLX-7131`).

**Its own environment.** The child is `python -I -m flux_engine.consumers._child`. It is started with an
environment of its own that holds `PATH=/usr/bin:/bin`, `LC_ALL=C.UTF-8` and `TZ=UTC` and nothing else, no
standard error, and no file descriptor but its two pipes. No variable or credential of the parent reaches
it. Its first reply lists the names, never the values, of the environment it sees.

**Limits first.** Before it imports DuckDB the child lowers its own resource limits where the platform
enforces them: `RLIMIT_FSIZE` to zero, `RLIMIT_CPU` to its time budget plus a margin of 30 seconds, and
`RLIMIT_AS` to its memory budget plus 4 GiB for the interpreter and the tables. After its imports and the
connection it lowers `RLIMIT_NOFILE` to the highest descriptor it holds plus 8, so it can open no new file.
The limits it set are in its first reply.

**Locked at connect.** It opens an in-memory DuckDB with these settings, all passed when the connection
opens: `enable_external_access=false`, `autoload_known_extensions=false`,
`autoinstall_known_extensions=false`, `allow_community_extensions=false`, `allow_unsigned_extensions=false`,
`allow_persistent_secrets=false`, `python_enable_replacements=false`, `threads=1`, `memory_limit` from
`limits.memory`, `max_temp_directory_size='0B'` and `lock_configuration=true`.

**Data in, counts out.** The parent reads the run's emitted streams, the only files this path reads,
without following a link. Each stream is digested as `outputDigest` is and compared with the receipt's
before a count is taken (`FLX-3703`), and read as one row per line (`FLX-7135`). The parent builds each
table with the contract's types and sends it as an Arrow IPC stream, as framed bytes and never a pickle. A
header may hold 4 MiB and a table 1 GiB (`FLX-7132`). The child registers the tables and opens no file.
It runs only the counts the engine writes around a canonical text: `SELECT count(*) FROM (<query>)`, and the
count of the unit, grouped when the query declares `groupBy` (at most 100,000 groups; more is refused, not
truncated). Before it runs one, it splits the whole text with DuckDB's `extract_statements` and runs it
only when that finds exactly one statement and it is a `SELECT`. It returns whole numbers. DuckDB's own
message is never sent, since it can quote a value.

**Many runs, one process.** A calibration counts the rows of many runs in one process: `clear` drops every table
and the next candidate's tables are sent.

**Time.** A timer calls `interrupt()` on the connection when a query passes `limits.seconds`. The parent
keeps a clock of its own for the whole batch, the sum of the limits plus 30 seconds, and ends the child
(`terminate`, then `kill`) when it runs out, raising `FLX-7133`. The child has 30 seconds to start
(`FLX-7134`).

**Statuses of a count.** Each count takes one status:

| Status | Means |
|---|---|
| `counted` | DuckDB returned the counts. |
| `timedOut` | The query passed `limits.seconds` and was interrupted. The others still run. |
| `outOfMemory` | The query passed `memory_limit`. The others still run. |
| `error` | DuckDB refused the text at execution, or the child refused it before it ran. The reason holds the engine's exception class and a sentence of the engine's own, never DuckDB's message. The process keeps running. |
| `crashed` | The process died (a signal, a limit, a fault inside DuckDB) while the query was in flight. The query is not run again. A fresh child under the same locks, with the tables sent again, runs the queries that remain; one that cannot start leaves the rest `crashed`. |

**Ceilings.** The engine's ceilings are 30 seconds and `1GiB` for a query, and 64 evaluations for a fit. A
bundle may only lower them: `limits.seconds` above 30 is `FLX-3614`, `limits.memory` above 1GiB is
`FLX-3615` and `calibration.evaluations` above 64 is `FLX-3616`, in every command. Raising a ceiling takes a
flag on the command that runs the queries, never a bundle value, so a bundle from someone else cannot claim
the machine: `--max-query-seconds` (up to 600) and `--max-query-memory` (up to `16GiB`, such as `4GiB`) on
`verify`, `ship` and `calibrate`, and `--max-evaluations` (up to 4,096) on `calibrate --fit`. A flag raises
the ceiling for a bundle that declares no value of its own; a bundle's own value is kept. A flag outside its
range is `FLX-3700`, or `FLX-3704` for the evaluations.

**Defense in depth, not a sandbox.** The settings and limits above narrow what the text can do. They are
not an operating-system sandbox, and DuckDB's own guidance says so. A bundle from someone else runs inside
a container.

## 4. Rates and statuses

For a query with a `unit`, `rows` is the number of rows the query returns and `unitRows` the number of rows
of its unit. Its rate is `rows / unitRows`, an exact fraction, so two runs that count the same rows state
the same rate to the last digit and no float is involved. A profile states the same rate in its `queries`
entry of the same name: `k` is the unit's records and the `true` count the query's rows. With `groupBy` the
rate is taken per group of the unit's column, the null group first and then the levels by code point.

A query is `measured` when the unit has rows and the query returned no more than the unit holds. It is
`noRows` when the unit has no rows and the query returned none, and `notARate` when `rows` pass `unitRows`
(an empty unit included): it is no share of the unit and is never compared. A group the query has no rows for
has `rows` 0, and a group only the query holds has `unitRows` 0 and so is `notARate`.

A query without a unit has no rate. It is `measured` when it returned rows and `noRows` when it returned
none.

The status a query takes on the scorecard is one of:

| Status | Means |
|---|---|
| `measured`, `noRows`, `notARate` | As above. |
| `noTarget` | The profile holds no rate for the query. |
| `withheld` | The profile withheld the cell its rate would come from. |
| `timedOut`, `outOfMemory`, `error`, `crashed` | The runner's statuses (section 3). |
| `versionsDiffer` | The bundle recorded another sqlglot version than the one running (`compiledWith`), so the query is never compared. |

A target is the `true` count over `k` of the entry of the query's name in a profile. A tolerance is the
query's own `tolerance`, or for a held-out query the block's `calibration.heldOutTolerance`. A query with no
tolerance is compared with no bound and is in no share. The gap is the rate less the target, exact and
signed.

## 5. Requirements

A query also says what the copy must contain: a filter on `plan = 'premium'` needs the level `premium`.
Every query, calibration and held-out alike, is read for its requirements, since a held-out query must
return rows too. Only its text is read; no rate takes part. sqlglot's scope helpers resolve each column to
its table through aliases, `WITH` entries, views and derived tables, and a predicate between one resolved
column and literals gives one requirement:

| Predicate | On | Requirement (`kind`) |
|---|---|---|
| `c = 'x'`, `'x' = c`, `c IN ('x', ...)` | a string or boolean column | `level`, one per literal |
| `c = v`, `c IN (v, ...)` | a number, date or timestamp column | `value`, one per literal |
| `c LIKE p`, `c ILIKE p` | a string column | `pattern` |
| `c IS NULL` | any column | `nulls` |
| `c BETWEEN a AND b`, `<`, `<=`, `>`, `>=` | a number, date or timestamp column | `range` |

A predicate under `NOT` (including `IS NOT NULL` and `NOT IN`) asks for rows that lack something and is no
requirement. A query derives at most 256 requirements and a registry at most 10,000; past either the
registry is refused with `FLX-3715`, naming the query, before any count is built.

`fluxsim init --consumers FILE` embeds the registry in the Simulation, records the sqlglot version in
`compiledWith`, and applies what an existing declaration can carry:

- a level on a categorical column whose levels come from the profile becomes a declared level of that
  column, so ADR 0014's rule gives it its published count when the histogram lists it and otherwise a share
  of the remainder, never zero; its entry in the register names the query as its reason;
- a level outside a contract's `enum(...)` is unmet, since no row may hold it;
- a pattern is met on a categorical column when one of its levels matches; on an identifier or text column
  it is left to `verify`;
- a value is met when a declaration produces it, such as a link's sentinel or the end of an open version,
  and otherwise is unmet, with the declarations that would produce it;
- nulls are met when the column draws nulls, from a declared null fraction or a published null count above
  zero;
- a range is met when the column's declared range, or the run's time window, meets it.

`init` prints each requirement as `met`, `unmet` or left to `verify`, and refuses nothing for an unmet one.
`verify` derives the requirements again from the bundle and counts the rows that meet each on the run, with
the same runner. A requirement's result is `met`, `unmet`, `versionsDiffer` (compiled under another sqlglot
version than the one that checks it), or the status the count took when it did not finish. An unmet
requirement is reported and fails nothing.

## 6. What the profile request carries, and what a profiler owes

`fluxsim init --consumers FILE`, given a contract and no profile, writes request format 1-1-0
([PROFILE-REQUEST-SPEC.md](./PROFILE-REQUEST-SPEC.md) section 6):

- **`queries`**: one entry `{ name, sql, unit, groupBy }` per calibration query that has a unit. `sql` is the
  canonical text over `exposeId`s, and `unit` is `{ table }` or, for a view, `{ sql }`. A held-out query is
  never in this list.
- **`tables.<exposeId>.workload`**: a list of `{ columns, weight }`. Each entry is a set of 1 to 8
  categorical columns of the table that calibration queries read together in their predicates and
  groupings, and `weight` is the number of calibration queries that read exactly that set. The workload
  holds no value. A private release may weigh the marginals it selects by it.
- **The held-out request**: when the registry holds held-out queries, `init` also writes
  `<directory>.held-out-request.yml`, a request of the same format that lists only their `queries` entries,
  with each unit's table under `tables` and no column. Its answer is a separate profile document, the
  held-out profile, which only `verify` and `calibrate` read and which no bundle pins.

Neither request is an input of `inputDigest` (ADR 0006).

**What a profiler owes.** For each entry of `queries`, and for each entry of the held-out request, the
profiler writes into the profile's `queries` section, under the same `name`, a `levelHistogram/1` over
`["false", "true"]`: `k` is the number of records of the unit and the `true` count is the number of rows
the query returns. With `groupBy` the statistic is grouped by that column (profile format 1-3-0), its groups
keyed by the column's levels, and the disclosure rules apply group by group as they do for a column's
statistic.

Each entry is untrusted text. A profiler must check it again under section 2 and run it under the locks of
section 3 or stronger ones. Its disclosure checks must look at the set of queries as a whole and not one
query at a time, because two queries that differ by one literal can reveal one record by differencing. The
engine's part ends with the request; the profiler implements the checks.

## 7. The scorecard's `queries` dimension

`verify` on a bundle that declares `consumers` counts every query on the run and writes the `queries`
dimension in scorecard format 1-4-0. The member, its rows, its statuses, its targets and its shares are
fixed by [SCORECARD-SPEC.md](./SCORECARD-SPEC.md) section 3a. In outline:

- per query: `name`, `set` (`calibration` or `heldOut`), `digest`, `rows`, `unitRows`, `rate`, `target`,
  `gap`, `tolerance`, `status`, `met` and `reason`, and one row per group for a query with `groupBy`;
- targets: a calibration query's from the profile the bundle pins, a held-out query's from
  `verify --held-out FILE`;
- the requirements of section 5, `met` or `unmet`;
- two shares, of the calibration queries and of the held-out queries with a target and a tolerance that fall
  within it. The dimension's score is the smaller of the shares that exist, and it is `not measured` when
  neither does;
- the duckdb, sqlglot and pyarrow versions, and the grade of the pinned profile beside the held-out
  profile's (ADR 0020, rule 2).

The pinned profile must not name a held-out query (`FLX-3701`), and the held-out profile must not name a
calibration query, nor be given to `verify` or `ship` when the bundle declares no held-out query (`FLX-3702`).
Either is refused before anything runs, naming the query. `calibrate --fit` on a bundle that declares no
held-out query runs without `--held-out` and does not read one (section 8). Without the `sql` extra the dimension is `not measured`, with the
command that installs it, and a threshold declared on it then fails as any unmet threshold does. `ship`
counts the queries with the same functions and flags. A `ship` that reads a held-out profile (`--held-out`)
grades it as it grades the pinned one, states the weakest grade of every profile it read in the "Privacy
Grade" section of `EVIDENCE.md`, whether or not forecasts were scored, and holds each labelled column's
minimum grade to that weakest grade: a ship whose held-out profile is below a column's minimum is refused
(`FLX-4005`), naming the held-out profile, and writes nothing.

## 8. The calibration patch

`fluxsim calibrate --fit BUNDLE --profile PROFILE --held-out FILE --out DIR` fits the numbers the
`calibration` block names to the rates of the calibration queries. On a bundle that declares no held-out
query, `--held-out` is not needed and the file is not read: the fit runs, and its output says that no held-out
check was made. It needs the `calibrate` extra, which adds
`cma==4.6.0` to the `sql` extra (`FLX-3708`).

**The block.** `calibration` holds at most 64 `parameters`, and `evaluations`, `scale`, `heldOutTolerance` and
`seed` (`FLX-3612`).

| Key | Holds |
|---|---|
| `parameters[].document` | The file name of a document of the bundle's closed set (ADR 0006): a top-level `*.flux.yml`, `*.flux.yaml`, `*.fluid.yml` or `*.fluid.yaml` file of the bundle directory (`FLX-3617`). |
| `parameters[].pointer` | An RFC 6901 JSON Pointer to one number in that document. |
| `parameters[].min`, `max` | Exact decimals, `min` below `max`. |
| `parameters[].step` | An exact decimal above zero. A candidate is rounded to the grid of the step, anchored at zero, and kept in range. |
| `evaluations` | From 1 to the ceiling in force (64 unless `--max-evaluations` raised it). When the block gives none, a fit runs to the ceiling in force. |
| `scale` | From above 0 to 1: the scale each evaluation runs at (1 when absent). |
| `heldOutTolerance` | An exact decimal from 0 to 1, the tolerance of a held-out query that has none of its own. |
| `seed` | A whole number. The Simulation's seed when absent. |

**What may move.** Each parameter names a number the parameter register lists with the source `assumption` or
`default`. A value the profile or the contract states, or one an earlier fit already calibrated, is refused
(`FLX-3705`). A file outside the closed set, a file that holds more than one document, a pointer that reaches
no number the register counts, a value outside `min` and `max`, a step with no value between them, or two
parameters that name one number is `FLX-3706`.

**The fit.** CMA-ES runs over the parameters scaled to the range 0 to 1, bounded, seeded with `seed` (moved
into 1 to 2,147,483,647, since cma reads 0 as "from the clock"), for at most `evaluations` runs, and the start point is evaluated as well. Each
evaluation writes the candidate values, each rounded to its `step`, into a copy of the bundle's files, plans
and applies it at `scale` under the state directory, loads the run's tables into the one query process,
counts the calibration queries, and removes the copy and its run. The objective is the sum of the squared
gaps between each calibration query's rate and its target in the pinned profile, an exact fraction. A target
a private release states with noise (profile format 1-5-0) is its point estimate. A query whose rate stands
inside the noise has no target and is left out, with a note: its unit's records, the sum of its two cells, are
at most twice `noise_floor` over every cell of its statistic, zero and below included, since a sum that noise
alone made passes that only when one of its cells passes the floor. A profile whose rates all stand inside it
gives `FLX-3707` naming them. Each noisy query's squared gaps weigh the inverse of its rate's variance (the
delta method's `v (f^2 + t^2) / n^4`, the mean over its groups for a grouped one), scaled so the least noisy
weighs 1, as an exact rate does. The best
evaluation wins, and of equals the earlier one. A run that was refused stops the fit (`FLX-7101`). The fit
needs a bundle with consumers, at least one parameter, and a pinned profile that states the rate of at least
one calibration query (`FLX-3707`); a bundle compiled under another sqlglot version than the one installed
is refused (`FLX-3709`).

**Determinism.** Every evaluation, the held-out check included, seeds its rows from the unpatched bundle's
per-emit `H` and not from the candidate's own, so candidates are compared on the same draws. The optimizer's draws come from cma's seeded generator and not from the seed tree, so no seed path is
added. The same engine, cma version and platform give the same patch. Another platform may find another, so
the patch is the record, not the search.

**The patch document** is JSON, written with sorted keys, a two-space indent and a final newline. Its fields:

| Field | Holds |
|---|---|
| `method` | `flux.calibration.cma/1`. |
| `versions` | `{ cma, duckdb, sqlglot }`, the versions that fitted. |
| `seed` | The seed the fit used. |
| `evaluations` | The number of runs made. |
| `scale` | The scale as the block wrote it. |
| `profile` | The `sha256:` digest of the pinned profile that held the targets. |
| `gaps` | One `{ query, gap }` per calibration query, of the best evaluation: the exact gap, written as a decimal or a fraction such as `-1/75`, or `null` when the profile states no rate for it. |
| `operations` | RFC 6902 `replace` operations, one per moved value, in the order of the parameters: `{ document, op: "replace", path, value }`. `path` is the pointer, and `value` is the new number as exact decimal text. |

A `replace` needs its target to exist, so a patch moves a declared number and adds nothing. The patch's
digest is the `raw-bytes` digest of the bytes that are written. A patch is not an input of a run
(ADR 0006): it is applied once, to make a bundle of its own.

**What it writes.** The patch is always written, as `DIR.patch.json`, before the held-out profile is opened.
When the held-out check holds (section 9) the fit also writes a new bundle at `DIR`: the input bundle with
the operations applied, written and checked as `fluxsim init` writes a bundle. Each value is spliced into
the text of its document and every other byte stays, comments, quote styles and key order included; a document
that uses an alias or a merge key is refused (`FLX-3711`). The input bundle is never changed. Neither `DIR` nor
`DIR.patch.json` may exist, `DIR` may not be the input or inside it, and its parent must exist (`FLX-3710`),
all checked before the fit starts.

**The register.** In the new bundle each moved value's entry has the source `calibrated` and a `calibration`
member (parameter register format 1-3-0, [PARAMETER-REGISTER-SPEC.md](./PARAMETER-REGISTER-SPEC.md) section 8):
the queries it was fitted to, the value `before` and `after`, the `patch` digest, the `targets` digest of the
profile that held the targets, the `heldOut` record (the held-out profile's digest, its largest gap and the
tolerance it met) and the cma, sqlglot and DuckDB versions.

The fit implements the engine's `Calibrator` protocol under the method id `flux.calibration.cma/1`. Declared
calibration, `fluxsim calibrate BUNDLE`, is unchanged and needs none of the extras.

## 9. The held-out gate

"Calibrated" waits on the held-out queries.

1. The fit reads the pinned profile only. The patch is written, and its digest taken, before the held-out
   profile is opened. The held-out profile's file name is checked before the fit and its queries are not read
   until the patch exists.
2. The held-out profile is graded under ADR 0020 rule 2, and the report states its grade beside the pinned
   profile's.
3. The calibrated values run at `scale`, and the held-out queries are counted. Each held-out query that has a
   target is set against its own `tolerance`, or else `heldOutTolerance`. A query is **within** when it was
   measured, it has a tolerance, and its gap, each group's for a query with `groupBy`, is no larger than the
   tolerance in size.
4. A calibration is **calibrated** only when at least one held-out query has a target and every held-out
   query with a target is within. A held-out query that could not be counted, or that has no tolerance to be
   held to, is a miss. A fit with no held-out target is never called calibrated.
5. Only a calibrated fit writes the bundle at `DIR`. Otherwise `calibrate` writes the patch and its report,
   names each held-out query that missed with its gap and why, writes no bundle, and exits with status 1
   (`FLX-3712`). A fit where no candidate beat the start point moves no value and is refused the same way.

The register records the largest gap rounded up to 12 decimals when it does not terminate, so it never
understates it.

Grades: a calibrated bundle pins the profile that held its targets, so ADR 0020 rule 2 grades it from that
profile, and a profile whose query rates are published exactly is P1 by it. The patch is a declaration and
enters no grade. `ship` states the weakest grade of every profile the run read, the held-out profile included, and holds the
column minimums to it.

## 10. `conditions`

A Simulation may declare `conditions` in the same namespace, a key no earlier engine reads, to fix values in
groups of rows and draw the rest of each row given them. The reader is `flux_engine.seam.conditions`.

```yaml
conditions:
  subscriptions:
    - values: { plan: premium, region: EU }
      count: 400
    - values: { plan: basic }
      count: 600
```

**The declaration.** The key maps an emitted `exposeId` to a non-empty list of at most 100,000 groups. A
group holds `values`, a non-empty mapping of column names to the text of a level (a number is written as
text, `'2.50'`), and `count`, a whole number of at least 1 (`FLX-3640`, `FLX-3641`). A group without a
`count` is allowed only when it is the emit's only group and then covers every row. A seed row is a group
with a count of 1.

**Rows.** Groups take their rows in the order listed, so a row's group is a function of its ordinal, and
the emit's rows are exactly the groups' rows: a declared row count that is not the sum of the counts is
refused (`FLX-3641`). Each group's rows hold its values, and the rest of each row is drawn given them.

**Which columns.** A name that is not a column of the expose, and a value that is not one of the column's
levels, is refused before any row is drawn (`FLX-3642`). A fixed column must be categorical and drawn on its
own, from its marginal or given other columns, through a junction tree (ADR 0021) or a Chow-Liu forest
(ADR 0014). A column drawn from `bins` with no `nullFraction` is not fixed by a group, but it is a variable of
the elimination below, its states its bins named by the text of each lower edge (SEAM-FIDELITY-SPEC.md section 2),
so a group that fixes a categorical column drawn given a bin changes the weights of the bins to their posterior.
A key or foreign key, an identifier, a column a rule sets (a copied column included), a column
a conditional link writes, a column drawn given a column of a linked row, a column that declares a
`nullFraction`, and a column that is not categorical are refused (`FLX-3643`). So are conditions on an emit
that declares a history and on a projected emit (a snapshot, events or periods of a history), whose rows the
history lays out, and on an emit where a column that declares `allocation: exact` is fixed by a group, is
drawn given a fixed column, or is one a fixed column is drawn given: such a column takes its class from its
row's slot, so the group would not hold (`FLX-3643`, naming the emit and the column). The emit must declare a
fidelity block, so a run's own events and a reference expose cannot be conditioned (`FLX-3644`). A column
whose FLUID `sensitivity` sets a minimum grade of P3 or stronger is refused (`FLX-3645`): a fixed value is a
declaration, and a grade never sees a declaration.

**Exactly.** In a clique with separator `S` and new columns `n_1` to `n_m`, in declared order, `n_j` is drawn
given `S` and `n_1` to `n_(j-1)`. For a group's values every conditional has its cells that disagree with
them set to zero. Each clique sends its parent a message over its separator: the sum, over all its new
columns (a fixed one only at its value), of the product of their conditionals and its children's messages.
The weight of a value `v` of `n_j` is its conditional given `S` and `n_1` to `n_(j-1)`, times the sum, over
the later new columns, of the product of their conditionals and the child cliques' messages. A fixed column
is set, not drawn. A Chow-Liu forest is conditioned the same way, each pair a clique. The arithmetic is
rational, so the draw is exact given the fixed values: there is no rejection and no approximation. Values
that leave no cell above zero are refused at `plan`, naming the group (`FLX-3646`), and a group that cannot
be summed exactly from the emit's conditionals is `FLX-3647`.

**The table a uniform is searched in.** A column's weights, exact fractions, are made shares. Each threshold
is the smallest whole number not below a cumulative share times `2**53`, as kernel 0.2.0 builds every table,
the last threshold `2**53`, and the cell's 53-bit uniform is searched in them as kernel 0.2.0 searches every
table. The per-row and the column path build the same thresholds.

**Draws.** A drawn cell of a conditioned row draws on its own path, at the index every cell of its column
draws at, and the values only choose the table of weights its uniform is searched in, as KERNEL-SPEC-2
section 12 says of `given`. A fixed cell draws nothing. A cell of a column drawn from bins chooses its bin from
the group's table with its first word, and the place inside the bin is the histogram's own draw at the index it
always has, the bin's pick left unread, on the per-row path and the column path alike. No seed path is added, and a bundle without the key
has the bytes it had. The groups are bound into the emit's `H` and not its `G`. Conditioned rows are kernel
0.2.0 only: kernel `0.1.0` refuses the key with `FLX-3105`, naming the kernel.

**Scale and shards.** `--scale` scales each group's count as the emit's rows are scaled, by the largest
remainder over the groups, so the counts add up to the scaled total. A shard takes the rows of its ordinals,
and with them their groups. Children follow their parents as they do today.

**Seed rows are declared values.** The engine cannot tell whether a value was copied from a record of the
data, so keeping ADR 0003's rule is the author's part.

**Evidence.** `EVIDENCE.md` names each conditioned emit under "What Was Not Proven", with each group's row
count and the names of the columns it fixes, and states that the fixed values are declarations of the bundle
that no statistic and no privacy grade covers. It never prints a fixed value.

## 11. Where the code is more exact than the ADR

- The child's environment also holds `TZ=UTC`, so a rate never depends on the machine's time zone.
- The block also reads `compiledWith`, and a query is `versionsDiffer` when it does not match.
- Statuses of a count include `error`, and a query's scorecard statuses add `versionsDiffer`.
- Conditions add `FLX-3647` for a group that cannot be summed exactly.
