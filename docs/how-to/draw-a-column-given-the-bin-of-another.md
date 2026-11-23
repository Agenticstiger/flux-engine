---
title: "Draw a column given the bin of another"
description: "Declare a column drawn given the bin of a number column, with weights from a table grouped by that column's bins, and see the fidelity gate check the link with groupByBins."
---

# Draw a column given the bin of another

Use this page to make the generator draw `market` given `amount`: small amounts mostly come from one market, large amounts from another. The parent is a number column, so each of its bins is a class, and each bin gets its own weights for `market`. The weights come from a table of your data grouped by the bins of `amount`, so the declaration holds counts and never a record.

[Draw a column given another](./draw-a-column-given-another.md) does this for a parent that is a category. A number column draws a bin first and then a place inside it, so the bin is a level the generator already draws. This page names it as one.

## What the parent must be

`given` may name a column of the emit that:

- is a `number` or `timestamp` column drawn from `bins` and `binWeights` (or `binWeightsGiven`);
- declares no `nullFraction`, so every row has exactly one bin to look up;
- is not a key, and is not set by a rule, a `lifetimes`, `within` or `rates` entry, or a copy.

The classes are the bins. In `levelWeightsGiven` and `binWeightsGiven` they are keyed by the text of each bin's lower edge, written as the parent's `bins` write it, and every bin has an entry. A column of a [copula](./draw-number-columns-together.md) is no parent, because it has one joint, and a column drawn with `allocation: exact` cannot be given a binned parent. Either is refused with [FLX-3402](../reference/errors/FLX-3402.md). The declaration needs kernel 0.2.0, which a new plan takes; under kernel 0.1.0 the plan is refused with [FLX-3105](../reference/errors/FLX-3105.md), which names the kernel.

Each class draws on its own stream at the place a histogram or a category draws at, and the parent's bin only chooses the table the column's draw is searched in. So the bytes of every other column are the ones they were, and nothing moves for a bundle that does not declare it.

## Count the grouped table

`levelWeightsGiven` takes one mapping per bin of the given column. Those mappings are the counts of a table grouped by the bins of `amount`: in your own data, count the rows in each (amount bin, market) cell, over the bins you declare. A `levelHistogram` property of `market` with `groupByBins` measures the same counts, which is how the fidelity check below compares them with the generated rows.

Counts can still single out people: a cell that holds one or two rows describes a handful of real records. Apply your own disclosure rule, such as a minimum cell count, before you declare a table.

The counts in this guide are made up. The table has 1,000 rows, in three amount bins:

```text
amount bins:   1-100  100-300  300-1000
DE               300       70         8
FR               125      140        15
GB                50      105        52
IE                25       35        75
```

## Steps

1. Start from the starter project.

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
```

2. Declare the emit's `fidelity` block. `amount` declares `bins` and `binWeights`. `market` names `amount` in `given` and carries one mapping per bin of `amount`, keyed by the bin's lower edge, in place of `levels`. The `levelHistogram` property groups `market` by the same bins: `groupByBins` takes the column and its edges, and the edges cover the column's declared range.

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
        rows: 2000
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 4, max: 100000 }
          columns:
            amount:
              range: { min: "1", max: "1000" }
              bins: ["1", "100", "300", "1000"]
              binWeights: ["50", "35", "15"]
              null: exclude
            market:
              support:
                values: ["IE", "GB", "FR", "DE"]
              maxCardinality: 8
              given: amount
              levelWeightsGiven:
                "1":   { DE: "60", FR: "25", GB: "10", IE: "5" }
                "100": { DE: "20", FR: "40", GB: "30", IE: "10" }
                "300": { DE: "5", FR: "10", GB: "35", IE: "50" }
              null: fail
            aborted_at:
              range: { min: "2026-07-01T00:00:00Z", max: "2026-09-30T23:59:59Z" }
              null: fail
          properties:
            - property: levelHistogram
              column: market
              groupByBins: { column: amount, bins: ["1", "100", "300", "1000"] }
              tolerance: { type: distance, measure: totalVariation, max: "0.1" }
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

The weights are counts here, and shares work too: each bin's mapping is normalised on its own. Every bin needs an entry, and every entry needs some weight and names exactly the levels `market` can draw, or the plan is refused with `FLX-3402` before any row is drawn.

3. Validate the bundle and run it.

```console
$ fluxsim validate .
.: valid -- 9 FLUX document(s), 1 FLUID contract(s), 1 Simulation(s)
$ fluxsim run .
...
emitted payment_recovery_moment: 2000 rows (generator)
run ... complete
```

The generator draws `amount` first, finds the bin it landed in, and draws `market` with the weights of that bin. The same bundle and seed give the same bytes, and a row's `amount` is the value it would have been without `given`.

## Check it against your data

The fidelity check needs a committed slice: rows you hold, one JSON object per line. This guide writes one from the counts above, with the amounts spread evenly inside each bin. In real use the slice is your own data.

```python
import json
from decimal import Decimal
from pathlib import Path

