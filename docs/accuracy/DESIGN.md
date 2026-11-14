# The accuracy program: design

This note says what the accuracy program measures, how, and what has to hold
before a result counts as a pass. The page that reports the latest results is
[REPORT.md](REPORT.md). That page is generated from the nightly artefact and
never edited by hand.

The engine already pins its own behaviour byte for byte: goldens, replay
digests and conformance vectors all say that the engine does what it did
yesterday. None of them says that what it does is right. A sampler that draws
from the wrong distribution, or a fidelity check that lets a bad dataset
through, would replay perfectly. The accuracy program measures the second
kind of claim: that the samplers draw from the distributions they declare,
that the fidelity check accepts and withholds when it should, and that the
seed tree's streams behave like independent random streams.

Results are measured, not established. A goodness-of-fit test that does not
reject is evidence that a distribution fits at the stated sample size and
power, and it is reported as exactly that.

## Tiers

Every family runs at two tiers, from the same test code. `FLUX_ACCURACY_TIER`
selects the tier.

| | Pull-request tier (`pr`, the default) | Nightly tier (`nightly`) |
|---|---|---|
| Seeds | Fixed. The base seed is `20261111`. | Fresh. Each night gets one logged base seed (`FLUX_ACCURACY_SEED`). |
| Sizes | Small enough to finish in minutes | The sizes the power claims are made at |
| Hypothesis | Derandomised | Seeded from the base seed |
| Purpose | A deterministic regression gate | A fresh sample, every night |
| Where | `.github/workflows/accuracy-nightly.yml`, on every pull request and push to `main` | The same workflow: daily at 02:41 UTC, on dispatch, or when a pull request is labelled `accuracy-nightly` (once, on the label event; re-add the label to run it again). On Mondays, or with `accuracy-weekly`, it adds the macOS cells and mutation testing |

Every seed a test uses comes from the tier's base seed and a label that names
what the seed is for (`tests/accuracy/harness/tier.py`). A nightly failure
therefore reproduces from the artefact alone:

```bash
FLUX_ACCURACY_TIER=nightly FLUX_ACCURACY_SEED=<baseSeed> pytest tests/accuracy
```

A failure at the pull-request tier is a change in the code. Its seeds never
move, so a red result is not bad luck. A changed sampler stream is,
statistically, a fresh seed, so roughly one innocent change in a hundred can
turn a battery red at `q = 0.01`. When that happens, the nightly tier's fresh
seeds settle which it was.

## Running it

The program needs the test-only `accuracy` dependency group (scipy,
hypothesis-jsonschema, pytest-benchmark). It is declared in `pyproject.toml`
and is never a runtime dependency.

```bash
uv sync --extra dev --group accuracy
.venv/bin/pytest tests/accuracy -n auto --dist loadscope
```

Without the group, the program's modules skip and give a reason that names
the group. `FLUX_ACCURACY_REQUIRED=1` turns that skip into a collection
failure; the accuracy workflow sets it. `FLUX_ACCURACY_RESULTS_DIR=<dir>`
writes one JSON line per measured case, which the nightly job joins into its
artefact.

## Controlling false alarms

A family runs many tests at once. If each one were read alone at a fixed
level, a battery of fifty correct samplers would raise a false alarm on most
nights. So no family reads a lone p-value against a threshold. Each family
collects its p-values in a battery and adjusts them together with
`scipy.stats.false_discovery_control` at `q = 0.01`:

- **Benjamini-Hochberg (BH)** when every test ran on its own sample, so the
  p-values are independent;
- **Benjamini-Yekutieli (BY)** when two tests share a sample, for example KS,
  Cramer-von Mises and Anderson-Darling on one set of draws. BY holds under
  any dependence.

When every sampler is correct, the false-discovery rate equals the
family-wise error rate. So `q = 0.01` bounds the chance that a correct engine
turns one battery red on one night at 1%. A family with several batteries
has a budget that is their sum: the journeys family, for example, runs five.

Across the whole program there are about sixteen batteries, so a correct
engine would fail at least one of them on about one night in six. A night
that teaches its readers to ignore it is worse than no night, so the nightly
job does not stop at the first pass:

- **Confirmation.** Every test that fails the first pass is run again once,
  at a second fresh seed (the base seed plus one, logged as the confirmation
  seed in the artefact). Only a failure the confirmation repeats turns the
  night red. A failure the confirmation clears is reported as cleared, with
  both seeds, and does not count as a failure.
- **What that costs.** For a correct engine, a red night now needs the same
  battery to raise a false alarm twice in a row: about 16 x 1% x 1%, roughly
  0.2% of nights. For a real defect that a single pass catches with power
  p, a night catches it with probability about p squared: 81% at the 90%
  power the checks are sized for. A defect missed one night is caught on the
  next, so it cannot hide for long.
- **Non-statistical failures** (an import error, a crashed subprocess, a
  wrong exact count) repeat at any seed, so the confirmation never clears
  them.

At the pull-request tier the seeds are fixed, so there is nothing to
confirm: the first pass is the verdict. The chance that an innocent change
in a stream trips a battery there is the same one in six. When it happens,
the nightly tier's fresh seeds and its confirmation pass settle which it
was.

A battery that never rejects says nothing about its own sharpness, so every
statistical family also carries a power check. A defect of stated size is
injected, and the check measures how often the battery catches it.

## The families

### 1. Aggregate-native fidelity on the profile S

The fidelity check compares a synthetic dataset with a committed state through
the derived statistic S. S holds bucket counts on the declared edges, the
moment sums (`Sx`, `Sy`, `Sxy`, `Sxx`, `Syy`), the sorted support, the
quantile witness, `n` and `k`
(`flux_engine/concordance/boundary/statistic.py`). Family 1 scores the same
comparison with the metrics a synthetic-data practitioner already reads, and
computes them from S alone. It never reads a record.

The definitions are borrowed from SDMetrics 0.33.0 (MIT): KSComplement,
TVComplement, CorrelationSimilarity and ContingencySimilarity. SDMetrics
computes them from DataFrames. Here they are reimplemented on aggregates
(`tests/accuracy/fidelity/s_metrics.py`):

| Metric | From S | Relation to the SDMetrics definition |
|---|---|---|
| TVComplement | `1 - 1/2 * sum(abs(c_a/k_a - c_b/k_b))` over the bins of a histogram property | Equal, with the bins as categories |
| KSComplement | `1 - max(abs(F_a(e) - F_b(e)))` over the declared edges, from cumulative bin counts | A lower bound on the record-level KS distance: the supremum taken only at the edges |
| CorrelationSimilarity | `1 - abs(r_a - r_b)/2`, with Pearson's r from `k`, `Sx`, `Sy`, `Sxx`, `Syy`, `Sxy` | Equal for Pearson, up to the declared grid's quantisation |
| ContingencySimilarity | TVComplement of the (group, bin) table of a grouped histogram property | Equal, with the groups and bins as the two columns' categories |

Pass criteria:

- **Definitions.** On synthetic record sets, every aggregate metric equals the
  SDMetrics formula evaluated on the records. TV and contingency must match
  exactly, as rationals. KS must match the edge-evaluated record KS exactly
  and must never exceed the full record KS. Pearson's r must match NumPy's
  r on the encoded values to `1e-12`.
- **Axioms**, as properties over random statistics: every metric lies in
  [0, 1]; a statistic scores 1 against itself; scores are symmetric; and
  KSComplement is at least TVComplement.
- **The engine against S.** The generator's declared statistics are set from
  a committed S alone, which is the aggregate-only loop of ADR 0003: bin
  weights from the histogram's counts, market weights from the grouped
  histogram's group sizes. D' is generated, and its S is compared with the
  committed S. TVComplement and KSComplement must stay within three times the
  expected sampling distance. ContingencySimilarity must too, when the
  committed columns are independent. The fidelity check's verdicts must equal
  `deviation <= margin` computed from the two S values.
- **What the loop shows the generator cannot do**, measured and pinned to a
  prediction rather than left as a gap:
  - The histogram family keeps bin masses, not the mean, because it draws
    uniformly inside each bin. With the coarse bins used here, the mean moves
    about 10%, and the fidelity check withholds on the mean property. The
    synthetic mean must sit within four standard errors of the within-bin
    uniform prediction, which shows that the whole shift comes from the
    declared shape.
  - Without `given`, the generator draws columns independently. When the
    committed markets differ in their amounts, ContingencySimilarity is capped
    by the committed table's own distance from independence. The measured
    score must sit within three sampling distances of that cap.
