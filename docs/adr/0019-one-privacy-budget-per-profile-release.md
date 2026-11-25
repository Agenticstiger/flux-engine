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

### Consequences

- A profile released and then cut down by a list of unused statistics is still a profile `init`
  and `plan` read. If every noisy statistic was cut, the run is graded `P1`, not `P3`.
- The guarantee the engine reports for a run is still the stated total (point 4), which bounds
  the loss of every part, spent or not.

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
   `marginalHistogram/1`, a bin that does not clear the floor carries no weight in the draw, and
   its register entry says so. The column's span, for the run's window, is the hull of the bins
   that clear it. When none does, the column is drawn evenly within the run's window, cut to its
   declared range, as a noted assumption, the way a column that publishes no order statistic is
   drawn. A window with no span anywhere is the assumed year, and its note says so.
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

