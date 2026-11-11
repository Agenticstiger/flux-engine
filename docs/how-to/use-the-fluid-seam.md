---
title: "Use the FLUID seam"
description: "Change a FLUID contract, copy its new digest into the Simulation, and check the emitted rows against the new columns."
---

# Use the FLUID seam

Goal: change the columns a run emits. The Simulation refers to a FLUID contract and never restates it. Its `emits[]` entry names the contract (`productRef`), the stream in it (`exposeId`), the FLUID version range (`fluidVersion`) and the contract's digest (`provenance.contractDigest`). The digest makes a contract change a deliberate act. For the contract format, see the [FLUID Spec](https://open-data-protocol.github.io/fluid/).

## 1. Start a project

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
```

## 2. Add columns to the contract

Replace `payment-recovery.fluid.yml`. It has two new columns, `priceSensitivity` and `plan_code`. Both are optional.

```yaml
# payment-recovery.fluid.yml
fluidVersion: "0.7.5"
kind: DataProduct
id: telco.gold.payment_recovery_moment
name: Payment Recovery Moment
metadata:
  owner:
    team: growth-lab
exposes:
  - exposeId: payment_recovery_moment
    kind: stream
    contract:
      schema:
        - { name: subscriber_id, type: STRING, required: true, tags: [identifier] }
        - { name: aborted_at, type: TIMESTAMP, required: true }
        - { name: amount, type: "DECIMAL(18,4)" }
        - { name: market, type: STRING }
        - { name: priceSensitivity, type: STRING }
        - { name: plan_code, type: STRING }
    binding:
      platform: kafka
      format: kafka_topic
      location:
        topic: billing.payment-recovery.v1
```

## 3. See the digest mismatch

The Simulation still carries the old digest, so validation refuses the bundle and prints the new one:

```console
$ fluxsim validate .   # exit 1
FLX-3105: bundle does not validate, so its seam cannot be resolved --...
  sim-payment-recovery: spec.emits[0]: provenance.contractDigest mismatch ... declared sha256:b9b5ee575a8b313f6f72d039653d8be5d76e7ecc0012d7bd6ad2d37e0e60435d, bundle contract is sha256:90324eff47f5f673c2838ecb6d23efa90739ae37ba43d69c920ca818559913dc
more: fluxsim explain FLX-3105  https://agenticstiger.github.io/flux/engine/reference/errors/FLX-3105.html
```

Read the contract change before you accept it. Then copy the digest into the Simulation.

## 4. Update the Simulation

Replace `simulation.flux.yml`. Only `contractDigest` differs from the starter.

```yaml
# simulation.flux.yml
fluxVersion: "0.5.0"
kind: Simulation
id: sim-payment-recovery
name: Payment Recovery Simulation
metadata: { owner: { team: growth-lab } }
spec:
  worldRef: q3-retention-world
  fidelity: deterministic
  seed: 42
  timeBounds: { start: "2026-07-01T00:00:00Z", end: "2026-09-30T23:59:59Z" }
  personaRefs: [persona-pragmatic-family]
  signalRefs: [sig-payment-aborted]
  moduleRefs: [module-payment-recovery]
  emits:
    - productRef: telco.gold.payment_recovery_moment
      exposeId: payment_recovery_moment
      fluidVersion: "^0.7.3"
      provenance:
        contractDigest: "sha256:90324eff47f5f673c2838ecb6d23efa90739ae37ba43d69c920ca818559913dc"
```

## 5. Run and read the rows

```console
$ fluxsim run .
plan written to ...
applied to ... (runDigest=sha256:...)
emitted payment_recovery_moment: 170 rows (scenario)
run ... complete
$ head -n 3 .flux/runs/*/emits/payment_recovery_moment.ndjson
["sub-a28ccf4ac08ce0ea","2026-07-01T02:51:43.755Z","44.6700","mkt-a","medium",null]
["sub-dda21178cadba282","2026-07-01T11:44:57.171Z","53.4500","mkt-a","medium",null]
["sub-b08aa68d8a67f740","2026-07-01T21:35:05.552Z","42.0300","mkt-c","medium",null]
```

Each line is a JSON array in the order of the contract's columns, not an object. The `rows: ok` line of `verify` checks the rows against the contract:

```console
$ fluxsim verify . --run-id $(ls .flux/runs)
reproducible: not checked | validity: not asserted
...
  rows: ok
...
```

## What the engine does with a column

- **Types.** The engine renders each value as the column's declared type. `DECIMAL(18,4)` and `TIMESTAMP` values are strings, as above. A value that does not fit, such as a decimal in an `INT` column, fails the run with FLX-3003.
- **Where a value comes from.** For this contract the engine fills `subscriber_id`, `aborted_at`, `amount` and `market` through the telco alias table: the customer id, the moment of the event, the event's amount and the customer's market. A column that is not in that table is filled when its name matches an attribute of the event or the customer. `priceSensitivity` matches a Persona trait, so it has a value. List the names with `fluxsim expr model --bundle .`.
- **A column with no source.** `plan_code` has none, so every row has `null` for it. This is allowed only because the column is optional. In a test with `required: true` on it, planning stopped with FLX-3302 and no run was written.
- **`tags: [identifier]`.** The tag marks a column as an identifier. It does not give the column values: in a test, a second tagged column with no source was `null`. `subscriber_id` has values because the engine maps it to the customer id.

See [the seam](../explanation/the-seam.md) for the design, and [FLX-3105](../reference/errors/FLX-3105.md) for the error.
