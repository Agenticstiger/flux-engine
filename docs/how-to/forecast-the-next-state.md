---
title: "Forecast the next state and score it"
description: "Declare a response family and forecasts, make them with fluxsim forecast make before the next profile exists, score them against it with fluxsim forecast score, read what drift flags, and see every forecast in the evidence pack."
---

# Forecast the next state and score it

Use this page when you run an offer on subscribers and want the twin to say, before the next
period is seen, what that period will show, and then to be scored on it whichever way the
score falls. A forecast is made from one profile of your data, the state the bundle pins. It is
scored against the next profile, a later release answering the same request. The design is
[ADR 0029](../adr/0029-response-families-forecast-the-next-state-and-every-forecast-is-scored.md).

The steps:

1. declare a response family, the effect of the offer by segment, and the forecasts to make;
2. `fluxsim forecast make` runs the Simulation's sweep and seals each forecast before the next
   state exists;
3. `fluxsim forecast score` reads the next state and gives every forecast a verdict, with two
   scores and a drift check between the two states;
4. `fluxsim ship` puts every forecast, hit or miss, in the evidence pack;
5. the next pass starts from the state that was just observed.

## 1. The scenario and its three states

The page uses the engine's own test fixture, `tests/fixtures/outcomes`, so run it from a
checkout of the repository. It is a table of subscriptions with a `plan`, an `offered` flag and
a `renewed` flag, and a bundle of two personas (basic and premium), a renewal journey, a
retention offer and a control, and a Playback. Three profiles of the table, `state-1` to
`state-3`, are three releases of the same request. They differ in the grouped query cells and
in nothing else: how many subscriptions of each plan were offered, and how many of those
renewed. The fixture script draws the cells from a stated truth with a seeded generator, and in
state 3 the premium plan renews far less than before.

```python
import shutil
from pathlib import Path

import flux_engine

fixture = Path(flux_engine.__file__).resolve().parents[1] / "tests" / "fixtures" / "outcomes"
shutil.copytree(fixture / "bundle", "bundle")
shutil.copytree(fixture / "states", "states")
```

```console
$ ls states
state-1.profile.json
state-2.profile.json
state-3.profile.json
```

## 2. Declare a response family and forecasts

A response family states what an intervention does, segment by segment, and reads it from the
profile: a segment's rate among the subscriptions not offered, and what the offer adds to it.
Both come from grouped consumer queries, so the family names a treated query and a control
query, and a `Segment` for each group of their `groupBy` column. The forecasts name the family,
an arm, a segment, and how the next state's arms will be measured.

The Simulation holds both declarations, in the namespace's `responses` and `forecasts` keys.
These are the ones in the fixture:

```console
$ sed -n '/^    responses:/,$p' bundle/simulation.flux.yml
    responses:
    - id: offer-by-plan
      treatment: tr-retention-offer
      experiment: exp-offer-test
      journey: jr-renewal
      segments:
        basic: seg-basic
        premium: seg-premium
      estimate:
        treated: renewed-offered
        control: renewed-not-offered
        confidence: '0.9'
    forecasts:
      playback: pb-renewal
      confidence: '0.9'
      items:
      - id: premium-offered
        response: offer-by-plan
        arm: treated
        segment: premium
        basis: historical-holdout
      - id: basic-control
        response: offer-by-plan
        arm: control
        segment: basic
```

- The family estimates, for each segment, the control rate `p0` and the effect of the offer
  with an interval at `confidence`, by an exact two-proportion readout of the two queries'
  cells. In the journey, a subject in a segment succeeds with `p0` when it is untreated and
  with `p0` plus the effect when its experiment variant names the treatment. A cell that is
  withheld gives its segment no estimate, and the segment keeps the journey's own probability.
- An item forecasts the rate the next profile will show in the cell of its family's `treated`
  or `control` query, for its segment. `premium-offered` forecasts the share of offered premium
  subscriptions that renew. `basic-control` forecasts the share of basic subscriptions not
  offered that renew.
- `basis` is `historical-holdout` unless you state that the next state's arms come from a live
  randomised comparison, in which case it is `live-ab`. Leaving it out takes the cautious
  reading.
- `forecasts.playback` names the Playback whose `drift` rule is used when the forecasts are
  scored. A Playback's drift is read only when a forecast names it, so a bundle that declares
  drift and no forecast behaves as it did.
