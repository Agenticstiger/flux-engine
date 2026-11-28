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

Both drawn columns say they are allocated exactly: 600 rows is a small table, so `init` wrote `allocation: exact` on them and `sampling: true` on their tolerances (see [How init reads a profile](../reference/how-init-reads-a-profile.md)).

The directory must not exist yet, or must be empty: nothing is ever overwritten. The
profile stays outside the bundle, which holds only FLUX and FLUID documents; the
Simulation pins its digest instead. Before it writes a file, the command checks the
bundle as `fluxsim validate` does, and as `plan` does from the documents alone before a
row is drawn, and writes nothing either refuses.

The `profile:` line is the command's ledger ([ADR 0014](../adr/0014-init-reads-every-statistic-a-profile-publishes.md)). It lists every statistic and metric of the profile, by its JSON Pointer, in one of three states. A statistic is used when a number of the bundle rests on it, a rule read it, a property is scored against it, or the bundle was checked against it. It is unused when none of these holds. It is withheld when the profile published no value. Each unused one is named with the reason no rule reads it. The Simulation lists them under `unused` beside the register. `plan`, `verify` and `ship` count them, and `ship` names each one under "What Was Not Proven" in `EVIDENCE.md`. In `--json`, `ledger.statistics` names
each statistic on its own, with its state, and `ledger.unused` is the Simulation's list: what an
owner can leave out of the profile. The profile reader sometimes needs one of an entry's statistics: a calendar entry holds at least one of its three histograms. Then `ledger.unused` names the whole entry when none of it is read, so a profile with exactly those pointers dropped is one the reader accepts. Drop that list, not every statistic marked unused.

A profile cut by dropping the `ledger.unused` list can leave an entry in part. Examples are a date pair with its `order` and no statistic, an exposure with one of its counts, or a history with some of its members. From 0.13.0 the engine reads each member that is left, checks it as in a whole
entry, and `init` and `plan` accept the profile. `init` then writes the bundle it wrote from the
whole profile. From 0.13.1 that holds for every case measured: it is checked in CI over the
repository's fixtures, and is not proved for every profile. In 0.13.0 a few shapes gave another
bundle, and its changelog lists them as known issues. A register
citation of a member that is gone is refused as any citation of an unpublished statistic is
([FLX-3113](../reference/errors/FLX-3113.md)). Engine 0.12.0 refused an entry cut in part.

`--strict` turns the ledger into a gate. It writes nothing, and exits 1, when a statistic is unused or an assumption stands in for something the profile did not publish. The tolerances and the bounds of a committed state are settings, not gaps. Each finding is named, and `--allow POINTER` accepts the ones at or below a profile
pointer or a Simulation pointer. An `--allow` that accepts nothing is refused too:

```console
$ fluxsim init strict --contract contract.orders.fluid.yaml --profile orders.profile.json --strict --allow /tables/orders/columns/created_at # exit 1
init: --strict refuses this bundle (1 statistic of the profile unused, 6 assumptions standing in for what the profile did not publish); nothing was written
  unused /tables/orders/columns/country/metrics/count (count/1): no rule of fluxsim init reads this statistic for this column
...
accept one with --allow POINTER (a profile pointer for a statistic, a Simulation pointer for a gap), or publish what the profile left out
```

## 3. What each number rests on

`init` reads each statistic by a fixed rule, from the most the profile says to the least. A published row count, a range, the quartiles, a mean, a null count, a histogram, level counts and the children per parent of a relationship each become the number of the bundle that they can hold. A fact of the contract fills in where the profile is silent, and an assumption states its reason where neither speaks. A table of two foreign keys and no other column is written as an association table when its conditions hold. [How init reads a profile](../reference/how-init-reads-a-profile.md) lists every rule and the register source each one gives.

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
profile of the contract). The ledger lists the statistics with how many numbers each would
replace. `init` also writes `requests/assumptions.profile-request.yml` inside the bundle, a
[profile request](../spec/PROFILE-REQUEST-SPEC.md) for exactly those statistics, which a
profiler measures where the data lives. Run `init` again with the profile it writes, and each
number a statistic answers is cited as the profile's. A tolerance, the bounds of a committed
state and the other settings have no statistic that replaces them, so they name none. The
request is part of the directory `init` writes, so it is created and removed with it, and the
loader and `inputDigest` do not read it (see [PARAMETER-REGISTER-SPEC.md](../spec/PARAMETER-REGISTER-SPEC.md) section 1.3).

If the profile is a private release, with noisy counts or a noisy history, or you want to declare a history yourself, [Write a bundle from a private release](./write-a-bundle-from-a-private-release.md) covers it.

