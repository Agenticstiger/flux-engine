---
title: "Check consumer queries, calibrate to them"
description: "Declare the SQL your consumers run, have fluxsim check and count it on a run, read the queries dimension of the scorecard, then fit declared numbers to the queries' rates and let held-out queries decide whether the result is called calibrated."
---

# Check consumer queries, calibrate to them

Use this page when the people who read your data ask it questions in SQL (dashboards, tests and reports)
and you want the synthetic copy to serve those questions: each one runs unchanged, returns rows, and gives
a rate close to the one the real data gives. The design is
[ADR 0028](../adr/0028-consumer-queries-run-locked-compile-requirements-and-calibrate.md) and the rules are
in the [specification](../spec/CONSUMER-QUERIES-SPEC.md).

The steps:

1. declare a registry of the consumers' queries in the bundle;
2. `fluxsim init --consumers` writes the request a profiler answers, and a bundle that carries the queries;
3. `plan` checks every query, `apply` makes the run;
4. `verify --held-out` counts the queries on the run and scores them;
5. `calibrate --fit` fits declared numbers to the queries' rates, and the held-out queries decide whether the
   result may be called calibrated.

## Before you start

Consumer queries need the `sql` extra, and fitting needs the `calibrate` extra:

```text
pip install 'flux-sim-engine[calibrate]'
```

Generation reads no SQL, so a bundle that declares queries plans and applies without either extra. `verify`
then reports the queries as not measured, with that command.

The locks that run the queries are defense in depth, not a sandbox. Each query is checked against a closed
list before DuckDB sees it, and counted in a process of its own with no network, no file access and a limit
on time and memory, but DuckDB's own guidance is to treat these as layers and not as a boundary. **Run a
bundle that someone else wrote inside a container.** A bundle you wrote yourself is text you have read.

## 1. The scenario

The page uses the engine's own test fixture, `tests/fixtures/consumers`, so run it from a checkout of the
repository. It is a table of subscriptions with a `plan`, a `region`, an `offered` flag, a `renewed` flag, a
renewal date and an amount, its FLUID contract, a profile of it, and a bundle.

```python
import shutil
from pathlib import Path

import flux_engine

source = Path(flux_engine.__file__).resolve().parents[1] / "tests" / "fixtures" / "consumers"
shutil.copytree(source, "fixture")
```

```console
$ ls fixture
_make_fixture.py
bundle
consumers.yml
subscriptions.fluid.yaml
subscriptions.held-out-profile.json
subscriptions.held-out-request.yml
subscriptions.profile-request.yml
subscriptions.profile.json
```

## 2. Declare the registry

The registry is a `consumers` block. It is written as a file for `fluxsim init --consumers`, with the
block's keys at the top level, and `init` embeds it in the Simulation under
`extensions['com.agentics.flux/1'].consumers`.

```console
$ cat fixture/consumers.yml
# The consumers block of ../bundle/simulation.flux.yml, as the file `fluxsim init --consumers`
# reads it: the keys of the block at the top level. Two calibration queries and one held out.
dialect: snowflake
asOf: '2026-06-30'
tables:
  prod.analytics.subscriptions: subscriptions
prefixes:
- prod.analytics
columns:
  subscriptions:
    sub_plan: plan
views:
- name: offered
  sql: SELECT * FROM subscriptions WHERE offered
queries:
- id: premium-renewed
  sql: SELECT * FROM prod.analytics.subscriptions WHERE sub_plan = 'premium' AND renewed
  unit: subscriptions
  tolerance: '0.02'
- id: renewed-offered
  sql: SELECT plan FROM offered WHERE renewed
  unit: offered
  groupBy: plan
- id: renewed-since
  sql: SELECT * FROM subscriptions WHERE renewed_at >= :since
  parameters:
    since: '2025-03-01'
  unit: subscriptions
  heldOut: true
limits:
  seconds: 30
  memory: 1GiB
```

