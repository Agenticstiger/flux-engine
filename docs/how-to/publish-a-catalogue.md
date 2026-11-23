---
title: "Publish a catalogue whole"
description: "Declare a small table such as a product catalogue as a reference expose, so its rows are the rows the pinned profile publishes and the tables that point at it pick only those rows."
---

# Publish a catalogue whole

Use this page when a small table that other tables point at, such as products, plans or branches, should hold the owner's own rows instead of rows drawn from its statistics. A catalogue drawn from statistics does not hold its own 26 products, so a query that filters on one finds rows the source never had. A reference expose is the owner's table, released whole in the profile, and every table that points at it picks one of its rows.

A reference expose is for non-personal reference data. The profile holds the table's rows as they are, with no noise on them, and the run's privacy grade does not cover them (see [Check what the grade covers](#check-what-the-grade-covers)). It needs kernel 0.2.0, which a new plan takes.

## The profile publishes the rows

Format `1-3-0` of the profile has a `references` section: one entry per `exposeId`, with the name of the disclosure rule that was applied (`{}` when none was), the columns, and the rows in published order. Every cell is a string or `null`, written as a key's value is written in [EXPR-SPEC-2](../spec/EXPR-SPEC-2.md) section 8: a number as canonical decimal text, a date as `YYYY-MM-DD`, a timestamp as its milliseconds since the epoch, a bool as `true` or `false`, and a string as stored.

```json
{
  "references": {
    "product": {
      "disclosure": "none",
      "columns": ["sku", "pname", "price", "kind"],
      "rows": [
        ["A-1", "Anvil", "12.5", "tool"],
        ["B-2", "Bolt", "0.25", null],
        ["C-3", "Crate", "30", "bulk"]
      ]
    }
  }
}
```

The data owner writes the section where the data lives ([PROFILE-SPEC.md](../spec/PROFILE-SPEC.md) section 5b). Pin the profile in the Simulation by its digest, as [Score a run against a profile](./score-a-run-against-a-profile.md) shows.

## Declare the expose

The emit's block carries `reference: true` and no other key. The contract declares the table's columns, its key and its relationships as it does for any expose; every contract column must be one of the published columns, and every published column a contract column.

```yaml
extensions:
  com.agentics.flux/1:
    profile:
      digest: sha256:0000000000000000000000000000000000000000000000000000000000000000
    emits:
      product:
        reference: true
      line:
        rows: 400
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 1, max: 1000000 }
          columns:
            l_qty: { range: { min: "1", max: "9" } }
          properties:
            - property: mean
              column: l_qty
              tolerance: { type: absolute, value: "100" }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
```

Plan and run it with the profile:

```text
$ fluxsim run ./bundle --profile catalogue.profile.json --verify
```

The `product` stream holds the published rows, in published order, on every seed. `line` is generated as before, and its foreign key `l_sku` picks a published row: every value it holds is a published key, and the row's other columns are copied from it. A foreign key that declares no draw picks a row uniformly. One that declares `levels` takes the row whose key equals the level it drew, so `l_sku: { levels: { "A-1": "0.7", "C-3": "0.3" } }` keeps a share of 70 % of the lines on the first product. A declared level that is no published key is refused at plan time (`FLX-3403`).

## What is refused

Every refusal comes before a row exists.

- `FLX-3105`: the Simulation pins no profile, the pinned profile publishes no rows for the expose, the block declares another key beside `reference`, or the run is on kernel 0.1.0.
- `FLX-3403`: a contract column the rows do not publish, a published column the contract does not declare, a cell that is not its column's type or does not fit it, a key that repeats or is `null`, and a relationship that names a key no reference expose publishes. A relationship of a reference expose points only at another reference expose.
- `FLX-4005`: a column of the expose has a FLUID `sensitivity` that sets a minimum grade. The expose is released whole, so no grade covers such a column.

The plan records the published rows, so `apply --run-id` and `verify` need no profile for them: a plan made once is applied and checked as it was made.

## Check what the grade covers

`fluxsim verify` checks that the output of a reference expose equals its published rows, and prints the result as a line of the expose's rules:

```text
  rules product: published-rows 3 of 3 (100.00%)
```

`EVIDENCE.md` lists each reference expose under Privacy Grade as released whole and outside the guarantee the grade states. A profile that holds `references` grades P1 at most, since the engine cannot tell that a published table is non-personal; the owner's release is what says so.