- **Conditional draws lift the cap.** The same dependent markets, with
  `amount` declared `given` `market` and one bin-weight list per market taken
  from the grouped histogram's (group, bin) counts, draw D' from S's own joint
  table. ContingencySimilarity must then sit within three one-sample sampling
  distances of 1, and the fixture must discriminate: its independence cap lies
  further below 1 than that. The ungrouped histogram's TVComplement must stay
  within its own sampling distance too, because the joint implies it. A chain (channel, then market given channel, then
  amount given market) is declared from the tree
  `flux_engine.calibrate.structure` suggests from the two-way count tables
  (`tests/accuracy/fidelity/test_tree_draws.py`). The suggested tree must be
  the true chain. Each table on a tree edge must sit within three one-sample
  distances of the committed table, and the table off the tree and the
  three-way table within three two-sample distances.

### 2. Goodness of fit, for every sampler and every declared distribution

Each case draws the way a run draws, with one path-derived stream per value
(`tests/accuracy/harness/draws.py`). The test therefore covers the whole chain
from the seed tree to the sampler's transform, not PCG64 alone. Each case
also states its reference with an independent scipy implementation. That
reference includes the engine's quantisation onto the declared scale, its
clamping onto the inward-rounded bounds and its Poisson search cap. A
reference that ignored those would fail a correct sampler.

- **Kernel samplers** (`gof/kernel_cases.py`): `uniform53`, `bernoulli`,
  `categorical`, `bounded_uint` (including a bound at which an undebiased
  multiply-shift would be visibly wrong), `permutation`, `truncated_normal`
  (both branches, both far tails, and integer and one-digit scales),
  `exponential` and `poisson`.
- **Declared distributions, by kind**: Persona traits, market assignment and
  lifecycle cohorts (World); every generator family (categorical, levels and
  equal-weight support, bounded-normal, rate, uniform grid, histogram, null
  fraction); signal arrivals (emitter share, occurrence count, burst start,
  gaps); journey reactions; campaign delivery; experiment assignment; and
  temperature dials.
- **Tests**: `kstest` and `cramervonmises` against the fully specified
  reference for continuous output. `chisquare` with thin cells pooled, or the
  exact binomial test, for discrete output. `anderson`, composite, where scipy
  supports the family. scipy reports Anderson-Darling p-values interpolated
  from tables and clipped to [0.01, 0.15]. A clipped p-value only ever
  overstates, so it enters the adjustment as a conservative bound. It can
  never cause a false alarm, and on its own it cannot raise one at
  `q = 0.01`; the battery's power comes from KS and CvM.
- **Pass criterion**: no test rejected after BH or BY adjustment at
  `q = 0.01`, within each battery.
- **Power** (`gof/test_kernel_power.py`): the mean of a truncated normal,
  an exponential, a Poisson and a Bernoulli is moved by 2%. The shifted
  sample must be caught in at least 90% of repetitions. "Caught" means its
  smallest p-value is at or below the battery's rank-one critical value:
  `q/m` under BH, `q/(m * c(m))` under BY, where `m` is the size of the
  battery. At that value the real battery rejects it whatever the other
  cases did. The exponential and Bernoulli cases reach that power only at
  about 250 000 draws, so their power checks run nightly.
- **Numerics**: the decimal Φ and Φ⁻¹ against scipy's `ndtr` and `ndtri`, to
  `1e-12` relative, over the whole reachable range. The round trip
  `Φ(Φ⁻¹(p))` is held to the precision measured for each range of p. It
  measured about `5e-26` relative for p in [0.001, 0.999], `2e-23` down to
  p = 1e-16, the smallest p an untruncated draw reaches, and `2e-19` at
  p = 1e-300, which only a far-tail truncation reaches. That is fewer than
  the 28 digits the kernel evaluates at: in the far tail, one Halley step
  does not fully recover Acklam's starting estimate. Every draw is quantised
  to its declared scale, at most 8 places in the shipped bundles, long before
  those digits matter.

