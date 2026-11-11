---
title: "The FLUID seam"
description: "Why a FLUX Simulation references a FLUID contract instead of restating it, and what the engine does with that reference."
---

# The FLUID seam

```console
$ fluxsim init demo
...
$ cd demo
```

A FLUX bundle describes a simulated universe: a population, what it does, what the business does about it. A FLUID contract describes a data product: its columns, their types, who owns it. The rows a simulation writes are written for one such product. The point where the two documents meet is called the seam.

## The rule: reference, never restate

A Simulation does not copy the contract's columns into its own YAML. It names the contract, and pins its bytes.

```console
$ sed -n '/emits:/,$p' simulation.flux.yml
  emits:
    - productRef: telco.gold.payment_recovery_moment
      exposeId: payment_recovery_moment
      fluidVersion: "^0.7.3"
      provenance:
        # The digest of payment-recovery.fluid.yml. The engine refuses to run when the
        # contract moves away from the one this simulation was written against, and
        # prints the new digest so you can copy it in on purpose.
        contractDigest: "sha256:b9b5ee575a8b313f6f72d039653d8be5d76e7ecc0012d7bd6ad2d37e0e60435d"
```

Each field earns its place:

- `productRef` is the `id` of a FLUID contract in the bundle.
- `exposeId` picks one of that contract's `exposes[]`. A product can expose several streams.
- `fluidVersion` is an exact version or a range. The contract's own `fluidVersion` must satisfy it.
- `provenance.contractDigest` is the digest of the contract as a parsed document.

If the Simulation held its own copy of the columns, there would be two documents that could disagree. One would describe what was checked, the other what ships. With a reference there is one file. "The checked bytes are the shipped bytes" is then a statement about a single digest, not a hope.

## What the contract contains

```console
$ sed -n '/^exposes:/,$p' payment-recovery.fluid.yml
exposes:
  - exposeId: payment_recovery_moment
    kind: stream
    contract:
      schema:
        - name: subscriber_id
          type: STRING
          required: true
          tags: [identifier]
        - name: aborted_at
          type: TIMESTAMP
          required: true
        - name: amount
          type: "DECIMAL(18,4)"
        - name: market
          type: STRING
    binding:
      platform: kafka
      format: kafka_topic
      location:
        topic: billing.payment-recovery.v1
```

## A moved contract stops the run

The digest is taken over the parsed document, so a comment does not move it. A change of meaning does. Here the topic name changes:

```console
$ sed 's/payment-recovery.v1/payment-recovery.v2/' payment-recovery.fluid.yml > ../changed.fluid.yml
$ cp payment-recovery.fluid.yml ../original.fluid.yml
$ cp ../changed.fluid.yml payment-recovery.fluid.yml
$ fluxsim plan .   # exit 1
FLX-3105: bundle does not validate, so its seam cannot be resolved ...
  sim-payment-recovery: spec.emits[0]: provenance.contractDigest mismatch ... bundle contract is sha256:...
...
$ cp ../original.fluid.yml payment-recovery.fluid.yml
```

The message prints the new digest. You can paste it into `provenance.contractDigest` on purpose, once you have decided the change is meant. The engine never edits a document in the bundle for you.

The same error code covers every way the seam can fail to resolve: a dangling `productRef`, an `exposeId` the product lacks, a version outside the range. [`FLX-3105`](../reference/errors/FLX-3105.md) lists them under one name because they share one cause, a bundle that does not validate.

## From columns to rows

The contract fixes the column order and the types. The engine turns each column into a slot and decides, once at plan time, which runtime value fills it. For each column it tries three sources, first match wins:

1. a `mapping` override on the emit, if the bundle declares one;
2. the vertical's alias table (for telco, `aborted_at` is the moment of the event);
3. the column's own name, looked up in the event payload, then the subject.

A required column that nothing fills is an error at plan time (`FLX-3302`). An optional column renders `null`. The engine does not invent a value.

Types come from a small closed set. `float` and `double` columns are refused by name (`FLX-3001`) because they have no reproducible spelling. Use `DECIMAL(p,s)` instead. A row is a JSON array in contract order:

```console
$ fluxsim run .
...
$ head -n 2 .flux/runs/8dec55892096169f/emits/payment_recovery_moment.ndjson
["sub-e19b5b0ae87789b0","2026-07-01T20:46:18.581Z","40.5500","mkt-b"]
["sub-e19b5b0ae87789b0","2026-07-01T20:55:26.840Z","30.5500","mkt-b"]
```

## The attestation

For every emit, `apply` writes `seam-attestation-<exposeId>.json` into the run directory. It records which bytes met which bytes. It holds digests and declarations, no verdict.

```console
$ head -n 17 .flux/runs/8dec55892096169f/seam-attestation-payment_recovery_moment.json
{
  "G": "sha256:e0f6c367377aec038ad465b67546e958eafe70d921637f684434725ec3180881",
  "H": "sha256:e0f6c367377aec038ad465b67546e958eafe70d921637f684434725ec3180881",
  "columns": [
    "subscriber_id",
    "aborted_at",
    "amount",
    "market"
  ],
  "emitIndex": 0,
  "reference": {
    "contractDigest": "sha256:b9b5ee575a8b313f6f72d039653d8be5d76e7ecc0012d7bd6ad2d37e0e60435d",
    "contractFileDigest": "sha256:4872cdd85279bda848bc4ce7c6ded21a0466b99ff773af54d5708ec2e8962785",
    "exposeId": "payment_recovery_moment",
    "fluidVersion": "^0.7.3",
    "productRef": "telco.gold.payment_recovery_moment"
  },
```

The contract appears twice. `contractDigest` is over the parsed document, which is what the Simulation pins. `contractFileDigest` is over the raw file. Packing copies the file byte for byte and checks the copy against it.

`H` and `G` are two fingerprints of the whole crossing. `H` covers the contract, the emit entry, any fidelity block the emit declares, and the ordered column names. Change any of them and `H` changes. The run's seed is derived from `H`, so a different crossing is a different random universe, never a silent re-use of the old one. `G` is `H` with the tolerances, the minimum-variance settings and the state-age budget left out. Here they are equal, because the starter declares no fidelity check. Once one is declared, you can loosen a tolerance and `G` stays put, so work already done under it can be reused.

## Gates on the seam

`minCredibility` on an emit names the lowest credibility a calibrating Playback may declare for the rows to ship. The number is the Playback author's own, not a measurement (see [calibrate from aggregates](../how-to/calibrate-from-aggregates.md)). The check happens offline, during validation. The starter sets none. Add one and the bundle fails until a scorecard declares enough:

```console
$ printf '      minCredibility: 0.7\n' >> simulation.flux.yml
$ fluxsim validate .   # exit 1
FLX-3105: bundle does not validate, so its seam cannot be resolved ...
  sim-payment-recovery: spec.emits[0]: minCredibility 0.7 set but no Playback scorecard calibrates world 'q3-retention-world'
...
```

After `apply`, `verify` checks conformance again from the files on disk. Every stream's row count must match the receipt, and every row must be as wide as the attestation's column list. The stronger per-value check already ran when each row was written. See [the six-step loop](./the-six-step-loop.md).

## Who owns the schema

The engine does not own the FLUID schema or the FLUX schema. It depends on the published FLUX specification package for validation, and that package carries the FLUID schemas for versions 0.7.3 to 0.7.5. A contract cannot silently target a version the toolchain has not seen. If a range matches none of them, validation fails. When FLUID moves, the schemas move with the package, not with this engine.

## What the seam does not do

It does not publish. In this release the engine writes NDJSON files in a run directory and an evidence pack. It sends nothing to a Kafka topic or any platform.

The contract's `binding` block is read for one thing: the first value of `binding.location` names the output dataset in `lineage.ndjson`. Here that is `billing.payment-recovery.v1`. The `platform` value is not read.

For more, see [using the seam](../how-to/use-the-fluid-seam.md) and [the evidence pack](./the-evidence-pack.md).
