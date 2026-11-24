# ADR 0036: binned number columns join clique marginals beside categorical ones, and a column may be drawn given the bin of another

## Status

Accepted 2026-11-23. Amends decision 5 of
[ADR 0021](./0021-clique-marginals-are-drawn-through-a-junction-tree.md), whose cliques hold categorical columns
only, and the rule of SEAM-FIDELITY-SPEC.md section 2 that a histogram column is always a leaf because a bin is not
a level: a declaration may now name a bin as a parent. The Chow-Liu forest of
[ADR 0014](./0014-init-reads-every-statistic-a-profile-publishes.md) section 4 keeps taking a histogram column as a
leaf. Builds on [ADR 0019](./0019-one-privacy-budget-per-profile-release.md),
[ADR 0020](./0020-privacy-grades.md) and the `conditions` of
[ADR 0028](./0028-consumer-queries-run-locked-compile-requirements-and-calibrate.md) (section 10). A companion of
[ADR 0035](./0035-number-columns-are-drawn-through-a-gaussian-copula-of-published-rank-correlations.md), which also
states how a profile says its cliques were chosen, and of
[ADR 0037](./0037-child-columns-and-child-counts-follow-their-parent-and-a-unit-can-follow-its-children.md). The
format text is in [PROFILE-SPEC.md](../spec/PROFILE-SPEC.md) (`cliqueMarginal/2`, `groupByBins`),
[SEAM-FIDELITY-SPEC.md](../spec/SEAM-FIDELITY-SPEC.md) (a binned parent in `given`),
[CONSUMER-QUERIES-SPEC.md](../spec/CONSUMER-QUERIES-SPEC.md) section 10 (`conditions` over bins) and
[SCORECARD-SPEC.md](../spec/SCORECARD-SPEC.md) (`groupByBins` rows).

**What this release builds.** `cliqueMarginal/2` and the derived marginals with bins (decision 1), a column drawn
given the bin of another (decision 3), `conditions` over bins (decision 5), `groupByBins` (decision 7), and the
`verify` rows and grades for them (decisions 8 and 9). **What it leaves for later:** a NULL class in a clique
(decision 6), binned parents in the Chow-Liu forest (decision 4), and cliques estimated from measurements, which
this release does not read.

## Context

ADR 0021 draws the cliques of a profile released under differential privacy through a junction tree, and its scope is
categorical columns: a key, a column a rule sets and a column with a declared null fraction are not drawn from a
clique, and PROFILE-SPEC.md section 5a says that a number column is released as a noisy `marginalHistogram/1` of its
own. So in a P3 profile every number column is drawn independently of every other column. In an exact profile a
number column can at least be drawn given one categorical column (`binWeightsGiven`), but never given another number
column, because the forest of ADR 0014 takes a histogram column only as a leaf.

A histogram column already draws a bin and then a place in it: a categorical draw over its bins, then a bounded draw
over the bin's grid values (KERNEL-SPEC-2 section 2). A bin is therefore a level the generator draws anyway. What the
junction tree lacks is a clique that lists bins for a column, and a parent whose levels are bins.

**Borrow-before-build (2026-11-23).** Inside the repository: `flux_engine/calibrate/junction.py`,
`flux_engine/author/cliques.py` and `flux_engine/generator/conditions.py` take any column whose levels are a finite
list, and the histogram draw is a categorical draw over bins. Searches: how marginal-based private synthesizers treat
continuous columns; binning in private-pgm, OpenDP and smartnoise. What was read:

- private-pgm (github.com/ryan112358/private-pgm, Apache-2.0, head `bad442f` of 2026-11-18): AIM and MST
  (`mechanisms/aim.py`, `mechanisms/mst.py`) need a discrete domain and ship no binning; MST's `compress_domain`
  merges values whose noisy count is below three noise scales into one bucket.
- OpenDP 0.16.0 (MIT): `make_contingency_table` takes `cuts`, bin edges for a number column, and `keys`, the levels
  of a categorical one; a column given neither has its levels released from a noisy, thresholded one-way marginal,
  which spends part of the budget (by default up to half of it, spread over such columns).
- smartnoise-synth 1.0.8 (MIT): a `BinTransformer` with equal-width bins, ten by default, inferring bounds from the
  data only when it is given budget for it.
- J. Zhang, G. Cormode, C. M. Procopiuc, D. Srivastava and X. Xiao, "PrivBayes: Private Data Release via Bayesian
  Networks", ACM Transactions on Database Systems 42(4), 2017 (its bibliographic page): its network is built over
  discrete attributes. Its discretisation rule was not confirmed for this record.

