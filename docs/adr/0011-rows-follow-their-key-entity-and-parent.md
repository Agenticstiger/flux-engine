# ADR 0011: synthetic rows follow their key, their entity and their parent through declared rules

## Status

Accepted 2026-11-18. **Amended 2026-11-18:** step 1 is complete in the engine: FLUX generates
a contract `derive` that reads its own row and a difference form over days (EXPR-SPEC-2, section 6), and the
FLUX-side flux-expr/3 rules of ADR 0013 generate the day of the month, the same day a year back and a
quarter's name. With them the first day of the month and dsdgen's quarters are contract rules again. The AWS
harness writes the calendar's and the clock's rules into its contract and its bundle. Nothing in `fluid profile`
changes yet. A prototype in the
AWS harness, `e2e/aws/flux_e2e/follow.py`, applies the rules below to a copy of FLUX's output,
so the harness's judges can measure what they would buy before either tool carries them.
**Amended again 2026-11-18:** step 3 is specified (EXPR-SPEC-2, section 6, rules over a
relationship, and section 7), with the choices its specification makes recorded at the end of this
record.

**Amended 2026-11-19:** step 2 has landed in the engine: FLUX generates a contract's one-way `dependency`
whose band is zero violations or an upper bound, as EXPR-SPEC-2 section 8 says. A dependency with no band, or a band
whose only count is zero, draws a `to` column's NULLs per key. Under an upper bound they are drawn per row, on NULL
rows: each covered row is a NULL row with a share `q` of the covered rows, and on a NULL row each `to` column is NULL
with its declared null fraction over `q`, so every column keeps its null fraction. `q = 1` is the independent draw,
and a smaller `q` puts the NULLs on the same rows, down to the share the column with the most NULLs gives alone. The
plan reads `q` off the band at plan time, in every run: the weakest coupling that keeps the expected count of rows
with a NULL four standard deviations below the bound at the declared row count. An emit that declares no row count
takes the fewest violating rows for a `percent` band, and a `rows` band there, or any upper bound in a strict run,
is `FLX-6204` until the emit declares its rows. A band below what the column with the most NULLs can give alone is
`FLX-6204`, and so, in a strict run, is one that leaves less room than the four deviations above it; one with a
lower bound above zero is `FLX-6210`, since it needs violations, which wait for a later step. The reason is that the NULLs fix the violating share the run will have, so a
band they cannot meet would fail its own check whatever the run, and a source such as dsdgen's, which picks some
lines and NULLs a random subset of each picked line's columns, keeps its structure. This was decided by the
maintainers, 2026-11-19. Each key gets its `to` values once on a stream
keyed by the key, under kernel `0.1.0` and `0.2.0`, so a ticket's rows carry one date, time, customer, household
and store. A `to` column that is a foreign key draws the foreign key's own statistics and takes the parent's nearest
key. A dependency with `bothWays` is refused with `FLX-6210` until a later step. Nothing in `fluid profile` changes.

## Context

FLUX generates the tables of a contract together (KERNEL-SPEC section 8.1): every foreign key
names a row its parent holds, and no declared key repeats.

A consumer query that runs on the source and on the synthetic copy can still give different
answers, because three kinds of relation between rows are ones the generator does not keep:

1. **A dimension's attributes ignore its key.** `date_dim` is a calendar keyed by the Julian
   day, and `time_dim` is a clock keyed by the second of the day. FLUX draws `d_year`, `d_moy`
   and the rest apart from `d_date_sk`, so a sale's date key names a row whose year has
   nothing to do with it, and a query that filters on a year or a month meets other sales.
2. **Rows that share an entity ignore it.** The lines of one store ticket share its date,
   time, customer, demographics, address and store. FLUX draws each line's values on their
   own, so a query that groups lines by ticket finds tickets of one line.
3. **A child row ignores the parent row its key names.** A store return keeps its sale's
   customer about four times in five, is dated 4 to 180 days after the sale, returns at most
   the quantity sold, and is priced at the sale's price. FLUX draws each of these from its own
   column's statistics.

The data owner's side measured these on the source. The source is TPC-DS-shaped and holds no
real people, so the figures describe dsdgen's generator, not a customer base:

