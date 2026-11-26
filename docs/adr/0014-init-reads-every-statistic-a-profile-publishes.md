# ADR 0014: init reads every statistic a profile publishes

## Status

Accepted 2026-11-19. On a gap in the profile, `fluxsim init` assumes by default,
labels each assumption and never draws a column as one value; `--strict` refuses
the gap instead. The maintainers chose this over refusing by default: one command
still gives a bundle that runs, and the ledger, the register and the evidence
make every assumption visible.

## Context

`fluxsim init --contract C --profile P` writes a bundle from a FLUID contract
and a profile of aggregates (PROFILE-SPEC), and no record. In 0.7.0 several
published statistics produced a bundle that a person would not recognise as the
data, or were dropped, and nothing said so:

- A lone published order statistic won over everything else: a column with a
  published `min` and a mean was drawn with the `min` on every row.
- `sum` and `count` without `distinctCount` put the mean on every row, and a
  column with nothing published was 0 on every row. A foreign key with one of
  these was under one parent.
- A level histogram counted only when its levels were exactly the contract's
  `enum`. A profiler lists a declared level only when it holds records, so one
  missing level sent every level back to equal weights.
- Grouped statistics, and the profile's `relationships` (children per parent),
  were never read.
- A column a contract `derive` sets was given its own profiled range, and
  `verify` refused the run (`FLX-5005`) once a property read that column.
- An optional categorical column whose null count was withheld was written with
  `nullFraction: '0'`, which keeps it from being the parent of a `given`
  (`FLX-3402`).

## Decision

### 1. The ledger

init records every statistic and metric of the profile, by its JSON Pointer
(RFC 6901), as one of:

- **used**: a number of the bundle rests on it, or a rule read it to bound or
  shape one, or init checked the bundle against it;
- **unused**, with the reason;
- **withheld**: the profile publishes no value (a status other than `F`).

init prints the counts and every unused pointer with its reason. The bundle
records the unused ones in the Simulation's register, under the engine's
namespace key `unused`, each with its pointer, its definition and the reason
(PARAMETER-REGISTER-SPEC, format 1-2-0). `apply` copies them into
`run/parameter-register.json`; `plan`, `verify` and `ship` count them in their
`parameters:` line, and `ship` names each one under "What Was Not Proven" in
`EVIDENCE.md`. A register names format 1-2-0 only when the Simulation
declares `unused`, so every existing record keeps its bytes and its digest.

### 2. Strict mode

`fluxsim init --strict` writes nothing and exits 1 when a statistic is unused
or when the bundle rests on a gap: an assumption that stands in for something
the profile did not publish. The settings of the check (each tolerance, the bounds
on a committed state's size) are assumptions too and stay labelled as such, but
no profile publishes them, so they are not gaps. `--allow POINTER`, repeatable,
exempts one profile statistic or one register pointer. An `--allow` that
exempts nothing is refused as well, so an allow-list cannot go stale.

The shape is mypy's `--warn-unused-configs` and `--warn-unused-ignores`, and
dbt's `--warn-error-options` with its per-event `silence`. Terraform's silent
acceptance of an unknown `TF_VAR_` name is the behaviour this avoids.

### 3. Assumption rules: never one value

A column is drawn with one value only when its profile shows one value. When
the profile leaves a gap, init assumes, says what it assumed and why, and the
assumption is a number of the register with its reason.

- **Keys and foreign keys** are never one value. A foreign key the profile gives
  no spread for is drawn uniformly over the parent's keys.
- **A key is read as the rows' own layout.** A key's values are the row count's
  consecutive grid values laid evenly. With its `min` or `max` withheld and a mean
  published (`sum` with `count`, or `mean/1`), init centres them on the mean. In every
  path a published `count` or `distinctCount` equal to the rows is checked as one value of
  the key per row, a published mean over a published `min` and `max` is checked against
  their middle, and each ungrouped `quantile/2` is checked against the even layout: the
  keys below each of its two edges, within the profile's rounding (the disclosure rule's
  `roundingBase`, plus one). A statistic the layout does not hold stays unused, and the
  reason says what disagrees. The checks run once the keys' spans are final, against the
  span the bundle declares, and replace what an earlier span gave.
