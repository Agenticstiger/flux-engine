# Size, split and speed

Offline first: `fluxsim plan --help` and `fluxsim apply --help` are the authority for these options. The pages: [Size a run, split it and time it](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/how-to/size-and-split-a-run.md).

## `--scale F`

- It multiplies every emit's `rows`, the `records` bounds of its fidelity block, and the span of every integer or decimal key column that declares a range. A key span grows from its minimum: `min: "1", max: "1000"` at `--scale 10` becomes `min: "1", max: "10000"`.
- Related tables grow together and their keys still fit their rows, with two limits. Below 1, a key over two or more key columns loses values faster than rows. Above 1, a key with no column that grows with the rows keeps its values while its rows grow. A scale that leaves a key fewer values than rows is refused (FLX-3403, naming `--scale`).
- A table built from the run's events keeps the rows its events give it at any scale. The starter's only table is one, so scaling the starter changes nothing visible; say so rather than reporting a failure.
- A scale is a plain decimal above 0 and at most 1000000 (FLX-3405). `--scale 1` changes nothing: no `scale` entry in the plan, the same digests as always.
- The plan records the scale and `apply`, `replay` and `verify` read it back. `apply --run-id` refuses `--scale` (exit 2), since the plan has its own; this exit 2 is the engine's rule, not a sign that the engine differs from the skill. Give `--scale` to `plan`, or to `run`, or to `apply` without `--run-id` (a fresh plan).
- A scaled plan has its own digests: it is a different run, not a bigger copy of the first.
- Scale and shard need the current kernel, which a new plan takes; a plan that asks for the older kernel with `--kernel` refuses them.

```bash
fluxsim plan bundle --scale 3 --timings
fluxsim apply bundle --run-id RUN_ID
```

## `--shard I/N`

- `apply --run-id RUN_ID --shard I/N` makes the I-th of N contiguous shares of every emit's rows. `I` runs from 1 to `N` (FLX-3404).
- Each share is written as `emits/EXPOSE.shard-I-of-N.ndjson` in the run directory, with a `shard-I-of-N.json` that holds the row range, size and digest of each file.
- The shares joined in order, one file name at a time, are the bytes of the run's own stream. Never join with a glob: the whole stream's file would match too and its rows would be written twice.
- A share has no run digest and no receipt: it is part of a run, not a run. `verify` and `ship` refuse a directory that holds only shares (FLX-2109), and `--sink` refuses a share.
- An emit made from the run's own events has no row range to cut; it belongs whole to the first share.
- Shares can run on separate machines from the same plan; plan with `--no-stage` so each machine generates its own rows.
- To check a split run, apply the run whole once (no `--shard`) and compare each stream with the joined shares, or run `fluxsim verify bundle --run-id RUN_ID`.

```bash
fluxsim plan bundle --no-stage
fluxsim apply bundle --run-id RUN_ID --shard 1/3
fluxsim apply bundle --run-id RUN_ID --shard 2/3
fluxsim apply bundle --run-id RUN_ID --shard 3/3
fluxsim apply bundle --run-id RUN_ID
fluxsim verify bundle --run-id RUN_ID
```

## `--jobs`

- `--jobs N` is the most processes that may generate the rows of an emit declared by statistics: a number (never more than one per CPU) or `auto` for one per CPU. The default is the `FLUX_JOBS` environment variable, else 1.
- The run summary says how many processes were used. The rows and every digest are the same whatever the number, so it is a speed setting only; never change it to chase a different digest.
- A process takes at least 4096 rows, so a small emit uses one process whatever the number.
- Give `--jobs` to `plan` or `run`. A default plan stages its rows, and `apply --run-id` only commits the staged rows, so `--jobs` there counts only for a plan made with `--no-stage`.

```bash
fluxsim plan bundle --jobs auto
fluxsim apply bundle --run-id RUN_ID
```

## `--timings`

`--timings` prints the wall-clock seconds of each step (load, compile, simulate, each emit, framing, digests, writes). The numbers sit outside every digest and are recorded in no file of a run, so the bytes are the same with and without it. Use it to say where a slow run spent its time.
