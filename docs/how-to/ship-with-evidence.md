---
title: "Ship with evidence"
description: "Pack a run into an evidence archive with fluxsim ship, pass the fidelity gate with a committed slice, sign the record, and use the JSON output in CI."
---

# Ship with evidence

Use this page to pack a run into an evidence archive, and to make the fidelity gate check the emitted rows against a slice of real data you hold. `fluxsim ship` runs the gate first and packs only if no emit is withheld.

## Ship the plain starter

The starter has no `fidelity` block, so the gate has nothing to check.

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
$ fluxsim run .
plan written to .flux/runs/205ba6e504c40820/plan.json (runId=205ba6e504c40820, planDigest=sha256:205ba6e504c408208fb15c5da5f96edac2f514606d6024bc19733c6c5cac90cc)
applied to .flux/runs/205ba6e504c40820 (runDigest=sha256:6c9de0409b3c2e98c435c3141f1728ff17511087cd3b39f1e87625fa5d238f06)
emitted payment_recovery_moment: 155 rows (scenario)
run 205ba6e504c40820 complete
```

These examples write the pack beside the bundle directory, so that directory keeps only its documents. Writing it inside, as the tutorial does with `-o out`, also works: `ship` leaves that output folder out of the archive.

`ship` packs the run directory as it holds it, and refuses a file the run did not write (`FLX-2123`). `sink-receipts.json` and `calibration-provenance.json` are carried as the directory holds them: nothing the run digest binds says whether the run wrote them, so they are not covered by the run digest.

```console
$ fluxsim ship . --run-id 205ba6e504c40820 -o ../out
[ok] packed ../out/demo.flux.tgz
     profile=core documents=9 files=38
     merkleRoot=sha256:...
     archive sha256:...
     in-toto statement: ../out/demo.attestation.json
...
shipped ../out/demo.flux.tgz
```

## Look inside

The output directory holds the archive and three JSON files: the manifest, a build statement and a runtime statement. Both statements are in-toto statements whose subject is the archive's SHA-256 digest.

```console
$ ls ../out
demo.attestation.json
demo.flux-manifest.json
demo.flux.tgz
demo.runtime-evidence.json
$ tar tzf ../out/demo.flux.tgz | grep -E 'EVIDENCE|manifest'
demo/.flux/runs/205ba6e504c40820/EVIDENCE.md
demo/flux-manifest.json
demo/run/EVIDENCE.md
```

`flux-manifest.json` lists every other file in the archive with its digest. `EVIDENCE.md` is the report. Read it straight from the archive.

```console
$ tar xzOf ../out/demo.flux.tgz demo/run/EVIDENCE.md
# Evidence: demo

## What This Bundle Is
...
## Conformance
...
## Reproducibility

reproducibility: not-checked

## Fidelity

- `payment_recovery_moment`: no fidelity block declared
- Signature: ...

## Population

There is no target to draw: this run was shipped without a profile.

## Privacy Grade

Run grade: **P0 (assumed)**.

Patterns: **P0 assumed**, no profile was pinned and every parameter is declared by hand; **P1 checked**, a profile of aggregates under the profiler's output checks, with no formal guarantee; **P3 differentially private marginals**, a profile that states one release under differential privacy and holds only noisy statistics; **P4 differentially private model**, a model from a generator plugin whose bundle declares the account of its training; **P5 learned model, no privacy account**, a model from a generator plugin with no such account, the weakest pattern. P2 is not assigned, and the numbers are stable. The grade is the weakest of the pinned profile's and every declared generator plugin's, and is one grade for the run; a grade for each column is a later refinement.

Declared hand-written: no profile was pinned, so the engine read no data; it cannot confirm that no data shaped the numbers a person typed. No privacy guarantee is stated.

| Expose | Column | Sensitivity | Minimum | Met |
|---|---|---|---|---|
| `payment_recovery_moment` | `subscriber_id` | none | any pattern | yes |
| `payment_recovery_moment` | `aborted_at` | none | any pattern | yes |
| `payment_recovery_moment` | `amount` | none | any pattern | yes |
| `payment_recovery_moment` | `market` | none | any pattern | yes |

## Column Provenance

This is what `apply` recorded for the run. The run's plan holds a digest of it, and `ship` checked the record against that digest; it does not work out again how each column was produced.

### `payment_recovery_moment`

4 columns: 4 copied.