- **A lone order statistic is a bound.** It is combined with the mean (from
  `sum` and `count`, or `mean/1`) and `distinctCount`, never drawn on its own.
- **A mean with no spread is filled by maximum entropy.** Among the
  distributions on a known span with a known mean, the one of largest entropy
  is the truncated exponential, `p(x) ∝ exp(λx)`, and on the integers from a
  bound it is the geometric (K. Conrad, "Probability distributions and maximum
  entropy", theorems 5.1 and 5.8). init lays equal-width bins over the span and
  weights bin `k` by `r^k`, with `r = exp(λ·width)` found by bisection in exact
  fractions so that the mean as the generator draws it is the published one. A
  published bound is one end of the span. The other end is a second published
  bound, else the point past which the geometric tail holds less than half a row,
  else, with no bound at all, 0 on the side of zero the mean lies on, stated as
  an assumption. With a published `distinctCount` and no bound, the 0.7.0 rule
  stays: an even span centred on the mean, as many values wide as
  `distinctCount`.
- **A binned quantile is two points.** A `quantile/2` group says `binBelow` of
  its `k` records lie below one edge of its ladder and `binThrough` below the next,
  so it gives the column two points of its distribution, level `binBelow / k` at
  the one edge and `binThrough / k` at the other, as a published quartile does. The
  ladder's first and last edges say nothing of where the values lie and give none.
  Counts are rounded, so two points can disagree: a level never falls below the one
  before it. When `min` or `max` is withheld and the outermost point on that side is
  a binned quantile, the rows beyond it are no longer put in the outermost bin. A
  number column with a published mean and one end withheld fills that part by
  maximum entropy, with the mean the rest leaves. Otherwise the part lies in the next
  bin of the profile's own ladder, drawn evenly. Each end is an assumption, stated
  as one. A published `distinctCount` is checked against the bins' span.
- **A contract's range bounds the draw.** A column's `validationRules` of type
  `range` (`between A and B`, or `>=`, `>`, `<=`, `<` joined by `and`; several rules
  hold together, the tightest bound on each side) are read exactly, and a strict bound
  is not reached: the first grid value inside it is. No bin, level or
  maximum-entropy tail of a drawn number column reaches past it; the range the check
  reads and a property's bins keep the profile's edges, which the check compares. A bin left with no grid
  value is dropped and the weights scaled, stated as an assumption only when weight is
  lost. A range that leaves the profile no weight is drawn evenly within what it leaves,
  and one that leaves a single grid value gives that value as the column's one value. A
  range that lies wholly outside the profile's values draws evenly over the range, cited
  to the contract, and every statistic the draw had read is unused: the profile's values
  lie outside the range the contract declares, so none of them shapes the draw. The
  range is held to what the column's type holds; a range beyond it keeps the placeholder,
  and an assumption says so. A column with nothing published draws evenly over the
  declared range, cited to the contract.
- **Nothing published** (and no contract range) gives a placeholder that is not constant: an even
  spread over the first hundred values of the column's grid from 0 (0 to 99 for an
  integer, 0.00 to 0.99 for two decimal places, within the declared precision), or
  the run's time window for a date or timestamp. The evidence lists every
  placeholder.
- **Levels.** A declared level the histogram does not list, and a withheld cell,
  share the remainder the published counts leave: the column's non-null rows
  less the sum of the published cells. A cell withheld as small (status `A`)
  takes no more than half the minimum cell. Only when the remainder cannot be
  known (no row count is published) does the 0.7.0 rule stay: the mean of the
  published cells, or half the minimum cell for `A`. A level is never assumed to
  be zero: under the disclosure rules a zero is withheld like any small count.
  Every message names the levels it assumed.
- A categorical column is written with no assumed `nullFraction`. An absent
  `nullFraction` already draws no null, and a declared one, even `'0'`, keeps
  the column from being a parent (`FLX-3402`).

### 4. Grouped statistics become `given`

