# ADR 0028: consumer queries are declared in the bundle, run in a locked DuckDB, compile into requirements, and fit a calibration patch

## Status

Accepted 2026-11-22: the maintainers accepted the design, and the changes its review asked for are folded
in. Builds on
[ADR 0003](./0003-sdv-bsl-and-aggregate-only-rule.md) (the engine reads aggregates and never a record),
[ADR 0006](./0006-input-digest-is-a-closed-set.md) (the closed set of input files),
[ADR 0014](./0014-init-reads-every-statistic-a-profile-publishes.md) (the shares of undeclared levels),
[ADR 0018](./0018-module-facts-live-in-the-flux-bundle.md) (module facts and the profile request),
[ADR 0020](./0020-privacy-grades.md) (grades) and
[ADR 0021](./0021-clique-marginals-are-drawn-through-a-junction-tree.md) (the junction tree).

## Context

The people who read a dataset ask it questions in SQL: dashboards, tests and reports. A synthetic copy
serves them when those questions run on it unchanged, return rows, and give rates close to the ones the
data gives. Nothing checks that today:

- The profile format has carried a `queries` section since format 1-0-0: per named query, a
  `levelHistogram/1` over `["false", "true"]`, the records a query does not and does match. Nothing
  writes the questions, and nothing reads the section.
- The scorecard reserves a `queries` dimension and scores nothing in it.
- `fluxsim calibrate` reads a Playback's authored scorecard and fits nothing.
- The engine holds no SQL engine. DuckDB is a test dependency only.

A question also says what the copy must contain. A filter on `plan = 'premium'` needs the level
`premium`, a test for `ended_at = DATE '9999-12-31'` needs that value, and `sku LIKE 'PRO-%'` needs
values of that shape. A copy drawn from published statistics can miss each of them: a profile withholds
a small cell, the level is never drawn, and the query returns nothing.

A copy fitted to the consumers' own rates can fit them too closely. The rates of questions that took no
part in the fit are the check, so the two kinds are reported apart, and the status "calibrated" waits on
the second kind.

Two more needs meet here. A private release (P3) chooses the marginals it measures with AIM, which
weighs a workload of column sets, and the questions consumers ask are that workload. And a consumer
often wants a world that holds given values, such as every subscriber on one plan, or rows that start
from given partial rows, with the rest of each row consistent with them.

**The threat model.** Consumer SQL is text in a bundle, and a bundle may come from someone else. That
text must not read a file, reach the network, change a setting, load code, or exhaust the machine.
DuckDB's own guidance for SQL from an untrusted source ("Securing DuckDB", version 2.0) is to run it in
a separate process with minimal privileges and with timeouts set by the application. It calls its
settings defense in depth, not a sandbox.

**Borrow-before-build.** Searched, fetched and probed on 2026-11-22; the table at the end of this
record gives each result and its use.

- In the engine at `1c317c0`: the profile's `queries` section; the scorecard's empty `queries`
  dimension; the `Calibrator` protocol (`flux_engine.api.calibration`) and its one declared
  implementation; `flux_engine.sinks.arrow`, which builds typed Arrow batches from a run's rows;
  `flux_engine.calibrate.junction`; ADR 0014's rule for the share of an undeclared level; the
  parameter register's `source` and `reason`; KERNEL-SPEC-2 section 12.
- In the dependencies: `duckdb>=1.5` in the `dev` extra and `pyarrow>=18` in `[arrow]` and `[adbc]`.
  No sqlglot, cma, pyABC, sbi or gbstats in `pyproject.toml` or `uv.lock`.
- Searches: securing DuckDB against untrusted SQL; exact conditional sampling in a junction tree; the
  propagation of evidence (Dawid, 1992); conditional sampling in synthetic data generators; sqlglot's
  qualification of columns.
- Fetched: the PyPI records of sqlglot, duckdb, cma, pyabc, sbi, highspy and pgmpy; the GitHub records
  of their repositories and pycma's `LICENSE`; DuckDB 2.0's "Securing DuckDB" page; mbi's
  `markov_random_field.py`; RFC 6902 section 4.3.
- Probed with duckdb 2.0.1, sqlglot 30.33.0 and pyarrow on Python 3.13: the settings of section 3.

## Decision

### 1. The registry is a module fact under `consumers`