The pattern they share: a number column joins a marginal as the bin its value falls in, with edges that are public or
paid for out of the budget, and the place inside the bin is not modelled. That is what FLUX adopts. No code is used;
FLUX reads the counts a profiler releases.

## Decision

### 1. `cliqueMarginal/2` (profile format 1-4-0)

A new registered definition of a clique, in `tables.<exposeId>.cliques` beside `cliqueMarginal/1`:

| Key | Holds |
|---|---|
| `definition` | `cliqueMarginal/2`. |
| `columns` | One or more distinct names of columns of the table. |
| `domains` | One per column, in the order of `columns`: `{ "levels": [...] }` for a categorical column, distinct and sorted by code point, or `{ "bins": [...] }` for a number or timestamp column, at least two strictly increasing decimal edges. |
| `noisyCounts` | One noisy value per combination of levels and bins, row-major, as `cliqueMarginal/1`. |
| `noise` | The mechanism, scale and share (PROFILE-SPEC.md section 3a). A clique estimated from measurements, with a variance in place of `noise`, is not read in this release: it is refused with `FLX-5036` (ADR 0035 decision 10). |

- **Bins** are read as `marginalHistogram/1` reads them: a value is floored onto the profile's grid, bin `i` is
  `[e_i, e_(i+1))`, and the last bin is closed above. The profiler chooses edges that cover the column's declared
  range, so every known value lies in a bin. The profile reader cannot check that, since the range is the contract's;
  `init`, which reads the contract, refuses a clique whose edges do not cover it (`FLX-3402`). The edges of a DATE
  column are milliseconds, converted to days as PROFILE-SPEC.md section 7 converts a histogram's.
- **A record counts** when every column of the clique is known. A column with a null fraction is still not drawn
  from a clique (decision 6).
- **One column** is allowed in `cliqueMarginal/2`: it is that column's marginal, which a profiler may release for a
  column its other cliques leave alone. `cliqueMarginal/1` keeps its two-column minimum.
- **Agreement** is ADR 0021's and PROFILE-SPEC.md section 5a's: cliques of either definition that share a column list
  the same levels or the same edges for it, and agree exactly on their noisy sums over the shared columns. Two cliques
  over one set of columns are refused.
- **Noise** is a count table's, unchanged: a record lands in one cell.

`cliqueMarginal/1` keeps its meaning, and a profile of format 1-3-0 or earlier reads as it did.

### 2. The junction tree takes bins

A binned column is a node of the junction tree whose levels are its bins, and every rule of ADR 0021 holds for it: the
tree, the root and the order, each column drawn given its separator and the columns its clique introduced before it,
and the one rule that makes a vector of noisy counts a distribution. A binned column's conditional is a vector of bin
weights. A column drawn given a separator that holds a binned column reads that column's bin.

### 3. The declaration: a column given a bin

`given` may name a column drawn from `bins`: a number or timestamp column of the emit with no `nullFraction`, not a
key and not set by a rule. Its classes are its bins. In `levelWeightsGiven` and `binWeightsGiven` they are keyed by
the text of each bin's lower edge as the parent's `bins` write it, and every bin has an entry:

```yaml
columns:
  balance:
    bins: ["0", "1000", "5000", "50000"]
    binWeights: ["0.55", "0.35", "0.10"]
  plan:
    given: balance
    levelWeightsGiven:
      "0":    { basic: "0.9", premium: "0.1" }
      "1000": { basic: "0.6", premium: "0.4" }
      "5000": { basic: "0.2", premium: "0.8" }
    support: { values: [basic, premium] }
```

- **The class of a row** is the bin the parent's own draw chose. It is known before the child draws, on the per-row
  path and on the column path alike.
- **The place inside a bin** of a binned column drawn given others is today's bounded draw, independent of every
  other column: the clique carries no more than the bin. (ADR 0035's copula is different, and draws its place from its
  own residual, and a copula column is no `given` parent.)
- **No seed path is added.** Each column draws on its own cell path at the indices a histogram or categorical column
  draws at, and the parent's bin only chooses the table the uniform is searched in, so KERNEL-SPEC-2 section 12's
  example of a declaration that adds no path applies. A test holds the per-row and the column path to the same bytes.
- Under kernel `0.1.0`, a binned parent is `FLX-3105`, naming the kernel. Anything else this decision refuses is
  `FLX-3402`, and so, in this design, is a column with `allocation: exact` drawn given a binned parent: KERNEL-SPEC-2
  section 8.3 lays out a child given an exact categorical column only.