A statistic grouped by a categorical column of the same table is a two-way
table of counts. init scores every pair by its mutual information and keeps the
maximum spanning forest under the generator's rules (Chow and Liu 1968,
`flux_engine/calibrate/structure.py`): at most one `given` per column, no cycle,
a histogram column only as a leaf, and a categorical parent with no
`nullFraction`. The chosen child declares `levelWeightsGiven` or
`binWeightsGiven` from the group counts. Each withheld cell is filled by the
level rules above, within its group. A parent level the table holds no group
for draws the child's own shares, stated as an assumption. Every grouping the
forest leaves out is unused in the ledger, with the grouping that won.
A column drawn given another from a table grouped by it takes its weights from the
table's groups, so its own level histogram is no weight of its own. It is checked against
the groups' sizes, within the profile's rounding: agreeing, it is used; else unused, and
the reason names the counts that differ.

### 5. Children per parent become key spreads

A profile `relationships` entry describes how many child rows each parent row
has. For a one-column foreign key, init lays the count classes of its
histogram out over the parent's key range, in order: each class takes a share
of the range equal to its share of the parents, and the foreign key's draw
weights each class by its share times its mean number of children. The class
of parents with no child gets weight 0. The generator then snaps each drawn
value to the nearest parent key (ADR 0011).

This is an approximation, and documented as one. A parent in a class whose
mean is `m` receives a Poisson-like number of children, so about `e^-m` of
them receive none, and the childless share comes out larger than published by
about that much; exact counts need the parents drawn first, which is a later
change. A top-coded class takes the mean that makes the relationship's
published mean hold, or its lower bound when none is published. A relationship
that publishes only its mean gives an even spread, the 0.7.0 behaviour.

A foreign key that gets a spread from a relationship reads its own statistics too. With
no histogram of its own, its binned quantiles cut the parent's keys as a histogram's
bins would, and the classes are laid out inside each cut. Its mean marks its source
used, a `mean/1` beside `sum` and `count` among them when it agrees. A published `min`
or `max` is checked against the parent's key span: inside it, used; outside it, unused
with the span in the reason. The reason that a foreign key with no spread is drawn
evenly stays on the foreign keys that have none.

A foreign key the profile gives no relationship for reads its binned quantiles like any
other number column: with its other order statistics they cut its draw, and a `min`, `max`,
mean and `distinctCount` are checked against the result. A column drawn given another from
a grouped table also checks its own level histogram against what the table's groups add up
to.

### 6. Columns a derive determines

A column a contract `derive` sets is not drawn. init declares no spread for it
and never its own profiled range. Its range, when a property needs one, comes
from exact bounds of the rule: an interval in exact fractions evaluated over
the expression's syntax tree, with the ranges of the columns it reads. The sum,
difference and product of two intervals have exact interval results, and
`ROUND` is monotone, so its bounds are the rounded ends; an expression the
interval cannot follow gives no range. The bounds rest on the spans the bundle has for the
columns the rule reads, which can be assumptions, so a published `min` or `max` of the column
beyond them widens the range: the check reads the profile's own values against it too. The
column's own statistics are checked through the rule: they become properties that `verify`
scores against what the rule produced. A column a contract `dependency` sets is still drawn,
once for each value of the columns it follows, so its mean rests on a few draws and it gets
no mean property.
Each published, ungrouped `quantile/2` of such a column is checked against the range the
rule gives: it is used when the bin that holds the quantile overlaps that range, and unused
with the reason when the bin lies outside it or the rule gives no range. It gets no
`quantile` property, because the column is measured through the inputs the rule reads,
whose bins do not line up with the column's own ladder, so the quantile can fall on either
side of an edge. A published `distinctCount` is checked against the grid values the declared
range holds. The rule's values are not clipped to a range the contract also declares for the
column; when the rule's bounds pass it, an assumption says so, and a binned quantile's bin is
checked against the intersection of both ranges.

### 7. DATE in one format

A DATE bound is spelled `YYYY-MM-DD` everywhere a fidelity block declares one,
and the check reads a profile's DATE statistics, which the profile encodes as
timestamps in milliseconds, as days. The profile format does not change.

## Consequences

- No kernel behaviour changes and no golden digest moves: everything here is
  authoring, plan-time reporting and the check's reading of DATE bounds. A
  bundle written by 0.7.0 init still validates and runs as before.
- For the same contract and profile, init writes a different bundle than 0.7.0
  did. Its register counts change with it.
- The key spreads are an approximation, and the ledger, the register and this
  record say so.
