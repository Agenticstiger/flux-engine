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
coverage: payment_recovery_moment: 1 of 6 columns fell back to a type default
  plan_code (STRING): null: no override, alias or attribute of the column's name
--strict-coverage refuses the run, and so does strictCoverage in the bundle; fluxsim explain FLX-3304 says how to cover a column
applied to ... (runDigest=sha256:...)
emitted payment_recovery_moment: 167 rows (scenario)
run ... complete
$ head -n 3 .flux/runs/*/emits/payment_recovery_moment.ndjson
["sub-8a788581f8006326","2026-07-01T04:15:50.508Z","65.3500","mkt-c","high",null]
["sub-8a788581f8006326","2026-07-01T04:20:58.314Z","53.7700","mkt-c","high",null]
["sub-6fd215d178e56f52","2026-07-02T19:21:25.861Z","36.0000","mkt-c","medium",null]
```

Each line is a JSON array in the order of the contract's columns, not an object. The `rows: ok` line of `verify` checks the rows against the contract:

```console
$ fluxsim verify . --run-id $(ls .flux/runs)
reproducible: not checked | validity: not asserted
...
  rows: ok
...
```

A `range` rule in a column's `validationRules` is checked as well. A DATE or TIMESTAMP column that declares one is drawn inside it on kernel 0.2.0, and `verify` counts the rows of a DATE, TIMESTAMP, INT or DECIMAL column outside it: a column with rows outside adds a `ranges:` line under `rows: ok`, naming the table, the column, how many rows and the range, and the run fails. A run with none outside prints no such line.

## What the engine does with a column

- **Types.** The engine renders each value as the column's declared type. `DECIMAL(18,4)` and `TIMESTAMP` values are strings, as above. A value that does not fit, such as a decimal too large for its declared precision, fails the run with FLX-3003. An `INT` column needs a source that is a whole number on every row, such as a count: mapped onto anything else, such as an amount, it stops planning with FLX-3303.
- **Where a value comes from.** For this contract the engine fills `subscriber_id`, `aborted_at`, `amount` and `market` through the telco alias table: the customer id, the moment of the event, the event's amount and the customer's market. A column that is not in that table is filled when its name matches an attribute of the event or the customer. `priceSensitivity` matches a Persona trait, so it has a value. List the names with `fluxsim expr model --bundle .`.
- **A column with no source.** `plan_code` has none, so every row has `null` for it. This is allowed only because the column is optional. In a test with `required: true` on it, planning stopped with FLX-3302 and no run was written. The plan names such a column, as the `coverage:` lines above do, and the evidence pack lists it under "What Was Not Proven". Add `--strict-coverage` to `plan`, `apply` or `run` to refuse the run instead, with FLX-3304 and a table of every such column (it also refuses, with FLX-3305, a declaration the run does not generate, such as `sharedColumns` or `sharedAttribute` on kernel 0.1.0, or on kernel 0.2.0 in a Simulation that does not declare `copies: true`); map the column in the emit's `mapping` block, or give it an attribute of its own name, to cover it. To make that a property of the bundle, put `strictCoverage: true` under the Simulation's `extensions`, in the `com.agentics.flux/1` block next to `emits`: the run is strict whether or not the flag is given, and the flag cannot turn it off.
- **`tags: [identifier]`.** The tag marks a column as an identifier. It does not give the column values: in a test, a second tagged column with no source was `null`. `subscriber_id` has values because the engine maps it to the customer id.

## Keys from the contract

From FLUID 0.7.7 on, a contract declares its keys, and the engine reads them from the contract. A `primary` or `unique` entity in an expose's `semantics` is a one-column key of that expose. A relationship is a foreign key: its `fromColumns` in the declaring expose equal the `toColumns` of the expose it names in `to`, paired by position, so a composite key is one relationship with several columns.

Replace `payment-recovery.fluid.yml` once more. Each payment moment now belongs to one subscriber, and a `subscriber` table is keyed by its id:

```yaml
# payment-recovery.fluid.yml
fluidVersion: "0.7.7"
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
    semantics:
      relationships:
        - name: moment_to_subscriber
          to: subscriber
          fromColumns: [subscriber_id]
          toColumns: [subscriber_id]
          cardinality: many_to_one
    binding:
      platform: kafka
      format: kafka_topic
      location:
        topic: billing.payment-recovery.v1
  - exposeId: subscriber
    kind: table
    contract:
      schema:
        - { name: subscriber_id, type: STRING, required: true }
        - { name: market, type: STRING }
    semantics:
      entities:
        - { name: subscriber, type: primary, expr: subscriber_id }
    binding:
      platform: kafka
      format: kafka_topic
      location:
        topic: billing.subscriber.v1
```

FLUID 0.7.7 is a preview in the FLUX specification package, and a version range never selects a preview, so the emit has to name it exactly. The contract has moved too:

```console
$ fluxsim validate .   # exit 1
FLX-3105: bundle does not validate, so its seam cannot be resolved --...
  sim-payment-recovery: spec.emits[0]: payment-recovery.fluid.yml declares FLUID 0.7.7, a preview, and a range never selects a preview: pin fluidVersion "0.7.7" exactly to opt in
  sim-payment-recovery: spec.emits[0]: provenance.contractDigest mismatch ... bundle contract is sha256:2b505ad44769166ea95ffb33870b5e04329dd880368aaafa0452c06f02b68919
more: fluxsim explain FLX-3105  ...
$ sed -i.bak -e 's/fluidVersion: "^0.7.3"/fluidVersion: "0.7.7"/' -e 's/sha256:90324eff47f5f673c2838ecb6d23efa90739ae37ba43d69c920ca818559913dc/sha256:2b505ad44769166ea95ffb33870b5e04329dd880368aaafa0452c06f02b68919/' simulation.flux.yml
$ rm simulation.flux.yml.bak
$ fluxsim validate .
.: valid -- 9 FLUX document(s), 1 FLUID contract(s), 1 Simulation(s)
```

The engine now reads the keys and holds the bundle to them:

- **The Simulation never restates a key.** A key in its extensions, such as `primaryKey: [subscriber_id]` in the emit's block, is refused with [FLX-3111](../reference/errors/FLX-3111.md), and so is a fidelity `unique: false` on a column the contract makes a key.
- **The contract stays pinned.** An emit of a contract that declares keys must pin `provenance.contractDigest` ([FLX-3112](../reference/errors/FLX-3112.md)), so a moved key stops the run until you pin the new digest. A key that does not resolve, such as a relationship to an expose the contract does not have, is refused where the contract declares it ([FLX-3005](../reference/errors/FLX-3005.md)).
- **A one-column text key is generated as an identifier.** In an emit with a `fidelity` block, a one-column `STRING` key is derived from the seam's digest and the row index, as an identifier column is, with no fidelity `unique` or `identifier` tag needed. This emit has no `fidelity` block, so its rows still come from the simulated customers, as above.
- **Related tables are generated together from statistics.** When every table a relationship names is emitted with a `fidelity` block, the keys never repeat and every foreign key takes a key its parent's rows hold: a declared draw for the foreign key keeps its shape and moves to the parent's nearest key, and an optional foreign key keeps its `nullFraction` unless the relationship's `joinType` is `inner` ([KERNEL-SPEC.md](../spec/KERNEL-SPEC.md) §8.2). Here the moments come from the run, and no emit names `subscriber`, so `subscriber_id` is copied from the simulated customers, as before. A generated emit whose relationship names a table the Simulation does not generate is refused ([FLX-3403](../reference/errors/FLX-3403.md)). A foreign key picks its parent one row at a time, so a parent may have no child; a `counts` entry gives each parent a stated number of children instead, and the child table then comes out grouped by parent in row order, as a table loaded parent by parent does (a consumer that needs another order sorts it). See [Give every parent its children](./give-every-parent-its-children.md).

A contract on FLUID 0.7.6 or earlier is read as before: its entities are not keys to the engine.

See [the seam](../explanation/the-seam.md) for the design, and [FLX-3105](../reference/errors/FLX-3105.md) for the error.
