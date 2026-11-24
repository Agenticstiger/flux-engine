# ADR 0035: number columns are drawn together through a Gaussian copula of published rank correlations, and a profile states how its cliques were selected

## Status

Accepted 2026-11-23. Builds on
[ADR 0019](./0019-one-privacy-budget-per-profile-release.md) (one budget per release, and what the reader
charges a noisy statistic), [ADR 0020](./0020-privacy-grades.md) (grades),
[ADR 0021](./0021-clique-marginals-are-drawn-through-a-junction-tree.md) (cliques through a junction tree) and
[ADR 0014](./0014-init-reads-every-statistic-a-profile-publishes.md) (`fluxsim init`, which passes `columnPairs`
over today), and on KERNEL-SPEC-2 sections 5 (binary64 transforms) and 12 (additive paths). Two records come with
it: [ADR 0036](./0036-binned-number-columns-join-clique-marginals.md) lets number columns join cliques, and
[ADR 0037](./0037-child-columns-and-child-counts-follow-their-parent-and-a-unit-can-follow-its-children.md) draws
across a relationship. The format text is in [PROFILE-SPEC.md](../spec/PROFILE-SPEC.md) (profile format 1-4-0),
[PROFILE-REQUEST-SPEC.md](../spec/PROFILE-REQUEST-SPEC.md) (request format 1-2-0),
[SEAM-FIDELITY-SPEC.md](../spec/SEAM-FIDELITY-SPEC.md), [KERNEL-SPEC-2.md](../spec/KERNEL-SPEC-2.md) section 8.6
and [SCORECARD-SPEC.md](../spec/SCORECARD-SPEC.md) (scorecard format 1-5-0), and the profiler's side of clique
selection in [clique-selection-for-profilers.md](../spec/clique-selection-for-profilers.md).

**What this release builds.** The rank correlations and their charge (decisions 1 and 2), `fidelity.copula`, its
plan and its draw (decisions 4 to 6), the `rankCorrelation` property and the scorecard format that holds it
(decision 7), the grades (decision 8), and `cliqueSelection` for measured cliques (decision 10). **What it leaves
for later:** cliques estimated from measurements (the format does not read `estimate`, `measurements` or
`published: estimated`), a copula over categorical columns, NULL as a level of a clique, and binned parents in the
Chow-Liu forest. The parameter register stays at format 1-3-0.

## Context

**What FLUX draws today.** Categorical columns are drawn together: through the cliques of a profile released under
differential privacy (ADR 0021), and through the Chow-Liu forest of its grouped statistics (ADR 0014 section 4). A
number column can be drawn given one categorical column (`binWeightsGiven`). Two number columns are never drawn
together. A profile may publish `correlation/1` over a pair of number columns (PROFILE-SPEC.md section 5,
`columnPairs`); `verify` scores it, and `fluxsim init` passes it over because no declaration draws from it
(`flux_engine/author/ledger.py`).

Pearson's correlation is the wrong thing to draw from. It depends on the two margins, so a profile's histogram and
its Pearson correlation need not be reachable together. It is refused over a DATE column (PROFILE-SPEC.md section 7).
And its sensitivity to one row is not bounded unless the values are clipped, so it cannot be released with noise
that ADR 0019 can charge. A rank correlation has none of these faults: it does not change when either column is
mapped by an increasing function, so it combines with any margin; it is the same for a date in milliseconds or in
days; and one row moves it by a bounded amount. A Gaussian copula takes a matrix of rank correlations and any
margins and gives the joint (Kruskal 1958, below).

**Borrow-before-build (2026-11-23).** Inside the repository: kernel 0.2.0's pinned `ppnd16` (Φ⁻¹, AS241) and
`log_basic`, the 50-digit `normal_cdf` and `_pi` of `flux_engine/core/rng.py`, `flux_stats.noise.point_estimate`
and `flux_engine/calibrate/junction.py`. There is no copula and no nearest correlation routine. numpy is a
dependency, but `numpy.linalg` calls LAPACK, whose bits differ between builds, and KERNEL-SPEC-2 section 4's list of
what never runs on a digest path keeps it off one. Searches: a differentially private Gaussian copula; the sensitivity of Kendall's tau and of
Spearman's rho under differential privacy; nearest correlation matrix implementations; Gaussian copula libraries and
their licences. What was read:

- H. Li, L. Xiong and X. Jiang, "Differentially Private Synthesization of Multi-Dimensional Data using Copula
  Functions", EDBT 2014, pp. 475-486, DOI 10.5441/002/edbt.2014.43 (DPCopula). DPCopula-Kendall releases each
  pairwise Kendall's tau, the plain form (sign products over all pairs, divided by `C(n, 2)`), with Laplace noise,
  and states its sensitivity as `4 / (n + 1)` for data sets that differ by one record (Lemma 4.1). It forms the
  copula's correlation as `sin(pi * tau / 2)` and repairs a matrix that is not positive definite by replacing its
  negative eigenvalues. Its demonstration is H. Li, L. Xiong, L. Zhang and X. Jiang, "DPSynthesizer", PVLDB
  7(13):1677-1680, 2014, DOI 10.14778/2733004.2733059 (read as a bibliographic record). The pattern is taken. The
  code at github.com/Emory-AIMS/DPCopula states no licence, so none of it is used.
- H. J. Asghar, M. Ding, T. Rakotoarivelo, S. Mrabet and M. A. Kaafar, "Differentially Private Release of
  High-Dimensional Datasets using the Gaussian Copula", arXiv 1902.01499, 2019: noisy two-way counts per pair become
  correlations, repaired by Higham's nearest correlation matrix and a floor on the eigenvalues.
- N. J. Higham, "Computing the nearest correlation matrix: a problem from finance", IMA Journal of Numerical
  Analysis 22(3):329-343, 2002, DOI 10.1093/imanum/22.3.329; N. J. Higham, N. Strabić and V. Šego, "Restoring
  definiteness via shrinking, with an application to correlation matrices with a fixed block", SIAM Review
  58(2):245-263, 2016, DOI 10.1137/140996112. statsmodels 0.15.0 (BSD-3-Clause) has `corr_nearest` and
  `corr_clipped` in `statsmodels.stats.correlation_tools`, both on `numpy.linalg.eigh`.
- sdv-dev Copulas 0.15.0 is under the Business Source License 1.1 (its LICENSE file and PyPI). It was read for the
  pattern only: `GaussianMultivariate` maps each column through its fitted distribution and the normal quantile and
  takes the Pearson matrix of the scores. No code is used.
- W. H. Kruskal, "Ordinal measures of association", Journal of the American Statistical Association
  53(284):814-861, 1958, DOI 10.2307/2281954: Kendall's tau and Spearman's rho, and their values under the
  bivariate normal, `tau = (2 / pi) arcsin(r)` and `rho_S = (6 / pi) arcsin(r / 2)`. C. Genest and J. Nešlehová,
  "A primer on copulas for count data", ASTIN Bulletin 37(2):475-515, 2007, on why these relations fail for
  margins with ties.

**What a prototype measured (2026-11-23, outside the repository).** Every sensitivity of decision 2 was checked
exhaustively for data sets of up to seven rows and by adversarial search up to thirty. Kendall's floored tau reaches
its bound `4 / (f + 1)` at `n = f` and never passes it. The floored Spearman form never passed `6 / (f + 2)`,
under the `9 / (f + 1)` derived in decision 2. The Pearson correlation of mid-ranks has no bound of the form `C / n`:
`n - 1` rows tied at one point and one row at another have a correlation of 1, and one more row, high in one column
and low in the other, takes it to `-1 / n`. For the draw of decision 6, the per-row and the column computation gave
the same bits over 200,000 rows of eight columns; the repair of decision 5 took 1.7 seconds for 64 columns and 12
seconds for 128, and gave the same bits on every run. Drawing the place inside a bin independently of the copula, as
a histogram does today, lowered the reproduced tau by 17 to 25 percent with five bins and by 1 to 3 percent with
twenty; drawing it from the copula's own residual removed the loss. The checks of the bounds are tests of the
repository (`packages/flux-stats/tests/test_rank_correlation_sensitivity.py`), so anyone can run them.