### 3. The fidelity gate's own accuracy

`ship` withholds a bundle with `FLX-8001` when the fidelity check fails.
Family 3 measures how often that decision is wrong. Deviations of known size
are injected into a synthetic dataset, relative to a synthetic committed
state: a mean shift for a mean property, a mixture shift of stated total
variation for a histogram property, off-support records for a support
property, and a shift of the order statistic for a quantile property. The
decision is recorded for each injected case.

- **False accept**: the population deviation exceeds the declared tolerance
  and the gate ships.
- **False reject**: the deviation is within the tolerance and the gate
  withholds.

The family reports the withhold rate at each injected deviation with its
exact (Clopper-Pearson) interval, and the ROC with its area. The ROC is
traced by the sample deviation the gate thresholds, with positives as
deviations above the tolerance.

Pass criteria:

- **Exactness**: every decision equals an independent recomputation, in
  rational arithmetic, of whether the sample deviation lies within the margin
  `τ' = τ - ε`. Any disagreement is a defect, so the criterion is 100%.
- **Operating point**, at the nightly sizes (10 000 committed and 10 000
  synthetic records, 150 trials each at no deviation and at twice the
  tolerance): the false-reject rate with no deviation and the false-accept
  rate at twice the tolerance each have an exact upper 95% bound of at most
  5%. The pull-request tier runs 12 trials per deviation, too few for that
  bound, so it records the rates without gating on them.
- **Separation**: the ROC area is at least 0.99 between deviations of at most
  half the tolerance and deviations of at least 1.5 times it.
- **Support**: off-support records are counted exactly, so the gate withholds
  if and only if their count exceeds the declared maximum.
- **End to end**: through `python -m flux_engine.cli ship` on
  `tests/fixtures/telco-fidelity-small`, a committed slice equal to the run's
  own rows ships with exit 0. The same slice with every amount raised by 200
  exits 1 with `FLX-8001` and writes no bundle. In both cases the outcome
  must match the declared rule, `|mean_synthetic - mean_committed| <= 50`,
  evaluated on the two record sets.

The rates near the tolerance itself, at 0.9 and 1.1 times it, are reported but
not gated. They measure the sampling noise at the stated sizes, which is a
property of `n` and `τ`, not of the gate.

### 4. Properties and metamorphic relations

- **Schema-driven properties.** hypothesis-jsonschema 0.23.2 (MPL-2.0,
  test-only, never vendored) generates documents that validate against the
  vendored FLUX 0.5.0 and FLUID 0.7.5 schemas. Each one is spliced into a
  known-good bundle and run. The engine must either refuse with a typed
  `FLX-` error or produce output that conforms to its contract. It must
  never crash.
- **Metamorphic relations**, each tested at the level where it holds:
  1. *The same seed gives the same bytes*: in-process, across processes, and
     under a hostile ambient `decimal` context.
  2. *Column order does not change other columns*: under a fixed seed-tree
     root, permuting the expose's columns leaves every column's values
     unchanged. End to end the root moves, by design, because `H` binds the
     column order (KERNEL-SPEC §3).
  3. *Scaling a dwell distribution scales the output*: on one stream,
     `exponential(rate / c) = c * exponential(rate)` up to quantisation, and
     doubling the gap mean of the arrival model doubles every gap.
  4. *More rows keep the marginals*: under a fixed root, the first `n` rows
     of a `2n`-row stream are the `n`-row stream, and the marginals of both
     pass the same goodness-of-fit tests.
  5. *Adding a column leaves sibling streams untouched*: under a fixed root
     for the generator, and end to end for a Persona trait, which is not part
     of `H`.
  6. *A forest of conditional draws keeps every generator invariant*:
     Hypothesis draws random valid forests of 2 to 7 columns (`given` links
     from categorical columns of every kind, histogram leaves, chains, NFC
     and NFD spellings of a parent's levels, zero weights, a shuffled declared
     order). Two runs agree byte for byte; permuting the declared order
     changes no column; column-first equals row-first; slices join; a longer
     plan extends a shorter one; every drawn level and bin carries weight in
     the row of its parent's level; replacing every conditional declaration
     with an unconditional one leaves the root columns unchanged; and a
     single corruption of a valid forest is refused at plan time with
     `FLX-3402`, never with another exception type.
