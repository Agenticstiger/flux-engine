# Refusals of consumer queries, calibration and fixed values

For any code: run `fluxsim explain FLX-nnnn` (offline, the same text as its page), apply the remedy, then run the same step again. `fluxsim explain --list` lists every code of the installed engine. The index of
[all error codes](https://github.com/Agenticstiger/flux-engine/blob/v0.10.0/docs/reference/errors/README.md)
links each page. Never weaken a check, raise a tolerance, move a query between the two sets, or record past a refusal.

## Code families

| Codes | What they mean | Next step |
|---|---|---|
| `FLX-3601` to `FLX-3613` | the registry's shape: the block, the dialect, `asOf`, the table, prefix and column maps, the ids, views, SQL text, unit and `groupBy`, tolerance and `heldOut`, parameters, limits, the calibration block, a key the engine does not read | fix the named key in the consumers file; the shapes are in the registry reference of this skill |
| `FLX-3614` to `FLX-3616` | a bundle's `limits` or `evaluations` is above the engine's ceiling | lower it in the bundle; a bundle may only lower a ceiling |
| `FLX-3617` | a calibration parameter names a document outside the bundle's closed set | name a document of the bundle |
| `FLX-3618` to `FLX-3636` | the SQL: it does not parse, holds more than one statement or not a query, uses an expression or function that is not allowed, reads a table function, a string or a path, names a table or column that does not resolve (or resolves to more than one), has a placeholder with no parameter, is too large or too deep, reads the clock with no `asOf`, orders rows that are not totally ordered, views that read each other in a cycle, or a unit or `groupBy` that does not fit its rows | write a plainer query inside the closed lists, with the user's agreement; never ask the lock to admit a construct |
| `FLX-3637` | the file given to `init --consumers` is not a consumers block | write the file with the block's keys at the top level |
| `FLX-3713` to `FLX-3715` | a query reads a projection alias of its own `SELECT`, holds a `LIKE` pattern of more than 512 characters, or derives more requirements than the engine reads | rewrite the query; each refusal names it |
| `FLX-5038` | a profile request is refused, which now includes a consumer query or workload entry that breaks its rules | regenerate the request with `fluxsim init` |
| `FLX-7131` | consumer queries need the sql extra, which is not installed | ask the user to install it |
| `FLX-7132` to `FLX-7135` | the query process: a message past its bound, ended after its time, did not start as asked, or a run's stream cannot be read as a table | run the command again; if it repeats, report the code; the user decides about `--max-query-seconds` |
| `FLX-3629`, `FLX-3630` | a clock with no `asOf`, a `LIMIT` or a window with no total order | declare `asOf` or take the date from a parameter; add a unique key to the `ORDER BY` |
| `FLX-3700` | a query ceiling flag (`--max-query-seconds`, `--max-query-memory`) is outside its range | pass a value in range, or none |
| `FLX-3701`, `FLX-3702` | the held-out sets are not apart: the pinned profile holds a held-out query's rate, or the held-out profile names a calibration query, or a bundle with no held-out query was given `--held-out` | for a profile that holds a held-out rate or names a calibration query, ask the profiler for a profile that keeps the sets apart; for a bundle with no held-out query, drop `--held-out`, or declare held-out queries with the user; never edit a profile |
| `FLX-3703` | a run's emitted stream does not digest to its receipt | the run was changed after `apply`; report it, plan again |
| `FLX-3704` | `--max-evaluations` is outside its range | pass a value in range, or none |
| `FLX-3705` to `FLX-3707` | what a fit may move: a source it may not move, a document or number the bundle does not have, or nothing to fit | the user's decision on what may move; see the calibration reference |
| `FLX-3708`, `FLX-3709` | the `calibrate` extra is missing; or the queries were checked with another sqlglot than the one installed | ask the user to install the extra or the recorded version, or write the queries again with `fluxsim init` |
| `FLX-3710`, `FLX-3711` | the place a fit writes is taken, or a patch operation cannot be applied | name a new output folder; report the second |
| `FLX-3712` | the held-out error does not hold, so the bundle was not called calibrated, or no candidate beat the start point; the patch is written, no bundle | report each missed query and gap; see the calibration reference |
| `FLX-3716` | a calibrated entry whose held-out gap is larger than its tolerance | the record contradicts itself; report it, never edit the register to fit |
| `FLX-7101` | a candidate of the fit could not be run | report the code and the candidate's message |
| `FLX-3640` to `FLX-3647` | `conditions`: see the fixed values reference | see it |
| `FLX-4005` | the run's grade is below a column's minimum | the `flux-privacy-and-profiling` skill; never lower a grade or change a label |

## Handling one

1. Quote the code and its one-line meaning to the user.
2. Name what `fluxsim explain` says to do.
3. If the remedy is a file of the bundle and the user's request covers it, change only that. If it is a decision (a tolerance, a held-out flag, what a fit may move, a profile, a ceiling), ask the user.
4. Run the step again. Do not loop on one code without a change.

A usage error (exit code 2, no code) means the installed engine differs from this skill. Read `fluxsim <command> --help`.
