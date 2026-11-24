# ADR 0016: children per parent are counted, not drawn: the counted layout, a minimum, a parent condition, parent aggregates and exact allocation

## Status

Accepted 2026-11-22. The number was reserved for the counted layout when
[ADR 0011](./0011-rows-follow-their-key-entity-and-parent.md) left exact counts to a later change, and
[ADR 0014](./0014-init-reads-every-statistic-a-profile-publishes.md) section 5 named the approximation this record
replaces. It builds on [ADR 0022](./0022-rows-read-their-linked-rows.md), whose parent reads it uses, keeps
[ADR 0018](./0018-module-facts-live-in-the-flux-bundle.md)'s rule that module facts live in the FLUX bundle, and
extends profile format `1-3-0` of [ADR 0023](./0023-a-catalogue-and-the-rows-that-follow-it.md), which is not yet
released. Everything below generates under kernel `0.2.0` only, as ADR 0022 section 1 says of the module facts.
**Amended 2026-11-23:** profile format `1-3-0` (flux-stats) shipped in 0.10.0.

## Context

A foreign key picks its parent row one child at a time (KERNEL-SPEC section 8.2): `uniform` draws a bounded integer,
`weighted` searches the parent's weights, and `declared` snaps a drawn value to the nearest parent key. Each child is
placed on its own, so the number of children a parent receives is a sum of chances. Four things follow, and each was
reproduced on the engine before this record:

- **Childless parents.** With 1,000 parents and 3,000 children under a `uniform` pick, about `e^-3` of the parents,
  some 50, receive no child. An invoice with no line and an order with no item do not exist in the data the bundle
  imitates. ADR 0014 section 5 lays the profile's children-per-parent classes out over the parent's keys and says
  the same of each class: a parent of a class whose mean is `m` receives about a Poisson number of children, so about
  `e^-m` of them receive none.
- **No minimum and no condition.** Nothing states that every parent has at least one child, or that only active
  accounts have open subscriptions. FLUID has no spelling for either.
- **Summaries cannot hold.** FLUID's `aggregate` invariant (a `derive` with `function` `count`, `sum`, `min` or `max`
  over the rows of a child expose that point to this row) is refused with `FLX-6210`: to write an invoice's total, a
  row would need every line that names it, and a line names its invoice only after its own pick. No process knows a
  parent's children without generating the whole child table.
- **Small tables miss their own profile.** A 26-row table drawn level by level from its own shares misses them by
  sampling: a level of share `0.5` lands on 13 rows only about 15 % of the time. The run fails a tolerance the source
  meets exactly, and the default tolerances say nothing about the row count.

The way out is to place the children of a parent together: decide how many children each parent has first, then lay
the child rows out parent by parent. A parent's children are then a run of consecutive child rows that any process
can name from the parent's row alone, which is what a summary and a minimum need.

Borrow-before-build search, 2026-11-22. Versions and licences were read from each project's registry entry or
repository on that day.

| Name | What it does here | Licence | Taken |
|---|---|---|---|
| `flux_engine.core.lrm` (this repository) | the Largest Remainder Method on exact rationals, ties by key and never by seed (KERNEL-SPEC section 5) | Apache-2.0 | reused: parents per class, and children per parent |
| `flux_engine.core.permute` (this repository) | `permute/2`, a keyed Feistel permutation with cycle walking, and `permute2_array` | Apache-2.0 | reused: which parent takes which count, and which row takes which level |
| `flux_engine.author.children` (this repository) | ADR 0014's classes of parents and each class's mean, the open last class solved from the published total | Apache-2.0 | reused by `fluxsim init` for the classes it writes |
| simfam 1.1.6 (CRAN, `draw_num_children_per_fam`) | each family takes `children_min` plus a Poisson draw, then random families gain or lose one child until the total is the population; it refuses a minimum above the average | GPL-3.0-or-later | pattern only, the one precedent found for a minimum: a base of the minimum for every parent, the rest allotted on top, an exact total, and a refusal when the children cannot cover the minimum. The allotment here is the largest remainder instead of Poisson draws and random corrections, so it needs no draw |
| Misata 0.9.8.1 (PyPI, `rasinmuhammed/misata`) | `min_children` on a relationship refuses when the child table cannot cover its parents; roll-ups (`misata/rollups.py`) compute a parent's summary from its children after both tables are materialised | MIT | pattern only: the refusal, and a roll-up's `count`, `sum`, `min` and `max`, `where` filter and fill for a parent with no child. FLUX never materialises a table, so a summary reads its children through the counted layout instead |
| humanleague 2.4.5 (PyPI) and Lovelace and Ballas, "Truncate, replicate, sample" (2013) | integer populations that meet their marginals exactly, built in integer space rather than rounded afterwards | MIT; paper | pattern only: exact counts first, then the rows that take them |
| SDMetrics 0.33.0 (`CardinalityShapeSimilarity`, `CardinalityBoundaryAdherence`) | children per parent compared as a distribution, and the share of parents whose count lies within the data's bounds | MIT | the definition of the children-per-parent check, used as a pattern |
| SyntheRela 0.0.4 (PyPI) | cardinality shape similarity, and parents enriched with their children's aggregates, as relational fidelity measures | MIT | confirms the two checks; never a dependency |
| libffx 2.0.1 (PyPI, FF1 of NIST SP 800-38G) | a format-preserving Feistel permutation over a domain with cycle walking | MIT | the test oracle for the permutation's properties (a bijection of the domain at every size the layout uses); never imported by the engine |
| FLUID 0.7.7 (`invariantAggregate`) | `function`, `column`, `from`, `via`, `filter`, `ifNone` | Apache-2.0 | the semantics of a summary, as EXPR-SPEC-2 section 5 already reads it |

