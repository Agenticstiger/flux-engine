# ADR 0019: one privacy budget per profile release, split across its statistics

## Status

Accepted 2026-11-21. Builds on the second amendment of
[ADR 0003](./0003-sdv-bsl-and-aggregate-only-rule.md), which lets a profile carry marginals
released under differential privacy and says what a run built from them carries. This record
says how the profile accounts for the loss of that release, and what the engine checks. The
[amendment of 2026-11-25](#amendment-of-2026-11-25) changes one check: a stated loss that nothing
spends is accepted. The
[second amendment of 2026-11-25](#amendment-of-2026-11-25-a-released-date-histogram-is-read-where-it-stands-clear-of-the-noise)
says how `init` reads a released date histogram: only where its counts stand clear of the noise.

## Context

Borrow-before-build search: "OpenDP composition privacy loss add" (2026-11-21); fetched
https://docs.opendp.org/en/stable/api/python/opendp.combinators.html, whose composition
combinators (`make_composition`, `make_adaptive_composition`) sum the privacy usage of the
queries linearly, and
https://docs.opendp.org/en/stable/api/python/opendp.measures.html, which names the measures
`zero_concentrated_divergence` and `fixed_smoothed_max_divergence`. The zero-concentrated
definition and its composition (rho adds) are those of M. Bun and T. Steinke, "Concentrated
differential privacy: simplifications, extensions, and lower bounds", Theory of Cryptography
Conference (TCC) 2016-B, LNCS 9985, pp. 635-658 (arXiv 1605.02065). NIST SP 800-226 (March 2025)
was read as text (https://csrc.nist.gov/pubs/sp/800/226/final): it asks that a guarantee be stated
with its privacy parameters, its unit of privacy and the way the loss was accounted for, and it
warns that guarantees with different units or different delta values are not directly comparable.
The noise's reach on a total variation distance (decision 7) rests on B. Laurent and P. Massart,
"Adaptive estimation of a quadratic functional by model selection", Annals of Statistics 28(5):
1302-1338, 2000, Lemma 1 (DOI 10.1214/aos/1015957395); on the discrete Gaussian's sub-Gaussian
tails, C. L. Canonne, G. Kamath and T. Steinke, "The discrete Gaussian for differential privacy",
NeurIPS 2020 (arXiv 2004.00010); and on D. Hsu, S. Kakade and T. Zhang, "A tail inequality for
quadratic forms of subgaussian random vectors", Electronic Communications in Probability 17,
article 52, 2012 (DOI 10.1214/ECP.v17-2079).
OpenDP is used as a vocabulary only (measure, privacy unit, privacy loss, scale) and is not
a dependency.

A profile of format 1-2-0 can hold several noisy statistics and clique marginals. Each is an
output of one mechanism on the same data, so the loss of the release is the composition of
their losses. A reader of the profile, and of the evidence pack made from it, has to be told
one number, and has to be able to see that the parts do not exceed it.

## Decision

1. **A profile release has one budget.** The profile's `privacy` block states the measure,
   the total privacy loss and the unit of privacy once (PROFILE-SPEC.md section 3a). Every
   noisy statistic and every noisy clique states its own share of it in its `noise` block.
   A noisy statistic in a profile with no `privacy` block, and a `privacy` block that
   no statistic spends any of, are refused. (A `privacy` block that nothing spends is accepted
   since [the amendment of 2026-11-25](#amendment-of-2026-11-25).)
2. **The split is over what the profile request asks for.** The profile request
   (PROFILE-REQUEST-SPEC.md) names the statistics a profiler must measure. The profiler
   divides the budget among those it releases noisily, and among the cliques it chooses. The
   engine does not choose the split and does not read it for any purpose other than the
   check below and the evidence.
3. **Composition is by addition, in the profile's measure.**
   - Under zero-concentrated DP the parts are values of rho, and rho adds.
   - Under approximate DP the parts are pairs (epsilon, delta), and under basic composition
     epsilon adds and delta adds. Pure DP is the case delta = 0.
   - Every part is stated in the measure of the total. A part in the other measure is
     refused: the engine does not convert between measures.
   - Sums are exact (decimal strings read as rationals), never floats.
4. **The check.** The engine refuses a profile (`FLX-5036`, naming the statistic or clique
   whose part is wrong, never a value) when:
   - a part is negative, absent or not a decimal;
   - the parts of the release add up to more than the total, in rho or in epsilon or in
     delta taken one at a time;
   - a noisy statistic lies in a table other than the unit's expose (the profile does not
     state how many rows of another table one unit contributes, so the loss there is not
     known);
   - a part is below what its own mechanism spends. Every count table has sensitivity 1 under
     adding or removing one row of the unit's expose (each row lands in one cell), so the L1 and
     the L2 sensitivity are both 1, and every pairing of a mechanism and a measure is checked, in
     exact arithmetic:
     - a Gaussian or discrete Gaussian of scale `s` under zero-concentrated DP spends
       rho = 1 / (2 s^2);
     - a Laplace of scale `b` under approximate DP spends epsilon = 1 / b, with any delta;
     - a Laplace under zero-concentrated DP spends rho = (1 / b)^2 / 2, because an epsilon-DP
       release is (epsilon^2 / 2)-zero-concentrated DP (Bun and Steinke 2016, Proposition 1.4);
     - a Gaussian or discrete Gaussian under approximate DP states a delta above zero and below
       one, since it never satisfies delta = 0, and an epsilon of at least
       rho + 2 sqrt(rho ln(1 / delta)) with rho = 1 / (2 s^2) (Bun and Steinke 2016,
       Proposition 1.3; for the discrete Gaussian's zero-concentrated DP, Canonne, Kamath and
       Steinke 2020). The engine computes that epsilon in decimal arithmetic at a fixed precision,
       rounds every inexact step up so the figure is never below the true one, and compares it
       exactly with the stated epsilon, so the same document is accepted or refused the same way on
       every platform.

     A stated share below what its pairing spends is understated and refused. A share above it is
     accepted: it is slack the profiler keeps. A tighter share than the engine can confirm, a Laplace's
     rho below (1 / b)^2 / 2 for example, is refused too, because the engine cannot check it, so
     the check fails closed. The check takes the sensitivities as parameters (`l1` and `l2_squared`,
     both 1 for a count table), so a statistic of a larger sensitivity is checked by the same rule.

   A sum below the total is accepted. What is left is the profiler's: a data dependent
   choice of cliques, for instance, spends loss that no statistic carries. The guarantee the
   engine reports is the stated total, which bounds the sum of every part, spent or not.
5. **What the engine does not check.** It does not check that a mechanism of the stated kind
   and scale made the numbers: it reads the numbers and not the sampler. It does not tighten the
   total by any other accountant, such as advanced composition or privacy loss distributions, and
   does not convert the total from one measure to the other. Both would give a smaller figure and
   neither is needed for an upper bound. It does use the conversion of rho to (epsilon, delta) to
   check the share of a Gaussian under approximate DP (point 4), and for that alone. It cannot check that the levels, bin edges and `sampleSize` bands were chosen
   without the data, so the profile declares it (`publicStructure`) and the grade rests on the
   declaration (ADR 0020).
6. **The variance is derived from the scale**, not from the share. The engine's tolerance depends
   on the noise the numbers carry, and that is the mechanism's scale.
7. **A noisy reference widens the tolerance.** A run compared with a noisy statistic is compared
   with an estimate of the data's shares, so a tolerance declared for sampling alone fails a faithful
   run about as often as the noise moves the estimate past it. Each property compared with a noisy
   `levelHistogram/1` or `marginalHistogram/1`, a released one or one formed from a clique, has its
   compiled margin widened group by group by `w` of SCORECARD-SPEC.md section 9, from the cells'
   variance, the number of cells and the estimated records. The scorecard row carries the variance,
   the mechanism and the widening, and the scorecard names format 1-3-0 when a row does. For a world
   whose rows match the profile at least 95 of 100 noise seeds pass `verify` (the test measures 100
   of 100, and 14 of 100 with no widening). The widening is conservative, about three to five times
   the noise's typical effect on the distance. Sampling variance is not added, and the section states
   each assumption: the bound is for the estimate before its projection, it needs a per-cell noise
   covariance at most `v` times the identity (PROFILE-SPEC.md section 5a), and its probability
   statement is for Gaussian tails.

## Consequences

- The evidence pack states one total, a definition and a unit, and the parts it was checked
  against (ADR 0020).
- A profile whose parts exceed its total never reaches `init`, so no bundle is written from
  a release that overspent.
- Several releases of one data set are several profiles, each with its own budget, and the
  engine cannot add them. ADR 0003's second amendment says so.

## Alternatives considered

- **A budget per statistic with no total.** Rejected: nothing then states the loss of the
  release, and there is nothing for the engine to check the parts against.
- **The engine splits the budget.** Rejected: the profiler's mechanisms and their
  calibration are its own, and an engine that assigned shares would be asserting how the data
  was measured.
- **An accountant library (OpenDP, a privacy loss distribution accountant).** Rejected for
  this release: a dependency for an upper bound that addition already gives, and the
  engine would then be computing a guarantee rather than checking a stated one.

## Amendment of 2026-11-25

### Context

A profile released under differential privacy can be made smaller after the release: a step that
drops each statistic an `init --json` inventory marks unused can drop the only noisy statistic the
profile held. What is left is a `privacy` block that no statistic or clique spends, and the
reader refused it (`FLX-5036`, "states a privacy loss that no noisy statistic or clique spends").
The refusal was the one check that turned away a stated loss above what the parts spend. Every
other comparison of the check is in one direction: the parts may add up to the total or to less,
never to more (point 4: "A sum below the total is accepted").

### Decision

1. **A stated loss is an upper bound, and a bound above what is spent is accepted, including
   when nothing is spent.** The reader no longer refuses a `privacy` block because no noisy
   statistic, clique or share spends any of it. The check is now the one rule it always stated:
   the parts add up to at most the total, in rho, in epsilon and in delta, each on its own. A total
   below what the parts spend (understated) keeps the `FLX-5036` refusal, and so does every other
   check of point 4. The rule changed outright and is not a reader option: it was refused by that
   one check and no other, and an option would leave a profile that is valid for the engine
   refused by every other reader of the format.
2. **The grade reads the statistics, never the block alone** (ADR 0020). A profile with a block and
   no noisy statistic or clique is `P1`, with the reason that its account is spent by no noisy
   statistic or clique, and a profile with exact statistics beside a block grades no better than
   the same profile without the block. A total above what is spent changes no grade: all
   statistics noisy under an account is `P3` whatever slack the total leaves. Tests pin both.
3. **`init` says when the stated loss exceeds what the profile spends.** The output carries one
   `privacy:` line and an `init --json` `privacyLoss` member (measure, the stated total, what the
   noise blocks add up to, and the number of parts) when a stated share is above its sum. Both
   figures are values the profile states: the total, and the shares its `noise` blocks record.
   Nothing in it depends on the records.
4. **An entry a release cut is read with the members it keeps.** The same step can drop some or
   all of the members of a calendar entry, a date pair, a relationship's date pair, an exposure, a
   history or a tracked column of one: when one member is withheld and another unused, only the
   unused one leaves. The inventory stays as it is, each member with the state it has, and the
   profile reader takes `allow_emptied_entries`, off by default and set by the engine: with it each
   member of such an entry is read when present, and an entry left with none holds nothing, kept in
   its place, with a citation at or under it refused (`FLX-5036`). A citation of a member that is
   gone is refused as a citation of any unpublished statistic is (`FLX-3113`). Unlike point 1 this
   is an option, because the format itself still requires the members of an entry that is
   published whole.

### Consequences

- A profile released and then cut down by a list of unused statistics is still a profile `init`
  and `plan` read. If every noisy statistic was cut, the run is graded `P1`, not `P3`.
- The guarantee the engine reports for a run is still the stated total (point 4), which bounds
  the loss of every part, spent or not.

## Amendment of 2026-11-25: a noisy history and a noisy null count (format 1-5-0)

### Context

A table's `history` (PROFILE-SPEC section 5) could not carry noise, so a profile that held one never reached P3
(ADR 0020): one version row moves an entity between cells, and the account charged a noisy statistic a
sensitivity of 1. A null count had no noisy form either: the `count` and `nullCount` metrics are exact, and a
private release had no way to state how many rows of a column are NULL. Both are wanted by the owner of a table
whose rows are versions or have optional values, and both can be charged with the rule the account already uses
for a table that a unit follows into (ADR 0037).

### Decision

1. **A history member may carry noise.** Each of `versionsPerEntity`, `firstStart`, `versionLength`, `chain`,
   `lastVersion`, a tracked column's `initial` and `transitions`, and `reasons` may state a `noise` block and write
   its groups in the noisy form. Members may mix noisy and exact. A noisy `transitions` publishes every
   current-value group, one cell per next level.
2. **It is allowed when the history's table `T` is reached by the unit through `follows` (`m_T >= 1`) and is not
   the unit's own expose.** Otherwise the reader refuses it (`FLX-5036`), naming the member and never a value. The
   expose is excluded because one unit is one row there, and an entity is several rows.
3. **The charge is `m_T` for every noisy member, as a count table: L1 = L2 = `m_T`.** One unit holds at most `m_T`
   rows of `T`, so at most `m_T` entities (the members `versionsPerEntity`, `firstStart`, `lastVersion` and
   `initial`) and at most `m_T - 1` consecutive pairs of versions (`versionLength`, `chain`, `transitions` and
   `reasons`). Each moves one cell by one, so the sensitivity is `m_T`, and the pair members are charged `m_T` as
   well, which is one more than they need. Each noisy member states its own share, and the shares add with every
   other part of the budget (basic composition, Bun and Steinke 2016, as in point 4 of the decision).
4. **Three obligations stay the profiler's and are stated, not checked.** Every version of an entity references one
   row of the parent of `T`'s listed relationship, so an entity lies in one unit (true by construction when that
   relationship's child columns are the history's `entity` columns); the `maxChildren` bounding of `T` keeps or
   drops an entity's versions together, by a rule that reads only that parent's rows; and `publicStructure: true`
   also covers the bins of `versionsPerEntity`, `firstStart` and `versionLength`, the levels of `initial` and
   `transitions`, and the candidate columns. The evidence says the engine cannot check them.
5. **`nullCount/2` is a noisy column statistic of two cells.** It states the rows where a column is known and where
   it is NULL, in that order, for a column of any domain, noisy form only, ungrouped. It is a count table over the
   column's table: L1 = L2 = `m_T`, since a row lands in exactly one cell, under the same table rules as any noisy
   column statistic. The reader reads it as it reads any noisy group, so its point estimate gives the rows and the
   null share; the second cell costs nothing and is the share's denominator, at variance `2 s^2`.
6. **A history whose every member carries noise under the account no longer keeps a profile from P3.** A history
   with any member without noise does, as before. A `nullCount/2` carries noise and never keeps a profile from P3.
7. **Format.** A document that holds a noisy history member or a `nullCount/2` names `1-5-0`, and one that names an
   earlier format and holds one is refused (`FLX-5036`). Documents of `1-4-0` and earlier read exactly as before.

8. **A query's rate may carry noise, and its query states a `unit`.** A `queries[]` statistic (a `levelHistogram/1`
   over `["false", "true"]`) may carry a `noise` block, with its groups in the noisy form. A noisy query states its
   `unit`: `{ "table": T }`, each row of `T` a record (charge `m_T`), or `{ "table": T, "by":
   [columns] }`, where `by` is the child columns of a relationship from a parent `P` into `T` that the unit follows,
   or into a table it does not reach (one group is one row of `P`, charge `m_P`, 1 when `P` is the expose; twice that, L1 = 2 `m_P` and L2² = 2 `m_P`², when
   `T` is not reached, since a group stays when its row of `P` leaves and can change cell) or `T`'s
   history entity (charge `m_T`, under the history's whole-entity obligation, and refused in the unit's own expose as
   a noisy history member is: one unit is one version there, and can move its entity between the cells). When
   relationships from more than one parent the unit reaches give those child columns a charge, in any order, the query
   is refused:
   one unit's rows of each can move a group, so the charges would add. A query whose
   unit has `by` states no `groupBy` or `groupByBins`: a group of rows can hold several levels of a column and land in
   more than one cell. The profiler owes, stated and not checked: whether a record matches reads only rows of that
   record's own unit and rows of tables the unit does not reach, so the query uses no aggregate, subquery or join over
   other units' rows, since removing one unit may change the cells of its own records only. Both cells are a count table,
   L1 = L2 = the charge (except in the unreached form above), and each query states its own share. A noisy query with no unit, another unit, a charge of 0, a grouping with a `by` unit or an
   earlier format is refused (`FLX-5036`), naming the query. A run whose queries are all noisy no longer loses P3 for
   them.
9. **The engine reads each new part with the noise floor of the amendment above.** `fluxsim init` reads a noisy
   history member through its point estimate, and only its cells above `noise_floor` over all its cells carry
   weight; a member with none above it says nothing and is read as withheld. A `nullCount/2` gives a nullable
   column the NULL share its estimate states when its NULL cell stands clear of the floor over the two cells.
   Otherwise the release cannot tell the count from none, and no null is drawn, as for a null count withheld as
   small, with an assumption that names the interval the release allows, 0 to the NULL cell's estimate plus the
   floor over the rows: a column that declares a share is never the column another is drawn given, a clique
   column or a tracked one. A clique column holds no null, so the request init writes names as `cliqueColumns`
   only the columns the contract requires, and a profiler releases a column the contract leaves optional over its
   known values, outside the cliques. A release that still puts a column with a clear NULL cell in a clique that
   init draws has that share set aside, named in the ledger with the share, and the cliques are drawn; `--strict`
   refuses it, naming the column's statistic and the share, as a known loss. `fluxsim calibrate --fit` leaves out
   a noisy rate
   whose unit's records, the sum of its two cells, are at most twice the floor over every cell of its statistic
   (a sum that noise alone made passes that only when one of its two cells passes the floor), with a note
   (`FLX-3707` naming them when none is left), and weighs
   each query's squared gaps by the inverse of its rate's variance, scaled so the least noisy weighs 1, as an
   exact rate does. All of it is post-processing of the release.

### Consequences

- The noise of a history grows with the cap `maxChildren` of its table, as a count's does: a larger bound on how
  many versions one unit holds means a larger `m_T` and more noise for the same share.
- The charge is an upper bound. It is not the tightest one for the pair members (`m_T - 1`), and a tighter figure
  can replace it later without moving a profile that states a share at or above the charge.
- The profiler's three obligations are part of the guarantee and are not part of what the engine verifies. A release
  that breaks them is not covered by the stated loss, and the evidence names them.

### Prior art

- **OpenDP identifier truncation** (docs.opendp.org, tabular data, identifier truncation): `truncate_per_group(k)`
  keeps at most `k` records per identifier, and the noise grows with the bound. `maxChildren` is that bound.
- **Tumult Analytics**, "Understanding sensitivity" (docs.tmlt.dev): `MaxRowsPerID` makes a count's sensitivity the
  bound; with `MaxGroupsPerID` and `MaxRowsPerGroupPerID`, Laplace sensitivity is their product and Gaussian
  sensitivity is rows per group times the square root of groups. A history member is one group per entity, so the
  charge `m_T` is the conservative reading of it.
- **SmartNoise SQL** (docs.smartnoise.org, metadata): `max_ids`, how many rows one user can appear in, sampled down
  when exceeded.
- **Bun and Steinke 2016** (zero-concentrated DP): `rho` adds under composition, as in the budget above.

## Amendment of 2026-11-25: a released date histogram is read where it stands clear of the noise

### Context

A histogram released with noise holds noise in every cell, the empty ones too. `init` read a
released date histogram's point estimate as the column's draw and its outer edges as the column's
span. On a profiler's year grid (1900 to 2100 by year, with an outer bin on each side to the
first and last instant), the noise in the empty years put weight on dates across the calendar,
and the run's window stretched to 0001-01-01 and 9999-12-31. On a release of a few hundred
records, the noise of one cell can be larger than any year's count, so no year stands out at all.
A column with no order statistic is drawn evenly within the window, so it drew dates across
thousands of years.

### Decision

1. **The floor.** A cell of a released histogram stands clear of the noise when its released count
   exceeds

       B = sqrt(2 v (t + ceil(ln m)))   for Gaussian or discrete Gaussian noise of variance v,
       B = b (t + ceil(ln m))           for Laplace noise of scale b,

   over the histogram's `m` cells, with `t` the confidence exponent of point 7,
   `CONFIDENCE_EXPONENT = 5`. It is a union bound over the cells: a cell whose true count is zero
   exceeds `B` with probability at most `e^-t / m`, so with probability at least `1 - e^-5` no
   such cell among the `m` does. For a Gaussian the tail is the sub-Gaussian one point 7 uses,
   `P(eta > a) <= e^(-a^2 / 2v)`; for a Laplace it is `P(eta > a) = e^(-a / b) / 2`.
   `flux_stats.noise.noise_floor` computes `B`, and `ceil_ln` computes `ceil(ln m)` exactly, so
   `B` is the same number on every platform. For a year grid of 202 cells, `ceil(ln 202) = 6` and
   `B = sqrt(22 v)`, about 4.69 times the noise's scale.
2. **What `init` does with it.** For a date or timestamp column read from a released
   `marginalHistogram/1` (a number column too, since the amendment of 2026-11-26), a bin that does
   not clear the floor carries no weight in the draw, and
   its register entry says so. The column's span, for the run's window, is the hull of the bins
   that clear it. When none does, the column is drawn evenly within the run's window, cut to its
   declared range, as a noted assumption, the way a column that publishes no order statistic is
   drawn. When the range and the window do not meet, it is drawn evenly over its whole range,
   outside the window, and the column's note says so. A window with no span anywhere is the
   assumed year, and its note says so.
3. **What stays.** The column's declared range stays the histogram's whole grid, which its
   histogram property's bins partition (`FLX-5032`), and the property stays, so `verify` scores
   the column against the release with the widening of point 7. Plan reads the edges the draw can
   reach, not the range ([ADR 0017](./0017-an-entity-is-a-chain-of-versions.md), amendment of
   2026-11-25), so a range wider than the draw is accepted.
4. **Privacy.** The floor reads the released numbers only. It is post-processing of the release
   and spends no privacy loss.

### Why the union bound

The bound of one cell, `sqrt(2 t v)`, lets an empty cell through with probability `e^-5`. Over
202 cells, one stray year clears it in most releases, and on a small release the cells that clear
it are single scattered years, which is noise. The union bound is the same confidence for the
whole histogram, the confidence the widening of point 7 is stated at.

### Consequences

- Only bundles written from profiles released with noise change: their date columns draw inside
  the years that stand clear of the noise, or evenly inside the window, and their window shrinks.
  An exact histogram keeps its weights and its span.
- Plans already recorded replay unchanged: this changes what `init` writes, not what the generator
  draws from a bundle.
- A small release has no clear bin, so its dates are an assumption the run says it made, and
  `--strict` refuses it until the profile gives more.


## Amendment of 2026-11-26: a released number histogram is read where it stands clear of the noise

### Context

The floor of the amendment of 2026-11-25 read a date or timestamp histogram only. A number
column's released histogram was still read with every bin at face value, so an outer bin inside the
noise drew values the column does not hold, and the span a rule that reads the column sees was the
histogram's whole grid.

### Decision

1. **The same floor.** For an INT or DECIMAL column read from a released ungrouped
   `marginalHistogram/1`, a bin whose count does not exceed `B` of point 1 of that amendment, over
   the histogram's `m` cells, carries no weight in the draw, and its register entry says so. There
   is one floor rule: `flux_stats.noise.noise_floor`, read once for the statistic, as for a date.
2. **Where its values lie.** A column that has a bin inside the noise at its outer end draws no
   value there, so the span a rule that reads it takes is the hull of the bins that carry weight,
   as a date column's span is the hull of the bins that clear the floor. That span is what a
   contract `derive` over the column bounds its result by. When no bin clears the floor, the column is drawn evenly over its declared
   range as one bin, a noted assumption in its ledger entry, the way a date column is drawn evenly
   within the run's window. A number column has no window to be drawn within.
3. **What stays.** The declared range stays the histogram's whole grid, which its histogram
   property's bins partition (`FLX-5032`), and the property stays, so `verify` scores the column
   against the release with the widening of point 7.
4. **A key keeps its weights.** A column the contract makes a key, alone or with others, is read
   at face value: its rows hold distinct values, so a bin's count is the room it must have, and a
   small count inside the noise still asks for its values. Without them the key could hold fewer
   values than rows, and the bundle would be refused (`FLX-3403`).
5. **Privacy.** As before: the floor reads the released numbers only, and spends no privacy loss.

### Consequences

- Only bundles written from profiles released with noise change, and only a number column whose
  histogram holds a bin at or below the floor. An exact histogram, and a release whose bins all
  clear the floor, keep their bytes.
- Plans already recorded replay unchanged: this changes what `init` writes, not what the generator
  draws from a bundle.
- A column whose release has no clear bin is an assumption the run says it made, evenly over its
  range, and `--strict` refuses it until the profile gives more.
- That even draw is one bin, so a children-per-parent histogram grouped by the column's bins is
  not used for it, and the ledger says so. A published exact `min` or `max` inside a bin that
  carries no weight stays unreached by the draw.
