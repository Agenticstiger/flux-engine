# ADR 0015: one generation per run

## Status

Accepted 2026-11-21. `plan` stages its execution under the run directory by
default, and `apply --run-id` commits the stage instead of generating the rows
again. `plan --no-stage` keeps the plan that writes only `plan.json`.

## Context

`fluxsim plan` runs the whole pipeline in memory, as `apply` will, so that it
can record every digest (KERNEL-SPEC section 11). `fluxsim apply --run-id` then
ran the pipeline a second time to write the run directory. `fluxsim run` already
ran it once, because it hands the plan's execution to `apply`. So the same
bundle cost two generations of every row when it was planned and applied in two
commands, which is how a team reviews a plan before it applies it, and one
generation when it was run in one.

Nothing in the first execution was lost: the rows, the framed lines, each
stream's digest, the trace and the receipt were all in memory when `plan`
returned, and `plan` threw them away.

Borrow-before-build search: "terraform saved plan file stale plan error apply"
and "content-addressed output verification hash build system" (2026-11-21);
fetched https://developer.hashicorp.com/terraform/cli/commands/plan and
https://developer.hashicorp.com/terraform/cli/commands/apply. Terraform's saved
plan is the closest pattern: `apply <planfile>` executes a reviewed plan without
planning again, the plan is bound to the state it was made against, and its
documentation asks that saved plan files be treated as potentially sensitive
artefacts. It binds no output digest. Nix's fixed-output derivations
(https://nix.dev/manual/nix/2.28/store/derivation/outputs/content-address) are
the case where an output hash is declared before the build and checked after
it. The stage here adapts the first pattern: the plan is applied without a
second generation, and every staged file is checked against its recorded digest
and the receipt before it is committed.

## Decision

### 1. Plan stages what apply would write

By default `plan` writes every file `apply` would write, with the function
`apply` writes them with, under `<run>/.stage/`. The run directory is named by
the plan's digest, so the stage is found only by the plan it was made for.
`stage.json` is written last. It records the plan digest, the input digest, the
kernel, the digest and size of every staged file (the receipt last) and, for each
emit, its header and `outputDigest`. A plan killed part-way leaves no record, and
`apply` then generates as it did before.

`plan.json` and `planDigest` do not change: the stage is a file beside them, not
a field in them, and a bundle's digests are the ones they were.

### 2. Apply checks the stage, then commits it

`apply --run-id` generates nothing when a stage exists. Before it writes a byte
to the run directory it checks:

- the stage names this run, this plan digest, this input digest and this kernel;
  a stage that names another is not read, and `apply` generates as before;
- the bundle's bytes are the ones the plan hashed: `inputDigest` is worked out
  from the bundle again, and a moved bundle is `FLX-2101`, as for an apply that
  generates;
- every staged path is one plain name under the run directory (or `emits/NAME`),
  read without following a link, and its bytes have the digest and size recorded
  (`FLX-2120`);
- each stream, digested as `outputDigest` is, equals the digest the staged
  receipt reports, and its line count is the receipt's row count;
- the two records the plan binds, the column record and the parameter register,
  hash to the plan's digests (`FLX-2115`, `FLX-2116`).

The bytes written to the run directory are the bytes that were checked; the stage
is not read a second time. The receipt is written last, as it always was, and the
stage is removed once it is down. The files of a run committed from a stage are
byte for byte the files of a run `apply` generated, but `lineage.ndjson`, which
carries the time the events were made.

### 3. What does not stage

`fluxsim run`, and an `apply` with no `--run-id`, hold the execution in memory
and write it once, so they plan with no stage. An `apply --run-id` with
`--strict-coverage`, or with another `--simulation-id` or `--zone` than the plan
was made with, does not use the stage and generates.

## Consequences

`plan` followed by `apply --run-id` costs one generation. `plan` writes the run
files a second time into the stage and `apply` copies them, so plan alone is a
little slower and uses disk for a stage until the run is applied. `--no-stage`
avoids both for a plan that is only read.

A stage is trusted only as far as the state directory is. `apply` commits staged bytes
that the run directory attests about itself: the manifest, the receipt and the streams
are all in the stage, and `plan.json` binds no output digest. The checks below catch a
stage that was damaged, truncated or swapped for a link, and a stream that does not match
its own receipt. They do not catch someone who can write the whole stage consistently. So
when a state directory crosses a trust boundary between `plan` and `apply` (planned on one
machine or in CI and applied elsewhere), plan with `--no-stage`, or plan again where you
apply: `apply` then generates the rows from the bundle itself. A stage is also used only by
an `apply` that names the same `--simulation-id` and `--zone` as the plan, and a plan made
with `--no-stage` removes an older stage of its run.

A stage is an attack surface: it is a directory of files an apply will commit. It
is written and read with the no-follow helpers, a link or a path that leaves the
run directory is refused, and what is committed is what hashed to the record. A
person who can write the stage and its record together can still forge a run, as
a person who can write the run directory could before; the receipt's
`outputDigest` and the run digest are what `verify` and `ship` read back, and they
are checked against the streams there as before.

## Alternatives

Keeping the rows in the plan file would make `plan.json` as large as the run.
Having `apply` look for a cached execution by digest anywhere in the state
directory would let one run's rows be committed under another's name. Making
`plan` write the whole run directory and `apply` only the receipt would leave a
run directory that looks applied before it is.
