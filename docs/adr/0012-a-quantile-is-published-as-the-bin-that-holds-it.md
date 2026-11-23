# ADR 0012: a quantile is published as the bin that holds it

## Status

Accepted 2026-11-18. The maintainers chose bins declared on the property over
bins implied by the tolerance, and the publication rule of decision 5,
"three cells, one partition", over the bin-only rule first proposed, which the
security review showed lets small cells out, and, for every count table,
one withheld code, `C`, over codes that name the check. After the fourth
security review, the maintainers also chose that a `quantile/1` is published only
alone on its column's edges, that a group withheld for its size withholds its
siblings and a level histogram is checked over its levels as over bins, and
that a cell of no records is protected where zeros are disclosive (decision
5). **Amended 2026-11-18:** the maintainers chose to leave outside the rule, and
to name in PROFILE-SPEC §3, two ways a profile says that a range holds no
record: the declared levels missing from a `support/1` or from a grouped
statistic's groups, and the part of the declared range beyond a published
`min` or `max` (see the amendment below).

## Context

A `quantile/1` group publishes the order statistic itself, the witness, with
its two ranks. A profiler publishes it only when at least `minimumCell`
records hold that exact value, the output-checking rule for an order
statistic, because a value held by one record is that record's value. On a
continuous column almost no value is held by ten records, so the rule
withholds almost every quantile, and a property whose every group is
withheld is censored: never scored.

The maintainers decided on 2026-11-18 to allow bin-rounded quantiles: publish a
quantile as the bin that holds it, and only when that bin holds at least 10
records. This record says what the profile document carries, how the
fidelity check compares the synthetic quantile with a bin, and where the bins
come from. The spec text is in PROFILE-SPEC §3, §7 and §8,
SEAM-FIDELITY-SPEC §2, §4 and §5, and SCORECARD-SPEC.

## Decision

1. **The bins are declared.** A `quantile` property may carry `bins`, under
   the rules a `marginalHistogram`'s bins follow (SEAM-FIDELITY-SPEC §2): at
   least two strictly increasing edges in the column's domain, which
   partition its declared range and land on the grid. A quantile property
   with bins is formed and compared by a new registered definition,
   `quantile/2`, on the committed-slice path and on the profile path alike.
   One without bins keeps `quantile/1`, unchanged. The bins are part of the
   declaration, so adding them moves `G`.
2. **What a group carries.** `quantile/2` takes the order statistic as
   `quantile/1` does, at index `j = ceil(level * k)`, and spells in its place
   the bin that holds it and two counts:
   - `quantileBin`, the index `i` of the bin `[e_i, e_{i+1})`, the last bin
     closed at the top, as a histogram's is;
   - `binBelow`, the records below `e_i`;
   - `binThrough`, the records below `e_{i+1}`, or all `k` for the last bin.

   The witness never leaves the boundary and never enters a profile.
3. **The bin is shown, not asserted.** The order statistic lies in bin `i`
   exactly when `binBelow < j <= binThrough`: the order-statistic rule of
   `quantile/1`, read at the bin's edges. The verifier checks it on the
   reference side, and checks the synthetic witness's own ranks as
   `quantile/1` does.
4. **The comparison is against the bin.** The synthetic side is the engine's
   own data, so its witness `w` is compared exactly. The property is within
   when `w` lies within `τ′` grid steps of the reference bin:
   `e_i - τ′ <= w <= t_i + τ′`, where `t_i` is the bin's largest grid value,
   `e_{i+1} - 1`, or `e_n` for the last bin. A synthetic quantile within `τ`
   of the reference quantile always passes. One up to a bin's width further
   can pass too: that slack is what the bins trade for publication, and the
   declaration chooses it.