- Both keys need the Simulation's `spec.sweep`, here `runs: 20`. The sweep gives the
  replicates, and 20 is the fewest that give a 0.9 interval. The fewest runs at confidence `c` is
  `2 / (1 - c)` rounded up, and a confidence that needs more than 100,000 runs (above 0.99998) is
  refused with `FLX-3674`.

The keys are read by kernel 0.2.0 only. Kernel 0.1.0 refuses them with `FLX-3105`, naming the
kernel.

## 3. Pin the bundle to state 1 and make the forecasts

A bundle pins its profile by digest, so the state a forecast rests on is named exactly.
`fluxsim init --profile` writes the pin when it authors a bundle from a profile. Here the
bundle exists already, so this small script writes the digest of a state into a copy of the
Simulation:

```console
$ cat > pin.py <<'EOF'
import sys
from pathlib import Path

import yaml

from flux_engine.seam.profile import load_profile

bundle, profile = Path(sys.argv[1]), Path(sys.argv[2])
path = bundle / "simulation.flux.yml"
document = yaml.safe_load(path.read_text(encoding="utf-8"))
digest = load_profile(profile).digest.value
document["extensions"]["com.agentics.flux/1"]["profile"]["digest"] = digest
path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
print(f"{bundle} pins {profile.name} ({digest[:19]}...)")
EOF
$ python pin.py bundle states/state-1.profile.json
bundle pins state-1.profile.json (sha256:384427f5a895...)
```

Now make the forecasts. The engine reads no clock, so you give the instant they are registered
at, and the instant after which the next state can be read:

```console
$ fluxsim forecast make bundle --profile states/state-1.profile.json --registered-at 2026-07-01T00:00:00Z --horizon-end 2026-09-30T23:59:59Z
registered premium-offered.384427f5a895aaf5 (sha256:...): 0.8078947368421052631578947368 [0.6136363636363636363636363636, 0.9583333333333333333333333333]
registered basic-control.384427f5a895aaf5 (sha256:...): 0.4880952380952380952380952381 [0.3125, 0.6341463414634146341463414634]
```

`make` ran the sweep as 20 replicates. Replicate `r` takes the effect at the midpoint of the
`r`-th of 20 equal parts of the effect's interval, so the spread of the replicates holds both
the draws and what state 1 leaves unknown about the effect. The forecast is the median of the
20 values, and its interval runs from the second smallest to the second largest. Each forecast
is registered in the outcome-accuracy store, sealed and write-once: its id is the item's id and
the first 16 hexadecimal digits of the pinned profile's digest, so an item has one prediction
per state.

```console
$ ls .flux/accuracy/predictions
basic-control.384427f5a895aaf5.json
premium-offered.384427f5a895aaf5.json
```

Making the same forecasts again changes nothing, and a different forecast under a used id is
refused ([FLX-7007](../reference/errors/FLX-7007.md)), so a forecast cannot be replaced once
the next state is seen. An id is the item's id and the pinned state, so two bundles that
pin one profile and name an item alike collide, and the refusal then says the registered
forecast belongs to another bundle and gives the first 16 hex digits of both bundles'
`inputDigest`. Give the item its own id in one of the two bundles:

```console
$ fluxsim forecast make bundle --profile states/state-1.profile.json --registered-at 2026-07-01T00:00:00Z --horizon-end 2026-09-30T23:59:59Z
already registered premium-offered.384427f5a895aaf5 (sha256:...): 0.8078947368421052631578947368 [0.6136363636363636363636363636, 0.9583333333333333333333333333]
already registered basic-control.384427f5a895aaf5 (sha256:...): 0.4880952380952380952380952381 [0.3125, 0.6341463414634146341463414634]
```

Publish each seal where others can see it before the next state is released, as the page on
[sealing a prediction](./seal-and-score-a-prediction.md) describes. The order of forecast and
outcome rests on the time you give and on that publication.

## 4. Score the forecasts against state 2

Once state 2 exists, `score` reads it and gives every forecast registered for the bundle a
verdict. The observation is state 2's cell for the forecast's query and segment, and a score
may not observe anything before the forecast was registered:

```console
$ fluxsim forecast score bundle --profile states/state-1.profile.json --next states/state-2.profile.json --observed-at 2026-06-01T00:00:00Z   # exit 1
FLX-7001: observed readout predates its registered prediction -- ...
more: fluxsim explain FLX-7001  ...
$ fluxsim forecast score bundle --profile states/state-1.profile.json --next states/state-2.profile.json --observed-at 2026-10-01T00:00:00Z
basic-control.384427f5a895aaf5: within-interval, observed 0.5066666666666666666666666667 of 150, absolute error 0.0185714285714285714285714286, interval score 0.3216463414634146341463414634
premium-offered.384427f5a895aaf5: within-interval, observed 0.7833333333333333333333333333 of 180, absolute error 0.0245614035087719298245614035, interval score 0.3446969696969696969696969697
drift (psi, threshold 0.2): largest 0.006082520804479017800123370257 at query:renewed-offered/basic; the calibration is not stale
```

Every forecast takes a verdict and none is skipped. Beside inside or outside the interval, each
verdict holds two scores in exact decimals:

- the absolute error between the observation and the forecast;
- the interval score of Gneiting and Raftery (2007) at the level `alpha` of one minus the
  confidence. It is the width of the interval, plus `2 / alpha` times the distance below the
  interval when the observation falls under it, or above the interval when it falls over it.
  Lower is better, and a narrow interval pays for every miss.

A withheld cell in state 2, or one with no records, leaves its forecast not scored, with that
reason in the verdict. `--profile` must be the pinned state and `--next` another release;
scoring against the pinned state is refused ([FLX-7143](../reference/errors/FLX-7143.md)).

Each verdict also states the grade of the two profiles. A profile that publishes its cells
exactly is graded P1, and a declaration, a family or a forecast never enters a grade.

## 5. What drift does

When it scores, the engine compares the two states over every statistic both hold with the
same definition, levels and grouping: each histogram of a column and each query cell the
families read. A cell with no records in either state, like a withheld cell, is not compared;
the score lists it as left out, with the reason. It uses the metric the Playback that `forecasts.playback` names declares: `psi`
here, with a threshold of 0.2. A statistic past the threshold has drifted, and one drifted
statistic marks the calibration made on the earlier state stale. The score is recorded and
published either way.

The first pass found the two states alike: the largest value, 0.006, is far below 0.2. Walk
forward to the next pass. Pin a copy of the bundle to state 2, make the forecasts at the
instant state 2 was read, and score them against state 3, where the premium plan changed:

```console
$ cp -R bundle second
$ python pin.py second states/state-2.profile.json
second pins state-2.profile.json (sha256:3bbf9efdfddb...)
$ fluxsim forecast make second --profile states/state-2.profile.json --registered-at 2026-10-01T00:00:00Z --horizon-end 2026-12-31T23:59:59Z
registered premium-offered.3bbf9efdfddbc16a (sha256:...): 0.8225288831835686777920410783 [0.6363636363636363636363636364, 1]
registered basic-control.3bbf9efdfddbc16a (sha256:...): 0.5 [0.3125, 0.6341463414634146341463414634]
$ fluxsim forecast score second --profile states/state-2.profile.json --next states/state-3.profile.json --observed-at 2027-01-01T00:00:00Z
basic-control.3bbf9efdfddbc16a: within-interval, observed 0.5666666666666666666666666667 of 150, absolute error 0.0666666666666666666666666667, interval score 0.3216463414634146341463414634
premium-offered.3bbf9efdfddbc16a: outside-interval, observed 0.4555555555555555555555555556 of 180, absolute error 0.3669733276280131222364855227, interval score 3.979797979797979797979797980
drift (psi, threshold 0.2): largest 0.4737137055293028596933621901 at query:renewed-offered/premium; the calibration is stale
```

The premium forecast missed, by far, and the drift check says why: the offered premium cell
moved well past the threshold, so the calibration made on state 2 no longer describes the data.
The basic forecast was inside its interval, and its cell did not drift. The same drift report
is recorded in every verdict of the pass.

With the Playback's `onBreach: warn`, the default, `score` reports the stale calibration and
exits 0. With `onBreach: fail` it writes every verdict first and then exits 1
([FLX-7146](../reference/errors/FLX-7146.md)), so a pipeline can stop on a stale calibration
without losing a score.

