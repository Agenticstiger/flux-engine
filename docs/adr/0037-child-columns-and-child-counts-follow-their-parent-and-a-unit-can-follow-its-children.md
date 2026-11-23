# ADR 0037: a parent's number of children is drawn given one of its columns, relational statistics may be released with noise, and a unit of privacy can take its child rows with it

## Status

Accepted 2026-11-23, with the amendments of decisions 1 and 2 (the followed relationships form a tree, and a labelled
column of a table the unit treats as fixed counts as `P1` for the minimum grade) and of decision 3 (estimated cliques
are not read in this release). Builds on
[ADR 0016](./0016-children-per-parent-are-counted.md) (the counted layout),
[ADR 0022](./0022-rows-read-their-linked-rows.md) (a row reads its linked row),
[ADR 0023](./0023-a-catalogue-and-the-rows-that-follow-it.md) (a column drawn `given` a linked row's column, from a
statistic grouped by a link), [ADR 0019](./0019-one-privacy-budget-per-profile-release.md) (one unit of privacy, and
noise only in its expose) and [ADR 0020](./0020-privacy-grades.md). A companion of
[ADR 0035](./0035-number-columns-are-drawn-through-a-gaussian-copula-of-published-rank-correlations.md) and
[ADR 0036](./0036-binned-number-columns-join-clique-marginals.md). The format text of the unit that follows
its child rows and of what a noisy statistic is charged is in [PROFILE-SPEC.md](../spec/PROFILE-SPEC.md) section 3a;
the text for a number of children given a parent column is in
[SEAM-FIDELITY-SPEC.md](../spec/SEAM-FIDELITY-SPEC.md) section 3 and [KERNEL-SPEC-2.md](../spec/KERNEL-SPEC-2.md) section 8.1.

**What this release builds.** `privacyUnit.follows` and the charges of decision 2, the relational statistics of
decision 3 (a relationship's statistics grouped by a parent column, noise on a relationship's histogram and on a
statistic grouped by a link), the grades of decision 8, and request format 1-2-0's `groupBy` and `linked` asks.
**What it leaves for later:** estimated cliques, relational cliques (a clique whose columns span a link), and a
cap chosen privately from a ladder.

## Context

**What FLUX draws across a relationship today.** A child column can be drawn given a column of the row its link names
(`given: <relationship>.<column>`), and `fluxsim init` writes that from a statistic grouped by a link (`groupByLink`,
ADR 0023 section 5). This is the joint of a parent attribute and a child attribute. A parent's number of children
follows one shape for every parent (ADR 0016), or a hook that a module supplies; it never follows the parent's own
columns, although a business account has more invoices than a personal one.

**What a profile released under differential privacy can hold across tables today: nothing.** PROFILE-SPEC.md section
3a refuses a noisy statistic in a table other than the unit's expose, in a relationship, or grouped by a link: the
profile does not say how many rows of another table one unit contributes, so the loss there is not known. A parent row
changes the join row of each of its children, so grouping by a link has no bounded sensitivity unless something bounds
the children. A P3 profile therefore describes one table.

**Borrow-before-build (2026-11-23).** Inside the repository: the counted layout and its expected-count hook
(KERNEL-SPEC-2 section 8.1), `given` across a link and `link_givens` in `flux_engine/author/catalogue.py`, and the
budget check of `flux_stats`. Searches: differential privacy for relational data with foreign keys; synthesizers of
related tables under differential privacy; contribution bounding in privacy libraries. What was read:

- K. Cai, X. Xiao and G. Cormode, "PrivLava: Synthesizing Relational Data with Foreign Keys under Differential
  Privacy", SIGMOD 2023, extended version arXiv 2304.04545. One relation is the primary private one, and neighbouring
  databases differ by one of its tuples together with every tuple that depends on it through foreign keys. Each
  foreign key's multiplicity is bounded by a public constant, enforced by truncation, and every sensitivity is a
  multiple of the tuples or groups one neighbour can change. It releases noisy counts of child groups by their size and
  by latent classes, and of parent attributes. No code was found.