- `d_date` is `1970-01-01` plus `d_date_sk - 2440588` days on every row, and the year, month,
  day, month and quarter sequences, week sequence and first day of the month follow from it on
  every row. dsdgen's own quirks are part of the calendar: its weeks start on a Tuesday, its
  weekend is Friday and Saturday, and April 1, July 1 and October 1 of a year not divisible by
  four belong to the quarter before.
- `time_dim`'s hour, minute, second, half of day, shift, part of day and meal follow from
  `t_time_sk` on every row.
- `ss_ticket_number` determines the seven columns named in item 2: no ticket holds two values
  of any of them.
- Every return joins its sale. Where both customers are known, the return's is the sale's on
  about four returns in five. The lag runs from 4 to 180 days and is close to uniform, the
  returned quantity is at most the sold one on every row, and the return amount is the
  returned quantity times the sale's price on every row. The return's demographics, address
  and store agree with the sale's no more often than chance.

Two accepted decisions bound the answer.

- **ADR 0003** (amended 2026-11-16): the engine never reads a record. Every input that
  describes the data is an aggregate statistic or non-personal reference data, and the
  aggregates come from the profiler, `fluid profile`, which reads records only where they
  live and applies output checks before anything leaves. A statistic across tables is no
  exception: it comes from `fluid profile`, under output checks.
- **ADR 0008**: the rules about valid data live in the FLUID contract as invariants (`derive`,
  `check`, `dependency`) written in FLUID SQL/1, and each rule also generates the column it
  determines (EXPR-SPEC-2 sections 6 to 8). A `derive` can read a linked row
  (`<relationship>.<column>`), a dependency gives each key its values once, a rule that holds
  on part of the rows generates at a target inside its band, and `fluid profile` measures the
  band on the real data. flux-expr/2 is closed: its four FLUX extensions are the only ones,
  and new ones go to flux-expr/3 (EXPR-SPEC-2, preamble). The engine implements the language.
  It does not generate from rules yet.

So most of the mechanism is decided. Three things are not:

- **How much of a calendar FLUID SQL/1 states.** It leaves out date arithmetic, `CAST` and
  string functions by design (RFC-data-rules section 2.7), but it has exact division, `ROUND`,
  `CASE` and the whole-unit date differences, and those state most of it. A difference-form
  check could solve for `d_date` from `d_date_sk`. `YEARS_BETWEEN` and `MONTHS_BETWEEN` from
  1900-01-01 give the year and the month sequence. `ROUND` of a shifted ratio floors it:
  `ROUND((d_date_sk - 2415018) / 7, 0)` is the week sequence, and the remainder of the same
  division is the day of the week, from which `CASE` gives its name and the weekend flag. The
  clock is the same arithmetic on seconds. What needs more is everything that turns on the day
  of the month or on a shifted date: the day of the month itself, the first day of the month,
  the same day a year back and dsdgen's quarter quirk, which need a date part, date arithmetic
  or a long leap-year expression, and a quarter's name, which joins a number to text.
- **No statistic carries the shape of a relation across tables.** A rule's band is a share of
  rows. "A return is dated a lag after its sale" also needs the lag's distribution, and no
  profile statistic is a distribution of an expression over a relationship.
- **What an output check counts over a join, and what it leaves out.** Over a join a record is
  a joined row, and many rows can belong to one person, so a cell of 10 rows can describe fewer
  than 10 people. Each table also covers only the rows on which what it reads is known, so the
  rows one table covers and another leaves out are found by subtracting their totals.

## The options

**A. Declared rules, with checked statistics over relationships.** Each relation is a rule
FLUX generates from, and each number a rule needs is a statistic `fluid profile` publishes
under the output checks:

- a reference dimension's attributes are `derive` rules over its key, which read no statistic:
  a calendar and a clock are reference data;
- a functional dependency inside a table, such as the ticket's, is a contract `dependency`,
  generated as EXPR-SPEC-2 section 8 says: each key gets its values once;
- a child's relation to its parent is a rule over the relationship: a `derive` that copies a
  parent's column, with a band for the share of rows on which it holds; a bounds form whose
  bounds read the parent (the return date between the sale date plus the shortest and the
  longest lag, the quantity between 1 and the sold quantity), drawn from a published
  distribution of the expression over the joined rows; and a `derive` that computes across the
  link, such as the amount.