## 6. See every forecast in the evidence pack

`fluxsim ship` reads the outcome-accuracy store and adds a "Forecast Accuracy" section to
`EVIDENCE.md`, after "Fidelity", when the store holds a forecast registered for the run's input
digest. Ship the second pass:

```console
$ fluxsim run second --profile states/state-2.profile.json --ship -o out
...
run ... complete; shipped to out
$ RUN=$(grep -l '## Forecast Accuracy' .flux/runs/*/EVIDENCE.md | xargs dirname | xargs basename)
```

Each forecast is a row with its metric, the digest of the state it was made on, its
registration time, the forecast and interval, the observation, inside or outside, the interval
score, the absolute error and the drift. A miss is listed as a hit is, and a forecast with no
verdict yet is listed as pending.

```console
$ grep -E '^\| `(basic|premium)-' .flux/runs/$RUN/EVIDENCE.md | awk -F' [|] ' '{print $1 " | " $7 " | " $8 " | " $9}'
| `basic-control.3bbf9efdfddbc16a` | inside | 0.3216463414634146341463414634 | 0.0666666666666666666666666667
| `premium-offered.3bbf9efdfddbc16a` | outside | 3.979797979797979797979797980 | 0.3669733276280131222364855227
```

Then the totals. The measured interval coverage, the share of scored forecasts inside their
interval, stands beside the coverage the Playback declares in its scorecard. The engine never
rewrites the declared figure:

```console
$ grep -E '^- (Registered|Inside|Measured|Declared)' .flux/runs/$RUN/EVIDENCE.md
- Registered for this run: 2. Scored: 2. Not scored: 0. Pending: 0.
- Inside their interval: 1 of 2.
- Measured interval coverage: 0.5 (1 of 2 scored forecasts).
- Declared interval coverage: 0.92, from the Playback `pb-renewal`. The engine never rewrites the declared figure.
```

The section also counts how many forecasts the store holds on each state across all bundles, so
a forecast made from another bundle on the same state is never out of sight, and it states the
grade of the pinned state and of each next state. The privacy section states the weakest grade of
every profile the run read (a ship that reads a held-out profile states it as well, with or without
forecasts, and holds the column minimums to the weakest of them all):

```console
$ grep -E '^(Weakest grade|- (Pinned|Next) state)' .flux/runs/$RUN/EVIDENCE.md | sed -E 's/sha256:[0-9a-f]+/sha256:.../' | cut -c1-62
- Pinned state `sha256:...`: **P1 (checked)**
- Next state `sha256:...`: **P1 (checked)**
Weakest grade of every profile this run read: **P1 (checked)**
```

`forecast-accuracy.json` in the run directory holds the same rows, and it is packed with the
run. A run with no forecast registered for its input digest keeps its `EVIDENCE.md` and its
pack as they were.

```console
$ grep -c '"predictionId"' .flux/runs/$RUN/forecast-accuracy.json
2
```

## 7. Walk forward over a series of states

For states 1 to `n`, the bundle pinned to state `k` forecasts state `k + 1`, and state `k + 1`
scores it, for every `k` up to the one before the last state. This is rolling-origin evaluation with a window of one
state. This page ran it for `n` of 3, and the loop for any `n` is the commands above:

```text
for k in 1 up to the one before the last state:
    pin a bundle to state k
    fluxsim forecast make   --profile state-k --registered-at T_k --horizon-end H_k
    fluxsim forecast score  --profile state-k --next state-(k+1) --observed-at T_(k+1)
    fluxsim ship            (the pack lists the forecasts of the run's own bundle)
```

Each state's bundle is the one `fluxsim init` writes from that state's profile, with the
declarations above. Three things hold the series together:

- `T_(k+1)` is later than `T_k`, and the registration is sealed before the next state is
  released, which is what makes each score a test the forecast could fail;
- each pass has its own bundle and so its own input digest, so each pass ships its own
  section; the count of forecasts on a state spans every bundle;
- a forecast is as good as its scenario. The bundle for the next period states what that
  period holds, such as the experiment's weights and the population's mix, and the forecast
  assumes it.

A Wald interval, which the effect's interval is, covers less than it says on small counts. The
scores in the pack show that rather than hide it.