- **Counterexamples** become regression cases. A falsifying example is written
  to `FLUX_ACCURACY_COUNTEREXAMPLES` and uploaded with the nightly artefact.
  Each one is then committed under `tests/accuracy/properties/regressions/`,
  where a replay test runs it on every pull request.
- **Mutation testing** runs weekly with mutmut 3.8.1 over the kernel's numeric
  core: `core/lrm.py`, `core/rng.py`, `core/seedtree.py`,
  `core/fixedpoint.py` and the S statistic. The target and its justification
  are in the [mutation section](#mutation-testing-target).

### 5. Seed-tree stream quality

- **Collisions**: the 256-bit entropies, and the first 64-bit words, of large
  sets of sibling paths must be distinct. This covers paths that differ by
  one ordinal, by one character, or only by Unicode normalisation (which
  must collide, by design). The reference is NumPy's own bound for
  SeedSequence: about `n**2 * 2**-128` for `n` streams from a 128-bit pool.
  At the sizes tested, that bound puts any collision at odds far below one
  in a trillion. A single collision is therefore a defect.
- **Avalanche** (strict avalanche criterion): an ordinal moved by one, an
  occurrence moved by one, one bit of one name character flipped, or the root
  seed's lowest bit flipped must flip each output bit with probability 1/2.
  For each of the four mutations and each of the 256 entropy bits and 64
  first-word bits, an exact binomial test over the sampled base paths (2 000
  at the pull-request tier, 20 000 nightly) goes into one battery at
  `q = 0.01`, and none may be rejected. The 320 tests of one mutation come
  from the same pairs, so they share a sample and the battery uses BY. The overall flip fraction of each
  mutation and output must also lie inside its exact interval around 1/2.
  Those eight intervals are taken at the Bonferroni level `1 - 0.01/8`, so
  together they keep the 1% false-alarm budget. A stuck bit and a biased bit
  are injected into the real battery as controls and must be caught: 60% at
  the pull-request tier and 55% nightly, because 55% is not reliably caught
  at 2 000 pairs.
- **Sibling first words**, at the pull-request tier as a cheap stand-in for
  PractRand: the first words of 2^16 sibling streams (2^20 nightly). The
  battery has byte frequencies (chi-square over 256 cells), the balance of
  each of the 64 bits, and the lag-1 serial correlation. These tests read
  the same words, so they share a sample and the battery uses BY.
  A forced bit, correlated words and skewed bytes are injected as controls.
- **PractRand** (public domain), nightly, as a subprocess. It reads 2^30
  bytes of the first words of sibling path-derived streams, which is the
  sequence a column of one-draw cells actually sees. It also reads an
  interleave of 256 sibling streams' longer runs. The pass criterion is no
  `FAIL` at any length, PractRand reaching the full 2^30 bytes, and a clean
  exit; anything milder (`unusual` and the grades of suspicion) is logged.
  TestU01 is not used, because its licence is in conflict. dieharder is not
  bundled, because it is GPL.

### 6. Journey conformance

- **Transitions**: each (Persona, state) row of the empirical transition
  matrix is compared with the declared one. The declared row gives success
  probability `p` toward `nextState`, and failure stays put. A subject leaves
  its row at its first success, so counts pooled across steps are not
  binomial. Each (row, step) cell gets its own exact binomial test instead,
  in one BH battery at `q = 0.01` across two seeds. The family reports the
  total-variation and Jensen-Shannon distances of each row, and a two-sample
  chi-square on each row's tables compares the two seeds. Under the
  `campaignResponse` dial, at `t` = 0, 0.5 and 1, the frozen share's success
  count must equal `LRM(count, p)` exactly, and the stochastic share is
  binomial. A shift of 0.1 in `p` must be rejected in every row.
- **Dwell times**: the dwell is the time from one step of a subject to its
  next, counted where the first step failed and the subject stayed. That is
  one gap of the signal's burst, produced by the real arrival and journey
  stages. Two independent seeds are compared with two-sample KS and k-sample
  Anderson-Darling (one BY battery, since the tests share samples), and must
  not be rejected. Scaling the arrival gap mean by 2 must scale the dwell
  distribution by 2. The rescaled sample must not be rejected, and the
  unscaled comparison at `c = 1.1` must be rejected in at least 90% of
  repetitions.
- Implemented with scipy and NumPy only. pm4py is not used, because it is
  AGPL-3.0. ocel-standard is not used, because it is GPL-3.0.

### 7. The determinism matrix

Linux and macOS, Python 3.11, 3.12 and 3.13, each with NumPy at its floor and
at its latest. The floor is `numpy>=1.26` from `pyproject.toml`, raised to the
first release with wheels for the interpreter. That is 1.26.0 for 3.11 and
3.12, and 2.1.0 for 3.13. Each cell writes a manifest of digests computed
under the night's fresh seeds: kernel draws, generator rows, seed-tree
entropies, and the run digest of one small end-to-end run. A final job
requires every manifest to be byte-identical. The cells at the NumPy floor
also run the pinned digest set. `ci.yml` already runs that set at the
latest NumPy on every pull request and weekly on all six cells, so it is not
repeated there. Linux runs nightly. macOS runs weekly, on Mondays (UTC), and
on demand: label a pull request `accuracy-weekly`, or dispatch the workflow
with `weekly` set.

### 8. Nightly accuracy and speed

pytest-benchmark (BSD-2-Clause) times the kernel samplers, stream
derivation, row generation, S and the fidelity check. Each night produces
one joined JSON artefact (`flux-accuracy-nightly/1`). It contains the run's
identity and base seed, every family's measured cases, the benchmarks,
PractRand, the determinism comparison and, in the weekly run, the mutation
score. A benchmark that runs three times slower than the committed snapshot
fails. One that runs 1.5 times slower is flagged.

### 9. The report

`docs/accuracy/REPORT.md` is rendered from `docs/accuracy/latest.json` by
`tests/accuracy/report/render.py`. `latest.json` is a committed nightly
artefact. The report covers what is tested, the criteria and the latest
results, and it ends with a plain "What was not tested" section. A
pull-request test re-renders the page and fails if the committed page
differs, so the page cannot be edited by hand. Every nightly run uploads a
freshly rendered page with its artefact. Refreshing the committed snapshot
is a pull request like any other.

### Recovery tests for `calibrate` and `accuracy`

- `calibrate` declares and never fits, so the recovery it owes is
  exact. Every authored scorecard field comes back unchanged. The evidence
  digest moves if and only if the authored block moves. A grade is never
  invented.
- `accuracy register | score | record`: predictions with known intervals
  are scored against readouts drawn from a known truth. Each outcome must
  equal an independent containment check, boundaries included. Over many
  predictions at nominal coverage `c`, the recorded within-interval rate must
  fall inside the exact binomial interval around `c`.

## Mutation testing target

Each listed module keeps a kill-rate floor on its covered lines. No module
may drop more than five percentage points below the score in the committed
snapshot, `docs/accuracy/latest.json`. That snapshot is refreshed by hand, so
the comparison is against the last published weekly score, whatever its age.
A snapshot taken on a night without mutation testing carries no score, and
the drop rule then has nothing to compare against.
The floors are the first baseline, rounded down to the nearest five points:

| Module | Baseline (2026-11-11) | Floor |
|---|---|---|
| `flux_engine/concordance/boundary/statistic.py` | 90.4% (481 of 532) | 90% |
| `flux_engine/core/lrm.py` | 96.9% (31 of 32) | 95% |
| `flux_engine/core/rng.py` | 78.7% (448 of 569) | 75% |
| `flux_engine/core/seedtree.py` | 70.4% (426 of 605) | 70% |
| `flux_engine/core/fixedpoint.py` | 67.9% (534 of 787) | 65% |
| All five | 76.0% (1 920 of 2 525) | |

The baseline was measured with mutmut 3.8.1 and `mutate_only_covered_lines`,
on a development machine. The weekly job's own first run replaces it in the
report.

Two of the floors sit below the 75% that is a common expectation for core
logic, for reasons that were measured, not assumed. In a sample of 40
survivors per module, 17 in `fixedpoint.py` and 18 in `seedtree.py` changed
nothing but a string literal. These modules spell out their refusals in long
diagnostic messages, and the suite checks the error code rather than its
prose, by design. With those mutants set aside, both modules come to about
80%. The floors work as a ratchet rather than a target: a week whose score
drops fails, and a floor goes up when the survivors behind it are killed.
Equivalent mutants account for the rest of the gap. For example, a mutated
rounding mode on a value that is already on the grid changes nothing that
can be observed.

The tests mutmut runs are the kernel's own unit tests (`tests/core`,
`tests/concordance`), plus the two fast, deterministic accuracy modules
(`gof/test_kernel_numerics.py` and `fidelity/test_s_metric_definitions.py`).
The statistical batteries are left out. Each one recomputes tens of
thousands of draws in a module-scoped fixture, so running them for every
mutant would cost hours a week, on mutants the unit tests already settle.
The whole run took 11 minutes on 8 cores.