- The same tests as any draw hold it: the same digests at `--jobs 1` and `--jobs 4`, shards of `--shard I/N` that
  concatenate to the whole run, and `--scale` changing only the number of rows.

### 4. `fluxsim init`

- **Cliques.** `flux_engine/author/cliques.py` reads `cliqueMarginal/2` as it reads the first definition: each column
  of a clique is drawn given its separator, a binned column with `bins` from the clique's domain and `binWeights` or
  `binWeightsGiven` from its noisy counts. The clique's bins win over the column's own histogram when their edges
  differ, and the ledger passes that histogram over with the reason.
- **Grouped tables.** A statistic grouped by the bins of a number column (`groupByBins`, decision 7) is a two-way
  table. The Chow-Liu forest keeps taking a binned column as a leaf in this release, and a binned parent in the
  forest is left for the next.

### 5. `conditions`

`flux_engine/generator/conditions.py` propagates fixed values through the plan's conditionals (ADR 0028 section 10).
A binned column becomes one of its variables, its states the bins. A condition still fixes categorical columns only;
the place inside a bin is drawn as decision 3 says once the bin is known.

### 6. Still not drawn from a clique

A key, a column a rule sets, a column with a declared null fraction, and a copula column (ADR 0035). A NULL class in a
clique is left for a later format.

### 7. `groupByBins` (profile format 1-4-0)

A column statistic may be grouped by the bin of another number or timestamp column of its table:
`"groupByBins": { "column": name, "bins": [edges] }`. Groups are keyed by the canonical decimal text of each bin's
lower edge; the records whose grouping value is NULL are one more group, never written. A statistic holds at most one
of `groupBy`, `groupByLink`, `groupByBins` and `per`. The disclosure rules apply group by group, as for `groupBy`. A count
table grouped this way may carry `noise`: each record lands in one cell of one group, so its sensitivity is a count
table's. It is how a profile publishes a two-way table of two number columns, exactly or under noise.

### 8. `verify` (scorecard format 1-5-0)

The reader forms the one-way and two-way marginals of a clique, as PROFILE-SPEC.md section 5a does today, now with
bins:

- one column: `levelHistogram/1` for a categorical column, `marginalHistogram/1` over the clique's bins for a number
  column;
- two categorical columns: a `levelHistogram/1` of one grouped by the other, as today;
- a number and a categorical column: a `marginalHistogram/1` of the number grouped by the category;
- two number columns: a `marginalHistogram/1` of the first in the clique's order grouped by the bins of the second
  (`groupByBins`).

Each is marked derived and carries the variance of the cells it sums, so its margin widens as SCORECARD-SPEC.md
section 9 widens one. A property row that holds `groupByBins` names scorecard format 1-5-0.

### 9. Grades

A `cliqueMarginal/2` is noisy as a `cliqueMarginal/1` is, so neither keeps a run from P3. Estimated cliques are not
read in this release. A `groupByBins` count table with a `noise` block is noisy; without one
it is exact.

## Consequences

- A P3 profile can keep the dependence between a number column and the categories and numbers it is published with,
  at the bin's resolution.
- Inside a bin, a number is independent of every other column. Fewer, wider bins lose more; the profiler chooses
  the edges, and they must be public or paid for.
- A clique's cells multiply with each binned column's bin count, and the noise per cell does not shrink with them.
  Bins for cliques should be few, and the cell limits of ADR 0035 decision 10 bound the rest.
- An exact profile gains the same: a two-way table of two number columns, and a number column that is a parent.

## Alternatives considered

- **A clique of number columns with the place inside a bin coupled, as the copula does.** Rejected: a clique says
  nothing about the place, so a coupling there would be invented.
- **Keep number columns out of cliques and use the copula alone.** Rejected: the copula carries no dependence on a
  category, and a profile that measures cliques has already paid for the bins.
- **Bins keyed by index in a weight table.** Rejected: a lower edge says what it is; an index does not.

## Questions settled

1. **A NULL class.** Whether a clique may list NULL as a level of a column with a null fraction is left for a later
   format, since the generator would need a parent class that is NULL.
2. **Bins for timestamps.** Any public edges are accepted. The note for profilers recommends calendar months for a
   date column, which a profiler can fix without the data.
3. **The forest.** Binned parents in the Chow-Liu forest belong to the next release.
4. **The clique's bins and the column's histogram.** The clique's edges win, and the ledger names the histogram it
   passes over.

## What would change this decision

- A profile format that publishes a column's NULLs inside its cliques: decision 6 would give way.
- A measured need for dependence inside a bin: the copula of ADR 0035 would take those columns instead.
