# ADR 0029: response families forecast the next committed state, and the outcome-accuracy record scores every forecast

## Status

Accepted 2026-11-22: the maintainers accepted the design, and the changes its review asked for are folded
in. Builds on
[ADR 0028](./0028-consumer-queries-run-locked-compile-requirements-and-calibrate.md) (consumer queries and
their grouped rates), [ADR 0020](./0020-privacy-grades.md) (grades) and the outcome-accuracy record
(`docs/how-to/seal-and-score-a-prediction.md`).

## Context

A twin is worth its forecasts. Calibrated on what the data showed in one period, it should say what the
next period will show when an intervention runs, say it before that period is seen, and be scored
against it whichever way the score falls. The engine holds most of the parts and joins none of them:

- **Treatments have no effect.** A `Treatment` carries an `actionType` and a payload template. An
  `Experiment` assigns subjects to variants by the largest remainder, from its own stream, and each
  variant names a treatment. A journey's reactions are drawn the same way whatever a subject's variant,
  so two arms differ only in name. `flux_engine.runtime.experiment` holds an exact two-proportion readout
  (`TwoProportionReadout`, a Wald interval in exact `Decimal`) that nothing calls.
- **A Playback declares what nothing computes.** Its `calibration` names the dials a source fits, its
  `drift` names a metric (`psi`, `kl` or `ks`), a threshold and `onBreach`, and its `scorecard` states an
  `intervalCoverage`: the share of observed outcomes inside the twin's predicted intervals over a backtest
  window. The engine reads the scorecard only, and copies it.
- **The outcome-accuracy record scores what a person registers.** `fluxsim accuracy register` seals a
  prediction write-once, with a digest over the prediction and its registration time. `score` binds an
  observed readout that may not predate the registration (`FLX-7001`), and the verdict is whether the
  observation falls inside the interval. The published record (`accuracy-record/1`) counts every
  prediction registered in its window, misses included, and carries no value. The engine registers no
  prediction of its own.
- **A committed state is a profile release.** A bundle pins its profile by digest, so the state a run
  was calibrated on is named exactly. ADR 0028 lets a profile publish a consumer query's rate per group.

**Borrow-before-build.** Searched and fetched on 2026-11-22; the table at the end of this record gives
each result and its use.

- In the engine at `1c317c0`: the runtime's `ExperimentStatistics` protocol and its two-proportion
  readout; `journey.py`'s reaction draw, a Bernoulli trial per step or a dial's allocation; the
  seed tree's `sweep` path, which seeds `sweep.runs` independently replayable runs from
  `baseSeed + run_index`; the accuracy store and its policy; `ClaimBasis`; the Playback schema in the
  FLUX spec 0.5.4.
- Searches: interval scores as proper scoring rules; how forecast hubs score forecasts made before their
  targets; walk-forward, or rolling-origin, evaluation; the population stability index between two
  profiles.
- Fetched: the PyPI records of gbstats, whylogs, scoringrules, sbi and pyabc, and the GitHub records of
  their repositories; the source of scoringrules' interval score; scikit-learn's `TimeSeriesSplit`
  documentation.

## Decision

### 1. A state is a profile release

State `k` is the profile the bundle pins, named by its digest. State `k+1` is a later release answering
the same request, given when a forecast is scored. A response family and a forecast read the pinned
profile and nothing else when they are made.

### 2. Response families under `responses`

A Simulation declares an intervention's effect by segment under
`extensions['com.agentics.flux/1'].responses`, a key no engine before this one reads:

```yaml
responses:
  - id: offer-by-plan
    treatment: retention-offer
    experiment: offer-test
    journey: renewal
    segments: { basic: seg-basic, premium: seg-premium }
    estimate:
      treated: renewed-offered
      control: renewed-not-offered
      confidence: "0.9"
```

- **What it names.** `treatment` is a `Treatment`; `experiment` an `Experiment` with a variant that
  names that treatment; `journey` a `Journey`. `segments` maps each group key of the estimate's queries
  (a level of their `groupBy` column) to a `Segment`. `estimate.treated` and `estimate.control` are
  calibration queries of the registry (ADR 0028) with the same `groupBy`, whose cells in the pinned
  profile are the treated and the untreated outcomes per segment. A held-out query is refused here.
- **Estimated from aggregates.** For each segment, the control cell gives `n_c` records and `x_c`
  matches, the treated cell `n_t` and `x_t`. The runtime's readout (`ExperimentStatistics`, exact
  two-proportion by default) gives the control rate `p0 = x_c / n_c`, the effect `d = x_t / n_t - p0` and
  its interval `[L, U]` at `confidence`. A withheld cell, or an arm whose record count `n` is zero, gives
  that segment no estimate: it is reported, and its subjects keep their journey's own probability. A
  match count `x` of zero is an estimate, a rate of zero. The parameter register lists each estimate with
  the cells it was read from.