- `--strict` gives a pipeline a gate on the profile: a new statistic nothing
  reads, or a new gap, fails the build rather than drifting into the bundle.

## Considered and rejected

- **Refuse by default, with a flag to accept gaps.** It turns every profile with
  a withheld cell into an error, and the first run of a new user into a
  configuration task.
- **A fitted joint model (Private-PGM's successor, mbi).** It meets several
  cross-tables at once, but needs JAX and Python 3.11 in the statistics
  package, which supports 3.10 and imports the standard library only. A spanning
  forest of one-parent conditionals is what the generator draws today.
- **scipy for the maximum-entropy fill.** The engine's core is exact-rational
  and imports the standard library only. The fill is about forty lines.
- **mpmath's `iv` or `portion` for the interval bounds.** `mpmath.iv` keeps
  floating-point endpoints, and `portion` is LGPL-3.0. The interval over the
  engine's own syntax tree is about thirty lines of fractions.

## Prior art

- K. Conrad, "Probability distributions and maximum entropy",
  <https://kconrad.math.uconn.edu/blurbs/analysis/entropypost.pdf>.
- C. K. Chow and C. N. Liu, "Approximating discrete probability distributions
  with dependence trees", IEEE Transactions on Information Theory 14(3), 1968,
  <https://doi.org/10.1109/TIT.1968.1054142>.
- mypy, `--warn-unused-configs` and `--warn-unused-ignores`,
  <https://mypy.readthedocs.io/en/stable/command_line.html> (MIT).
- dbt, `--warn-error-options` and `silence`,
  <https://docs.getdbt.com/reference/global-configs/warnings> (Apache-2.0).
- Misata (MIT), <https://pypi.org/project/misata/>: a multi-table generator from
  stated facts that refuses a conflicting declaration with the arithmetic shown,
  a pattern only.
- NIST SDNist's k-marginal score, <https://github.com/usnistgov/SDNist>: the mean
  total variation over k-way marginals, used by the round-trip harness under
  `e2e/` and nowhere in the engine.

## Amendment of 2026-11-26: a date the profile gives no distribution is drawn where the contract's derives hold

### Context

A column the profile gives no distribution is drawn evenly within the run's window (the
placeholder rule above): a private release that publishes nothing for a table leaves a DATE
column as a bare `timestamp` domain. A contract may derive another column from that date, as the
whole months or days from a fixed date, and declare a range for the derived column. An even draw
over a window of several years writes the derived column outside its range on most rows, and
`verify` reports them. A date column's own declared range already cuts its draw, so only the
range of the derived column was missing.

### Decision

1. **The family.** A `derive` (with no `when`) whose text the engine's parser reads as
   `DAYS_BETWEEN`, `MONTHS_BETWEEN` or `YEARS_BETWEEN` of a date literal and one column of the row,
   in either argument order, bounds that column when the derived column declares a `range` rule.
   Any other shape (a sum, a rounding, a column of another row), a derived column with no declared
   range, and a column the profile publishes a distribution for change nothing.
2. **The bound is the preimage.** The difference is a monotone step function of the date, so each
   end of the dates whose difference lies in the range is found by bisection over the day ordinals
   with the engine's evaluator, and then checked with it: the difference lies in the range at each
   end, and the day beside each end does not. The engine's whole-unit semantics decide, for a
   month difference as for a day one: the count truncates toward zero, so a date up to a month
   before the fixed date has a difference of 0 in either argument order.
3. **What `init` writes.** The column is declared with one bin over the bound, weight 1, cut to
   the run's window and to the column's own declared range, as an assumption whose sentence names
   the derive and the range; the report says so. Two derives over one column intersect. When the
   bound misses the run's window, the column is drawn within the bound, since the contract's range
   decides, and the sentence says so. When the intersection is empty, when a bound has no end to
   draw to inside the window, or when one date is all that holds, the column keeps its even draw
   and the sentence gives the reason. A column whose released histogram is noise everywhere (ADR
   0019, amendment of 2026-11-25) is bounded the same way.

### Consequences

- Only an authoring change: no kernel behaviour or golden digest moves, and a contract that
  declares none of this gets the bundle it got before.
- A bin's upper edge is a date the draw can reach, so the bin runs to the last date that holds.
