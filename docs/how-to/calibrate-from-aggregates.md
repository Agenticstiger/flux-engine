---
title: "Calibrate from aggregates: declare a scorecard"
description: "Declare where a Playback's dials came from, read its scorecard back with fluxsim calibrate, and see how an emit's minCredibility uses it. Nothing is fitted in this release."
---

# Calibrate from aggregates: declare a scorecard

Use this page to attach a Playback to a FLUX bundle, read its declared scorecard back, and gate an emit on the credibility it states.

::: warning What calibration means in version 0.1
Calibration here is declared-scorecard calibration. A Playback states where its dials came from and carries a scorecard. `fluxsim calibrate` reads that scorecard and reports it. No fitting is performed, and the optional fitting backends are not part of this release. `fluxsim describe --self` lists `calibrate` among the capabilities that are on and `calibrate_abc` among those that are not available.
:::

The rule behind the scorecard is that a Playback describes aggregate statistics only (rates, mixes, counts), never record-level customer data. In this release the engine reads nothing from a Playback's `sourceStream`; the field only names where the numbers came from.

## Steps

1. Start from the starter project.

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
```

2. Add a Playback document. It names the world it calibrates and the scorecard it declares. The `grade`, `credibility` and `intervalCoverage` values are what the author states, not what the engine measured. The starter's persona and world already exist, so no other document is needed.

```yaml
# playback.flux.yml
fluxVersion: "0.5.0"
kind: Playback
id: pb-billing-history
name: Billing History Calibration
metadata:
  owner:
    team: growth-lab
spec:
  worldRef: q3-retention-world
  sourceStream: kafka://billing.events.v1
  personaMapping:
    - behaviorPattern: "late_payment_count >= 2"
      mapToPersona: persona-pragmatic-family
  calibration:
    targets: [population.lifecycleMix, eventRates]
    window: P90D
    method: mle
  drift: { metric: psi, threshold: 0.2, onBreach: warn }
  scorecard:
    backtestWindow: P90D
    grade: B
    intervalCoverage: 0.92
    credibility: 0.78
```

3. Validate the bundle.

```console
$ fluxsim validate .
.: valid -- 10 FLUX document(s), 1 FLUID contract(s), 1 Simulation(s)
```

4. Read the scorecard back. This prints it and writes nothing.

```console
$ fluxsim calibrate .
pb-billing-history: credibility declared, not graded
```

Add `--json` to see the whole scorecard. The `grade` is `null`, the `credibility` is the word `declared`, and `methodId` is `flux.calibration.declared/1`. The engine never copies your letter grade into the graded field, because that field is reserved for a computed score. Your block is kept behind `evidenceDigest`.

```console
$ fluxsim calibrate . --json
{
...
    "scorecard": {
      "backtestWindow": "P90D",
      "credibility": "declared",
      "evidenceDigest": "sha256:37c906285b8fd5b90ac455f82850a5a8f276222700bfa5b83670fb4280c63bb6",
      "grade": null,
      "intervalCoverage": "0.92",
      "methodId": "flux.calibration.declared/1",
      "provenance": "declared"
    }
...
}
```

5. Run the bundle, then record the calibration in the run directory. `$(ls .flux/runs)` supplies the one run id in this fresh project; use the id `fluxsim run` printed.

```console
$ fluxsim run .
...
run ... complete
$ fluxsim calibrate . --write --run-id $(ls .flux/runs)
wrote .flux/runs/.../calibration-provenance.json
```

6. Open the file it wrote. In this release it records the world and an empty `dials` map, because nothing was fitted.

```console
$ cat .flux/runs/*/calibration-provenance.json
{
  "dials": {},
...
  "kind": "CalibrationProvenance",
...
  "worldRef": "q3-retention-world"
}
```

## Gate an emit on the scorecard

An emit can require a minimum credibility with `minCredibility`. The check compares that number with the `credibility` number in a Playback bound to the same world. It does not look at the word `declared`.

7. Ask for more than the scorecard states.

```yaml
# simulation.flux.yml
fluxVersion: "0.5.0"
kind: Simulation
id: sim-payment-recovery
name: Payment Recovery Simulation
metadata:
  owner:
    team: growth-lab
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
      minCredibility: 0.9
      provenance:
        contractDigest: "sha256:b9b5ee575a8b313f6f72d039653d8be5d76e7ecc0012d7bd6ad2d37e0e60435d"
```

The bundle is refused. In this release the refusal is reported as `FLX-3105`, with the credibility message in its detail.

```console
$ fluxsim validate .   # exit 1
FLX-3105: bundle does not validate, so its seam cannot be resolved -- 
  sim-payment-recovery: spec.emits[0]: twin credibility 0.78 is below the required minCredibility 0.9
more: fluxsim explain FLX-3105  https://agenticstiger.github.io/flux/engine/reference/errors/FLX-3105.html
```

8. Lower the requirement to 0.7, or raise the scorecard's `credibility`. The bundle validates again.

```yaml
# simulation.flux.yml
fluxVersion: "0.5.0"
kind: Simulation
id: sim-payment-recovery
name: Payment Recovery Simulation
metadata:
  owner:
    team: growth-lab
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
      minCredibility: 0.7
      provenance:
        contractDigest: "sha256:b9b5ee575a8b313f6f72d039653d8be5d76e7ecc0012d7bd6ad2d37e0e60435d"
```

```console
$ fluxsim validate .
.: valid -- 10 FLUX document(s), 1 FLUID contract(s), 1 Simulation(s)
```

## What a declared scorecard tells you

- It tells you what the author of the Playback states about the model's backtest, and which world it applies to.
- It tells you the bundle's gate was met by that stated number.
- It does not tell you the model is accurate. Nobody has measured it against the world.
- It does not tell you the stated numbers are true. The engine reads them and does not recompute them.
- The `grade` letter is yours. The engine leaves its own graded field empty.

To measure predictions against outcomes you need a scoring step, which is outside this page. See [the six-step loop](../explanation/the-six-step-loop.md) and the [`calibrate` reference](../reference/cli/calibrate.md).
