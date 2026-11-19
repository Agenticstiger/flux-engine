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
  country     5 opaque levels, at equal weights
  amount      a histogram over 5 published order statistics, shaped to the published mean
  created_at  uniform within the run's time window: the profile publishes no order statistic

parameters: 34 declared -- 12 from the pinned profile, 19 assumed, 3 default
assumed (the register gives each assumed number its reason; replace what you know better):
  - country: The profile publishes how many distinct values the column holds and neither the values nor their counts: as many opaque levels, named country-<n>, at equal weights.
  - country, amount, created_at: The profile withheld nullCount (status A): a count withheld as small is zero or below the minimum cell, so no nulls are drawn.
  - amount: Drawn evenly within its bins, the histogram would give a mean other than the one the profile publishes, so within each bin the values are drawn from the part on the mean's side: each bin keeps its share of the rows, so every published quantile or bin count holds, and the mean is the published one, or as near to it as the bins allow on the column's grid.
  - created_at: The profile withheld every order statistic, so it is drawn within the run's time window (spec.timeBounds).
  - time window: No date or timestamp column publishes its range, so the run covers 2026-01-01 to 2026-12-31. Set spec.timeBounds to the span your data covers.

next:
  fluxsim run bundle --profile orders.profile.json
```

The directory must not exist yet, or must be empty: nothing is ever overwritten. The
profile stays outside the bundle, which holds only FLUX and FLUID documents; the
Simulation pins its digest instead. Before it writes a file, the command checks the
bundle as `fluxsim validate` does, and as `plan` does from the documents alone before a
row is drawn, and writes nothing either refuses.

## 3. What each number rests on

The rules run from the most the profile says to the least:

| The profile or the contract says | The bundle declares | Source in the register |
|---|---|---|
| a table's `rowCount` | the emit's `rows` | profile |
| a column's `min` and `max` | its `range`, which the fidelity check holds the run to | profile |
| `min`, the quartiles and `max` | histogram `bins` and `binWeights`, a quarter of the rows in each | profile |
| a mean: `sum` and `count`, or a `mean/1` statistic | the histogram shaped to that mean and, for a `mean/1` over a number column with a published range, a `mean` property the run is checked against | profile, with the shape assumed |
| `nullCount` | `nullFraction` | profile |
| a histogram, level counts or a list of levels | `bins` and weights, `levels`, and a property the run is scored on | profile |
| as many distinct values as values | `unique: true`: an identifier the engine derives | no number |
| an `enum(...)` type | `support: declared`, with `maxCardinality` its number of levels | contract |
| a key, in its `semantics` | no key of its own: the engine reads keys from the contract | no number |
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

A date or timestamp bound is RFC 3339 text, which the register does not count, so an
assumed time window is in the command's output and nowhere else. Here no time column
publishes its range, because no single instant is held by ten orders, and `created_at`
is drawn within the run's `timeBounds`.

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

## 5. Run it, check it, ship it

```console
$ fluxsim run bundle --profile orders.profile.json --verify --ship -o out
plan written to ...
coverage: orders: 1 of 4 columns fell back to a type default
  created_at (TIMESTAMP): uniform within the simulation's time bounds: the column declares no draw of its own
--strict-coverage refuses the run, and so does strictCoverage in the bundle; fluxsim explain FLX-3304 says how to cover a column
parameters: 34 declared -- 12 from the pinned profile, 19 assumed, 3 default
applied to ...
emitted orders: 600 rows (generator)
reproducible: not checked | validity: not asserted
...
  fidelity orders: passed -- 1 of 1 declared property within the margin against the pinned profile
  parameters: 34 declared -- 12 from the pinned profile, 19 assumed, 3 default
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
be checked against, as `fluid profile` wrote before it wrote `mean/1`, gives a bundle
whose one property `verify --profile` refuses with
[FLX-5037](../reference/errors/FLX-5037.md), and the command's output says so. Given more
statistics (histograms, level counts), `fluxsim init` declares a property for each one it
can carry: [Score a run against a profile](./score-a-run-against-a-profile.md) shows the
check.

Some columns carry no property at all. A boolean column's counts shape its draw, but
the check reads a category's values as text. A date column's bounds are declared in
days, while a profile reads a date as a timestamp in milliseconds. And an integer
column whose published values pass 2^53 is a placeholder, 0 on every row, because a
run's rows cannot hold such an integer exactly.

## What the command refuses

Nothing is written when:

- the directory holds anything, or cannot be written;
- `--contract` comes without `--profile`, or the reverse (exit 2);
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