A Simulation declares its consumers' questions under `extensions['com.agentics.flux/1'].consumers`, a key
no engine before this one reads (ADR 0018, section 1). A bundle without it reads, plans and draws as before,
byte for byte.

```yaml
consumers:
  dialect: snowflake
  asOf: "2026-06-30"
  tables:
    prod.analytics.subscriptions: subscriptions
  prefixes: [prod.analytics]
  columns:
    subscriptions: { sub_plan: plan }
  views:
    - name: offered
      sql: SELECT * FROM subscriptions WHERE offered
  queries:
    - id: premium-renewed
      sql: SELECT * FROM prod.analytics.subscriptions WHERE sub_plan = 'premium' AND renewed
      unit: subscriptions
      tolerance: "0.02"
    - id: renewed-offered
      sql: SELECT plan FROM offered WHERE renewed
      unit: offered
      groupBy: plan
    - id: renewed-since
      sql: SELECT * FROM subscriptions WHERE renewed_at >= :since
      parameters: { since: "2026-01-01" }
      unit: subscriptions
      heldOut: true
```

| Key | Holds |
|---|---|
| `dialect` | The sqlglot dialect the consumers write in, from a closed list; `duckdb` when absent. |
| `asOf` | The date that `CURRENT_DATE`, `CURRENT_TIMESTAMP` and `NOW()` read as. A query that reads the current time is refused when the registry declares none, so a rate never depends on the day it runs. |
| `tables` | The table names consumers write, each mapped to an `exposeId` of the bundle. |
| `prefixes` | Qualifiers dropped from any other table name before it is matched to an `exposeId`. |
| `columns` | Per `exposeId`, the column names consumers write, each mapped to a column of the contract. |
| `views` | Named queries that other queries read from, checked as queries are (section 2). |
| `queries` | The questions. |
| `limits` | `seconds`, the wall clock a query may take, and `memory`, DuckDB's memory limit. A bundle may only lower the engine's ceilings, 30 seconds and `1GiB`. Raising one takes a flag on the command line, `--max-query-seconds` (up to 600) or `--max-query-memory` (up to `16GiB`), never a bundle value, so a bundle from someone else cannot claim the machine. |
| `calibration` | The parameters a fit may move (section 8). |