5. **The disclosure rule.** A published `quantile/2` group states three
   cells of its bins: the records below its bin, in it and above it. It is one
   of the statistics of a column that count records between edges, with
   `marginalHistogram/1`, `levelHistogram/1` (its declared levels read as
   bins, one per level in order) and `quantile/1` (its ranks count at its
   witness). A cell is small when it holds 1 to `minimumCell - 1` records, or
   none when the rule holds zeros disclosive, as ACRO's default does: a
   withheld zero that a reader could subtract back would say what the zero
   rule keeps back. A profiler applies these checks, in this order:
   - **Three cells.** On unrounded counts, each of the three holds at least
     `minimumCell` records, or none at the ends of the range (below the first
     bin, above the last), which lie outside the declared range. Otherwise the
     group is withheld. A `quantile/1` group's records below its witness,
     holding it and above it are checked the same way, except that none below
     or above, which says that no record lies between the witness and the end
     of the range, is a cell of no records, allowed only where zeros are not
     disclosive. A histogram's small cell is withheld.
   - **One partition.** Every statistic of a column that publishes counts at
     edges, at every level and in every group, uses one declared partition:
     the bins of the column's first histogram, or of its first such statistic
     when it has none, or the column's declared levels. A quantile's bins
     equal the partition's or join adjacent ones of it. A statistic on other
     edges is withheld whole, because edges chosen one property at a time
     would let a reader walk a cumulative count across the column. A
     `quantile/1` counts at its witness, an edge no declaration fixes, so it
     is published only as the one such statistic of its column, and beside
     any other (a histogram, a `quantile/2`, another level, or the whole
     beside a groupBy) it is withheld: its ranks less a histogram's cells
     would give a withheld cell back, and two levels' ranks the records
     between their witnesses. A quantile checked beside them declares bins.
   - **A group's size.** Every statistic that holds a group publishes its
     size, so the column's count less its siblings' sizes gives a group's
     size back. A group of 1 to `minimumCell - 1` records, or of none where
     zeros are disclosive, is withheld, and so is every group of every grouped
     statistic of its groupBy, histograms, quantiles, means and supports alike.
   - **What a reader can form.** The profiler withholds more until no small
     cell is one a reader can form by adding or subtracting these statistics'
     published counts. Within one group, every
     published count ties two edges together (a cell its two edges, a
     `quantile/2` group the first edge and each edge of its bin, the group's
     size the first edge and the last), and a reader knows the records between
     any two edges tied together, whether or not either is tied to the first
     edge: a histogram's cell less a finer histogram's is one. So a published
     count that ties none of its group's edges to the first edge is withheld,
     since a reader could subtract it from what they know outright and count
     cells that are not next to each other. The groups of a groupBy add up to
     the whole column, which gives a group's count at an edge where the whole
     and every other group are known. And a groupBy whose counts still leave a
     group partly unknown is withheld whole whenever the column publishes
     another count at its edges, the whole's or another groupBy's, since
     groups known only together could still be subtracted from the whole.
   - **Then rounding.** The counts are rounded as the rule rounds counts, and
     a group whose rounded counts no longer show its bin
     (`binBelow < ceil(level * k) <= binThrough`), or no longer prove a
     `quantile/1`'s witness by its ranks, or whose positive count rounds to
     zero, is withheld.
   - **One withheld code.** Every group and cell of a count table
     (`marginalHistogram/1`, `levelHistogram/1` and `quantile/2`), and every
     group of a `quantile/1`, that an output check withholds is published
     `C`, whatever the check, and the
     profiler's own report keeps the reason. A code that named the check
     would tell a reader a range (`A`: fewer than `minimumCell` records),
     and a known total can turn a range into a count: cells of 10, 9, 9 and
     10 records published `F`, `A`, `A`, `F` give both withheld cells as 9.

   `flux_stats.edge_counts.check_edge_counts` is this rule, so a profiler
   applies it rather than implementing one of its own, once per column and
   once per relationship's count of children, with all of the column's such
   statistics in one call, since one checked alone is judged alone. It takes
   the sizes of the column's groups as known, since its means and supports
   publish them too, which is why the size rule reaches those. Where it must withhold one more
   count, it takes one next to the small cell, so that withheld cells stay
   side by side rather than leaving published ones between them. It refuses
   counts that cannot describe one set of records (groups that do not add up
   to the column, a group with two sizes), since it could not see what a
   reader subtracts. The edges are declared, so a published group tells a
   reader which declared band holds the quantile, and how many records lie
   below and through that band. A withheld value's status, `C`, says nothing
   of its size.
6. **The format and the score.** Registering a definition is a minor version
   of the profile format (PROFILE-SPEC §8). A profile that holds a
   `quantile/2` statistic names format `1-1-0`, and a reader accepts `1-0-0`
   and `1-1-0`. The scorecard scores a `quantile/2` group as
   `max(0, 1 - d / W)`, where `d` is the synthetic witness's distance in grid
   steps from the reference bin, zero inside it, and `W` the width of the
   declared range: `quantile/1`'s score, with the bin in place of the
   reference witness.

## Alternatives considered

- **The bin only** ("publish when the bin holds at least 10 records"), first
  proposed. The security review published 3 records below a bin as a rounded
  5 where a histogram over the same bins withholds that cell, recovered the
  3rd and 8th smallest values exactly by moving one edge across several
  quantile properties, and gave small cells back by differencing against a
  histogram's withheld cells, other levels and other groups.
- **Three cells only**, without the partition and the differencing check:
  several quantile properties on chosen edges still difference out small
  counts.