- **Applied in the runtime.** In the family's journey, a subject whose persona a segment's `Segment`
  selects (the first in key order when several do) succeeds with probability `p0` when it is untreated,
  and `min(1, max(0, p0 + d))` when its variant in the family's experiment names the family's treatment.
  A subject in no segment keeps the probability its persona's reaction declares. When a bundle declares a
  family, the experiment's assignment is made before the journeys run. It reads only the cast and its own
  stream, so it is the assignment it was.
- **Draws.** A reaction draws on its own journey path at the index it always draws at; the family only
  changes the probability its uniform is compared with, which KERNEL-SPEC-2 section 12 counts as adding
  no path. A bundle without the key has the bytes it had. Families are kernel 0.2.0 only: kernel 0.1.0
  refuses the key with `FLX-3105`, naming the kernel. A family on a journey whose reactions the World's
  temperature dials is refused in this record: the dial's frozen share is allocated over a group of
  subjects, and splitting that group by segment and arm would move subjects the family does not touch.
- A subject's treatment comes from its experiment variant. A Campaign's deliveries are not read by this
  record.

### 3. Forecasts under `forecasts`

```yaml
forecasts:
  playback: pb-renewal
  confidence: "0.9"
  items:
    - id: premium-offered
      response: offer-by-plan
      arm: treated
      segment: premium
      basis: historical-holdout
```

- **What an item forecasts.** The rate the next state's profile will show in the cell of its family's
  `treated` or `control` query, as `arm` says, for its segment.
- **The twin's value in one run.** Among the subjects of the segment in the arm (treated: their variant
  names the family's treatment; control: any other variant of the experiment), the share whose first
  reaction in the family's journey succeeded, as an exact fraction. A run with no such subject gives no
  value.
- **Replicates are the sweep.** A forecast runs the Simulation's `spec.sweep`: `R` runs, each seeded as
  the sweep already seeds it and each replayable on its own. A Simulation without a sweep is refused.
  Replicate `r`, counting from 1, takes the effect at the midpoint of the `r`-th of `R` equal parts of
  the effect's interval, `L + (U - L)(2r - 1) / (2R)`, so the spread of the replicates holds both their
  draws and what state `k` leaves unknown about the effect. No seed-tree path is added. Each replicate's
  plan records `r`, `R` and the effect it took, so `fluxsim replay` reproduces the replicate's run digest,
  and the forecast's run digest can be checked replicate by replicate. Kernel 0.1.0 refuses `forecasts`
  with `FLX-3105`, naming the kernel.
- **The forecast and its interval.** The forecast is the median of the `R` values, the mean of the two
  middle ones when `R` is even. Its interval runs from the value of rank `a` to the value of rank
  `R + 1 - a`, with `a` the largest whole number not above `R (1 - c) / 2` for the confidence `c`. A
  sweep too short to give `a` of at least 1, fewer than 20 runs at 0.9, is refused. The arithmetic is
  exact.
- **Basis.** `basis` is a `ClaimBasis` of the record: `historical-holdout` when absent, the cautious
  reading, or `live-ab` when the author states that the next state's arms come from a live randomised
  comparison.

### 4. The prediction registry

`fluxsim forecast make BUNDLE --registered-at T` makes every item's forecast and registers it in the
outcome-accuracy store, sealed and write-once, exactly as `fluxsim accuracy register` registers a
prediction:

- its id is the item's id, a dot, and the first 16 hexadecimal digits of the pinned profile's digest, so
  an item has one prediction per state;
- two bundles that read one profile therefore share the ids of an item they name alike. The second is
  refused with `FLX-7007` although its `inputDigest` differs, and the message says the registered record
  belongs to another bundle, names the first 16 hexadecimal digits of both `inputDigest` values, and tells
  the person to give the item its own id in one of the bundles. Scoring and `ship` already tell the two
  bundles apart by `inputDigest`, so the id is not changed;
- its run digest is the digest of the replicates' run digests, in sweep order; its metric is
  `query:<query id>/<segment>`; it holds the forecast, the interval and the next state as its horizon;
- it holds two more sealed members: `stateDigest`, the pinned profile's digest, and `inputDigest`, the
  bundle's.

`T` comes from the caller, since the engine reads no clock. Registering the same forecast again changes
nothing, and a different forecast under a used id is refused (`FLX-7007`), so a state's forecast cannot
be replaced once the next state is seen. The order of forecast and outcome rests on the time the caller
gives and on the sealed digest being published before the next state is. `score` refuses an observation
that predates the registration (`FLX-7001`), and refuses a next state whose digest is the forecast's own
state.