**Clique selection.** The receipts for decision 10 are in the note for profilers: AIM (R. McKenna, B. Mullins,
D. Sheldon and G. Miklau, PVLDB 15(11):2599-2612, 2022, arXiv 2201.12677), MST (R. McKenna, G. Miklau and
D. Sheldon, "Winning the NIST Contest", Journal of Privacy and Confidentiality 11(3), 2021, DOI 10.29012/jpc.778),
private-pgm (github.com/ryan112358/private-pgm, Apache-2.0; its package `mbi` 2.1.0 needs jax), OpenDP 0.16.0
(MIT; `make_contingency_table` in `opendp.extras.mbi`, whose `opendp[mbi]` extra pins `mbi` 1.1.0) and
smartnoise-synth 1.0.8 (MIT).

## Decision

### 1. Two rank correlations a profile may publish

Profile format 1-4-0 lets a `columnPairs` entry hold, beside `correlation/1`, two registered definitions over its
pair. The two columns are number or timestamp columns; a DATE column is one too, since ranks do not change when
milliseconds become days. Values are floored onto the profile's grid first, so a tie is a tie on the grid. A record
counts when both of its values are known, and `n` is the number of such records.

- **`kendallTau/1`.** `S` is the sum over every pair of records of `sign(x_i - x_j) * sign(y_i - y_j)`; a tied pair
  adds zero. The statistic is `tau = S / C(N, 2)` with `N = max(n, f)`. Its quantities are `S` and `k = n`, both
  integers.
- **`spearmanRho/1`.** `R` and `Q` are the mid-ranks of the `n` records in each column, and
  `A = sum (R_i - (n + 1) / 2) * (Q_i - (n + 1) / 2)`, so `4A` is an integer. The statistic is
  `rho = 12 A / (N (N^2 - 1))`. Its quantities are `A4 = 4A` and `k`. Without ties it is Spearman's coefficient.
  With ties it is not the Pearson correlation of the mid-ranks, deliberately: that form has no sensitivity bound.
- **`rowFloor`.** `f` is the statistic's `rowFloor`, a count of at least 2 chosen without the data, or `n` when the
  statistic has none. It is part of the statistic's definition, not a claim that `n` is at least `f`: a smaller `n`
  only shrinks the value towards zero.

Both are ungrouped in format 1-4-0, and the disclosure rules apply to them as they apply to `correlation/1`. The
group size rule never withholds the noisy form: a noisy rank correlation is released whatever the number of its
records, since its noise is its protection.

### 2. Their noise, and what the reader charges for it

A rank correlation may carry a `noise` block (PROFILE-SPEC.md section 3a) when it states a `rowFloor`. Its one group
is `{ "group": "", "status": "F", "noisyValue": v }`, a decimal string, with no `S`, `A4` or `k`. Under adding or
removing one row of the unit's expose:

- **Kendall: `Delta = 4 / (f + 1)`.** A new row changes `S` by some `g` with `|g| <= n`. For `n >= f`,
  `tau' - tau = 2 (g - n tau) / (n (n + 1))`, so the change is at most `4 / (n + 1)`, which is at most
  `4 / (f + 1)`. For `n < f` both sides divide by `C(f, 2)`, and the change is `|g| / C(f, 2) <= 2 / f`, below the
  same bound. The bound is reached at `n = f`. DPCopula's `4 / (n + 1)` is the case with no floor, where the noise
  scale would depend on the private `n`; the floor removes that dependence.
