# Correlations: profile format 1-4-0, request format 1-2-0 and scorecard format 1-5-0 (draft)

**Draft for design review, 2026-11-23. Nothing on this page is implemented.** The engine reads profile formats
`1-0-0` to `1-3-0` and request formats `1-0-0` and `1-1-0`, and writes scorecards up to `1-4-0`. This page drafts the
format changes of [ADR 0035](../../adr/0035-number-columns-are-drawn-through-a-gaussian-copula-of-published-rank-correlations.md)
(a Gaussian copula of rank correlations, and how a profile states its clique selection),
[ADR 0036](../../adr/0036-binned-number-columns-join-clique-marginals.md) (binned number columns in cliques) and
[ADR 0037](../../adr/0037-child-columns-and-child-counts-follow-their-parent-and-a-unit-can-follow-its-children.md)
(relational marginals, and a unit of privacy that follows its child rows). When the work is built, each part moves
into the specification its heading names, and this page is removed. The profiler's side of clique selection is in
[clique-selection-for-profilers.md](./clique-selection-for-profilers.md). The parameter register keeps format `1-3-0`:
`fidelity.copula` joins the declarations whose numbers it does not count (PARAMETER-REGISTER-SPEC.md section 2),
beside `counts` and `conditions`, and a citation may not name the new definitions. Counting the copula's values would
be register format `1-4-0`, a question ADR 0035 leaves for review.

## 1. Profile format 1-4-0 (PROFILE-SPEC.md)

### 1.1 What names the format

A document that holds any of the following names `1-4-0` in `_schemaURL`
(`https://agenticstiger.github.io/flux/engine/spec/profile/1-4-0/FluxProfile.json`). One that names an earlier format
and holds one is refused with `FLX-5036`. A document that holds none of them reads as it did and keeps its digest.

- a `kendallTau/1` or `spearmanRho/1` statistic in a `columnPairs` entry, and the `rowFloor` key (section 1.2);
- a `cliqueMarginal/2` clique (section 1.3);
- `groupByBins` on any statistic (section 1.4);
- `groupBy` or `groupByBins` on a relationship's statistic (section 1.5);
- `noise` on a relationship's statistic, on a statistic with `groupByLink`, or on a rank correlation (section 1.7);
- `follows` in `privacyUnit` (section 1.6);
- `cliqueSelection` in a table (section 1.8).

The schema `profile-1-4.schema.json` validates documents of formats `1-0-0` to `1-4-0`, and the earlier schemas are kept
as published.

### 1.2 Rank correlations

A `columnPairs` entry may hold, beside `correlation/1`, the two definitions below over its pair `[a, b]`. `a` and `b`
are two different `number` or `timestamp` columns of the table. Each value is floored onto the profile's grid before it
is ranked, so a tie is a tie on the grid. A record counts when both of its values are known; `n` is their number.

| Definition | Quantities besides `group`, `k` and `status` | The statistic |
|---|---|---|
| `kendallTau/1` | `S`: the sum over every pair of counted records of `sign(a_i - a_j) * sign(b_i - b_j)`, a tied pair adding 0 | `tau = S / C(N, 2)` |
| `spearmanRho/1` | `A4`: four times `sum (R_i - (n + 1) / 2) * (Q_i - (n + 1) / 2)`, with `R` and `Q` the mid-ranks of the counted records in `a` and in `b` | `rho = 3 * A4 / (N * (N^2 - 1))` |

- `k = n`, a count. `S` and `A4` are integers written as decimal strings, as a sum is.
- `N = max(n, f)`, where `f` is the statistic's `rowFloor` when it has one, and `n` when it has none.
- `rowFloor` is optional: a count of at least 2, chosen without the data (section 1.9). The statistic is formed with
  it. It does not state that `n` is at least `f`.
- A published group with `k` below 2 is refused (`FLX-5036`).
- A rank correlation is ungrouped in this format: its one group is keyed by the empty string. It names a disclosure
  rule, and the group size rule of section 3 applies to it as to `correlation/1`.

**The noisy form.** A rank correlation may carry a `noise` block (section 3a) when it has a `rowFloor`. Its group is
`{ "group": "", "status": "F", "noisyValue": v }`, a decimal string that may lie outside `[-1, 1]`, and it holds no
`S`, `A4` or `k`. The mechanism is `gaussian` or `laplace`. The reader's point estimate is `v` clamped to `[-1, 1]`,
with the variance of section 3a (`scale^2`, or `2 * scale^2` for a Laplace).

