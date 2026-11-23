# ADR 0023: a catalogue and the rows that follow it: reference exposes, groupings across a link, weighted picks and stable traits

## Status

Accepted 2026-11-21. Builds on [ADR 0022](./0022-rows-read-their-linked-rows.md), whose parent reads it uses,
on [ADR 0003](./0003-sdv-bsl-and-aggregate-only-rule.md), whose amendment of 2026-11-16 lets non-personal
reference data such as a product catalogue cross into the engine, and on
[ADR 0014](./0014-init-reads-every-statistic-a-profile-publishes.md), whose rules `fluxsim init` keeps.

## Context

A catalogue is a small table that other tables point at: products, plans, branches. Today FLUX draws a
catalogue like any table, from its columns' statistics, and draws its children apart from it:

- a 26-row product table generated from its own profile does not hold its own 26 products, so a query
  that filters on one finds rows the source never had;
- an order line's price follows the price column's whole histogram, not the price of the product it
  names, because `given` reads only a column of the same row;
- every product is picked as often as every other, because a foreign key picks its parent row uniformly
  unless the parent key is an ordered layout and the child declares a draw;
- a grouping by a number column (an INT with a handful of values, such as a tier) is not a grouping the
  profile can publish, since `groupBy` names a categorical column of the same table only;
