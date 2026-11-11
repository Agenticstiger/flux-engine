---
title: "Model your customers"
description: "Change the population in the World, add a second Persona, and add a Segment you check with fluxsim expr before a run uses it."
---

# Model your customers

Goal: change who your simulated customers are and see what changes in a run. The World sets the population, a Persona sets traits, and a Segment names a group with a `flux-expr/1` query.

## 1. Run the starter project (500 customers, 154 rows)

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
$ fluxsim run .
plan written to .flux/runs/8dec55892096169f/plan.json (runId=8dec55892096169f, planDigest=sha256:8dec55892096169f5b6a9281a379f5dc9c085418ad2d22d52543a34835c26bb3)
applied to .flux/runs/8dec55892096169f (runDigest=sha256:c4bb981d99467e59f8a89c546d59a35fb35f973c3abe77d70b2af1174604915f)
emitted payment_recovery_moment: 154 rows (scenario)
run 8dec55892096169f complete
```

## 2. Change the population

Replace `world.flux.yml`: 800 customers, more at risk, a fourth market.

```yaml
# world.flux.yml
fluxVersion: "0.5.0"
kind: World
id: q3-retention-world
name: Q3 Retention World
metadata: { owner: { team: growth-lab } }
spec:
  seed: 42
  vertical: telco
  sovereignty: { allowedZones: [eu-west, eu-central], enforcement: block }
  population:
    size: 800
    lifecycleMix: { new: 0.10, active: 0.50, atrisk: 0.30, churned: 0.10 }
    markets: [mkt-a, mkt-b, mkt-c, mkt-d]
  temperature: { engagement: 0.5, campaignResponse: 0.5 }
```

## 3. Add a second Persona

A Persona sets traits: a number, or a distribution drawn once per customer.

```yaml
# persona-budget.flux.yml
fluxVersion: "0.5.0"
kind: Persona
id: persona-budget-single
name: Budget Single Subscriber
metadata: { owner: { team: growth-lab } }
spec:
  traits:
    digitalFluency: 0.9
    priceSensitivity:
      dist: categorical
      levels: { low: 0.1, medium: 0.3, high: 0.6 }
    monthlySpend: { dist: normal, mean: 18.0, stddev: 6.0, min: 0 }
```

## 4. Test a Segment query before you use it

A query can only use attributes the engine knows; `fluxsim expr model --bundle .` prints them all. `fluxsim expr eval` is the only command in this release that checks a Segment's query: `validate`, `plan` and `run` accept a query with a misspelled attribute. So evaluate the query against a made-up customer first. `--values` is JSON keyed by attribute name:

```console
$ fluxsim expr eval "traits.priceSensitivity == 'high' && lifecycle == 'atrisk'" --bundle . --values '{"traits.priceSensitivity": "high", "lifecycle": "atrisk"}'
true
```

A misspelled attribute fails with FLX-6004. A query on `persona` fails with FLX-6003 unless the Simulation lists that persona in `personaRefs`.

## 5. Add the Segment and reference both documents

```yaml
# segment-price.flux.yml
fluxVersion: "0.5.0"
kind: Segment
id: seg-price-sensitive
name: Price-sensitive at-risk subscribers
metadata: { owner: { team: growth-lab } }
spec:
  queryLanguage: flux-expr/1
  query: "traits.priceSensitivity == 'high' && lifecycle == 'atrisk'"
```

The Simulation must list the new documents. Replace `simulation.flux.yml` (the contract digest is the starter's):

```yaml
# simulation.flux.yml
fluxVersion: "0.5.0"
kind: Simulation
id: sim-payment-recovery
name: Payment Recovery Simulation
metadata: { owner: { team: growth-lab } }
spec:
  worldRef: q3-retention-world
  fidelity: deterministic
  seed: 42
  timeBounds: { start: "2026-07-01T00:00:00Z", end: "2026-09-30T23:59:59Z" }
  personaRefs: [persona-pragmatic-family, persona-budget-single]
  segmentRefs: [seg-price-sensitive]
  signalRefs: [sig-payment-aborted]
  moduleRefs: [module-payment-recovery]
  emits:
    - productRef: telco.gold.payment_recovery_moment
      exposeId: payment_recovery_moment
      fluidVersion: "^0.7.3"
      provenance:
        contractDigest: "sha256:b9b5ee575a8b313f6f72d039653d8be5d76e7ecc0012d7bd6ad2d37e0e60435d"
```

## 6. Run and compare
```console
$ fluxsim run .
plan written to ...
applied to ... (runDigest=sha256:...)
emitted payment_recovery_moment: 278 rows (scenario)
run ... complete
```

The rows went from 154 to 278 and the digest changed. A bigger population means more aborted payments and more detector firings.

## What a Persona and a Segment change

In the starter project, adding the second Persona with the World left at 500 customers did this:

- It changed the drawn traits, so `population.ndjson` and the run digest changed. The mean `monthlySpend` fell from about 42 to about 30.
- It left the emitted rows unchanged: still 154, byte for byte.
- It changed journey outcomes slightly (66 transition events became 64).

Adding a valid Segment on top of that changed no row, event or journey. It did change the run id and the run digest, because a Segment is part of the bundle's input. The engine does not check the query when it validates, plans or runs, so test it with `fluxsim expr eval`, as in step 4, before you rely on it.

## The journey reaction

A Journey's `personaReactions` map a persona id to a `successProbability`, an `outcome` and a `nextState`. The starter has one entry, for `persona-pragmatic-family`. A run with the second persona and no entry for it validates and runs, and adding an entry left the events and journeys digests unchanged in the starter project. Add one anyway, so the journey states what each persona does. See [Add a journey](./add-a-journey.md).
