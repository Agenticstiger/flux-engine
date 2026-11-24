# Scorecard: normative specification (format 1-5-0)

A scorecard says how close a run is to the profile its Simulation pins
(PROFILE-SPEC.md), as exact scores per property, per table, per fidelity
dimension and for the whole run. `fluxsim verify` and `fluxsim ship` form it
when the run's plan pins a profile and the profile is handed to them with
`--profile`. A Simulation can declare thresholds on the scores, and `ship`
withholds a run that misses one. A shipped run carries its scorecard in the
evidence pack as `run/scorecard.json`.

Normative files beside this page:

- [`scorecard-1-5.schema.json`](scorecard-1-5.schema.json): the JSON Schema,
  whose `$id` is
  `https://agenticstiger.github.io/flux/engine/spec/scorecard/1-5-0/FluxScorecard.json`.
  The schema checks the shape; this page says how every number is formed.
  [`scorecard-1-4.schema.json`](scorecard-1-4.schema.json),
  [`scorecard-1-3.schema.json`](scorecard-1-3.schema.json),
  [`scorecard-1-2.schema.json`](scorecard-1-2.schema.json) and
  [`scorecard-1.schema.json`](scorecard-1.schema.json) are kept beside it for
  the documents of formats `1-4-0`, `1-3-0`, `1-2-0`, and `1-0-0` and `1-1-0`, which name their own `$id`.
- [`scorecard-example.json`](scorecard-example.json): a scorecard of format
  `1-2-0` with a scored, a censored and a refused property, a table with no
  fidelity block, a missed threshold, a coverage floor and a `minCredibility`
  read against a measured score.
  [`scorecard-1-3-example.json`](scorecard-1-3-example.json) is a scorecard of
  format `1-3-0`: a fact scored against its profile share, a fact the profile
  gave no share for, and a relationship scored for referential integrity.
  [`scorecard-1-5-example.json`](scorecard-1-5-example.json) is a scorecard of
  format `1-5-0`: a rank correlation compared with a noisy reference, a property grouped by bins and a
  children-per-parent row grouped by a parent column. The tests validate each against its schema.

The scorecard reads the outcomes of the profile path (PROFILE-SPEC.md §7)
and nothing else. The engine still reads no record of the data a profile
describes, and the scorecard holds no value of it (§7).

## 1. The document

A scorecard is one JSON object in UTF-8, written with sorted keys, a two-space
indent and a final newline. It holds no clock reading and nothing of the host,
so one run and one profile give the same bytes everywhere.

| Key | Holds |
|---|---|
| `_producer` | A URI naming the tool that wrote the scorecard. |
| `_schemaURL` | The schema's `$id`. The format version is part of it. A scorecard names `1-5-0` only when it holds a `rankCorrelation` row or a property row with `groupByBins` (§3), or a children-per-parent row grouped by a parent column; otherwise it names `1-4-0` only when it holds a children-per-parent row or a calendar row (§3), a property row that carries the `sampling` member of its `widening` (§9a), or the `queries` member of the `queries` dimension (§3a), or the top-level `overlay` member; otherwise it names `1-3-0` when it holds a fact or a relationship row (§3) or a property compared with a noisy statistic (§9), and every other scorecard names `1-2-0`, each keeping the bytes that format wrote. |
| `run` | `runId`, `planDigest` and `runDigest` of the run scored. |
| `profile` | `digest`, the pinned profile's `jcs-document` digest. |
| `overlay` | Format `1-4-0`, only when the Simulation declares a profile overlay (ADR 0030 section 7): `name` and `digest`, the overlay's name and the `jcs-document` digest of its `{name, patch}`. The properties are then scored against the overlaid profile, `MergePatch(profile, patch)`, read by the profile reader at the pinned profile's format, while `profile` still names the pinned profile. The overlay's numbers are a scenario the maintainers wrote, not a measurement, and the privacy grade reads the pinned profile. `ship` and `verify` read the overlay from the plan, which records it. |
| `met` | Whether every declared threshold is met and every measured `minCredibility` is reached (§4). |
| `overall` | The run's score (§3). |
| `coverage` | How much of the contract the score speaks for, over every table that records a contract (§3). |
| `dimensions` | The eight dimensions, always all eight, in the order of §3. |
| `tables` | One entry per emit of the run, sorted by `exposeId`, each with its own `coverage` (§3), `null` when the run records no contract for it. |
| `thresholds` | The declared thresholds and coverage floors, and what each read (§4). |
| `shapeFlags` | The shape flags (§6), with the status `checked`. A scorecard formed without them holds `{"status": "not checked"}`. |
| `intervals` | `{"status": "not computed"}`: the sample-based estimates are not built in this format. |

Numbers:

- A count is a JSON integer.
- A score, a weight and a margin are exact rationals written as strings:
  a terminating decimal (`"0.75"`, `"1"`) or a reduced fraction
  `"numerator/denominator"` (`"2/3"`). Nothing is rounded. A score is in
  `[0, 1]`.
- A score that was never formed is `null`. No default number stands in for
  it.
- A threshold's operand is written as it was declared (§4).

Text output (`verify`'s legs, `ship`'s summary, `EVIDENCE.md`) shows a score
exactly when it has at most four decimal places, and rounded down to four
otherwise. The document always holds the exact value, and the gate reads the
exact value.

## 2. A property's similarity

Every scored group of a property gets a similarity to its reference in
`[0, 1]`, formed from the same two groups the comparison of SEAM-FIDELITY-SPEC
§5 reads: for a count table, the cells the profile published, each side
renormalised to them (PROFILE-SPEC.md §7). The outcome, within or outside the
compiled margin, is still decided by the comparison and is still the gate.
The similarity says how close the two sides are, on one scale for every kind.

| Definition | Similarity | Pattern |
|---|---|---|
| `marginalHistogram/1`, `levelHistogram/1` | `1 - TV`, with `TV = (1/2) * sum_i abs(c_i/k - c'_i/k')` over the compared cells | SDMetrics TVComplement; mostlyai-qa accuracy |
| `mean/1` | `max(0, 1 - abs(m - m') / W)`, where `m = Sx/k` in grid units and `W` is the width of the column's declared range on the block's grid | SDMetrics StatisticSimilarity, over the declared range |
| `quantile/1` | `max(0, 1 - abs(w - w') / W)`, over the two order-statistic witnesses | the same, for the witness |
| `quantile/2` | `max(0, 1 - d / W)`, where `d` is the synthetic witness's distance in grid units from the reference bin, `0` inside it | the same, for a bin |
| `support/1` | the share of the synthetic group's records whose value is a member of the reference support; `1` for a group with no record | SDMetrics CategoryAdherence |
| `correlation/1` | a lower bound of `1 - abs(r - r') / 2` (below) | SDMetrics CorrelationSimilarity |
| `kendallTau/1`, `spearmanRho/1` | `1 - abs(t - t') / 2`, exact, rounded down to 12 decimal places | SDMetrics CorrelationSimilarity |

`W` places both ends of the declared range on the grid the way a value is
placed (floored). A range of one grid point (`W = 0`) scores `1` when the two
sides agree and `0` when they do not.

A correlation `r = N / sqrt(Qx * Qy)` is a ratio over a square root, so its
similarity is not rational in general. The score is an exact lower bound:
each root is bracketed by an integer square root at 64 bits, the difference
of the two correlations is bounded above over both brackets, and the result
is rounded down to 12 decimal places. Two correlations that are exactly equal
score exactly `1`. The bound is the same number on every platform and never
above the true score, so a floor that it clears, the true score clears too.

