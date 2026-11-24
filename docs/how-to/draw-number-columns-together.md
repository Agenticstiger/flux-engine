---
title: "Draw number columns together"
description: "Declare a Gaussian copula so two number or timestamp columns are drawn with a stated rank correlation, keep each column's own histogram, and score the correlation against a profile with a rankCorrelation property."
---

# Draw number columns together

Use this page when two number or date columns of one table move together in your data, such as a larger amount for a later payment, and the generated rows should keep that. Without a declaration the generator draws every column on its own. Each column keeps its histogram, and any link between two columns is lost.

A `copula` declares which columns are drawn together and how strongly. The strength is a rank correlation, Kendall's tau or Spearman's rho, one number per pair. A rank correlation does not depend on a column's histogram, so the column keeps the histogram you declared, and a pair of columns can be given a correlation whatever their histograms are. It also reads the same for a date in days or in milliseconds. A profile of the real data can publish the numbers, exactly or with noise ([PROFILE-SPEC.md](../spec/PROFILE-SPEC.md) section 5), so the declaration holds counts of aggregates and never a record.

A copula needs kernel 0.2.0, which a new plan takes. Under kernel 0.1.0 the plan is refused with [FLX-3105](../reference/errors/FLX-3105.md), which names the kernel.

## What a copula column is

Each column of a copula is a `number` or `timestamp` column of the emit that:

- is drawn from `bins` and `binWeights`, the histogram it would have without the copula;
- is drawn given nothing, and no other column is drawn given it, because a column has one joint;
- has no `allocation`, and is set by nothing else: not a key, not a rule, not a `lifetimes`, `within` or `rates` entry, not a copy.

The copula holds 2 to 64 such columns and a rank correlation for each pair you list. A pair you leave out is 0. Anything the plan cannot draw is refused with [FLX-3402](../reference/errors/FLX-3402.md), naming where and never a value.

## Steps

1. Start from the starter project.

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
```

2. Get the profile. The data owner sends it, and it holds one number per pair. This page writes one by hand, so it runs anywhere: it says that over 1,000 rows the Kendall rank correlation of `amount` and `aborted_at` has `S = 249750`, which is a tau of 0.5. Tau is `S` over `C(1000, 2) = 499500`. Keep the file outside the bundle directory.

```console
$ cat > ../source.profile.json <<'EOF'
{
  "_producer": "https://example.com/hand-written-profile",
  "_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile/1-4-0/FluxProfile.json",
  "encoding": { "fractionalBits": 16, "rounding": "floor" },
  "disclosureRules": {
    "acro-default": {
      "minimumCell": 10,
      "zerosDisclosive": true,
      "roundingBase": 5,
      "dominance": { "n": 2, "k": "0.90" },
      "pRatio": "0.10"
    }
  },
  "tables": {
    "payment_recovery_moment": {
      "columns": {
        "amount": { "domain": "number" },
        "aborted_at": { "domain": "timestamp" }
      },
      "columnPairs": [
        {
          "columns": ["amount", "aborted_at"],
          "statistics": [
            {
              "definition": "kendallTau/1",
              "sampleSize": { "min": 1000, "max": 1000 },
              "disclosure": "acro-default",
              "groups": [{ "group": "", "k": 1000, "S": "249750", "status": "F" }]
            }
          ]
        }
      ]
    }
  }
}
EOF
```

3. Pin the profile and declare the copula. Replace `simulation.flux.yml`. `amount` and `aborted_at` each declare `bins` and `binWeights`, and the `copula` block under `fidelity` names them with the measure and the pair's value. The `rankCorrelation` property is the check: the run's tau must be within 0.05 of the profile's, widened for the number of rows the run has. The digest pins the profile above.

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
    profile:
      digest: sha256:0054902b34439ab59ed57e17d1ae062e0e3fca30d9834e42101f0759ed685fa1
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
            aborted_at:
              range: { min: "2026-07-01T00:00:00Z", max: "2026-09-30T23:59:59Z" }
              bins: ["2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z", "2026-09-30T23:59:59Z"]
              binWeights: ["31", "31", "30"]
              null: fail
            market:
              support:
                values: ["IE", "GB", "FR", "DE"]
              maxCardinality: 8
              null: fail
          copula:
            columns: [amount, aborted_at]
            rankCorrelations:
              measure: kendall
              pairs:
                - { columns: [amount, aborted_at], value: "0.5" }
          properties:
            - property: rankCorrelation
              columns: [amount, aborted_at]
              measure: kendall
              tolerance: { type: absolute, max: "0.05" }
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

The `value` is a rank correlation of the population, in `[-1, 1]`, written as text. For Kendall's measure the plan turns it into the correlation of a Gaussian copula with `sin(pi * tau / 2)`, and for Spearman's with `2 * sin(pi * rho / 6)`. A profile that publishes the statistic with noise gives a number that can lie outside `[-1, 1]`: clamp it to `[-1, 1]` before you declare it, which is post-processing of the release.

4. Run it and score it. `fluxsim run --verify` plans, draws the rows once and scores them against the profile:

```console
$ fluxsim run . --profile ../source.profile.json --verify
plan written to .flux/runs/.../plan.json (runId=..., planDigest=sha256:...)
applied to .flux/runs/... (runDigest=sha256:...)
emitted payment_recovery_moment: 2000 rows (generator)
reproducible: not checked | validity: not asserted
...
  fidelity payment_recovery_moment: passed -- 1 of 1 declared property within the margin against the pinned profile
  scorecard: overall ... against profile sha256:... (covers 2 of 4 columns, 0 of 0 relationships)
    dimensions: joint ...; not measured: structure, marginals, crossTable, temporal, invariants, queries, defects