## Aggregate-only

ADR 0003 holds throughout. Calibration and fidelity work from aggregates.
Every record set in this program is synthetic, drawn by NumPy or by the
engine itself from stated parameters. No test fits anything to record-level
real data, and nothing imports SDV, which is BUSL-1.1. SDMetrics contributes
definitions, not code.

## Borrowed, not built

| Component | Choice | Licence | Notes |
|---|---|---|---|
| Fidelity metric definitions | SDMetrics 0.33.0 | MIT | Definitions only, reimplemented on S. SDMetrics itself needs pandas and scikit-learn and reads records. |
| Hypothesis tests, FDR control, exact intervals | scipy (`kstest`, `cramervonmises`, `anderson`, `chisquare`, `binomtest`, `ks_2samp`, `anderson_ksamp`, `false_discovery_control`) | BSD-3-Clause | scipy 1.17 added the `method` that gives `anderson` a p-value. |
| Jensen-Shannon distance | `scipy.spatial.distance.jensenshannon` | BSD-3-Clause | |
| ROC area | Mann-Whitney U from scipy, as `U / (n_pos * n_neg)` | BSD-3-Clause | No scikit-learn dependency for one number. |
| Schema-driven generation | hypothesis-jsonschema 0.23.2 | MPL-2.0 | Test-only, never vendored. |
| Stream battery | PractRand 0.95 | Public domain | Built from source in CI, with the archive's sha256 pinned. |
| Mutation testing | mutmut 3.8.1 | BSD-3-Clause | Separate `mutation` dependency group. |
| Benchmarks | pytest-benchmark 5.3 | BSD-2-Clause | Its cpuinfo dependency is py-cpuinfo2, an MIT fork. |
| Simulation-based calibration | simuk, ArviZ | | Deferred until the fitted calibrator exists. |
| Rejected | SDV (BUSL-1.1), TestU01 (licence in conflict), dieharder (GPL), pm4py (AGPL-3.0), ocel-standard (GPL-3.0) | | |