- `subscriber_id` (STRING): copied from `subject.id` (named by the vertical's alias table)
- `aborted_at` (TIMESTAMP): copied from `event.time` (named by the vertical's alias table)
- `amount` (DECIMAL(18,4)): copied from `event.data.amount` (named by the vertical's alias table)
- `market` (STRING): copied from `subject.market` (named by the vertical's alias table)

## What Was Not Proven

This bundle asserts no privacy or re-identification claim about any source data, no fairness claim about any source system, and no claim about any system trained or tested using its output.

No validity claim is made (validity: not asserted): this run's predicted outcomes have not been measured against the world. In particular, the following are authored dials -- values this bundle's author declared, not values this engine fitted or measured: `temperature.campaignResponse`.

No column fell back to a type default: every column was produced by a declaration, a copy or a derivation.
```

The report records what was run and what was checked. It does not say the simulated customers match real ones. Its last section says so itself. The section before it, Column Provenance, says how each column was made, and Privacy Grade, before that, says which pattern the run's numbers rest on and what guarantee that carries; the archive also holds the same record as `run/column-provenance.json`, for a tool to read.

## Declare a fidelity block

Add a `fidelity` block to the emit. It lives in the Simulation under `extensions`, then `com.agentics.flux/1`, then `emits`, keyed by the expose id. This block asks for 64 rows, a mean amount within 50 of the real mean, and the same set of markets.

```yaml
# simulation.flux.yml
fluxVersion: "0.5.0"
kind: Simulation
id: sim-payment-recovery
name: Payment Recovery Simulation
metadata:
  owner:
    team: growth-lab
extensions:
  com.agentics.flux/1:
    emits:
      payment_recovery_moment:
        rows: 64
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 4, max: 100000 }
          columns:
            amount:
              range: { min: "1", max: "10000" }
              null: exclude
            market:
              support:
                values: ["IE", "GB", "FR", "DE"]
              maxCardinality: 8
              null: fail
            aborted_at:
              range: { min: "2026-07-01T00:00:00Z", max: "2026-09-30T23:59:59Z" }
              null: fail
          properties:
            - property: mean
              column: amount
              tolerance: { type: absolute, value: "50" }
            - property: support
              column: market
              tolerance: { type: count, max: 0 }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
spec:
  worldRef: q3-retention-world
  fidelity: deterministic
  seed: 42
  timeBounds:
    start: "2026-07-01T00:00:00Z"
    end: "2026-09-30T23:59:59Z"
  personaRefs: [persona-pragmatic-family]
  signalRefs: [sig-payment-aborted]
  moduleRefs: [module-payment-recovery]
  emits:
    - productRef: telco.gold.payment_recovery_moment
      exposeId: payment_recovery_moment
      fluidVersion: "^0.7.3"
      provenance:
        contractDigest: "sha256:b9b5ee575a8b313f6f72d039653d8be5d76e7ecc0012d7bd6ad2d37e0e60435d"
```

Run it again. The run id is new, so the later commands read it from the run directory.

```console
$ fluxsim run .
...
emitted payment_recovery_moment: 64 rows (generator)
run ... complete
```

Read that line twice. A fidelity block makes the emit generator-driven: its rows are drawn from the declared encoding and ranges, not from the World, the Persona and the Journey that produced the 155 rows of the first run, and the label says `generator` where it said `scenario`. The amounts here come from the range `1` to `10000`, which is why the slice below has such large ones. What the gate checks is therefore the generator's rows against your slice, and a pass says nothing about the simulated customers.

## See the gate refuse

Ship without a slice. The gate withholds the emit and writes nothing.

```console
$ fluxsim ship . --run-id $(ls -t .flux/runs | head -1) -o ../out-a   # exit 1
withheld: payment_recovery_moment
  - committed-slice.present: payment_recovery_moment declares a fidelity block; ship needs --committed-slice for it
nothing was written to the output directory
FLX-8001: withheld payment_recovery_moment: committed-slice.present: ...; to fix: pass --committed-slice, ...
more: fluxsim explain FLX-8001  https://agenticstiger.github.io/flux/engine/reference/errors/FLX-8001.html
```

## Write a slice

A slice is the committed real-data sample the check is measured against. The file holds one JSON object per line. The keys are the contract's column names. Decimals are strings, such as `"42.5000"`, because a JSON float is refused. The array-per-line form of the run's own `emits/*.ndjson` is refused too. Keep the file outside the bundle directory, since the loader refuses unknown files in it.

Start with four lines that are well formed but do not match the generated rows.

```console
$ cat > ../slice.ndjson <<'EOF'
{"subscriber_id": "s-0001", "aborted_at": "2026-07-04T09:12:00Z", "amount": "42.5000", "market": "IE"}
{"subscriber_id": "s-0002", "aborted_at": "2026-07-04T11:40:00Z", "amount": "18.0000", "market": "GB"}
{"subscriber_id": "s-0003", "aborted_at": "2026-08-05T08:03:00Z", "amount": "73.2500", "market": "FR"}
{"subscriber_id": "s-0004", "aborted_at": "2026-09-05T16:55:00Z", "amount": "9.9900", "market": "DE"}
EOF
$ fluxsim ship . --run-id $(ls -t .flux/runs | head -1) -o ../out-a --committed-slice payment_recovery_moment=file://$(dirname "$PWD")/slice.ndjson   # exit 1
withheld: payment_recovery_moment
  - properties.outcomes: 1 of 2 declared properties are outside their margins
  - property 000:mean:amount (mean of amount): outside the declared margin ...
nothing was written to the output directory
FLX-8001: withheld payment_recovery_moment: properties.outcomes: ...; to fix: change the numbers the bundle declares, ...
more: fluxsim explain FLX-8001  https://agenticstiger.github.io/flux/engine/reference/errors/FLX-8001.html
```

The mean amount in this slice is far from the generated mean, so the `mean` property is outside its margin. The `support` property passed.

## See the gate pass

In real use the slice is data you hold, and you never change it to make the gate pass. The rows below are made up, only to show what a pass looks like: they have a mean amount near the generated mean.

```console
$ cat > ../slice.ndjson <<'EOF'
{"subscriber_id": "s-0001", "aborted_at": "2026-07-04T09:12:00Z", "amount": "5150.0000", "market": "IE"}
{"subscriber_id": "s-0002", "aborted_at": "2026-07-04T11:40:00Z", "amount": "5250.0000", "market": "GB"}
{"subscriber_id": "s-0003", "aborted_at": "2026-08-05T08:03:00Z", "amount": "5200.0000", "market": "FR"}
{"subscriber_id": "s-0004", "aborted_at": "2026-09-05T16:55:00Z", "amount": "5270.0000", "market": "DE"}
EOF
$ fluxsim ship . --run-id $(ls -t .flux/runs | head -1) -o ../out-a --committed-slice payment_recovery_moment=file://$(dirname "$PWD")/slice.ndjson
[ok] packed ../out-a/demo.flux.tgz
...
shipped ../out-a/demo.flux.tgz
$ tar xzOf ../out-a/demo.flux.tgz demo/run/EVIDENCE.md | grep 'fidelity checked'
- `payment_recovery_moment`: fidelity checked, 2 declared properties within tolerance
```

The pass means the generated rows sit inside the declared margins against this slice. It says nothing about rows the slice does not hold.

## Sign the record

Without a key, the fidelity record is signed by a key made in memory for that one invocation, and the report says that it attributes the records to no operator. To sign with your own key, give `--signing-key` a file holding a 32-byte ed25519 seed, raw or hex. With a key and nothing else, the report calls the signature self-attested.

```console
$ openssl rand -hex 32 > ../signer.key
$ fluxsim ship . --run-id $(ls -t .flux/runs | head -1) -o ../out-b --signing-key ../signer.key --committed-slice payment_recovery_moment=file://$(dirname "$PWD")/slice.ndjson
[ok] packed ../out-b/demo.flux.tgz
...
$ tar xzOf ../out-b/demo.flux.tgz demo/run/EVIDENCE.md | grep Signature
- Signature: this run's fidelity records were signed with a supplied key and checked against the public half of that same key -- self-attested. ...
```

To have the gate check against a key a reader already trusts, give `--trust-key` the public half, raw or hex. The record carries it as `publicKey`.

```console
$ grep -o '"publicKey": "[0-9a-f]*"' .flux/runs/*/fidelity-record-payment_recovery_moment.json | grep -o '[0-9a-f]\{64\}' > ../trust.pub
$ fluxsim ship . --run-id $(ls -t .flux/runs | head -1) -o ../out-c --signing-key ../signer.key --trust-key ../trust.pub --committed-slice payment_recovery_moment=file://$(dirname "$PWD")/slice.ndjson
[ok] packed ../out-c/demo.flux.tgz
...
$ tar xzOf ../out-c/demo.flux.tgz demo/run/EVIDENCE.md | grep Signature
- Signature: this run's fidelity records were signed with a supplied key and checked against a trust anchor supplied separately.
```

A record signed by a different key is withheld.

```console
$ openssl rand -hex 32 > ../other.key
$ fluxsim ship . --run-id $(ls -t .flux/runs | head -1) -o ../out-d --signing-key ../other.key --trust-key ../trust.pub --committed-slice payment_recovery_moment=file://$(dirname "$PWD")/slice.ndjson   # exit 1
withheld: payment_recovery_moment
  - record.signature: the record's signature does not verify against any trusted key
nothing was written to the output directory
FLX-8001: withheld payment_recovery_moment: record.signature: ...; to fix: ship with the signing key that --trust-key names, ...
more: fluxsim explain FLX-8001  https://agenticstiger.github.io/flux/engine/reference/errors/FLX-8001.html
```

## Score against a pinned profile

A Simulation can pin a profile, a document of aggregate statistics computed where the real data lives ([PROFILE-SPEC.md](../spec/PROFILE-SPEC.md)), in place of handing `ship` a slice. Hand the profile to every stage of the run with `--profile`. The profile stays outside the bundle directory, as the pack does here:

```text
fluxsim run . --profile ../source.profile.json --verify --ship -o ../out
```

[Score a run against a profile](./score-a-run-against-a-profile.md) writes such a profile, pins it and runs this end to end. `run --verify --ship` is the one-pass path from a bundle to a pack: the rows are drawn once.

`verify` and `ship` then compare each emit's declared properties with the profile and score the run, per property, per table, per dimension and overall ([SCORECARD-SPEC.md](../spec/SCORECARD-SPEC.md)). `verify` prints the scores under its legs:

```text
  scorecard: overall 0.9871 against profile sha256:... (covers 3 of 5 columns, 0 of 1 relationship)
    dimensions: structure 1, marginals 0.9744; not measured: joint, crossTable, temporal, invariants, queries, defects
    table payment_recovery_moment: 0.9871 (2 of 2 properties scored); covers 3 of 5 columns, 0 of 1 relationship
    shape: no flags in 3 profiled columns (no count table to test)
```

Under the scores come the shape flags ([SCORECARD-SPEC.md](../spec/SCORECARD-SPEC.md) §6). Every column the profile describes is checked, declared or not, for cells the run leaves empty, one value holding an unlikely share of the rows, a constant column, and a distinct count far from the one the profile predicts. They are in `run/scorecard.json` and `EVIDENCE.md` too. They withhold nothing unless the Simulation's `scorecard` block declares `shape: { maxFlags: n }`, which withholds a run with more than `n` flags.

A property outside its margin withholds the ship, and so does a missed threshold. The Simulation declares thresholds beside the pin:

```yaml
extensions:
  com.agentics.flux/1:
    profile:
      digest: sha256:af97633046a8c9621d32fec595dd02672bc10e3fabd98c846a2ffcd713e89361
    scorecard:
      thresholds:
        overall: { mustBeGreaterOrEqualTo: "0.95" }
      coverage:   # a floor on how much of the contract the score speaks for
        columns: "0.8"
```

A shipped pack carries the scores as `run/scorecard.json`, and `EVIDENCE.md` summarises them. A run whose Simulation pins a profile is refused by `ship` without it (`FLX-3109`), and with another profile (`FLX-3110`).

## Say where each number comes from

A Simulation can declare what each number its run reads rests on: a statistic of the profile it pins, a place in the FLUID contract it emits from, an assumption with its reason, or a default ([PARAMETER-REGISTER-SPEC.md](../spec/PARAMETER-REGISTER-SPEC.md)). Each entry names a document by its id and the numbers in it by a JSON Pointer; a pointer to a mapping names every number under it, and a number no entry names is a default.

```yaml
extensions:
  com.agentics.flux/1:
    parameters:
      - document: q3-retention-world
        pointer: /spec/population/size
        source: assumption
        reason: Sized for a quick look, not taken from any subscriber count.
      - document: persona-pragmatic-family
        pointer: /spec/traits
        source: assumption
        reason: The growth team's persona workshop.
```

`plan`, `verify` and `ship` then report the shares on one line, here for the starter:

```text
parameters: 20 declared -- 0 from a profile, 9 assumed, 11 default
```

The pack carries the register as `run/parameter-register.json`, and `EVIDENCE.md` lists every assumption and its reason under "What Was Not Proven". A number from the profile cites a statistic by a pointer into the pinned profile and its definition, `statistic: { pointer: /tables/payment_recovery_moment/rowCount }` for a row count, and `plan` refuses a citation the profile does not publish ([FLX-3113](../reference/errors/FLX-3113.md)).

## Use it in CI

Put `--json` before the command name to get one JSON document. The exit status is the gate: 0 when the pack was written, 1 when an emit was withheld. In the JSON, `ok` and `exitCode` repeat it, `artifacts.withheld` lists the withheld exposes, and `artifacts.archive` is the archive path on success.

```console
$ fluxsim --json ship . --run-id $(ls -t .flux/runs | head -1) -o ../out-e > ../report.json   # exit 1
$ grep -E '"(ok|exitCode)"' ../report.json
  "exitCode": 1,
  "ok": false,
```

See the [`ship` reference](../reference/cli/ship.md), [`FLX-8001`](../reference/errors/FLX-8001.md) and [the evidence pack](../explanation/the-evidence-pack.md).