### 5. Every forecast is scored against the next state

`fluxsim forecast score BUNDLE --profile NEXT --observed-at T` reads the next state's profile and scores
every forecast registered for the bundle's `inputDigest`:

- **The observation** is the next state's cell for the forecast's query and segment: its `true` count over
  its `k`, with `k` as the sample size. A withheld cell, or a `k` of zero, leaves the forecast not scored,
  with that reason in its verdict. Every registered forecast takes a verdict, and none is skipped.
- **The scores.** Beside the record's containment verdict, two scores in exact `Decimal`: the absolute
  error between observation and forecast, and the interval score of Gneiting and Raftery (2007) at the
  level `alpha = 1 - c`. For the interval `[l, u]` and the observation `x`, it is `u - l`, plus
  `(2 / alpha)(l - x)` when `x < l`, plus `(2 / alpha)(x - u)` when `x > u`. It is a proper scoring rule
  for the interval's two quantiles: lower is better, and an honest interval has the lowest expected score.
- **What is published.** `accuracy-record/1` is unchanged and still counts every prediction of its
  window, misses included. The values and scores reach the evidence pack (section 8).
- **Grades.** The next state's profile is graded under ADR 0020's rule 2, and each verdict states its grade
  beside the pinned profile's.

### 6. Drift between the two states flags a stale calibration

When it scores, the engine compares state `k` and state `k+1` over the statistics both hold with the
same definition, levels or bins, and grouping: each `levelHistogram/1` and `marginalHistogram/1` of a
column, and each query cell the families read. A statistic with a withheld cell in either state is left
out and listed. So is a statistic with no records in either state: smoothing would give it a uniform
share that no record supports, and a cell that lost all its records would read as a change of the
calibration. It is listed as not compared, with that reason, and the share it lost or gained still shows
in the statistic of the column that groups it.

- **The metric** is the one the Playback that `forecasts.playback` names declares: `psi`, the sum of
  `(q - p) ln(q / p)` over the cells; `kl`, the sum of `q ln(q / p)`; or `ks`, the largest gap between
  the two cumulative shares in the order the profile lists the cells. `p` is state `k`'s share of a cell
  and `q` state `k+1`'s. Each share is `(count + 1/2) / (total + n/2)` over the statistic's `n` cells, so
  no share is zero, and `ln` is taken in `Decimal` at the kernel's precision.
- **The flag.** A statistic whose value passes the Playback's `threshold` has drifted. One drifted
  statistic marks the calibration on state `k` stale. Each verdict of the pass records the metric, the
  threshold, the largest value and its statistic, and whether the calibration is stale. The score is
  recorded and published either way. With `onBreach: fail` the command exits with status 1 after it has
  written every verdict; with `warn`, the default, it reports and exits 0.
- **Opt in.** The engine reads a Playback's `drift` only when `forecasts.playback` names it, so a bundle
  whose Playback declares drift and no forecast reads as before. Without a named Playback the pass reports
  no drift.

### 7. Walk-forward over a series of states

For states `1` to `n`, the bundle pinned to state `k` forecasts state `k+1`, and state `k+1` scores it,
for `k` from 1 to `n - 1`. This is rolling-origin evaluation with a window of one state; scikit-learn's
`TimeSeriesSplit` is the expanding form of the same split. Each state's bundle is written by `fluxsim init`
from that state's profile. Over the record's window, the share of scored forecasts inside their interval
is the measured interval coverage. The evidence reports it beside the Playback's declared
`scorecard.intervalCoverage`, and the engine never rewrites the Playback.

### 8. The evidence pack reports every scored forecast

- `fluxsim ship` reads the outcome-accuracy store under its state directory and adds the section
  "Forecast Accuracy", after "Fidelity", when the store holds at least one forecast registered for the
  run's `inputDigest`, scored or not.
- The section lists every such forecast, ordered by id: its metric, the digest of the state it was made
  on, its registration time, the forecast and interval, the observation, inside or outside, the interval
  score, the absolute error and the drift. Then the totals: forecasts scored, inside their interval, the
  measured coverage and the declared one. A miss is listed as a hit is, and nothing filters either.
- A registered forecast with no verdict yet is listed too, as pending.
- For each state digest it lists, the section reports how many forecasts the store holds on that state
  across all bundles, so a forecast made from another bundle on the same state is never out of sight.
- It states the grade of each profile the forecasts read, the pinned state's and each next state's, and
  `ship` states the weakest.
- `forecast-accuracy.json` in the run directory holds the same rows and is packed with the run.
- A run with no forecast registered for its `inputDigest` keeps its `EVIDENCE.md` and its pack as they
  were.

### 9. Grades