A similarity is defined per registered identifier. Forming one another way
is a new definition (`mean/2`) with a measure of its own, never a new meaning
of the old one.

## 3. The tree

**A property** has an `outcome` and, when it was scored, a `score`:

| Outcome | Meaning | Score |
|---|---|---|
| `within` | every scored group is within the compiled margin | the mean of its scored groups' similarities |
| `outside` | some scored group is outside the margin | the same |
| `censored` | the profile withheld every group, or every cell of every group, so nothing was compared | `null` |
| `refused` | a fail-closed condition was met for it, or for its whole table | `null` |

The groups are named by ordinal in the NFC-sorted group order, never by key:
`groups` holds how many were scored and censored, and the ordinals of those
outside. Each property row also carries its statistic, its columns, its
`groupBy`, its dimension, its declared tolerance, its compiled margin and its
refusals, each a code and a reason that names columns, rows and counts.

**The dimensions** are the scorecard's eight, in this order:

| Dimension | Scored by |
|---|---|
| `structure` | `support`, grouped or not: whether every value is one the data holds |
| `marginals` | `mean`, `quantile`, `marginalHistogram` and `levelHistogram` with no `groupBy` |
| `joint` | `correlation`, `rankCorrelation` (format `1-5-0`), and those four kinds with a `groupBy` or a `groupByBins`: two columns of one table together |
| `crossTable` | referential integrity of every declared relationship (format `1-3-0`, below) and children per parent (format `1-4-0`, below); later, multi-table fidelity properties |
| `temporal` | each histogram of a date column's published calendar (format `1-4-0`, below); later, periods and series and checks across periods |
| `invariants` | each fact scored against the share the profile measured (format `1-3-0`, below) |
| `queries` | the consumers' queries as counted on the run, when the bundle declares `consumers` (format `1-4-0`, §3a) |
| `defects` | nothing yet (defect rates, declared defects) |

**Facts and relationships (format `1-3-0`).** Two kinds of row sit beside the
properties of a table. Each has a `propertyId`, a `dimension`, an `outcome`
(`within`, `outside` or `censored`), a `score` and a `weight` of `1`, so the
tables and dimensions below read them as they read a property, and a threshold
on `invariants` or `crossTable` reads their mean. Neither reads a profile's
statistic of a column; each says how many rows hold something.

- **A fact** (`"property": "fact"`, `invariants`) is one rule of an emit, checked on
  the run's rows as the share that holds: `run` is `{satisfied, total, share}`, the
  rows the rule covers and the rows that satisfy it (EXPR-SPEC-2 section 6). Its
  `propertyId` is `fact:<exposeId>.<ruleId>`. `profile` is the same share as the
  profile measured it on the data, the `true` count over the published counts of
  the fact's entry in the profile's `invariants` section (PROFILE-SPEC.md section 5),
  or `null` when the profile holds no entry, withheld its counts, or counted no row.
  With both shares the fact is scored as the profile scores a two-level histogram,
  as one minus the total variation distance: `1 - |profile share - run share|`,
  exact. Its `gap` is the distance, `tolerance` is the largest gap the Simulation
  declares for the fact (`extensions['com.agentics.flux/1'].facts`, default `0`), and
  the outcome is `within` when the gap is at most the tolerance and `outside` when
  it is not. A fact with no profile share, or whose rule covers no row, is
  `censored`: it is shown and has no score, and no number stands in for it.
- **A relationship** (`"property": "referentialIntegrity"`, `crossTable`) is a
  relationship the child's contract declares, in the table of the child. It holds
  the four checks that apply, each as `{satisfied, total, share}`:
  `found` (each child key whose columns are all present is a key of the parent),
  `unique` (the parent's keys are distinct, over the parent rows whose key is
  present; not counted when the parent is versioned, which repeats a key by
  design), `nulls` (under `joinType: inner`, no child key has a NULL; not counted
  under `left`, which allows them) and `oneToOne` (under `cardinality: one_to_one`,
  each parent key that has a child has exactly one). Its `propertyId` is
  `relationship:<child exposeId>.<name>`. Its `score` is the unweighted mean of the
  shares of the checks that cover at least one row, exact; `null`, and the outcome
  `censored`, when none does. The outcome is `within` when every check holds on every
  row it covers, and `outside` when any does not, so a single broken key shows as a
  share under 1. The definition of `found` is SDMetrics' `ReferentialIntegrity`
  (sdv-dev/SDMetrics 0.33.0, MIT), used as a pattern and never as a dependency.

**Children per parent (format `1-4-0`).** A third kind of row sits in the table of the
child, for each relationship the profile describes with a `marginalHistogram/1` of
children per parent (a profile `relationships` entry, PROFILE-SPEC.md section 3).

- **A children-per-parent row** (`"property": "childrenPerParent"`, `crossTable`) counts
  the run's parents into the profile's bins by their number of children (a parent with
  none is counted at 0; a bin is half open and the last is closed), and compares the
  result with the profile's counts as a histogram is compared: the score is one minus the
  total variation distance, exact. Its `propertyId` is
  `children:<child exposeId>.<relationship>`. `bins` are the profile's edges, `run` and
  `profile` are `{parents, binCounts}`, `distance` is the total variation distance and
  `tolerance` the largest it may be: the `tolerance` of the relationship's `counts` entry
  (`extensions['com.agentics.flux/1'].counts`), default `0.10` for a relationship with no
  entry. The outcome is `within` when the distance is at most the tolerance and `outside`
  when it is not. A row whose profile counts were withheld, or that has no parent to
  count, is `censored`: it is shown and has no score. A relationship whose parent is
  versioned repeats keys by design, so its parents are not counted and it has no row.
  Like the rows above, it holds counts and exact shares and no key or row of the data.
- **A children-per-parent row, grouped** (format `1-5-0`). A relationship whose `counts` entry has `given`
  (SEAM-FIDELITY-SPEC.md section 3) is scored per group when the profile holds a `marginalHistogram/1` of
  children per parent grouped by that column, by `groupBy` or by `groupByBins`; a profile that holds only the
  ungrouped histogram leaves the row as the paragraph above states it. The row has the same `propertyId` and
  the same `bins`, and holds the profile's grouping as `groupBy` (the column) or `groupByBins` (`column` and
  `bins`), and `groups`, one entry per group of the profile's statistic in its order, which is the order of its
  levels or of its bins. The run's parents of each group are counted into the profile's bins by their number
  of children as the ungrouped row counts them, a parent being in the group its value of the column on its row
  names: for `groupBy` the value as the emit spells it, and for `groupByBins` the bin of the profile that holds
  it, a value outside every bin being in no group. A group is named by its `ordinal`, and holds its `outcome`,
  `run` and `profile` as `{parents, binCounts}` (`profile` is `null` for a group withheld), and `distance`,
  the total variation distance of the two. A group is `within` when its distance is at most the row's
  `tolerance`, the `counts` entry's and `0.10` without one, plus its widening when the reference is noisy. A
  group whose profile counts were withheld, or that holds no run parent, is `censored`: it is shown and has no
  distance. The row is `outside` when any scored group is, `within` when every scored group is, and `censored`
  when none was scored. Its `score` is the mean of the scored groups' scores, each one minus the distance, and
  its `distance` the largest. A noisy reference is widened by §9's `w` for each group, from that group's own
  noisy total, and a group's `widening` holds it; the row then carries `noise` as §9 states it, `widening`
  being the largest over its groups. A scorecard that holds such a row names `1-5-0`. A row with no `given`
  behind it is the ungrouped row, unchanged.