Refused with `FLX-5036`: a noisy rank correlation with no `rowFloor`, or under `discreteGaussian`; a noisy
`correlation/1`, as before.

### 1.3 `cliqueMarginal/2`

| Key | Holds |
|---|---|
| `definition` | `cliqueMarginal/2`. |
| `columns` | One or more distinct names of columns of the table, each `category`, `number` or `timestamp`. |
| `domains` | One object per column, in the order of `columns`: `{ "levels": [...] }` for a `category` column, distinct and sorted by code point, or `{ "bins": [...] }` for a `number` or `timestamp` column, at least two strictly increasing decimal strings. |
| `noisyCounts` | One noisy value per combination of levels and bins, row-major: the first column varies slowest and the last fastest. |
| `noise` | The mechanism, scale and share (section 3a), in a table whose cliques are measured. |
| `estimate` | `{ "variance": v }`, a decimal string above zero, in a table whose cliques are estimated (section 1.8), in place of `noise`. |

- Bins are read as `marginalHistogram/1`'s: a value is floored onto the grid, bin `i` is `[e_i, e_(i+1))`, and the last
  bin is closed above. The profiler chooses edges that cover the column's declared range; the reader cannot check
  that, and `fluxsim init` refuses a clique whose edges do not cover the contract's range (`FLX-3402`). A DATE
  column's edges are milliseconds, converted to days as section 7 converts a histogram's.
- A record counts when every column of the clique is known.
- `estimate` appears only in a table whose `cliqueSelection` is `estimated`, and `noise` in every other table.
- Cliques of `cliqueMarginal/1` and `cliqueMarginal/2` in one table agree as section 5a says: a column they share has
  the same levels, or the same edges, in each, and their noisy sums over the shared columns are equal exactly. Two
  cliques over one set of columns are refused.
- The reader forms the one-way and two-way marginals of section 5a, now with bins: a number column's one-way marginal
  is a `marginalHistogram/1` over the clique's bins; a number and a category give a `marginalHistogram/1` of the number
  grouped by the category; two numbers give a `marginalHistogram/1` of the first in the clique's order grouped by the
  bins of the second (section 1.4). Each is marked derived, with the variance of the cells it sums: `v` times their
  number, where `v` is the noise's variance or the estimate's.

### 1.4 `groupByBins`

A column statistic may hold `"groupByBins": { "column": name, "bins": [...] }`: `column` a `number` or `timestamp`
column of the same table other than the statistic's own, and `bins` at least two strictly increasing decimal strings,
read as a histogram's. Its groups are keyed by the canonical decimal text of each bin's lower edge (no exponent, no
leading zeros, no trailing zeros after the point, `0` for zero). The records whose grouping value is NULL are one more
group, never written. A statistic holds at most one of `groupBy`, `groupByLink`, `groupByBins` and `per`. The
disclosure rules apply group by group, as for `groupBy`. A `levelHistogram/1` or `marginalHistogram/1` with
`groupByBins` may carry `noise`.

### 1.5 Relationship statistics

A statistic of a `relationships` entry (the number of children per parent row) may hold `groupBy`, naming a `category`
column of the parent table, or `groupByBins`, naming a `number` or `timestamp` column of it. Its groups follow
section 1.4 and the rules of a grouped column statistic; the parents whose value is NULL are one more group, never
written. A relationship's `marginalHistogram/1`, grouped or not, may carry `noise` under section 1.7; its other
statistics may not. On a relationship that `follows` lists, a level or a bin whose least count is above `maxChildren`
is refused, whatever its count, and a relationship that `follows` names only to be followed may hold an empty
`statistics` list.

### 1.6 The unit follows its child rows

`privacyUnit` is `{ "expose": id }` or, in this format, `{ "expose": id, "follows": [...] }`. Without `follows` the
unit is one row of `expose`, as in every earlier format, and a profile's charges and bytes are what they were. Each
entry of `follows` is `{ "parent": { "table", "columns" }, "child": { "table", "columns" }, "maxChildren": c }`:

- it names exactly one entry of the profile's `relationships`, by its parent and child tables and columns, and no
  entry is named twice;