ADR 0020 grades a run from the profile it pins (its rule 2), and a declaration, a family or a forecast
never enters a grade. A pinned profile that publishes its query cells exactly is P1 by that rule, so a run
whose families read such cells is P1. The next state's profile is graded under the same rule when it
scores, the section states both grades, and `ship` states the weakest grade of every profile the run read.
The observations the section prints are cells the next state's profile released.

### 10. Formats

- **The Simulation's namespace:** the keys `responses` and `forecasts`.
- **Profile 1-3-0:** the grouped query cell of ADR 0028, section 12, which the families read.
- **Parameter register 1-3-0:** entries for each response estimate and the cells it was read from, in the
  format ADR 0028 and ADR 0030 share: whichever change lands first defines 1-3-0, and the others extend it.
- **The outcome-accuracy store:** a sealed prediction may hold `stateDigest` and `inputDigest`, and a
  verdict may hold `absoluteError`, `intervalScore` and `drift`. Records and verdicts written before read
  as they did. `accuracy-record/1` does not change.
- **`EVIDENCE.md`:** the optional section of section 8.

## Consequences

- A bundle that declares neither key has the bytes and outputs it had, and the scenario runtime draws as
  it did.
- The engine's own fixtures run the whole loop in the end-to-end tests: a scenario bundle and three
  profile states, where the bundle pinned to each state forecasts the next, the next state scores it, and
  `EVIDENCE.md` shows the scores.
- A forecast is as good as its scenario: the bundle for the next period states what that period holds,
  such as the experiment's weights and the population's mix, and the forecast assumes it.
- A Wald interval covers less than it says on small counts. The published scores show that, and a
  score-type interval can arrive later as another readout behind the same protocol.
- The new code lives in new modules. The core changes in the journey's reaction probability, the order
  of the experiment's assignment when a family is declared, and the evidence report's sections.

## Alternatives considered

- **A forecast computed from the estimate alone,** `p0 + d`, without running the twin. Rejected: the
  forecast is the twin's, and what the twin adds is its scenario: who is treated, the consent gates, the
  population's mix.
- **Drawing each replicate's effect at random.** Rejected: it needs a posterior and a seed-tree path. The
  midpoint grid is deterministic and covers the interval evenly.
- **A new seed-tree path for replicates.** Rejected: the sweep already defines independently replayable
  runs.
- **Values and scores in `accuracy-record/1`.** Rejected: that record is built to carry no value, so it
  can be published widely. The evidence pack carries them.
- **gbstats for the effect.** Rejected as a dependency: gbstats 0.8.0 requires numpy below 2 and pandas
  below 2, so it cannot be installed beside the engine, and its last release is from 2024-06-20. Its
  inputs, counts per arm, are the pattern the readout protocol already follows.
- **whylogs for drift.** Rejected as a dependency: no release since 1.6.4 (2024-12-03) and no push since
  2025-01-10. Its profile-to-profile comparison is the pattern, applied on the profile's own pinned bins.
- **sbi or pyABC to fit the families.** Not needed: an effect by segment from counts has a closed form,
  and both are heavy dependencies (ADR 0028).
- **Reading a Playback's `calibration` and `drift` wherever they are declared.** Rejected: a bundle
  written before would change behaviour. They are read only when `forecasts` names the Playback.

## What is borrowed

| Source | Version and licence | Checked on 2026-11-22 | Use |
|---|---|---|---|
| gbstats | 0.8.0, MIT; released 2024-06-20; requires numpy below 2 and pandas below 2 | PyPI | not a dependency; the inputs pattern |
| whylogs | 1.6.4, Apache-2.0; released 2024-12-03; last push 2025-01-10 | PyPI, GitHub (whylabs/whylogs) | not a dependency; the profile-to-profile pattern |
| scoringrules | 0.12.0, Apache-2.0; released 2026-11-08; Python 3.12 or later | PyPI, GitHub (frazane/scoringrules), its interval score | the reference for section 5; the formula is written in `Decimal` |
| Gneiting and Raftery (2007), "Strictly proper scoring rules, prediction, and estimation", Journal of the American Statistical Association 102, 359 to 378 | paper | | the interval score |
| Bracher and others (2021), "Evaluating epidemic forecasts in an interval format", PLOS Computational Biology (arXiv 2005.12881) | paper | | forecasts made before their targets and every score published, as forecast hubs do |
| scikit-learn | 1.9.1, BSD-3-Clause | `TimeSeriesSplit` documentation | the rolling-origin split of section 7 |
| The population stability index | a convention of credit scoring | search results from NannyML and Fiddler | the `psi` metric and its bins pinned to the reference |
| sbi, pyABC | 0.27.0, Apache-2.0; 0.13.1, BSD-3-Clause | PyPI | not needed (alternatives) |