- W. Dong, J. Fang, K. Yi, Y. Tao and A. Machanavajjhala, "R2T: Instance-optimal Truncation for Differentially
  Private Query Evaluation with Foreign Keys", SIGMOD 2022, pp. 759-772, DOI 10.1145/3514221.3517844: the same
  neighbouring relation; without a public bound on a tuple's dependents the global sensitivity has none, and
  truncation at a ladder of thresholds chooses the bound adaptively.
- I. Kotsogiannis and others, "PrivateSQL: A Differentially Private SQL Query Engine", PVLDB 12(11):1371-1384, 2019:
  a policy over a primary private relation, and truncation of join keys above a threshold, which can be learned with
  a reserved share of the budget.
- Tumult Analytics 0.21.0 (Apache-2.0): a privacy identifier per row and truncation to a maximum of rows per
  identifier. PipelineDP 0.3.1 (Apache-2.0) bounds contributions per privacy unit. OpenDP 0.16.0 (MIT) names a unit
  by an identifier column with bounded contributions.
- PrivBench (github.com/teijyogen/privbench, GPL-3.0-or-later; "Privacy-Enhanced Database Synthesis for Benchmark
  Publishing", arXiv 2405.01312): read for the pattern only, and no code is used. PrivPetal (arXiv 2503.22970) and
  the relational synthesizer of Alimohammadi and others (arXiv 2405.18670) publish no code that was found.

FLUX adopts the neighbouring relation, the public bound per followed relationship enforced by truncation, and the
charge of each statistic by the rows one unit can change. It does not adopt PrivLava's latent variables: they are a
model fitted to records, and FLUX reads aggregates.

## Decision

### 1. A unit of privacy may follow its child rows (profile format 1-4-0)

`privacyUnit` gains `follows`, a list of `{ "parent": { "table", "columns" }, "child": { "table", "columns" },
"maxChildren": c }`. Each entry names a `relationships` entry of the profile, at most once, and `c` is a count of at
least 1. A relationship named only to be followed may hold an empty list of statistics. The entries form the tree
described below.

- **Opt in.** `follows` is optional. Without it the unit is one row of the expose, as it always was, and every
  charge, byte and digest of such a profile is what it was. A profile of an earlier format cannot hold it.
- **The unit.** One unit is one row of the unit's expose together with, recursively, the rows of child tables that
  reference a row of the unit through a listed relationship. Neighbouring data sets differ by one unit.
- **A tree.** The listed relationships form a tree rooted at the expose: each table they reach, other than the expose,
  is the child of exactly one listed relationship; the parent of every listed relationship is the expose or a table
  they reach; no table is the child of two listed relationships; they form no cycle; and the expose is never a child.
  A list that is not such a tree is refused with `FLX-5036`, naming the entry. A table reached by two paths, or a
  child whose parent the unit does not reach, would make the stated reach understate what one unit changes: two
  followed foreign keys of cap 1 into one table state a reach of 2 while a single parent row can bring its rows in
  through both, and the rows of a parent that is no part of the unit move with that parent, not with the unit. The
  tree keeps every reached row on one path from the unit's row, so the reach below is the number of rows one unit
  can change.
- **Truncation.** Before it measures anything, the profiler keeps at most `c` children of each parent row on each
  listed relationship, dropping the rest by a rule that reads only that parent's own child rows (a fixed order of
  their values, or a seeded draw), and a dropped row takes the rows that follow it. Rows of a followed table that
  follow no unit row are dropped too. Every statistic of the profile is measured on what remains. Truncation is what
  makes `c` a bound.
- **Public.** `maxChildren` is a fixed public cap per followed relationship, chosen without the data, and the
  profile's `publicStructure` declaration covers it. It is not a threshold chosen privately from a ladder.
