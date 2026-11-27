---
title: "How init reads a profile"
description: "The rules fluxsim init applies to a contract and a profile, statistic by statistic: what each published number becomes in the bundle, which register source it carries, and the rules that shape the assumptions."
---

# How init reads a profile

`fluxsim init --contract --profile` turns the statistics of a profile and the facts of a contract into the numbers of a bundle. This page is the lookup of those rules. The steps that use the command are in [Write a bundle from a profile](../how-to/write-a-bundle-from-a-profile.md), and the rules for a profile released under differential privacy are in [Write a bundle from a private release](../how-to/write-a-bundle-from-a-private-release.md).

## From the most the profile says to the least

The rules run from the most the profile says to the least. Each row is one rule, and the last column is the `source` the register gives the number it writes.

| The profile or the contract says | The bundle declares | Source in the register |
|---|---|---|
| a table's `rowCount` | the emit's `rows` | profile |
| a column's `min` and `max` | its `range`, which the fidelity check holds the run to | profile |
| `min`, the quartiles and `max` | histogram `bins` and `binWeights`, a quarter of the rows in each | profile |
| a mean: `sum` and `count`, or a `mean/1` statistic | the histogram shaped to that mean and, for a `mean/1` over a number column with a published range, a `mean` property the run is checked against | profile, with the shape assumed |
| a binned quantile (`quantile/2`) | two points of the column's distribution for the histogram: where the profile withholds `min` or `max` beyond them, a tail from the published mean or the profile's next bin out, each assumed | profile, with the tail assumed |
| `nullCount` | `nullFraction` | profile |
| a histogram, level counts or a list of levels | `bins` and weights, `levels`, and a property the run is scored on | profile |
| a histogram or level counts grouped by a categorical column | `given` that column, with `binWeightsGiven` or `levelWeightsGiven` from the group counts, and a grouped property | profile |
| the same, grouped by an INT column the profile lists at most 64 whole numbers of (a `levelHistogram/1` or `support/1`) | that INT column declared as a categorical over those integer levels (canonical decimal text, each inside the contract's range), then `given` it as above | profile |
| a histogram or level counts `per` the columns a contract `dependency` follows, when those columns are a key of the table or a relationship's foreign key | the dependent column drawn from that statistic in place of the one that counts rows, no histogram or mean property for it (a run's properties count rows, the statistic counts entities), and a choice that says so | profile |
| children per parent of a relationship the contract declares (`relationships`) | a `counts` entry whose `children` are the histogram's classes, so each class of parents holds its published share of the parents; a relationship the layout cannot place keeps the foreign key's `bins` and `binWeights`, each class a share of the parent's keys | assumption, from the profile's counts |
| as many distinct values as values | `unique: true`: an identifier the engine derives | no number |
| an `enum(...)` type | `support: declared`, with `maxCardinality` its number of levels | contract |
| a key, in its `semantics` | no key of its own: the engine reads keys from the contract | no number |
| a `derive` in the contract's `invariants`, with no `when` | no draw, and a `range` from the rule's exact bounds over the columns it reads (or its published histogram's edges when they hold them), with its published statistics scored through the rule | contract, or profile for the edges |
| a `range` rule in the contract's `validationRules` (`between A and B`, or `>=`, `>`, `<=`, `<`) | the bound on the bins, levels and tails of a drawn number column; a column with nothing published draws evenly over it | contract |
| a `dependency` in the contract | the column keeps its draw and gets no `mean` property, since the run draws it once per value of the columns it follows | contract |
| nothing, or a withheld value | an assumption, stated with its reason | assumption |

## What shapes the assumptions

These rules shape the assumptions:

- A column's `range` and `support` are what the fidelity check holds the run, and a
  slice of real records, to: a value outside them is refused. So they are declared only
  from a published value or the contract. A shape that rests on an assumption, such as
  the opaque `country` levels, is declared with keys only the generator reads. One exception: a fidelity block needs a property. In a table where no column publishes
  a range or its levels, one column's assumed span or levels is declared for the check,
  and the output says so.
- A count withheld as a small one (status `A`: a bin, a level or the distinct values)
  lies below its rule's minimum cell. So it is assumed to be half of that cell: 5, under ACRO's
  default of 10. Any other status says nothing of a count's size, so a cell withheld
  under one is assumed to be the mean of the cells its table publishes. A withheld null
  count is the exception. It is most often a zero, which the checks withhold as
  disclosive, so no nulls are drawn rather than nulls invented in every column.
- A number key lies between its published `min` and `max` when they hold one value per
  row. Otherwise its span is assumed, 1 to the row count. It is widened to the values
  the foreign keys that reference it are declared over, or moved inside the key it
  references when that key's span is published, down a chain of keys. A foreign key the
  profile says nothing of is drawn over the span of the key it references.
- Grouped statistics are read as two-way tables. Each pair of columns is scored by its mutual information. The tree of the most informative links that makes no cycle decides which column is drawn given which. A column has at most one `given`, a number column only at the end of a link, and never given a column that holds nulls. A grouping
  the tree leaves out is unused, and the output says why. An INT column the profile groups
  another column by, and lists at most 64 whole numbers of, is a categorical over those
  levels for this purpose. A statistic grouped by a number column of the profile (format
  `1-3-0`) is listed as unused: a number column publishes bins, not levels.
- A table the profile publishes whole under `references` (format `1-3-0`) is written as a
  reference expose: `reference: true` and nothing else, so its rows are the published ones. A
  foreign key into it declares the levels the profile counts it over (the child rows of each
  catalogue row). It does so when they are keys of the catalogue. A foreign key whose statistic is over
  other values declares none. So it picks a catalogue row uniformly, and its statistic is
  unused with that reason. A statistic of a column grouped by a column of a linked row
  (`groupByLink`) is read as a two-way table. The column is drawn
  `given: <relationship>.<column>` with the grouped counts as its weights. It is scored by a
  property grouped by the same link. It is unused, with the reason the generator would refuse it for, in these cases:

  - the contract declares no one relationship over the grouping's columns;
  - the foreign key may be NULL (a NULL has no level to draw given);
  - a rule sets the column;
  - the column is already drawn given another;
  - the parent column is not text or not a categorical drawn from its own levels; or
  - the rows would read each other in a cycle.

  See [Publish a catalogue whole](../how-to/publish-a-catalogue.md).
- A trait that is stable per entity, such as a customer's segment on each of that customer's
  rows, is a contract `dependency` from the entity's key. `fluxsim init` never writes the
  contract, so declare the `dependency` there. Where it is declared and the profile publishes the
  column's statistic `per` that key, the column is drawn from it, because the dependency draws
  once per key. A statistic `per` columns that no dependency of the contract follows is listed as
  unused, with that reason.
- Children per parent are laid out exactly where the counted layout can place the relationship
  (see [Give every parent its children](../how-to/give-every-parent-its-children.md)). `init` writes a
  `counts` entry whose `children` are the histogram's classes, so the run's histogram of children
  per parent is the published one, class for class, for a key of any type. The class of parents
  with no child is read as published, a zero included. A relationship the profile marks
  `structural: [childless]` (the source enforces at least one child per parent) is written as
  `minChildren: 1`, and its class of none takes no weight. `orphan` is listed as checked, since
  every foreign key finds its parent. A `one_to_one` relationship is laid out the same way, with `levels: ["0", "1"]` weighted by the
  profile's classes of none and of one. A histogram with parents in a class above one keeps the
  spread, and `init` says so in a note. A relationship the layout cannot place keeps the
  approximation. This holds when the relationship is a key or part of one, or a relationship to its own table.
  It also holds for a foreign key with a null share, and for one with a draw or a contract rule of its own.
  Its foreign key spreads each class of parents over the parent's keys.
  A parent of a class whose mean is `m` receives a Poisson-like number of children, so about `e^-m` of them receive none.
  The run's share of parents with no child comes out that much larger than published.
- A relationship's histogram of children per parent can be grouped by a `category` column of the parent, or by the
  bins of a `number` or `timestamp` one (profile format `1-4-0`). Such a grouped histogram becomes the `given` and `childrenGiven` of its
  `counts` entry, beside the `children` of the ungrouped histogram or, failing that, of the sum of the groups. The
  classes are the column's declared levels, or its bins keyed by the text of their lower edges, and the
  statistic's bins must be the entry's. A class the profile has no group for takes the shape's own weights, and
  the bundle says so. A column with a null fraction, one a summary over the children sets, a key, a column of a
  rule, or one drawn given a linked row cannot be given. The ledger says why, and the entry has no `given`.
  A relationship's noisy histogram is read as its point estimate, as a column's is, with its noise in the ledger.
- A table whose profile publishes `kendallTau/1` gets one `fidelity.copula`. The same holds for `spearmanRho/1` when no Kendall value is published for
  any pair. The copula covers the number and timestamp columns that `init` draws from bins and
  weights, with no `given` and no `allocation`. It leaves out any column that a clique, rule, key, relationship, history or date
  entry decides (see [Draw number columns together](../how-to/draw-number-columns-together.md)). A column has one
  joint, so what the cliques draw stays theirs. A noisy value is written as the released number clamped to
  `[-1, 1]`. A pair the profile did not publish is held at 0 and said so as an assumption. Each pair is
  scored by a `rankCorrelation` property. A column is left to the cliques when it ties often. A column is
  tie-heavy when the sum over its bins of the squared share of the bin divided by the number of grid values in
  it is at least 1/20. The reason is that the statistics carry no tie correction. The rule reads the declared bins and the
  grid only, and the report names each column left out. Under kernel 0.1.0 no copula is written.
- A column of a `cliqueMarginal/2` clique that holds numbers or timestamps is drawn from `bins` and `binWeights`,
  or `binWeightsGiven` its separator, over the clique's edges. These edges win over the column's own histogram and
  make its range the span of the edges. A clique whose edges do not cover the range the contract declares, or
  that holds a column with nulls, is refused with [FLX-3402](./errors/FLX-3402.md).
- Every distance tolerance `init` writes declares `sampling: true`. So the check widens its
  margin by the sampling error of the run's rows, and a small table is not judged by a margin only
  a large one can meet. A table is small when that widening at its rows, for any property `init`
  writes for it, exceeds the default tolerance. Every column of a small table that `allocation: exact` can read then declares it, so each class holds exactly its share of the rows. Such a column is drawn from `levels`, from `bins` with `binWeights`, or given a column that is itself exact. A key, a foreign key and a column a rule sets are left as they are. A bundle that
  declares either key plans only under kernel 0.2.0, the default, and kernel 0.1.0 refuses it
  with [FLX-3105](./errors/FLX-3105.md).
- A column of a key over several columns spans its rows the same way, and a text one
  takes as many opaque levels as rows, so the key can hold one value per row. A key's
  columns declare no `nullFraction`, nor does an `inner` foreign key, and a composite
  foreign key takes its nulls from its first column alone.
- A histogram's outer edges are often round numbers beyond the data. Its draw keeps to
  the published `min` and `max`, while the check reads the histogram's own edges.
- Drawn evenly within its quartile bins, a column would average a value away from the published
  mean: `amount` in the worked example would average about 40, and the profile says 46.25. So
  within each quartile bin the values are drawn from the part on the mean's side. Each bin
  keeps its quarter of the rows, so every published quartile holds, and the mean is the
  published one. The cuts this adds are assumptions in the register. The worked example of
  [Write a bundle from a profile](../how-to/write-a-bundle-from-a-profile.md) shows it.
- The World and the Persona are the least a run needs. Their numbers are `default` in
  the register: the generator emits read none of them.

## The time window

A timestamp bound is RFC 3339 text and a date bound `YYYY-MM-DD`, which the register
does not count, so an assumed time window is in the command's output and nowhere else. When no
time column publishes its range, because no single instant is held by enough rows (in the
worked example, ten orders), each such column is drawn within the run's `timeBounds`.

When some columns do publish a range, the run's `timeBounds` span all of them. They run from the earliest
start to the latest end of every date column's published range. They also cover the date range each date
column's contract declares (a `range` rule such as `between '2020-01-01' and '2026-12-31'` or `>= '2020-01-01'
and <= '2026-12-31'`), and the bins, levels and range each column is drawn from. No column's bins are cut. To
choose the window yourself, pass `--window START END`, two dates (`YYYY-MM-DD`, midnight UTC) or RFC 3339
timestamps with START before END. The Simulation's `timeBounds` are exactly those, a date column's bins that
leave them are cut at them (and the column says so), and the output notes the window under "time window". The
end column of a validity window whose contract declares no `openEnd` gives the window nothing from the year
9999 on, since that year spells an open end. The note names the column and says to declare the `openEnd` on
it.

## Association tables

A table of two foreign keys and no other column, laid out by counts on both sides, is written
as an association: see [give every parent its children](../how-to/give-every-parent-its-children.md#association-tables).
`init` gives it `distinct: true`, or `distinct: false` when the published rows are more than the
pairs of the two parents.
