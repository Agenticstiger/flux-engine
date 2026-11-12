---
title: "Determinism and the seed tree"
description: "Why the same FLUX bundle and seed give the same bytes: path-addressed seeds, fixed-cost draws, canonical JSON, exact arithmetic, and what the input digest does and does not cover."
---

# Determinism and the seed tree

The FLUX Simulation Engine makes one promise about a run: the same bundle and the same seed give the same bytes. This page explains how the kernel keeps that promise, how you can check it, and where it stops.

## The seed tree

A simulation draws a lot of random-looking values: a trait for each customer, the time of each event, the outcome of each reaction. If those values came from one shared generator, the result would depend on the order of the calls. Add one customer and every later draw would shift.

The engine does not share a generator. Every value comes from a stream addressed by a path. A path is a list of names, such as the world, the subject's ordinal and the trait. The stream for a path depends on that path and on a root, and on nothing else. It does not depend on how many streams were made before it, or on which one the scheduler reached first.

The root is a SHA-256 hash of three things: a version tag, `H`, the seam digest, and the seed. `H` is a fingerprint of where the simulation meets its FLUID contract: the contract, the emit entry, any fidelity block the emit declares, and the ordered column names (see [the seam](./the-seam.md)). Two consequences follow.

- Changing any of those changes every stream. A run against an edited contract, or an edited emit entry or column list, is a different run, not the same run with other columns.
- Adding a document that nothing references, or a consumer on a new path, changes no existing stream. The kernel's tests include this property.

Each stream is a PCG64 generator from numpy, started from a seed sequence built from the hash of root and path. The engine uses numpy only as a source of raw 64-bit words. It calls no numpy distribution function on anything that is hashed.

Every path a run uses is written, sorted, to `seed-paths.ndjson` in the run directory. The plan records the digest of that file as `seedTreeDigest`. A run that reaches a path the plan did not expect stops with `FLX-2201`.

## Draws with a fixed cost

A sampler that loops until it likes its value consumes a number of words that depends on the values. That moves every later draw in the same stream. The kernel's samplers take a fixed number of words, set by their arguments and never by what they draw. A categorical draw uses one word. A bounded integer always draws its full budget of 64 words, even when the first would have done. There are no rejection loops.

## Canonical bytes

Digests are taken over bytes, so the bytes must be unique. The engine writes JSON in the canonical form of RFC 8785. It first normalises every string, and every key, to Unicode NFC, so two spellings of one word hash alike. It refuses any floating-point number in a hashed object (`FLX-1001`). Decimals are written as strings with their scale, such as `"42.0000"`, never as JSON numbers.

## No floating point in anything hashed

Probabilities and continuous values are exact decimals. Comparisons are exact integer or fraction arithmetic. The inverse normal distribution is computed in Python's `decimal` module at 28 digits, whose operations are correctly rounded, so the digits do not depend on the platform's maths library. Statistics for the fidelity check live on an integer grid with a declared number of fractional bits, and the quantiser rounds toward minus infinity.

Splitting a population into cohorts uses the largest-remainder method on exact fractions. Each cohort gets the floor of its share, and the leftover units go to the largest fractional parts. Ties are broken by the NFC-sorted name, never by the seed. So a different seed never changes cohort sizes.

## Order and time

Time in the kernel is an integer number of milliseconds from the start of `timeBounds`. No wall clock is read. Every scheduled item has a key made of the time, a fixed phase, the subject's ordinal, the producing document's ordinal and a counter. The key is unique, the queue orders by it, and the scheduler runs in one thread. Mappings are walked in sorted key order.

The kernel's tests include six deliberately broken kernels that must fail the property suite: a spawn-ordered seed tree, float-accumulating remainders, a scheduler that iterates a set, a wall-clock timestamp, a random event id, and a row writer that sorts object keys.

## More than one job

`--jobs N`, or the `FLUX_JOBS` environment variable, lets several processes generate the rows of an emit that declares statistics. `--jobs auto` uses one per CPU the process may run on; in a container with a CPU quota, give the number instead. Each cell's stream is addressed by its row and its column, never by the order the generator reaches it, so a slice of rows is the same rows in whichever process it is made. Each worker generates, renders and frames a slice. The run joins the framed lines in row order, and those lines are what the output digest covers. The rows, the digests and every file the run writes are therefore the ones one job gives; only the time it takes changes, and the run summary, which says how many processes made an emit when there was more than one. A small emit stays in one process, because starting a worker costs about as much as generating a few thousand rows, and a run never uses more than one worker per CPU.

## What the input digest covers

The input digest is the digest of the files that went in. ADR 0006 settles which files those are. A bundle directory holds inputs and products and nothing else.

- Inputs are the top-level `*.flux.yml` and `*.flux.yaml` documents, the `*.fluid.yml` and `*.fluid.yaml` contracts, and `flux.lock`. Each is hashed by its raw bytes.
- Products are `flux-manifest.json` and its detached signature. They are recognised and not hashed, because a manifest is computed from the inputs.
- Any other regular file in the directory ends the command with `FLX-3108`, naming the file. There is no flag to hash it anyway.

The selection does not descend into directories. The `.flux` state directory and an output directory are therefore outside the input digest.