- **Spearman: `Delta = 9 / (f + 1)`.** A new row moves each other record's centred mid-rank by `-1/2`, `0` or `1/2`
  in each column, and its own lies within `n / 2` of the centre. Mid-ranks spread no further than ranks, so the
  absolute centred ranks add to at most `n^2 / 4` and `|A| <= n (n^2 - 1) / 12`, which give
  `|A' - A| <= n^2 / 2 + n / 4`. For `n >= f` the change is then at most
  `3 (3n + 2) / ((n + 1)(n + 2)) <= 9 / (n + 1)`, and for `n < f` at most `12 (n^2 / 2 + n / 4) / (f (f^2 - 1))`,
  which is below `6 / (f + 1)`. The largest change the prototype found is `6 / (f + 2)`; the reader charges the
  derived constant until a tighter argument replaces it.
- **The charge** is ADR 0019's for a count table, with `Delta` in place of 1: a Gaussian of scale `s` spends
  `rho = Delta^2 / (2 s^2)` under zero-concentrated DP, and a Laplace of scale `b` spends `epsilon = Delta / b`. A
  share below that is refused (`FLX-5036`), and one above it is the profiler's slack. All four pairings of a
  mechanism and a measure are checked with the sensitivities of the statistic, as they are for a count table: a
  Laplace under zero-concentrated DP spends `rho = (Delta / b)^2 / 2`, and a Gaussian under approximate DP states
  a `delta` above zero and an `epsilon` at least `rho + 2 sqrt(rho ln(1 / delta))` with `rho = Delta^2 / (2 s^2)`.
- **A matrix is its pairs.** Each pair is a statistic with its own share, and the shares add (ADR 0019 decision 3).
  A copula over `q` columns therefore costs `m = q (q - 1) / 2` shares: under a Gaussian the same as charging the
  vector of its pairs its L2 sensitivity `sqrt(m) * Delta`, and under a Laplace its L1 sensitivity `m * Delta`.
- **Another table.** In a table other than the unit's expose, `Delta` is multiplied by the number of that table's
  rows one unit contributes (ADR 0037 decision 1). Where that number is zero, noise is refused, as it is today.
- **Refused** (`FLX-5036`): a noisy rank correlation with no `rowFloor`; one under `discreteGaussian`, since the
  statistic is not an integer count; and, as today, a noisy `correlation/1`, whose sensitivity has no bound.

### 3. `fluxsim init` writes one copula per table

For a table whose profile publishes rank correlations between number or timestamp columns that `init` draws from a
histogram (ADR 0014 section 3), `init` writes one copula over those columns (decision 4). An exact value is
`S / C(N, 2)` or `3 A4 / (N (N^2 - 1))`, with the statistic's own `N` (decision 1), rounded half to even at 12
places. A noisy value is the published one clamped to `[-1, 1]`, which is post-processing of the release. A copula
has one measure: Kendall's when the profile publishes both.

The ledger passes over, each with its reason: a pair with a column that decision 4 does not admit, that is drawn by
a clique (ADR 0021, ADR 0036) or that is drawn given another column; a pair the profile withheld, which the copula
then holds at zero, stated in the ledger as an assumption; and, beyond 64 columns, the columns with the fewest
published pairs, name order breaking ties. `correlation/1` stays passed over, now with the reason that the copula
reads rank correlations.