- `dialect` is the SQL the consumers write, Snowflake here. The engine reads each text with sqlglot in that
  dialect and writes it back as one DuckDB text, the query's canonical text.
- `asOf` is the date that `CURRENT_DATE` and `NOW()` read as, so a rate never depends on the day it is
  counted. A query that reads the clock with no `asOf` is refused.
- `tables`, `prefixes` and `columns` map the names consumers write to the bundle's exposes and the
  contract's columns. Here the consumers write `prod.analytics.subscriptions` and `sub_plan`.
- `views` are named queries that other queries read from. `renewed-offered` reads the view `offered`.
- Each query has an `id` and its `sql`. `unit` is the table or view whose rows the query's rate counts: the
  rate of `premium-renewed` is the rows it returns over the rows of `subscriptions`. `groupBy` takes the rate
  per group of a column of the unit. `tolerance` is the largest gap from the target that still meets it.
  `parameters` give each placeholder its value.
- `heldOut: true` keeps a query out of every fit. Its target comes from a separate profile, and it is the
  check on the result (section 6).
- `limits` may only lower the engine's ceilings of 30 seconds and 1GiB. Raising one takes a flag on the
  command, such as `--max-query-seconds`, never a bundle value, so a bundle from someone else cannot claim
  the machine.

The text of each query is checked before anything runs: one statement, a query and not a command, every
expression and function on a closed list, every table and column resolved, and a total order wherever
`LIMIT` or a window depends on one. A refusal names the query and the construct. The list is in the
specification.

## 3. Ask the profiler, then write the bundle

With a contract and no profile, `init --consumers` writes the request a profiler answers. It asks for the
rate of each calibration query, and for the sets of columns the queries read together. The held-out queries
go in a request of their own.

```console
$ fluxsim init request-demo --contract fixture/subscriptions.fluid.yaml --consumers fixture/consumers.yml
wrote the profile request request-demo.profile-request.yml: 1 table, 7 columns, 0 relationships, 0 facts

consumers: 3 queries read with sqlglot 30.33.0, 2 requirements to apply once a profile is given; pass the same --consumers file with --profile
the request asks for the rate of 2 queries and lists 2 sets of columns the queries read together
wrote the held-out request request-demo.held-out-request.yml: 1 query, no column

next:
  measure what request-demo.profile-request.yml lists with your profiler, and write a profile
...
```

The request carries the canonical text of each question (every name qualified and quoted, every view written
out, each placeholder a literal), so a profiler measures the same question the engine counts. Here are its
names, units and grouping:

```console
$ sed -n '/^"queries":/,$p' request-demo.profile-request.yml | grep -E '"(name|table|groupBy)"'
  - "name": "premium-renewed"
      "table": "subscriptions"
  - "name": "renewed-offered"
    "groupBy": "plan"
```

The held-out request lists only the held-out query. Its answer is a separate profile, read only by `verify`
and `calibrate`, which no bundle pins:

```console
$ grep -c '"name"' request-demo.held-out-request.yml
1
```

A profiler is owed these entries as untrusted text: it checks each query again and runs it under the same
locks or stronger ones, and its disclosure checks look at the queries together, since two that differ by one
literal can reveal a record by differencing.

Once a profile exists, `init` runs again with the same `--consumers` file and writes the bundle. It also
reads each query for what it needs the copy to hold, and reports each need as met or unmet:

```console
$ fluxsim init bundle --contract fixture/subscriptions.fluid.yaml --profile fixture/subscriptions.profile.json --consumers fixture/consumers.yml | grep -A3 '^consumers:'
consumers: 3 queries read with sqlglot 30.33.0, 2 requirements -- 2 met, 0 unmet, 0 left to verify
  met       premium-renewed: subscriptions.plan needs the level 'premium' -- the column declares the level
  met       renewed-since: subscriptions.renewed_at needs the range '>=2025-03-01' -- the column's declared range meets it
assumed (the register gives each assumed number its reason; replace what you know better):
```