- `c` is a count of at least 1, a fixed public cap chosen without the data.

The listed relationships form a tree rooted at the expose. Each table they reach, other than the expose, is the child
of exactly one listed relationship; the parent of every listed relationship is the expose or a table they reach; no
table is listed as a child twice; they form no cycle; and the expose is never a child. Anything else is refused
(`FLX-5036`), naming the entry. A table reached by two paths, or a child whose parent the unit does not reach, would
make the stated reach understate the sensitivity: two followed keys of cap 1 into one table state a reach of 2, yet
one parent row can bring rows in through both, and the rows under a parent that is no part of the unit move with that
parent.

One unit of privacy is one row of `expose` together with, recursively, the rows of child tables that reference a row
of the unit through a listed relationship. Neighbouring data sets differ by one unit (OpenDP's symmetric distance at
distance one, over units).

**What the profiler does.** Before it measures anything, the profiler keeps at most `c` children of each parent row on
each listed relationship. It drops the others by a rule that reads only that parent's own child rows, such as a fixed
order of their values or a seeded draw, and a dropped row takes with it the rows that follow it. It also drops the rows
of a followed table that follow no row of the unit's expose. Every statistic of the document is measured on what
remains.

**Reach.** `m_T` is the number of rows of table `T` that one unit contributes: `m_expose = 1`, and for a reached table
`T`, `m_T = c * m_P` for its one listed relationship (`P` to `T`, cap `c`), that is the product of the caps along the
one path from the expose to `T`. A table that no listed relationship reaches has `m_T = 0`. The numbers are exact
integers.

**What the guarantee covers.** Units. The rows of a table with `m_T = 0` are the same in every neighbouring data set,
so a statistic grouped by such a table's column, or a count of its rows' children, protects the unit's rows and not
that table's. The account names the tables the unit reaches and the tables it treats as fixed. For the minimum grade
of a column (ADR 0020), any labelled column (one that carries a sensitivity label such as `pii` or `restricted`) of a
table with `m_T = 0` counts as `P1`, whether or not the statistic that touches it is noisy.

### 1.7 What a noisy statistic is charged

Section 3a's paragraph "Every count table has sensitivity 1" becomes: a noisy statistic over the rows of table `T` has
the sensitivity of the table below times `m_T`.

| Statistic | L1 for one row of `T` | L2 for one row of `T` |
|---|---|---|
| a count table: `levelHistogram/1`, `marginalHistogram/1`, either grouped by `groupBy`, `groupByBins` or `groupByLink`, a calendar or date pair histogram, a clique | 1 | 1 |
| `kendallTau/1` with `rowFloor` `f` | `4 / (f + 1)` | `4 / (f + 1)` |
| `spearmanRho/1` with `rowFloor` `f` | `9 / (f + 1)` | `9 / (f + 1)` |

Two forms read a row of another table and have their own rules:

- **A statistic with `groupByLink`**, of table `T` grouped by a column of parent table `P`: it may carry noise only when
  `m_P = 0`, or when its relationship is listed in `follows`. It is charged `m_T` as a count table. Otherwise its noise
  is refused.
- **A relationship's `marginalHistogram/1`**, from parent table `P` to child table `C`. Let `m_C'` be `m_C` less the
  contribution of this relationship to it (`c * m_P` when it is listed, 0 when it is not).
  - `m_P >= 1` and `m_C' = 0`: L1 and L2 are `m_P`.
  - `m_P = 0` and `m_C >= 1`: L1 is `2 * m_C` and L2 is `sqrt(2) * m_C`, so the squared L2 is `2 * m_C^2`.
  - Otherwise its noise is refused.

  A `groupBy` or `groupByBins` naming a column of `P` leaves the sensitivity as it is.

**The arithmetic** is section 3a's with the sensitivity in place of 1: a Gaussian or discrete Gaussian of scale `s`
spends `rho = L2^2 / (2 s^2)` under zero-concentrated DP, and a Laplace of scale `b` spends `epsilon = L1 / b` under
approximate DP. A share below that is refused (`FLX-5036`), and one above it is the profiler's slack. A Laplace under
zero-concentrated DP, and a Gaussian under approximate DP, are not recomputed, as before. `discreteGaussian` applies to
count tables only.