When `init` writes a profile request, it asks, in request format 1-2-0, for `kendallTau/1` over the number and
timestamp columns it would draw from histograms, and names the columns a clique could hold (the draft's section 2).

### 4. The declaration: `fidelity.copula`

An emit's fidelity block may hold one `copula`:

```yaml
fidelity:
  copula:
    columns: [amount, balance, opened_at]
    rankCorrelations:
      measure: kendall
      pairs:
        - { columns: [amount, balance], value: "0.42" }
        - { columns: [amount, opened_at], value: "-0.08" }
```

- `columns`: `q` distinct columns of the emit, 2 to 64, each a number or timestamp column drawn from `bins` and
  `binWeights`, with no `given` and no `allocation`, and set by nothing else: not a key, and not set by a rule, a
  `lifetimes`, `within` or `rates` entry, a history, a hidden declaration or a copy (`sharedColumns`,
  `sharedAttribute`). A bin of positive weight that holds no grid value is refused. The order is the order of the
  draws (decision 6).
- `measure`: `kendall` or `spearman`, the population measure the values estimate.
- `pairs`: each names two of the `columns`, at most once in either order, with a decimal `value` in `[-1, 1]`. A pair
  not listed has the value 0.
- A copula column may declare a `nullFraction`: its NULL draw stays on its own cell path, independent of the copula,
  and the copula draws its value whether or not the cell is NULL. A copula column is no `given` parent, since a
  column has one joint (decision 9).

Anything else is `FLX-3402`. Under kernel `0.1.0` the key is `FLX-3105`, naming the kernel. The copula lives in the
fidelity block, so, like `given` and the weights beside it, it enters both `H` and `G` (SEAM-FIDELITY-SPEC.md
section 3): `strip_excluded_from_fidelity_block` copies it through unchanged, and a committed state made under one
copula is not reused under another. An end-to-end `ship` test covers the key.

### 5. The plan makes the matrix valid, in pinned arithmetic

In `decimal` at 50 digits, rounding half to even, as KERNEL-SPEC-2 section 5.3 computes Φ:

1. **The latent correlations.** `r = sin(pi * tau / 2)` for Kendall's measure and `r = 2 sin(pi * rho / 6)` for
   Spearman's. They are exact for continuous margins. π is `rng._pi` at 60 digits, and the sine is its Taylor
   series summed at 60 digits, then rounded to 50 digits: the first term is always added, a later term is added only when
   its magnitude is at least `10^-55`, and the series stops at the first term below that, which is not added.
   The argument lies in `[-pi/2, pi/2]`, so the series needs no reduction.
2. **The matrix** `R` has a unit diagonal and `r` off it, in the order of `columns`.
3. **The test.** `R` passes when every pivot of its LDLᵀ factorisation, without pivoting and in the order of
   `columns`, is at least `delta = 10^-6`.
4. **The repair.** When `R` passes, `alpha = 0`. Otherwise `R` is shrunk towards the identity,
   `R(alpha) = (1 - alpha) R + alpha I` (Higham, Strabić and Šego 2016), with `alpha` from 40 steps of bisection on
   `[0, 1]`: `lo = 0`, `hi = 1`, and at each step the midpoint becomes `hi` when `R(mid)` passes and `lo` when it
   does not; `alpha = hi`. The identity always passes, so the repair never refuses.
5. **The factor.** `L` is the Cholesky factor of `R(alpha)`, each entry the unit factor times the square root of
   its pivot, at 50 digits, then rounded once to binary64. An entry whose rounded binary64 magnitude is below
   `2^-900` is taken as zero, so that no product of decision 6 is subnormal (KERNEL-SPEC-2 section 5.1).

The emit's plan document gains a `copula` member, `{ "alpha", "factor" }`, `factor` the sha256 of `L`'s binary64
words, the lower triangle including the diagonal in row-major order, each entry as big-endian IEEE 754 bytes, only when the emit declares a copula, so it enters `planDigest` there and nowhere else.
`EVIDENCE.md` states `alpha` and that every correlation was multiplied by `1 - alpha` on the latent scale.

### 6. The draw (kernel `0.2.0`)

The path is `("sim", sim_id, "gen", exposeId, "copula", row)`, with the row an ordinal: its key label leaves the row
out, and the counter is `(row, 0, d, 0)`. Draw `d`, for `d` from 0 to `q - 1`, is the column at position `d` of
`columns`. For each row:

```text
u_d = (2 * (w0 >> 12) + 1) * 2^-53          exact, strictly inside (0, 1), symmetric about 1/2
z_d = Φ⁻¹(u_d)                              KERNEL-SPEC-2 section 5.2
x_i = L[i][0] * z_0 + L[i][1] * z_1 + ... + L[i][i] * z_i     left to right, one IEEE operation each
```

**The value.** A column's bins `b = 1 .. B` have shares `p_b` (its `binWeights`, normalised, exact) and cumulative
shares `c_b = p_1 + ... + p_b`, with `c_0 = 0`. Bin `b` holds `w_b` grid values, numbered `0 .. w_b - 1` in the order
the histogram's own draw counts them, and the share before value `j` is `F(b, j) = c_(b-1) + p_b * j / w_b`, an exact
fraction. `binary64(F)` is the fraction rounded once to the nearest binary64, ties to even. A threshold is
`t(F) = Φ⁻¹(binary64(F))` when `F <= 1/2` and `-Φ⁻¹(binary64(1 - F))` when `F > 1/2`, so an upper tail keeps its
digits; a binary64 argument below `2^-1022` gives an infinite threshold of its sign. With `x = x_i`:

```text
lo = 0, hi = Bp                                    the bin; Bp is the last bin of positive weight
while hi - lo > 1:  mid = (lo + hi) // 2;  if t(c_mid) <= x: lo = mid  else: hi = mid
b = lo + 1
lo = 0, hi = w_b                                   the grid value inside bin b
while hi - lo > 1:  mid = (lo + hi) // 2;  if t(F(b, mid)) <= x: lo = mid  else: hi = mid
the value is grid value lo of bin b
```

A bin of weight zero is never chosen: its thresholds equal the next bin's, and starting `hi` at the last bin of
positive weight keeps `x = +inf` from selecting a trailing bin of weight zero.

So the value is uniform over its bin's grid values given its bin, its margin is its histogram's, and the copula holds
across bins and inside them. The path draws nothing else, and a copula column draws nothing on its own cell path but
its NULL draw. An implementation may tabulate thresholds, for a narrow bin for instance; the bisection defines the
result.

**Section 12.** The literal `copula` stands at the place after `exposeId`, where no other shape has it (`row`, `key`,
`pair`, `count`, `allocate`, `entities`, `versions`, `history`, `change`, `ended`, `reason`, `lag` and `kind` are
taken there). Only
a bundle that declares `fidelity.copula` reads the path; nothing that exists moves; and
`tests/vectors/kernel2-copula-vectors.json` pins the key label, the key, the counters and blocks, and `u`, `z`, `x`,
the thresholds, the bins and the values of small copulas, one of them shrunk and one with a bin of weight zero.

**Equivalence, jobs, shards and scale.** The column path makes the same operations element by element: no matrix
product and no reduction that could reorder a sum. A test holds the two paths to the same bytes. A row's values
depend on its ordinal alone, so `--jobs 1` and `--jobs 4` give the same digests, the shards of `--shard I/N`
concatenate to the whole run, and `--scale` changes only the number of rows.

### 7. `verify` scores rank correlations (scorecard format 1-5-0)

A property kind `rankCorrelation`, with `columns: [a, b]` and `measure`, sits in the `joint` dimension and reads its
reference from a profile.

- **The two sides.** The run's side is the plain statistic over the run's rows, with no floor; the reference is the
  profile's value, exact or the noisy point estimate. Both are formed on the profile's grid: the block's
  `fractionalBits` must equal the profile's, as PROFILE-SPEC.md section 7 asks of `correlation/1`. A pair is read in
  either order, since both statistics are symmetric, and a DATE column needs no conversion, since ranks do not change
  when milliseconds become days.
- **The cost.** Forming `S` or `A4` sorts the run's rows of the pair: `O(n log n)` time (Knight's merge count for
  Kendall) and two values per row in memory, over the whole run after its shards are joined.
