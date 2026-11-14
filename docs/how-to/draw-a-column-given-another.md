---
title: "Draw a column given another"
description: "Declare a histogram column drawn given a categorical one, with weights from a grouped histogram's counts, and see the fidelity gate check the link."
---

# Draw a column given another

Use this page to make the generator draw `amount` given `market`: each market gets its own amount histogram instead of one histogram shared by every market. The weights come from a grouped histogram of your data, so the declaration holds counts and never a record.

Without `given`, the generator draws every column on its own. The rows keep each column's distribution, but any link between two columns is lost: if your German customers abort small payments and your Irish ones large payments, the generated rows mix them.

## Count the grouped histogram

`binWeightsGiven` takes one list of bin weights per level of the given column. Those lists are the counts of a grouped histogram: in your own data, count the rows in each (market, amount bin) cell, over the bins you declare. A grouped `marginalHistogram` property measures the same counts, which is how the fidelity check below compares them with the generated rows.

Counts can still single out people: a cell that holds one or two rows describes a handful of real records. Apply your own disclosure rule, such as a minimum cell count, before you declare a table.

The counts in this guide are made up. Each market has 100 rows, spread over five amount bins:

```text
bins:  1-100  100-200  200-300  300-400  400-10000
DE        30       50       15        5          0
FR         5       30       45       15          5
GB         0       10       40       35         15
IE         0        0       10       30         60
```

## Steps

1. Start from the starter project.

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
```

2. Declare the emit's `fidelity` block. `amount` declares its `bins`, names `market` in `given`, and carries one weight list per market in `binWeightsGiven`, in place of `binWeights`. The grouped `marginalHistogram` property checks the same table: it groups `amount` by `market` over the same bins.

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
              range: { min: "1", max: "10000" }
              bins: ["1", "100", "200", "300", "400", "10000"]
              given: market
              binWeightsGiven:
                DE: ["30", "50", "15", "5", "0"]
                FR: ["5", "30", "45", "15", "5"]
                GB: ["0", "10", "40", "35", "15"]
                IE: ["0", "0", "10", "30", "60"]
              null: exclude
            market:
              support:
                values: ["IE", "GB", "FR", "DE"]
              maxCardinality: 8
              null: fail
            aborted_at:
              range: { min: "2026-07-01T00:00:00Z", max: "2026-09-30T23:59:59Z" }
              null: fail
          properties:
            - property: marginalHistogram
              column: amount
              groupBy: market
              bins: ["1", "100", "200", "300", "400", "10000"]
              tolerance: { type: distance, measure: totalVariation, max: "0.1" }
            - property: support
              column: market
              tolerance: { type: count, max: 0 }
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

The weights are counts here, and shares work too: each market's list is normalised on its own. Every market that `market` can draw needs a list, and every list needs some weight, or the plan is refused with `FLX-3402` before any row is drawn. The given column must be a categorical with no `nullFraction`, so that every row has exactly one market to look up.

3. Validate the bundle and run it.

```console
$ fluxsim validate .
.: valid -- 9 FLUX document(s), 1 FLUID contract(s), 1 Simulation(s)
$ fluxsim run .
...
emitted payment_recovery_moment: 2000 rows (generator)
run ... complete
```

The generator draws `market` first and then `amount` with the weights of the market that row drew. Each cell still has its own stream, so the same bundle and seed give the same bytes, and a row's `market` is the value it would have been without `given`.

## Check it against your data

The fidelity check needs a committed slice: rows you hold, one JSON object per line. This guide writes one from the counts above, with the amounts spread evenly inside each bin. In real use the slice is your own data.

```python
import json
from decimal import Decimal
from pathlib import Path

edges = [Decimal(edge) for edge in ["1", "100", "200", "300", "400", "10000"]]
counts = {
    "DE": [30, 50, 15, 5, 0],
    "FR": [5, 30, 45, 15, 5],
    "GB": [0, 10, 40, 35, 15],
    "IE": [0, 0, 10, 30, 60],
}
lines = []
for market, row in counts.items():
    for index, count in enumerate(row):
        low, high = edges[index], edges[index + 1]
        for j in range(count):
            amount = (low + (high - low) * (2 * j + 1) / (2 * count)).quantize(Decimal("0.0001"))
            n = len(lines) + 1
            record = {
                "subscriber_id": f"s-{n:04d}",
                "aborted_at": f"2026-08-{1 + n % 28:02d}T12:00:00Z",
                "amount": str(amount),
                "market": market,
            }
            lines.append(json.dumps(record))