No tool found lays children out so that a parent's children are found from the parent's row without a pass over
the child table, which is what generation in slices, in any process, needs.

## Decision

### 1. The `counts` key

The Simulation's namespace gains a top-level key `counts` (`extensions['com.agentics.flux/1'].counts`), a list with
one entry per relationship it lays out:

```yaml
counts:
  - emit: invoice_line
    relationship: invoice
    minChildren: 1
    parentWhen: "status <> 'void'"
    children: { bins: ["1", "2", "4", "10"], binWeights: ["0.40", "0.35", "0.25"] }
    tolerance: "0.10"
```

- `emit` and `relationship` name an emitted expose and a relationship it declares (FLUID's
  `semantics.relationships[].name`). Each relationship has at most one entry.
- `minChildren`, a whole number, default `0`: every parent the condition admits has at least that many children.
- `parentWhen`, a FLUID SQL/1 predicate over the parent expose's own columns: only a parent whose final row makes it
  TRUE has children; every other parent has none.
- `children`, the shape of the number of children per parent, in the fidelity block's own spelling: `levels` with
  `levelWeights`, or `bins` with `binWeights`, over whole counts (section 2).
- `tolerance`, a decimal, default `0.10`: the largest total variation distance the children-per-parent check of
  section 8 allows against the profile.
No other key is read; an unknown key is `FLX-3105`. A per-parent expected count is declared elsewhere, by the
  module that owns it, and reaches the layout through the hook of section 3.

An entry puts its relationship on the counted layout. A relationship with no entry picks as it always has. The entry
changes the rows the child generates, so it is bound into the child's `H`, as a `links` entry is (ADR 0022), and `G`
leaves it out. Under kernel `0.1.0` the key is refused with `FLX-3105`, naming the kernel.

Refused at plan time with `FLX-3403`: a relationship the expose does not declare; one whose foreign key column
declares a `nullFraction` or a draw of its own (`levels`, `bins`, a mean, `given`), since the layout sets it; a
`distinct` relationship (one whose columns are a key of the child) and one that is part of a key, implied by
another, a side of a bridge, or a self-relationship; one a `links` entry gives a `when` (ADR 0022 section 5) or a `weight` (ADR 0023 section 4), with a message that
names `counts`, since a condition that clears a child's foreign key after the layout allotted it would drop its
parent below its minimum and change the allotted count, and a weighted pick is a second layout; a target that
is not generated from statistics or by reference; a `children` shape with a count below zero, a fraction, or a
class whose least count is above its greatest; and a `minChildren` above every count `children` allows.

### 2. The counted layout

The layout is a pure function of the child's rows `N`, the parent's final rows and the entry. It draws no value. It
is worked out once per process, as a weighted pick's thresholds are (ADR 0023 section 4), and every slice of the
child reads it.

1. **Eligible parents.** `E` is the parent rows on which `parentWhen` is TRUE, in row order, or every parent row when
   the entry has no condition. A parent's rank `r` is its position in `E`, and `e = |E|`.