**Refused with `FLX-5036`**, naming where in the document and never a value: a noisy statistic over a table with
`m_T = 0`; a `groupByLink` or relationship statistic that the rules above refuse; noise on a `mean/1`, a quantile, a
`support/1`, a `per` statistic, a `history` member, a relationship's `dates` or `exposure`; and every refusal of section
1.2.

### 1.8 `cliqueSelection`

A table may hold `cliqueSelection`, which says how its cliques were chosen and released:

| Key | Holds |
|---|---|
| `method` | `aim`, `mst`, `fixed` (the cliques were named before the data was read) or `other`. |
| `producer` | A URI naming the tool that chose and measured the cliques, with its version. |
| `selection` | Optional: `{ "privacyLoss": ... }`, the share spent choosing the cliques. Absent for `fixed`. |
| `domain` | Optional: `{ "privacyLoss": ... }`, the share spent releasing levels or bin edges that were not public. |
| `published` | `measured` or `estimated`. |
| `measurements` | With `estimated` only, at least one: `{ "columns": [...], "noise": {...} }` for each marginal that was measured, `columns` distinct names of the table's columns. |

- **Measured.** Every clique of the table carries its `noise` block and share, as section 5a says.
- **Estimated.** Every clique of the table is a `cliqueMarginal/2` with an `estimate` and no `noise`: post-processing
  of the measurements, such as a model fitted to them and projected onto the maximal cliques of its junction tree. The
  measurements' `noise` blocks are the shares. The columns of each measurement lie inside one published clique. An
  estimate's `variance` is the `v` of section 5a's condition: the profiler states that the errors of the clique's
  cells have a covariance of at most `v` times the identity.
- An `estimate` in a table with no `cliqueSelection`, or with a measured one, and a `noise` block on a clique of an
  estimated table, are refused.
- **The budget.** The parts of section 3a are every `privacyLoss` of the document: the `noise` blocks of statistics,
  cliques and measurements, and `selection` and `domain`. Each is in the measure of the total, and the sums are exact.
- **Limits.** A clique holds at most 1,000,000 cells, as before. New in this format: a table holds at most 1,024
  cliques and 4,096 measurements, a clique at most 32 columns, and a table's cliques at most 10,000,000 cells
  together. A document beyond one is refused whole.
- **What the reader does not check.** That the method ran, that the selection's share is what it spent, and that the
  estimate's variance bounds its error. Each is the profiler's statement, as a noise scale is, and the evidence says
  so.

### 1.9 The declaration about structure

`publicStructure: true` declares, in this format, that these were chosen without the data or are part of the
release: the levels and bin edges of the profile's histograms and cliques, the bins of every `groupByBins`, the group
keys of every grouped noisy statistic, the `sampleSize` bands, every `rowFloor`, and every `maxChildren`. A profile
with `domain` in a `cliqueSelection` has released some of them under its account, which the declaration allows.

The group keys are the levels of a `groupBy` column, the lower edges of `groupByBins`, and the values of the parent
column of a `groupByLink`: a noisy statistic publishes every group it names, and the names themselves come from the
declaration, not from a count. The `sampleSize` bands of the new statistics are covered in the same way: a band is a
number of records stated beside the noisy values, so it is chosen without the data or is part of the release.

When estimated cliques are read, in a later format, an estimate's `variance` must be a function of the measurement
design and the noise scales only, and it belongs to this list: a variance computed from the data would be a number
released outside the account.

### 1.10 P3

Section 3a's last paragraph becomes: a run is graded P3 only when the profile has the account and the declaration, and
every statistic and metric it holds that the engine can read is noisy under that account. A relationship's
`marginalHistogram/1`, a statistic with `groupByLink`, a rank correlation and a clique of either definition can now be
noisy; a `rowCount`, a column metric, `correlation/1`, a `periods` statistic, a `queries` or `invariants` section,
`references`, a relationship's `mean/1`, quantiles, `dates` or `exposure`, a `per` statistic and a `history` still
cannot.

## 2. Profile request format 1-2-0 (PROFILE-REQUEST-SPEC.md)

A request that uses any addition below names
`https://agenticstiger.github.io/flux/engine/spec/profile-request/1-2-0/FluxProfileRequest.json`; one that names an
earlier format and holds one is refused with `FLX-5038`, naming the addition. The request still holds names and
registered identifiers only, and no budget: how much noise to add, and where, is the data owner's choice, stated in the
profile.