- **Reach.** `m_T`, the number of rows of table `T` that one unit contributes, is 1 for the unit's expose. For a
  reached table `T` it is `c * m_P`, with `c` the cap of the one listed relationship from `P` to `T`: the product of
  the caps along the one path from the expose to `T`. A table no listed relationship reaches has `m_T = 0`.
- **What the guarantee covers.** The guarantee is about units. The rows of a table the unit does not reach are the
  same in every neighbouring data set, so a statistic grouped by such a table's column, or a count of its rows'
  children, protects the unit's rows and not that table's. `EVIDENCE.md` names the tables the unit reaches and the
  tables it treats as fixed, and the grade's account says the same (decision 8).
- **The fixed-table floor.** For the minimum-grade check of each column (ADR 0020), any labelled column of a table
  the unit treats as fixed (`m_T = 0`) counts as `P1`, whether or not the statistic that touches it is noisy: a
  label such as `pii` or `restricted` on a column of a fixed table says that one of its rows must not move a
  published number, and the guarantee does not cover those rows. A run graded `P3` therefore cannot draw on such a
  column of a table the unit does not follow.

### 2. What a noisy statistic is charged

ADR 0019 decision 4 charges a count table a sensitivity of 1 in the unit's expose. It now charges a noisy statistic
over the rows of `T` `m_T` times the sensitivity that one row of `T` has, by the triangle inequality over the unit's
rows added one at a time: `m_T` in L1 and in L2 for a count table, `m_T * Delta` for a rank correlation (ADR 0035). A
statistic over a table with `m_T = 0` is refused with noise (`FLX-5036`). For a profile with no `follows` that is
today's rule for a statistic of one table; the two forms below, which read a row of another table and have rules of
their own, become possible there too: a statistic of the unit's expose grouped by a column of a parent the unit does
not reach, and the children per parent of a relationship whose child is the unit's expose.

- **Grouped by a link** (`groupByLink`, a statistic of table `T` grouped by a column of parent table `P`). A unit that
  removed a row of `P` without its rows of `T` would move them into the group that is never written, so noise is
  allowed only when `P` is not reached (its rows never change), or when the link's relationship is listed in
  `follows` (a removed row of `P` takes its rows of `T` with it, and they are counted in `m_T`). It is charged `m_T`.
  Otherwise it is refused.
- **Children per parent** of a relationship from `P` to `C`, whose records are the rows of `P` and whose value is
  their number of children:
  1. `P` reached, and `C` reached by no other listed relationship (`m_C'`, which is `m_C` less this relationship's
     contribution `c * m_P` when it is listed and 0 when it is not, is 0): one unit adds or removes at most `m_P`
     records with their children and changes no other, so the sensitivity is `m_P` in L1 and in L2.
  2. `P` not reached and `C` reached: each of the unit's `m_C` child rows moves one parent between two count classes,
     so the sensitivity is `2 m_C` in L1 and `sqrt(2) m_C` in L2, and the squared L2 is `2 m_C^2`. With the fixed-table
     floor of decision 1 this case is only safe where the parent's labelled columns are not read at `P3`.
  3. Otherwise noise is refused, with `FLX-5036`.

  A grouping by a column of `P` keeps the same sensitivity: a parent's group is the record's own value. On a followed
  relationship no parent has more than `c` children, so a level or a bin whose least count is above `c` is refused,
  whatever its noisy count.

The arithmetic is ADR 0019's with `Delta` in place of 1: a Gaussian of scale `s` spends `rho = Delta_2^2 / (2 s^2)`,
a Laplace of scale `b` spends `epsilon = Delta_1 / b`, and a share below that is refused.

### 3. Relational statistics a profile may publish

- **A parent attribute and its number of children.** A relationship's statistics (`mean/1`, `marginalHistogram/1`,
  `quantile/1` and `quantile/2` of the number of children per parent) may carry `groupBy`, naming a categorical column
  of the parent table, or `groupByBins`, naming a number or timestamp column of it (ADR 0036 decision 7). Their groups
  follow the rules of a grouped column statistic: keys distinct and sorted, the disclosure rules applied group by group,
  the parents whose value is NULL one more group, never written.
