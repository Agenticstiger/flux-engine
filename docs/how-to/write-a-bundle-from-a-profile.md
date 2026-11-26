---
title: "Write a bundle from a profile"
description: "Turn a FLUID contract and a profile of the data into a FLUX bundle with fluxsim init --contract --profile, see what every number in it rests on, then run it, check it against the profile and ship it."
---

# Write a bundle from a profile

Use this page when you have a FLUID contract for a table and a profile of its data, and
want synthetic rows that follow both. One command writes the bundle:

```text
fluxsim init bundle --contract contract.orders.fluid.yaml --profile orders.profile.json
```

It reads the two documents and never a record. Each number it writes is a published
aggregate of the profile, a fact of the contract, or an assumption, and the Simulation's
[parameter register](../spec/PARAMETER-REGISTER-SPEC.md) says which, number by number.
The run it makes is then checked against the profile and packed with its evidence.

The data owner makes the profile where the data lives, with
[`fluid profile`](https://github.com/Agenticstiger/forge_docs/blob/main/docs/cli/profile.md),
and the contract usually comes from `fluid init --discover`. This page writes both by
hand so it runs anywhere: 600 orders with an id, a country, an amount and a time.

If you have the contract and no profile yet, `init` runs in two steps. Give it the contract
alone and it writes a profile request, the list of statistics, column metrics and fact shares
a profiler must measure where the data lives ([PROFILE-REQUEST-SPEC](../spec/PROFILE-REQUEST-SPEC.md)):

```text
fluxsim init bundle --contract contract.orders.fluid.yaml
```

It writes `bundle.profile-request.yml` beside the directory, never inside it, and never over a
file that exists (`--request FILE` names another place). A profiler reads the request and writes
the profile; then run the command above with `--profile`. The request holds names and registered
identifiers and no value of your data.

## 1. The two documents

The contract, as `fluid init --discover` writes it for a Parquet file:

```yaml
# contract.orders.fluid.yaml
fluidVersion: 0.7.6
kind: DataProduct
id: bronze.orders.orders
name: orders / orders
domain: data
description: Source-aligned Bronze ingestion for orders / orders.
metadata:
  layer: Bronze
  productType: SDP
  owner:
    team: data-platform
builds:
- id: ingest_orders
  description: Discovered-source ingest of 1 stream(s).
  pattern: acquisition
  engine: duckdb
  capabilities:
  - full_refresh
  - schema_discovery
  properties:
    source:
      kind: filesystem
      connection:
        uri: file:///srv/orders/data
      mode: full_refresh
      streams:
      - orders
    sink:
      format: parquet
  outputs:
  - orders
exposes:
- exposeId: orders
  kind: table
  binding:
    platform: local
    format: parquet
    location:
      path: ./out/orders.parquet
  contract:
    schema:
    - name: order_id
      type: varchar
      required: false
    - name: country
      type: varchar
      required: false
    - name: amount
      type: decimal(10,2)
      required: false
    - name: created_at
      type: timestamp
      required: false
```

The profile, as `fluid profile` writes it: a row count, per column the metrics its
output checks let out, and for the number column its mean (`mean/1`), the statistic a
run is checked against. A value the checks withheld carries a status code and no value,
here `A` (a small count, or a zero):

```console
$ cat > orders.profile.json <<'EOF'
{
  "_producer": "https://github.com/Agenticstiger/forge-cli/tree/main/fluid_build/profiler",
  "_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile/1-0-0/FluxProfile.json",
  "encoding": {"fractionalBits": 16, "rounding": "floor"},
  "disclosureRules": {
    "acro-default": {"dominance": {"k": "0.90", "n": 2}, "minimumCell": 10, "pRatio": "0.10", "roundingBase": 5, "zerosDisclosive": true}
  },
  "tables": {
    "orders": {
      "rowCount": 600,
      "columns": {
        "amount": {
          "domain": "number",
          "metrics": {
            "count": {"definition": "count/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "F", "value": 600},
            "distinctCount": {"definition": "distinctCount/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "F", "value": 10},
            "max": {"definition": "max/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "F", "value": "100"},
            "min": {"definition": "min/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "F", "value": "10"},
            "nullCount": {"definition": "nullCount/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "A"},
            "quantiles": {"definition": "quantiles/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "F", "values": {"0.25": "20", "0.5": "35", "0.75": "50"}},
            "sum": {"definition": "sum/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "F", "value": "27750"}
          },
          "statistics": [
            {"definition": "mean/1", "disclosure": "acro-default", "groups": [{"Sx": "1818624000", "group": "", "k": 600, "status": "F"}], "sampleSize": {"max": 602, "min": 598}}
          ]
        },
        "country": {
          "domain": "category",
          "metrics": {
            "count": {"definition": "count/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "F", "value": 600},
            "distinctCount": {"definition": "distinctCount/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "F", "value": 5},
            "nullCount": {"definition": "nullCount/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "A"}
          }
        },
        "created_at": {
          "domain": "timestamp",
          "metrics": {
            "count": {"definition": "count/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "F", "value": 600},
            "distinctCount": {"definition": "distinctCount/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "F", "value": 310},
            "max": {"definition": "max/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "A"},
            "min": {"definition": "min/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "A"},
            "nullCount": {"definition": "nullCount/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "A"},
            "quantiles": {"definition": "quantiles/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "A"}
          }
        },
        "order_id": {
          "domain": "category",
          "metrics": {
            "count": {"definition": "count/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "F", "value": 600},
            "distinctCount": {"definition": "distinctCount/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "F", "value": 600},
            "nullCount": {"definition": "nullCount/1", "disclosure": "acro-default", "sampleSize": {"max": 602, "min": 598}, "status": "A"}
          }
        }
      }
    }
  }
}
EOF
```

## 2. Write the bundle

```console
$ fluxsim init bundle --contract contract.orders.fluid.yaml --profile orders.profile.json
created bundle from contract.orders.fluid.yaml and orders.profile.json (4 files):

  contract.orders.fluid.yaml  the FLUID contract, copied unchanged
  world.flux.yml              the least World a run needs: the generator emits read none of its numbers
  persona.flux.yml            the least Persona a run needs, for the same reason
  simulation.flux.yml         the run: one generator emit per profiled expose, the profile pin and the register

orders: 600 rows, 4 columns
  order_id    an identifier: 600 distinct values in 600, so the engine derives one per row
  country     5 opaque levels, at equal weights; allocated exactly, since the table is small ...
  amount      a histogram over 5 published order statistics, shaped to the published mean; allocated exactly, since the table is small ...
  created_at  uniform within the run's time window: the profile publishes no order statistic

profile: 21 statistics -- 11 used, 3 unused, 7 withheld
unused (no number rests on these; the register lists them):
  - /tables/orders/columns/country/metrics/count (count/1): no rule of fluxsim init reads this statistic for this column
  - /tables/orders/columns/created_at/metrics/count (count/1): no rule of fluxsim init reads this statistic for this column
  - /tables/orders/columns/created_at/metrics/distinctCount (distinctCount/1): no rule of fluxsim init reads this statistic for this column
a profile of the contract would replace assumptions (bundle/requests/assumptions.profile-request.yml asks a profiler for them):
  - /tables/orders/columns/amount/metrics/nullCount (nullCount/1): 1 assumed number
  - /tables/orders/columns/amount/statistics/0 (marginalHistogram/1): 8 assumed numbers
  - /tables/orders/columns/country/statistics/0 (levelHistogram/1): 1 assumed number
  - /tables/orders/columns/created_at/metrics/nullCount (nullCount/1): 1 assumed number
parameters: 33 declared -- 12 from the pinned profile, 0 from the contract, 18 assumed, 3 default
assumed (the register gives each assumed number its reason; replace what you know better):
  - country: The profile publishes how many distinct values the column holds and neither the values nor their counts: as many opaque levels, named country-<n>, at equal weights.
  - country, amount, created_at: The profile withheld nullCount (status A): a count withheld as small is zero or below the minimum cell, so no nulls are drawn.
  - amount: Drawn evenly within its bins, the histogram would give a mean other than the one the profile publishes, so within each bin the values are drawn from the part on the mean's side: each bin keeps its share of the rows, so every published quantile or bin count holds, and the mean is the published one, or as near to it as the bins allow on the column's grid.
  - created_at: The profile withheld every order statistic, so it is drawn within the run's time window (spec.timeBounds).
  - time window: No date or timestamp column publishes its range, so the run covers 2026-01-01 to 2026-12-31. Set spec.timeBounds to the span your data covers.

next:
  fluxsim run bundle --profile orders.profile.json
```

Both drawn columns say they are allocated exactly: 600 rows is a small table, so `init` wrote `allocation: exact` on them and `sampling: true` on their tolerances (see the list of what `init` writes below).

The directory must not exist yet, or must be empty: nothing is ever overwritten. The
profile stays outside the bundle, which holds only FLUX and FLUID documents; the
Simulation pins its digest instead. Before it writes a file, the command checks the
bundle as `fluxsim validate` does, and as `plan` does from the documents alone before a
row is drawn, and writes nothing either refuses.

The `profile:` line is the command's ledger ([ADR 0014](../adr/0014-init-reads-every-statistic-a-profile-publishes.md)):
every statistic and metric of the profile, by its JSON Pointer, is used (a number of the
bundle rests on it, a rule read it, a property is scored against it, or the bundle was
checked against it), unused, or withheld (the profile published no value). Each unused
one is named with the reason no rule reads it, and the Simulation lists them under
`unused` beside the register: `plan`, `verify` and `ship` count them, and `ship` names
each one under "What Was Not Proven" in `EVIDENCE.md`. In `--json`, `ledger.statistics` names
each statistic on its own, with its state, and `ledger.unused` is the Simulation's list: what an
owner can leave out of the profile. Where the profile reader needs one of an entry's statistics
(a calendar entry holds at least one of its three histograms), `ledger.unused` names the whole
entry when none of it is read, so a profile with exactly those pointers dropped is one the reader
accepts. Drop that list, not every statistic marked unused.

`--strict` turns the ledger into a gate. It writes nothing, and exits 1, when a statistic
is unused or an assumption stands in for something the profile
did not publish; the tolerances and the bounds of a committed state are settings, not
gaps. Each finding is named, and `--allow POINTER` accepts the ones at or below a profile
pointer or a Simulation pointer. An `--allow` that accepts nothing is refused too:

```console
$ fluxsim init strict --contract contract.orders.fluid.yaml --profile orders.profile.json --strict --allow /tables/orders/columns/created_at # exit 1
init: --strict refuses this bundle (1 statistic of the profile unused, 6 assumptions standing in for what the profile did not publish); nothing was written
  unused /tables/orders/columns/country/metrics/count (count/1): no rule of fluxsim init reads this statistic for this column
...
accept one with --allow POINTER (a profile pointer for a statistic, a Simulation pointer for a gap), or publish what the profile left out
```

## 3. What each number rests on

The rules run from the most the profile says to the least:

| The profile or the contract says | The bundle declares | Source in the register |
|---|---|---|
| a table's `rowCount` | the emit's `rows` | profile |
| a column's `min` and `max` | its `range`, which the fidelity check holds the run to | profile |
| `min`, the quartiles and `max` | histogram `bins` and `binWeights`, a quarter of the rows in each | profile |
| a mean: `sum` and `count`, or a `mean/1` statistic | the histogram shaped to that mean and, for a `mean/1` over a number column with a published range, a `mean` property the run is checked against | profile, with the shape assumed |
| a binned quantile (`quantile/2`) | two points of the column's distribution for the histogram: where the profile withholds `min` or `max` beyond them, a tail from the published mean or the profile's next bin out, each assumed | profile, with the tail assumed |
| `nullCount` | `nullFraction` | profile |
| a histogram, level counts or a list of levels | `bins` and weights, `levels`, and a property the run is scored on | profile |
| a histogram or level counts grouped by a categorical column | `given` that column, with `binWeightsGiven` or `levelWeightsGiven` from the group counts, and a grouped property | profile |
| the same, grouped by an INT column the profile lists at most 64 whole numbers of (a `levelHistogram/1` or `support/1`) | that INT column declared as a categorical over those integer levels (canonical decimal text, each inside the contract's range), then `given` it as above | profile |
| a histogram or level counts `per` the columns a contract `dependency` follows, when those columns are a key of the table or a relationship's foreign key | the dependent column drawn from that statistic in place of the one that counts rows, no histogram or mean property for it (a run's properties count rows, the statistic counts entities), and a choice that says so | profile |
| children per parent of a relationship the contract declares (`relationships`) | a `counts` entry whose `children` are the histogram's classes, so each class of parents holds its published share of the parents; a relationship the layout cannot place keeps the foreign key's `bins` and `binWeights`, each class a share of the parent's keys | assumption, from the profile's counts |
| as many distinct values as values | `unique: true`: an identifier the engine derives | no number |
| an `enum(...)` type | `support: declared`, with `maxCardinality` its number of levels | contract |
| a key, in its `semantics` | no key of its own: the engine reads keys from the contract | no number |
| a `derive` in the contract's `invariants`, with no `when` | no draw, and a `range` from the rule's exact bounds over the columns it reads (or its published histogram's edges when they hold them), with its published statistics scored through the rule | contract, or profile for the edges |
| a `range` rule in the contract's `validationRules` (`between A and B`, or `>=`, `>`, `<=`, `<`) | the bound on the bins, levels and tails of a drawn number column; a column with nothing published draws evenly over it | contract |
| a `dependency` in the contract | the column keeps its draw and gets no `mean` property, since the run draws it once per value of the columns it follows | contract |
| nothing, or a withheld value | an assumption, stated with its reason | assumption |

These rules shape the assumptions:

- A column's `range` and `support` are what the fidelity check holds the run, and a
  slice of real records, to: a value outside them is refused. So they are declared only
  from a published value or the contract. A shape that rests on an assumption, such as
  the opaque `country` levels, is declared with keys only the generator reads. One
  exception: a fidelity block needs a property, so in a table where no column publishes
  a range or its levels, one column's assumed span or levels is declared for the check,
  and the output says so.
- A count withheld as a small one (status `A`: a bin, a level or the distinct values)
  lies below its rule's minimum cell, so it is assumed to be half of it: 5, under ACRO's
  default of 10. Any other status says nothing of a count's size, so a cell withheld
  under one is assumed to be the mean of the cells its table publishes. A withheld null
  count is the exception. It is most often a zero, which the checks withhold as
  disclosive, so no nulls are drawn rather than nulls invented in every column.
- A number key lies between its published `min` and `max` when they hold one value per
  row. Otherwise its span is assumed, 1 to the row count. It is widened to the values
  the foreign keys that reference it are declared over, or moved inside the key it
  references when that key's span is published, down a chain of keys. A foreign key the
  profile says nothing of is drawn over the span of the key it references.
- Grouped statistics are read as two-way tables. Each pair of columns is scored by its
  mutual information, and the tree of the most informative links that makes no cycle
  decides which column is drawn given which: at most one `given` per column, a number
  column only at the end of a link, and never given a column that holds nulls. A grouping
  the tree leaves out is unused, and the output says why. An INT column the profile groups
  another column by, and lists at most 64 whole numbers of, is a categorical over those
  levels for this purpose. A statistic grouped by a number column of the profile (format
  `1-3-0`) is listed as unused: a number column publishes bins, not levels.
- A table the profile publishes whole under `references` (format `1-3-0`) is written as a
  reference expose: `reference: true` and nothing else, so its rows are the published ones. A
  foreign key into it declares the levels the profile counts it over (the child rows of each
  catalogue row), when they are keys of the catalogue; a foreign key whose statistic is over
  other values declares none, so it picks a catalogue row uniformly, and its statistic is
  unused with that reason. A statistic of a column grouped by a column of a linked row
  (`groupByLink`) is read as a two-way table, and the column is drawn
  `given: <relationship>.<column>` with the grouped counts as its weights, and scored by a
  property grouped by the same link. It is unused, with the reason the generator would refuse it
  for, when the contract declares no one relationship over the grouping's columns, the foreign
  key may be NULL (a NULL has no level to draw given), a rule sets the column, the column is
  already drawn given another, the parent column is not text or not a categorical drawn from its
  own levels, or the rows would read each other in a cycle. See
  [Publish a catalogue whole](./publish-a-catalogue.md).
- A trait that is stable per entity, such as a customer's segment on each of that customer's
  rows, is a contract `dependency` from the entity's key. `fluxsim init` never writes the
  contract, so declare the `dependency` there. Where it is declared and the profile publishes the
  column's statistic `per` that key, the column is drawn from it, because the dependency draws
  once per key. A statistic `per` columns that no dependency of the contract follows is listed as
  unused, with that reason.
- Children per parent are laid out exactly where the counted layout can place the relationship
  (see [Give every parent its children](./give-every-parent-its-children.md)). `init` writes a
  `counts` entry whose `children` are the histogram's classes, so the run's histogram of children
  per parent is the published one, class for class, for a key of any type. The class of parents
  with no child is read as published, a zero included. A relationship the profile marks
  `structural: [childless]` (the source enforces at least one child per parent) is written as
  `minChildren: 1` and its class of none takes no weight; `orphan` is listed as checked, since
  every foreign key finds its parent. A `one_to_one` relationship is laid out the same way, with `levels: ["0", "1"]` weighted by the
  profile's classes of none and of one; a histogram with parents in a class above one keeps the
  spread, and `init` says so in a note. A relationship the layout cannot place keeps the
  approximation: it is a key or part of one, a relationship to its own table, a foreign key with
  a null share, or one with a draw or a contract rule of its own.
  Its foreign key spreads each class of parents over the parent's keys, a parent of a class whose
  mean is `m` receives a Poisson-like number of children, so about `e^-m` of them receive none, and
  the run's share of parents with no child comes out that much larger than published.
- A relationship's histogram of children per parent grouped by a `category` column of the parent, or by the
  bins of a `number` or `timestamp` one (profile format `1-4-0`), becomes the `given` and `childrenGiven` of its
  `counts` entry, beside the `children` of the ungrouped histogram or, failing that, of the sum of the groups. The
  classes are the column's declared levels, or its bins keyed by the text of their lower edges, and the
  statistic's bins must be the entry's. A class the profile has no group for takes the shape's own weights, and
  the bundle says so. A column with a null fraction, one a summary over the children sets, a key, a column of a
  rule, or one drawn given a linked row cannot be given: the ledger says why and the entry has no `given`.
  A relationship's noisy histogram is read as its point estimate, as a column's is, with its noise in the ledger.
- A table whose profile publishes `kendallTau/1`, or `spearmanRho/1` when no Kendall value is published for
  any pair, gets one `fidelity.copula` over the number and timestamp columns that `init` draws from bins and
  weights, with no `given` and no `allocation`, and that no clique, rule, key, relationship, history or date
  entry decides (see [Draw number columns together](./draw-number-columns-together.md)). A column has one
  joint, so what the cliques draw stays theirs. A noisy value is written as the released number clamped to
  `[-1, 1]`, a pair the profile did not publish is held at 0 and said so as an assumption, and each pair is
  scored by a `rankCorrelation` property. A column is left to the cliques when it ties often: it is
  tie-heavy when the sum over its bins of the squared share of the bin divided by the number of grid values in
  it is at least 1/20, because the statistics carry no tie correction. The rule reads the declared bins and the
  grid only, and the report names each column left out. Under kernel 0.1.0 no copula is written.
- A column of a `cliqueMarginal/2` clique that holds numbers or timestamps is drawn from `bins` and `binWeights`,
  or `binWeightsGiven` its separator, over the clique's edges, which win over the column's own histogram and
  make its range the span of the edges. A clique whose edges do not cover the range the contract declares, or
  that holds a column with nulls, is refused with [FLX-3402](../reference/errors/FLX-3402.md).
- Every distance tolerance `init` writes declares `sampling: true`, so the check widens its
  margin by the sampling error of the run's rows and a small table is not judged by a margin only
  a large one can meet. A table is small when that widening at its rows, for any property `init`
  writes for it, exceeds the default tolerance. Every column of a small table that
  `allocation: exact` can read (a column drawn from `levels`, from `bins` with `binWeights`, or
  given a column that is itself exact) then declares it, so each class holds exactly its share of
  the rows; a key, a foreign key and a column a rule sets are left as they are. A bundle that
  declares either key plans only under kernel 0.2.0, the default, and kernel 0.1.0 refuses it
  with [FLX-3105](../reference/errors/FLX-3105.md).
- A column of a key over several columns spans its rows the same way, and a text one
  takes as many opaque levels as rows, so the key can hold one value per row. A key's
  columns declare no `nullFraction`, nor does an `inner` foreign key, and a composite
  foreign key takes its nulls from its first column alone.
- A histogram's outer edges are often round numbers beyond the data. Its draw keeps to
  the published `min` and `max`, while the check reads the histogram's own edges.
- Drawn evenly within its quartile bins, `amount` would average about 40, and the
  profile says 46.25. So within each quartile bin the values are drawn from the part on the
  mean's side: each bin keeps its quarter of the rows, so every published quartile
  holds, and the mean is the published one. The cuts this adds are assumptions in the
  register.
- The World and the Persona are the least a run needs. Their numbers are `default` in
  the register: the generator emits read none of them.

A timestamp bound is RFC 3339 text and a date bound `YYYY-MM-DD`, which the register
does not count, so an
assumed time window is in the command's output and nowhere else. Here no time column
publishes its range, because no single instant is held by ten orders, and `created_at`
is drawn within the run's `timeBounds`.

When some columns do publish a range, the run's `timeBounds` span all of them: the earliest start
to the latest end of every date column's published range, of the date range its contract column
declares (a `range` rule such as `between '2020-01-01' and '2026-12-31'` or `>= '2020-01-01' and
<= '2026-12-31'`), and of the bins, levels and range the column itself is drawn from. No column's
bins are cut. To choose the window yourself, pass `--window START END`, two dates
(`YYYY-MM-DD`, midnight UTC) or RFC 3339 timestamps with START before END. The Simulation's
`timeBounds` are exactly those, a date column's bins that leave them are cut at them (and the
column says so), and the output notes the window under "time window". The end column of a
validity window whose contract declares no `openEnd` gives the window nothing from the year
9999 on, since that year spells an open end; the note names the column and says to declare the
`openEnd` on it.

The register is the `parameters` list in `simulation.flux.yml`. Each entry names a
number by a JSON Pointer and gives its source: a pointer into the profile and the
statistic it lands in, a pointer into the contract, or a reason:

```console
$ grep -A 4 'emits/orders/rows' bundle/simulation.flux.yml
      pointer: /extensions/com.agentics.flux~11/emits/orders/rows
      source: profile
      statistic:
        pointer: /tables/orders/rowCount
    - document: sim-bronze.orders.orders
```

`plan` reads every citation in the profile it is handed, and refuses one the profile
does not publish ([FLX-3113](../reference/errors/FLX-3113.md)). A contract citation is
read in the bundle's contract by every command.

## 4. Replace the assumptions you can

The assumptions are a first draft. Where you know better, edit the Simulation, and change
the number's register entry with it: give it your own reason, or a citation. Here you
might:

- set `spec.timeBounds` to the quarter the orders cover;
- name the countries you know in place of `country-1` to `country-5`, with their weights;
- tighten the property's tolerance to the margin you accept.

Most assumptions are the answer to a question a profiler can measure: how many rows, which
levels, which histogram, how many nulls. Each assumed number of that kind says which statistic
would replace it, in `replacedBy` on its register entry (a pointer and a definition into a
profile of the contract), and the ledger lists the statistics with how many numbers each would
replace. `init` also writes `requests/assumptions.profile-request.yml` inside the bundle, a
[profile request](../spec/PROFILE-REQUEST-SPEC.md) for exactly those statistics, which a
profiler measures where the data lives. Run `init` again with the profile it writes, and each
number a statistic answers is cited as the profile's. A tolerance, the bounds of a committed
state and the other settings have no statistic that replaces them, so they name none. The
request is part of the directory `init` writes, so it is created and removed with it, and the
loader and `inputDigest` do not read it (see [PARAMETER-REGISTER-SPEC.md](../spec/PARAMETER-REGISTER-SPEC.md) section 1.3).

## 4a. A profile released under differential privacy

A profile of format 1-2-0 can carry statistics that the profiler released under noise
([PROFILE-SPEC.md](../spec/PROFILE-SPEC.md) section 3a). `fluxsim init` reads one of them as a
point estimate with a variance. The noisy counts can be negative or fractional, so they are
made a distribution by one rule, in exact arithmetic: the counts are projected onto the
non-negative vectors that add up to their noisy total (a vector that is non-negative already
is only normalised), the shares are those counts over the total, and the draw takes them as
whole counts that add up to the estimated number of records. A table with no `rowCount` takes
its row count from the larger estimate of the records its noisy statistics give. The output
lists each noisy statistic with its mechanism, scale and the variance of each cell, and the
ledger in `--json` carries the same, so you can see how much noise stands behind a number. A
register citation of a noisy cell points at the profile's own `noisyCounts`.

A date or timestamp column's noisy histogram is read only where its counts stand clear of the
noise: above `sqrt(2v(5 + ceil(ln m)))` for Gaussian noise of variance `v` over `m` bins, or
`b(5 + ceil(ln m))` for Laplace noise of scale `b`
([ADR 0019](../adr/0019-one-privacy-budget-per-profile-release.md), the second amendment of
2026-11-25). A bin below that floor carries no weight, so noise in a grid's empty years draws no
dates there, and the run's window is the hull of the bins above it. When no bin is above it, which
is common for a release of a few hundred records, the column is drawn evenly within the run's
window, and the output says so. The column still declares the histogram's whole grid as its range,
and its histogram property still compares the run with the release.

A number column's noisy histogram is read the same way (the amendment of 2026-11-26): a bin below
the floor carries no weight, a contract `derive` over the column is bounded by the hull of the bins above it, and
a column with no bin above it is drawn evenly over its declared range, with its note. A column the
contract makes a key keeps its bins as they stand, since its rows hold distinct values.

A table whose profile carries noisy cliques (section 5a of the spec) is drawn through a
junction tree: the cliques are arranged into a tree rooted at the one with the most columns
(the first by name among equals), and each column is drawn given the columns its clique shares
with the one before it, so the chain of conditionals reproduces the cliques' joint. Each row of
weights is the point estimate of the clique's noisy counts for that combination of levels.
Cliques that cannot form a tree (the running intersection property fails) are refused, naming
them, and the levels a clique lists for a column must be the ones the contract declares. The
run is scored on the one- and two-way level histograms the cliques answer.

## 5. Run it, check it, ship it

`fluxsim run --verify --ship` is the one-pass path: one command plans, applies, checks and
ships, and the rows are drawn once.

```console
$ fluxsim run bundle --profile orders.profile.json --verify --ship -o out
plan written to ...
coverage: orders: 1 of 4 columns fell back to a type default
  created_at (TIMESTAMP): uniform within the simulation's time bounds: the column declares no draw of its own
--strict-coverage refuses the run, and so does strictCoverage in the bundle; fluxsim explain FLX-3304 says how to cover a column
parameters: 33 declared -- 12 from the pinned profile, 0 from the contract, 18 assumed, 3 default; 3 statistics of the profile unused
applied to ...
emitted orders: 600 rows (generator)
reproducible: not checked | validity: not asserted
...
  fidelity orders: passed -- 1 of 1 declared property within the margin against the pinned profile
  scorecard: overall ... against profile sha256:...
...
shipped out/bundle.flux.tgz
run ... complete; shipped to out
```

The run's mean `amount` is within the property's 10 % of the profile's, so the check
passes, the scorecard scores it, and `ship` packs the run with its evidence.
`created_at` is reported as a type default, because the profile gave it nothing to
declare: it is drawn within the run's time window, and the evidence report names it under
"What Was Not Proven", beside the register's shares.

## 6. What the check does not cover

A run is checked against the statistics a profile publishes, and this one publishes a mean
for `amount` alone. Nothing checks the `country` levels, which the profile never names,
or `created_at`, whose time window is assumed. A profile with no statistic a property can
be checked against, as `fluid profile` wrote before it wrote `mean/1`, gives a table with
`properties: []`: `init` writes no property that `verify --profile` would refuse with
[FLX-5037](../reference/errors/FLX-5037.md), names what it left out in its output (and under
`leftOut` of the table in `--json`), and says the table has nothing to verify. Given more
statistics (histograms, level counts), `fluxsim init` declares a property for each one it
can carry: [Score a run against a profile](./score-a-run-against-a-profile.md) shows the
check.

A table the profile gives no statistic to score, such as one whose columns have only a
minimum and a maximum (a date column's usual case), gets `properties: []`: the block keeps each column's range, and the command
says the table has nothing to verify. `verify --profile` reports it as not scored, and
never as passed, and adds a note naming any statistic the profile publishes that a
property could be scored against.

Some columns carry no property at all. A boolean column's counts shape its draw, but
the check reads a category's values as text. A date column's bounds are declared in
days, while a profile reads a date as a timestamp in milliseconds. And an integer
column whose published values pass 2^53 is a placeholder, 0 on every row, because a
run's rows cannot hold such an integer exactly.

A profile that publishes date pairs, calendars or exposure gives the bundle more than ranges:
lifetimes, dates that lie within a parent's lifetime, seasonal rates and exposure. `init`
trials each exposure and each rates entry it writes, and leaves out, with the trial's numbers,
the ones the run does not reproduce. [Keep dates inside their parent's lifetime](./keep-dates-inside-their-parents-lifetime.md)
reads what it wrote and what it left out, and how to change a fact.

## 6a. One bundle for each what-if: profile overlays

A team that asks what happens if a table doubles needs a variant of the profile, and a
register that says which numbers the variant changed. An overlay is a named merge patch
([RFC 7396](https://www.rfc-editor.org/rfc/rfc7396)) over the profile, in a file of its own:

```bash
cat > double-orders.yml <<'EOF'
name: double-orders
patch:
  tables:
    orders: {rowCount: 1200}
EOF
cat > flat-orders.yml <<'EOF'
name: flat-orders
patch:
  tables:
    orders:
      columns:
        amount:
          metrics:
            max: {definition: max/1, disclosure: acro-default, sampleSize: {min: 598, max: 602}, status: F, value: "100"}
EOF
```

```console
$ fluxsim init bundles --contract contract.orders.fluid.yaml --profile orders.profile.json --overlay double-orders.yml --overlay flat-orders.yml
created 2 bundles in bundles, one for each overlay:

created bundles/double-orders from contract.orders.fluid.yaml and orders.profile.json with the overlay double-orders (4 files):
...
orders: 1200 rows, 4 columns
...
parameters: 33 declared -- 11 from the pinned profile, 0 from the contract, 1 from the overlay, 18 assumed, 3 default
...
created bundles/flat-orders from contract.orders.fluid.yaml and orders.profile.json with the overlay flat-orders (4 files):
...
```

`--overlay` is repeatable and each file writes one bundle, in a directory of the one given
named after the overlay (`[a-z][a-z0-9-]*`, at most 63 characters). The bundle is written
from the overlaid profile, so `rows` is 1200 above, and its Simulation declares the overlay
under `extensions["com.agentics.flux/1"].overlay` beside the pin of the profile as published.
The register cites the overlay (`source: overlay`) for each number whose statistic the patch
changed, and the profile for every other number: a number the patch changed cannot be cited
as the profile's, and `plan` refuses that ([FLX-3113](../reference/errors/FLX-3113.md)).

A patch may touch only numbers a profile publishes: a table's `rowCount`, a column's `metrics`
and `statistics`, a table's `columnPairs` and `periods`, and the top-level `relationships`
and `invariants`. A patch replaces a list whole, so it restates a column's `statistics` to
change one of them, and a statistic it restates carries no `noise`. Anything else is
refused ([FLX-3105](../reference/errors/FLX-3105.md)), and so are a table or a column the
profile does not hold, a patch that makes a document the profile reader refuses, and a new
`rowCount` for a table that publishes a `history`: its rows are the versions of the history's
entities, which no patch can change, so more rows would only lengthen each chain. A patch
that restates a column's `mean/1` beyond what the column's published minimum, maximum,
quantiles and histogram allow, such as a mean of 69 for a column whose quarters sit at
or below 20, 35, 50 and 100, is refused by `init` and `plan`
([FLX-3402](../reference/errors/FLX-3402.md)), and the message gives the bound it breaks,
because no column the profile describes has that mean. Run each
bundle with the profile as published: `plan` and `verify` read the overlay from the bundle and
the plan, `verify` scores against the overlaid profile, and the scorecard names the overlay.

An overlay never raises or lowers the privacy grade, which reads the profile as published, and
its numbers are the maintainers' scenario, not a measurement: `EVIDENCE.md` says so. An overlay
needs kernel 0.2.0.

## What the command refuses

Nothing is written when:

- the directory holds anything, or cannot be written;
- `--contract` comes without `--profile`, or the reverse, or `--overlay` or `--window` comes without both (exit 2);
- a `--window` value is not a date or an RFC 3339 timestamp in the years 1 to 9999, START is not before END, or END
  is in the year 9999, which spells an open end rather than an instant (exit 2);
- a `--history` file's `timeBounds` end, an entry's `until` or a `start` bin is in the year 9999;
- a date range the contract declares would make the run's window longer than 300 years (write an open end in
  the year 9999, which no window reads);
- an overlay file is refused by the reader, two files name one overlay, an overlay names a table the profile does not hold, or an overlay gives a table with a history another `rowCount`;
- the profile cannot be read ([FLX-3109](../reference/errors/FLX-3109.md)) or is not a
  profile ([FLX-5036](../reference/errors/FLX-5036.md));
- the profile describes none of the contract's exposes: the message names the tables on
  both sides;
- the bundle would not validate or plan, for example because the contract does not: the
  message gives the engine's own reason.

An expose is left out of the bundle, and named in the output with the reason, when the
profile does not describe it, when a column of it is JSON or bytes (the generator has no
draw for them), when a DECIMAL column of it has more than 38 decimal places, or when no
column of it has a range or levels a property could read. The exposes of one contract
are generated together, so a foreign key into an expose left out is refused, as `plan`
refuses it ([FLX-3403](../reference/errors/FLX-3403.md)), and nothing is written. If the
profile has no table for the expose it references, profile that table too. If the command
left that expose out for another reason, such as a JSON column, the relationship cannot be
generated as the contract declares it.