A filter on `plan = 'premium'` needs the level `premium`, and a copy drawn from published statistics can
miss it when the profile withheld a small cell. A level a query needs becomes a declared level of its
column, with a share of its own, and the register names the query as the reason. A value, a pattern, nulls
and a range that no declaration produces are reported as unmet, and `init` refuses nothing for them.

## 4. Plan and apply

`fixture/bundle` is the bundle that command writes, with a `calibration` block added to its Simulation for
section 6. Copy it and plan. `plan` checks every query against the bundle's contract, so a text that would
fail is found here and not after a run:

```console
$ cp -R fixture/bundle declared
$ fluxsim plan declared --profile fixture/subscriptions.profile.json
plan written to .flux/runs/.../plan.json (runId=..., planDigest=sha256:...)
staged 1200 rows in 1 emits under .flux/runs/.../.stage (...)
parameters: 31 declared -- 18 from the pinned profile, 2 from the contract, 8 assumed, 3 default; 9 statistics of the profile unused
$ RUN=$(ls .flux/runs)
$ fluxsim apply declared --run-id $RUN
applied to .flux/runs/... (runDigest=sha256:...)
emitted subscriptions: 1200 rows (generator)
```

## 5. Verify, with the held-out profile

`verify` counts every query on the run, in a locked DuckDB in a process of its own, and scores the rates in
the scorecard's `queries` dimension. The calibration queries take their targets from the profile the bundle
pins, and the held-out queries from `--held-out`:

```console
$ fluxsim verify declared --run-id $RUN --profile fixture/subscriptions.profile.json --held-out fixture/subscriptions.held-out-profile.json
reproducible: not checked | validity: not asserted
  golden: not checked (...)
  artifacts: every run-digest component reproduces
  rows: ok
  rfc-04 vectors: 0 failure(s)
  fidelity subscriptions: passed -- 2 of 2 declared properties within the margin against the pinned profile
  parameters: 31 declared -- 18 from the pinned profile, 2 from the contract, 8 assumed, 3 default; 9 statistics of the profile unused
  scorecard: overall 0.9920 against profile sha256:... (covers 2 of 7 columns, 0 of 0 relationships)
    dimensions: marginals 0.9841, queries 1; not measured: structure, joint, crossTable, temporal, invariants, defects
    table subscriptions: 0.9841 (2 of 2 properties scored); covers 2 of 7 columns, 0 of 0 relationships
    queries: calibration 1 of 1 within tolerance, held-out none compared; requirements 2 of 2 met; profile grade P1, held-out profile grade P1
      query premium-renewed (calibration): rate 0.13, target 0.15, gap -0.02, tolerance 0.02: within
      query renewed-offered (calibration): rate 586/891: noTarget
      query renewed-since (held-out): rate 103/150, target 0.6, gap 13/150: measured
    shape: no flags in 7 profiled columns (27 tests at 1/1000 per run)
```

Read the `queries` lines:

- A rate is rows over unit rows, an exact fraction. `premium-renewed` returned 156 of 1200 subscriptions,
  0.13, against a target of 0.15 in the pinned profile. Its gap, the rate less the target, is no larger than
  its tolerance of 0.02, so it is within.
- `renewed-offered` is `noTarget`. It groups by `plan`, and the pinned profile states its rate only as a
  whole, not per plan, so there is nothing to compare. A query takes one status: `measured`, `noRows`,
  `notARate` (it returned more rows than its unit holds), `noTarget`, `withheld` (the profile withheld the
  cell), `timedOut`, `outOfMemory`, `error`, `crashed` or `versionsDiffer`.
- `renewed-since` is held out. It has a target from the held-out profile and no tolerance, so it is
  measured and in no share yet. The dimension's score is the smaller of the shares of the calibration and
  of the held-out queries that have a target and a tolerance.
- The requirements, each literal and range a query needs, are counted on the run and reported met or unmet.
  An unmet requirement fails nothing.