- **Noise** is allowed under decision 2 on a relationship's `marginalHistogram/1`, grouped or not, and on a count
  table with `groupByLink` (a parent attribute and a child attribute). A relationship's `mean/1` and quantiles stay
  exact, as a column's do, and a relationship that holds one keeps a run at P1 (decision 8).

Estimated cliques are not read in this release: a table's cliques are measured (ADR 0035 decision 10, ADR 0036), so
no estimate's variance enters the account.

### 4. The declaration: children given a parent column

A `counts` entry (ADR 0016 section 1) may name a column of the parent expose in `given` and carry `childrenGiven`, one
weight list per class of that column, over the classes its `children` shape declares:

```yaml
counts:
  - emit: invoice
    relationship: account
    children: { bins: ["0", "1", "3", "10", "40"], binWeights: ["0.10", "0.40", "0.35", "0.15"] }
    given: a_segment
    childrenGiven:
      business: ["0.02", "0.18", "0.45", "0.35"]
      personal: ["0.12", "0.48", "0.32", "0.08"]
```

- The given column is a categorical column of the parent with declared levels and no `nullFraction`, or a binned
  column (ADR 0036 decision 3), whose classes are keyed by the text of their lower edges.
- `children` is required with `given`. It fixes the classes, and its weights serve a parent class that has no list,
  which the plan states.
- Refused with `FLX-3403`: a `given` the parent does not have; a key of `childrenGiven` that is not a class of the
  given column; a list of another length than the classes, or with no weight; `childrenGiven` without `given`; and,
  as for `parentWhen` (SEAM-FIDELITY-SPEC.md section 3), a given column that reads a link, a previous version,
  children, siblings, or a column that a summary over the parent's children sets. Under kernel `0.1.0` the keys are
  `FLX-3105`, naming the kernel. The entry is bound into the child's `H` and left out of `G`, as `counts` is, and an
  end-to-end `ship` test covers it.

### 5. The layout (KERNEL-SPEC-2 section 8.1, extended)

With `given`, the eligible parents `E` are split by the class of the given column's value on each parent's final row
into `E_l`, keeping their order. Class `l` has `e_l = |E_l|` parents, `n_(l,k) = LRM(e_l, weights_l)` of them in count
class `k`, and the slots `v_(l,i)` of section 8.1. `sigma = permute/2(K, e, r)` is taken over the whole of `E`, as
today, and the parents of `E_l` in ascending order of `sigma` take the slots of class `l` in order: the `i`-th takes
`v_(l,i)`, which is its expected count `x_r`. The minimum, the allotment and the rows are section 8.1's, unchanged. With
an expected-count hook, the rank coupling of section 8.1 applies within each class.

No seed path is added: `K` is the key of the `count` path and `sigma` is today's permutation, read for a new
declaration, as KERNEL-SPEC-2 section 12's examples of a declaration that adds no path read theirs. The vector file of
the counted layout gains layouts with given classes. A layout is a pure function of the run, so `--jobs`, `--shard`
and `--scale` behave as section 8.1's do, and tests cover each.

### 6. `fluxsim init`

- A statistic grouped by a link, exact or now noisy (its groups made distributions by PROFILE-SPEC.md section 3a's
  projection), becomes `given: <relationship>.<column>`, as ADR 0023 section 5 writes it.
- A children-per-parent histogram grouped by a parent column becomes the `given` and `childrenGiven` of the
  relationship's `counts` entry; the ungrouped histogram, or failing it the sum of the groups, is its `children`.
- The ledger passes over, with the reason, what the generator would refuse: a grouping by a column with a null
  fraction, by a column set from this relationship's children, or by a link whose parent does not draw that column.
- When `init` writes a profile request, it asks, in request format 1-2-0, for each relationship it lays out on the
  counted layout to be grouped by the parent columns it draws (`relationships[].groupBy`), and for the link
  groupings its `given` columns would read (`relationships[].linked`).

### 7. `verify` (scorecard format 1-5-0)