- `tables.<exposeId>.rankCorrelations`: a list of `{ "definition", "columns" }`. `definition` is `kendallTau/1` or
  `spearmanRho/1`, each at most once per table; `columns` names 2 to 64 distinct `number` or `timestamp` columns of the
  table. The profile states the statistic over every pair of them.
- `tables.<exposeId>.cliqueColumns`: a list of the columns a profiler may join into cliques, `category`, `number` or
  `timestamp`, each named once, at most 1,024. The profiler chooses the cliques, their method and the bins of a number
  column, and states them (section 1.8).
- `relationships[].groupBy`: a list of columns of the relationship's parent table, each named once, at most 64. The
  profile states the relationship's statistics grouped by each (section 1.5), by its levels for a `category` column
  and by bins the profiler chooses for a `number` or `timestamp` one.
- `relationships[].linked`: a list of `{ "column", "parentColumn" }`, a column of the child table and one of the
  parent table, each pair named once, at most 1,024. The profile states the child column's statistics grouped by the
  parent column through this relationship (`groupByLink`).

`fluxsim init` writes these asks for the columns it would draw from them: number columns it draws from histograms, the
columns a clique could hold, and the parent columns of relationships it lays out on the counted layout.

## 3. The bundle (SEAM-FIDELITY-SPEC.md section 2 and the `counts` key)

**Keys that change the rows.** Each is read under kernel `0.2.0` only; under kernel `0.1.0` it is refused with
`FLX-3105`, naming the kernel. Each has an end-to-end `ship` test.

- **`fidelity.copula`** (ADR 0035 decision 4): `columns`, 2 to 64 distinct `number` or `timestamp` columns of the emit
  drawn from `bins` and `binWeights`, with no `given` and no `allocation`, and set by nothing else (not a key, and not
  set by a rule, a `lifetimes`, `within` or `rates` entry, a history, a hidden declaration or a copy);
  `rankCorrelations`, holding `measure` (`kendall` or `spearman`) and `pairs`, a list of
  `{ "columns": [a, b], "value" }`, each pair of listed columns at most once, `value` a decimal in `[-1, 1]`. A pair not
  listed is 0. A bin of positive weight with no grid value is refused. Anything else is `FLX-3402`. It lives in the
  fidelity block, so, like `given`, it enters both `H` and `G` (SEAM-FIDELITY-SPEC.md section 3), with no change to
  `strip_excluded_from_fidelity_block`.
- **A binned parent** (ADR 0036 decision 3): `given`, alone or in a list, may name a column drawn from `bins` with no
  `nullFraction`, not a key and not set by a rule. Its classes are its bins, keyed in `levelWeightsGiven` and
  `binWeightsGiven` by the text of each lower edge as its `bins` write it, and every bin has an entry. A column with
  `allocation: exact` given a binned parent is refused. Anything else is `FLX-3402`. It enters `H` and `G` as every
  `given` does.
- **`counts[].given` and `counts[].childrenGiven`** (ADR 0037 decision 4): `given` names a `category` column of the
  parent expose with declared levels and no `nullFraction`, or a binned column; `childrenGiven` maps classes of it to
  weight lists over the classes `children` declares, and a class with no list takes `children`'s weights, which the
  plan states. `children` is required with them. Refused with `FLX-3403`: a key that is not a class of the given
  column, a list of the wrong length or with no weight, and, as for `parentWhen`, a given column that reads a link, a
  previous version, children, siblings or a column a summary over the parent's children sets. The emit's `extension`
  member carries the entry for `H`, and `G` leaves it out, as it leaves `counts` out.

**Property kinds**, which change no row, are read under both kernels, and their tolerances stay out of `G` as every
tolerance does:

- `rankCorrelation`, with `columns: [a, b]`, `measure` and a tolerance `{ type: absolute, max }` (default `"0.05"`), in
  the `joint` dimension, scored from a profile only (section 5);
- a grouped property may hold `groupByBins`, with the profile's spelling;
- a relationship whose `counts` entry has `given` is scored per class of its parent column (section 5).

## 4. Kernel `0.2.0` (KERNEL-SPEC-2.md)

### 4.1 The one new path