The pinned profile may not name a held-out query, and the held-out profile may not name a calibration
query: either is refused, naming the query, so the targets of the two sets stay apart. The scorecard states
the grade of each profile, and a threshold declared on `queries` fails `verify` and withholds `ship` when it
is not met. A fixture-sized run takes a second; a long query takes the seconds its `limits` allow, and one
that passes them takes the status `timedOut` while the others still run.

## 6. Fit, and let the held-out queries decide

A fit moves only numbers the bundle assumes. The `calibration` block of the fixture's Simulation names one,
the weight of the first bin of `renewed_at`, but that histogram has one bin, so its weight is a share of
itself and moves no query's rate: a fit of it would report a value and change nothing. A fit is worth running on
a number that decides a calibration query's rate. `premium-renewed` counts the premium subscriptions that
renewed, so the share of `renewed` the generator leaves NULL decides it. The contract allows NULL there and
the profile publishes no count of it, so the working copy assumes a tenth, enters the assumption in the
parameter register with its reason, and points the block at it. This edits the page's own copy, `declared`,
and leaves the shared fixture alone:

```python
from pathlib import Path

path = Path("declared/simulation.flux.yml")
text = path.read_text(encoding="utf-8")
nulls = "/extensions/com.agentics.flux~11/emits/subscriptions/fidelity/columns/renewed/nullFraction"
weight = "/extensions/com.agentics.flux~11/emits/subscriptions/fidelity/columns/renewed_at/binWeights"
edits = [
    # The assumed share of NULLs in `renewed`.
    (
        "            renewed:\n              levels:",
        "            renewed:\n              nullFraction: '0.1'\n              levels:",
    ),
    # Its entry in the parameter register, before the next column's.
    (
        "    - document: sim-telco.silver.subscriptions\n      pointer: " + weight + "\n",
        "    - document: sim-telco.silver.subscriptions\n      pointer: " + nulls + "\n"
        "      source: assumption\n"
        "      reason: The contract allows NULL in renewed and the profile publishes no count of it,\n"
        "        so a tenth is assumed and a calibration may move it.\n"
        "    - document: sim-telco.silver.subscriptions\n      pointer: " + weight + "\n",
    ),
    # The block fits that share, from none to a half, a hundredth at a time.
    (weight + "/0\n          min: '0.5'\n          max: '2'\n          step: '0.5'", nulls + "\n          min: '0'\n          max: '0.5'\n          step: '0.01'"),
]
for old, new in edits:
    assert text.count(old) == 1, old
    text = text.replace(old, new)
path.write_text(text, encoding="utf-8")
```

```console
$ sed -n '/^      calibration:/,$p' declared/simulation.flux.yml
      calibration:
        parameters:
        - document: simulation.flux.yml
          pointer: /extensions/com.agentics.flux~11/emits/subscriptions/fidelity/columns/renewed/nullFraction
          min: '0'
          max: '0.5'
          step: '0.01'
        evaluations: 24
        scale: '0.25'
        seed: 7
```

Each parameter names a document of the bundle and a JSON Pointer to a number the parameter register lists
with the source `assumption` or `default`. A value the profile or the contract states is refused: a fit moves
only what the data does not state. CMA-ES runs for at most `evaluations` runs of the bundle at `scale`, from
a seeded generator, and the objective is the sum of the squared gaps between each calibration query's rate
and its target. It reads the pinned profile only.

```console
$ fluxsim calibrate --fit declared --profile fixture/subscriptions.profile.json --held-out fixture/subscriptions.held-out-profile.json --out calibrated   # exit 1
fit: 24 evaluations, 1 value moved
wrote calibrated.patch.json (sha256:...)
grade: pinned profile P1, held-out profile P1
held-out query renewed-since: gap 0.08, tolerance none: missed (it has no tolerance (neither its own nor calibration.heldOutTolerance))
FLX-3712: the held-out error does not hold, so the bundle was not called calibrated -- ...
more: fluxsim explain FLX-3712  ...
```

