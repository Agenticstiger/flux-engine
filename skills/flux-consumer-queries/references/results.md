# Reading the results

What `fluxsim init` with `--consumers` and `fluxsim verify` print about consumer queries, and how to report it. The worked page is
[check consumer queries](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/check-consumer-queries.md).

## Requirements, from `init`

With a profile, `init --consumers FILE` prints one line per requirement the queries add: `met`, `unmet` or `left to verify`. `init` refuses nothing for an unmet one.

| A query needs | Met when |
|---|---|
| a level (`c = 'x'`, `c IN (...)`) on a categorical column | the column declares the level; a level from a query becomes a declared level with a share of its own, and the register names the query as its reason |
| a level outside a contract's `enum(...)` | never: reported unmet, since no row may hold it |
| a pattern (`LIKE`) | a categorical column has a level that matches; on an identifier or text column it is recorded and `verify` checks it |
| a value | a declaration produces it (a link's sentinel, the end of an open version); otherwise unmet, with the declarations that would produce it |
| nulls (`IS NULL`) | the column draws nulls |
| a range | the column's declared range, or the run's time window, meets it |

Report each unmet requirement and the declaration that would meet it. Do not add a declaration the user did not ask for, and never copy a value from the real records to meet one. `verify` derives the requirements again and counts each on the run.

## Rates, from `verify`

`verify` on a bundle that declares `consumers` counts every query on the run, in a locked DuckDB in a process of its own, and fills the scorecard's queries dimension. It prints one line per query, such as `query premium-renewed (calibration): rate 0.13, target 0.15, gap -0.02, tolerance 0.02: within` or `rate 586/891: noTarget`. The set is in parentheses, the rows show only as a fraction, and `within` or `measured` stands beside the status. `verify --json` carries the full fields: `rows`, `unitRows`, rate, target, gap (rate less target), tolerance and status.

- A rate is `rows` over `unitRows`, an exact fraction. With `groupBy` it is per group.
- A calibration query's target comes from the profile the bundle pins (`--profile`); a held-out query's from `--held-out`. The pinned profile may not name a held-out query (`FLX-3701`), and the held-out profile may not name a calibration query (`FLX-3702`).
- Each query takes one status: `measured`, `noRows`, `notARate` (it returned more rows than its unit holds), `noTarget` (nothing to compare, for example a rate stated only as a whole when the query groups), `withheld` (the profile withheld the cell), `timedOut`, `outOfMemory`, `crashed`, `versionsDiffer` or `error` (DuckDB refused the text at execution). A timeout or a crash takes its own query only; the others still run.
- Two shares are reported, of calibration queries and of held-out queries that have a target and a tolerance and fall within it. The dimension's score is the smaller of the shares that are measured.
- A held-out query with a target and no tolerance is measured and in no share.
- The scorecard records the sqlglot, DuckDB and pyarrow versions. A rate made under other versions shows as `versionsDiffer`, never silently.

"No target", "withheld", "not measured" and a missing share are gaps, not passes. Say so, and name the leg or the file that would fill it. Without the `sql` extra the dimension reads `not measured`, with no FLX code; a threshold declared on it then fails as any unmet threshold does. Ask the user to install the extra.

The two grades are stated side by side: the pinned profile's and the held-out profile's. A profile that publishes its query rates exactly is graded P1 by ADR 0020's rule, and a query, a requirement, a workload and a calibration patch enter no grade. `ship` states the weakest grade of every profile the run read, and counts the queries as `verify` does (it takes `--held-out`, `--max-query-seconds` and `--max-query-memory` too); ship belongs to the `flux-scale-and-ship` skill, and runs only when the user asks for it and names the destination.

## Do not over-read a rate

- A query's rate is one measure of fit. It sits beside the scorecard's other dimensions and replaces none.
- Never describe a run as valid or realistic because its rates are within tolerance.
- A rate within tolerance of a target is not a statement about any record.

## Time and size

`verify` takes as long again as its queries take, plus one process start. A query past `limits.seconds` is interrupted and takes `timedOut`. A whole batch that stops answering is ended and counts nothing (`FLX-7133`); run it again, and if it repeats, make the queries do less work. Raising the ceiling is the user's choice (`--max-query-seconds`, up to 600, and `--max-query-memory`, up to 16GiB).