What is not covered: file names and layout beyond those top-level patterns, comments and whitespace only as far as they change the raw bytes (a comment edit does change the digest), and anything a document points at but the engine does not read. ADR 0006 notes that `Playback.sourceStream` and `Signal.dataschema` can hold file URIs and that no engine code reads either today. If a document ever starts to read a file, that file has to become an input.

## Check it yourself

Run the starter project and read the run's seed addresses.

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
$ fluxsim run .
plan written to .flux/runs/8dec55892096169f/plan.json (runId=8dec55892096169f, planDigest=sha256:8dec55892096169f5b6a9281a379f5dc9c085418ad2d22d52543a34835c26bb3)
applied to .flux/runs/8dec55892096169f (runDigest=sha256:c4bb981d99467e59f8a89c546d59a35fb35f973c3abe77d70b2af1174604915f)
emitted payment_recovery_moment: 154 rows (scenario)
run 8dec55892096169f complete
$ head -2 .flux/runs/8dec55892096169f/seed-paths.ndjson
["sim","sim-payment-recovery","signal","sig-payment-aborted","subject",0,0]
["sim","sim-payment-recovery","signal","sig-payment-aborted","subject",0,1]
$ grep -c . .flux/runs/8dec55892096169f/seed-paths.ndjson
2795
$ grep -o '"seedTreeDigest": "[^"]*"' .flux/runs/8dec55892096169f/plan.json
"seedTreeDigest": "sha256:92c35178b00fd7c4f034b1546dcbad41a1f238c6b64251e63ae5fc1c2741291b"
```

The direct check is `fluxsim replay`. It re-executes the recorded plan and refuses unless the run digest it produces is the one you assert.

```console
$ fluxsim replay . --run-id 8dec55892096169f --assert-digest sha256:c4bb981d99467e59f8a89c546d59a35fb35f973c3abe77d70b2af1174604915f
replay of 8dec55892096169f matches asserted digest sha256:c4bb981d99467e59f8a89c546d59a35fb35f973c3abe77d70b2af1174604915f
```

A wrong digest is refused with `FLX-2106`, and the message shows both digests.

```console
$ fluxsim replay . --run-id 8dec55892096169f --assert-digest sha256:0000000000000000000000000000000000000000000000000000000000000000   # exit 1
FLX-2106: a replay produced a different run digest than the one asserted -- asserted sha256:0000000000000000000000000000000000000000000000000000000000000000, replay produced sha256:c4bb981d99467e59f8a89c546d59a35fb35f973c3abe77d70b2af1174604915f
more: fluxsim explain FLX-2106  https://agenticstiger.github.io/flux/engine/reference/errors/FLX-2106.html
```

Run the same bundle into a second, separate state directory. The run id, the run digest and the emitted rows come out the same.

```console
$ fluxsim run . --state-dir ../other-state
plan written to ../other-state/runs/8dec55892096169f/plan.json (runId=8dec55892096169f, planDigest=sha256:8dec55892096169f5b6a9281a379f5dc9c085418ad2d22d52543a34835c26bb3)
applied to ../other-state/runs/8dec55892096169f (runDigest=sha256:c4bb981d99467e59f8a89c546d59a35fb35f973c3abe77d70b2af1174604915f)
emitted payment_recovery_moment: 154 rows (scenario)
run 8dec55892096169f complete
$ cmp .flux/runs/8dec55892096169f/emits/payment_recovery_moment.ndjson ../other-state/runs/8dec55892096169f/emits/payment_recovery_moment.ndjson
```

## What legitimately differs between runs

Two runs of one bundle are not byte-identical in every file. The digests cover the behaviour, and the files below carry the clock or a key.

```console
$ diff -rq .flux/runs/8dec55892096169f ../other-state/runs/8dec55892096169f   # exit 1
Files .flux/runs/8dec55892096169f/lineage.ndjson and ../other-state/runs/8dec55892096169f/lineage.ndjson differ
Files .flux/runs/8dec55892096169f/plan.json and ../other-state/runs/8dec55892096169f/plan.json differ
```

- The `generated_at` field in `plan.json`. The plan digest masks it.
- The `eventTime` of each lineage event in `lineage.ndjson`. It is read from the wall clock when the event is written.
- The generation time in `EVIDENCE.md` and in the runtime statement of an evidence pack, and with them the archive's own SHA-256 and Merkle root.
- The key that signs a fidelity record when you do not pass `--signing-key`. It is made for each invocation.
- Anything under the output directory you chose.

## What CI checks across systems

The workflow in `.github/workflows/ci.yml` runs the pinned-digest tests on every cell of its test matrix. They compare the plan, the run digest, the component digests and one-byte differentials against stored values. On a pull request or a push to main the cells are Ubuntu with Python 3.11, 3.12 and 3.13, plus one macOS cell on 3.12 that runs only the determinism set. On the weekly schedule and on release tags, all six combinations of Ubuntu and macOS with those three Python versions run the fast and the slow suites.

That is what is checked. It is stored digests on two operating systems and three Python versions, each cell with the one numpy that its install resolved within the range the project allows. No other operating system and no Windows runner are tested. Passing those tests shows the digests did not move on those machines. It does not show the engine is correct, and it does not show a run matches the real world. Whether the samplers draw from the distributions they declare, and whether the fidelity check accepts and withholds when it should, is measured separately by [the accuracy program](../accuracy/DESIGN.md). For what the evidence pack records, see [the evidence pack](./the-evidence-pack.md) and [the six-step loop](./the-six-step-loop.md).