...
run ... complete
```

The property sits in the `joint` dimension. The score is one minus half the distance between the run's tau and the profile's, so a run that has the profile's tau scores 1.

## Look at what the plan decided

A rank correlation matrix must be positive definite before it can be drawn from. The plan makes it so in pinned arithmetic and, when it has to, shrinks every correlation towards 0 by the same factor, `alpha`. With two columns there is nothing to shrink, so `alpha` is `0`. The plan records it, with a digest of the matrix factor, in the emit's entry:

```python
import glob
import json

demo = f"{workdir}/demo"
plan = json.load(open(glob.glob(f"{demo}/.flux/runs/*/plan.json")[0]))
copula = plan["emits"]["payment_recovery_moment"]["copula"]
print(copula["alpha"])
assert copula["alpha"] == "0"
```

A copula over many columns whose published correlations cannot all hold together is shrunk, and `alpha` says by how much: every correlation is multiplied by `1 - alpha`. `EVIDENCE.md` says so, and the scorecard's row carries `alpha` for the reader.

## Check the rows yourself

The generated rows are in `.flux/runs/<run>/emits/`. Kendall's tau of the two columns, counted pair by pair, should be close to the declared 0.5. The run's tau is that of 2,000 draws, so it differs from 0.5 by sampling alone.

```python
import glob
import json
from fractions import Fraction

demo = f"{workdir}/demo"
path = glob.glob(f"{demo}/.flux/runs/*/emits/payment_recovery_moment.ndjson")[0]
rows = [json.loads(line) for line in open(path)]
# A row is a list, in the contract's column order: subscriber_id, aborted_at, amount, market.
when = [row[1] for row in rows]
amount = [Fraction(row[2]) for row in rows]
n = len(rows)
total = 0
for i in range(n):
    for j in range(i + 1, n):
        total += ((amount[i] > amount[j]) - (amount[i] < amount[j])) * ((when[i] > when[j]) - (when[i] < when[j]))
tau = total / (n * (n - 1) / 2)
print(round(tau, 3))
assert abs(tau - 0.5) < 0.06
```

A run draws the same rows for the same bundle and seed, on any machine, with any `--jobs` and `--shard`; `--scale` only changes how many.

## What the check allows for

`rankCorrelation` widens the margin by the noise of a profile released under differential privacy and by the size of the run: a small run cannot be held to a margin its own size cannot meet. [SCORECARD-SPEC.md](../spec/SCORECARD-SPEC.md) section 9b gives the formulas. When the margin and both widenings add up to 2 or more, every run is within and the row says the reference is too noisy to tell.

A committed slice and `verify --sample` do not score the property, because the sums that form a rank correlation do not add up across blocks. They name it and compare nothing.

## What it does not do

- A column drawn through a copula is independent of the categorical columns. To tie a number column to a category, draw it given the category ([Draw a column given another](./draw-a-column-given-another.md)), which makes it no copula column.
- A column with few distinct values keeps its rank correlation less well, because ties limit what a rank correlation can say. A clique of the profile fits such a column better.
- A Gaussian copula has no tail dependence: the extremes of two columns come together less often than in a heavy-tailed source.