**Calendars (format `1-4-0`).** A fourth kind of row sits in the table of a date column the
profile publishes a `calendar` for (PROFILE-SPEC.md section 3), one for each histogram `verify`
scores in its `calendar:<column>` line (ADR 0025 section 8). A calendar row keeps its outcome whether or not
its line gates the run: a line gates, and an outside row then makes the scorecard not met, only for a column a
`rates` entry shapes.

- **A calendar row** (`"property": "calendar"`, `temporal`) compares the run's known values of
  the column, counted by month, by ISO day of week or by hour of week in UTC, with the profile's
  `levelHistogram/1` of the same name as a histogram is compared: the score is one minus the
  total variation distance, exact, and `distance` is that distance. Its `propertyId` is
  `calendar:<column>.<histogram>`, `histogram` is `month`, `dayOfWeek` or `hourOfWeek`, and
  `records` is the run's count of known values. `tolerance` is the largest distance the histogram
  may have: `0.10`, widened by the noise of a profile histogram released under noise (`widening`
  then holds the widening, as in §9). The outcome is `within` when the distance is at most the
  tolerance and `outside` when it is not. A histogram the profile withheld is `censored` and one
  whose comparison was refused is `refused`: each is shown, has no score, and its `distance` and
  `tolerance` are `null`. A calendar histogram `verify` lists as not scored (a hidden column or table, a
  column the run does not hold or that is no date, the `hourOfWeek` of a DATE) is not a row. The
  rows are read from the comparison of the `calendar:<column>` lines and are never formed a second
  time. They hold counts and exact decimals and no value of the data.

**Rank correlations and grouping by bins (format `1-5-0`).** Two more kinds of row sit among a table's properties.

- **A rank correlation** (`"property": "rankCorrelation"`, `joint`) compares the rank correlation of a pair of
  columns of the run with the profile's (PROFILE-SPEC.md section 5). The run's statistic is the plain one over the
  run's rows, with no row floor, so `N` is the number of rows that hold both values, and the reference is the
  profile's value: exact, or its clamped noisy estimate. Both are formed on the profile's grid, a tie being a tie on
  the grid, so the block's `fractionalBits` must equal the profile's (PROFILE-SPEC.md section 7). A pair is read
  in either order, since both statistics are symmetric, and a DATE column needs no conversion. Forming the run's
  side sorts the pair's values over the whole run, `O(n log n)`, and fewer than two rows that hold both values
  refuse the property (`FLX-5005`). A profile that holds no such statistic over the pair refuses it (`FLX-5037`),
  and one that withheld the group makes the row `censored`. The score is `1 - |t - t'| / 2`, an exact rational,
  rounded down to 12 places as a correlation's is (§2). The outcome is `within` when
  `|t - t'| <= margin + w_noise + w_sampling` (§9b), where `margin` is the compiled absolute margin of the
  property's tolerance (`{ type: absolute, max }`, `0.05` by default), `w_noise` is zero for an exact reference
  and `w_sampling` follows the run's size. When that sum is 2 or more, every run is within, and the row says so
  (`tooNoisyToTell`). The row's `rank` member holds `measure`, `records`, `run`, `reference`, `distance`,
  `samplingWidening`, `reach` (the sum above) and `tooNoisyToTell`, and `alpha`, the shrinkage of the copula the
  run's plan states for the emit (KERNEL-SPEC-2.md section 8.6), when it has one. A noisy reference adds the
  `noise` member of §9. A committed slice and a sampled `verify` name the property and compare nothing, since `S`
  and `A4` do not add up across blocks (SAMPLE-SPEC.md section 4). A shrunk copula is not widened for: the run is
  scored against what the profile published, so a large `alpha` shows as an outside score, and the row carries
  `alpha` for the reader.
- **A property grouped by bins** (`groupByBins`, `joint`) reads as a `groupBy` row, with the groups keyed by the
  lower edge of each bin and named by ordinal in the order of the bins. The row holds `groupByBins`, `{column,
  bins}`, in place of `groupBy`. A noisy reference is widened group by group (§9).

A relationship is scored whenever its parent and child are both emitted by the
run, whatever the profile holds. A fact is a row for every rule of an emit's contract
and of the bundle, whether or not the profile measured it. A table that declares no
fidelity block but has such rows is measured on them. No key, value or row of the
data appears in any of these rows: they are counts and exact shares.

**A table and a dimension** score the weighted mean of their scored
properties. **The run** scores the mean of its measured dimensions, each
weighted equally, so that many marginal properties cannot drown a few joint
ones. Every weight is `1` in this format and is written out (`"weight":
"1"`) so that declared weights are a change of value, not of shape.

A censored or refused property is shown and left out of every mean. A table,
a dimension or the run with no scored property is `not measured`, and its
score is `null`. A table whose every property was stopped by one fail-closed
condition (the profile holds no table for the expose, a row count outside
`records`) is `refused`, with that condition's code. An emit with no fidelity
block is listed with `declaresFidelity: false` and is not measured.

Each table also carries `dataQualityAssertions`, an OpenLineage
`DataQualityAssertionsDatasetFacet` (1-1-0) with one assertion per property
that was compared: `assertion` is the statistic's identifier, `success` is
whether the outcome is `within`, `name` is the property id, `severity` is
`error` (an outside property withholds), `column` names a single-column
property's column, and `params` holds the dimension, the outcome, the declared
tolerance, the score when there is one, the `groupBy` and a pair's two
columns. A censored property ran no comparison and has no assertion.

## 3a. The `queries` dimension (format `1-4-0`)

A bundle that declares `consumers` (ADR 0028) has every one of its queries counted on the run by `verify`. The
`queries` entry of `dimensions` then holds a `queries` member, and the scorecard names `1-4-0`. The member is
in its own block of the schema (`Queries` and the definitions that follow it), and a scorecard of a bundle that
declares no `consumers` keeps the bytes it had.

The `queries` member has a `status`. `counted` means the queries ran in a locked DuckDB over the run's emitted
streams, after each stream was digested as `outputDigest` is and compared with the receipt's. `not counted`
means they did not: the `sql` extra is not installed, or the run's artifacts do not reproduce
`run-digest.json`. A `not counted` member holds a `detail` and the `install` command, and the dimension is
`not measured`, so a threshold declared on it is not met, as any threshold on a score that was never formed is
not.

A `counted` member holds:

- **`queries`**, one row per query of the registry, in its order: `name`, `set` (`calibration` or `heldOut`),
  `digest` of its canonical text, `rows` the query returned, `unitRows` its unit holds, `rate`, `target`, `gap`
  (the rate less the target, exact and signed), `tolerance`, `status`, `met` and `reason`. The rate is
  `rows / unitRows` as an exact fraction. A query without a `unit` has no rate; it is `measured` when it
  returned rows and `noRows` when it returned none. A query with `groupBy` also holds `groups`, one row per
  group of the unit's column with its own counts, rate, target, gap, status and `met`; groups are named by
  ordinal as every group of a scorecard is (the null group first, then the levels by code point), and the
  query's own `target`, `gap` and `tolerance`-bound `met` are the groups' together (`met` is true when every
  group that has a target is within, and `null` when none has).