What `fluid profile` publishes is small: a share per copy rule and a histogram per bounded
expression, each a frequency table over the joined rows with one declared set of bins. No
table is split by another column, and the rows the tables leave out are checked as cells of
their own (see the Decision), so no cell below the minimum can be found by difference.

**B. Join-conditioned marginals, fitted to a model.** `fluid profile` publishes contingency
tables over joins (children per parent attribute level, a child column by a parent column), and
a calibration step fits a joint model to them, in the pattern of Private-PGM's estimator,
PrivLava's latent type per parent row, or SDV HMA's child parameters on the parent row; FLUX
then samples from the model. This also conditions a fact's foreign key on its parent's
attributes (sales per item category), which option A does not. It costs many more cells, so
more suppression and more risk of differencing between linked tables (ACRO's linked-table
check only asks a human to look), a fitting stage with its own dependencies, and a model whose
output is harder to explain row by row.

**C. Workload cardinalities.** The profiler publishes row counts for the operators of a known
query workload, and a solver sizes the synthetic tables to match, in the pattern of Hydra,
Touchstone, Mirage and DataSynth, or SAM's learned model. It fits the queries it was given
and says little about others (SAM's unseen join queries keep a long error tail), its many
overlapping counts are a differencing risk, and exact equalities conflict with rounded counts.

**D. A record-fitted relational model** (ClavaDDPM, REaLTabFormer, RCTGAN, IRG, SDV) is out by
ADR 0003's rule, whatever its licence.

## Prior art

Borrow-before-build search, 2026-11-18. Licences were read from each repository's licence file
or its PyPI metadata on that day; "unverified" means no licence file or code could be found.

| Name | Reads | How a child follows its parent | Licence |
|---|---|---|---|
| Private-PGM (McKenna et al.) | noisy marginals | one table; a graphical model fitted to marginals | Apache-2.0, no releases |
| MST, AIM | records, to measure marginals | one table | unverified |
| PrivBayes, DataSynthesizer | records | one table, a Bayesian network | DataSynthesizer MIT; PrivBayes unverified |
| PrivLava (SIGMOD 2023) | records, with differential privacy | a latent type per parent row sets its children's count and attributes | no code found |
| SDV HMA | records | child-model parameters, the child count among them, stored on the parent row | BUSL-1.1 (SDV 2.2.0) |
| ClavaDDPM, REaLTabFormer, RCTGAN, IRG | records | clusters, a parent-to-child sequence model, a row-conditional GAN, a context walk | none, MIT, none stated, none |
| Hydra, Touchstone, Mirage, DataSynth | query-operator cardinalities | per-table summaries sized by linear programming or constraint solving | unverified |
| SAM (SIGMOD 2022) | queries with their cardinalities | one autoregressive model of the full outer join | NTUitive dual licence |
| dbldatagen | a spec | `baseColumn` orders columns, `expr` computes one from others | Databricks License (0.4.0) |
| ACRO | the output | threshold 10, zeros disclosive, round base 5; counts rows, not people; its linked-table check passes and asks a human | MIT (1.1.0) |
| DwB output-checking guidelines | the output | 10 units per cell, 90% group disclosure, dominance; hierarchical units need care | guidance |
| tau-Argus, sdcTable | the output | secondary suppression for linked and hierarchical tables | EUPL-1.2 (4.2.5.2), GPL (0.34.0) |

Two patterns carry over. dbldatagen's `expr` is a column computed from others, which is
ADR 0008's `derive`; SDV HMA's child parameters on the parent row are what option B would
publish as statistics. No guideline found says what a cell counts when the statistic is over
a join. The DwB guidelines count units (people or businesses), ACRO counts rows, and PrivLava's
privacy unit is a parent row with every row that depends on it. forge-cli's own profiler
already counts the records a `groupBy` null leaves out of every group as one more group, never
written (`fluid_build/profiler/disclosure.py`), and its profile RFC refuses a profile that
re-counts the same rows on every refresh unless it declares noise or the owner accepts the
differencing (`RFC-data-profile-port.md`, "Differencing across states"). TPC-DS's own
data holds the relations above directly: `date_dim` follows the Julian day, and a return
follows its sale. No method found reaches 10% on unseen TPC-DS or JOB queries from aggregates
alone.

## How the prototype was measured

The harness measured a prototype of the three layers, one for each kind of relation above (a
dimension's attributes from its key, the columns a ticket shares, and a child's values from its
parent), on one run at scale factor 1 with one seed and a 10% holdout. It judged that run's
synthetic copy, then three clones of the same copy, each with one more layer, so that a change
between two of them comes from the layer and not from a new draw. A small move between two
clones is not read as a layer's.

The harness keeps its results to its operator, and this record does not state them. Each step
of the decision below is measured again when it lands (see Consequences).

Three things are outside what the layers can change:

- a filter on a store's name, city or size: the star has twelve stores, so each level holds
  fewer than 10 rows and stays withheld;
- a filter on an item's class, brand or manufacturer, a city, a zip code or a reason's text:
  those columns have more levels than the harness declares as an enum, so the synthetic side
  makes the levels up;
- the shape of a column inside its range: a fact's date and each of its measures follow five
  published order statistics, so a month's sales and a sum under a filter keep that coarse
  shape.

The rules do not reach these three things. Gains there depend on the profile's own
statistics, which this decision does not change.

## Decision

Option A, in three steps, each its own change with its specification first:

1. **Reference dimensions.** The calendar and clock rules FLUID SQL/1 can state are contract
   rules: `derive` rules, and one difference-form check for the date. The owner writes them in
   the contract, where EXPR-SPEC-2 puts a rule that needs no extension (`FLX-6209`), and FLUX
   generates from them as ADR 0008 decided. The columns FLUID SQL/1 cannot state usefully
   (those that turn on the day of the month or a shifted date, and a quarter's name) need a
   date part, date arithmetic and string concatenation. They come in through flux-expr/3, the
   route EXPR-SPEC-2 names for new extensions, which amends ADR 0008's closed list, or through
   a later FLUID SQL, which grows the core instead. Until then those columns keep their own
   statistics. A quirk that holds on part of the rows, such as dsdgen's quarters, can only be
   a band until then, which puts its exceptions on random rows rather than on its own dates.
2. **Dependencies.** FLUX generates a contract `dependency` as EXPR-SPEC-2 section 8 already
   specifies: each key gets its `to` values once, and every row with the key copies them.
3. **Rules over a relationship.** FLUX generates a `derive` that reads a linked row, a bounds
   form whose bounds read it, and a `derive` that computes across it. A copy is written
   `derive: sr_customer_sk` with `expr: sale.ss_customer_sk` and
   `when: sale.ss_customer_sk IS NOT NULL`. Its band counts a return whose own customer is null
   as violating (`IS DISTINCT FROM`), while the prototype measured the share where both are
   known and kept the return's own nulls. Step 3's specification settles how a copy keeps the
   child's null fraction. Three more points are
   part of the step:
   - **Parent values.** The parent's value comes from its own generator at the parent row,
     through the on-demand read that foreign keys use today (`ParentView`), so a slice of rows
     is the same wherever it is made and `--jobs` stays byte-identical. That read returns what
     the parent's own pass writes, and does not apply rules today, so step 3 extends it to the
     parent's rule-determined columns. A return's sale customer is then the ticket's, as step 2
     generates it.
   - **Foreign keys.** A rule that determines a foreign key generates only values the parent
     holds. The prototype moves a date down to the nearest key `date_dim` holds, which never
     moves it on a dense calendar. Step 3 allows such a rule only where its interval lands on
     keys the parent holds. Otherwise the row is traced as infeasible and keeps a key the
     parent holds, drawn as the foreign key draws today, never a violation drawn from the
     column's own statistics (EXPR-SPEC-2 section 7), which could name a missing row.
   - **New statistics.** `fluid profile` gains two statistics over a declared relationship:
     the share of joined rows on which a rule holds (a band, as EXPR-SPEC-2 already expects),
     and the histogram of a declared expression over the joined rows, on declared bins, which
     the bounds form draws from. The expression is a column less a parent value or a column
     divided by one, with its rounding stated, so that FLUX can invert it. The prototype draws
     a whole quantity inside the bin chosen, not a share rounded up, because a share rounded
     up lands in the next bin.

Every statistic over a relationship is a frequency table under these checks. This record is
where they are written down; they follow the checks chosen for bin-rounded quantiles.

- Each cell holds at least 10 joined rows and at least 10 distinct people. The contract names
  the column that identifies the child's person (the return's customer here). A cell whose
  rows name no person counts rows only.
- Counts are rounded to a multiple of 5, and a cell with no rows is disclosive, so a declared
  bin with nothing in it withholds the table and the owner declares other bins.
- Each table covers the joined rows on which what it reads is known, and is split by nothing
  else, one table per rule or expression.
- The rows a table leaves out are cells too. Across the tables of one relationship, every
  pattern of tables a row falls out of is checked as one more cell and never written:
  - a null in what a table reads;
  - a child that joins no parent.

  This is the profiler's rule for the records a `groupBy` null leaves out, carried across
  tables.
- One failing cell withholds every table of the relationship (D), and FLUX then generates
  those columns from their own statistics, as today.
- Each expression has one declared set of bins, fixed across releases, and a relationship's
  statistics are published under the profile's own window and differencing rules, so neither
  a second set of bins nor a second profile of the same rows can be subtracted from the first.

Option B is not taken now. It is the next step if, after these three, the remaining error is
mostly a fact's keys ignoring their parents' attributes. The measure is the queries that group
by an item's or a store's attribute, once those levels are published.

## Consequences

- A contract with rules gets a new contract digest, and every stream it seeds moves. That is
  ADR 0008's existing rule for any change to a contract. A contract without rules keeps its
  bytes.
- Every step changes the engine. Step 1 amends ADR 0008's list of extensions only for the
  columns FLUID SQL/1 cannot state, and only if they go through flux-expr/3. Step 3 changes
  EXPR-SPEC-2, PROFILE-SPEC and `fluid profile`. Each step lands on its own, with the harness
  re-run on it, and the prototype is deleted once step 3 has landed, so the harness then
  measures FLUX's own output.
- A synthetic return now carries its sale's customer as often as a real one does. That is a
  pattern of the source, carried by one published share, not a copy of any record. The
  privacy judge is re-run after step 3, because a person's sales and returns now agree with
  each other and linkage tests have more to work with.
- What stays out of reach is outside these rules: a high-cardinality string column still gets
  made-up levels unless its levels are published, a small dimension such as twelve stores
  stays below every cell minimum, and a column still follows only the statistics the profile
  publishes for it. Those belong to the profile's own statistics (declared bins and levels,
  grouped statistics), not to relations.

