# ADR 0003: aggregate-only calibration; no SDV, no record-level fitting in the v0.1/v0.2 build

## Status

Accepted. **Amended 2026-11-11:** the opt-in `[synth]` extra named under
Decision was removed, because nothing imported it. The rule itself is unchanged.
**Amended 2026-11-16:** the rule is extended. The engine never reads a
record of the data a bundle imitates, and a separate profiler package reads
records only where they live and emits only aggregates, under the
disclosure defaults set out in [the amendment](#amendment-of-2026-11-16) at
the end. One engine path, `--committed-slice`, still reads records today:
the amendment names it, and it moves into the profiler package. The
Context, Decision and Consequences above that section are unchanged.
**Amended 2026-11-21:** an opt-in noise mode. A profile may carry marginals that the
profiler released under differential privacy, and a run built from them carries the
release's guarantee, for the stated unit and for nothing else, as set out in
[the second amendment](#amendment-of-2026-11-21). The rule that the engine reads no
record is unchanged, and so is point 3 above for every statistic that carries no noise.
**Amended again 2026-11-21:** learned record-level generators stay out of the engine and
are planned as a package of their own, as set out in
[the third amendment](#amendment-of-2026-11-21-learned-record-level-generators).
**Amended 2026-11-23:** a profiler protects the facts it publishes over one table as a
set, not one at a time, as set out in
[the fourth amendment](#amendment-of-2026-11-23-facts-protected-as-a-set).

## Context

Borrow-before-build search: "SDV synthetic data vault license Business
Source License" (2026-09-06); fetched
https://datacebo.com/blog/sdv-bsl-license/. SDV's core package is
BSL 1.1: non-production use is always allowed, but production use is
restricted, and each release converts to MIT only four years after that
release ships (v1.0.0 converts 2027-03-28). Depending on SDV in a
production FLUX Simulation Engine would put a restricted license under
every downstream user.

Separately, and independent of licensing: the project's scope is to build
synthetic rows from a contract schema, calibrated on aggregate statistics
only and never on real records, so that no synthetic data is derived from
personal data. Record-level model fitting (SDV, synthcity's CTGAN/TVAE, or
any generative model trained on real rows) is a different technique from
schema-driven generation calibrated on aggregate statistics, and is out of
scope for the v0.1/v0.2 build regardless of which library would supply it.

## Decision

- The v0.1 generator (`flux_engine.generator`) produces rows from a FLUID
  expose schema's declared column types and four distribution families,
  parameterized by aggregate statistics -- never by fitting a model to
  real records.
- No record-level library is a dependency, in any extra. synthcity
  (Apache-2.0) and SDMetrics (MIT) were first listed as an opt-in `[synth]`
  extra for a customer's own record-level workflow. Nothing imported it, so
  it was removed.
- SDV is not a dependency, in any extra, under any circumstance, while its
  core package remains BSL 1.1.

## Consequences

- No production-license exposure from a restricted-license dependency.
- The aggregate-only rule is enforced by what the v0.1/v0.2 code paths
  call, not only by a license choice -- `flux_engine.generator` and the
  fitted calibration path (v0.2) both take aggregate statistics as their
  only cross-boundary input, never a stream of individual records.

## Amendment of 2026-11-16

### Context

A synthetic copy of a whole multi-table source has to be calibrated to
statistics of that source: per column, per pair of columns, per
relationship (children per parent), per period and per consumer query.
Someone has to compute those numbers from records. The decision above says
what the generator and the calibration path take as input. It does not say
who reads the records, where, or what may leave with the numbers.

### Decision

1. The engine never reads a record of the data a bundle imitates. Every
   input that describes that data is an aggregate statistic or
   non-personal reference data, such as a product catalogue. This holds
   for every code path and every extra, not only for the generator and the
   calibration path. The rows the engine writes, and reads back in
   `verify` and `ship`, are synthetic and are not such records. One path
   does not hold yet; it is named below.
2. A separate profiler package computes the aggregates. It reads records
   only where they live, inside the data owner's environment, and emits
   only aggregates: no record, sample of records or per-record value
   leaves it. It is a package of its own, so once the path below has moved
   into it, installing the engine installs no record reader.
3. The profiler applies disclosure rules before an aggregate leaves, and
   records which rule it applied to each statistic. Its defaults take
   their values from ACRO (https://github.com/AI-SDC/ACRO, MIT,
   `acro/default.yaml` at v1.1.0):
   - a cell with fewer than 10 records is suppressed and flagged
     (`safe_threshold: 10`);
   - a zero cell is disclosive (`zeros_are_disclosive: True`);
   - the counts it publishes are rounded to a multiple of 5, the base of
     ACRO's rounding mitigation (`safe_round_base: 5`);
   - no noise is added by default.

   These are output-checking conventions only; no privacy or anonymity
   guarantee is made or implied. ACRO comes from the SACRO project, whose
   tools are built to assist human output checkers, not to replace them.
4. A fidelity check's result, and any score computed from it, states a
   statistical relation between the synthetic rows and the statistics
   they are checked against (a committed state today, a profile once
   profiles exist), under the properties registered for the check, and
   nothing more. It says nothing about privacy, fairness or the validity
   of a simulated outcome.

The path that reads records today is the fidelity check's boundary side
(SEAM-FIDELITY-SPEC §4), which `--committed-slice` reaches from
`fluxsim run`, `ship` and `verify`. It reads a committed record set where
it lives. What it lets out is the list in §4 (`S`, `C1`, `C2`, the state
ordinal, the record count or declared range, and the commitment time),
plus `G` and the slice's URI and line range. It applies none of the
defaults in point 3. Its counts are exact, its support list names every
category it saw however few records hold it, and a quantile witness is a
value read from one record; the contract's `records.min` is its only
floor. It moves into the profiler package when that package ships, and
how point 3 then applies to its statistics is part of that package's
design. Until then it is the one exception to point 1.

### Consequences

- Calibrating a multi-table source keeps the rule: what crosses into the
  engine is aggregates and non-personal reference data, never a record.
- A fidelity check against a profile must treat a cell the profiler
  suppressed as unknown, not zero, and leave it out of the result:
  scored as zero, it would count against every run whose synthetic rows
  fall in that cell.
- This amendment changes no code. The profiler package, and the move of
  the boundary side into it, are later work.

## Amendment of 2026-11-21

### Context

Borrow-before-build search: "OpenDP privacy unit privacy loss measure" (2026-11-21);
fetched https://docs.opendp.org/en/stable/api/python/opendp.measures.html, which defines the
measures `zero_concentrated_divergence` (rho-zCDP) and `fixed_smoothed_max_divergence`
((epsilon, delta)-approximate DP) and says the input metric of a measurement defines which
datasets are adjacent. Also fetched NIST SP 800-226 (final, March 2025,
https://csrc.nist.gov/pubs/sp/800/226/final, the PDF read as text), whose definition of the
unit of privacy (the definition of neighboring datasets) this record uses. Point 3 of the first
amendment ends "no noise is added by default". Some data owners want a statement that is more
than an output check: a number, a unit and a way of accounting for it. Differential privacy is
the one technique whose guarantee survives every later computation on its output
(post-processing), which is the property an aggregate-only engine can use: the engine never
sees a record, so everything it computes is a function of the numbers a profiler let out.

### Decision

1. **A noise mode exists, and it is opt-in.** A profile (PROFILE-SPEC.md, format 1-2-0) may
   carry a `privacy` block and, on a statistic or a clique marginal, a `noise` block. The
   `privacy` block states the measure (approximate DP or zero-concentrated DP), the total
   privacy loss and the unit of privacy, an expose id. A statistic that carries a `noise`
   block is one the profiler released through the mechanism and scale the block names, with
   the share of the total that the block states. A profile without either block is what
   it was: output-checked aggregates and no guarantee, and point 3 of the first amendment
   describes it as before.
2. **The engine reads only released aggregates.** The only inputs of a run that describe the
   data are the statistics of the profile (and the contract, the bundle and the seed, none of
   which is data). The engine does not look at a record in this mode either.
3. **What a run built from noisy statistics carries.** Let R be the profiler's release: the
   noisy statistics and cliques of one profile, made by one release of the data under the
   stated measure and total, for the stated unit. Differential privacy is closed under
   post-processing: any computation on R that does not read the data again satisfies the
   same guarantee. A run whose every input about the data is an element of R is such a
   computation: the plan, the rows, the scorecard and the evidence are functions of R, of the
   contract and bundle, and of a seed chosen without reading the data. It therefore carries the
   guarantee of R, (epsilon, delta) or rho as stated, for the stated unit of privacy, and
   nothing more.
4. **What is not covered.**
   - Any statistic in the same profile that carries no `noise` block. A count without
     noise, a published `min` or `max`, a `support` list, a row count and a metric are
     released under the disclosure rules of the first amendment, which are output checks and
     no guarantee. A run that reads a profile holding one is graded below the formal pattern
     (ADR 0020).
   - The levels of a histogram, its bin edges and the `sampleSize` bands. They are chosen by
     the profiler, and they describe the data if the profiler looked at it to choose them. The
     claim holds only when the profile declares that they were chosen without the data or are
     part of the release (`publicStructure`, PROFILE-SPEC.md section 3a); the engine cannot
     check the declaration, and a profile without it is graded below the formal pattern.
   - Hand-written bundle parameters. They are not data, so the guarantee says nothing about
     them: a number an author typed after looking at the records is outside it, and the engine
     cannot tell.
   - That the profiler released what the profile says. The engine checks the accounting
     arithmetic (ADR 0019) and the consistency of the cliques, and it cannot check that a
     mechanism of the stated kind and scale produced the numbers. The guarantee is
     conditional on the profiler's statement and says so.
   - Any unit larger than the one stated. An entity that contributes several rows of the
     unit's expose is protected at the stated loss times the number of rows it contributes
     (group privacy), which the profile does not state.
   - A second release of the same data. Another profile, a profile with other parameters and
     any other output made from the same records add their losses to this one's.
   - Any statement of what the synthetic rows resemble or how they might be used. The
     fidelity check's statement (point 4 of the first amendment) is unchanged: it is a
     statistical relation and nothing about privacy.
   - The record path of `--committed-slice`, which reads records and adds no noise.

### Consequences

- A data owner with a noisy profile can say what the engine's output is guaranteed
  to be, and the evidence pack states the same (ADR 0020). A data owner without one is told
  that no guarantee is made.
- Nothing here lets the engine draw noise: a noisy statistic is made where the data lives,
  by the profiler.
- The disclosure defaults of the first amendment stay as they are. A profiler that adds noise
  may still apply them to the statistics it releases without noise.

## Amendment of 2026-11-21: learned record-level generators

### Context

A learned record-level generator is a model fitted to the rows of a table, such as a
copula, a generative adversarial network, a variational autoencoder or a sequence model,
that then draws new rows from what it learned. It can capture dependence that no declared
statistic names, so it is a technique the engine will be asked for. The decision above kept
record-level fitting out of the v0.1 and v0.2 builds, and the amendment of 2026-11-16 made
the rule a boundary: the engine never reads a record of the data a bundle imitates, and
every input that describes that data is an aggregate. This record has not said what that
boundary means for a learned generator.

### Decision

1. **Learned record-level generators stay out of the engine.** The engine does not fit a
   model to records, does not read records to fit one, and takes no dependency, in its core
   or in any extra, that does. The engine holds an aggregates-only boundary, and a learned
   generator is on the other side of it by construction, because its input is the records
   themselves.
2. **They live in a package of their own.** `flux-learned`, planned for 0.11.0, is a separate
   distribution that whoever wants one installs beside the engine. Installing the engine
   installs none of it, and the engine does not name it as an extra.
3. **The engine's guarantees do not reach what it makes.** The statement of the second
   amendment holds because every input of a run that describes the data is an element of
   one release. The rows a learned generator draws are a function of the records, not of a
   release, so that statement does not cover them, and neither do the grades of ADR 0020,
   which describe what a profile states. What `flux-learned` says about its own output is
   its own, and the engine does not check it. A learned generator that stated a
   differential privacy guarantee for its own fit is the case ADR 0020 reserves as P4, a
   differentially private model from a plugin, and 0.9.0 produces none.
4. **Why not an extra.** An extra would put model fitting on the engine's install path and
   in its test matrix, with the dependencies that come with it (a deep learning framework,
   for most such models). It would also make "the engine reads no record" a statement about
   a default and not about the code. A separate package keeps the sentence about the code.

### Consequences

- Nothing changes in this release. No engine code reads or fits a record, and the
  dependency list does not move.
- Someone who wants a learned generator installs `flux-learned` when it ships and takes
  what it says about privacy from that package.
- A model fitted to rows can reproduce rows it was fitted to. The engine's aggregate path has
  no such output, which is part of why the two are kept apart.

## Amendment of 2026-11-23: facts protected as a set

### Context

The disclosure defaults of the first amendment judge a statistic by its own cell counts.
A profiler that publishes several facts over one table, each a share or a count of the
rows that satisfy a rule, can pass every fact on its own and still disclose a small count
by differencing. If A and B are each large, and A or B is large, the count of A and B is
the sum of the first two minus the third, and it can be 3. Checking facts one at a time
does not see that.

### Decision

1. **A profiler protects its facts as a set.** The unit of the check is the whole set of
   facts published over one table, not a single fact. The rule is in section 2 of the
   profile specification and every FLUX profiler applies it.
2. **The check runs on the atoms.** One grouped query over the bitmask of the rules gives
   every cell of the Venn diagram of the facts. A fact is judged by the atoms it touches,
   and the withholding repeats until nothing changes, so the outcome does not depend on
   order.
3. **Counts are checked before rounding.** Two rounded counts can differ by an amount
   that reveals a small count that neither shows alone.

### Limits

The rule covers facts over one table. It does not cover overlap with histogram cells, or
with relationship counts. A profile that publishes those next to such facts is not
protected against differencing across them, and the maintainers do not claim it is.

### Consequences

- A profiler that adopts the rule withholds more than the per-fact check did, with the
  existing withheld code, so a reader of a profile needs no change.
- The rule withholds; it does not perturb. Where a release must withstand differencing
  beyond what this rule covers, the route the cited ONS note describes is perturbation,
  as in the amendment of 2026-11-21.

### References

- Handbook on Statistical Disclosure Control, second edition (the sdcTools online
  edition, no publication date shown), section 4.3.1 on hierarchical and linked tables,
  which says that selecting secondary suppressions for each table separately is not
  enough, and section 4.3.3 on processing linked tables. Section 4.3 describes
  tau-Argus as the software that implements cell suppression.
  <https://sdctools.github.io/HandbookSDC/04-magnitude-tabular-data.html>
- sdcTable (R package, version 0.32.6), reference page "Attacking primary suppressed
  cells", for `attack()`, which computes lower and upper bounds on suppressed cells under
  the current suppression pattern. The page does not mention linked tables.
  <https://search.r-project.org/CRAN/refmans/sdcTable/html/attack.html>
- UK Office for National Statistics, "Comparison of post-tabular statistical disclosure
  control methods", released 3 May 2024 (edited 22 November 2024), which credits
  perturbation with protecting against differencing.
  <https://www.ons.gov.uk/peoplepopulationandcommunity/populationandmigration/populationestimates/methodologies/comparisonofposttabularstatisticaldisclosurecontrolmethods>