| Path | Kind | Draws |
|---|---|---|
| `("sim", sim_id, "gen", exposeId, "copula", row)` | stream, one ordinal (the row) | `0 .. q - 1`: one word each, the copula's `q` columns in the order of `columns` |

It is the only new literal of these three records. Binned parents draw on each column's own cell path at the indices
every histogram and categorical column draws at, and children given a parent column use the key of the `count` path
and its permutation; neither adds a path.

### 4.2 The copula at plan time, in `decimal` at 50 digits, rounding half to even

1. `r = sin(pi * tau / 2)` for `kendall`, `r = 2 * sin(pi * rho / 6)` for `spearman`. π is `rng._pi` at 60 digits;
   the sine is its Taylor series summed at 60 digits until a term's magnitude is below `10^-55`, then rounded to 50
   digits. The argument lies in `[-pi/2, pi/2]`, so no reduction is needed.
2. `R`: unit diagonal, `r` off it, in the order of `columns`.
3. `R` passes when every pivot of its LDLᵀ factorisation, without pivoting, is at least `10^-6`.
4. `alpha = 0` when `R` passes. Otherwise 40 steps of bisection on `[0, 1]` for `R(alpha) = (1 - alpha) R + alpha I`:
   `lo = 0`, `hi = 1`; the midpoint becomes `hi` when `R(mid)` passes and `lo` otherwise; `alpha = hi`. Each pivot of
   `R(alpha)` is concave in `alpha` and 1 at `alpha = 1`, so the values that pass form an interval that ends at 1, and
   the bisection finds its start.
5. `L`, the Cholesky factor of `R(alpha)` (the unit factor times the square root of each pivot), each entry rounded
   once to binary64; an entry whose magnitude is below `2^-900` is taken as zero, so no product of section 4.3 is
   subnormal.

The emit's plan document gains `copula: { "alpha", "factor" }`, `factor` the sha256 of `L`'s binary64 words in
row-major order, only when the emit declares a copula, so it enters `planDigest` there and nowhere else.

### 4.3 The copula's draw, in binary64 (section 5.1's arithmetic)

```text
u_d = (2 * (w0 >> 12) + 1) * 2^-53                    w0 of the block of draw d at attempt 0
z_d = Φ⁻¹(u_d)                                        section 5.2
x_i = ((L[i][0] * z_0 + L[i][1] * z_1) + ...) + L[i][i] * z_i
```

For column `i`, with bins `b = 1 .. B`, shares `p_b` and cumulative shares `c_b = p_1 + ... + p_b` (`c_0 = 0`), all
exact, and `w_b` grid values in bin `b`, numbered `0 .. w_b - 1` in the order the histogram's own draw counts them:
`F(b, j) = c_(b-1) + p_b * j / w_b`, an exact fraction; `binary64(F)` rounds the fraction once to the nearest
binary64, ties to even; `t(F) = Φ⁻¹(binary64(F))` for `F <= 1/2` and `-Φ⁻¹(binary64(1 - F))` for `F > 1/2`, with a
binary64 argument below `2^-1022` giving `-∞` or `+∞`. With `x = x_i`:

```text
lo = 0, hi = B
while hi - lo > 1:  mid = (lo + hi) // 2;  if t(c_mid) <= x: lo = mid  else: hi = mid
b = lo + 1                                            the bin, of 1 .. B
lo = 0, hi = w_b
while hi - lo > 1:  mid = (lo + hi) // 2;  if t(F(b, mid)) <= x: lo = mid  else: hi = mid
grid value lo of bin b                                 the value, of 0 .. w_b - 1
```

A copula column's cell path draws only its NULL draw, at index 0, when it declares a `nullFraction`.

### 4.4 Children given a parent column (section 8.1)

With `given`, `E` is split into classes `E_l` by the given column's value on each parent's final row (its bin, for a
binned column), keeping `E`'s order. Class `l` has `n_(l,k) = LRM(|E_l|, weights_l)` parents in count class `k` and the
slots `v_(l,i)` of section 8.1. `sigma = permute/2(K, |E|, r)` as before; the parents of `E_l` in ascending order of
`sigma` take `v_(l,0), v_(l,1), ...` as their expected counts. With a hook, the rank coupling applies within each class.
The minimum, the allotment and the rows are unchanged.

### 4.5 Vectors and section 12