## What would change this decision

- If the harness shows little gain from a step on another seed, scale factor or schema, that
  step waits, and the order changes.
- If the maintainers want a formal privacy guarantee in place of output checking, option B
  becomes the path, since Private-PGM's estimator is built for noisy marginals.
- If grouped answers stay far off after step 3 because a fact's keys ignore their parents'
  attributes, option B's conditioned picks come next.
- If FLUID SQL gains a date part, date arithmetic and string functions, step 1's remaining
  columns move into the contract with the others.
- If a relationship has no column that identifies a person, its statistics count rows only,
  and the owner decides whether that is enough before they leave.

## Amendment of 2026-11-18: step 3's specification

Step 3 is specified in EXPR-SPEC-2, section 6 ("Rules over a relationship") and section 7, with the
two FLUX settings it adds in SEAM-FIDELITY-SPEC section 1. The engine refuses its rules with `FLX-6210`
until it implements them. The specification makes seven choices that the Decision above left open,
each decided by the maintainers, 2026-11-18.

1. **How far a rule reaches.** A contract's rule reads one hop, `<relationship>.<column>`, as the core
   writes it, and a FLUX-side rule may follow a chain of to-one relationships, a one-to-one
   relationship over several columns included. Every expose a path crosses must be generated by the
   same simulation, and the graph of exposes and relationships that the simulation's rules read
   through must have no cycle, a relationship of an expose to itself included. The alternatives were
   one hop only, and a graph of columns rather than exposes. A chain is the same parent read repeated,
   so it costs nothing more to allow. A graph of columns would also allow some cycles, such as an
   employee's rule reading its manager's own drawn hire date, but a cycle of exposes can need a row
   before it exists, and step 3 does not need that reach.
