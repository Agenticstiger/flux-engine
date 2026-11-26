# The catalogue, grouped draws, weights and stable traits

Read this when the user wants a small table that other tables point at kept as the owner's own rows, a column drawn given a column of a linked row, parents picked by a weight, or a trait the same on every row of an entity. The record is [ADR 0023](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/adr/0023-a-catalogue-and-the-rows-that-follow-it.md); the pages are [publish a catalogue whole](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/how-to/publish-a-catalogue.md) and [draw a column given another](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/how-to/draw-a-column-given-another.md). All of it generates under kernel 0.2.0 only.

## Why a catalogue

A table drawn from its columns' statistics does not hold its own 26 products, so a query that filters on one finds rows the source never had; an order line's price follows the whole price histogram, not the price of the product it names; and every product is picked as often as every other. A reference expose is the owner's table, released whole in the profile.

## What the profile publishes

Profile format `1-3-0` has a `references` section: one entry per `exposeId`, with the name of the disclosure rule applied (`{}` when none was), the columns, and the rows in published order. Every cell is a string or `null`: a number as canonical decimal text, a date as `YYYY-MM-DD`, a timestamp as milliseconds since the epoch, a bool as `true` or `false`, a string as stored.

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

The data owner writes this section where the data lives; you never read the records to make it. The format is in the [profile specification](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/spec/PROFILE-SPEC.md). The same format adds a statistic grouped by a number column, a statistic grouped by a column of a linked row (`groupByLink`), and a statistic counted once per entity (`per`).

## Declare the expose

The emit's block carries `reference: true` and no other key. Every contract column must be a published column and every published column a contract column. The Simulation pins the profile by its digest. The block below is from [publish a catalogue whole](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/how-to/publish-a-catalogue.md), and the digest is a placeholder:

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
```

The `product` stream holds the published rows, in published order, on every seed; it draws nothing. The child table's foreign key picks a published row, uniformly where it declares no draw. A foreign key that declares `levels`, such as `l_sku: { levels: { "A-1": "0.7", "C-3": "0.3" } }`, takes the row whose key equals the level it drew; a level that is no published key is `FLX-3403`.

## What is refused

Every refusal comes before a row exists.

- `FLX-3105`: the Simulation pins no profile, the pinned profile publishes no rows for the expose, the block holds another key beside `reference`, or the plan is on kernel 0.1.0.
- `FLX-3403`: a contract column the rows do not publish, a published column the contract does not declare, a cell that does not fit its column's type, a key that repeats or is `null`, and a relationship that names a key no reference expose publishes. A reference expose's relationship points only at another reference expose.
- `FLX-4005`: a column of the expose has a FLUID sensitivity that sets a minimum grade. The expose is released whole, so no grade covers it. You never change the label or lower the grade: tell the user the table is not a catalogue under the grade the label sets, and ask the contract owner. The `flux-privacy-and-profiling` skill covers grades. Do not decide yourself that a table is non-personal: only the data owner's release says so.

## What the grade does not cover

A reference expose is released whole, with no noise on it. A profile that holds `references` grades P1 at most, since the engine cannot tell that a published table is non-personal. `EVIDENCE.md` lists each reference expose as released outside the guarantee the grade states. `fluxsim verify` checks that the output equals the published rows and prints a line such as `rules product: published-rows 3 of 3 (100.00%)`. Tell the user this when you hand a run over.

## A column drawn given a linked row's column

`given: <relationship>.<column>` draws a column given the level that the row's link reaches. The yaml block of that declaration, with its grouped `levelHistogram` check, is in step 7 of the skill; it is from [draw a column given another](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/how-to/draw-a-column-given-another.md). The conditions:

- the relationship reaches a row on every row: no `nullFraction` on its foreign key and no condition on its link;
- the linked column is a categorical drawn from its own levels with no `nullFraction`, or a column of a reference expose, whose levels are its distinct values;
- the linked column is not set by a rule (`FLX-3402`), and every level of it has a weight list;
- the weights are counts or shares of the user's grouped data, each list normalised on its own.

`verify` joins each child row to the row its link names and compares each group with the profile's statistic grouped by the same link. A row whose link is null or missing belongs to no group. The check reads the other emit of the same run, so it needs the profile, and a slice or a `--sample` verify, which hold one emit's rows, refuse it. Counts can single out people: a cell of one or two rows describes a handful of real records. Ask whether the data owner's disclosure rule was applied; do not apply one or make up counts.

## Parents picked by a weight column

A `links` entry's `weight` names a number column of the relationship's target. The parent row is the first row whose cumulative share of the weights exceeds one draw, over the parent's final weights in row order, NULL weighing zero. A negative weight, all-zero weights, or a weight on a distinct relationship, on a declared pick or on a bridge side is `FLX-3403`. Without a weight a foreign key picks its parent row uniformly. No tested page shows a `links` entry: write it from [ADR 0023](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/adr/0023-a-catalogue-and-the-rows-that-follow-it.md) section 4 and ADR 0022 section 5, key for key.

## A trait stable per entity

A segment that is the same on every row of its entity is a contract `dependency` whose `from` columns are a key of the expose; its `to` column is then drawn once per key. `fluxsim init` never writes the contract, so the owner declares the `dependency`. Where it exists and the profile publishes the column's statistic counted per entity, init draws the column from that statistic. A per-entity statistic of a column that no such dependency sets is recorded as unused, with the reason that a stable trait is a contract `dependency` from the entity key: report it to the user and do not edit the contract.

## What init writes from a profile

From a contract and a profile, `fluxsim init` writes:

- `reference: true` for each table the profile publishes whole;
- a foreign key's `levels` over a catalogue's keys, from the profile's counts of child rows per catalogue row, when they are keys of the catalogue; otherwise no draw, so the pick is uniform, and the statistic is unused with that reason;
- `given: <relationship>.<column>` for each statistic grouped by a link, with the grouped counts as weights and a grouped property to score it. It is unused, with the reason the generator would refuse it for, when the contract declares no one relationship over the grouping's columns, the foreign key may be NULL, a rule sets the column, the column is already drawn given another, the parent column is not text or not a categorical drawn from its own levels, or the rows would read each other in a cycle;
- a whole-number column with at most 64 values that the profile groups another column by, as a categorical over those values; a statistic grouped by a number column that publishes bins stays unused;
- `copies: true` for a contract that declares `sharedColumns` or `sharedAttribute`, with no draw and no `nullFraction` on a copied column.

It writes no `links`, `bridges`, weight, sentinel or `counts`. See [the init reference](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/reference/cli/init.md) for its options and [write a bundle from a profile](https://github.com/Agenticstiger/flux-engine/blob/v0.12.0/docs/how-to/write-a-bundle-from-a-profile.md) for how it reads each statistic.

## Check it

Run `fluxsim validate bundle` after each edit. Then hand over to the `flux-run-and-verify` skill: the plan refuses before any row exists, and `verify` prints the `published-rows` line and the grouped checks. A failure is met with the remedy `fluxsim explain` prints, never by loosening the declaration, the tolerance or the check.