- **`status`** of a query is `measured`, `noRows` or `notARate` (the query returned more rows than its unit
  holds, so it is no share of the unit and is never compared), `noTarget` (the profile holds no rate for it),
  `withheld` (the profile withheld the cell its rate would come from), `timedOut` or `outOfMemory` (the query
  passed its limit), `crashed` (the query process died), `error` (DuckDB refused the text at execution), each with
  a `reason` that names no value, or `versionsDiffer` (the bundle
  recorded a sqlglot version other than the one running, so the query is never compared).
- **Targets.** A calibration query's target is the `true` count over `k` of the entry of its name in the
  `queries` section of the profile the bundle pins, per group of its `groupBy` column for profile format
  `1-3-0`. A held-out query's target is read the same way from the profile given to `verify --held-out`, which
  no bundle pins. The pinned profile may not name a held-out query (`FLX-3701`), and the held-out profile may
  not name a calibration query (`FLX-3702`); either is refused before anything runs, naming the query.
- **Tolerance** is the query's own `tolerance`, or for a held-out query the registry's
  `calibration.heldOutTolerance`. A query with no target or no tolerance is `met: null` and is in no share.
- **`shares`**: of the calibration queries, and of the held-out queries, with a target and a tolerance, how
  many fall within it (`{covered, of, share}`, as in §3). The dimension's `score` is the smaller of the shares
  that exist, and it is `not measured` when neither does. Its `counts` are the queries (`properties`), those
  in a share (`scored`), those with no target (`censored`) and those the query process did not answer or the
  versions kept from comparing (`refused`). The run's overall score takes the dimension as it takes any
  measured one.
- **`requirements`**: each literal, pattern, sentinel and range a query needs the copy to hold, derived from
  the queries' text (ADR 0028 section 5), `met` or `unmet` by counting the rows that meet it. A requirement
  repeats what the consumers wrote and names no value of the data. `verify` reports an unmet requirement and
  fails nothing for it.
- **`versions`**: the duckdb, sqlglot and pyarrow versions that counted.
- **`grades`**: the privacy grade of the pinned profile beside the held-out profile's (ADR 0020, rule 2), or
  `null` for one that was not read.

A bundle's `limits` may only lower the engine's ceilings of 30 seconds and 1GiB, and every stage refuses a
bundle that asks for more (`FLX-3614`, `FLX-3615`). `verify` and `ship` take `--max-query-seconds` (up to 600)
and `--max-query-memory` (up to 16GiB), which raise the ceiling of the command that runs the queries and
nothing else: a query runs with the bundle's own value where it declares one, and with the raised or default
ceiling where it does not. A value outside those ranges is refused with `FLX-3700`.

**Coverage.** A score says how close the run is to the profile on the
properties that were scored. It does not say how much of the contract those
properties are about, so the scorecard states that beside the score, per table
and for the run:

- `columns` counts the columns of the emit's contract schema that carry at
  least one scored property: a property that has a score and names the column
  (a pair names two). The column a property is grouped by is not named by it.
  A column whose every property is censored, refused or absent is not
  covered. `covered` is that count and `of` is the number of columns the
  contract declares.
- `relationships` counts the relationships the contract declares, in the
  child expose's `semantics.relationships` (FLUID 0.7.7), that the profile
  constrains. The profile constrains a relationship when its top-level
  `relationships` list holds an entry for that parent and child, with the
  same key columns (paired by position), and at least one of the entry's
  statistics has a published group. `covered` is that count and `of` is the
  number of declared relationships. In format `1-3-0` its referential integrity is
  scored under `crossTable` whether or not the profile constrains it, and in format
  `1-4-0` its children per parent are scored there when the profile describes them.

Each of the two is `{"covered", "of", "share"}`, where `share` is the exact
`covered / of`, or `null` when `of` is `0`: nothing was declared, so there is
no share to state. A table's `coverage` is `null` when the run records no
contract for the emit, which is the case for an emit with no fidelity block.
The run's `coverage` sums `columns` and `relationships` over the tables that
have one, and `tables` says how many tables are `counted` of the `total`, so
a reader sees how many it leaves out. Coverage is not a dimension and enters
no mean.

The text output prints it with the overall score, as in

```text
scorecard: overall 0.93 against profile sha256:... (covers 12 of 20 columns, 0 of 3 relationships)
```

and a table's line ends with the table's own counts. When tables are left
out the parenthesis says so, and when none records a contract it says that
coverage is not known.

## 4. Thresholds and `minCredibility`

A Simulation that pins a profile can declare floors on its scores under the
top-level `scorecard` key of the engine's namespace, beside `profile`:

```yaml
extensions:
  com.agentics.flux/1:
    profile:
      digest: sha256:af97633046a8c9621d32fec595dd02672bc10e3fabd98c846a2ffcd713e89361
    scorecard:
      thresholds:
        overall: { mustBeGreaterOrEqualTo: "0.9" }
        dimensions:
          marginals: { mustBeGreaterOrEqualTo: "0.95" }
        tables:
          payment_recovery_moment: { mustBeGreaterThan: "0.8" }
      coverage:
        columns: "0.8"
        relationships: "0.5"
      shape:
        maxFlags: 0
    emits:
      payment_recovery_moment:
        rows: 64
```

- `scorecard` holds `thresholds`, `coverage` and `shape` and nothing else, at
  least one of them. `thresholds` holds `overall`, `dimensions` (keyed by
  dimension) or `tables` (keyed by `exposeId`), at least one threshold in
  all.
- `coverage` holds `columns`, `relationships` or both, each an operand (below)
  that floors the `share` of §3 with `mustBeGreaterOrEqualTo`: the run's
  `coverage` of that kind, summed over the tables that record a contract. It
  is the floor of a share of the contract, not of a score, and it is met or
  missed as a score is. A share that is `null` meets no floor, so a floor on
  `relationships` over a contract that declares none is not met: remove it.
- `shape` holds `maxFlags` and nothing else: the most shape flags (§6) the
  run may show, summed over every column of every emit, as a whole number of
  0 or more. It is the one key that bounds something from above, and it is
  opt-in: a run is withheld, or `verify` fails, only for the limit it declares
  (§6). `maxFlags: 0` allows no flag.
- A threshold holds exactly one operator, one of the Open Data Contract
  Standard's quality operators: `mustBeGreaterOrEqualTo` or
  `mustBeGreaterThan`. A threshold is a floor, and ODCS's other six operators
  (`mustBe`, `mustNotBe`, `mustBeLessThan`, `mustBeLessOrEqualTo`,
  `mustBeBetween`, `mustNotBeBetween`) bound a score from above or exclude a
  band, so they are refused rather than read as something else.
- The operand is an exact decimal between 0 and 1, with no sign and no
  exponent: a string, a YAML number read as an exact decimal, or `0` or `1`.
  A float in a document built some other way, and a bool, are refused.
- Refused too, because each could only withhold: a block in a Simulation that
  pins no profile; `mustBeGreaterThan` `1`; an `overall` threshold when no emit
  declares a fidelity property this engine scores, and the same for
  `coverage`; a dimension no property
  kind of this engine is scored in (§3); a dimension no declared fidelity
  property is scored in, other than `invariants`, `crossTable` and `queries`, which are
  scored from the contract's rules and relationships and from the consumers' queries (§3a), and
  `temporal`, which is scored from the calendars the profile publishes, and not from a declared
  property (so a threshold on one of them is not met when nothing under it was scored, and `ship`
  withholds); a table the Simulation does not emit; a table that
  declares no fidelity block; and two spellings of one name (keys equal under
  Unicode NFC).