- a trait that is stable per entity (a customer's segment on every one of its rows) is already generated
  by a contract `dependency` from the entity key (EXPR-SPEC-2 section 8), but its shares come from a
  per-row histogram, which weighs an entity by its number of rows.

Borrow-before-build search, 2026-11-21. Licences were read from each repository or its documentation
on that day.

| Name | What it does here | Licence | Taken |
|---|---|---|---|
| dbt seeds (dbt docs, 1.12) | static reference data "best suited to static data which changes infrequently", loaded whole as tables | Apache-2.0 | the pattern of a reference table released whole |
| dbldatagen (docs) | `values` with integer `weights` per value | Databricks License | pattern only |
| Snowfakery (docs; archived 2026-09-18) | `random_choice` with odds per option | BSD-3-Clause | pattern only |
| Python's `random.choices` (standard library) | cumulative weights searched by bisection | PSF | the shape of the weighted pick, on the engine's own integer thresholds |
| SDV HMA, RC-TGAN, PrivLava (ADR 0011's table) | child parameters conditioned on the parent row | BUSL, MIT, none found | pattern only; ADR 0011 option B stays the fitted route |
| SEAM-FIDELITY-SPEC section 2 (this repository) | `given` draws on the column's own path, the parent's level picking the weight table | Apache-2.0 | reused: a linked level picks the table the same way |

No new dependency is needed. Every draw below is a draw the kernel already pins.

## Decision

### 1. Profile format `1-3-0`

Format `1-2-0` is published with the 0.9 releases, so its additions go to a new minor format,
`1-3-0`, which the reader takes beside `1-0-0` to `1-2-0` (PROFILE-SPEC section 8). It adds, all
optional:

- **`references`**: a whole table, keyed by `exposeId`, as `{disclosure, columns, rows}`, every cell a
  string or `null`. The owner releases it as non-personal reference data, under a disclosure rule that
  names what was applied (`{}` when nothing was). The reader bounds its rows, cells and text.
- **`groupBy` on a number column**: a column statistic may be grouped by a number column of the same
  table. Its group keys are the canonical decimal text of each value.
- **`groupByLink`**: a column statistic grouped by a column of the row the table's relationship names,
  as `{parent: {table, columns}, columns, column}`, the same key columns the `relationships` section
  names on both sides.
- **`per`**: a column statistic counted once per distinct value of the named columns of its table, so a
  trait stable per entity is published per entity, not per row.

### 2. A reference expose is its published rows

An emit whose block declares `reference: true` takes its rows from the pinned profile's `references`
entry for its expose: one row per published row, in published order, each cell read as its contract
column's type. Its block carries nothing else. Every contract column must be published, each key must
be unique over the rows, and a relationship it declares must name a reference expose that holds every
key it uses (`FLX-3403`). A foreign key into it picks a row as any foreign key does, and a column drawn
`declared` over it takes the row whose key equals the drawn level, a level that is no key being refused
at plan time (`FLX-3403`). A reference expose draws nothing, so its bytes do not depend on the seed.
Because it is released whole, the governance gate refuses one whose columns carry a FLUID `sensitivity`
that sets a minimum grade (ADR 0020, `FLX-4005`), and `EVIDENCE.md` lists each reference expose as
released outside the run's guarantee. A profile that holds `references` is graded P1 at most, since the engine
cannot tell that a published table is non-personal.

### 3. A column drawn given a linked row's column

`given: <relationship>.<column>` draws a column given the level that the row's link reaches: the level of
the parent's final value (ADR 0022 section 3), under the weight tables SEAM-FIDELITY-SPEC section 2 already
defines (`levelWeightsGiven`, `binWeightsGiven`). The relationship must reach a row on every row (no
`nullFraction`, no condition), and the parent column must be a categorical drawn from its own levels with
no `nullFraction`, or a column of a reference expose, whose levels are its distinct values. A parent column
that a rule sets is refused (`FLX-3402`). The cell is drawn on its own path at the index it always takes,
so no seed-tree path is added.

### 4. Parents picked by a weight column

A `links` entry's `weight` names a number column of the relationship's target. The parent row is the
first row `i` with `u < T_i`, where `u` is one `uniform53` at the draw index the uniform pick takes and
`T_i = floor(2^53 * (w_0 + ... + w_i) / W)` on exact rationals over the parent's final weights in row
order, NULL weighing zero. The thresholds are worked out once per process over every parent row. A
negative weight, or weights that are all zero, is `FLX-3403`. A weight on a `distinct` relationship, on a
`declared` pick or on a bridge side is `FLX-3403`.

### 5. `fluxsim init`

- **An INT column with few values** that the profile publishes as a categorical (a `levelHistogram/1` or
  `support/1` over at most 64 whole numbers, each written as decimal text) and groups another column by
  becomes a categorical over those integer levels, so the grouped column is drawn `given` it, under the
  forest rules ADR 0014 already applies to categorical parents. A statistic grouped by a column of the
  `number` domain stays unused, since such a column publishes bins, not levels.
- **A catalogue** follows from the profile: `reference: true` for each expose it publishes whole,
  `given: <relationship>.<column>` for each statistic grouped by link, and a foreign key's declared levels
  from its level histogram over a reference expose's keys.
- **A stable trait.** For a contract `dependency` whose `from` columns are a key of the expose, each `to`
  column is drawn from its per-entity statistic (`per` the `from` columns) when the profile publishes one,
  since the dependency draws once per key. A per-entity statistic of a column that no such dependency
  sets is recorded as unused, with the reason that a stable trait is a contract `dependency` from the
  entity key. `fluxsim init` never writes the contract.

### 6. Checks

`fluxsim verify` checks a property grouped by link group by group, joining each child row to the row its
link names, as a `given` link is checked by a grouped property today (SEAM-FIDELITY-SPEC section 2). A
reference expose is checked for equality with its published rows.

## Consequences

- A profile of format `1-2-0` or earlier, and a bundle that declares none of this, keep every byte and
  every digest.
- A reference expose is exact on every seed, which is what makes a small catalogue pass its own profile.
- Everything here generates under kernel `0.2.0` only, as ADR 0022 section 1 says.
- The engine's flux-stats pin moves with the release that ships format `1-3-0`, and the profiler gains
  the statistics on its side.

## What would change this decision

- A catalogue too large to publish whole, which would move it to grouped statistics alone.
- A contract-side spelling of a stable trait or a weighted pick, should FLUID gain one.