Path(workdir, "slice.ndjson").write_text("\n".join(lines) + "\n")
assert len(lines) == 400
```

4. Verify the run against the slice. The grouped histogram of the generated rows is within the declared distance of the slice's.

```console
$ fluxsim verify . --run-id $(ls -t .flux/runs | head -1) --committed-slice payment_recovery_moment=file://$(dirname "$PWD")/slice.ndjson
reproducible: not checked | validity: not asserted
...
  fidelity payment_recovery_moment: passed
```

## See the check catch a missing link

5. Declare the same amounts without `given`: one `binWeights` list, each bin the sum of its column in the table above. Every market now draws from the same histogram.

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
              range: { min: "1", max: "10000" }
              bins: ["1", "100", "200", "300", "400", "10000"]
              binWeights: ["35", "90", "110", "85", "80"]
              null: exclude
            market:
              support:
                values: ["IE", "GB", "FR", "DE"]
              maxCardinality: 8
              null: fail
            aborted_at:
              range: { min: "2026-07-01T00:00:00Z", max: "2026-09-30T23:59:59Z" }
              null: fail
          properties:
            - property: marginalHistogram
              column: amount
              groupBy: market
              bins: ["1", "100", "200", "300", "400", "10000"]
              tolerance: { type: distance, measure: totalVariation, max: "0.1" }
            - property: support
              column: market
              tolerance: { type: count, max: 0 }
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

6. Run it and ship it against the same slice. Each column still matches on its own, but the grouped histogram does not, so the gate withholds the emit.

```console
$ fluxsim run .
...
run ... complete
$ fluxsim ship . --run-id $(ls -t .flux/runs | head -1) -o ../out --committed-slice payment_recovery_moment=file://$(dirname "$PWD")/slice.ndjson   # exit 1
withheld: payment_recovery_moment
  - properties.outcomes: 1 of 2 declared properties are outside their margins
  - property 000:marginalHistogram:amount@market (marginalHistogram of amount): outside the declared margin ...
nothing was written to the output directory
FLX-8001: withheld payment_recovery_moment
more: fluxsim explain FLX-8001  https://agenticstiger.github.io/flux/engine/reference/errors/FLX-8001.html
```

## Pick the links from your tables

With more columns, you choose which column each one is drawn given. `flux_engine.calibrate.structure` suggests the choice from two-way count tables alone. It builds a Chow-Liu tree: the spanning tree with the most mutual information between linked columns (Chow and Liu, 1968), the selection step of the MST synthesizer (McKenna, Miklau and Sheldon, 2021), without its privacy noise. A histogram column is always a leaf, because a bin is not a level another column can be drawn given. `declarations` turns the tree into the `columns` entries to declare. The module is outside the governed `flux_engine.api` surface (see the [Python API](../reference/python-api.md) reference), so its names can still change.

```python
from dataclasses import dataclass
from typing import List

from flux_engine.calibrate.structure import declarations, pair_table_from_groups, suggest_tree


@dataclass
class Group:
    group: str
    bin_counts: List[int]


table = pair_table_from_groups("market", "amount", [Group(m, row) for m, row in counts.items()])
edges = suggest_tree([table], categorical=["market"], numeric=["amount"])
columns = declarations(edges, [table], categorical=["market"], numeric=["amount"])
assert [(edge.parent, edge.child) for edge in edges] == [("market", "amount")]
assert columns["amount"]["given"] == "market"
assert columns["amount"]["binWeightsGiven"]["IE"] == ["0", "0", "10", "30", "60"]
```

A categorical column can be drawn given another categorical with `levelWeightsGiven`, one mapping of level to weight per level of the given column, in place of `levels`. Columns linked this way form a chain or a tree, and a cycle is refused.

## What the check covers

- A histogram column drawn given a categorical one is checked end to end: a grouped `marginalHistogram` over the same bins compares the generated (market, bin) counts with your slice's.
- A categorical column drawn given another is not. Its `levelWeightsGiven` table comes from your own cross-tab, and the committed statistic has no two-way count property for two categorical columns yet, so the gate checks each column's own levels but not the link between them.
- A pass says the generated rows sit inside the declared margins against this slice. It says nothing about rows the slice does not hold.