- A children-per-parent row (SCORECARD-SPEC.md section 3) grouped by a parent column counts the run's parents of each
  group into the profile's bins by their number of children, as the ungrouped row does, and compares each group with
  the profile's group by total variation distance; groups are named by ordinal, and the tolerance is the `counts`
  entry's.
- A noisy reference, grouped by a link or by a parent column, widens each group's margin by SCORECARD-SPEC.md
  section 9's `w`.

### 8. Grades

The `relationships` section is no longer exact as a whole. A relationship is noisy when every statistic in it carries a
`noise` block and it holds no `dates` and no `exposure`; otherwise it keeps a run at P1, with a reason that names it. A
statistic grouped by a link counts as noisy when it carries a `noise` block. `EVIDENCE.md` states the unit with what it
follows, for example one row of `account` with at most 20 rows of `invoice`, which is the guarantee's unit in NIST SP
800-226's terms, and names the tables the unit reaches and the tables it does not reach, whose rows the guarantee
treats as fixed. A labelled column of a table treated as fixed counts as `P1` for that column's minimum grade
(decision 1). A member the
grade does not know counts as released without noise.

### 9. Not in this record

A child column given its parent's column and other child columns together (a clique that spans a relationship); a
column given two parents' columns, or a grandparent's; numbers of grandchildren; PrivLava's latent variables; and noise
on a relationship's `dates` and `exposure`, on `per` statistics and on histories.

## Consequences

- A profile released under differential privacy can describe several tables, with a guarantee per unit that follows
  its rows, and keep the two relational patterns most visible in data: who has how many children, and how a child's
  attribute depends on its parent's.
- The price of following is the cap: a statistic of a followed table is charged its reach. Through a cap of 20, a
  statistic of the child table needs noise 20 times as large as the same statistic in the unit's expose for the same
  share, or 400 times the share in rho for the same noise. A cap set too low truncates real children, and every
  statistic then describes the truncated data; `EVIDENCE.md` says which cap applied.
- Exact profiles gain a parent's number of children given its columns, with no privacy statement involved.
- A P3 grade now says less about some tables than a reader might assume: a parent table the unit does not reach is
  treated as fixed, so its rows are only as private as its own exact statistics, if any, were. The evidence names
  those tables.
- The engine checks the arithmetic of the charges from what the profile states. It cannot check that the profiler
  truncated as it says, as it cannot check that noise was drawn.

## Alternatives considered

- **The child row as the only unit.** A child-row unit charges a link grouping 1, but protects no parent, and a
  profile with any parent statistic could not be P3. Decision 2's second case keeps it available where the parent is
  not reached.
- **R2T's ladder of thresholds** in place of a fixed cap: better accuracy for the same budget, but the cap then
  depends on the data through a private choice, and the profile would state the chosen threshold and
  its share. Not adopted; a later format could accept one.
- **PrivLava's latent variables.** Rejected: a model fitted to records, which FLUX does not run.
- **A new seed path for the grouped layout.** Rejected: today's permutation, restricted to a class, is a uniform order
  of that class, so no new randomness is needed.

## Questions settled

1. **The neighbouring relation.** A unit that takes its children with it, as PrivLava and R2T define one, for a
   profile that opts in with `follows` and only where the followed relationships form a tree. One row of the expose
   stays the default.
2. **The cap.** A fixed public cap per followed relationship, covered by `publicStructure`.
3. **The second case of children per parent** (`2 m_C` and `sqrt(2) m_C`): kept, with the fixed-table floor of
   decision 1.
4. **Relational cliques.** A child column given its parent's column and other child columns together is the next
   step, and needs a clique whose columns span a link. Not in this release.
5. **Numbers of children given a binned parent column**, which decision 4 allows: in this release, with ADR 0036's
   binned `given`.

## What would change this decision

- A profiler that learns its caps privately: decision 1 would accept a cap with a stated share.
- A demand for patterns that span a relationship beyond one parent column: relational cliques would follow.
