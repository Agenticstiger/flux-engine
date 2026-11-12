---
title: "First evidence pack in five minutes"
description: "Create a small FLUX project, run it, verify it and ship an evidence pack, then change one number to see what moves and what does not."
---

# First evidence pack in five minutes

You will run a simulation of 500 customers, check the run, pack the result into an
evidence archive, and then change one number to see what moves. Nothing here needs an
account, a network service or a checkout of the specification repository. Most of the
five minutes is reading.

You need [uv](https://docs.astral.sh/uv/) and a terminal.

## 1. Install the command

<!-- skip: next "installs the published package, so it cannot test this checkout; the First run workflow runs init and run from a built wheel" -->
```console
$ uv tool install flux-sim-engine
```

That puts one command, `fluxsim`, on your `PATH`. (With `pip`, install `flux-sim-engine`
into a virtual environment instead.) To try it without installing anything, write
`uvx --from flux-sim-engine` in front of the command instead, for example
`uvx --from flux-sim-engine fluxsim init demo`. Every command on this page works
either way.

```console
$ fluxsim --version
0.4.0
```

## 2. Create a project

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
next:
  cd demo
  fluxsim run . --verify --ship -o out
$ cd demo
```

`init` writes ten small files and never overwrites anything: it refuses a directory that
already holds files. Together they are a *bundle*, the unit the engine runs:

| File | What it is |
|---|---|
| `simulation.flux.yml` | The run itself: the seed, the time window, and the FLUID contract it emits rows for. |
| `world.flux.yml` | The population: 500 customers, in which lifecycle state, in which markets. |
| `persona.flux.yml` | A kind of customer, with traits drawn from distributions. |
| `signal.flux.yml`, `detector.flux.yml`, `journey.flux.yml`, `taxonomy.flux.yml`, `treatment.flux.yml`, `module.flux.yml` | The behaviour: a payment is aborted, a detector spots two in fifteen minutes, a journey says what the business does and how customers react. The module ties them together. |
| `payment-recovery.fluid.yml` | The FLUID contract. The rows come out in its shape. |

## 3. Run it

```console
$ fluxsim run . --verify --ship -o out
plan written to .flux/runs/8dec55892096169f/plan.json (runId=8dec55892096169f, planDigest=sha256:8dec55892096169f5b6a9281a379f5dc9c085418ad2d22d52543a34835c26bb3)
applied to .flux/runs/8dec55892096169f (runDigest=sha256:c4bb981d99467e59f8a89c546d59a35fb35f973c3abe77d70b2af1174604915f)
emitted payment_recovery_moment: 154 rows (scenario)
reproducible: not checked | validity: not asserted
  golden: not checked (no golden baseline recorded yet -- record one with `--update-golden --reason "<why>"`)
  artifacts: every run-digest component reproduces
  rows: ok
  rfc-04 vectors: 0 failure(s)
  fidelity payment_recovery_moment: not declared
[ok] packed out/demo.flux.tgz
     profile=core documents=9 files=...
     merkleRoot=...
     archive sha256:...
     in-toto statement: out/demo.attestation.json
     sign it: ...
          or: ...
shipped out/demo.flux.tgz
run 8dec55892096169f complete; shipped to out
```

Read it from the top.

- **plan** records what the run will do and gives it an id. The `runId` is the front of
  the plan's digest, so the same bundle always gets the same id.
- **applied** writes the run to `.flux/runs/<runId>/`. The `runDigest` is a fingerprint
  of everything the run produced.
- **emitted** is the point of it all: 154 rows in the shape of the FLUID contract.
- **verify** checks the run on five legs, one line each. Two things are kept apart on
  purpose. `reproducible` compares the run's digest with a baseline you record, and here
  it says `not checked` because none has been recorded yet; step 6 shows the check that
  runs the plan again. `validity` asks whether the scenario describes the real world, and
  the engine never claims that: it says `not asserted`.
- **ship** packs the bundle, the run and a manifest into `out/demo.flux.tgz`, with two
  in-toto statements beside it.

## 4. Look at the rows

```console
$ head -3 .flux/runs/8dec55892096169f/emits/payment_recovery_moment.ndjson
["sub-e19b5b0ae87789b0","2026-07-01T20:46:18.581Z","40.5500","mkt-b"]
["sub-e19b5b0ae87789b0","2026-07-01T20:55:26.840Z","30.5500","mkt-b"]
["sub-3ee1d4f847ee4a44","2026-07-01T23:51:54.082Z","48.4600","mkt-a"]
```

One row per line, in the contract's column order: `subscriber_id`, `aborted_at`,
`amount`, `market`. Each row is one firing of the detector. The first two belong to the
same subscriber, nine minutes apart.

## 5. Read what it does not claim

Every run that ships writes a report. Its last section is the one to read first.

```console
$ grep '^## ' .flux/runs/8dec55892096169f/EVIDENCE.md
## What This Bundle Is
## Conformance
## Reproducibility
## Fidelity
## What Was Not Proven
$ sed -n '/^## What Was Not Proven/,$p' .flux/runs/8dec55892096169f/EVIDENCE.md
## What Was Not Proven

This bundle asserts no privacy or re-identification claim about any source data, no fairness claim about any source system, and no claim about any system trained or tested using its output.

No validity claim is made (validity: not asserted): this run's predicted outcomes have not been measured against the world. In particular, the following are authored dials -- values this bundle's author declared, not values this engine fitted or measured: `temperature.campaignResponse`.
```

The pack records what was run and what was checked. It does not say the simulated
customers behave like real ones. [The evidence pack](../explanation/the-evidence-pack.md)
explains what each section is for.

## 6. Run it again and get the same answer

`replay` runs a recorded plan again and refuses unless it reaches the digest you ask for.

```console
$ fluxsim replay . --run-id 8dec55892096169f --assert-digest sha256:c4bb981d99467e59f8a89c546d59a35fb35f973c3abe77d70b2af1174604915f
replay of 8dec55892096169f matches asserted digest sha256:c4bb981d99467e59f8a89c546d59a35fb35f973c3abe77d70b2af1174604915f
```

Ask for a digest that is not the one it produces and it says so, with exit status 1:

```console
$ fluxsim replay . --run-id 8dec55892096169f --assert-digest sha256:0000000000000000000000000000000000000000000000000000000000000000   # exit 1
FLX-2106: a replay produced a different run digest than the one asserted -- ...
more: fluxsim explain FLX-2106  ...
```

That is the determinism promise as a command. [Determinism and the seed
tree](../explanation/determinism-and-the-seed-tree.md) says how it is kept.

## 7. Change one number

The seed is in `simulation.flux.yml`. Change it from 42 to 43. The backup file `sed`
leaves behind has to go: next to the FLUX and FLUID documents (and `flux.lock` and a pack
manifest) a bundle directory may hold no other file, and the engine refuses one
([FLX-3108](../reference/errors/FLX-3108.md)).

```console
$ sed -i.bak 's/^  seed: 42$/  seed: 43/' simulation.flux.yml
$ rm simulation.flux.yml.bak
$ fluxsim run .
plan written to .flux/runs/273f33609fdde017/plan.json (runId=273f33609fdde017, ...)
applied to .flux/runs/273f33609fdde017 (runDigest=sha256:1f290e23d92ce6e0dfc6b73f67f0f7ec1b30f52f3bfdf024ee6129f9733a7c9f)
emitted payment_recovery_moment: 153 rows (scenario)
run 273f33609fdde017 complete
```

Everything moved: a new run id, a new digest, 153 rows. The first run is still on disk,
but it no longer belongs to this bundle:

```console
$ fluxsim verify . --run-id 8dec55892096169f   # exit 1
FLX-2101: plan recorded sha256:e8c40bd3ec5da6733dd64b242cbce42f4e6a81d263456118a0382c7fdc1c033a, bundle is now sha256:...
more: fluxsim explain FLX-2101  ...
```

A run is tied to the exact bytes of the bundle that made it. Put the seed back and the
old id works again.

## Where to go next

- Make the customers your own: [Model your customers](../how-to/model-customers.md).
- Add behaviour: [Add a journey](../how-to/add-a-journey.md).
- Change the contract the rows follow: [Use the FLUID seam](../how-to/use-the-fluid-seam.md).
- Hand the pack to someone else: [Ship with evidence](../how-to/ship-with-evidence.md).
- Understand the steps you just ran: [The six-step loop](../explanation/the-six-step-loop.md).
- Look up a command or a code: [the command line](../reference/cli/README.md) and
  [the error codes](../reference/errors/README.md).