2. **Each eligible parent's expected count `x`**, a non-negative rational, from the shape alone; section 3 says how
   a per-parent expected count enters it:
   - from `children`, when it declares a shape. Its classes are its levels, each one count, or its bins, each the
     whole counts from its lower edge up to the next edge, the last bin holding its upper edge. The parents of each
     class are `n_k = LRM(e, weights)`, KERNEL-SPEC section 5's method with each class keyed by its position written
     as a decimal zero-padded to one width, so the keys sort in class order. The `e` slots are laid out class by
     class; the `j`-th slot of class `k` holds `lo_k + floor(j * (hi_k - lo_k + 1) / n_k)`, which spreads a bin's
     parents evenly over its counts, as KERNEL-SPEC section 8.2 spreads a key's rows over a bin. The parent of rank
     `r` takes the slot `permute/2(K, e, r)`, where `K` is the key, by KERNEL-SPEC-2 section 2, of the path
     `("sim", sim_id, "gen", exposeId, "count", relationship)`;
   - `1` for every parent otherwise, which spreads the children as evenly as whole counts allow.
3. **The minimum.** With `m = minChildren`, the spare children are `S = N - m * e`. `S < 0` is `FLX-3403`: the child
   rows cannot give every eligible parent its minimum. It is refused at plan time whenever `e` is known there (no
   condition, so `e` is the parent's rows), and otherwise when the layout is first worked out. `N` and the parent's
   rows are the scaled ones under `--scale`, and a refusal under a scale other than 1 names `--scale`, since the
   scale is what left too few child rows. Each parent's share of the spare is `y = max(x - m, 0)`; when
   every `y` is `0` and `S > 0`, every `y` is `1`.
4. **The allotment.** Each eligible parent receives `a = floor(S * y / Y)`, `Y` the sum of the `y`, and the
   `S - sum(a)` parents with the largest remainders one more, ties to the lower rank, on exact integers. Its count is
   `c = m + a`. A parent outside `E` has `c = 0`.
5. **The rows.** Parent `p`'s children are the child rows `C_p` to `C_p + c_p - 1`, where `C_p` is the sum of the
   counts of the parent rows before `p`. Child row `i` takes the key of the parent whose run holds it, found by
   bisection over the `C`, so its foreign key copies that parent row whole, as every pick does.

Three properties follow, and the property tests state them. The counts total `N` exactly. No eligible parent has
fewer than `m` children. When the expected counts are whole numbers of at least `m` that total `N`, every parent's
count is its expected count, so a shape declared from a profile of the same row counts is met exactly: the
histogram of children per parent is the declared one, class for class, up to the largest remainder of step 2.

The siblings of a parent are consecutive child rows, in parent row order. A parent's children are named from its
row and the layout alone, which is what sections 5 and 6 need, and what ADR 0022's "What would change this decision"
asked of a window over siblings too large for one process.

**Cardinality.** A `one_to_one` relationship on the counted layout gives no parent more than one child. A `children`
shape that allows a count above one, a `minChildren` above one, and more child rows than eligible parents are
`FLX-3403`, at plan time whenever `e` is known there. Where step 4 would give a parent a second child, the spare
children go one each to the eligible parents with the largest `y`, ties to the lower rank. That is step 4's own
allotment whenever it gives no parent more than one child, so a shape declared from a profile of the same row counts
is met as before; under `--scale`, where the rounding of the child rows and of the shape's classes can leave one
child more than the parents of the class of one, the layout places it instead of refusing the run. The same holds
for the expected counts of section 3. `joinType` is honoured by construction, since every child row reaches a parent.

**Additive.** The key `K` comes from a path of a new shape, with a literal (`count`) that no other shape has at that
position, so its address is new; it is read only for a bundle that declares a `counts` entry; nothing that exists
moves; and the vector file pins its key, the slots and the counts of chosen layouts (KERNEL-SPEC-2 section 12). The
path stays out of the seed-path ledger, as a generator cell's does. The foreign key column's own stream draws
nothing on the counted layout, and no other cell's draws move. A bundle with no `counts` entry has no layout, and its
plan documents and digests are those it had.

### 3. The expected-count hook

A parent's expected count may come from somewhere other than a declared shape: the days a parent is active inside
the window, for example, so that an account open for two months cannot have forty invoices. The module that owns
such a count declares it in a key of its own and gives it to the layout through one interface,
`flux_engine.generator.counts.ExpectedCountHook`:

```python
class ExpectedCountHook(Protocol):
    def member(self, namespace: Mapping[str, Any], child: GeneratorPlan, relationship: str) -> Optional[Mapping[str, Any]]:
        """At plan time: None when the bundle declares no expected count for this relationship; otherwise what the
        hook binds into the child plan's document, so that a changed declaration moves its digest. Refusals are
        FLX-3403."""

    def expected(self, parent: GeneratorPlan, row: int, final_row: Tuple[Any, ...], relationship: str) -> Fraction:
        """The expected number of children of parent row `row`, exact and not negative, from that row as the
        parent's own pass writes it (ADR 0022 section 3, in `parent.columns` order) and from the plan, its
        `spec.timeBounds` included."""
```

Hooks are registered by the engine's own modules and never loaded from a bundle, and at most one returns a member
for a relationship (two are `FLX-3403`). A relationship whose hook returns a member is on the counted layout as if
it had a `counts` entry; its entry, when it has one, adds the minimum, the condition, the shape and the tolerance.
`expected` is called once per eligible parent, in rank order, when the layout is worked out; a value that is negative
or not exact is `FLX-3403`. This version registers no hook; a test registers a stub.

**How the expected count `e_p` enters each family the layout supports.** It replaces the declared distribution's
mean, parent by parent, and keeps the shape's spread around it:

- **No shape** (no `children`): `x_p = e_p`. The children are allotted in proportion to the expected counts.
- **A histogram or a level set** (`children` with `bins` or `levels`): a rank coupling. The `e` slot values of
  step 2 are worked out as without the hook, and the parents take them in the order of their expected counts: the
  parent with the largest `e_p` takes the largest slot value, the next the next, and parents whose `e_p` are equal
  are ordered by the slot `permute/2(K, e, r)` that step 2 gives them, so the tie is broken by the layout's own
  permutation and never by row order alone. Then `x_p` is the slot value the parent took. The declared histogram is
  met exactly, class for class, and a parent's count rises with its expected count. A product of the two, `e_p`
  times the slot value over the mean, was rejected: the profile's histogram of children per parent already holds the
  spread that exposure causes, so a product counts it twice, widens the run's histogram past the declared one and
  fails the children-per-parent check of section 8 on any bundle whose exposure varies.
- **`one_to_one`**: as either of the above, and no count may exceed one.

Steps 3 to 5 are unchanged: the minimum lifts every eligible parent to `m`, the spare is allotted by the largest
remainder over `max(x_p - m, 0)`, and the counts total `N` exactly whatever the expected counts total.

**The hook draws nothing and moves no position.** `expected` is a pure function of the parent plan, the parent row,
its final row and the relationship: it reads no stream, it takes no draw index on any path, and its value only
weighs the allotment of step 4. The layout's own permutation is keyed by the same path with or without it. So a
bundle that declares no expected count calls no hook: its counts, its rows and every draw are exactly those of
section 2, and a bundle without a `counts` entry or a hook's declaration draws exactly what it drew before this
record. A later hook that needs randomness per parent draws on a path of its own, under KERNEL-SPEC-2 section 12,
and says so in its own record.

### 4. A minimum and a parent condition

`minChildren` and `parentWhen` are the counted layout's steps 1 and 3. The condition is evaluated on the parent's
final rows (ADR 0022 section 3), worked out once per process over every parent row, as a weighted pick's weights
are. A condition that reads a column of another row (a link, `previous`, children or siblings) is `FLX-3403` in this
version. `fluxsim verify` checks both on the run's rows (section 8).

### 5. Parent aggregates

A contract `derive` with `aggregate` generates when its `via` relationship of `from` is on the counted layout. The
parent row's value is the summary of its children, the child rows of section 2 step 5, as the child's own pass writes
them, with EXPR-SPEC-2 section 5's semantics: `count` the number of children for which `filter` is TRUE, `sum`,
`min` and `max` over those whose `column` is not NULL and NULL when there are none, and `ifNone` in place of a NULL
summary. A FLUX-side `derive` whose value holds an aggregate over children stays `FLX-6210` in this version, its message
naming the `counts` key; the contract form covers the summaries FLUID states. An aggregate whose relationship is not on the counted layout stays `FLX-6210`, and its
message names the `counts` key. An aggregate nested over another collection, and one whose summarised column reads
the parent's aggregate back through a path, which is a cycle (`FLX-6208`), are refused.

The parent's plan document gains `sums`, a list of `{expose, relationship, generatorDigest}` for each child plan an
aggregate reads, so a changed child moves the parent. The child reads the parent's plan as it is before its sums are
attached, so no plan holds itself and no digest names itself, and a child that reads an aggregate column through a path,
or a column computed from one, is a cycle (`FLX-6208`), raised at plan time. Another expose that reads a summary
through a link is `FLX-6210` in this version. A summary must fit its column (`FLX-6110`
for its type and scale at plan time, `FLX-6112` for its digits when it is computed). Each process that writes a slice of the parent generates the
children of that slice's parents, which costs one more pass over those child rows; parents are usually fewer than
their children, and each child row is made once per parent slice.

### 6. Exact allocation

A fidelity column declaration gains `allocation`, whose one value is `exact`. It is read for a column drawn from
`levels`, from `bins` and `binWeights`, or from `levelWeightsGiven` or `binWeightsGiven` given a column that is itself
exact. The column's classes (its levels or bins, and NULL when it declares a `nullFraction`) take
`LRM(N, weights)` rows, keyed as in section 2 step 2, and row `i` takes the class of the slot
`permute/2(K, N, i)`, laid out class by class, where `K` is the key of the path
`("sim", sim_id, "gen", exposeId, "allocate", column)`. A bin's value inside the bin is drawn as always. Given a
parent column, the rows of each parent level are the slots of that level's class, a row's index among them is its
slot less the class's first slot, and the level's own counts and permutation, over a domain of that level's rows, give
the row its class. So each level, each bin and each pair of a parent level and a child level holds exactly its
share of the rows, up to the largest remainder.

Every draw keeps its index: the null draw and the class's draw are made and left unread, so the bin's value draw is
the word it would have been. Kernel `0.1.0` refuses the key with `FLX-3105`, and a column `given` a linked row's
column, or given a column that is not exact, and the key on a column of any other family, are `FLX-3402`. The path is new, as in section 2, so the change is
additive.

### 7. `fluxsim init`

- **The counted layout from the profile.** For each `relationships` entry of the profile whose relationship the
  contract declares, with a published `marginalHistogram/1` of children per parent, `init` writes a `counts` entry
  whose `children` are the histogram's classes: ADR 0014's class weights, and the open last class's upper edge chosen
  so that its even spread has the mean `_class_means` solves from the published total, or from the
  child rows when the profile publishes no mean (the layout gives every child a parent). It no longer spreads a number
  foreign key's draw over the parent's keys, since the layout holds the counts exactly and for a key of any type. A
  `one_to_one` relationship takes `levels: ["0", "1"]`.
- **The class of none.** The histogram's first class, the parents with no child, is read as published. A published
  count, zero included, is that class's weight, and a withheld one is filled under ADR 0014 section 3, which never
  assumes a level is zero. Only a class the profile marks structural is known to be empty: a relationships entry of
  format `1-3-0` may carry `structural`, a list of `childless` (the source enforces at least one child per parent)
  and `orphan` (the source enforces referential integrity, so no child key that is not NULL misses its parent). A
  structural class is empty because of a constraint of the source, not a count of its records, so it discloses
  nothing about them and is published even where zeros are disclosive. For `childless`, `init` writes
  `minChildren: 1`, and the class of none takes no weight. For `orphan`, `init` records the entry as checked, since
  FLUX builds every foreign key so that it finds its parent (KERNEL-SPEC section 8.1), structural or not.
- **Small tables.** `init` writes `sampling: true` on each distance tolerance it writes (section 8). A table is small
  when the widening of section 8 at its rows, for any property `init` writes for it, exceeds the default tolerance;
  every column of a small table that `allocation` can read takes `allocation: exact`, and its widening becomes the
  rounding term alone.

### 8. Checks

- **Children per parent, under `crossTable`.** For each relationship the profile describes with a
  `marginalHistogram/1` of children per parent, the scorecard gains a row, `"property": "childrenPerParent"`, with
  `propertyId` `children:<child exposeId>.<relationship>`: the run's parents counted into the profile's bins by their
  children, compared as a histogram is, one minus the total variation distance, within when the distance is at most
  the entry's `tolerance` (default `0.10` for a relationship with no entry). Scorecard format `1-4-0`; a scorecard
  with no such row and no row of the next item names `1-3-0` and is byte for byte what that format wrote.
- **The minimum and the condition, as `rules` lines.** For each `counts` entry, `fluxsim verify` counts the eligible
  parents with fewer than `minChildren` children and the parents outside the condition with any child. Each is
  enforced, so one such parent fails the run, as a `derive` without a band does.
- **Aggregates.** A generated aggregate is checked on every parent row as a `derive` is, joining each child to its
  parent through the relationship in the run's rows, and is enforced. An aggregate that does not generate is still
  not checked, so the output of a bundle that declares none of this does not move.
- **Tolerances that follow the row count.** A distance tolerance in a fidelity block may declare `sampling: true`.
  Its margin is then widened, group by group, by
  `w = sqrt(m) * (sqrt(m * v) + sqrt(10 * v)) / n + m / (2 * n)`, with `m` the cells compared, `n` the run's rows in
  the group and `v = n * p_max`, `p_max` the largest share of the reference: the bound of SCORECARD-SPEC section 9,
  with multinomial sampling in place of noise, whose covariance is at most `n * diag(p)`. For a column under exact
  allocation only the rounding term `m / (2 * n)` remains. Roots and `w` are rounded up to 12 places as there. The
  statement is for the Gaussian approximation of the multinomial; the pass rate at small `n` is the one a test
  measures, as section 9 says of Laplace noise. A tolerance without the key is compared as before.

### 9. Format changes

- **Profile format `1-3-0`** (flux-stats, released with 0.10.0, ADR 0023): a `relationships` entry may carry `structural`.
  Optional and additive; the format's other keys keep their meaning.
- **The fidelity block** (flux-stats): the column key `allocation` and the tolerance key `sampling`. Both were
  refused as unknown before, so a block that does not use them reads as it did.
- **flux-stats API**: `RelationshipEntry.structural`, and the widening of section 8 as a function beside the noise
  widening, so a profiler can state the margin a run will be held to.
- **Scorecard format `1-4-0`** (the engine's): the `childrenPerParent` row and the `sampling` member of a property
  row's widening.
- **The profile request** needs no new field for this record: it already asks for children per parent, row counts
  and the fact shares of every rule, aggregates included. Request format `1-1-0`, the one new request format of this
  release, is defined by the temporal record, which owns its fields.

## Consequences

- A bundle that declares no `counts` entry, no `allocation` and no `sampling` keeps every byte and every digest under
  both kernels. A golden pins such a bundle with the digests origin/main gives for it, and the `--jobs 1` and
  `--jobs 4` runs of a bundle that declares them give the same digests.
- `fluxsim init` writes these keys for new bundles, so a bundle made again from the same contract and profile differs
  from one made before: its parents have their counts, its summaries hold and its small tables are exact. That is a
  new bundle, not a moved one.
- Kernel `0.2.0`'s column path takes the counted layout and exact allocation a column at a time, and a test per
  construct compares its rows with the per-row path's. Aggregates are applied a row at a time, as ADR 0022 applies
  rules that read another row.
- Each process that reads a counted relationship works out the layout over every parent row, and a condition or a
  hook reads the parent's final rows: a cost that follows the parent's rows, paid once per process.
- A parent's children sit on consecutive child rows in parent order, so a child table on the counted layout comes
  out grouped by parent in row order, as a table loaded parent by parent does. A consumer that needs another order
  sorts it. The child's own key is laid out from the row index (KERNEL-SPEC section 8.2), so a number or date key
  laid out in ascending order gives a parent's children consecutive keys, and a text or UUID key, permuted, does
  not. The how-to and the reference of `plan` and `apply` say so.
- The engine's flux-stats range moves with the release that ships format `1-3-0`, and the profiler gains `structural`
  on its side when it can state it.

## What would change this decision

- A profile that publishes children per parent jointly with a parent column, which would let the shape vary by
  parent class rather than through a hook.
- A source whose siblings are not consecutive in any useful order, which would need the layout's runs permuted over
  the child rows and an inverse of `permute/2`.
- A measured correlation between a parent's expected count and its number of children. The rank coupling of
  section 3 makes the order perfect, which is a simplification: a profile that publishes how far counts follow
  exposure would let the coupling mix ranks to that degree.
- A hook that needs more than one parent's row, or a condition through a link, when a bundle needs one.
- FLUID gaining a spelling for a minimum or a parent condition, which would move the declaration into the contract.
