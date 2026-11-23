---
title: "Size a run, split it and time it"
description: "Plan a run at a multiple of the bundle's size with --scale, generate it in shares with --shard, and see where the time went with --timings."
---

# Size a run, split it and time it

Use this page when a bundle declares a few thousand rows and you want a million, when one machine should not make all of them, or when you want to know which step of a run is slow. All three work on kernel 0.2.0 only, which a new plan takes; a plan that names `--kernel 0.1.0` refuses them.

## Make the run bigger or smaller

`--scale F` multiplies every emit's `rows`, the `records` bounds of its fidelity block, and the span of every integer or decimal key column that declares a range. A key span grows from its minimum, so `range: { min: "1", max: "1000" }` at `--scale 10` becomes `min: "1", max: "10000"`. The related tables of a bundle grow together, and their keys still fit their rows, with two limits. Below 1, a key over two or more key columns loses values faster than rows, because the scale cuts each column's range as it cuts the rows. Above 1, a key with no column that grows with the rows (an integer or decimal key column with a declared range, or a key into a parent's rows) keeps its values while its rows grow. A scale that leaves a key fewer values than rows is refused (`FLX-3403`, naming `--scale`). A table built from the run's events keeps the rows its events give it, and `--scale` leaves it as it is: the starter project's only table, `payment_recovery_moment`, is one, and it has the same rows at any scale. `--scale` multiplies what a bundle declares, and the rows of such a table are declared nowhere.

```text
$ fluxsim plan ./bundle --scale 10
$ fluxsim apply ./bundle --run-id <runId>
```

The plan records the scale, and `apply`, `replay` and `verify` read it back from there, so they run the size the plan was made at. `--scale 1` changes nothing: the plan has no `scale` entry and every digest is the one the bundle always had. A scale is a plain decimal number above zero and at most 1000000 (`FLX-3405`). It changes what is generated, so a scaled plan has its own digests.

## Make the run in shares

`--shard I/N` makes the I-th of N contiguous shares of every emit's rows. Each share is written beside the others, and joining them in order gives the bytes of the run's own stream:

```text
$ fluxsim plan ./bundle --no-stage
$ fluxsim apply ./bundle --run-id <runId> --shard 1/4
$ fluxsim apply ./bundle --run-id <runId> --shard 2/4
$ cat emits/store_sales.shard-1-of-4.ndjson emits/store_sales.shard-2-of-4.ndjson \
      emits/store_sales.shard-3-of-4.ndjson emits/store_sales.shard-4-of-4.ndjson > store_sales.ndjson
```

Run each share on its own machine from the same plan. A share writes `shard-I-of-N.json` with the row range, the size and the digest of each file, and no run digest and no receipt: it is part of a run, not a run. An emit that is made from the run's own events has no row range to cut, and belongs whole to the first share. `--shard` needs `I` from 1 to `N` (`FLX-3404`). The share number counts from 1: share `I` of `N` is what a TPC-H style generator calls step `I - 1` of `N` children (DuckDB's `dbgen(sf, children, step)` counts its step from 0; tpchgen-rs counts `--part` from 1).

A joined set of shares holds the run's rows, but it is not a run. It has no run digest and no receipt, so `verify` and `ship` refuse a run directory that holds only shares (`FLX-2109`): they read a run that `apply` wrote whole. To check a split run, apply the run whole once, without `--shard`, and compare each of its streams with the joined shares, or verify the whole run:

```text
$ fluxsim apply ./bundle --run-id <runId>
$ cmp .flux/runs/<runId>/emits/store_sales.ndjson store_sales.ndjson
$ fluxsim verify ./bundle --run-id <runId>
```

The share files stay in the run directory, in `emits/` beside the whole run's streams (`store_sales.ndjson` next to `store_sales.shard-1-of-4.ndjson`), and applying the run whole does not remove them. That is why the join above names its files one by one: a glob such as `store_sales*.ndjson` would pick up the whole stream as well and write its rows twice. `--sink` takes the run whole for the same reason, so a share is refused with it.

## See where the time went

`--timings` prints the wall-clock seconds of each step of the stage you ran: loading the bundle, compiling it, simulating the population, each emit, framing its rows, the digests, the seam attestations and the writes.

```text
$ fluxsim plan ./bundle --timings
timings (seconds, wall clock; outside every digest):
  load                    0.156
  compile                 0.013
  simulate                0.313
  emit store_sales        0.836
  ...
```

The numbers appear in the output and under `timings` in the `--json` envelope. No plan, receipt, digest or file of a run carries them, so a run's bytes are the same with and without the flag. An emit made by worker processes is one line: the time from handing its slices out to joining its lines.