- **Bins implied by the tolerance**, of width `τ` from the declared minimum.
  No new key, but what a profile can publish then follows the margin: a
  change of `τ` changes the bins, and a domain's own bands, such as income
  brackets, cannot be used.
- **Bins the profiler chooses from the data.** The choice is a function of
  the data, so it discloses in its own right.
- **The bins of a `marginalHistogram` on the same column.** It couples two
  properties, and a quantile with no histogram beside it could not be
  published.
- **Binning the synthetic side too, and comparing bins.** The engine's own
  data is not withheld from it, and the exact synthetic witness against the
  reference bin is the tightest check a published bin allows.

## Prior art

- Statistics Iceland's SDC guidelines for research outputs (T3.3): a
  percentile is reported only when it represents at least `n` units, `n`
  from 5 to 10 after Eurostat's guidelines, and an extreme percentile may be
  capped, rounded or reported in intervals.
- ACRO 1.1.0's threshold rule for a cell (`safe_threshold`, 10) and its
  rounding (`safe_round_base`, 5), which the profile's disclosure rules
  already name (PROFILE-SPEC §2). A bin is a cell.
- The DwB output-checking guidelines class an order statistic as unsafe
  unless enough units hold it: the rule `quantile/1` follows, which this
  record keeps.

## Consequences

- `quantile/1` reads and compares as before, so every existing bundle and
  profile does, and no digest or golden moves. What changes is when a
  profiler publishes one: alone on its column's edges, with its three cells.
- `flux_stats.fidelity` reads `bins` on a quantile property, and
  `flux_stats.definitions` registers `quantile/2`. The registry picks a
  property's definition from its declaration, not from its kind alone,
  because one kind now has two.
- forge-cli's `fluid profile --bundle` writes `quantile/2` for a quantile
  property with bins once a flux-stats release carries it, and keeps
  `quantile/1`'s rule for one without.
- `fluxsim init --contract --profile` writes no quantile property today, so
  it is unaffected.
- `quantile/1`'s published ranks had the gap the three-cell check closes
  here: the records below and above its witness were not checked. Its ranks
  also count records at the witness, an edge the partition does not have, so
  beside a histogram or another quantile of the column it is now withheld.
- A grouped histogram or quantile with one small group is withheld whole, as
  forge-cli's means and supports already were, and a level histogram with one
  small level loses a neighbouring level too.

## Amendment of 2026-11-18: what lies outside the rule

Two ways a profile says that a range holds no record publish no count that
`check_edge_counts` sees. Where the rule holds zeros disclosive, each still
tells a reader of a cell of no records. The maintainers decided on 2026-11-18 to
document both as outside the rule rather than extend the rule to them.

1. **Levels that hold no record.** A `support/1` lists the members a
   categorical column holds, and a grouped statistic lists its groups, so both
   say which declared levels hold no record. The rule withholds a level
   histogram's count of no records where zeros are disclosive, but not a
   level's absence from these lists.
2. **The range beyond `min` and `max`.** A published `min` or `max`
   (PROFILE-SPEC §4) says that no record lies below or above it, so the part
   of the declared range beyond it holds none. This leaves the rule
   inconsistent at the ends of the range: where zeros are disclosive, a
   `quantile/1` whose witness is the column's smallest value is withheld
   because no record lies below its witness, though a published `min` already
   shows that, and one whose witness is the largest value is withheld because
   no record lies above its witness, though a published `max` already shows
   that.

The alternative was to close both; the maintainers chose to document them
over it:

- Closing the first would publish a `support/1` only when every declared
  level is present, which leaves its fidelity check, a comparison of member
  sets, nothing to compare wherever a declared level is empty. It would also
  list every declared level as a group, an empty one withheld, so that under
  the zero and sibling rules every grouped statistic over a groupBy with an
  empty declared level would be withheld whole. That is common: a declaration
  often lists more levels than the data holds.
- Closing the second would withhold `min` and `max` wherever zeros are
  disclosive, unless the value is the declared bound itself. Under ACRO's
  defaults nearly every number and timestamp column would lose both, though
  no fidelity statistic reads them, and the engine's check that a profile's
  values lie inside the declared range would rarely run. A `min` or `max` is
  an order statistic, which forge-cli's profiler publishes only when
  `minimumCell` records hold its value, as the Context says of a quantile,
  and the rule's zero protection is about the cells of count tables.

Consequences of the amendment:

- PROFILE-SPEC §3 names both, in the same list as a group's count of nulls,
  which the rule does not see either.
- Neither changes what `check_edge_counts` decides or what a profiler
  publishes.