Every refusal is `FLX-3105`, raised by `validate` as by every other command,
before anything is written. A bundle that declares no `scorecard` block
behaves exactly as before.

**The plan records them.** When the Simulation pins a profile, `plan.json`
holds `profileDigest`, the pinned digest, and, when thresholds are declared,
`scorecard`: `{"thresholds": [{"target", "operator", "value"}]}`, overall
first, then the dimensions in the order of §3, then the tables by
`exposeId`, then the coverage floors, `coverage.columns` before
`coverage.relationships`, each with the operator `mustBeGreaterOrEqualTo`. A
declared shape limit is `scorecard.shape`, `{"maxFlags": n}`, and the plan of a
Simulation that declares none has no such key. `verify` and `ship` read the pin and the thresholds only through
the plan, which its own digest binds, never from a bundle directory nothing
binds to the run. A plan whose Simulation pins nothing holds neither key, so
its bytes and its `planDigest` are the ones it had.

**A threshold is met** when the score it names exists and clears the floor,
compared exactly. A score that is `null` meets no threshold. A shape limit is met
when the flags counted are at most `maxFlags`. A coverage floor is read in the
same list as `thresholds`, with the target `coverage.columns` or
`coverage.relationships` and the `share` as its `score`.

**`minCredibility`.** Wherever a property measures something, the computed
score replaces the declared credibility. An emit that declares
`minCredibility` is read against its table's score when the table has one:
the entry `credibility` of the table is `{"basis": "measured", "met": ...}`,
and a measured score below the floor is not met. When the table has no
score, the basis stays `declared` and `met` is `null`: the Playback's
declared credibility, which `fluxsim validate` checks offline, is the only
basis there is.

## 5. `verify` and `ship`

Both take `--profile PATH` and pair it with the plan's pin as `plan` pairs
it (PROFILE-SPEC.md §6): a profile handed to a run that pins none is refused
with `FLX-3109`, another profile with `FLX-3110`, and a document that is not
a profile with `FLX-5036`, before any leg runs. `fluxsim run --profile PATH
--verify --ship` hands its profile to both. The profile is never copied into
the run directory or the evidence pack.

Every emit that declares a fidelity block is compared with the profile.

**`verify`.** An emit with no `--committed-slice` is checked against the
profile in the fidelity leg, which says so (`"reference": "profile"`) and
lists the properties the profile left nothing to score (`censored`). An emit
with a slice is checked against the slice as before, and against the profile
too: the leg fails when either finds it outside, as `ship`'s gate does, and
the profile's result is under `profile`. The scorecard is in
`artifacts["scorecard"]` as `{"status": "computed", "met", "document"}`, and
its lines follow the legs. A missed threshold, or a measured score below an
emit's `minCredibility`, fails the stage. A run whose plan pins a profile and
is handed none reports its fidelity legs and its scorecard as not checked and
says to pass `--profile`. A run whose artifacts do not reproduce
`run-digest.json` (`FLX-2110`) is not scored at all: its rows are not the
run's. One emit's records that cannot be read (its fidelity input, its seam
attestation, a row) refuse that emit, and its table is `refused` on the
scorecard. A run whose plan pins none has no `scorecard` key. The shape flags (§6) are
in the document and in the lines, and fail nothing unless the plan records a
shape limit (§4), when more flags than `maxFlags` fail the stage, and the
scorecard's `met` is false, with a line that counts the flags and names each
flagged column.

