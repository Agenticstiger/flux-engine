# Scorecard: normative specification (format 1-1-0)

A scorecard says how close a run is to the profile its Simulation pins
(PROFILE-SPEC.md), as exact scores per property, per table, per fidelity
dimension and for the whole run. `fluxsim verify` and `fluxsim ship` form it
when the run's plan pins a profile and the profile is handed to them with
`--profile`. A Simulation can declare thresholds on the scores, and `ship`
withholds a run that misses one. A shipped run carries its scorecard in the
evidence pack as `run/scorecard.json`.

Normative files beside this page:

- [`scorecard-1.schema.json`](scorecard-1.schema.json): the JSON Schema,
  whose `$id` is
  `https://agenticstiger.github.io/flux/engine/spec/scorecard/1-1-0/FluxScorecard.json`.
  The schema checks the shape; this page says how every number is formed.
- [`scorecard-example.json`](scorecard-example.json): a scorecard with a
  scored, a censored and a refused property, a table with no fidelity block,
  a missed threshold and a `minCredibility` read against a measured score.
  The tests validate it against the schema.

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
| `_schemaURL` | The schema's `$id`. The format version is part of it. |
| `run` | `runId`, `planDigest` and `runDigest` of the run scored. |
| `profile` | `digest`, the pinned profile's `jcs-document` digest. |
| `met` | Whether every declared threshold is met and every measured `minCredibility` is reached (§4). |
| `overall` | The run's score (§3). |
| `dimensions` | The eight dimensions, always all eight, in the order of §3. |
| `tables` | One entry per emit of the run, sorted by `exposeId`. |
| `thresholds` | The declared thresholds and what each read (§4). |
| `shapeFlags` | The shape flags (§6), with the status `checked`. A scorecard formed without them, and every document of format `1-0-0`, holds `{"status": "not checked"}`. |
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
| `joint` | `correlation`, and those four kinds with a `groupBy`: two columns of one table together |
| `crossTable` | nothing yet (relationships between tables, multi-table fidelity properties) |
| `temporal` | nothing yet (periods and series, multi-table fidelity properties and checks across periods) |
| `invariants` | nothing yet (declared invariants) |
| `queries` | nothing yet (consumer queries, query checks and query-result profiles) |
| `defects` | nothing yet (defect rates, declared defects) |

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
    emits:
      payment_recovery_moment:
        rows: 64
```

- `scorecard` holds exactly `thresholds`, and `thresholds` holds `overall`,
  `dimensions` (keyed by dimension) or `tables` (keyed by `exposeId`), at
  least one threshold in all.
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
  declares a fidelity property this engine scores; a dimension no property
  kind of this engine is scored in (§3); a dimension no declared fidelity
  property is scored in; a table the Simulation does not emit; a table that
  declares no fidelity block; and two spellings of one name (keys equal under
  Unicode NFC).

Every refusal is `FLX-3105`, raised by `validate` as by every other command,
before anything is written. A bundle that declares no `scorecard` block
behaves exactly as before.

**The plan records them.** When the Simulation pins a profile, `plan.json`
holds `profileDigest`, the pinned digest, and, when thresholds are declared,
`scorecard`: `{"thresholds": [{"target", "operator", "value"}]}`, overall
first, then the dimensions in the order of §3, then the tables by
`exposeId`. `verify` and `ship` read the pin and the thresholds only through
the plan, which its own digest binds, never from a bundle directory nothing
binds to the run. A plan whose Simulation pins nothing holds neither key, so
its bytes and its `planDigest` are the ones it had.

**A threshold is met** when the score it names exists and clears the floor,
compared exactly. A score that is `null` meets no threshold.

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
in the document and in the lines, and fail nothing.

**`ship`.** A run whose plan pins a profile is refused with `FLX-3109` when
no profile is handed over, before anything is written. Each emit's properties
are compared with the profile, and an evaluation that is not within (an
outside property, a refusal, or nothing scored at all) withholds the emit with
the check `profile.properties`, which names each property outside its
margin; an emit whose recorded fidelity input cannot be read is withheld with
`fidelity-input.readable`. With a committed slice as well, the slice's
gate runs as before and earns the signed record, and the emit ships only when
both references find it within. Then the scorecard is formed and gated:

- a missed table threshold, or a measured score below the emit's
  `minCredibility`, withholds that emit;
- a missed dimension or overall threshold withholds every emit whose scored
  properties feed the score; when none was scored, every emit with a property
  under it; and failing that every emit that declares a fidelity block, so a
  miss always withholds something;
- each gets a failed check, `scorecard.threshold` or `scorecard.credibility`,
  that names what was missed and the lowest-scoring property under it.

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
included, and "What Was Not Proven" says what it does not show. No shape flag
withholds anything.

## 6. Shape flags

`verify` and `ship` read every emit whose table the profile describes, and
in it every column the profile describes, whether or not a fidelity property
is declared on it (shape flags). A value is read as the profile reads its
column's domain: an exact decimal, integer milliseconds since the epoch, or
an NFC string. A value that cannot be read so refuses its column with
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

**The flags inform.** `met` does not read them, `verify` does not fail on
them, and `ship` withholds nothing for them. `verify`'s lines and
`EVIDENCE.md` say how many columns were checked and name each flagged
column.

## 7. What a scorecard does not show

- It compares the run with the profile's published aggregates, under the
  properties the bundle declares, and with nothing else. A dimension marked
  not measured was not checked at all.
- A censored property was not scored: what the profile withheld is unknown,
  not zero.
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
`1-0-0` means. Format `1-1-0` added the shape flags (§6):
`shapeFlags` gained the status `checked` and the flags per column. The
planned additions:

- **Intervals (sample-based verification):** a sampled `verify` already reports
  each estimate with its sample size and interval in its own leg
  (SAMPLE-SPEC.md); `intervals` gains the status `computed`, and the same
  estimates, once `ship` samples too.
- **Declared weights:** a property's `weight` takes the declared
  value.
- **The other dimensions:** a new property kind lands in the dimension §3
  names for it, which then stops being `not measured`.

## 9. Prior art

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