## What this program does not test

- **Simulation-based calibration** waits for the fitted calibrator. The engine
  declares scorecards today and fits nothing, so there is no posterior to
  calibrate.
- **Drift-detection accuracy** waits for drift detection on S, which does not
  exist yet.
- **Privacy and re-identification resistance**: S is aggregate by
  construction, and its privacy is outside this program.
- **The agentic tier and the stochastic sweep executor** are not built. The
  engine refuses them, and the existing suite tests those refusals.
- **Joint structure in generated data**: without `given`, the generator
  draws each column independently, and Family 1 measures the shortfall in
  ContingencySimilarity and CorrelationSimilarity. With `given`, Family 1
  measures the declared links: a histogram drawn given a categorical, and a
  three-column chain. Two numeric columns are never linked directly, because
  a histogram column is a leaf. A link between two categorical columns comes
  from the owner's own cross-tab, and S has no two-way count property for two
  categorical columns yet, so the fidelity check does not test that link.
  Nothing here claims joint fidelity beyond the declared links.
- **Real data**: every record set here is synthetic, drawn from stated
  parameters (ADR 0003). A fit to an owner's real committed state is
  measured only by that owner's own fidelity check.

The report page carries the same list, from `NOT_TESTED` in
`tests/accuracy/report/render.py`. A test holds the two together.