A query holds `id`, `sql` and, optionally, `unit` (the `exposeId` or view whose rows its rate counts),
`groupBy` (a column of the unit, which then needs a unit), `heldOut` (`false` when absent), `tolerance`
(an exact decimal from 0 to 1, the largest gap between the run's rate and its target that still meets it)
and `parameters` (the value text of each placeholder). An `id` is a lowercase letter followed by at most
127 lowercase letters, digits, `.`, `-` and `_`, and names the query in the profile, the request and the
scorecard. The block is read by the exact YAML reader with closed keys and bounds: at most 1,024 queries,
256 views, 64 KiB of SQL per query or view, and 256 parameters per query. Kernel 0.1.0 refuses the key with
`FLX-3105`, naming the kernel.

### 2. SQL is parsed and checked with sqlglot before anything runs

Each view and query is parsed with sqlglot in the declared dialect, checked, and rewritten before DuckDB
sees any of it. `validate`, `plan`, `init`, `verify` and `calibrate` refuse the bundle, naming the query and the construct,
when a text:

- holds more than one statement, or a statement that is not a query: a `SELECT`, a set operation of
  them, or `WITH` before one. DDL, DML, `PRAGMA`, `SET`, `ATTACH`, `COPY`, `INSTALL`, `LOAD`, `EXPORT`,
  `CALL`, and anything sqlglot reads as a command, are refused;
- holds an expression whose sqlglot class is not on a closed list (CONSUMER-QUERIES-SPEC.md): column
  references, literals, comparisons, boolean logic, arithmetic, `CASE`, `CAST` to a closed list of types,
  `IN`, `BETWEEN`, `LIKE`, `ILIKE`, `IS NULL`, joins, subqueries, `WITH` that is not recursive, set
  operations, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT`, and windows over the functions on the list;
- calls a function that is not on a closed list of aggregate, string, number and date functions that
  read only their arguments. A table function (`read_csv`, `read_parquet`, `glob`, `range`), and a
  function that reads the environment, a setting, the catalog, a sequence or a random source, is
  refused;
- names a table that does not resolve, through `tables`, `prefixes`, the names of its `WITH` and the
  declared views, to an expose of the bundle or a view, or names a table as a string or a path;
- holds a placeholder with no value in `parameters`, or a parameter that no placeholder uses;
- holds more than 20,000 tokens, or brackets nested deeper than 64, both counted on sqlglot's tokens before
  the parser runs, or more than 10,000 expression nodes once parsed. A `RecursionError` inside sqlglot is
  refused with its own code, never raised;
- uses `LIMIT`, `OFFSET` or a window function whose `ORDER BY` does not make the order total. A total order
  lists a key the contract declares unique for the table it orders, or every column of the rows it orders, so
  the rows a query keeps or numbers never depend on how ties fall.

The rewrite then works on the parsed tree, never on the text: table names become `exposeId`s, columns
take their contract names, views become `WITH` entries, each placeholder becomes a typed literal (a
string, a whole number, an exact decimal, a date, a timestamp or a boolean, from the value's spelling and
the column it is compared with), and the current time becomes the `asOf` literal. sqlglot then writes the
tree as one DuckDB text with its comments dropped (`sql(dialect="duckdb", comments=False)`), the query's
canonical text. That text is parsed again, in the duckdb dialect, and passes every check of this section once
more, so the tree that was checked and the text DuckDB runs are one statement. Its digest is what the
scorecard and the profile request carry.

Every requirement (section 5) and every rate (section 4) is recorded with the sqlglot and DuckDB versions
that produced it, as a pinned library's version is recorded elsewhere in the engine. A recorded requirement or
rate is compared with one made under other versions only as `versionsDiffer`, never silently.

### 3. Queries run in a locked DuckDB, in a process of their own

Each `verify` and each calibration starts one child process, and stops it when it is done.

- **Its own environment.** The child is `python -I -m flux_engine.consumers._child`, started with an
  environment of its own that holds only `PATH` and `LC_ALL`, so no inherited variable, credential, or
  `DUCKDB_` or `PYTHON` setting reaches it, and with every file descriptor closed but its two pipes.
- **Limits first.** Before it imports DuckDB, the child lowers its own resource limits where the
  platform enforces them: `RLIMIT_FSIZE` to zero, so a file it writes may not grow past zero bytes,
  `RLIMIT_CPU` to its time budget, and `RLIMIT_AS` to its memory budget. After its imports and the
  connection, it lowers `RLIMIT_NOFILE` to the highest descriptor it holds open plus a small margin, so it
  can open no new file; the limits it set are in its first reply.
- **Locked at connect.** It opens an in-memory DuckDB 2.0 database with `enable_external_access=false`,
  `autoload_known_extensions=false`, `autoinstall_known_extensions=false`,
  `allow_community_extensions=false`, `allow_unsigned_extensions=false`,
  `allow_persistent_secrets=false`, `python_enable_replacements=false`, `threads=1`, `memory_limit` from
  `limits.memory`, `max_temp_directory_size='0B'` and `lock_configuration=true`, all passed when the
  connection opens.
- **Data in, counts out.** The parent reads the run's emitted files, the only files this path reads,
  and builds each table with the contract's types (`flux_engine.sinks.arrow`). It sends each one as an
  Arrow IPC stream over a pipe, as framed bytes and never as a pickle. The child registers the tables and
  opens no file. It runs only the counts the engine writes around a canonical text,
  `SELECT count(*) FROM (<query>)`, and the count of the unit, grouped when the query declares `groupBy`.
  Before it runs one, it splits the whole text with DuckDB's own `extract_statements`, and runs it only
  when that finds exactly one statement and it is a SELECT. It returns whole numbers. No row of a result
  leaves DuckDB, so this path prints and logs no value of the run.
- **Time and crashes.** A timer calls `interrupt()` on the connection when a query passes
  `limits.seconds`. The parent ends the child when the whole set passes the sum of its limits and a margin.
  A query that times out or runs out of memory takes that status, and the others still run. When the child
  dies (a signal, a limit, a fault inside DuckDB), the query in flight takes the status `crashed`, and a
  fresh child, under the same locks and with the tables sent again, runs the queries that remain.
- **Versions.** The child reports the duckdb, sqlglot and pyarrow versions, which the scorecard records.

Probed on duckdb 2.0.1 with these settings: `read_csv`, `read_text`, `glob`, `ATTACH`, `COPY`, `EXPORT`,
`INSTALL`, `LOAD` and an `https` URL are refused with a permission error; every `SET` after the
connection opens is refused, `lock_configuration` included; a Python object is not visible to a query;
`register` of an Arrow table still works; a timer's `interrupt()` stopped a runaway join in about one
second; a query past `memory_limit` raised an out-of-memory error; and the connection stayed usable after
both.

This is defense in depth, not an operating-system sandbox. Run a bundle from someone else inside a
container, as DuckDB's guidance says; the docs say so where they introduce consumer queries.

### 4. A query's rate

For a query with a `unit`, `rows` is the number of rows the query returns, `unitRows` the number of rows
of its unit, and its rate is `rows / unitRows`, an exact fraction. A profile states the same rate for the
data in its `queries` entry of the same name: `k` is the unit's records and the `true` count the query's
rows. So a query that returns one row per matching record of its unit has the rate the profile states. A
query whose `rows` pass `unitRows` takes the status `notARate` and is never compared. With `groupBy`, the
rate is taken per group of the unit's column.

A query without a unit is checked for rows only. Each query takes one status: `measured`, `noRows`,
`notARate`, `noTarget`, `withheld` (its target's cell is withheld), `timedOut`, `outOfMemory`, `crashed` or
`versionsDiffer`.

### 5. Literals, patterns and sentinels become requirements

Every query, calibration and held-out alike, is read for what it needs the copy to hold, since a held-out
question must return rows too. Only its text is read here: no rate of a held-out query takes part. sqlglot's
qualification resolves each column to its table through aliases, `WITH` entries and views, and each
predicate gives one requirement:

| Predicate | On | Requirement |
|---|---|---|
| `c = 'x'`, `'x' = c`, `c IN ('x', ...)` | a categorical column | the level `x` |
| `c = v`, `c IN (v, ...)` | a number, date or timestamp column | the value `v` |
| `c LIKE p`, `c ILIKE p` | a string column | the pattern `p` |
| `c IS NULL` | any column | nulls |
| `c BETWEEN a AND b`, and `<`, `<=`, `>`, `>=` | a number, date or timestamp column | the range |

`fluxsim init --consumers FILE` embeds the registry in the Simulation and applies what an existing
declaration can carry:

- **A level** on a categorical column whose levels come from the profile becomes a declared level of
  that column, so ADR 0014's rule gives it its share: its published count when the histogram lists it,
  and otherwise a share of the remainder the published counts leave, never zero. Its entry in the
  Simulation's `parameters` names the query as its reason.
- **A level outside a contract's `enum(...)`** is reported as unmet: no row may hold it.
- **A pattern** is met on a categorical column when one of its levels matches. On an identifier or text
  column it is recorded and checked by `verify`; drawing values to a shape belongs to the record on
  identifiers and text.
- **A value** is met when a declaration produces it, such as a link's sentinel (ADR 0022) or the end of
  an open version. Otherwise it is reported as unmet, with the declarations that would produce it.
- **Nulls** are met when the column draws nulls, from a declared null fraction or a null count the
  profile publishes above zero.
- **A range** is met when the column's declared range, or the run's time window, meets it.

`init` prints each requirement as met or unmet and refuses nothing for an unmet one. `verify` derives
them again from the bundle and checks each on the run's tables with counts (section 3).

### 6. The profile request asks for the rates and hands over a workload

`fluxsim init --consumers FILE`, given no profile, writes request format 1-1-0 (section 12), to which this
record adds:

- **`queries`:** one entry per calibration query with a unit, `{ name, sql, unit, groupBy }`. `sql` is
  the canonical text over `exposeId`s, and `unit` is `{ table }` or, for a view, `{ sql }`. A profiler
  answers each with a `levelHistogram/1` over `["false", "true"]`, grouped when the entry says so: `k`
  is the unit's records and the `true` count the query's rows. A held-out query is never in this list.
- **`tables.<exposeId>.workload`:** a list of `{ columns, weight }`. Each entry is a set of 1 to 8
  categorical columns of the table, all listed in the request with the domain `category`, that
  calibration queries read together in their predicates and groupings. Its `weight` is the number of
  calibration queries that read exactly that set. The workload holds no value. A private release may
  weigh the marginals it selects by it; how a profiler uses it is the profiler's, and the engine's part
  ends with the request.
- **A held-out request:** when the registry holds held-out queries, `init` also writes
  `<dir>.held-out-request.yml`, a request of the same format that lists only their `queries` entries,
  with each unit's table under `tables` and no column. Its answer is a separate profile document, the
  held-out profile, which only `verify` and `calibrate` read, and which no bundle pins.

Requests stay outside the bundle (ADR 0018), so neither is an input of `inputDigest`. A query's text is
the consumers' question: its literals come from the consumers, never from the data.

The request's specification states what a profiler owes these entries. Each is untrusted text: a profiler
checks it again under section 2 and runs it under section 3's locks, or stronger ones. Its disclosure
checks look at the set of queries as a whole and not one query at a time, because two queries that differ
by one literal can reveal one record by differencing. The profiler implements this; the engine's
specification states it now.

### 7. `verify` reports rates, calibration and held-out apart

`fluxsim verify` on a bundle that declares `consumers` runs every query on the run (section 3) and writes
the scorecard's `queries` dimension, in scorecard format 1-4-0 (section 12):

- per query: its name, its set (`calibration` or `heldOut`), the digest of its canonical text, `rows`,
  `unitRows`, rate, target, gap (rate less target), tolerance, status, and whether it is met; per group
  for a query with `groupBy`;
- targets: a calibration query's from the profile the bundle pins, a held-out query's from
  `--held-out FILE`;
- each requirement of section 5, met or unmet;
- two shares, of calibration and of held-out queries with a target and a tolerance that fall within it.
  The dimension's score is the smaller of the shares that are measured;
- the duckdb, sqlglot and pyarrow versions.

The pinned profile may not name a held-out query, and the held-out profile may not name a calibration
query: either is refused, naming the query. The held-out profile is graded under ADR 0020's rule 2, and the
scorecard states its grade beside the pinned profile's. Without the `[sql]` extra the dimension is `not measured`,
with the command that installs it, and a threshold declared on it then fails, as any unmet threshold does
(SCORECARD-SPEC.md section 5).

### 8. Calibration fits declared parameters into a calibration patch

```yaml
calibration:
  parameters:
    - document: simulation.flux.yml
      pointer: /extensions/com.agentics.flux~11/emits/subscriptions/fidelity/columns/region/nullFraction
      min: "0.05"
      max: "0.60"
      step: "0.0001"
  evaluations: 64
  scale: "0.25"
  heldOutTolerance: "0.02"
  seed: 7
```

- **What may move.** Each parameter names a value the parameter register lists, by its document and
  JSON pointer, whose source is `assumption` or `default`. Its `document` is one of the files ADR 0006's
  closed set names for the bundle. A value the profile or the contract states is refused: a fit moves only
  what the data does not state. At most 64 parameters.
- **The fit.** `fluxsim calibrate --fit` runs CMA-ES (cma, in the `[calibrate]` extra) over the
  parameters scaled to the range 0 to 1, bounded, seeded with `seed` (the Simulation's seed when
  absent), for at most `evaluations` runs, and the start point is evaluated as well. A bundle may only lower the engine's ceiling of 64;
  `--max-evaluations` raises it, up to 4,096. Each evaluation runs the
  bundle with the candidate values, each rounded to its `step`, at `scale`, staged under the state
  directory and removed once its counts are taken, and counts the calibration queries (section 3). The
  objective is the sum of the squared gaps between each calibration query's rate and its target in the
  pinned profile. The best evaluation wins, and of equals the earlier one. The declared mode of
  `fluxsim calibrate` is unchanged.
- **Determinism.** Every evaluation, the held-out check included, seeds its rows and its seed root from
  the per-emit `H` of the unpatched bundle (common random numbers), so candidates are compared on the
  same draws. Left alone, `H` binds the fidelity block and each candidate would seed every row again, and
  the fit would follow seed noise. The evaluations run through an option only `calibrate --fit` passes
  to `run`; no other run, and no command line, has it, so every other run keeps its seeds and digests.
  The run an evaluation makes is removed and is not one to replay. A normal run of the calibrated bundle
  seeds from its own `H`, so its rows are a fresh draw: the rate it reaches differs from the fit's by sampling
  noise, which the held-out check and `verify` measure. The optimizer's own draws come from cma's seeded generator, not from the seed tree, so no
  seed-tree path is added. The same engine, cma version and platform give the same patch. Another
  platform may find another, so the patch is the record, not the search.
- **The calibration patch** is a JSON document. Its header holds the method (`flux.calibration.cma/1`), the
  cma, sqlglot and DuckDB versions, the seed, the evaluations run, the scale, the pinned profile's digest
  and each calibration query's gap. Its `operations` are RFC 6902 `replace` operations, one per moved value:
  `{ document, op: "replace", path, value }`. A `replace` needs its target to exist, so a patch moves a
  declared number and can add nothing.
- **What it writes.** `fluxsim calibrate --fit BUNDLE --held-out FILE --out DIR` always writes the patch
  beside the output, as `DIR.patch.json`. When the held-out check holds (section 9) it also writes a new
  bundle at `DIR`: the input bundle with the operations applied, written and checked as `fluxsim init`
  writes a bundle. The input bundle is never changed.
- **The register,** format 1-3-0, gives each moved value the source `calibrated` and a `calibration`
  member: the queries it was fitted to, the value before and after, the patch's digest, the digest of the
  profile that held the targets, and the held-out profile's digest with its largest gap and tolerance.
  Format 1-3-0 is shared with ADR 0030, which adds the source `overlay` and the member `replacedBy` for
  profile overlays: whichever record's change lands first defines 1-3-0, and the other extends it.
  "Overlay" stays ADR 0030's word for a profile overlay.
- The fit implements the engine's `Calibrator` protocol, with the method id `flux.calibration.cma/1`.

### 9. "Calibrated" waits on the held-out queries

- The fit reads the pinned profile only. The patch is written, and its digest taken, before the
  held-out profile is opened. The held-out profile is graded under ADR 0020's rule 2, and the report states
  its grade beside the pinned profile's.
- The calibrated values then run at `scale`, and the held-out queries are counted. Each gap is set
  against the query's own `tolerance`, or else `heldOutTolerance`.
- A calibration is "calibrated" only when at least one held-out query has a target and every held-out
  query with a target falls within its tolerance. Only then is the bundle at `DIR` written. Otherwise
  `calibrate` writes the patch and its report, exits with status 1, and names each held-out query that
  missed, with its gap. A fit with no held-out target is never called calibrated.

  *Amendment, 2026-11-24.* A bundle that declares no held-out query is fitted without one: `--held-out` is
  optional for it, a profile given anyway is not read, the patch is written, and the output (text and
  `--json`) says that no held-out check was made because the bundle declares no held-out query. Such a fit is
  not called calibrated, as above. A bundle that declares a held-out query still needs `--held-out`.

### 10. Conditional generation: fixed values and seed rows

A Simulation may declare `conditions`, a key no engine before this one reads, per `exposeId`:

```yaml
conditions:
  subscriptions:
    - values: { plan: premium, region: EU }
      count: 400
    - values: { plan: basic }
      count: 600
```

- **Rows.** Each group's rows hold its values, and the rest of each row is drawn given them. Groups
  take their rows in the order listed, so a row's group is a function of its ordinal, and the emit's rows
  are exactly the groups' rows: a declared row count that disagrees is refused. One group without a
  `count` covers every row of the emit. A seed row is a group with a count of 1. At most 100,000 groups
  per emit.
- **Which columns.** A fixed column is categorical and is drawn through a junction tree (ADR 0021), a
  Chow-Liu forest (ADR 0014), or from its own marginal with no column drawn given it. A key, an
  identifier, a column a rule sets, a copied or linked column, and a column whose value a link or a
  lifetime sets, are refused, and so is a value that is not one of the column's levels. So is a column
  whose FLUID `sensitivity` requires grade P3 (ADR 0020): a fixed value is a declaration, and a grade never
  sees a declaration.
- **Exactly.** ADR 0021's rule 3 draws column by column: in a clique with separator `S` and new columns
  `n_1` to `n_m`, in declared order, `n_j` is drawn given `S` and `n_1` to `n_(j-1)`. For a group's values,
  every conditional (ADR 0021, rule 4) has its cells that disagree with them set to zero. Each clique sends
  its parent a message over its separator: the sum, over all its new columns (a fixed one only at its value),
  of the product of their conditionals and its children's messages. The weight of a value `v` of `n_j` is
  then its conditional given `S` and `n_1` to `n_(j-1)`, times the sum, over the later new columns `n_(j+1)`
  to `n_m` (a fixed one only at its value), of the product of their conditionals and the messages of the
  child cliques. A fixed column is set, not drawn. A Chow-Liu forest is conditioned the same way, each pair
  a clique. The arithmetic is rational, and the product of the conditionals drawn is the joint the cliques
  describe, given the fixed values: there is no rejection and no approximation. This is the propagation of
  evidence in a junction tree (Dawid, 1992), the pattern mbi follows when its constraints enter as
  potentials before it samples in elimination order. Values that leave no cell above zero are refused at
  `plan`, naming the group.
- **The table a uniform is searched in.** A column's weights, exact fractions, are made shares. Each
  threshold is the smallest whole number not below a cumulative share times `2**53`, as kernel 0.2.0 builds
  every table, the last threshold `2**53`, and the cell's 53-bit uniform is searched in them as kernel 0.2.0
  searches every table. The per-row and the column path build the same thresholds.
- **Draws.** A drawn cell of a conditioned row draws on its own path, at the index every cell of its
  column draws at, and the values only choose the table of weights its uniform is searched in, as
  KERNEL-SPEC-2 section 12 says of `given`. A fixed cell draws nothing. So no seed-tree path is added,
  and a bundle without the key has the bytes it had. Conditioned rows are kernel 0.2.0 only: kernel 0.1.0
  refuses the key with `FLX-3105`, naming the kernel.
- **Scale and shards.** `--scale` scales each group's count as the emit's rows are scaled, by the largest
  remainder over the groups, so the counts add up to the scaled total. A shard takes the rows of its
  ordinals, and with them their groups. Children follow their parents as they do today.
- **Seed rows are declared values.** The engine cannot tell whether a value was copied from a record of
  the data, so keeping ADR 0003's rule is the author's part, and the docs say so.
- **Evidence.** `EVIDENCE.md` names each conditioned emit with its groups' counts and fixed columns under
  "What Was Not Proven", and never prints a fixed value.

### 11. Grades

ADR 0020 grades a run from the profile it pins (its rule 2), and a declaration never enters a grade. A
calibrated bundle pins the profile that held its targets, so that rule grades it, and a profile whose query
rates are published exactly is P1 by it; the patch is a declaration and enters no grade. Every other profile
a command reads is graded under the same rule and its grade stated: the held-out profile by `verify` and
`calibrate`. `ship` states the weakest grade of every profile the run read. A level that a requirement adds
shares the remainder by ADR 0014's rule, as any undeclared level does, and changes no grade. The workload
holds no value and changes none.

### 12. Formats

This record ships in one release with other records that change the same formats, so each format gets one
new version in the release. The first record to land defines that version, and the others extend it.

- **Profile request 1-1-0:** `queries`, `tables.<exposeId>.workload`, and the held-out request
  (section 6).
- **Profile 1-3-0:** a query's `levelHistogram/1` may carry `groupBy`, naming a column of the query's
  unit and of its rows. Its groups are keyed by that column's levels, and the disclosure rules apply
  group by group, as they do for a column's statistic.
- **Scorecard 1-4-0:** the `queries` dimension's first property kind (section 7).
- **Parameter register 1-3-0:** the source `calibrated` and the `calibration` member (section 8), in the
  format ADR 0030 shares.
- **The Simulation's namespace:** the keys `consumers` and `conditions`.

Each addition is optional. A document without it keeps its format, its bytes and its digest, and a
reader of the new format reads an older document as it did.

## Consequences

- A bundle that declares neither key has the bytes, digests and outputs it had, under both kernels. A
  golden whose digests come from the engine before this record pins that.
- Parsing and running SQL needs the `[sql]` extra: `sqlglot==30.33.0`, `duckdb>=2.0.1,<2.1` and
  `pyarrow>=18`. `[calibrate]` adds `cma==4.6.0` to it. A bundle that declares `consumers` plans and
  applies without either, because generation reads no SQL; `verify` then reports the queries as not
  measured.
- `verify` takes as long again as its queries take, plus one process start.
- The new code lives in new modules. The generator's core changes in three places: the namespace's list
  of keys, the row draw that conditions read, and `init`'s levels.
- The consumers' questions become a reviewable part of the bundle, and a profiler learns them from the
  request.

## Alternatives considered

- **DuckDB in the engine's own process.** Simpler, and rejected: DuckDB's guidance asks for a separate
  process for untrusted SQL, and a crash or a runaway query would take the engine down with it.
- **Checking the text with patterns, or with DuckDB's own parser.** Rejected: sqlglot reads the
  consumers' dialect into a typed tree, and a text refused before DuckDB parses it never reaches DuckDB.
- **The calibration patch as an input of a run (`plan --patch`).** Rejected: it adds a kind of file to ADR
  0006's closed set, and one bundle could then run with several sets of values under one name. A calibrated
  bundle is a bundle of its own.
- **multiprocessing's `spawn` start method for the child.** Rejected: its child inherits the parent's
  environment, credentials included. A child started with an environment of its own receives none.
- **Held-out rates in the pinned profile, flagged.** Rejected: the fit could read them. A separate
  document, opened after the patch is written, keeps them out by construction.
- **pyABC or sbi for a posterior over the parameters.** Rejected as dependencies: pyABC 0.13.1 installs
  pandas, dask's distributed, redis, SQLAlchemy and Jupyter's kernel, and sbi 0.27.0 installs torch. A
  later record may add a posterior fit.
- **A linear or quadratic program (highspy).** A rate that is linear in the parameters could be met
  exactly. Rejected for now: the rates of joins and of queries over several tables are not linear in the
  parameters, and one method keeps one record.
- **Rejection sampling for conditions,** drawing rows and keeping those that hold the values, as SDV's
  neural synthesizers do. Rejected: it is slow for rare values and exact only in the limit, while
  conditioning the tree is exact.
- **Clamping,** fixing the value and drawing the rest forward as usual. Rejected: a column drawn before
  the fixed one does not see it, so the row is not consistent with the value.
- **A generator for point values,** so a sentinel test always finds its value. Rejected here: the modules
  that own a sentinel (links and versions) declare it.

## What is borrowed

| Project | Version and licence | Checked on 2026-11-22 | Use |
|---|---|---|---|
| sqlglot | 30.33.0, MIT; released 2026-11-17 | PyPI; GitHub (tobymao/sqlglot, pushed 2026-11-21); a Snowflake query with a placeholder parsed and written as DuckDB | depend, `[sql]`, pinned exactly: the rewrite is part of what ran |
| DuckDB | 2.0.1, MIT; released 2026-11-20 (2.0.0 on 2026-10-21) | PyPI; GitHub releases; "Securing DuckDB" for 2.0; the settings of section 3 probed | depend, `[sql]`, `>=2.0.1,<2.1` for its bug fixes, the version recorded per run |
| pyarrow | 18 or later, Apache-2.0 | already the `[arrow]` extra | depend, `[sql]` |
| cma (pycma) | 4.6.0, BSD-3-Clause (`LICENSE` read); released 2026-11-15; needs numpy only | PyPI; GitHub (CMA-ES/pycma) | depend, `[calibrate]`, pinned exactly |
| highspy | 1.16.0, MIT | PyPI | not used (alternatives) |
| pyABC | 0.13.1, BSD-3-Clause | PyPI, its install requirements | not a dependency (alternatives) |
| sbi | 0.27.0, Apache-2.0 | PyPI, its install requirements | not a dependency (alternatives) |
| mbi, formerly Private-PGM | Apache-2.0; pushed 2026-11-18 | `MarkovRandomField.synthetic_data`: constraints as potentials, Shafer-Shenoy message passing, sampling in elimination order | the pattern of section 10; no code used |
| pgmpy | 1.1.2, MIT | PyPI | not used: the engine's own tree carries the counts |
| SDV | BUSL-1.1 | conditional sampling: a condition holds a number of rows and the column values | the shape of a group; no code used |
| Dawid (1992), "Applications of a general propagation algorithm for probabilistic expert systems", Statistics and Computing 2, 25 to 36 | paper | its record | the propagation of evidence (section 10) |
| RFC 6902, JavaScript Object Notation (JSON) Patch | IETF | section 4.3 | the calibration patch's `replace` operations |