2. **NULLs come first.** A column that a rule over a relationship determines draws its own NULL first,
   at its declared null fraction, and the rule applies to the rest. The column keeps its declared null
   fraction, which is the point the Decision left to step 3, and the rule is written as it holds on
   real data, its NULL rows counted among the violations of its band. The alternatives were to apply
   the rule first, which writes a value over every NULL and moves the column's null fraction to that of
   what the rule reads, and to name the NULL in the rule's `when` (`c IS NOT NULL`), which the FLUID
   validator counts as the rule reading its own column, a cycle.
3. **A foreign key a rule determines** must name a row the parent holds. A copy of a foreign key to
   the same key names the row its source names, whatever the key's layout. Over an ordered key, a
   bounds form draws among the keys inside its interval, and any other form writes its value only
   when it is a key; otherwise the row keeps the key its foreign key drew and is traced as infeasible.
   The alternatives were the prototype's move to the nearest key below, which the maintainers chose for a
   dependency's `to` column in step 2, and inverting an identifier key's permutation to find a row. A
   dependency's `to` value is a draw from the foreign key's own statistics, and the nearest key keeps
   their distribution, but a rule's value is the relation itself, and a moved value breaks it. The
   inverse is possible, since a Feistel network runs backwards, and waits until a contract needs it.