- **Not compared.** A committed slice and a `--sample` verify do not score the kind: `S` and `A4` do not add up
  across blocks. Each names the property and compares nothing, as SAMPLE-SPEC.md section 4 names the kinds it does
  not estimate.
- **The score.** The similarity is `1 - |t - t'| / 2`, exact since both sides are rationals, rounded down to 12
  places as SCORECARD-SPEC.md section 2 rounds a correlation's. The outcome is within when
  `|t - t'| <= margin + w_noise + w_sampling`. The tolerance is `{ type: absolute, max }`, `0.05` by default.
  `w_noise` is zero for an exact reference; for a noisy one it is `sqrt(10 v)`, rounded up at 12 places, with
  `v = s^2` for a Gaussian and `2 b^2` for a Laplace: the noise exceeds it with probability below 0.002 for a
  Gaussian and about 0.011 for a Laplace. `w_sampling` is the same reach of the variance of the plain statistic
  over `n` rows of two independent columns, ties aside, `2 (2n + 5) / (9 n (n - 1))` for tau and `1 / (n - 1)` for
  rho, so that a small run is not held to a margin its own size cannot meet. When
  `margin + w_noise + w_sampling` is 2 or more, every run is within, and the row says the reference is too noisy
  to tell. SCORECARD-SPEC.md section 9b states it.