`tests/vectors/kernel2-copula-vectors.json` holds the copula path's key label, key, counters and blocks, and `u`, `z`,
`x`, the thresholds, the bins and the values of small copulas: one whose matrix passes, one that is shrunk, and one
with a bin of weight zero. The counted layout's vectors gain layouts with given classes, and the streams vectors a
column drawn given a binned parent. Section 12 gains the copula path (condition 1: only `fidelity.copula` reads it;
condition 2: the literal `copula` at the place after `exposeId`; condition 3: nothing that exists moves; condition 4:
the vector file), and names binned parents and grouped children among the declarations that add no path.

## 5. Scorecard format 1-5-0 (SCORECARD-SPEC.md)

A scorecard names `1-5-0` when it holds a `rankCorrelation` row, a row with `groupByBins`, or a children-per-parent row
grouped by a parent column. Otherwise it names the format it named before and keeps its bytes. The schema is
`scorecard-1-5.schema.json`.

- **`rankCorrelation`** (`joint`): the run's statistic is the plain one over the run's rows, with no floor, and the
  reference is the profile's (exact, or the clamped noisy estimate). Both are formed on the profile's grid, so the
  block's `fractionalBits` must equal the profile's (PROFILE-SPEC.md section 7); a pair is read in either order; a
  DATE column needs no conversion. Forming the run's side sorts the pair's values over the whole run, `O(n log n)`.
  The similarity is `1 - |t - t'| / 2`, an exact rational, rounded down to 12 places as section 2 rounds a
  correlation's. The outcome is within when `|t - t'| <= margin + w`. For a noisy reference, `w = sqrt(10 v)`, rounded
  up to 12 places, with `v` the reference's variance (a new section 9b: for a scalar with Gaussian noise,
  `P(|eta| > sqrt(10 v)) < 0.002`; with Laplace noise, about 0.011). When `margin + w` is 2 or more, the row is marked
  as one whose reference is too noisy to tell. The row carries the copula's `alpha` when the run's plan has one. A
  committed slice and a `--sample` verify name the property and compare nothing, since `S` and `A4` do not add up
  across blocks (SAMPLE-SPEC.md section 4 gains the kind).
- **`groupByBins`** rows read as `groupBy` rows, with the groups keyed by lower edges and named by ordinal.
- **Children per parent, grouped** (`crossTable`): the run's parents of each group of the parent column are counted
  into the profile's bins by their number of children, as section 3's ungrouped row counts them, and each group is
  compared with the profile's group by total variation distance; groups are named by ordinal, the tolerance is the
  `counts` entry's, and a noisy reference is widened by section 9's `w` group by group.
- **A noisy reference grouped by a link** is widened by section 9's `w` group by group, as a noisy grouped histogram
  is.

## 6. Grades (`grade_profile`, ADR 0020)

The grade reads each member by its rule, and a member it does not know counts as released without noise.

| Member | Noisy when | Otherwise the reason names |
|---|---|---|
| a `columnPairs` entry | every statistic in it carries `noise` (a `correlation/1` never does) | the pair |
| a clique, either definition | always: it carries `noise`, or `estimate` under an estimated `cliqueSelection` | (none) |
| `cliqueSelection` | it is accounting: its parts enter the account and it holds no statistic | (none) |
| a statistic with `groupByBins` or `groupByLink` | it carries `noise` | the statistic |
| a relationship | every statistic in it carries `noise` (so it holds no `mean/1` and no quantile), and it holds no `dates` and no `exposure` | the relationship |
| `privacyUnit.follows` | it is part of the account: `EVIDENCE.md` states the unit with what it follows, and names the tables the unit does not reach, which the guarantee treats as fixed | (none) |

`relationships` leaves the list of sections that cannot be noisy, which keeps `queries`, `invariants` and
`references`. The account the evidence shows gains the parts of `selection`, `domain` and `measurements`, and the unit's
`follows`.

## 7. Error codes

No code is added. The profile reader refuses with `FLX-5036`, a property the profile cannot answer is `FLX-5037`, a
request is refused with `FLX-5038`, a declaration of the fidelity block with `FLX-3402`, a `counts` entry with
`FLX-3403`, and a key under kernel `0.1.0` with `FLX-3105`. Each message names where the problem is and never quotes
a value.