4. **The bounds draw relative to a parent value** is over the column's own values, with one
   `uniform53`: a bin by thresholds `floor(2^53 * cumulative weight / total weight)` on exact
   rationals, then the value in order inside the bin, with a clipped bin keeping its share of the
   weight. A published rounding becomes the edge its bins are closed on (`closed: upper` for a ceiling
   of tenths). The alternatives were the sketch in the Decision, which draws the expression's value
   and turns it back into the column with a stated rounding, and two draws, a bin and then a value, as
   a column's histogram takes. A rounded inverse can land in the next bin, which is the failure the
   prototype's own note records for shares rounded up, and one draw is exact, monotone in its uniform,
   and always inside the interval.
5. **Shares below 100 %.** Step 3 generates section 7 for every rule that generates, those reading
   only their own row included. A violation of a foreign key names a row the parent holds, drawn as the
   foreign key draws, and the NULL rows count towards the target. The alternative was to refuse a band
   of positive width, as the maintainers chose for dependencies in step 2. For a rule over a relationship
   the share is what the step is for: a return's customer agrees with its sale's on a share of the
   rows, not on every row. Section 7's design is already written, and dependencies keep their refusal
   until a change of their own takes it up for them.
6. **Fixed draw positions.** On both kernels a rule's stream has fixed positions: the hold draw, then
   the value's sampler calls, then the candidates, each taking as many positions as one candidate's
   draw. Under kernel `0.2.0` the rule's stream is keyed by the rule with the row in its counter, so
   its draws for a slice are one call. The alternative was positions that follow what a row needed,
   which would make a later draw's index depend on an earlier draw's outcome, which KERNEL-SPEC-2
   rules out.
7. **Rules row by row.** Rules are evaluated row by row after the row's own cells on both kernels,
   and the column path supplies the rows that paths reach in bulk. The alternative was to evaluate
   rules as arrays. ADR 0010 keeps derived columns per row, and a copy taken as a gather can come
   later if a profile asks for it.

The implementation lands after step 2, which changes `generator/relations.py` too, in three changes,
each with its tests and its specification text moved from design to implemented:

- **3a, reads over a relationship.** `derive` and difference forms that read links; the rows that paths
  reach, with their own dependencies and rules; NULLs first; foreign keys a rule determines; the
  coverage report's `copied` by `rule`; the reading expose's digest covering what it reads; both
  kernels, the column path's bulk reads, and the same bytes for any `--jobs`.
- **3b, the bounds form.** Section 6's draw over a relationship and the row, the `distribution` setting
  and its one-uniform draw, with a vector file for the draw.
- **3c, shares below 100 %.** Section 7's targets, candidates and traces for the rules that generate,
  with violation counts in the evidence pack, and `fluxsim init --from-profile` writing `distribution`
  and `target` from the relationship statistics that PROFILE-SPEC and `fluid profile` add.

The AWS harness validates a run after 3a and again after 3c, the privacy judge is re-run after 3c as
Consequences above requires, and the prototype is deleted once 3c has landed.