- **Shrinking** (decision 5) is not widened for: the run is scored against what the profile published, a large
  `alpha` shows as an outside score, and the row carries `alpha` for the reader.

### 8. Grades

`grade_profile` reads a `columnPairs` entry as noisy only when every statistic in it carries a `noise` block; a
`correlation/1` is always exact. An exact rank correlation keeps a run at P1, with a reason that names it; a
correctly charged noisy one leaves P3 open. A member of a pair the grade does not know counts as released without
noise, as every unknown member does.

### 9. A column has one joint

A number column in a clique (ADR 0036) is drawn by the junction tree, and its pairs are passed over for the copula.
The copula draws its columns as a block, independently of the categorical columns; a dependence between a number and
a category stays with `binWeightsGiven` or with a clique that holds both. A column drawn `given` any column, a
category or a bin (ADR 0036), is not a copula column. `init` keeps the Chow-Liu forest's choices first and gives the
copula the number columns that the forest draws given nothing.

### 10. Clique selection belongs to the profiler

FLUX consumes the cliques a profile publishes as they are: it chooses none, reweighs none and fits no model. A
profiler chooses them, by AIM or MST as private-pgm implements them, and the recommended route is OpenDP's
`make_contingency_table`, MIT, with its `opendp[mbi]` extra. What profile format 1-4-0 asks the profile to state is
stated in the note for profilers:

- **`cliqueSelection`**, per table: the `method` (`aim`, `mst`, `fixed` or `other`), the `producer` (a URI that
  names the tool and its version), the share spent choosing the cliques (`selection`) and the share spent releasing
  levels or bin edges that were not public (`domain`). Both are parts of the budget. ADR 0019 left the loss of
  choosing cliques as unstated slack; a profile of format 1-4-0 states it.
- **Measured, per table, in this release.** The cliques of a table are all measurements, each with its own `noise`
  block and share as today. A later format may add cliques estimated from listed `measurements`, such as AIM's model
  projected onto the maximal cliques of its junction tree, each an estimate with a variance `v` per cell that the
  profiler vouches for, the condition PROFILE-SPEC.md section 5a states for a noisy clique. This release does not read
  them: `estimate` and `measurements`, and `published: estimated`, are refused with `FLX-5036`, which says that
  estimated cliques are not read in this format. An estimate's variance must then be a function of the measurement
  design and the noise scales only, and is part of the structure a profile declares public.
- **Limits** the reader enforces: at most 1,024 cliques in a table; at most 1,000,000 cells in a clique, as today;
  and, new, at most 32 columns in a clique and 10,000,000 cells over a table's cliques, the order of AIM's default
  model size of 80 MB; and, as today, cliques that agree. A set of cliques with no junction tree is refused at
  `init`.