## 5. Run it, check it, ship it

Before the run, check the bundle against the profile it pins. Plain `fluxsim validate` reads no
profile. `validate --profile` reads it and refuses what `plan` would refuse about it:

```text
fluxsim validate bundle --profile orders.profile.json
```

- [FLX-3109](../reference/errors/FLX-3109.md) and [FLX-3110](../reference/errors/FLX-3110.md):
  the profile is not the one the Simulation pins, or it cannot be read.
- [FLX-3105](../reference/errors/FLX-3105.md): an overlay names a table or a column the profile
  lacks.
- [FLX-3113](../reference/errors/FLX-3113.md): the register cites a table or a column the profile
  lacks.

A profile given to a Simulation that pins none, and the grade floor
([FLX-4005](../reference/errors/FLX-4005.md)), are left to `plan`. See
[`validate`](../reference/cli/validate.md).

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
declare. It is drawn within the run's time window. The evidence report names it under
"What Was Not Proven", beside the register's shares.

## 6. What the check does not cover

A run is checked against the statistics a profile publishes, and this one publishes a mean
for `amount` alone. Nothing checks the `country` levels, which the profile never names,
or `created_at`, whose time window is assumed. A profile with no statistic a property can
be checked against gives a table with `properties: []`. `fluid profile` wrote such profiles before it wrote `mean/1`.
In that case `init` writes no property that `verify --profile` would refuse with
[FLX-5037](../reference/errors/FLX-5037.md). It names what it left out in its output (and under
`leftOut` of the table in `--json`). It also says the table has nothing to verify. Given more
statistics (histograms, level counts), `fluxsim init` declares a property for each one it
can carry: [Score a run against a profile](./score-a-run-against-a-profile.md) shows the
check.

A table the profile gives no statistic to score, such as one whose columns have only a
minimum and a maximum (a date column's usual case), gets `properties: []`. The block keeps each column's range, and the command
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
changed, and the profile for every other number. A number the patch changed cannot be cited
as the profile's, and `plan` refuses that ([FLX-3113](../reference/errors/FLX-3113.md)).

A patch may touch only numbers a profile publishes: a table's `rowCount`, a column's `metrics`
and `statistics`, a table's `columnPairs` and `periods`, and the top-level `relationships`
and `invariants`. A patch replaces a list whole, so it restates a column's `statistics` to
change one of them, and a statistic it restates carries no `noise`. Anything else is refused ([FLX-3105](../reference/errors/FLX-3105.md)). So are a table or a column the profile does not hold, and a patch that makes a document the profile reader refuses. So is a new `rowCount` for a table that publishes a `history`: its rows are the versions of the history's entities, which no patch can change, so more rows would only lengthen each chain. `init` and `plan` refuse a patch that restates a column's `mean/1` beyond what the column's published minimum, maximum, quantiles and histogram allow ([FLX-3402](../reference/errors/FLX-3402.md)). An example is a mean of 69 for a column whose quarters sit at or below 20, 35, 50 and 100. No column the profile describes has that mean, and the message gives the bound it breaks. Run each
bundle with the profile as published: `plan` and `verify` read the overlay from the bundle and
the plan, `verify` scores against the overlaid profile, and the scorecard names the overlay.

An overlay never raises or lowers the privacy grade, which reads the profile as published, and
its numbers are the maintainers' scenario, not a measurement: `EVIDENCE.md` says so. An overlay
needs kernel 0.2.0.

## What the command refuses

Nothing is written when:

- the directory holds anything, or cannot be written;
- `--contract` comes without `--profile`, or the reverse, or `--overlay` or `--window` comes without both (exit 2);
- a `--window` value is not a date or an RFC 3339 timestamp in the years 1 to 9999 (exit 2);
- a `--window` START is not before END (exit 2);
- a `--window` END is in the year 9999, which spells an open end rather than an instant (exit 2);
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

`init` names an expose it leaves out of the bundle, with the reason. It leaves one out when:

- the profile does not describe it;
- a column of it is JSON or bytes (the generator has no draw for them);
- a DECIMAL column of it has more than 38 decimal places;
- no column of it has a range or levels a property could read.

The exposes of one contract
are generated together, so a foreign key into an expose left out is refused, as `plan`
refuses it ([FLX-3403](../reference/errors/FLX-3403.md)), and nothing is written. If the
profile has no table for the expose it references, profile that table too. If the command
left that expose out for another reason, such as a JSON column, the relationship cannot be
generated as the contract declares it.