edges = [Decimal(edge) for edge in ["1", "100", "300", "1000"]]
counts = [
    {"DE": 300, "FR": 125, "GB": 50, "IE": 25},
    {"DE": 70, "FR": 140, "GB": 105, "IE": 35},
    {"DE": 8, "FR": 15, "GB": 52, "IE": 75},
]
lines = []
for index, row in enumerate(counts):
    total = sum(row.values())
    low, high = edges[index], edges[index + 1]
    position = 0
    for market, count in row.items():
        for _ in range(count):
            amount = (low + (high - low) * (2 * position + 1) / (2 * total)).quantize(Decimal("0.0001"))
            position += 1
            n = len(lines) + 1
            record = {
                "subscriber_id": f"s-{n:04d}",
                "aborted_at": f"2026-08-{1 + n % 28:02d}T12:00:00Z",
                "amount": str(amount),
                "market": market,
            }
            lines.append(json.dumps(record))
Path(workdir, "slice.ndjson").write_text("\n".join(lines) + "\n")
assert len(lines) == 1000
```

4. Verify the run against the slice. The distribution of `market` within each amount bin is within the declared distance of the slice's.

```console
$ fluxsim verify . --run-id $(ls -t .flux/runs | head -1) --committed-slice payment_recovery_moment=file://$(dirname "$PWD")/slice.ndjson
reproducible: not checked | validity: not asserted
...
  fidelity payment_recovery_moment: passed
```

## See the check catch a missing link

5. Declare `market` without `given`: one `levels` mapping of shares, the same for every amount.

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
        rows: 2000
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 4, max: 100000 }
          columns:
            amount:
              range: { min: "1", max: "1000" }
              bins: ["1", "100", "300", "1000"]
              binWeights: ["50", "35", "15"]
              null: exclude
            market:
              support:
                values: ["IE", "GB", "FR", "DE"]
              maxCardinality: 8
              levels: { DE: "0.35", FR: "0.35", GB: "0.20", IE: "0.10" }
              null: fail
            aborted_at:
              range: { min: "2026-07-01T00:00:00Z", max: "2026-09-30T23:59:59Z" }
              null: fail
          properties:
            - property: levelHistogram
              column: market
              groupByBins: { column: amount, bins: ["1", "100", "300", "1000"] }
              tolerance: { type: distance, measure: totalVariation, max: "0.1" }
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

6. Run it and verify it against the same slice. The overall shares of `market` are about those of the slice, but the mix inside the amount bins is not, so the grouped property is outside its margin.

```console
$ fluxsim run .
...
run ... complete
$ fluxsim verify . --run-id $(ls -t .flux/runs | head -1) --committed-slice payment_recovery_moment=file://$(dirname "$PWD")/slice.ndjson   # exit 1
reproducible: not checked | validity: not asserted
...
  fidelity payment_recovery_moment: failed
    property 000:levelHistogram:market@amount#bins (levelHistogram of market): outside the declared margin...
payment_recovery_moment: fidelity check failed
  property 000:levelHistogram:market@amount#bins (levelHistogram of market): outside the declared margin...
```

The property's id says what was compared: its place in your `properties` list, its kind and column, and, after `@`, the column it is grouped by, with `#bins` when it is grouped by bins. A group is named by its place in the order of the bins, counted from zero, and never by an edge or a market, because those are values from your slice.

## What `groupByBins` takes

- `column`: another number or timestamp column of the emit. `bins`: at least two strictly increasing edges on the grid, in the column's domain (days for a DATE column), covering the column's declared range, so that every value lies in a bin. A bin is half open and the last is closed.
- A property holds at most one of `groupBy` and `groupByBins`. A profile of the data that holds the same statistic with the same `groupByBins` can answer the property too ([PROFILE-SPEC.md](../spec/PROFILE-SPEC.md) section 3), exactly or with noise, and a sampled `verify` leaves such a property out.

## Fixing values of another column

A group of [`conditions`](./fix-values-and-seed-rows.md) that fixes `market` changes the weights of the bins of `amount` to their posterior: the rows of the group draw `amount` given the market they hold, exactly. A group still fixes categorical columns only, and `amount` is drawn, not fixed.

## Links between more columns

The suggestion of links from two-way tables, `flux_engine.calibrate.structure`, still draws a histogram column as a leaf of the forest it builds, so it does not name a number column as a parent. Declare such a link by hand as this page does.