The patch is written first, and its digest taken, before the held-out profile is opened. The held-out
queries then decide. A bundle is called calibrated only when at least one held-out query has a target and
every held-out query with a target falls within its tolerance, its own or the block's `heldOutTolerance`. A
held-out query with no tolerance to be held to is a miss, so this first fit writes the patch and no bundle,
and exits 1. A fit with no held-out target is never called calibrated.

Say how close the held-out rate must be. A gap of a tenth is the largest this page accepts:

```console
$ sed -i.bak "s/^        seed: 7$/        seed: 7\n        heldOutTolerance: '0.1'/" declared/simulation.flux.yml
$ rm declared/simulation.flux.yml.bak
$ rm calibrated.patch.json
$ fluxsim calibrate --fit declared --profile fixture/subscriptions.profile.json --held-out fixture/subscriptions.held-out-profile.json --out calibrated
fit: 24 evaluations, 1 value moved
wrote calibrated.patch.json (sha256:...)
grade: pinned profile P1, held-out profile P1
held-out query renewed-since: gap 0.08, tolerance 0.1: within
calibrated: wrote calibrated
```

The same engine, optimiser version and platform give the same patch, so the patch is the record and not the
search. It holds RFC 6902 `replace` operations, one per moved value, and the header says how it was made:

```console
$ cat calibrated.patch.json
{
  "evaluations": 24,
  "gaps": [
    {
      "gap": "-13/300",
      "query": "premium-renewed"
    },
    {
      "gap": null,
      "query": "renewed-offered"
    }
  ],
  "method": "flux.calibration.cma/1",
  "operations": [
    {
      "document": "simulation.flux.yml",
      "op": "replace",
      "path": "/extensions/com.agentics.flux~11/emits/subscriptions/fidelity/columns/renewed/nullFraction",
      "value": "0.03"
    }
  ],
  "profile": "sha256:...",
  "scale": "0.25",
  "seed": 7,
  "versions": {
    "cma": "...",
    "duckdb": "...",
    "sqlglot": "..."
  }
}
```

The calibrated bundle at `calibrated` is the input bundle with the operations applied: every other byte of
the documents is as it was, and the input bundle is untouched. A replace needs its target to exist, so a
patch moves a number the bundle already declares and adds nothing. The register records the move, with the
source `calibrated`:

```console
$ grep -A15 'source: calibrated' calibrated/simulation.flux.yml
      source: calibrated
      calibration:
        queries:
        - premium-renewed
        before: '0.1'
        after: '0.03'
        patch: sha256:...
        targets: sha256:...
        heldOut:
          profile: sha256:...
          largestGap: '0.08'
          tolerance: '0.1'
        versions:
          cma: ...
          sqlglot: ...
          duckdb: ...
```

The entry names the queries the value was fitted to, the value before and after, the digests of the patch
and of the profile that held the targets, and the held-out profile with its largest gap and the tolerance it
met. The calibrated bundle pins the profile that held its targets, so the privacy grade is the pinned
profile's, and the patch, being a declaration, enters no grade.

## Limits to know

- A fit runs the bundle once per candidate. The engine's ceiling is 64 evaluations, and
  `--max-evaluations` raises it to at most 4,096 for the command, for a block that declares none of its own.
  `--max-query-seconds` (up to 600) and `--max-query-memory` (up to 16GiB) raise the query ceilings the same
  way. A longer fit is a background job:

```text
fluxsim calibrate --fit declared --profile pinned.profile.json --held-out held-out.profile.json \
    --out calibrated --max-evaluations 512
```

- A fit moves what you declared and no more. If the held-out queries miss, the fix is in what the fit may
  move, or in a better pinned profile, never in the held-out profile, which the fit must not see.
- The queries' rates are one measure of fit. They sit beside the scorecard's other dimensions and do not
  replace them.