- **The noise sampler** is the profiler's: the reader checks a share against a scale and cannot check how the noise
  was drawn. The note for profilers requires a float-safe sampler (I. Mironov, "On significance of the least
  significant bits for differential privacy", ACM CCS 2012).

The engine checks the arithmetic and the structure. It cannot check that the method ran as stated, and the
evidence says so.

## Consequences

- A P1 profile keeps the dependence between number columns that it publishes, and a P3 profile can too, at the price
  of one share per pair. Dependence between a number column and a category still needs a clique or a grouping.
- The repair is deterministic and cheap, and gives up accuracy for it: in the prototype, shrinking moved entries 1.5
  to 2 times as far as the nearest correlation matrix would have. A profiler that publishes a valid matrix, which
  exact statistics always are and noisy ones mostly are when their noise is small, is not shrunk at all.
- The conversions are exact for continuous margins only. A column with few distinct values reproduces its rank
  correlations within about 5 to 10 percent in the prototype, and is better served by a clique (ADR 0036).
- A Gaussian copula has no tail dependence, so extremes of two columns are reproduced less often together than a
  heavy-tailed source may show them.
- The per-row path is slow for a copula: about two minutes per million rows of eight columns in the prototype,
  against six seconds on the column path. The column path is kernel `0.2.0`'s default.
- The budget statement gains two parts that were silent before (selection and domain), which makes the stated total
  easier to read and no larger.

## Alternatives considered

- **Draw from `correlation/1`.** Rejected: it depends on the margins, it is refused over dates, and it has no
  sensitivity bound for noise.
- **Higham's nearest correlation matrix.** Closer to the published matrix, but it needs an eigendecomposition in
  every iteration. LAPACK's bits differ between builds, and an eigensolver pinned in decimal arithmetic would cost
  minutes at 64 columns. Eigenvalue clipping, as DPCopula does, has the same objection.
- **Draw the place inside a bin as a histogram does.** Rejected for the copula: it loses 1 to 25 percent of the
  correlation, most where the bins are few.
- **Let `init` write the latent matrix.** Rejected: the bundle would then hide the numbers the profile published,
  and a person who writes a copula by hand thinks in rank correlations.
- **A t copula or a vine copula.** Rejected for now: more parameters per pair, and no receipt for releasing them
  under a budget. The Gaussian copula needs one number per pair, which a profile can publish.
- **The Pearson correlation of mid-ranks.** Rejected: one row can move it by more than one, whatever the size of
  the data.
- **Noise on `S` and `n` apart.** Rejected: the sensitivity of `S` grows with `n`, so its noise would have to.

## Questions settled

1. **Which measure the profiler computes.** Spearman's form is a window ranking and a sum in SQL, where the data
   lives; Kendall's needs an `O(n log n)` pass outside SQL, and its sensitivity is less than half as large. Both are
   specified; the profiler may publish either or both, and a copula takes Kendall's when a profile holds both.
2. **The default tolerance** of a `rankCorrelation` property is `0.05`, with the small-run allowance of decision 7.
3. **Shrinking or a pinned nearest correlation matrix.** Shrinking, for cost and reproducibility.
4. **Ties.** No correction of a published tau for the ties its margins imply: tie-heavy columns are better served by
   a clique, and `init` says so in the ledger.
5. **A copula with categories.** A copula per level of a category, or a copula that holds categorical columns
   through thresholds, is left for later.
6. **Estimated cliques.** Measured cliques only in this release (MST and the `fixed` method), with AIM's estimates
   to follow in a later format.
7. **The parameter register.** It stays at format 1-3-0: `fidelity.copula` joins the declarations whose numbers it
   does not count, beside `counts` and `conditions`, and a citation may not name the new definitions. Counting the
   copula's values, with the rank correlation each rests on, would be register format 1-4-0.

   Amended 2026-11-24: register format 1-4-0 counts the copula's values and the namespace keys' numbers, and a citation may name the rank correlation and clique marginal definitions. A record is format 1-4-0 only when it counts a number at one of those positions or cites one of those definitions, so every other record keeps its format, bytes and digests.

## What would change this decision

- A pinned eigensolver fast enough for 64 columns: the repair would become the nearest correlation matrix.
- A tighter bound for Spearman's form: its charge would fall towards `6 / (f + 2)`.
- Profiles that publish dependence between numbers and categories together: the copula would grow categorical
  margins, or give way to mixed cliques for those columns.