**The consumers' queries.** A bundle that declares `consumers` has its queries counted by `verify`
whether or not the plan pins a profile (§3a): `artifacts["queries"]` holds the `queries` member, and its lines
follow the legs, inside the scorecard's when one is formed. `--held-out FILE` gives the held-out profile. A
run with no `--profile` has no calibration targets, so its calibration queries are `noTarget`. A queries
refusal (a held-out profile that is not apart, a ceiling flag out of range, a stream that is not the run's)
is named by its code, and a threshold declared on `queries` fails the stage when it is not met. `ship`
counts the queries with the same functions, takes the same three flags, and forms the same dimension in the
scorecard it gates on, so a threshold on `queries` withholds a ship exactly as it fails `verify`. Without the
`sql` extra the dimension is `not measured` in both, with the install command. A run that pins no profile forms
no scorecard, so `ship` counts no query for it.

**`ship`.** A run whose plan pins a profile is refused with `FLX-3109` when
no profile is handed over, before anything is written. Each emit's recorded
contract is pinned to the plan first: `contract.plannedH` compares the `H` the
gate recompiles with the emit's `H` in `plan.json`, and `contract.plannedG`
compares the emit's `G` in `plan.json` with the seam's reusable digest
recomputed from `fidelity-input-<exposeId>.json` by the function `plan` took it
with (SEAM-FIDELITY-SPEC §3), which leaves out the `facts`, `links`, `bridges`,
`counts` and `copies` members. The compiled block's own `G`, which committed
states are bound to, keeps `facts`. Each emit's properties
are compared with the profile, and an evaluation that is not within (an
outside property, a refusal, or nothing scored at all) withholds the emit with
the check `profile.properties`, which names each property outside its
margin; an emit whose recorded fidelity input cannot be read is withheld with
`fidelity-input.readable`. With a committed slice as well, the slice's
gate runs as before and earns the signed record, and the emit ships only when
both references find it within. Then the scorecard is formed and gated:

- a missed table threshold, or a measured score below the emit's
  `minCredibility`, withholds that emit;
- a missed coverage floor withholds every emit whose own coverage of that
  kind is below the floor, and when every counted table meets it alone, every
  counted table; failing that, as a missed overall threshold does;
- a missed dimension or overall threshold withholds every emit whose scored
  properties feed the score; when none was scored, every emit with a property
  under it; and failing that every emit that declares a fidelity block, so a
  miss always withholds something;
- each gets a failed check, `scorecard.threshold` or `scorecard.credibility`,
  that names what was missed and the lowest-scoring property under it;
- more shape flags than a declared `maxFlags` allows withhold every emit with a
  flagged column, with the failed check `scorecard.shape`, which counts the
  flags and names each flagged column. A run that declares no `maxFlags` is
  never withheld for a flag.

Any withholding withholds the whole ship (SEAM-FIDELITY-SPEC §7): nothing is
written to the output directory, and `artifacts["scorecard"]` holds the
document so the reader sees every score. A ship that passes writes
`scorecard.json` into the run directory and packs the bytes it computed, not
whatever the file holds by the time the directory is copied. The comparison
with a profile writes no fidelity record, and the scorecard is not signed:
the pack's manifest digests it with every other file, and a reader who holds
the profile forms it again with `fluxsim verify --profile`. A fidelity record
already in the run directory for an emit this ship gave no committed slice (an
earlier ship's, say) is packed as the directory holds it: this ship never
reads it, so `EVIDENCE.md` names it on that emit's line and attributes it to
no key, and its Signature line speaks only of the records this ship wrote.
`EVIDENCE.md`'s Fidelity section summarises the scorecard, its shape flags
included, and "What Was Not Proven" says what it does not show. A shape flag
withholds only under a declared `maxFlags`.

## 6. Shape flags

`verify` and `ship` read every emit whose table the profile describes, and
in it every column the profile describes, whether or not a fidelity property
is declared on it (shape flags). A value is read as the profile reads its
column's domain: an exact decimal, integer milliseconds since the epoch, or
an NFC string. A categorical column also reads a boolean as the string `false` or `true` and a whole number as the
string of its decimal text (`7`, `-3`), as SEAM-FIDELITY-SPEC §2 says; any other class that is not a string refuses
the column. A `YYYY-MM-DD` value of a timestamp column is the millisecond its day starts
at, in UTC, which is how a profile holds a date column (PROFILE-SPEC §7). A value that cannot be read so
refuses its column with
`FLX-5005` (a timestamp with `FLX-5022`), naming the row and the value's
class, and no other column. An emit whose rows cannot be read is refused as
a whole, as §5 refuses it, and a column the rows do not carry is `absent`.

The checks read the column's ungrouped count table, the first published
`marginalHistogram/1` (a number or a timestamp) or `levelHistogram/1` (a
category), and its `count` and `distinctCount` metrics. A value falls in a
bin as the fidelity check bins it: floored onto the profile's grid, in
half-open bins with the last closed at the top. A value below the first edge or above the
last, compared exactly and not on the grid, and a category that is not one
of the levels, is outside every cell.

Below, `m` is the run's non-null records in the column and `n` those of them
outside the withheld cells: in a published cell, or in none. A record outside
every cell is one the profile gives no chance at all, so it counts against
every cell. A withheld cell is never tested, and the run's records in it are
not in `n`: PROFILE-SPEC.md §7 compares a count table over its published
cells only. A published count `c` under a rule with `roundingBase` `b` may
differ by up to `h = b - 1` from the count it was rounded from (`h = 0`
without rounding), so a cell's share of the published total is read as the
share least favourable to a flag: the smallest it allows, `(c - h) / ((c -
h) + sum of (c' + h))` over the other published cells, for an empty cell;
the largest, `(c + h) / ((c + h) + sum of max(c' - h, 0))`, for a point
mass.

| Flag | Raised when |
|---|---|
| `EMPTY_BINS` | a published cell whose count is above `h` holds no record of the run, and `(1 - p)^n`, the chance of that under the cell's smallest share `p`, is at most the test level. The flag lists every such cell. |
| `IMBALANCE` | a value holds at least a tenth of the `m` records, and no cell of the profile holds it, or the published cell holding it makes that many records too unlikely: Chernoff's bound on `P(X >= k)` for `X ~ Binomial(n, p)`, with `k` the value's records and `p` the cell's largest share, is at most the test level. Every such value is tested and flagged on its own; one in a withheld cell is not tested. |
| `CONSTANT` | one distinct value in two or more records, where the profile shows more than one and makes that unlikely: the chance that every record holds one value is at most the test level. With a count table showing two cells above `h`, that chance is read as at most `sum of p^m` over the published cells at their largest shares; with none, from `count` `N` and `distinctCount` `D` (read as `D - h`, more than 1), as at most `((N - D + 1)/N)^m + (D - 1)/N^m`, the most lopsided column those counts allow, with `N` read at `N + h`. A value outside every cell has no chance at all; one in a withheld cell is not tested. |
| `HIGH_CARDINALITY` | a distinct count above twice the most the profile allows. |
| `LOW_CARDINALITY` | a distinct count of two or more below half the fewest the profile allows. A column of one value is `CONSTANT`'s to judge. |

What the profile allows is the number of distinct values `m` independent
records are expected to show, `sum of (1 - (1 - p)^m)` over the cells they
can hold. Over a `levelHistogram/1` whose every cell is published, the
fewest reads each level at its smallest share and the most at its largest,
at any `m`. Otherwise both come from the profile's `count` `N` and
`distinctCount` `D`: records drawn from `N` holding `D` distinct values
show no more than `D` equally likely values would, `D (1 - (1 - 1/D)^m)`,
with `D` read at `D + h` (at most `N`), and no fewer than `D - 1` values of
one record each and one holding the rest would, with `D` read at `D - h`
(at least 1) and `N` at `N + h`, where that bound is lowest. They are read
only for `m` no larger than `N` less its slack: the profile says nothing of
values a sample larger than its own never held. A column with neither is
not checked for its distinct count.

A column with no non-null record takes no test, and the lines say so.

**The test level.** The run takes one test per cell that can be flagged
empty (published, with a count above `h`), one per published cell for a
point mass, since a value's test is a test of its cell's count, and one per
column whose profile can say how unlikely a constant column is, across every
emit. Each test is taken at `1/1000`
divided by their number (Bonferroni), so a run drawn from the profile's own
shares shows a false `EMPTY_BINS`, `IMBALANCE` or `CONSTANT` flag in at most
one run in a thousand. The distinct-count flags are not tests at a level:
they compare a count with the bounds above.

**Exactness.** `(1 - p)^n` is the empty cell's exact chance. Chernoff's bound
in Hoeffding's form, `(np/k)^k * (n(1 - p)/(n - k))^(n - k)` for
`np < k < n`, is never below the tail; `k = n` reads `p^n` itself, and
`k <= np` is shown nothing below 1. Each of these and the occupancies are
decided against their levels exactly (`flux_stats.tails`): from
two binary bounds rounded outwards at every product, at a precision that
doubles while the level lies between them, and past 8192 bits from the exact
`Fraction`. An occupancy is decided through its expected empty mass, in
relative precision, so a large run whose occupancy lies a hair below its
cells is still decided from the bounds. No number on the way is a float.

**The document.** `shapeFlags` holds `status` (`checked`), `level`
(`1/1000`), `tests`, `testLevel` (the level of each test) and `tables`: one
entry per emit by `exposeId`, whose `status` is `checked`, `refused` (with
`code` and `reason`) or `not profiled`. A checked table lists its `columns`
by name: `checked` with `records`, `distinct` and `flags`, `refused` with
`code` and `reason`, or `absent`. A flag is `{"alert": "EMPTY_BINS",
"cells": [...]}`, `{"alert": "IMBALANCE", "cell": i, "count": k}` (`cell`
is `null` when no cell holds the value), or `{"alert": "CONSTANT"}`,
`{"alert": "HIGH_CARDINALITY"}` or `{"alert": "LOW_CARDINALITY"}`. Cells are
named by their ordinal in the profile's statistic. No value of the data
appears, and no count, share or distinct count of the profile: the predicted
distinct count, for one, is left out, since it and the run's `records` would
give back the profile's `distinctCount`.

**The flags inform, unless the Simulation says otherwise.** Without a
`maxFlags` (§4), `met` does not read them, `verify` does not fail on them, and
`ship` withholds nothing for them. With one, the run's flags are counted over
every column of every emit, and `met` is false when there are more than
`maxFlags`; `verify` then fails, and `ship` withholds each emit that has a
flagged column. A column that was refused, absent or empty carries no flag and
adds none. `maxFlags: 0` is the strict reading: a constant column, an empty bin
or any other flag fails the run. A Simulation that declares no `shape` key
behaves exactly as it did, and its plan holds the same bytes. `verify`'s lines
and `EVIDENCE.md` say how many columns were checked and name each flagged
column.

## 7. What a scorecard does not show

- It compares the run with the profile's published aggregates, under the
  properties the bundle declares, and with nothing else. A dimension marked
  not measured was not checked at all.
- A censored property was not scored: what the profile withheld is unknown,
  not zero.
- A score speaks only for what was scored. Coverage says how much that is,
  and does not say the uncovered columns match: a column with no scored
  property was not compared. A relationship counted as constrained by the
  profile was not scored for its children per parent, since nothing yet scores
  that; its referential integrity is scored apart (§3). A
  table whose contract the run does not record is left out of the run's
  coverage, and `tables` says so.
- A score says how close the run is to the profile. It says nothing about
  whether the profile describes its source accurately, and nothing about
  privacy or fitness for any use.
- No value of the data appears in it: no group key, no category, no
  statistic of the profile. Groups are named by ordinal. A score is a
  function of the profile's published statistics and the synthetic rows, so
  a reader who holds the rows can work back towards a profile statistic from
  its score: a mean's score and the run's own mean leave two candidates for
  the profile's mean, one on each side. The scorecard therefore discloses no
  more than the profile already published, and goes wherever the evidence
  pack goes.
- A shape flag says a column is unlikely under the profile's shares. No
  flag says only that the tests found none at their level, and a check the
  profile gave nothing to read (no count table, no `count` and
  `distinctCount`) was not run.
- The shape flags carry the run's own counts and the profile's cells by
  ordinal only. An `EMPTY_BINS` cell, and the number of tests, say that the
  profile counts more than its rounding slack in a cell (the number of tests
  also says whether it can bound a constant column); a cardinality flag
  says on which side of twice or half the prediction the run's distinct
  count lies. That is a bit per cell or column, and no more than a score
  already discloses.

## 8. Compatibility and later additions

The format grows by minor versions, as the profile format does
(PROFILE-SPEC.md §8): a minor version adds an optional key, a status value or
a dimension's first property kind, and never changes what a key of format
`1-0-0` means. Format `1-5-0` adds the `rankCorrelation` row and a property row's `groupByBins` member (§3), the `joint`
dimension's second scored kind, and the widening of §9b. Format `1-4-0` adds the children-per-parent row and the calendar row (§3), the dimension `temporal` its first scored rows, the `queries` member of the `queries` dimension (§3a), the `sampling` member of a property row's
`widening` (§9a) and the top-level `overlay` member (§1). Format `1-3-0` adds the fact and relationship rows (§3), the
dimensions `invariants` and `crossTable` their first scored rows, and the `noise` key of a property
row compared with a noisy statistic (§9). Format `1-1-0` added the shape flags (§6):
`shapeFlags` gained the status `checked` and the flags per column. Format
`1-2-0` adds the coverage (§3): `coverage` on the run and on every table, and
the targets `coverage.columns` and `coverage.relationships` among the
thresholds. The `shape` key of the Simulation's `scorecard` block (§4) adds no
key to the document: a missed `maxFlags` is a `met` of false, and the limit is
in the plan. The schema of formats `1-0-0` and `1-1-0` is kept in
`scorecard-1.schema.json`, the schema of `1-2-0` in `scorecard-1-2.schema.json`, and the
schema of `1-3-0` in `scorecard-1-3.schema.json`, as published. A scorecard that holds no
rank correlation row, no `groupByBins` member, no children-per-parent row, no calendar row, no `queries` member and no `sampling` member names `1-3-0` or `1-2-0` as it did before, and is byte for byte what
that format wrote. The planned additions:

- **Intervals (sample-based verification):** a sampled `verify` already reports
  each estimate with its sample size and interval in its own leg
  (SAMPLE-SPEC.md); `intervals` gains the status `computed`, and the same
  estimates, once `ship` samples too.
- **Declared weights:** a property's `weight` takes the declared
  value.
- **The other dimensions:** a new property kind lands in the dimension §3
  names for it, which then stops being `not measured`.

## 9. A noisy reference widens the margin

A profile can release a count statistic under differential privacy (PROFILE-SPEC.md §3a). Its
counts are point estimates with a known variance, so the run is no longer compared with the data's
shares but with an estimate of them. A property compared with a noisy `levelHistogram/1` or
`marginalHistogram/1` (a statistic the profile writes with a `noise` block, or one formed from a
clique, PROFILE-SPEC.md §5a) has its compiled margin widened, group by group, before the comparison
of §2 is made. The comparison is otherwise unchanged: within when the total variation distance of
the two sides, over the cells compared, is at most `margin + w`.

For a group of `m` cells, each with a count of variance `v`, whose noisy values add up to `T` (the
exact estimated number of records) and whose point estimate is spelled as whole counts over `K`
records (PROFILE-SPEC.md §3a):

```
w = sqrt(m) * ( sqrt(m * v) + sqrt(10 * v) ) / T  +  m / (2 * K)
```

Why this is a bound. The shares of the estimate err by `eta_i / T`, where `eta_i` is a cell's noise,
and the total variation distance from the true distribution is at most `|eta|_1 / T` (the
normalisation doubles the distance of the counts and halves it again). With `m` cells,
`|eta|_1 <= sqrt(m) * |eta|_2`, and for Gaussian noise, with probability at least `1 - e^-t`
(B. Laurent and P. Massart, "Adaptive estimation of a quadratic functional by model selection", Annals
of Statistics 28(5):1302-1338, 2000, Lemma 1), `|eta|_2 <= sqrt(m v) + sqrt(2 t v)`. The formula takes
`t = 5`, a confidence of about 0.993. Its last term is the most that the whole counts the estimate is
spelled in (the largest remainder of PROFILE-SPEC.md §3a) can differ from its shares. Each square root
is rounded up to 12 decimal places in exact integers, and `w` to 12 places, so it is a terminating
decimal everywhere and never below the bound.

**What the bound assumes.**

- **Independence, or less.** It needs the noise of the cells of one group to have a covariance at most
  `v` times the identity. PROFILE-SPEC.md §5a requires it of a clique, and independent noise, or
  independent noise made consistent by least squares, meets it.
- **Tails.** The statement is for Gaussian noise. A discrete Gaussian of scale `s` is sub-Gaussian with a
  variance proxy of `s^2` (C. L. Canonne, G. Kamath and T. Steinke, "The discrete Gaussian for
  differential privacy", NeurIPS 2020), and the same tail holds for a sub-Gaussian vector (D. Hsu,
  S. Kakade and T. Zhang, "A tail inequality for quadratic forms of subgaussian random vectors",
  Electronic Communications in Probability 17, article 52, 2012), so the statement holds for it with
  `v = s^2`. A Laplace's tails are heavier. The same `w` is used with its variance (`2 s^2`), and its
  pass rate is the one `tests/concordance/test_noisy_reference.py` measures, not this statement.
- **The projection.** The bound is derived for the estimate before its non-negative projection
  (PROFILE-SPEC.md §3a), and the reference compared is the projected one. The Euclidean projection onto
  the non-negative vectors with sum `T` does not increase the distance in `|.|_2` to any vector of that
  set, and the true counts scaled to total `T` are one, so the projection does not move the estimate
  away from them in `|.|_2`. The step from there to the true counts, whose total is not `T`, is not
  proved here: the engine assumes the bound holds for the projected estimate, and says so.
- **Sampling variance is not added.** The run is compared with the profile's distribution, as for an
  exact reference; the tolerance a Simulation declares already stands for sampling at the run's size.
  The widening is for the reference's noise alone.
- **Derived statistics.** A one- or two-way histogram formed from a clique sums `c` noisy cells per
  cell, so its cell variance is `c` times the clique's (PROFILE-SPEC.md §5a).

**What a pass says.** Let `tau'` be the compiled margin. A run whose distribution is within `tau'` of the
data's passes with probability at least `1 - e^-5` over the noise: the noise moves the reference at most
`w` from the truth, and the check allows `tau' + w`. A pass places the run within `tau' + 2 w` of the
data's distribution, with the same probability: it is within `tau' + w` of the reference, which is within
`w` of the truth. `w` is per group and depends on the data, because it reads the group's total `T`, so
an author reads it from the scorecard row (`noise.widening`, the largest over the groups) and cannot
state it in advance. A Simulation that wants a pass to mean a run within `tau'` of the truth declares
`tau' - 2 w` once it has read `w`; nothing in the engine checks that this is above zero. A group whose
noisy values add up to nothing above zero has no records to widen over, and the comparison is
refused (`FLX-5005`).

**Two noisy statistics.** A column in a clique is drawn from the clique, so when a property compares its
run with another noisy statistic the document holds for the column (its own histogram, with its own
noise), the run is within `w_draw` of the truth in distribution and the reference within `w_reference`
of it. The check widens by the sum, `w = w_reference + w_draw`, where `w_draw` is the widening of the
clique's own answer to the same question (the same cells and group). Each bound holds with probability
at least `1 - e^-5`, so by a union bound the two together hold with probability at least `1 - 2 e^-5`,
about 0.987, and the statements of the paragraph above carry that probability when two statistics are
summed. A reference formed from the clique is the one the column is drawn from, and adds nothing.

A statistic grouped by a link (`groupByLink`, PROFILE-SPEC.md section 3) or a children-per-parent histogram
grouped by a column of the parent (§3) is widened in the same way, group by group, each group by its own `w`.

A property row compared with a noisy statistic carries `noise`: `mechanism`, the mechanism it was
released under, `cellVariance`, the variance of each cell, and `widening`, the largest `w` any of its
groups took, the sum above when there are two statistics, both exact (format `1-3-0`). The
margin the row shows is still the compiled `tau'`, and an exact reference adds no key. A sampled
`verify` (SAMPLE-SPEC.md) widens a histogram's margin in the same way.

## 9a. A tolerance that follows the row count widens the margin

A distance tolerance in a fidelity block may declare `sampling: true` (SEAM-FIDELITY-SPEC.md §2). The run
is a sample of `n` rows, so its shares differ from the reference's by sampling alone even when the
generator is exact in expectation. The margin of the comparison of §2 is then widened, group by group, by
the same bound as §9 with the sampling variance in place of noise. For a group of `m` cells compared, `n`
the run's rows in the group and `p` the largest share of the reference, each cell's count has a variance of
at most `v = n * p`, because the counts of one multinomial draw have a covariance of at most `n * diag(share)`:

```
w = sqrt(m) * ( sqrt(m * v) + sqrt(10 * v) ) / n  +  m / (2 * n)
```

The total and the record count of §9 are both `n`. Each root is rounded up to 12 decimal places and `w` to
12 places, as in §9. For a column declared `allocation: exact` the run places whole counts by largest
remainder, so its shares carry no sampling variance and only the rounding term `m / (2 * n)` remains,
rounded up to 12 places. A tolerance without the key is compared exactly as before, and a scorecard that
holds no such widening is byte for byte what it was.

**What the bound is.** The statement is for the Gaussian approximation of the multinomial: with that
approximation, the distance of the run from the reference is within the margin and `w` with probability at
least `1 - e^-5`. A multinomial is not Gaussian, and its tails at a small `n` differ from the Gaussian's,
so the pass rate at small `n` is the one a test measures
(`tests/concordance/test_sampling_verify.py`), as §9 says of Laplace noise, not this statement.

**The row.** A property row whose tolerance declares `sampling: true` carries `widening`, an object with
the member `sampling`: the widening applied, the largest over its groups, exact (format `1-4-0`). A
scorecard with such a member names format `1-4-0`, and the schema is
[`scorecard-1-4.schema.json`](scorecard-1-4.schema.json). The margin the row shows is still the compiled
`tau'`. A property that is also compared with a noisy statistic keeps its `noise` member, and both
widenings are added to the margin.

## 9b. A rank correlation widens the margin (format `1-5-0`)

A rank correlation is one scalar, so its widening is that of a scalar. The row is within when the distance of
the two values is at most `margin + w_noise + w_sampling`, each width rounded up to 12 decimal places in exact
integers, so that it is a terminating decimal and never below the root.

- **`w_noise`**, for a reference released with noise (PROFILE-SPEC.md section 3a): `sqrt(10 * v)`, with `v` the
  variance of the released value, `scale^2` for a Gaussian and `2 * scale^2` for a Laplace. The released value
  exceeds its true value by more than that with probability below 0.002 for Gaussian noise, since
  `P(|Z| > sqrt(10))` is about 0.0016, and about 0.011 for Laplace noise, whose tails are heavier. The clamping of
  the released value to `[-1, 1]` does not move it away from the true value, which lies in `[-1, 1]`. For an exact
  reference `w_noise` is zero.
- **`w_sampling`**: `sqrt(10 * v_n)`, with `v_n` the variance of the plain statistic over `n` rows of two
  independent columns, ties aside: `2 * (2n + 5) / (9 * n * (n - 1))` for Kendall's tau and `1 / (n - 1)` for
  Spearman's rho. It is about `0.67 / sqrt(n)` for tau. It is the spread a run of `n` rows gives a rank
  correlation, taken at the confidence of §9, so that a small run is not held to a margin its own size cannot
  meet. It is not zero for an exact reference, and it falls as the run grows, so a large run is held to the
  margin.

The `noise` member of §9 is written when the reference is noisy, with `widening` equal to `w_noise`. A row whose `margin + w_noise + w_sampling` is 2 or more is marked
`tooNoisyToTell`: the statistic lies in `[-1, 1]`, so any run is within.

## 10. Prior art

- **SDMetrics** 0.33.0 (MIT): the tree. A score per item in `[0, 1]`, an
  unweighted mean per property group and of those for the overall score, an
  item that could not be scored shown and left out of every mean; and the
  measures TVComplement, StatisticSimilarity, CategoryAdherence and
  CorrelationSimilarity. Used as a pattern only: SDMetrics requires
  `copulas`, which is under the Business Source License 1.1.
- **mostlyai-qa** 1.10.11 (Apache-2.0): accuracy as `1 - L1/2` on the same
  cells, averaged per level and then across levels.
- **Great Expectations** 1.25.0 (Apache-2.0): a suite succeeds only when every
  expectation does, and a failed expectation is reported with what it
  observed. Here `met`, and the reason beside each check.
- **OpenLineage** `DataQualityAssertionsDatasetFacet` 1-1-0 (Apache-2.0): one
  assertion per property.
- **Open Data Contract Standard** 3.2.0 (Apache-2.0): the operator names of a
  threshold.
- **SynthEval** 1.7.3 (MIT) and **SDNist** 2.5.0: grouping by kind of
  question. Neither has these eight dimensions.
- **ydata-profiling** 4.20.0 (MIT): the alert names `CONSTANT`, `IMBALANCE`
  and `HIGH_CARDINALITY` (`model/alerts.py`). Its checks read one dataset
  against fixed thresholds; these read a run against a profile, and
  `EMPTY_BINS` and `LOW_CARDINALITY` have no ydata name.
- **SDMetrics** 0.33.0 (MIT) RangeCoverage, CategoryCoverage and
  BoundaryAdherence score the ends of a range and the categories covered,
  against real rows. None tests a gap inside the range or reads a profile.
- **Great Expectations** 1.25.0 (Apache-2.0)
  `expect_column_unique_value_count_to_be_between` and
  `expect_column_most_common_value_to_be_in_set`: a declared band on a
  distinct count and a check on the most common value, the pattern of the
  distinct-count and point-mass flags.
- **Hoeffding** (1963), Theorem 1: the bound on the binomial upper tail.
